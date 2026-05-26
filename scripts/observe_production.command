#!/bin/zsh
# ==============================================================================
# Krab Ear — Production Observation Snapshot (Wave 387)
#
# One-shot 30-second read-only health snapshot of the running production system.
# НЕ запускает и НЕ останавливает никаких сервисов.
#
# Запуск: ./scripts/observe_production.command
#         ./scripts/observe_production.command --dry-run  (alias, same behaviour)
#
# Выход:  0  — нет критических проблем
#         1  — есть ≥1 🚨 (CRITICAL)
# ==============================================================================

# ──────────────────────── constants ────────────────────────
KRAB_DATA_DIR="${KRAB_EAR_DATA_DIR:-$HOME/Library/Application Support/KrabEar}"
LAUNCHAGENT_LABEL="ai.krab.ear.backend"
AGENT_LABEL="ai.krab.ear.agent"
BUNDLE_BIN="/Applications/Krab Ear.app/Contents/MacOS/KrabEarAgent"
RUNTIME_BIN="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/native/runtime/KrabEarAgent"
PLIST_BACKEND="$HOME/Library/LaunchAgents/${LAUNCHAGENT_LABEL}.plist"
PLIST_AGENT="$HOME/Library/LaunchAgents/${AGENT_LABEL}.plist"
CRASH_DIR="$HOME/Library/Logs/DiagnosticReports"
SECRETS_FILE="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/.secrets"

# ANSI colours
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

CRITICAL_COUNT=0

# ──────────────────────── helpers ────────────────────────

_ok()   { printf "${GREEN}✅${NC} %s\n"  "$*"; }
_warn() { printf "${YELLOW}⚠️ ${NC}  %s\n" "$*"; }
_crit() { printf "${RED}🚨${NC} %s\n"  "$*"; CRITICAL_COUNT=$((CRITICAL_COUNT+1)); }
_info() { printf "   %s\n" "$*"; }

_section() {
    echo ""
    printf "${CYAN}${BOLD}── %s ──${NC}\n" "$1"
}

# Try to get a PID for a pattern; returns "" if not found
_pid_for() { pgrep -f "$1" 2>/dev/null | head -1 || true; }

# Process uptime in human-readable form (macOS ps -o etime)
_proc_uptime() {
    local pid=$1
    ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ' || echo "?"
}

# RSS in MB
_proc_rss_mb() {
    local pid=$1
    local kb
    kb=$(ps -p "$pid" -o rss= 2>/dev/null | tr -d ' ' || echo 0)
    echo $(( kb / 1024 ))
}

# Check TCP port reachable
_port_up() {
    timeout 1 bash -c "echo >/dev/tcp/127.0.0.1/$1" 2>/dev/null
}

# ──────────────────────── main ────────────────────────

echo ""
printf "${BOLD}🔍 Krab Ear Production Snapshot — $(date '+%Y-%m-%d %H:%M:%S %Z')${NC}\n"
printf "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

# ── 1. System uptime
_section "System"
UPTIME_OUT="$(uptime 2>/dev/null || sysctl -n kern.boottime 2>/dev/null || echo '?')"
_info "Host uptime : $UPTIME_OUT"
_info "Date        : $(date '+%a %Y-%m-%d %H:%M:%S %Z')"

# ── 2. Backend launchd state + PID + uptime
_section "Backend (launchd)"
BACKEND_PID=""
if launchctl list "$LAUNCHAGENT_LABEL" &>/dev/null; then
    LAUNCHD_STATE="$(launchctl list "$LAUNCHAGENT_LABEL" 2>/dev/null)"
    LAUNCHD_PID="$(echo "$LAUNCHD_STATE" | grep '"PID"' | grep -o '[0-9]*' | head -1 || true)"
    LAUNCHD_STATUS="$(echo "$LAUNCHD_STATE" | grep '"LastExitStatus"' | grep -o '[0-9]*' | head -1 || true)"
    if [ -n "$LAUNCHD_PID" ] && [ "$LAUNCHD_PID" != "0" ]; then
        BACKEND_PID="$LAUNCHD_PID"
        RSS="$(_proc_rss_mb "$BACKEND_PID")"
        UTIME="$(_proc_uptime "$BACKEND_PID")"
        _ok "Launchd: RUNNING  PID=$BACKEND_PID  uptime=$UTIME  RSS=${RSS} MB"
    else
        _crit "Launchd: label registered but NOT running (LastExitStatus=$LAUNCHD_STATUS)"
    fi
