# Wave 716 cron retirement — criteria + plan

**Status:** Active (cron runs every 10 min).  
**Retire AFTER:** v2.0.5 production deploy + 7 days clean observation.

---

## Background

### Why the cron exists (Wave 716)

`rest_server.py` has a module-level `engine = AudioEngine(...)` that, in versions
prior to v2.0.5, was called without `skip_gigaam_warmup=True`.  This caused the
REST server to spawn its own `gigaam_worker` subprocess (~1.5 GB RSS) in addition
to the legitimate one owned by the main `service.py` backend.  The duplicate
leaked memory silently; it was never freed during normal operation.

Wave 716 added `scripts/kill_dup_gigaam.command` as a tactical workaround: a
cron/launchd timer runs the script every 10 minutes.  The script finds any
`gigaam_worker.py` process whose PPID matches the REST server PID and sends
`SIGTERM`.  It is safe to run continuously — it never touches the legitimate
worker owned by `service.py`.

### Permanent fix (Wave 525 / PR #619, ships in v2.0.5)

Two independent layers were added:

1. **`rest_server.py` — `skip_gigaam_warmup=True`** (line 267):  
   `engine = AudioEngine(skip_gigaam_warmup=True)`  
   `AudioEngine.__init__` skips the GigaAM warmup thread entirely when this flag
   is set, and the internal `_skip_gigaam` guard also prevents any subsequent
   `_transcribe_gigaam()` call from reaching GigaAM — so the REST engine is fully
   isolated from the subprocess.

2. **`gigaam_worker.py` — fcntl singleton lock** (`_acquire_singleton_lock()`):  
   At process start the worker tries `fcntl.flock(fd, LOCK_EX|LOCK_NB)` on
   `/tmp/krab_ear_gigaam_worker.lock`.  If another worker already holds the lock,
   the new process logs a warning to stderr and exits immediately.  The lock is
   held until the OS releases it on process exit (clean shutdown or signal).  This
   makes the singleton guarantee race-free — no `pgrep` polling required.

Both layers together ensure that at most one `gigaam_worker` process can ever
exist system-wide, regardless of how many Python processes import `AudioEngine`.

---

## Retirement criteria

ALL of the following must hold for **7 consecutive days** after v2.0.5 is running
in production.

| # | Criterion | Pass condition |
|---|-----------|----------------|
| 1 | Backend is v2.0.5 | Version log line present at startup |
| 2 | Exactly one `gigaam_worker` at any sample point | `pgrep` count = 1 |
| 3 | Cron script has not killed anything | No "killing dup gigaam" in cron logs |
| 4 | REST engine confirms skip path is active | Log line present after REST server start |
| 5 | No Sentry gigaam-related errors | Zero matching events for 7 days |

---

## How to verify each criterion

### 1 — Backend version is v2.0.5

```bash
# Tail the backend log and look for the startup version line
grep "version" ~/Library/Logs/KrabEar/backend.log | tail -5
# OR query via IPC health endpoint:
python3 -c "
import socket, json, os
s = socket.socket(socket.AF_UNIX)
s.connect(os.path.expanduser('~/Library/Application Support/KrabEar/krabear.sock'))
s.sendall(json.dumps({'id':'1','method':'handshake','params':{}}).encode() + b'\n')
print(json.loads(s.recv(4096).decode()).get('result',{}).get('version','?'))
"
```

### 2 — Exactly one gigaam_worker process

```bash
# Should print exactly "1"
pgrep -fl "gigaam_worker.py" | wc -l

# For a fuller snapshot (run a few times across the day):
pgrep -fl "gigaam_worker.py"
```

### 3 — Cron has not triggered a kill

The cron pipes stdout to a log.  Check:

```bash
# If cron redirects to a file (check crontab -l for the exact path):
grep "killing dup" /tmp/kill_dup_gigaam.log 2>/dev/null || echo "no kills recorded"

# Also check syslog:
log show --predicate 'process == "bash" AND eventMessage CONTAINS "killing dup gigaam"' \
  --last 7d --style syslog 2>/dev/null | head -20
```

Acceptable output: only `"no duplicate gigaam workers found ✓"` lines — zero
`"killing dup gigaam"` entries.

### 4 — REST engine uses skip_gigaam_warmup path

