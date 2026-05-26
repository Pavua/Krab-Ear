# User-Action Checklist — Krab Ear (Wave 769)

This document lists actions that **only the user can perform** (require sudo
password, browser interaction, or physical hardware access) and that Claude /
sub-agents cannot auto-execute.

Last updated: 2026-05-26 (Wave 769 — W716/W704 shipped in v2.0.5, deploy section added)

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

## 🟢 P2 — v2.0.5 deploy execution

v2.0.5 is tagged and the binary is built. The main repo is on the dirty
`wave736` branch — it needs to be synced to `codex/krab-ear-v2` before the
running processes pick up the permanent fixes.

See `docs/DEPLOY_V2.0.5.md` (W753) for the full paranoid pre/post procedure:
backup `wave736` state → sync to `codex/krab-ear-v2` → verify → deploy →
post-deploy checks (Sentry release tag, gigaam_worker count = 1, ping
contract) → rollback path if needed.

**Quick deploy sequence** (abbreviated — read the full doc first):

```bash
# 1. Stop running processes gracefully
pkill -f "python.*KrabEar/main.py" || true
pkill -f KrabEarAgent || true

# 2. Switch main repo to v2.0.5 codebase
git -C "/Users/pablito/Antigravity_AGENTS/Krab Ear" checkout codex/krab-ear-v2

# 3. Restart
open "/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app"

# 4. Verify Sentry release tag in log
grep "Sentry release resolved" ~/Library/Logs/KrabEar/krabear.log | tail -1
# Expected: "Sentry release resolved" with value "2.0.5"

# 5. Verify single gigaam_worker
pgrep -fl gigaam_worker   # should show exactly ONE line
```

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

- ✅ Wave 716 P0: GigaAM dup worker permanent fix — W525 `skip_gigaam_warmup=True`
  in `rest_server.py` + fcntl singleton lock (PR #619) shipped in **v2.0.5**
  on `codex/krab-ear-v2`. Cron workaround (`kill_dup_gigaam.command`) remains
  active until deploy is verified per retirement criteria in
  `docs/WAVE_716_CRON_RETIREMENT.md` (W761).
- ✅ Wave 704 P2: Sentry release tag fix — backend now reads version from
  `Info.plist → CFBundleVersion → VERSION file` instead of hardcoded
  `__version__.py`. Shipped in **v2.0.5** (PR #668). Requires backend restart
  per `docs/DEPLOY_V2.0.5.md` to take effect (see deploy section above).
- ✅ Wave 545: audit allowlist scoped (PR #622) → unblocks ~30 PRs
- ✅ Wave 546: disk_monitor defensive cast (PR #625) → unblocks ~15 PRs
- ✅ Wave 547: CallAutomationController SF Symbols (PR #624) → AGENT-J sister
- ✅ Wave 554: Swift 6 strict concurrency fixes (PR #623) → all Swift PRs

---

*Generated by Wave 769. Update this doc whenever a new user-only action is
discovered or a pending item ships.*
