---
name: Wave 500 MILESTONE — 500-wave mega-marathon achievement
description: 13-day session, 9 services extracted, Phase B 24→54+ codes, ~165 PRs merged, ~17 production bugs caught, v2.0.3 SHIPPED, v2.0.4 ready
type: milestone
date: 2026-05-24
---

# Wave 500 Milestone Snapshot

## Achievement
**500 waves shipped** across 13 calendar days (May 12-24, 2026)

## Top-line metrics
- ~165 PRs merged
- 9 services extracted from service.py
- service.py: 5777 (peak) → 5478 LOC (-299, -5%)
- Active IPC handlers: 305 (peak) → 86 (-219, -72%)
- ERROR codes: 24 → 54 (+30 production-discovered)
- 10,864+ test methods / 404+ files
- 17+ production bugs caught & fixed
- 11 codified pattern lessons
- v2.0.3 SHIPPED, v2.0.4 ship-ready
- 0 flake8 warnings in production code
- 100% user constraint adherence (0 model loads triggered)

## Major bug discoveries (chronological)
1. Wave 91: analytics_dashboard ZeroDivisionError + 4 shutil.rmtree races + Parakeet/privacy_audit/auto_backup TOCTOU
2. Wave 96: AuditLogger PermissionError
3. Wave 154: model_cache evict race (explicit semantic upgrade)
4. Wave 66: AGENT-K BackendToast NSVisualEffectView/ColorSync (fix shipped Wave 326)
5. Wave 67: AGENT-J font glyph `●` Unicode (shipped in binary Wave 274 v2.0.3)
6. Wave 266: AGENT-M BackendToast sizeToFit() glyph metrics
7. Wave 306: LM Studio Stream(gpu) mis-classified as rewriter.timeout
8. Wave 326: Sentry options.dist never set + release=nil + no release page (3 bugs simultaneously)
9. Wave 342: sanitize_path relative traversal security
10. Wave 351: dispatch table direct vs stub pattern (12 PRs cross-cutting)
11. Wave 358/359: GigaAM longform threshold 24→30s
12. Wave 359 + 373: AudioChunker micro-advance ×2 layers
13. Wave 422: Wave 50 launchd CRITICAL bug — `pgrep + set -e` silent failure
14. Wave 470: cross-cutting `patch` import unblocked ~40 PRs
15. Wave 490: Phase B Wave 82 HIGH priority 3 codes wired (disk.critical, system.proc_cmdline_permission, startup.stt_model_cache_miss)
16. PerfBench mult=5.0 not robust against CI variance — cross-cutting all UNSTABLE fixes
17. test_privacy_audit.py missing `patch` import affected ~40 PRs

## 9 Services extracted timeline
1. CallSessionService (Wave 73)
2. AudioAnalyticsService (Wave 73)
3. VocabularyService (Wave 74)
4. ReportingService (Wave 75)
5. IntegrationService (Wave 76)
6. RecordingCoreService (Wave 331) — biggest -833 LOC
7. AnalyticsService (Wave 392)
8. TextScoringService (Wave 404)
9. HealthCheckService (Wave 423) — completes Wave 380 audit triple

## 11 Pattern lessons codified
1. CoreText "first render" penalty class (AGENT-J/K/M all share)
2. Two-binary drift — source-only fixes need rebuild+ship+restart
3. Dispatch invariant trade-off — stub delegation chosen for test backward compat
4. CI perf budget — M4 Max local vs macos-latest 3x variance, mult=15.0
5. xdist worker OOM — keep test inputs ≤1k chars OR @pytest.mark.slow
6. Routine-driven discovery > manual log diving
7. LaunchAgent autostart audit — silent 24/7 daemons
8. Sentry SDK session-caching — dist/release MUST be set in init()
9. Sub-agent worktree isolation — 17+ PR merge trains with zero conflicts
10. Dead handler audit convergence — 6 batches enough
11. Cross-cutting test infrastructure bug — 35+ simultaneous UNSTABLE with same fail → shared fixture/import drift

## Phase B (Loud Errors) progression
- Pre-Wave 60: 24
- Wave 60: +5 (29)
- Wave 61: +3 (34)
- Wave 64: +5 (42)
- Wave 77: +3 (45)
- Wave 78: +5 (50)
- Wave 81: +1 (51)
- Wave 82 HIGH wired: +3 (54)
- Wave 82 candidates remaining: 3

## Key infrastructure shipped
- Wave 311 PR #585: worktree cleanup script (100 GB recoverable)
- Wave 387 PR #600: observe_production.command (20-section health snapshot)
- Wave 416 PR #605: macOS Sequoia 26 docs + SF Symbol regression guard test
- Wave 440 PR #609: backend log digest v3 (6 new categories)
- Wave 364 PR #596: v2.0.4 ship checklist
- Wave 460 PR #612: audit_dead_ipc_handlers v3 (CLI pattern detection)
- Wave 470: cross-cutting test infrastructure fix

## User action items (carried — 5-10 min total)
1. Worktree cleanup (PR #585 + execute, ~100 GB freed)
2. VPN plist fix (KeepAlive=true + RunAtLoad=true)
3. Disable macOS auto-update reboot
4. Ship v2.0.4 (Wave 364 checklist)
5. HF accept pyannote/speaker-diarization-3.1

## Recommended next session focus
1. Wire 3 remaining Wave 82 codes (stt.postprocess_drop, rewriter.circuit_cascade, stt.gigaam_longform_unavailable)
2. Mega-merge train after Wave 470 + 490 CI runs settle (~50 PRs waiting)
3. CallAutomationController + GlobalStatusBar Unicode glyph fixes (Wave 416 found 6 sites)
4. v2.0.4 ship execution
5. macOS Sequoia 26 deeper integration tests
6. Performance regression suite expansion