```bash
# After restarting rest_server.py, check its log:
grep "skip_gigaam_warmup\|GigaAM warmup пропущен" ~/Library/Logs/KrabEar/rest_server.log | tail -5
# Expected: "GigaAM warmup пропущен (skip_gigaam_warmup=True) — этот engine не spawn'ит worker"
```

### 5 — No Sentry gigaam errors

Check the Sentry dashboard for project `krab-ear-backend` (org `po-zm`,
region `de.sentry.io`).  Filter by:
- `gigaam_worker_crashed`
- `gigaam.oom`
- `stt.gigaam_*`
- title contains `gigaam_worker`

Zero new events for 7 days = criterion passed.

---

## Retirement procedure

Run this once all five criteria are confirmed green:

```bash
# 1. Backup current crontab
crontab -l > /tmp/cron-pre-w716-retirement.bak
echo "Backup written to /tmp/cron-pre-w716-retirement.bak"

# 2. Remove the kill_dup_gigaam line
crontab -l | grep -v "kill_dup_gigaam.command" | crontab -

# 3. Verify removal
crontab -l | grep -c "kill_dup_gigaam" && echo "WARNING: still present!" || echo "Removed OK"

# 4. Write audit record
echo "W716 cron removed at $(date -Iseconds) — v2.0.5 singleton lock confirmed clean for 7d" \
  >> ~/Library/Logs/krab-ear-cron-retirement.log

# 5. (Optional) Archive the script rather than deleting it
git -C "$(git rev-parse --show-toplevel)" mv \
  scripts/kill_dup_gigaam.command \
  scripts/retired/kill_dup_gigaam.command
```

The script file itself can remain in `scripts/retired/` for reference; the cron
entry is the only live surface that needs removal.

---

## Rollback — if duplicates return after retirement

```bash
# Immediately restore the cron from backup
crontab /tmp/cron-pre-w716-retirement.bak
echo "W716 cron RESTORED at $(date -Iseconds)" >> ~/Library/Logs/krab-ear-cron-retirement.log

# Run the kill script manually to clear any current dup
bash scripts/kill_dup_gigaam.command
```

Then investigate the root cause before attempting retirement again:

```bash
# Which process spawned the new dup?
pgrep -fl "gigaam_worker.py"
# For each PID, check parent:
ps -o pid,ppid,command -p <PID>
# Is the singleton lock present?
ls -la /tmp/krab_ear_gigaam_worker.lock
# Is skip_gigaam_warmup actually set in the running rest_server?
python3 -c "import ast,inspect; \
  import importlib.util; \
  spec = importlib.util.spec_from_file_location('rs','KrabEar/backend/rest_server.py'); \
  m = importlib.util.module_from_spec(spec); \
  # Check source directly:
  " 2>/dev/null || grep "skip_gigaam_warmup" KrabEar/backend/rest_server.py
```

Common failure modes:
- A new code path imports `AudioEngine()` without `skip_gigaam_warmup=True`.
- The singleton lock file was manually deleted while the worker was running.
- A third Python process (e.g., a test runner or dev script) started a worker.

---

## 30-day monitoring after retirement

Even with both permanent layers active, spot-check weekly for 30 days:

```bash
# Memory — gigaam_worker RSS should be stable, not growing
ps -o rss,command $(pgrep -f gigaam_worker.py) 2>/dev/null

# Worker count — must stay at 1
pgrep -fl gigaam_worker.py | wc -l

# Lock file sanity
ls -la /tmp/krab_ear_gigaam_worker.lock 2>/dev/null
```

Watch for:
- **Worker count creeping above 1** — re-add cron immediately + open investigation issue.
- **RAM trend growing steadily** — could indicate a different leak; baseline is ~35 MB backend RSS (post-Wave 63 `mx.clear_cache()` fix).
- **Sentry `gigaam_worker_*` events** — any new event within 30 days warrants reverting retirement.

Set a calendar reminder: `+30d` from retirement date to do one final check and
close the monitoring period.

---

*Generated Wave 761 (2026-05-26). Related docs:*  
- `docs/audit/2026-05-26-wave695-wake-word.md`  
- `docs/USER_ACTION_CHECKLIST.md` (P0 W716 section)  
- `scripts/kill_dup_gigaam.command` (the script being retired)  
- PR #619 (Wave 525 — permanent singleton fix)