else
    # Fallback: look for python main.py
    BACKEND_PID="$(_pid_for 'KrabEar/main.py')"
    if [ -n "$BACKEND_PID" ]; then
        RSS="$(_proc_rss_mb "$BACKEND_PID")"
        UTIME="$(_proc_uptime "$BACKEND_PID")"
        _warn "Launchd label absent — backend running standalone  PID=$BACKEND_PID  uptime=$UTIME  RSS=${RSS} MB"
    else
        _crit "Backend NOT running (no launchd label, no python process)"
    fi
fi

# ── 3. Agent process
_section "Agent (KrabEarAgent)"
AGENT_PID="$(_pid_for 'KrabEarAgent')"
if [ -n "$AGENT_PID" ]; then
    AGENT_UTIME="$(_proc_uptime "$AGENT_PID")"
    # Get binary path from lsof or ps
    AGENT_BIN="$(ps -p "$AGENT_PID" -o comm= 2>/dev/null | tr -d ' ' || echo '?')"
    # Try codesignature UUID
    AGENT_UUID="$(codesign -dv "$BUNDLE_BIN" 2>&1 | grep 'CDHash' | head -1 | awk '{print $NF}' 2>/dev/null || echo '?')"
    _ok "Agent: PID=$AGENT_PID  uptime=$AGENT_UTIME  bin=$AGENT_BIN"
    _info "       CDHash(bundle): $AGENT_UUID"
else
    _crit "Agent (KrabEarAgent) NOT running"
fi

# ── 4. Two-binary drift check
_section "Binary Drift Check"
if [ -f "$BUNDLE_BIN" ] && [ -f "$RUNTIME_BIN" ]; then
    BUNDLE_HASH="$(codesign -dv "$BUNDLE_BIN" 2>&1 | grep 'CDHash' | awk '{print $NF}' || shasum -a 256 "$BUNDLE_BIN" | awk '{print substr($1,1,16)}')"
    RUNTIME_HASH="$(codesign -dv "$RUNTIME_BIN" 2>&1 | grep 'CDHash' | awk '{print $NF}' || shasum -a 256 "$RUNTIME_BIN" | awk '{print substr($1,1,16)}')"
    BUNDLE_MTIME="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$BUNDLE_BIN" 2>/dev/null || echo '?')"
    RUNTIME_MTIME="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$RUNTIME_BIN" 2>/dev/null || echo '?')"
    if [ "$BUNDLE_HASH" = "$RUNTIME_HASH" ]; then
        _ok "Binaries in sync  CDHash=$BUNDLE_HASH"
        _info "bundle  mtime: $BUNDLE_MTIME"
        _info "runtime mtime: $RUNTIME_MTIME"
    else
        _crit "TWO-BINARY DRIFT — bundle and runtime differ!"
        _info "bundle  : $BUNDLE_HASH  ($BUNDLE_MTIME)"
        _info "runtime : $RUNTIME_HASH  ($RUNTIME_MTIME)"
        _info "Fix: cp \"$BUNDLE_BIN\" \"$RUNTIME_BIN\" && codesign -s - -f \"$RUNTIME_BIN\""
    fi
elif [ -f "$BUNDLE_BIN" ] && [ ! -f "$RUNTIME_BIN" ]; then
    _warn "runtime binary absent at $RUNTIME_BIN (only bundle exists)"
elif [ ! -f "$BUNDLE_BIN" ] && [ -f "$RUNTIME_BIN" ]; then
    _warn "bundle binary absent at $BUNDLE_BIN (only runtime exists)"
else
    _warn "neither bundle nor runtime binary found at expected paths"
fi

