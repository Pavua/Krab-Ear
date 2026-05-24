# Wave 403 — 320-wave mega-marathon FINAL snapshot (2026-05-22)

## At-a-glance numbers (verified from HEAD)
| Metric | Value |
|--------|-------|
| Waves shipped | ~320+ (May 12–22, 10 calendar days) |
| PRs merged since 2026-05-19 | 80+ |
| Test methods | 10,615 |
| Test files | 399 |
| service.py LOC | 5,476 (down from ~6,300 = -824 LOC) |
| Active IPC handlers (dispatch table) | 86 |
| ERROR_REGISTRY codes | 48 (started at 24) |
| Services extracted from service.py | 7 |
| Production bugs caught + fixed | 15+ |
| v2.0.3 | SHIPPED (Wave 274, commit 018581c) |
| v2.0.4 | ship-ready (Wave 364 checklist) |

## Services extracted (service.py decomposition)
1. `CallSessionService` — call session state machine
2. `AudioAnalyticsService` — audio analysis methods
3. `VocabularyService` — vocabulary/hotword management
4. `ReportingService` — stats, reports, digests
5. `IntegrationService` — external integrations (Obsidian, webhooks, etc.)
6. `RecordingCoreService` — -833 LOC (Wave 301–351 batch 8)
7. `TextProcessingService` — text cleanup, scoring, diffs (Wave 391+, PR recent)

## Production bugs caught across mega-marathon
1. analytics_dashboard ZeroDivisionError (Wave 91)
2. AuditLogger PermissionError (Wave 96)
3. model_cache evict race (Wave 91 + 154)
4. Parakeet TOCTOU (Wave 91)
5. privacy_audit TOCTOU (Wave 91)
6. auto_backup race (Wave 91)
7. shutil.rmtree races (Wave 91, 4 sites)
8. AGENT-J font glyph hang `●` Unicode → SF Symbol (Wave 67, shipped v2.0.3)
9. AGENT-K BackendToast NSVisualEffectView/ColorSync crash (Wave 66, PR #406)
10. AGENT-M BackendToast.show sizeToFit() glyph metrics (Wave 266)
11. GigaAM longform threshold 24→30s (Wave 358/359)
12. AudioChunker micro-advance ×2 layers (Wave 359 + 373)
13. Sentry options.dist never set + release=nil + no release page (Wave 326, #588)
14. sanitize_path relative traversal security CRIT (Wave 342, #592)
15. LM Studio Stream(gpu,N) mis-classified as rewriter.timeout (Wave 306, #584)
16. Dispatch table direct vs stub inconsistency (Wave 351, cross-cutting 12 PRs)

## Key pattern lessons codified
- **CoreText "first render" penalty class**: AGENT-J/K/M all share — prewarm ALL CoreText/CoreImage at startup
- **Two-binary drift**: source-only fixes need explicit rebuild+ship+restart
- **Wave 58 runtime-vs-static drift**: ALL startup reads of user-overridable settings MUST use `_get_runtime_setting()` — eliminated 70 silent timeouts/day
- **Sentry SDK session-caching**: if dist/release not set in `init()`, Sentry caches previous values forever
- **LaunchAgent autostart audit**: silent 15 GB drain possible (RotorQuant mlx_lm.server)
- **Routine-driven discovery**: backend-error-digest pre-aggregation enabled GigaAM bug discovery without manual log diving

## Batch timeline
| Batch | Dates | Waves | Key milestone |
|-------|-------|-------|---------------|
| 1 | May 12 | ~42 | Routine production audit, AGENT-H root cause |
| 2 | May 13 | 42–58 | AGENT-H shipped, CRIT-1 fixed, Wave 58 drift fix (PR #391) |
| 3 | May 14 | 59 | Phase A residual, LM Studio JIT root cause |
| 4 | May 15/16 | 60–66 | Wave 50 launchd critical fix, AGENT-K, Wave 63 memory leak |
| 5 | May 18 | 65–69 | Dead handler cleanup batch 1 (19 removed), AGENT-J SF Symbol |
| 6 | May 19 | 70–86 | ~700 tests Round 1, GigaAM→Sentry wired |
| 7 | May 19/20 | 87–248 | 134 waves: 51 error codes, 5 service extractions, v2.0.3 ready |
| 8 | May 21 | 249–351 | v2.0.3 SHIPPED, RecordingCoreService -833 LOC, sanitize_path |
| 9 | May 22 early | 352–369 | GigaAM/AudioChunker cascade, dispatch invariant, v2.0.4 ready |
| 10 | May 22 late | 370–403 | AGENT-M fix, TextProcessingService, final stats |

## Pending user actions (carried forward)
1. VPN plist fix (5 min) — closes downstream complaints
2. Disable macOS auto-update reboot (1 min)
3. Ship v2.0.4 — checklist ready (Wave 364)
4. HF accept pyannote gated model (unblocks GigaAM longform)
5. Install Krab Ear agent launchd plist (optional)

## Recommended next session
1. v2.0.4 ship (Wave 364 checklist)
2. TextScoringService + HealthCheckService extractions (next 2 candidates)
3. Wave 65 batch 7+ (dead handler removal — ~80 handlers remain)
4. 3 missing routines registration (dsym-upload-verify, two-binary-drift-watch, bench-monitor)
