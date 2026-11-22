#!/bin/zsh
# C.1 MPS pool fix validation — A/B comparison.
#
# Measures gigaam_worker RSS growth WITH and WITHOUT torch.mps.empty_cache +
# gc.collect fix (PR #371, KRAB_EAR_DISABLE_MPS_POOL_FREE env-var bypass).
#
# IMPORTANT LIMITATION: get_diagnostics IPC calls do NOT trigger real GigaAM
# STT inference, so this script measures general backend + worker overhead only.
# For full H1 validation, run during 1h+ of actual dictation with CYCLES=50+
# and monitor gigaam_worker RSS manually:
#   watch -n 5 'ps -axo pid,rss,command | awk "/gigaam_worker/ && !/awk/ {printf \"%s MB  %s\n\", \$2/1024, \$3}"'
#
# Usage:
#   CYCLES=20 ./scripts/validate_c1_mps_fix.command
#   CYCLES=50 SLEEP_BETWEEN=3 ./scripts/validate_c1_mps_fix.command
#
# Output:
#   docs/measurements/c1-validation-<date>.md
#
# Requirements:
#   - Backend launchd service installed (scripts/install_backend_launchagent.command)
#   - Backend launchd label: ai.krab.ear.backend
#   - zsh, bc, ps, launchctl

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CYCLES="${CYCLES:-20}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-2}"
BACKEND_LABEL="${BACKEND_LABEL:-ai.krab.ear.backend}"
# safe_backend_restart.command перезапускает ТОЛЬКО ai.krab.ear.backend и label не принимает:
# при другом BACKEND_LABEL эксперимент перезапускал бы боевой backend, а отчёт называл бы
# нетронутый чужой job. Отказ — до любых изменений (env, рестарты, отчёт).
if [ "$BACKEND_LABEL" != "ai.krab.ear.backend" ]; then
    echo "ERROR: BACKEND_LABEL=$BACKEND_LABEL не поддерживается: рестарт идёт через safe_backend_restart.command, он управляет только ai.krab.ear.backend." >&2
    exit 2
fi
SOCK="${SOCK:-$HOME/Library/Application Support/KrabEar/krabear.sock}"
DATE=$(date +%Y-%m-%d-%H%M)
OUT_DIR="$ROOT_DIR/docs/measurements"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/c1-validation-$DATE.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_log() {
    printf '[validate_c1] %s\n' "$*" >&2
}

# 🔴 Скрипт переключает launchctl env глобально. Прежнее значение запоминаем и возвращаем
# при ЛЮБОМ выходе (ошибка рестарта, Ctrl+C, set -e) — иначе backend навсегда остаётся
# с выключенным исправлением, а следующий запуск вообще не знает об этом.
ENV_NAME="KRAB_EAR_DISABLE_MPS_POOL_FREE"
PREV_ENV="$(launchctl getenv "$ENV_NAME" 2>/dev/null || true)"
# С каким значением ЗАПУЩЕН backend после наших рестартов (__unchanged__ — мы его не перезапускали).
# Возврат launchctl env сам по себе работающий процесс не меняет.
RUNNING_ENV="__unchanged__"
restore_env() {
    if [ -n "$PREV_ENV" ]; then
        launchctl setenv "$ENV_NAME" "$PREV_ENV" 2>/dev/null || true
    else
        launchctl unsetenv "$ENV_NAME" 2>/dev/null || true
    fi
    if [ "$RUNNING_ENV" != "__unchanged__" ] && [ "$RUNNING_ENV" != "$PREV_ENV" ]; then
        _log "Backend запущен с экспериментальным значением $ENV_NAME='$RUNNING_ENV' — пробую безопасно вернуть исходное..."
        if ! "$ROOT_DIR/scripts/safe_backend_restart.command" >/dev/null 2>&1; then
            _log "ВНИМАНИЕ: состояние backend НЕ восстановлен (идёт запись/встреча или рестарт отказал)."
            _log "          Он продолжает работать с экспериментальной настройкой; перезапустите:"
            _log "          scripts/safe_backend_restart.command"
        fi
    fi
}
trap restore_env EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