# ── 5. Backend RSS (re-reported in context)
_section "Memory"
if [ -n "$BACKEND_PID" ]; then
    RSS_MB="$(_proc_rss_mb "$BACKEND_PID")"
    if [ "$RSS_MB" -gt 400 ]; then
        _crit "Backend RSS ${RSS_MB} MB — possible memory leak (>400 MB threshold)"
    elif [ "$RSS_MB" -gt 200 ]; then
        _warn "Backend RSS ${RSS_MB} MB — elevated (>200 MB)"
    else
        _ok "Backend RSS ${RSS_MB} MB — normal"
    fi
else
    _info "Backend not running — no RSS"
fi

# ── 6. LM Studio
_section "LM Studio (port 1234)"
if _port_up 1234; then
    LMS_MODELS="$(curl -s --max-time 3 http://127.0.0.1:1234/api/v1/models 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m.get('id','?') for m in d.get('data',[][:3])))" 2>/dev/null \
        || echo "?")"
    if [ -z "$LMS_MODELS" ] || [ "$LMS_MODELS" = "?" ]; then
        _warn "LM Studio reachable but no models returned (or /api/v1/models failed)"
    else
        _ok "LM Studio: reachable  model(s): $LMS_MODELS"
    fi
else
    _warn "LM Studio: NOT reachable on port 1234"
fi

# ── 7. GigaAM worker
_section "GigaAM Worker"
GIGAAM_PID="$(_pid_for 'gigaam_worker')"
if [ -z "$GIGAAM_PID" ]; then
    GIGAAM_PID="$(_pid_for 'gigaam')"
fi
if [ -n "$GIGAAM_PID" ]; then
    GIGAAM_RSS="$(_proc_rss_mb "$GIGAAM_PID")"
    _info "GigaAM worker: PID=$GIGAAM_PID  RSS=${GIGAAM_RSS} MB"
    if [ "$GIGAAM_RSS" -gt 1400 ]; then
        _warn "GigaAM RSS ${GIGAAM_RSS} MB > 1.4 GB — possible duplicate worker (Wave 69)"
    else
        _ok "GigaAM worker RSS ${GIGAAM_RSS} MB"
    fi
else
    _info "GigaAM worker: not running (normal when idle)"
fi

# ── 8. mlx_lm.server orphan (Wave 316 RotorQuant)
_section "mlx_lm.server Orphan Check"
MLX_LM_PID="$(_pid_for 'mlx_lm.server')"
if [ -n "$MLX_LM_PID" ]; then
    MLX_LM_RSS="$(_proc_rss_mb "$MLX_LM_PID")"
    _crit "mlx_lm.server orphan running! PID=$MLX_LM_PID  RSS=${MLX_LM_RSS} MB — kills with: kill $MLX_LM_PID"
else
    _ok "No mlx_lm.server orphan"
fi

# ── 9. Disk space
_section "Disk Space"
DATA_DIR_USAGE="$(du -sh "$KRAB_DATA_DIR" 2>/dev/null | awk '{print $1}' || echo '?')"
AVAIL="$(df -h "$KRAB_DATA_DIR" 2>/dev/null | tail -1 | awk '{print $4}' || echo '?')"
_info "Data dir usage : $DATA_DIR_USAGE  ($KRAB_DATA_DIR)"
_info "Disk available : $AVAIL"
# Warn if < 2 GB (same threshold as DiskSpaceMonitor)
AVAIL_BYTES="$(df "$KRAB_DATA_DIR" 2>/dev/null | tail -1 | awk '{print $4}' || echo 9999999)"
if [ "$AVAIL_BYTES" -lt 4194304 ] 2>/dev/null; then  # 4194304 × 512B = ~2 GB
    _crit "Disk free < 2 GB — DiskSpaceMonitor will warn"
else
    _ok "Disk free sufficient (${AVAIL})"
fi

# ── 10. Last backend log
_section "Backend Log"
LOG_DIR="$KRAB_DATA_DIR/logs"
BACKEND_LOG="$(ls -t "$LOG_DIR"/krab_ear_backend*.log 2>/dev/null | head -1 || echo '')"
if [ -z "$BACKEND_LOG" ]; then
    BACKEND_LOG="$(ls -t "$LOG_DIR"/*.log 2>/dev/null | head -1 || echo '')"
