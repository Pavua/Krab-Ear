#!/bin/bash
# Wave 50 — Phase D lightweight Swift agent recovery.
#
# Why this script (not launchd):
#   - SingleInstanceGuard.swift kills duplicate KrabEarAgent processes on startup.
#   - launchd KeepAlive + login-session-Dock-click → race condition: both spawn,
#     guard kills one, possibly the wrong one. Past Wave 41 attempted launchd
#     supervision for agent but rolled back due to TCC permission churn (new
#     process path → fresh Accessibility/Microphone prompts each restart).
#   - Safer: on-demand recovery from smoke-diagnostic routine OR user dock pin.
#
# Behavior:
#   1. Check if any KrabEarAgent process is running (.app bundle OR runtime path).
#   2. If absent → `open Krab Ear.app` (login-session-style launch, preserves TCC).
#   3. Wait 5s, re-check. If still absent → log error to .remember/agent-recovery.log.
#   4. If present → log line + exit 0.
#
# Exit codes:
#   0  agent running (was already, or successfully restarted)
#   1  agent absent and `open` failed
#   2  agent absent, `open` ran but agent didn't appear in 5s
#
# Usage:
#   scripts/ensure_agent_running.command                # interactive
#   scripts/ensure_agent_running.command --quiet        # no stdout, only log
#
# Designed to be safe under cron and from the e2e-smoke routine.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_PATH="$REPO_ROOT/Krab Ear.app"
LOG_FILE="$REPO_ROOT/.remember/agent-recovery.log"
mkdir -p "$(dirname "$LOG_FILE")"

QUIET=false
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=true ;;
    esac
done

log() {
    local ts msg
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    msg="$1"
    printf '%s  %s\n' "$ts" "$msg" >> "$LOG_FILE"
    if [ "$QUIET" = false ]; then
        printf '%s  %s\n' "$ts" "$msg"
    fi
}

count_agent_pids() {
    # Match both bundle path and runtime path.
    pgrep -fl "Krab Ear.app/Contents/MacOS/KrabEarAgent\|native/runtime/KrabEarAgent" \
        2>/dev/null | grep -v "ensure_agent_running\|grep\|pgrep" | wc -l | tr -d ' '
}

PRE_COUNT="$(count_agent_pids)"

if [ "$PRE_COUNT" -gt 0 ]; then
    log "OK  agent already running (pids=$PRE_COUNT)"
    exit 0
fi

log "WARN  agent absent — restoring via 'open $BUNDLE_PATH'"

if [ ! -d "$BUNDLE_PATH" ]; then
    log "FAIL  bundle path missing: $BUNDLE_PATH"
    exit 1
fi

if ! open "$BUNDLE_PATH"; then
    log "FAIL  'open' command returned non-zero"
    exit 1
fi

# Wait up to 5 s for the agent to register
for _ in 1 2 3 4 5; do
    sleep 1
    if [ "$(count_agent_pids)" -gt 0 ]; then
        log "OK  agent restored (pids=$(count_agent_pids))"
        exit 0
    fi
done

log "FAIL  agent still absent 5s after 'open'"
exit 2