IPC_FAILS=0

# Return gigaam_worker RSS sum in MB (integer). Returns 0 if no worker found.
get_worker_rss_mb() {
    ps -axo pid,rss,command 2>/dev/null \
        | awk '/gigaam_worker/ && !/awk/ {sum += $2} END {
            # macOS: rss is in KB
            print (sum > 0) ? int(sum/1024) : 0
        }'
}

# Return backend (Python main.py) RSS in MB.
get_backend_rss_mb() {
    ps -axo pid,rss,command 2>/dev/null \
        | awk '/KrabEar\/main\.py/ && !/awk/ {sum += $2} END {
            print (sum > 0) ? int(sum/1024) : 0
        }'
}

# Kick backend via IPC N times to generate load.
# Uses get_diagnostics — lightweight method that exercises IPC path without STT.
run_ipc_cycles() {
    local n="$1"
    local i=0
    while [ "$i" -lt "$n" ]; do
        i=$((i + 1))
        python3 -c "
import socket, json, os, sys

sock_path = os.path.expanduser('~/Library/Application Support/KrabEar/krabear.sock')
# Also try dev path as fallback
dev_path = os.path.expanduser('~/.krab_ear_data/backend.sock')

for path in [sock_path, dev_path]:
    if not os.path.exists(path):
        continue
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(path)
        req = json.dumps({'id': '$i', 'method': 'get_diagnostics', 'params': {}}) + '\n'
        s.sendall(req.encode())
        data = b''
        while True:
            chunk = s.recv(8192)
            if not chunk or b'\n' in data + chunk:
                data += chunk
                break
            data += chunk
        s.close()
        sys.exit(0)
    except Exception:
        pass

sys.exit(1)
" 2>/dev/null || IPC_FAILS=$((IPC_FAILS + 1))
        sleep "$SLEEP_BETWEEN"
    done
}

