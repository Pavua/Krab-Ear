#!/bin/bash
# run_e2e_smokes.command — one-command E2E smoke runner for Krab Ear.
#
# Spins up a THROWAWAY dev backend on a temp data-dir (so it never touches your
# real history or the production launchd backend), runs both live socket smokes
# against it, prints PASS/FAIL, then tears the backend down. Safe to run anytime.
#
#   scripts/run_e2e_smokes.command
#
# What it runs:
#   - scripts/e2e_ipc_smoke.py      : 37 user-facing methods + 5 CRUD round-trips,
#                                     asserts output sanity (caught the
#                                     get_topic_timeline crash).
#   - scripts/e2e_privacy_gates.py  : canary — privacy mode must suppress all
#                                     transcript-derived output (0 leaks).
#
# Exit 0 only if BOTH smokes pass.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

VENV="$REPO/.venv_krab_ear"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "ERROR: venv python not found at $PY (run setup first)"; exit 1; }

DATADIR="$(mktemp -d /tmp/krab_ear_e2e.XXXXXX)"
SOCK="$DATADIR/krabear.sock"
LOG="$DATADIR/backend.log"

# Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
# иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
export KRAB_EAR_PRIVACY_AUDIT_DIR="$DATADIR"

cleanup() {
  if [ -n "${BPID:-}" ] && kill -0 "$BPID" 2>/dev/null; then
    kill -TERM "$BPID" 2>/dev/null
    sleep 1
    kill -KILL "$BPID" 2>/dev/null
  fi
  rm -rf "$DATADIR"
}
trap cleanup EXIT INT TERM

echo "==> Starting throwaway dev backend (data-dir: $DATADIR)"
PYTHONPATH="$REPO/KrabEar" "$PY" KrabEar/main.py --data-dir "$DATADIR" > "$LOG" 2>&1 &
BPID=$!

# Wait up to ~20s for the IPC socket.
for _ in $(seq 1 40); do
  [ -S "$SOCK" ] && break
  if ! kill -0 "$BPID" 2>/dev/null; then
    echo "ERROR: backend exited during startup. Last log lines:"; tail -20 "$LOG"; exit 1
  fi
  sleep 0.5
done
[ -S "$SOCK" ] || { echo "ERROR: socket never appeared. Log tail:"; tail -20 "$LOG"; exit 1; }
sleep 2  # let warmups settle

rc=0
echo ""
echo "==> Running IPC behavior + CRUD smoke"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_ipc_smoke.py "$SOCK" || rc=1
echo ""
echo "==> Running privacy-gate canary"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_privacy_gates.py "$SOCK" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "============================================================"
  echo "  ✅ ALL E2E SMOKES GREEN"
  echo "============================================================"
else
  echo "============================================================"
  echo "  ❌ E2E SMOKE FAILURE — see output above; backend log: $LOG"
  echo "============================================================"
  # Keep the log on failure for debugging (copy out before cleanup wipes DATADIR).
  cp "$LOG" "/tmp/krab_ear_e2e_last_failure.log" 2>/dev/null && echo "  (backend log copied to /tmp/krab_ear_e2e_last_failure.log)"
fi
exit "$rc"
