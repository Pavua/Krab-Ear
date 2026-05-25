# Marathon Final Wrap — 2026-05-26

**Session duration**: ~10 days (mid-May → 2026-05-26)
**Outcome**: **125 PRs merged + 8 closed superseded = 133 PRs resolved**, 0 open

## Key shipped fixes (production-critical)

| Wave | PR | Description |
|---|---|---|
| **W525** | #619 | 🔴 GigaAM dup worker singleton lock — PERMANENT fix (fcntl `LOCK_EX|LOCK_NB`) |
| **W545** | #622 | Audit allowlist scoped — unblocked ~30 PRs in single fix |
| **W547** | #624 | CallAutomationController SF Symbols (AGENT-J sister) |
| **W554** | #623 | Swift 6 strict concurrency fixes |
| **W611** | #631 | Sister Krab lmstudio watchdog disable doc |
| **W716** | #656 | `scripts/kill_dup_gigaam.command` cron workaround (10 min) |
| **W718→W732** | #661 | Test xdist_group root cause + 8-line fix — cascaded 16 PR merge |
| **W734** | #663 | STTManagementService + AppleIntegrationService modules ship |

## Live production state (2026-05-26 06:00 CEST)

- Backend `service.py` ~5024 LOC (down from 5821), 318 IPC handlers
- 11 extracted services (AnalyticsService, TextScoringService, HealthCheckService, AppleIntegrationService, STTManagementService, RecordingCoreService, CallSessionService, AudioAnalyticsService, HistoryService, SettingsService, TranslationService)
- Free RAM ~240 MB nominal, ~1.5 GB after `lms server stop`
- gigaam_worker count: 1 (legit service.py child; Wave 716 cron kills rest_server.py dup every 10 min)
- 0 reboots in last hour
- Wave 525 permanent fix in main → cron можно отключить после v2.0.5 deploy

## Marathon stats

| Metric | Value |
|---|---|
| PRs merged | **125** |
| PRs closed superseded | **8** |
| Total resolved | **133** |
| Open PRs | **0** |
| Reboots survived | ~10 (kernel OOM) |
| Sub-agents spawned | ~80+ (≥60% died in reboot loop) |

## Architectural learnings codified

1. **Inline > backgrounded** — backgrounded async sub-agents die in 60% of reboot/compact cycles. Critical work должна быть inline.
2. **Max 2 concurrent sub-agents** post memory budget audit (5+ = OOM).
3. **NO pytest in agent tasks** — main OOM trigger (1-2 GB per invocation via Whisper/MLX imports).
4. **NO swift build in agents** — 2-3 GB peak.
5. **Smaller PRs ship faster** — modules-only (W734, 561 LOC) > full extraction with service.py diff (W733, 950 LOC, conflicted).
6. **git rebase --theirs** strategy resolved 6/8 final CONFLICTING PRs.
7. **xdist_group missing на sequential unittest.TestCase** = root cause Wave 718 (subtle, took W730 analysis to find).
8. **Wave 716 cron + Wave 525 permanent fix** = two-layer reboot solution.

## Operating procedures recommended

- Restart backend `service.py` every 6h (rolling memory recovery — RSS can grow to 1.5 GB)
- Kill LM Studio when not actively used (recovers ~600 MB-1 GB)
- Cron `scripts/kill_dup_gigaam.command` every 10 min (until v2.0.5 ships with W525 permanent)
- Force-merge mergeable PRs without waiting for CI (branch not protected)
- Mass-rebase via `gh api PUT /pulls/N/update-branch` после каждого merge cycle

## Outstanding user actions (P0/P1)

См. `docs/USER_ACTION_CHECKLIST.md`:
- macOS auto-update disable (sudo defaults write)
- HF pyannote/speaker-diarization-3.1 license accept (browser)
- VPN plist KeepAlive add (sudo)
- Restart backend after each release bump для Sentry release tag refresh

## Next session ideas

- Wave 740+: feature work — new tests / audits / code quality
- Wave 750+: v2.0.5 ship (incl. PR #619 W525 permanent + W734 modules)
- Wave 760+: continue service.py extractions (FileTranscribeService — biggest remaining at ~1440 LOC)
- W570 Sentry sweep recommendations (translation/call_assist/recording_core breadcrumb wirings уже сделаны)

---

*End of marathon. Marathon contributors: Pablo (user) + Claude (inline + 80+ sub-agents).*
