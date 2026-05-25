#!/bin/bash
# Wave 716 workaround — kill the rest_server.py child gigaam_worker.
#
# Wave 69 dup pattern: rest_server.py module-level `engine = AudioEngine()`
# spawns its own gigaam_worker subprocess (~1.5 GB), unnecessarily.
# REST endpoints proxy STT through IPC; they never need a local worker.
#
# Permanent fix is in PR #619 (Wave 525 — skip_gigaam_warmup + singleton
# lock). Until it merges, this script can be run on a timer (every 5-10
# min via cron / launchd) to keep memory pressure low.
#
# Safe to run as often as you like — it only kills workers parented by
# rest_server.py, never the legit service.py child.

set -euo pipefail

REST_PID="$(pgrep -f "KrabEar/backend/rest_server.py" | head -1 || true)"
if [ -z "$REST_PID" ]; then
  echo "[kill-dup-gigaam] rest_server.py not running — nothing to do"
  exit 0
fi

KILLED=0
for gpid in $(pgrep -f "KrabEar/core/workers/gigaam_worker.py" 2>/dev/null || true); do
  ppid="$(ps -o ppid= -p "$gpid" 2>/dev/null | tr -d ' ' || echo 0)"
  if [ "$ppid" = "$REST_PID" ]; then
    echo "[kill-dup-gigaam] killing dup gigaam pid=$gpid (ppid=$ppid = rest_server)"
    kill "$gpid" 2>/dev/null && KILLED=$((KILLED + 1)) || true
  fi
done

if [ "$KILLED" -eq 0 ]; then
  echo "[kill-dup-gigaam] no duplicate gigaam workers found ✓"
else
  echo "[kill-dup-gigaam] killed $KILLED duplicate worker(s) — ~$((KILLED * 1500)) MB freed"
fi
