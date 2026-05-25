# Wave 249-274 batch wrap + v2.0.3 SHIP (2026-05-20 → 2026-05-21)

## Headline accomplishment

**v2.0.3 SHIPPED** — Wave 274, commit `018581c`:
> "Wave 67 SF Symbol fix + 67 waves shipped"

AGENT-J fix (Wave 67 — `●` Unicode literal → SF Symbol `circle.fill`) was source-only since
2026-05-18. The v2.0.2 binary shipped 2026-05-14 never included it, causing AGENT-J regression
4→6 events. Wave 274 closes the source→binary gap.

## PRs in this batch (Waves 249-274, 19 commits)

| Wave | PR   | Description |
|------|------|-------------|
| 274  | —    | chore: v2.0.3 ship (binary rebuild + git tag) |
| 243  | #572 | recording_scheduler + bookmarks coverage |
| 238  | #571 | transcription_scorer + readability_scorer coverage |
| 233  | #570 | hallucination_manager + voice_commands coverage |
| 228  | #569 | paste_formatter + datetime_normalizer coverage |
| 222  | #567 | feat: _push_error guards surface internal failures to Sentry |
| 221  | #568 | E2E IPC happy path integration (8 workflows, 37 tests) |
| 218  | #565 | AuditLogger rotation deep edge cases |
| 217  | #564 | settings migration + validator deep tests |
| 216  | #562 | backend graceful shutdown lifecycle + signal handlers |
| 212  | #558 | translate_selection handler backend tests |
| 210  | #557 | Sentry tags + release tracking deep tests |
| 190  | #538 | Swift LiveSubtitlesOverlay unit tests |
| 189  | #537 | STT adapter common interface parity tests |
| 185  | #526 | call_cost_estimator + call_silence_probe + call_auto_end coverage |
| 184  | #527 | call_session + call_session_store coverage |
| 183  | #525 | observability edge cases + privacy contract reinforcement |
| 182  | #531 | live_subs_service deep coverage |

## Key findings

### Two-binary drift — confirmed gotcha
Source-only fixes need an explicit binary rebuild + ship event. Wave 67 (2026-05-18) fixed the
source; Wave 274 (2026-05-20) did the rebuild. Gap = 2 days of users running broken binary despite
fix being "done". Lesson: always rebuild + codesign + cp bundle→runtime immediately after any
Swift fix that touches UI rendering paths.

### _push_error Sentry guards (feat #567)
Internal backend failures that previously swallowed exceptions silently now surface to Sentry via
`_push_error()`. Critical for observability: the 51-code error registry is only useful if errors
actually reach Sentry.

### E2E IPC integration tests (#568)
8 full round-trip workflows tested end-to-end: record→transcribe, translate, history CRUD,
settings get/set, call session lifecycle, export, glossary, vocabulary. 37 assertions.
First E2E IPC coverage in the suite — previous tests were all unit-level.

### Coverage sweep (Waves 182-243)
Deep test pass on: call automation stack (cost estimator, silence probe, auto-end, session store),
STT adapter interface parity, Swift LiveSubtitlesOverlay, observability edge cases, AuditLogger
rotation, settings migration, graceful shutdown, translate_selection, Sentry tags, paste formatter,
datetime normalizer, transcription scorer, readability scorer, hallucination manager, voice commands,
recording scheduler, bookmarks.

## Stats at wrap

| Metric | Value |
|--------|-------|
| Waves in batch | ~26 (249-274) |
| New PRs | ~18 (#525-#572 range) |
| v2.0.3 ship commit | `018581c` |
| Latest PR | #572 |
| Sentry backend unresolved | 1 (BACKEND-J, silent) |
| Sentry agent unresolved | 0 |
| Backend RSS | ~35 MB (stable, Wave 63 leak fix) |

## Cumulative totals (all waves across mega-marathon)

| Batch | Waves | Notes |
|-------|-------|-------|
| Round 1 | 86-106 (20) | |
| Round 2 | 107-146 (40) | |
| Bonus | 147-196 (50) | |
| Batch 4 | 197-223 (25) | |
| Batch 5 | 224-248 (25) | |
| Batch 6 (current) | 249-274 (26) | v2.0.3 SHIPPED |
| **Total** | **~186 waves** | ~132+ PRs (#399-#572) |

## Production health

- **v2.0.3** ship-ready and tagged
- AGENT-J, AGENT-K, AGENT-M: all resolved / fixed in v2.0.3 binary
- Backend RSS stable at ~35 MB (Wave 63 `mx.clear_cache()` leak fix validated long-term)
- 3 missing routines (dsym-upload-verify, two-binary-drift-watch, bench-monitor) still unregistered
  — carry-forward to next session
