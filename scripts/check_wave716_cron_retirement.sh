#!/usr/bin/env bash
# check_wave716_cron_retirement.sh
#
# Wave 788 helper — check all 5 retirement criteria from docs/WAVE_716_CRON_RETIREMENT.md
# before removing the kill_dup_gigaam.command cron entry.
#
# Usage:
#   bash scripts/check_wave716_cron_retirement.sh
#
# Exit codes:
#   0 — all criteria PASS
#   1 — one or more criteria FAIL
#
# NOTE: does NOT retire the cron itself. To retire (after all PASS):
#   crontab -l > /tmp/cron-pre-w716-retirement.bak
#   crontab -l | grep -v "kill_dup_gigaam.command" | crontab -

set -uo pipefail

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
_green() { printf '\033[0;32m%s\033[0m' "$*"; }
_red()   { printf '\033[0;31m%s\033[0m' "$*"; }
_bold()  { printf '\033[1m%s\033[0m' "$*"; }

PASS="$(_green PASS)"
FAIL="$(_red FAIL)"
MANUAL="$(_bold MANUAL)"

# Collect results for summary table
declare -a LABELS
declare -a RESULTS
declare -a DETAILS

overall=0   # 0 = all good so far; will flip to 1 on first FAIL

_record() {
    local idx="$1" label="$2" result="$3" detail="$4"
    LABELS[$idx]="$label"
    RESULTS[$idx]="$result"
    DETAILS[$idx]="$detail"
}

# ---------------------------------------------------------------------------
# Print header
# ---------------------------------------------------------------------------
echo ""
_bold "=== Wave 716 cron retirement criteria check ==="
echo ""
echo "Reference: docs/WAVE_716_CRON_RETIREMENT.md"
echo "Date:      $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

# ---------------------------------------------------------------------------
# Criterion 1 — Backend is running v2.0.5
# ---------------------------------------------------------------------------
echo "$(_bold '[ 1/5 ] Backend version = 2.0.5')"

BACKEND_LOG="${HOME}/Library/Logs/KrabEar/backend.log"
VERSION_FOUND=""

if [ -f "$BACKEND_LOG" ]; then
    # Look for any line mentioning "version 2.0.5" (startup banner or Sentry tag)
    VERSION_FOUND="$(grep -m1 "version 2\.0\.5\|krab-ear@2\.0\.5\|2\.0\.5" "$BACKEND_LOG" 2>/dev/null | tail -1 || true)"
fi

if [ -n "$VERSION_FOUND" ]; then
    echo "  $PASS — found: ${VERSION_FOUND:0:120}"
    _record 0 "Backend v2.0.5" "$PASS" "matched in backend.log"
else
    echo "  $FAIL — 'version 2.0.5' not found in ${BACKEND_LOG}"
    echo "         (production is likely still v2.0.4; expected failure until v2.0.5 is deployed)"
    _record 0 "Backend v2.0.5" "$FAIL" "not found in backend.log"
    overall=1
fi
echo ""

# ---------------------------------------------------------------------------
# Criterion 2 — Exactly 1 gigaam_worker process
# ---------------------------------------------------------------------------
echo "$(_bold '[ 2/5 ] Exactly 1 gigaam_worker process')"

WORKER_COUNT=$(pgrep -fl "gigaam_worker.py" 2>/dev/null | wc -l | tr -d ' ')
WORKER_PIDS=$(pgrep -fl "gigaam_worker.py" 2>/dev/null || true)

if [ "$WORKER_COUNT" -eq 1 ]; then
    echo "  $PASS — exactly 1 worker (pid line: ${WORKER_PIDS})"
    _record 1 "Exactly 1 gigaam_worker" "$PASS" "count=$WORKER_COUNT"
elif [ "$WORKER_COUNT" -eq 0 ]; then
    echo "  $FAIL — 0 gigaam_worker processes found (backend/rest-server may not be running)"
    _record 1 "Exactly 1 gigaam_worker" "$FAIL" "count=0 (not running?)"
    overall=1
else
    echo "  $FAIL — $WORKER_COUNT workers found (expected exactly 1):"
    echo "$WORKER_PIDS" | while IFS= read -r line; do echo "         $line"; done
    _record 1 "Exactly 1 gigaam_worker" "$FAIL" "count=$WORKER_COUNT (duplicate!)"
    overall=1
fi
echo ""

# ---------------------------------------------------------------------------
# Criterion 3 — Cron has not triggered any kills
# ---------------------------------------------------------------------------
echo "$(_bold '[ 3/5 ] No cron kills in logs (no \"killing dup gigaam\" entries)')"

CRON_KILL_LOG="/tmp/kill_dup_gigaam.log"
CRON_KILL_FOUND=0
CRON_KILL_SOURCE=""

# Check the tmp log file (if crontab redirects there)
if [ -f "$CRON_KILL_LOG" ]; then
    if grep -q "killing dup" "$CRON_KILL_LOG" 2>/dev/null; then
        CRON_KILL_COUNT=$(grep -c "killing dup" "$CRON_KILL_LOG" 2>/dev/null || echo "?")
        CRON_KILL_FOUND=1
        CRON_KILL_SOURCE="$CRON_KILL_LOG (${CRON_KILL_COUNT} occurrences)"
    fi
