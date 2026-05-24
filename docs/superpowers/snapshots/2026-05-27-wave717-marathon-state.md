# Wave 717 Marathon Snapshot

**Date:** 2026-05-27  
**Branch at snapshot:** wave714/stt-management-service  
**Cumulative waves:** ~717  **PRs this marathon:** 60+  **Total PRs merged:** ~195+

---

## Cumulative Stats

| Metric | Value |
|---|---|
| Waves completed | ~717 |
| PRs merged (this marathon session) | 60+ (through #656) |
| Services extracted from service.py | 9 (latest: AppleIntegrationService −300 LOC, W688) |
| Active IPC handlers | 86 (−219 dead removed, W65 marathon complete) |
| Error codes (ERROR_REGISTRY) | 57 |
| Test methods | ~10,864 / 404+ files |
| service.py LOC | ~5,178 (after W688 extraction) |
| Sentry unresolved | 0 backend + 0 agent |

---

## Live Production State (snapshot 2026-05-27)

Processes were **not running** at snapshot time (box likely idle between sessions).

**RAM headroom (vm_stat reading):**
- Free + speculative pages: ~3.2 GB
- Active/inactive/wired: ~27.6 GB total visible
- Note: with backend stack running (service.py ~1.5 GB + rest_server ~1.5 GB + gigaam_worker ×1 ~0.7 GB) plus LM Studio, headroom shrinks to ~4–5 GB.

**Expected healthy process set when running:**
```
service.py       → 1 process,  RSS ~1,500–1,600 MB (incl. gigaam_worker child)
rest_server.py   → 1 process,  RSS   ~400–600 MB  (NO gigaam_worker since W525+PR#619)
gigaam_worker    → 1 total     (should NOT be 2 — see Root Cause below)
KrabEarAgent     → 1 process,  RSS    ~50–120 MB
```

---

## Last 10 Shipped PRs

| # | Wave | Title | Merged |
|---|---|---|---|
| #656 | W716 | `chore: kill_dup_gigaam.command — root cause of reboot loop` | 2026-05-24 21:48 |
| #650 | W693 | `test: dispatch invariant tests x5` | 2026-05-24 20:52 |
| #648 | W688 | `refactor: extract AppleIntegrationService from service.py (−300 LOC)` | 2026-05-24 20:52 |
| #646 | W686 | `feat: TCC permissions audit script` | 2026-05-24 20:52 |
| #645 | W689 | `docs: LM Studio probe URL audit — 9 call sites, 0 live JIT risks` | 2026-05-24 20:52 |
| #643 | W651 | `chore: prune 141 stale worktrees, free 9 GB` | 2026-05-24 19:22 |
| #642 | W656 | `feat: wire AgentRecoveryLogger into main.swift bootstrap` | 2026-05-24 19:22 |
| #641 | W654 | `test: dispatch invariant tests x5 for recent IPC handlers` | 2026-05-24 19:22 |
| #638 | W655 | `docs: Sentry breadcrumb audit — 5 services` | 2026-05-24 19:22 |
| #637 | W657 | `docs: IPC_API_REFERENCE drift audit` | 2026-05-24 19:22 |

**In-flight on current branch (wave714/stt-management-service, ahead of main):**
- W692 — SettingsService breadcrumbs
- W687 — log rotation (FileHandler→RotatingFileHandler)
- W702 — history_service top-3 breadcrumbs + tests
- W704 — Sentry release tag fix (CRITICAL, was stuck at 2.0.0)
- W706 — TranslationService top-3 breadcrumbs
- W707 — CallAssistService top-3 breadcrumbs

---

## Pending P0/P1 User Actions

From `docs/USER_ACTION_CHECKLIST.md` (Wave 553, updated W716):

### 🔴 P0 — Duplicate gigaam_worker (root cause of reboot loop)
Every backend restart, `rest_server.py` spawns its own GigaAM worker (~1.5 GB wasted). Combined with parallel agents (~400 MB × 5), OOM → reboot.

- **Permanent fix:** PR #619 (Wave 525 singleton lock) — `MERGEABLE`, blocked by audit/test failures. Once shipped in v2.0.5, dup will not respawn.
- **Workaround until then:** cron every 10 min:
  ```bash
  echo "*/10 * * * * /Users/pablito/Antigravity_AGENTS/Krab\ Ear/scripts/kill_dup_gigaam.command" | crontab -
  ```

### 🔴 P0 — macOS auto-update (daily 14:04 CEST reboots)
Disable via `sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false` (full commands in checklist).

### 🟡 P1 — Accept pyannote HF gated model
Go to huggingface.co/pyannote/speaker-diarization-3.1 and click "Accept license". Also accept `pyannote/segmentation-3.0`.

### 🟡 P1 — VPN plist KeepAlive
Add `KeepAlive=true` + `RunAtLoad=true` to `/Library/LaunchDaemons/com.po.vpnserver.plist` (requires sudo).

### 🟢 P2 — Register 3 missing routines (dsym-upload-verify, two-binary-drift-watch, bench-monitor)
Ask Claude: `mcp__scheduled-tasks__create_scheduled_task` for each. Click Approve in dialog.

### 🟢 P2 — Sentry release restart after each version bump
After v2.0.5 ships: `pkill -f "python.*KrabEar/main.py" && open "Krab Ear.app"` to re-initialize `sentry_sdk.init(release=...)`.

---

## Marathon Learnings (W717 additions)

### 1. Duplicate gigaam_worker = root cause of ~10 reboots during marathon
Wave 716 live measurement (2026-05-27 23:44Z) confirmed two GigaAM workers at ~1.5 GB each. `rest_server.py` module-level `engine = AudioEngine()` spawns a worker even though REST endpoints proxy through IPC and never transcribe locally. The `scripts/kill_dup_gigaam.command` (PR #656) is the stop-gap; PR #619 is the permanent seal.

### 2. PR #619 (Wave 525 singleton lock) is the permanent fix — unblock before v2.0.5
`skip_gigaam_warmup=True` in `rest_server.py` + `fcntl` singleton lock in `gigaam_worker.py`. Has been MERGEABLE since W525 but blocked by audit failures; needs one focused CI pass.

### 3. Cron kill-dup until PR #619 ships
```bash
echo "*/10 * * * * /Users/pablito/Antigravity_AGENTS/Krab\ Ear/scripts/kill_dup_gigaam.command" | crontab -
```
Releases ~1.5 GB immediately on each run, preventing the OOM cascade.

### 4. LM Studio JIT loading — secondary contributor (Wave 632)
Model eviction after 1800 s TTL + external SSD sleep adds ~3–5 s cold-load stall per rewriter call, not an OOM trigger by itself. Root cause fully documented in `docs/audit/2026-05-27-wave715-sentry-release-stale-process.md`.

### 5. Inline > backgrounded agents (survival rate 10–30% in reboot loop)
Backgrounded sub-agents die on reboots or compaction events. Use inline sequenced tasks or explicit worktrees with `git checkout -b` in each prompt. Keep concurrency ≤ 5 to avoid merge conflict spikes.

### 6. Wave 704 critical fix — Sentry release tag was stuck at 2.0.0
All Sentry events were mis-tagged across every release. Fix reads version from `KrabEar/version.py` at startup; ships in current branch wave714/stt-management-service.

---

## Next 30 Waves Roadmap

1. **Merge wave714 branch** — W692/702/704/706/707 breadcrumbs + W704 critical Sentry fix. Ship as one PR.
2. **STTManagementService extraction** — 5th attempt; `stt_management_service.py` already exists as untracked file. Pattern: delegate `_handle_stt_*` methods (~15 handlers). Previous blockers: missing `patch` import (fixed W470), concurrency invariant failures (audit scope fix needed).
3. **PR #619 unblock** — one focused CI pass to get singleton lock merged; clears the OOM root cause permanently.
4. **v2.0.5 release** — after PR #619 + STTMgmt merge; includes W704 Sentry tag fix + 9 breadcrumb PRs.
5. **Remaining breadcrumbs** — `AudioAnalyticsService`, `HealthCheckService`, `CallSessionService` not yet wired.
6. **IPC_API_REFERENCE regeneration** — current doc references pre-W65 handler names; regenerate from 86-handler ground truth.
7. **Wave 82 remaining 3 MED error codes** — `call.telnyx_auth_error`, `vgw.stream_timeout`, `realtime.partial_flush_error`.
8. **3 missing routines registration** — user-triggered via scheduled-tasks MCP.
9. **Sentry dSYM upload** — still blocked; Swift agent AppHang issues remain symbolicatable only with proper dSYMs.
10. **Worktree pruning round 2** — W651 freed 141 worktrees; estimate another 50–70 stale ones remain.