fi
if [ -n "$BACKEND_LOG" ]; then
    LOG_MTIME="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$BACKEND_LOG" 2>/dev/null || echo '?')"
    _info "Log file : $BACKEND_LOG"
    _info "Log mtime: $LOG_MTIME"
    _info "Last 3 lines:"
    tail -3 "$BACKEND_LOG" 2>/dev/null | while IFS= read -r line; do
        _info "  $line"
    done
else
    _warn "No backend log found in $LOG_DIR"
fi

# ── 11. Last 3 history entries
_section "History Store"
HISTORY_NDJSON="$KRAB_DATA_DIR/history.ndjson"
if [ -f "$HISTORY_NDJSON" ]; then
    LINE_COUNT="$(wc -l < "$HISTORY_NDJSON" | tr -d ' ')"
    SIZE="$(du -sh "$HISTORY_NDJSON" 2>/dev/null | awk '{print $1}')"
    _ok "history.ndjson: ${LINE_COUNT} lines, ${SIZE}"
    _info "Last 3 entries:"
    tail -3 "$HISTORY_NDJSON" 2>/dev/null \
        | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        ts   = d.get('ts','?')[:19]
        lang = d.get('language','?')
        dur  = d.get('duration_s','?')
        txt  = (d.get('text') or '(tombstone)')[:60]
        print(f'    {ts}  [{lang}]  {dur}s  {txt!r}')
    except Exception:
        print('    (parse error)', line[:80])
" 2>/dev/null || _info "  (python3 not available for JSON parsing)"
else
    _warn "history.ndjson not found at $HISTORY_NDJSON"
fi

# ── 12. Settings backup files
_section "Settings Backups"
SETTINGS_DIR="$KRAB_DATA_DIR"
BACKUPS="$(ls -t "$SETTINGS_DIR"/settings*.bak "$SETTINGS_DIR"/settings_backup*.json 2>/dev/null | head -5 || true)"
if [ -n "$BACKUPS" ]; then
    _info "Last 5 settings backups:"
    echo "$BACKUPS" | while IFS= read -r f; do
        MTIME="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$f" 2>/dev/null || echo '?')"
        _info "  $MTIME  $(basename "$f")"
    done
else
    _info "No settings backup files found in $SETTINGS_DIR"
fi

# ── 13. Recent crash reports (24h)
_section "Crash Reports (last 24h)"
RECENT_CRASHES="$(find "$CRASH_DIR" -name "KrabEar*" -o -name "KrabEarAgent*" 2>/dev/null \
    | xargs -I{} stat -f '%m %N' {} 2>/dev/null \
    | awk -v cutoff="$(date -v-24H +%s)" '$1 >= cutoff {print $0}' \
    | sort -rn | head -5 || true)"
if [ -n "$RECENT_CRASHES" ]; then
    CRASH_COUNT="$(echo "$RECENT_CRASHES" | wc -l | tr -d ' ')"
    _crit "${CRASH_COUNT} crash report(s) in the last 24h:"
    echo "$RECENT_CRASHES" | while IFS= read -r line; do
        TS="$(echo "$line" | awk '{print $1}')"
        FILE="$(echo "$line" | cut -d' ' -f2-)"
        _info "  $(date -r "$TS" '+%Y-%m-%d %H:%M:%S')  $(basename "$FILE")"
    done
else
    _ok "No KrabEar crash reports in last 24h"
fi

# ── 14. Open file descriptors
_section "File Descriptors"
if [ -n "$BACKEND_PID" ]; then
    FD_COUNT="$(lsof -p "$BACKEND_PID" 2>/dev/null | wc -l | tr -d ' ' || echo '?')"
    if [ "$FD_COUNT" != '?' ] && [ "$FD_COUNT" -gt 1000 ]; then
        _warn "Backend FD count: ${FD_COUNT} (>1000 — possible fd leak)"
    else
        _ok "Backend FD count: ${FD_COUNT}"
    fi
else
    _info "Backend not running — skipping FD check"
fi

