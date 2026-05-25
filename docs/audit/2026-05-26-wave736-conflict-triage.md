# Wave 736 — Conflict Triage (12 CONFLICTING PRs)

Date: 2026-05-26  
Base: `codex/krab-ear-v2`

## Summary table

| PR | Title (short) | Files | Conflict source | Action |
|----|---------------|-------|-----------------|--------|
| #659 | memory budget doc + log rotation | 17 | `service.py` (STTMgmt/AppleInteg extractions), `__version__.py`, `observability.py`, `AgentLogger.swift` | **Manual rebase** — service.py delegation already in main partially; unique value: AgentLogger rotation + test_log_rotation.py |
| #657 | 717-wave marathon snapshot | 16 | Same files as #659 — identical service.py diff | **Abandon/close** — superseded by #659 (one extra doc file difference only) |
| #644 | AGENT-J Unicode regression guard | 4 | `CLAUDE.md`, `CallAutomationController.swift`, `QuickEditOverlay.swift` | **Rebase OK** — small files, no logic overlap; CLAUDE.md conflict is 1-line append |
| #640 | fix verify_claude_md.py false-positives | 11 | `CallAutomationController.swift`, `QuickEditOverlay.swift`, `scripts/verify_claude_md.py`, `test_privacy_audit.py` | **Rebase OK** — verify_claude_md.py and test_privacy_audit patch already in main; only CallAutomation SF-Symbol diff is new |
| #639 | extraction candidates doc | 10 | `CallAutomationController.swift`, `QuickEditOverlay.swift`, `test_privacy_audit.py` | **Rebase OK** — pure docs + 1-line test patch; CallAutomation uses simpler `""` approach vs #640's NSImageView approach (pick #640 first, then rebase #639) |
| #616 | Phase B Wave 82 remaining 3 codes | 5 | `error_codes.py`, `llm_rewriter.py`, `core/engine.py` | **Manual resolve** — `error_codes.py` has wave82 HIGH codes in main already (disk.critical, stt_model_cache_miss); these 3 MED codes are new, need append after existing entries |
| #589 | RecordingCoreService extraction (wave331) | 3 | `service.py` (-976 LOC), `recording_core_service.py` (+1496) | **Abandon — superseded** by #524 which does the same extraction with more completeness (+2007 LOC removed). Pick #524. |
| #528 | AnalyticsService extraction | 4 | `service.py` (-159), `analytics_service.py`, `test_ipc_dispatch_invariants.py` | **Manual rebase** — analytics_service.py already exists in main (190 lines); PR adds more methods + invariant test update; check for duplicates before merge |
| #524 | RecordingCoreService extraction (wave172) | 3 | `service.py` (-2007 LOC), `recording_core_service.py` (+1876) | **Manual resolve** — recording_core_service.py does NOT exist in main yet (0 lines); service.py conflict is large but straightforward: PR removes handlers that now exist as delegation in main |
| #508 | Phase B Wave 77 error codes | 6 | `error_codes.py`, `service.py`, `core/engine.py`, `stt_gigaam.py` | **Manual resolve** — `error_codes.py`: stt.gigaam_worker_crashed / ipc.rate_limit_exceeded not in main; service.py: +19 lines (wiring calls) |
| #456 | plugin_system + transcription_queue tests | 2 | `test_plugin_system.py` (+334 new tests into existing 658-line file), `test_transcription_queue.py` | **Rebase OK** — test-only files; conflict in test_plugin_system.py is import reorganization at top; trivial 3-line merge |
| #450 | audit_logger + recording_chain tests | 2 | `test_audit_logger.py`, `test_recording_chain.py` | **Rebase OK** — both test files exist in main (329/460 lines); PR adds new test cases only; append-style, no logic overlap |

## Conflict root causes

Three clusters drive 80% of conflicts:

1. **`service.py` extractions** (#659, #657, #589, #528, #524, #508) — main already has `STTManagementService` and `AppleIntegrationService` imports wired; all PRs that touch service.py need a diff rebase against current 5292-line main.

2. **`CallAutomationController.swift` / `QuickEditOverlay.swift`** (#644, #640, #639) — three PRs apply overlapping SF-Symbol / `●` removal patches. Best order: merge #640 first (NSImageView approach, most correct), then rebase #644 (simpler approach, may be redundant), then #639.

3. **`error_codes.py`** (#616, #508) — non-overlapping code ranges (Wave 77 codes vs Wave 82 codes), but both landed after the Wave 510 base with 57 codes. Sequential append merge is safe once ordering is clear.

## Recommended merge order

```
1. #450  — rebase OK, tests only
2. #456  — rebase OK, tests only
3. #508  — manual error_codes.py append + service.py +19 lines
4. #616  — manual error_codes.py append (after #508 merged)
5. #640  — rebase OK after CallAutomation is clean
6. #639  — rebase OK after #640
7. #644  — verify not redundant after #640; may close
8. #528  — manual: verify analytics_service.py delta vs main
9. #524  — manual surgery: large service.py (-2007)
10. CLOSE #589 — superseded by #524
11. #659  — manual rebase (STTMgmt already in main; keep AgentLogger rotation + tests)
12. CLOSE #657 — superseded by #659
```

**Quick wins (rebase in 1 command each):** #450, #456, #640, #639  
**Manual surgery required:** #524, #508, #616, #528, #659  
**Close without merge:** #589 (superseded by #524), #657 (superseded by #659)