# Restart backend via launchd and wait for it to be ready.
restart_backend() {
    _log "Restarting backend ($BACKEND_LABEL) via safe_backend_restart..."
    # Слепой `kickstart -k` рвал бы живую диктовку; safe-скрипт отказывает под записью/встречей
    # и при неизвестном состоянии, UID не зашит, готовность подтверждается IPC-ping.
    if ! "$ROOT_DIR/scripts/safe_backend_restart.command"; then
        _log "ERROR: безопасный рестарт отказал (занято записью/встречей или состояние неизвестно)."
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

_log "C.1 MPS pool fix validation — $CYCLES cycles, sleep=${SLEEP_BETWEEN}s"
_log "Report: $REPORT"
_log ""
_log "NOTE: This script exercises the IPC path, NOT real GigaAM STT inference."
_log "      gigaam_worker may not even start unless a transcribe is triggered."
_log "      For full H1 signal: run during real dictation session with CYCLES=50+"
_log ""

if ! command -v launchctl &>/dev/null; then
    echo "ERROR: launchctl not found — macOS launchd required" >&2
    exit 1
fi

if ! command -v bc &>/dev/null; then
    echo "ERROR: bc not found — install via: brew install bc" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Write report header
# ---------------------------------------------------------------------------

cat > "$REPORT" << EOF
# C.1 MPS Pool Fix — A/B Validation Report

- **Date:** $DATE
- **Cycles per round:** $CYCLES
- **Sleep between cycles:** ${SLEEP_BETWEEN}s
- **Backend label:** $BACKEND_LABEL
- **Method:** KRAB_EAR_DISABLE_MPS_POOL_FREE env-var bypass

## Methodology

Two rounds of $CYCLES get_diagnostics IPC calls each. Between rounds, backend
is restarted to reset baseline. gigaam_worker RSS is sampled before and after
each round via \`ps\`.

**Known limitation:** get_diagnostics does NOT trigger GigaAM STT inference.
gigaam_worker subprocess only spawns when a real transcribe is requested.
RSS measurements below reflect Python backend process only if no actual
GigaAM transcription occurred. Best signal comes from real 1h dictation session.

---
EOF

# ---------------------------------------------------------------------------
# Round 1 — Control (fix DISABLED)
# ---------------------------------------------------------------------------

_log "=== ROUND 1: Control (KRAB_EAR_DISABLE_MPS_POOL_FREE=1) ==="
launchctl setenv KRAB_EAR_DISABLE_MPS_POOL_FREE 1
restart_backend || exit 1  # zsh: errexit из ФУНКЦИИ пропускает EXIT-trap, явный exit — нет
RUNNING_ENV="1"

PRE_CONTROL_WORKER=$(get_worker_rss_mb)
PRE_CONTROL_BACKEND=$(get_backend_rss_mb)
_log "Pre-run  — worker=${PRE_CONTROL_WORKER}MB  backend=${PRE_CONTROL_BACKEND}MB"

run_ipc_cycles "$CYCLES"

POST_CONTROL_WORKER=$(get_worker_rss_mb)
POST_CONTROL_BACKEND=$(get_backend_rss_mb)
_log "Post-run — worker=${POST_CONTROL_WORKER}MB  backend=${POST_CONTROL_BACKEND}MB"

DELTA_CONTROL_WORKER=$((POST_CONTROL_WORKER - PRE_CONTROL_WORKER))
DELTA_CONTROL_BACKEND=$((POST_CONTROL_BACKEND - PRE_CONTROL_BACKEND))

# Per-cycle MB (bc for float division)
PER_CYCLE_CONTROL_WORKER=$(echo "scale=2; $DELTA_CONTROL_WORKER / $CYCLES" | bc)
PER_CYCLE_CONTROL_BACKEND=$(echo "scale=2; $DELTA_CONTROL_BACKEND / $CYCLES" | bc)

cat >> "$REPORT" << EOF
## Round 1 — Control (fix DISABLED, KRAB_EAR_DISABLE_MPS_POOL_FREE=1)

| Metric         | Pre (MB) | Post (MB) | Delta (MB) | Per-cycle (MB) |
|----------------|----------|-----------|------------|----------------|
| gigaam_worker  | $PRE_CONTROL_WORKER | $POST_CONTROL_WORKER | $DELTA_CONTROL_WORKER | $PER_CYCLE_CONTROL_WORKER |
| backend total  | $PRE_CONTROL_BACKEND | $POST_CONTROL_BACKEND | $DELTA_CONTROL_BACKEND | $PER_CYCLE_CONTROL_BACKEND |

EOF

# ---------------------------------------------------------------------------
# Round 2 — Treatment (fix ENABLED)
# ---------------------------------------------------------------------------

_log ""
_log "=== ROUND 2: Treatment (fix ENABLED, env var unset) ==="
launchctl unsetenv KRAB_EAR_DISABLE_MPS_POOL_FREE
restart_backend || exit 1  # zsh: errexit из ФУНКЦИИ пропускает EXIT-trap, явный exit — нет
RUNNING_ENV=""

PRE_TREAT_WORKER=$(get_worker_rss_mb)
PRE_TREAT_BACKEND=$(get_backend_rss_mb)
_log "Pre-run  — worker=${PRE_TREAT_WORKER}MB  backend=${PRE_TREAT_BACKEND}MB"

run_ipc_cycles "$CYCLES"

POST_TREAT_WORKER=$(get_worker_rss_mb)
POST_TREAT_BACKEND=$(get_backend_rss_mb)
_log "Post-run — worker=${POST_TREAT_WORKER}MB  backend=${POST_TREAT_BACKEND}MB"

DELTA_TREAT_WORKER=$((POST_TREAT_WORKER - PRE_TREAT_WORKER))
DELTA_TREAT_BACKEND=$((POST_TREAT_BACKEND - PRE_TREAT_BACKEND))

PER_CYCLE_TREAT_WORKER=$(echo "scale=2; $DELTA_TREAT_WORKER / $CYCLES" | bc)
PER_CYCLE_TREAT_BACKEND=$(echo "scale=2; $DELTA_TREAT_BACKEND / $CYCLES" | bc)

cat >> "$REPORT" << EOF
## Round 2 — Treatment (fix ENABLED, KRAB_EAR_DISABLE_MPS_POOL_FREE unset)

| Metric         | Pre (MB) | Post (MB) | Delta (MB) | Per-cycle (MB) |
|----------------|----------|-----------|------------|----------------|
| gigaam_worker  | $PRE_TREAT_WORKER | $POST_TREAT_WORKER | $DELTA_TREAT_WORKER | $PER_CYCLE_TREAT_WORKER |
| backend total  | $PRE_TREAT_BACKEND | $POST_TREAT_BACKEND | $DELTA_TREAT_BACKEND | $PER_CYCLE_TREAT_BACKEND |

EOF

# ---------------------------------------------------------------------------
# Conclusion
# ---------------------------------------------------------------------------

WORKER_SAVINGS=$((DELTA_CONTROL_WORKER - DELTA_TREAT_WORKER))
BACKEND_SAVINGS=$((DELTA_CONTROL_BACKEND - DELTA_TREAT_BACKEND))
PER_CYCLE_SAVINGS=$(echo "scale=2; $WORKER_SAVINGS / $CYCLES" | bc)

if [ "$PRE_CONTROL_WORKER" -eq 0 ] || [ "$POST_CONTROL_WORKER" -eq 0 ] \
   || [ "$PRE_TREAT_WORKER" -eq 0 ] || [ "$POST_TREAT_WORKER" -eq 0 ] || [ "$IPC_FAILS" -gt 0 ]; then
    # Нулевой RSS = gigaam_worker не найден (его нет без реального STT); IPC-сбои = нагрузка не
    # создавалась. Из таких замеров нельзя делать вывод о гипотезе H1 ни в какую сторону.
    VERDICT="INCONCLUSIVE: gigaam_worker не обнаружен в замерах или IPC-вызовы падали (сбоев: $IPC_FAILS) — вывод о H1 невозможен; повторить во время реальной диктовки"
elif [ "$WORKER_SAVINGS" -gt 0 ]; then
    VERDICT="Fix REDUCES memory growth — H1 hypothesis confirmed (gigaam_worker path)"
elif [ "$WORKER_SAVINGS" -eq 0 ]; then
    VERDICT="No difference in gigaam_worker RSS — H1 not primary cause; investigate H2 (model re-load) or H3 (pyannote)"
else
    VERDICT="Treatment used MORE memory — possible measurement noise or cold-start artefact; re-run with real STT cycles"
fi

cat >> "$REPORT" << EOF
## Conclusion

| Metric                  | Value       |
|-------------------------|-------------|
| Worker growth reduction | $WORKER_SAVINGS MB total over $CYCLES cycles |
| Worker per-cycle saving | $PER_CYCLE_SAVINGS MB |
| Backend growth reduction| $BACKEND_SAVINGS MB total |

**VERDICT:** $VERDICT

### Next steps if fix not confirmed

- Run with \`KRAB_EAR_TRACE_GIGAAM_MEM=1\` during real dictation to get inline RSS logs
- Compare \`memory_soak_test.command\` output before/after enabling the env var
- Check H2: model is re-loaded on each transcribe (check \`_MODEL is None\` guard)
- Check H3: pyannote VAD accumulates segments — add explicit del + gc after longform
EOF

# ---------------------------------------------------------------------------
# Summary to terminal
# ---------------------------------------------------------------------------

_log ""
_log "=============================="
_log "C.1 A/B Validation Summary"
_log "=============================="
_log "Control   (fix OFF): worker delta=${DELTA_CONTROL_WORKER}MB, backend delta=${DELTA_CONTROL_BACKEND}MB"
_log "Treatment (fix ON):  worker delta=${DELTA_TREAT_WORKER}MB, backend delta=${DELTA_TREAT_BACKEND}MB"
_log "Worker savings: $WORKER_SAVINGS MB total ($PER_CYCLE_SAVINGS MB/cycle)"
_log "VERDICT: $VERDICT"
_log ""
_log "Report written: $REPORT"
