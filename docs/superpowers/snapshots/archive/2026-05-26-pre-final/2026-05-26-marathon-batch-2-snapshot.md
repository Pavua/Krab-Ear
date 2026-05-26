# Marathon Batch 2 — 2026-05-26 (Waves 740–750)

**Session duration:** ~2.5 hours (continuing from W740 final wrap baseline)
**Outcome:** 8+ PRs merged, 1 critical hotfix, v2.0.5 shipped, ~580 LOC out of service.py monolith.

## PRs shipped (chronological)

| PR | Wave | Description | LOC delta |
|---|---|---|---|
| [#668](https://github.com/Pavua/Krab-Ear/pull/668) | release | v2.0.5 — W704 Sentry release tag fix + W578-579 SF Symbol regression + new binary | +131 |
| [#669](https://github.com/Pavua/Krab-Ear/pull/669) | W741 | Wire AppleIntegrationService — 6 handlers, orphan from W734 | **−256** |
| [#670](https://github.com/Pavua/Krab-Ear/pull/670) | W742 | Wire STTManagementService — 6 handlers; `TestDispatchWiring` 3/3 green | **−168** |
| [#671](https://github.com/Pavua/Krab-Ear/pull/671) | W743 | Sentry breadcrumbs — `settings_service` + `startup_diagnostics` | +N |
| [#672](https://github.com/Pavua/Krab-Ear/pull/672) | W744 | CLAUDE.md drift fix — 5024→3287 LOC / 318→304 handlers / 11→13 services | ±1 |
| [#673](https://github.com/Pavua/Krab-Ear/pull/673) | **W746** | 🚨 **CRITICAL HOTFIX** — restored missing `TextProcessingService` import lost in W173 rebase | +1 |
| [#674](https://github.com/Pavua/Krab-Ear/pull/674) | W747 | Wire TextScoringService (W404) + AnalyticsService (W392) — 9 handlers | **−161** |
| pending | W748 | Sentry breadcrumbs batch 2 — `call_session` + `recording_core` + `audio_analytics` | +260 |
| pending | W745 | `IPC_API_REFERENCE.md` regen — 840 → ~2400 lines covering 304 handlers | +1500+ |
| pending | W750 | `scripts/audit_orphan_imports.py` CI check — W746 regression guard | +N |

**Tag:** [`v2.0.5`](https://github.com/Pavua/Krab-Ear/releases/tag/v2.0.5) on `codex/krab-ear-v2`.

## Critical findings

### W746 — Production was a ticking time bomb

`KrabEar/backend/service.py:427` instantiates `TextProcessingService(...)` but the corresponding import was silently dropped during a W173 rebase (commit c8aee7e3, 2026-05-22). Production backend (PID 1640) kept running only because Python had already compiled the .py file in-memory **before** the import was lost. **Any restart would crash with `NameError: name 'TextProcessingService' is not defined`** — preventing v2.0.5 deploy.

Found while preparing v2.0.5 ship. Fixed in [#673](https://github.com/Pavua/Krab-Ear/pull/673) by re-adding the one import line. Verified locally: `BackendService(...)` now instantiates clean.

Same fix also applied to wave736/conflict-triage working tree (main repo) so a manual restart won't crash either.

**Sister findings:**
- Recent PRs #669, #670, #671 had `backend-tests: FAILURE` on CI but were force-merged per the no-blocking-CI policy. Those failures were caused by this same NameError. W746 was the underlying root cause masked by the force-merge train.

### Orphan service modules

W734 created `apple_integration_service.py` + `stt_management_service.py` **modules** but never wired them. They had unit tests (including 3 forward-looking `TestDispatchWiring` tests that fail by design until wiring is done). W741+W742 wired them.

W747 wired the same pattern for `TextScoringService` (W404) + `AnalyticsService` (W392). After W747 there's still ONE orphan: `HealthCheckService` (W423) — deferred because `handle_ping` is contract-critical for Swift `HealthMonitor.swift` and needs more careful review.

W750 ships `scripts/audit_orphan_imports.py` to catch future W746-style bugs in CI (AST-scans for class instantiations without matching imports).

## Architectural lessons codified

1. **Orphan modules are dangerous** — extracted code that isn't wired creates two failure modes: dead code, or worse, half-wired code where instantiation exists but import doesn't. Always test by actually instantiating `BackendService` in CI.

2. **In-memory bytecode masks disk-state bugs** — production processes can survive disk-level breakage for hours/days as long as they don't reload. This makes "deploy on restart" risky. Mitigation: add static instantiation tests to CI.

3. **Force-merge requires distinguishing CI signals** — when "backend-tests FAILURE" is treated as advisory, real regressions slip in. The W746 hotfix turned several silent FAILUREs green at once, suggesting all of them shared this root cause.

4. **Module + tests + handler audit** — W750 adds the third layer of defense. Now: (a) module exists, (b) tests run, (c) audit script verifies wiring integrity.

## Live state (post-batch-2)

| Metric | Value |
|---|---|
| `service.py` LOC | **3287** (from 5024 stale CLAUDE.md claim — −34%) |
| IPC handlers (lookup table) | 304 |
| Extracted services (wired) | 13 of 15 (HealthCheck still orphan; live_subs + tts are standalone-by-design) |
| Sentry breadcrumb coverage | history, call_assist, translation, settings, startup_diagnostics, recording_core, call_session, audio_analytics (post W748) |
| `IPC_API_REFERENCE.md` | 2415 lines (post W745 regen; was 840 with 58% drift) |
| v2.0.5 binary | Signed with `Krab Ear Dev Local` (TCC-stable), in `Krab Ear.app/Contents/MacOS/` + `native/runtime/` |
| Sentry release tag | Will read `2.0.5` from Info.plist on next backend restart (W704 fix in #668) |

## Memory & process notes

- 5 sub-agents ran concurrently at peak. Free RAM dipped to ~565 MB; macOS reclaimed inactive cache as needed; no OOM observed.
- One agent (W749 worktree cleanup) returned "Prompt is too long" — split into smaller tasks for future runs.
- IPC_API regen agent worktree was inadvertently `git worktree remove`d mid-flight; the agent's process retained file descriptors and the worktree was effectively moved to `/private/tmp/krab-ipc-api-regen` (macOS's prunable area). Work continued anyway.

## Outstanding for next batch

- **HealthCheckService wiring** (W423 orphan, 6 handlers including the contract-critical `ping`). Needs paranoid contract-preservation review before wiring.
- **v2.0.5 production deploy** — kill PID 1640 (production backend) + restart from `codex/krab-ear-v2` HEAD (main repo currently on dirty `wave736/conflict-triage`; needs sync). After restart, verify Sentry release tag reads `2.0.5`.
- **Wave 716 cron retirement** — `scripts/kill_dup_gigaam.command` workaround can retire after v2.0.5 deploy confirms W525 permanent singleton lock in production binary.
- **HF pyannote license accept** (P1 from `docs/USER_ACTION_CHECKLIST.md`).
- **Worktree disk reclaim** — ~329 worktrees, most locked agent-* checkpoints from prior session. Cleanup script needs to handle path-with-spaces correctly (`git worktree list --porcelain` parsing).

---

*Marathon contributors: Pablo (user, max-parallelism authorization) + Claude Opus 4.7 (coordinator, inline + sub-agent orchestration).*
*5 sub-agents at peak: IPC_API regen, W747 wiring, W748 sentry, W749 cleanup (failed), W750 audit script.*
