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
#   3. Wait 15s, re-check. If still absent → log error to .remember/agent-recovery.log.
#   4. If present → log line + exit 0.
#
# Exit codes:
#   0  agent running (was already, or successfully restarted)
#   1  agent absent and `open` failed
#   2  agent absent, `open` ran but agent didn't appear in 15s
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
    # Wave 65 fix: pgrep exits 1 when no process matches, which under `set -e`
    # aborted the entire script BEFORE the auto-launch block could run.
    # Root cause: `pgrep ... | grep ...` — pgrep exit-1 propagates through the
    # pipeline's last-command-status rule and the `set -e` trap fires immediately,
    # killing the script before PRE_COUNT is assigned, so auto-launch NEVER ran.
    # Fix: capture pgrep output in a variable with `|| true` so a "no match"
    # result is an empty string, not a fatal exit.
    # 🔴 2026-07-23: pgrep на macOS использует ERE, а здесь стояла BRE-альтернация
    # `\|` — то есть искался ЛИТЕРАЛ "KrabEarAgent|native". Скрипт не находил
    # агента НИКОГДА: при живом агенте делал лишний `open` (риск дубля процесса
    # против SingleInstanceGuard), а после успешного старта всё равно рапортовал
    # `FAIL agent still absent` — 12 подряд с 18-07 и ни одного успеха.
    # Увеличение окна ожидания (рекомендация трёх смок-отчётов) лечило бы симптом.
    local raw
    raw="$(pgrep -fl "Krab Ear.app/Contents/MacOS/KrabEarAgent|native/runtime/KrabEarAgent" 2>/dev/null || true)"
    # Filter out this script itself, grep, and pgrep from the list.
    echo "$raw" | grep -v "ensure_agent_running\|grep\|pgrep" | grep -c . || true
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

# Wave 50 follow-up: agent takes 10-15s to launch + register в pgrep
# после `open Krab Ear.app` на M4 Max. 5s timeout was too short.
# Wait up to 15 s for the agent to register
for _ in $(seq 1 15); do
    sleep 1
    if [ "$(count_agent_pids)" -gt 0 ]; then
        log "OK  agent restored (pids=$(count_agent_pids))"
        exit 0
    fi
done

log "FAIL  agent still absent 15s after 'open'"
exit 2
