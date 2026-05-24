# User-Action Checklist — Krab Ear (Wave 553)

This document lists actions that **only the user can perform** (require sudo
password, browser interaction, or physical hardware access) and that Claude /
sub-agents cannot auto-execute.

Last updated: 2026-05-27 (Wave 716 — gigaam dup root cause added)

---

## 🔴 P0 (NEW Wave 716) — Duplicate gigaam_worker = 1.5 GB constant memory leak

### Root cause of frequent reboots while parallel-agent work is running

**Evidence (Wave 716 live measurement 2026-05-27 23:44Z)**:

```
PID  PPID  ETIME  RSS
3777 1904 (service.py)   ~1573 MB  ← legit
5015 1879 (rest_server.py)  ~1496 MB  ← DUPLICATE
```

Every time the backend stack restarts, `rest_server.py` module-level
`engine = AudioEngine()` spawns its own gigaam_worker subprocess. The legit
worker is the one spawned by `service.py`. The rest_server's worker is pure
waste — REST endpoints don't transcribe directly; they proxy through IPC.

**Combined with parallel sub-agents (~400 MB each × 5), this puts the box
over the kernel OOM threshold → forced reboot.** This explains all ~10
reboots observed during the marathon.

**Permanent fix shipped in PR #619** (Wave 525 — `skip_gigaam_warmup=True`
in `rest_server.py` + fcntl singleton lock in `gigaam_worker.py`). That PR
has been blocked by audit/test failures since 2026-05-26. Once it merges
and ships in v2.0.5, the dup will not respawn.

**Manual workaround (inline kill, releases ~1.5 GB immediately)**:

```bash
REST_PID=$(pgrep -f "KrabEar/backend/rest_server.py" | head -1)
pgrep -f gigaam_worker | while read gpid; do
  ppid=$(ps -o ppid= -p $gpid | tr -d ' ')
  [ "$ppid" = "$REST_PID" ] && kill $gpid
done
# Verify one worker remains:
pgrep -fl gigaam_worker
```

Add to cron / launchd post-boot if reboots keep happening before PR #619
ships:

```bash
echo "*/10 * * * * /Users/pablito/Antigravity_AGENTS/Krab Ear/scripts/kill_dup_gigaam.command" | crontab -
```

---

## 🔴 P0 — daily-reboot blocker

### macOS auto-update disable (root cause of 14:04 CEST reboots)

The MacBook reboots itself at 14:04 CEST nearly every day, killing all running
processes (Krab Ear backend, gateway, sub-agents). Evidence: Wave 553 confirmed
a fresh reboot today; uptime was 6 min when this doc was created.

**Fix** (paste into Terminal, enter password when prompted):

```bash
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticallyInstallMacOSUpdates -bool false

sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticallyInstallAppUpdates -bool false

sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticCheckEnabled -bool true   # keep notifications ON, install OFF

sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticDownload -bool false

# Optional: verify
sudo defaults read /Library/Preferences/com.apple.SoftwareUpdate \
    AutomaticallyInstallMacOSUpdates   # should print 0
```

After this you will still see update banners — just dismiss them. Manual update
runs only when YOU click "Install now" in System Settings.

---

## 🟡 P1 — observability blocker (HF gate)

### Accept pyannote/speaker-diarization-3.1 license

`pyannote.audio` requires accepting a per-model HF license before download.
Without acceptance, GigaAM longform path silently falls back to non-diarized
output.

**Action**:
1. Go to https://huggingface.co/pyannote/speaker-diarization-3.1
2. Click "Accept license to access this repository" (top of model page)
3. Confirm the second prompt for `pyannote/segmentation-3.0` (dependency)

Both must be accepted under the HF account whose token is in
`~/.cache/huggingface/token` (likely `pavelr7@gmail.com`).

---

## 🟡 P1 — VPN reliability

### VPN plist KeepAlive

`/Library/LaunchDaemons/com.po.vpnserver.plist` lacks `KeepAlive=true` +
`RunAtLoad=true`, so VPN does NOT come back after a network blip.

**Fix** (requires sudo):

```bash
sudo plutil -insert KeepAlive -bool true /Library/LaunchDaemons/com.po.vpnserver.plist
sudo plutil -insert RunAtLoad -bool true /Library/LaunchDaemons/com.po.vpnserver.plist
sudo launchctl bootout system /Library/LaunchDaemons/com.po.vpnserver.plist 2>/dev/null
sudo launchctl bootstrap system /Library/LaunchDaemons/com.po.vpnserver.plist
```

Verify with:
```bash
sudo launchctl print system/com.po.vpnserver | grep -E "(state|keepalive)"
```

---

## 🟢 P2 — Sentry release tag after version bump

### Restart backend + Swift agent after each v2.0.X release

When the backend starts, `sentry_sdk.init(release=...)` is called once and
baked in for the process lifetime. If the old process was not restarted after
a version bump, Sentry events continue to carry the previous release tag —
misrouting issues in the Sentry dashboard.

Root cause documented in `docs/audit/2026-05-27-wave715-sentry-release-stale-process.md`.

**Action** (after every release bump, e.g. 2.0.4 → 2.0.5):

```bash
pkill -f "python.*KrabEar/main.py" || true
pkill -f KrabEarAgent || true
open "Krab Ear.app"   # or re-run launchd variant
```

Verify in logs: look for `"Sentry release resolved"` line showing the new tag.

---

## 🟢 P2 — routine registration (3 missing)

`SKILL.md` files exist on disk but the routines are not registered with the
scheduler. Registration requires an approval dialog that sub-agents cannot
trigger — must be initiated by the user from chat:

- `krab-ear-dsym-upload-verify` — daily 7:30 AM (verifies fresh dSYMs are in Sentry)
- `krab-ear-two-binary-drift-watch` — daily 6:30 AM (UUID match bundle vs runtime)
- `krab-ear-bench-monitor` — weekly Sun 11:30 AM (performance budget tracking)

**Action**: in a chat session, ask Claude to register each routine using
`mcp__scheduled-tasks__create_scheduled_task`. The approval dialog will pop —
click Approve.

---

## 🟢 P2 — disk hygiene

### Worktree cleanup (~100 GB recoverable)

`.claude/worktrees/` accumulated ~377 directories from prior sub-agent sessions.
Most are stale (>30 days, branch already merged).

**Action**: run `scripts/cleanup_merged_worktrees.command` (no sudo required,
but script is interactive — confirms each removal). Skip any locked worktree
(active sub-agent).

---

## Recently completed (no action required)

- ✅ Wave 525: GigaAM dup worker singleton lock (PR #619 — pending audit unblock)
- ✅ Wave 545: audit allowlist scoped (PR #622) → unblocks ~30 PRs
- ✅ Wave 546: disk_monitor defensive cast (PR #625) → unblocks ~15 PRs
- ✅ Wave 547: CallAutomationController SF Symbols (PR #624) → AGENT-J sister
- ✅ Wave 554: Swift 6 strict concurrency fixes (PR #623) → all Swift PRs

---

*Generated by Wave 553. Update this doc whenever a new user-only action is
discovered.*
