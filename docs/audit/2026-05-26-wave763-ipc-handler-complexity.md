# IPC Handler Complexity Audit — wave763

**Source**: `KrabEar/backend/service.py`
**Date**: 2026-05-26
**Tool**: `scripts/audit_ipc_handler_complexity.py` (stdlib `ast`, no deps)

## Summary

| Metric | Count |
|--------|-------|
| Total `_handle_*` methods | 90 |
| Inline (need attention) | 51 |
| Delegated stubs (already extracted) | 39 |
| With risky calls (subprocess/network/sleep) | 1 |
| With lock usage | 1 |

43% of handlers are already thin delegation stubs — good progress from prior
extraction waves (W172/W392/W404/W423/W525/W683/W691/W734/W741–W751).
51 inline handlers remain as candidates for future extraction or simplification.

## Top 10 Inline Handlers by LOC (excluding delegated stubs)

| Rank | Handler | LOC | CC | Risky calls | Locks |
|------|---------|-----|----|-----------  |-------|
| 1 | `_handle_import_glossary_csv` | 95 | 17 | — | — |
| 2 | `_handle_get_recording_stats` | 71 | 13 | — | — |
| 3 | `_handle_replace_word_in_last_transcript` | 55 | 8 | — | — |
| 4 | `_handle_batch` | 54 | 7 | — | — |
| 5 | `_handle_connection` | 52 | 14 | `socket.` | — |
| 6 | `_handle_list_llm_models` | 45 | 6 | — | — |
| 7 | `_handle_batch_extract_action_items` | 41 | 10 | — | — |
| 8 | `_handle_summarize_item` | 38 | 8 | — | — |
| 9 | `_handle_extract_action_items` | 37 | 9 | — | — |
| 10 | `_handle_get_memory_stats` | 36 | 10 | — | — |

## Top 10 Inline Handlers by Cyclomatic Complexity

| Rank | Handler | LOC | CC | Risky calls |
|------|---------|-----|----|-------------|
| 1 | `_handle_import_glossary_csv` | 95 | 17 | — |
| 2 | `_handle_connection` | 52 | 14 | `socket.` |
| 3 | `_handle_get_recording_stats` | 71 | 13 | — |
| 4 | `_handle_batch_extract_action_items` | 41 | 10 | — |
| 5 | `_handle_get_memory_stats` | 36 | 10 | — |
| 6 | `_handle_extract_action_items` | 37 | 9 | — |
| 7 | `_handle_replace_word_in_last_transcript` | 55 | 8 | — |
| 8 | `_handle_summarize_item` | 38 | 8 | — |
| 9 | `_handle_batch` | 54 | 7 | — |
| 10 | `_handle_semantic_search` | 33 | 7 | — |

## High-Risk Handlers (subprocess / network / sleep calls)

> These handlers make blocking or external calls on the IPC thread.
> Consider delegating to a background thread or extracted service.

| Handler | LOC | CC | Risky calls | Locks |
|---------|-----|----|-------------|-------|
| `_handle_connection` | 52 | 14 | `socket.` | — |

**Note**: `_handle_connection` is the per-client IPC socket handler — the
`socket.` reference is the raw `socket.socket` type annotation/usage which is
architectural (not a network call). Still, its CC=14 means it should be
simplified or broken into phases.

## Extraction Candidates

Handlers with LOC > 30 or CC > 8 are the strongest candidates for extraction
into a dedicated service (risk score = LOC + CC × 5).

| Handler | LOC | CC | Risk score | Notes |
|---------|-----|----|------------|-------|
| `_handle_import_glossary_csv` | 95 | 17 | **180** | CSV parsing + validation loop — TranslationService candidate |
| `_handle_get_recording_stats` | 71 | 13 | **136** | Full history scan — AnalyticsService candidate |
| `_handle_connection` | 52 | 14 | **122** | IPC plumbing — simplify phases |
| `_handle_replace_word_in_last_transcript` | 55 | 8 | 95 | Multi-step store mutation |
| `_handle_batch_extract_action_items` | 41 | 10 | 91 | Batch loop — ActionItemsService candidate |
| `_handle_batch` | 54 | 7 | 89 | Recursive dispatch — architectural, keep inline |
| `_handle_get_memory_stats` | 36 | 10 | 86 | psutil calls — SystemMonitor candidate |
| `_handle_extract_action_items` | 37 | 9 | 82 | ActionItemsService candidate |
| `_handle_summarize_item` | 38 | 8 | 78 | LLM call — TextScoringService candidate |
| `_handle_list_llm_models` | 45 | 6 | 75 | HTTP + fallback logic |
| `_handle_send_diagnostics_to_sentry` | 34 | 7 | 69 | Sentry breadcrumbs loop |
| `_handle_semantic_search` | 33 | 7 | 68 | Already calls SemanticSearcher — thin it further |
| `_handle_get_topic_timeline` | 34 | 6 | 64 | Delegate to AnalyticsService |
| `_handle_get_metrics_dashboard` | 36 | 2 | 46 | Store scan inline — AnalyticsService candidate |
| `_handle_report_paste_failure` | 35 | 2 | 45 | ErrorBus wiring — acceptable inline |

## Recommended next waves

1. **W764** — extract `_handle_import_glossary_csv` (LOC 95, CC 17) into `TranslationService.handle_import_glossary_csv`. Highest risk score, pure translation domain logic.
2. **W765** — extract `_handle_get_recording_stats` (LOC 71, CC 13) into `AnalyticsService`. Already has a dashboard handler there; stats aggregation belongs alongside it.
3. **W766** — split `_handle_connection` into explicit phases (read_request → dispatch → write_response) to bring CC from 14 to ≤5 per phase.
4. **W767** — extract `_handle_extract_action_items` + `_handle_batch_extract_action_items` into a new `ActionItemsService` (combined LOC 78, related domain).

## Definitions

- **LOC**: total lines of the method (including docstring).
- **CC**: cyclomatic complexity — base 1 + 1 per `if/elif/while/for/try/except/with/ternary/comprehension` node.
- **Risky calls**: `subprocess.run/call/Popen`, `requests.*`, `socket.*`, `time.sleep`, `urllib` — blocking or external I/O on the IPC dispatch thread.
- **Delegated stub**: body is exactly `return self._<svc>.handle_*(...)` — already extracted, excluded from candidate tables.
- **Risk score**: LOC + CC × 5 (higher = stronger extraction priority).
