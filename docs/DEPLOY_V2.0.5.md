# Krab Ear v2.0.5 — Deployment Plan

**Status (2026-05-26 ~03:00 CEST):** v2.0.5 tag shipped, binary built + signed, but **production still runs v2.0.4 in-memory bytecode** (PID 1640, started 00:49 before today's PRs landed).

## Why a careful deploy

The main repo working tree is on `wave736/conflict-triage` — 105 commits ahead of the v2.0.4 tag, with 15 modified files + 27 untracked from the prior session. A naive `pkill -f service.py && open "Krab Ear.app"` would:

1. Reload `service.py` from main repo (wave736 state) — NOT `codex/krab-ear-v2`.
2. Inherit wave736's bugs — W746 NameError was patched in working tree, but only the import line, so other 105 commits' changes are still active.
3. Continue reporting Sentry release tag `2.0.4` because main repo's `Info.plist` hasn't been bumped to `2.0.5`.

## Pre-deploy checklist

- [ ] Backup the in-progress wave736 state — may contain legitimate WIP:
  ```bash
  cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
  git stash push --include-untracked -m "wave736 pre-v2.0.5-deploy backup $(date +%F)"
  git status --short  # should be clean now
  ```
  Alternative: commit them to wave736 first (`git add . && git commit -m "wip: pre-deploy snapshot"`).

- [ ] Sync main repo to `codex/krab-ear-v2`:
  ```bash
  git checkout codex/krab-ear-v2
  git pull --ff-only origin codex/krab-ear-v2
  ```

- [ ] Verify clean state:
  ```bash
  python3 scripts/audit_orphan_imports.py            # must exit 0 (W750 guard)
  python3 -c "from backend.service import BackendService; print('import ok')"
  plutil -p "Krab Ear.app/Contents/Info.plist" | grep CFBundleShortVersionString
  # expect: "2.0.5"
  codesign -dvv "Krab Ear.app/Contents/MacOS/KrabEarAgent" 2>&1 | grep Authority
  # expect: Authority=Krab Ear Dev Local
  ```

## Deploy

```bash
# 1. Stop the old backend gracefully
launchctl unload ~/Library/LaunchAgents/com.antigravity.krab-ear.plist 2>/dev/null || true
pkill -TERM -f "python.*KrabEar/backend/service.py" || true
sleep 3
pkill -KILL -f "python.*KrabEar/backend/service.py" || true

# 2. Optional: kill REST server too (W525 fix lives there)
pkill -TERM -f "python.*KrabEar/backend/rest_server.py" || true
pkill -KILL -f "python.*KrabEar/backend/rest_server.py" || true

# 3. Sanity: NO gigaam_worker should remain (will respawn cleanly)
pgrep -fl gigaam_worker || true

# 4. Launch (one of):
open "Krab Ear.app"
# OR via launchd:
# launchctl load ~/Library/LaunchAgents/com.antigravity.krab-ear.plist
```

## Post-deploy verification

```bash
# 1. Backend started cleanly (no NameError, version 2.0.5)
sleep 5
tail -50 ~/Library/Application\ Support/KrabEar/backend.log | grep -iE "(starting up|NameError|TextProcessingService)"
# expect: "Krab Ear backend version 2.0.5 starting up" + NO NameError

# 2. Sentry release tag refreshed
# Trigger any logger.error() or wait for next legitimate event. Verify in Sentry dashboard:
# po-zm org → krab-ear-backend project → release filter "krab-ear@2.0.5"

# 3. GigaAM dup worker absent (W525 permanent fix shipped)
sleep 30   # let backend warm up + REST server start
pgrep -fl gigaam_worker | wc -l
# expect: 1 (only the legit service.py child)

# 4. handle_ping returns correct shape (HealthMonitor 3-sec tick)
python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/Users/pablito/Library/Application Support/KrabEar/krabear.sock')
s.sendall(json.dumps({'id':'test','method':'ping','params':{}}).encode() + b'\n')
print(s.recv(4096).decode())
"
# expect: {"id":"test","ok":true,"result":{"status":"ok","service":"krabear-backend","version":"2.0.5",...}}
```

## Rollback plan

If anything goes wrong:

```bash
git checkout v2.0.4 -- KrabEar/ "Krab Ear.app/"
pkill -KILL -f "python.*KrabEar/backend/service.py"
open "Krab Ear.app"
```

Then restore WIP:
```bash
git checkout wave736/conflict-triage
git stash pop
```

## After successful deploy

- [ ] Retire `scripts/kill_dup_gigaam.command` cron (W716 workaround):
  ```bash
  crontab -l | grep -v "kill_dup_gigaam.command" | crontab -
  ```
  W525 permanent fix in v2.0.5 makes this cron redundant.

- [ ] Verify Sentry dashboard reads `krab-ear@2.0.5` for new events (and per-service `dist:2.0.5`).

- [ ] Update `docs/USER_ACTION_CHECKLIST.md` — remove the v2.0.5 deploy item.

- [ ] Tag the wave736 stash for archival, then drop:
  ```bash
  git stash list  # find the pre-deploy stash
  git stash drop stash@{N}  # only after confirming everything works
  ```

---

*Authored W753. Generated as a paranoid checklist after W746 revealed how easily production drift accumulates.*