# ── 15. Sentry token availability
_section "Sentry"
SENTRY_FOUND=0
if [ -n "${SENTRY_AUTH_TOKEN:-}" ]; then
    _ok "SENTRY_AUTH_TOKEN present in environment"
    SENTRY_FOUND=1
elif [ -f "$SECRETS_FILE" ] && grep -q 'SENTRY_AUTH_TOKEN' "$SECRETS_FILE" 2>/dev/null; then
    _ok "SENTRY_AUTH_TOKEN found in .secrets"
    SENTRY_FOUND=1
elif [ -f "$HOME/.secrets" ] && grep -q 'SENTRY_AUTH_TOKEN' "$HOME/.secrets" 2>/dev/null; then
    _ok "SENTRY_AUTH_TOKEN found in ~/.secrets"
    SENTRY_FOUND=1
fi
if [ "$SENTRY_FOUND" -eq 0 ]; then
    _warn "Sentry: token NOT found (crash reporting may be silent)"
fi

# ── 16. macOS automatic update reboot setting
_section "macOS Auto-Update"
AUTO_UPDATE="$(defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates 2>/dev/null \
    || defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled 2>/dev/null \
    || echo '?')"
RESTART_REQS="$(defaults read /Library/Preferences/com.apple.SoftwareUpdate RestartRequired 2>/dev/null || echo '0')"
if [ "$AUTO_UPDATE" = "1" ]; then
    _warn "macOS auto-update reboot ENABLED — will reboot ~14:04 CEST daily (known VPN reboot cause)"
else
    _ok "macOS auto-update reboot: off (value=${AUTO_UPDATE})"
fi

# ── 17. VPN plist KeepAlive
_section "VPN / LaunchAgent KeepAlive"
if [ -f "$PLIST_BACKEND" ]; then
    KEEPALIVE="$(defaults read "$PLIST_BACKEND" KeepAlive 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Print KeepAlive" "$PLIST_BACKEND" 2>/dev/null \
        || echo '?')"
    if [ "$KEEPALIVE" = "1" ] || [ "$KEEPALIVE" = "true" ] || echo "$KEEPALIVE" | grep -qi 'true'; then
        _ok "Backend plist KeepAlive=true"
    else
        _crit "Backend plist KeepAlive=$KEEPALIVE — backend will NOT auto-restart after reboot/crash!"
    fi
else
    _warn "Backend LaunchAgent plist not installed ($PLIST_BACKEND)"
fi

# ── 18. History stats
_section "History / Settings Files"
if [ -f "$HISTORY_NDJSON" ]; then
    HIST_LINES="$(wc -l < "$HISTORY_NDJSON" | tr -d ' ')"
    HIST_SIZE="$(du -sh "$HISTORY_NDJSON" 2>/dev/null | awk '{print $1}')"
    _info "history.ndjson : $HIST_LINES lines  /  $HIST_SIZE"
fi

# ── 19. Settings.json
SETTINGS_JSON="$KRAB_DATA_DIR/settings.json"
if [ -f "$SETTINGS_JSON" ]; then
    KEY_COUNT="$(python3 -c "import json; d=json.load(open('$SETTINGS_JSON')); print(len(d))" 2>/dev/null || echo '?')"
    _ok "settings.json: $KEY_COUNT keys"
else
    _warn "settings.json not found"
fi

# ── 20. Current LLM model
_section "LLM Model (settings)"
if [ -f "$SETTINGS_JSON" ]; then
    LLM_MODEL="$(python3 -c "
import json
d = json.load(open('$SETTINGS_JSON'))
print(d.get('llm_model') or d.get('rewriter_model') or '(not set)')
" 2>/dev/null || echo '?')"
    _info "LLM model: $LLM_MODEL"
else
    _info "settings.json absent — cannot determine LLM model"
fi

# ──────────────────────── summary ────────────────────────
echo ""
printf "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
if [ "$CRITICAL_COUNT" -eq 0 ]; then
    printf "${GREEN}${BOLD}✅  All checks passed — no critical issues${NC}\n"
    exit 0
else
    printf "${RED}${BOLD}🚨  ${CRITICAL_COUNT} critical issue(s) found — review output above${NC}\n"
    exit 1
fi