fi

# Also check crontab for the entry itself
CRON_ENTRY=$(crontab -l 2>/dev/null | grep "kill_dup_gigaam" || true)

if [ $CRON_KILL_FOUND -eq 1 ]; then
    echo "  $FAIL — kill events found in $CRON_KILL_SOURCE"
    echo "         Last few kill lines:"
    grep "killing dup" "$CRON_KILL_LOG" | tail -3 | while IFS= read -r line; do
        echo "         $line"
    done
    _record 2 "No cron kills in logs" "$FAIL" "kills found in ${CRON_KILL_SOURCE}"
    overall=1
else
    if [ -f "$CRON_KILL_LOG" ]; then
        echo "  $PASS — log exists but contains no kill events"
        _record 2 "No cron kills in logs" "$PASS" "log clean (no 'killing dup' entries)"
    else
        echo "  $PASS — $CRON_KILL_LOG does not exist (cron never fired a kill, or log not configured)"
        _record 2 "No cron kills in logs" "$PASS" "log absent — no kills recorded"
    fi
    if [ -n "$CRON_ENTRY" ]; then
        echo "         Note: cron entry IS active: ${CRON_ENTRY}"
    else
        echo "         Note: kill_dup_gigaam cron entry NOT found in crontab (already retired?)"
    fi
fi
echo ""

# ---------------------------------------------------------------------------
# Criterion 4 — rest_server.py log shows skip_gigaam_warmup=True
# ---------------------------------------------------------------------------
echo "$(_bold '[ 4/5 ] REST server log confirms skip_gigaam_warmup=True path')"

REST_LOG="${HOME}/Library/Logs/KrabEar/rest_server.log"
SKIP_FOUND=""

if [ -f "$REST_LOG" ]; then
    SKIP_FOUND="$(grep -m1 "skip_gigaam_warmup\|GigaAM warmup пропущен" "$REST_LOG" 2>/dev/null | tail -1 || true)"
fi

if [ -n "$SKIP_FOUND" ]; then
    echo "  $PASS — found: ${SKIP_FOUND:0:120}"
    _record 3 "REST skip_gigaam_warmup=True" "$PASS" "matched in rest_server.log"
else
    echo "  $FAIL — 'skip_gigaam_warmup' or 'GigaAM warmup пропущен' not found in ${REST_LOG}"
    if [ ! -f "$REST_LOG" ]; then
        echo "         (log file does not exist — rest_server may not have started yet)"
    else
        echo "         (rest_server.log exists but lacks the skip confirmation line)"
        echo "         Expected: 'GigaAM warmup пропущен (skip_gigaam_warmup=True)'"
    fi
    echo "         This PASS requires v2.0.5 to be running with PR #619 changes."
    _record 3 "REST skip_gigaam_warmup=True" "$FAIL" "not found in rest_server.log"
    overall=1
fi
echo ""

# ---------------------------------------------------------------------------
# Criterion 5 — Sentry (manual check)
# ---------------------------------------------------------------------------
echo "$(_bold '[ 5/5 ] No Sentry gigaam_worker_crashed events in last 7 days')"
echo "  $MANUAL — Requires Sentry dashboard access (API auth not available here)."
echo ""
echo "  Manual steps:"
echo "    1. Open https://de.sentry.io/organizations/po-zm/issues/"
echo "    2. Select project: krab-ear-backend"
echo "    3. Filter by: gigaam_worker_crashed OR gigaam.oom OR stt.gigaam_*"
echo "    4. Set time range: last 7 days"
echo "    5. Verify: zero new events"
echo ""
echo "  If zero events → mark PASS manually and proceed with retirement."
_record 4 "Sentry: no gigaam errors (7d)" "$MANUAL" "check dashboard at de.sentry.io/organizations/po-zm"
echo ""

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_bold "SUMMARY"
echo ""
printf "  %-4s  %-36s  %-6s  %s\n" "No." "Criterion" "Result" "Detail"
echo "  ─────────────────────────────────────────────────────────"
for i in 0 1 2 3 4; do
    printf "  %-4s  %-36s  %-6s  %s\n" \
        "$((i+1))." \
        "${LABELS[$i]}" \
        "${RESULTS[$i]}" \
        "${DETAILS[$i]}"
done
echo ""

if [ $overall -eq 0 ]; then
    _bold "Overall: $(_green 'ALL AUTOMATED CRITERIA PASS')"
    echo ""
    echo "  Remaining action: verify criterion 5 (Sentry) manually."
    echo "  Once confirmed, retire with:"
    echo ""
    echo "    crontab -l > /tmp/cron-pre-w716-retirement.bak"
    echo "    crontab -l | grep -v 'kill_dup_gigaam.command' | crontab -"
else
    _bold "Overall: $(_red 'ONE OR MORE CRITERIA FAIL — do NOT retire yet')"
    echo ""
    echo "  Fix failing criteria, then re-run this script."
fi

echo ""
exit $overall
