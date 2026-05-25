# service.py audit v3 (2026-05-22)

## Current state

| Metric | Value | Previous (Wave 161) |
|--------|-------|---------------------|
| LOC | **5476** | 5777 (pre-Wave-73 peak) |
| Active dispatch entries | **86** | 305 (Wave 161) / 296 (Wave 290 sync) |
| `_handle_*` private methods | **87** | — |
| `__init__` collaborators (`self._*`) | **99** | — |
| Imported service classes | **106** | — |
| Import lines | **129** | — |

> Note: The drop from 305→86 dispatch entries reflects that 6 extracted services now own their
> own dispatch tables internally; the top-level `handle_request` in `BackendService` delegates to
> those services for the bulk of methods. The 86 remaining are methods that still live directly
> in `BackendService`.

---

## Methods >100 LOC

| Method | LOC | Notes |
|--------|-----|-------|
| `__init__` | 400 | 99 collaborator inits — see extraction roadmap |
| `handle_request` | 390 | Dispatch table — shrinks as services extracted |
| `_transcribe_paths_core` | 242 | Core recording path, complex state machine |
| `_stop_recording_phase_e` | 158 | Recording teardown phase — pair with phase_d |
| `_handle_transcribe_paths_async` | 154 | Async job wrapper |
| `_stop_recording_phase_d` | 141 | Recording teardown phase |
| `_handle_connection` | 128 | Socket connection loop — infrastructure, not a handler |

`_handle_connection` is **not** in the dispatch table; it is called via `threading.Thread(target=self._handle_connection)` — it is the per-client socket loop, not a dead handler.

---

## Services extracted (history)

| # | Service | Wave | LOC removed (approx) |
|---|---------|------|----------------------|
| 1 | `CallSessionService` | Wave 73 | ~400 |
| 2 | `AudioAnalyticsService` | Wave 73 | ~350 |
| 3 | `VocabularyService` | Wave 74 | ~300 |
| 4 | `ReportingService` | Wave 75 | ~280 |
| 5 | `IntegrationService` | Wave 76 | ~250 |
| 6 | `RecordingCoreService` | Wave 331 | **-833** (largest) |

Cumulative removal: ~2400 LOC. Current 5476 vs estimated 7800+ pre-extraction peak.

---

## Next extraction candidates (ranked by cohesion + LOC)

### 1. AnalyticsService (~194 LOC, 8 handlers) — HIGHEST PRIORITY

Handlers:
- `compare_periods`, `generate_daily_digest`, `get_activity_calendar`
- `get_analytics_dashboard`, `get_metrics_dashboard`, `get_sentiment_trends`
- `get_timeline_view`, `get_topic_timeline`

Collaborators used: `_analytics_dashboard`, `_sentiment_trends`, `_quality_trends`,
`_daily_digest`, `_activity_calendar`, `_timeline_view`, `_speaker_stats`.

All read-only aggregators with no recording-state dependency. Clean extraction boundary.
Estimated file: `backend/analytics_service.py` (already exists as `backend/analytics_dashboard.py` — can be extended or a new thin service wrapping existing modules).

### 2. TextScoringService (~203 LOC, 8 handlers) — HIGH PRIORITY

Handlers:
- `extract_terms`, `get_keyword_cloud`, `warmup_rewriter`
- `score_transcription`, `generate_auto_title`, `list_normalization_profiles`
- `replace_word_in_last_transcript`, `get_last_llm_diff`

Collaborators: `_term_extractor`, `_keyword_cloud`, `_llm_rewriter`,
`_transcription_scorer`, `_auto_title`, `_normalization_profiles`.

Pure text/scoring logic, no audio or recording state. `warmup_rewriter` touches `_llm_rewriter`
which is already in `backend/llm_rewriter.py` — clean boundary.
Warning: `replace_word_in_last_transcript` also touches `_store` (history) — needs store injection.

### 3. HealthCheckService (~144 LOC, 7 handlers) — MEDIUM PRIORITY

Handlers:
- `check_integrity`, `get_diagnostics`, `get_startup_diagnostics`
- `health_check`, `ping`, `repair_integrity`, `send_diagnostics_to_sentry`

Collaborators: `_health_checker`, `_integrity_checker`, `_startup_diagnostics`.

`get_diagnostics` is 53 LOC (largest single handler here). All purely diagnostic reads.
`send_diagnostics_to_sentry` needs Sentry import — already in `backend/observability.py`.
Estimated file: `backend/health_check_service.py`.

### 4. HotwordService (~67 LOC, 3 handlers) — QUICK WIN

Handlers: `add_stt_hotword`, `list_stt_hotwords`, `remove_stt_hotword`

Collaborators: `_hotword_detector` only. Trivially isolated.

---

## Groupings for future waves

| Candidate | Handlers | Est LOC | Priority |
|-----------|----------|---------|----------|
| AnalyticsService | 8 | ~194 | HIGH |
| TextScoringService | 8 | ~203 | HIGH |
| HealthCheckService | 7 | ~144 | MEDIUM |
| HotwordService | 3 | ~67 | QUICK WIN |
| ExportService (extend) | 3 | ~120 | MEDIUM |
| MessagingService (Telegram/iMessage/Calendar) | 4 | ~220 | MEDIUM |

Extracting AnalyticsService + TextScoringService alone would remove ~400 LOC and reduce
dispatch entries by 16, bringing `service.py` toward ~5050 LOC.

---

## Dead handler scan

Running automated scan (dispatch table vs private method defs):

- **Confirmed dead in dispatch**: 0 (all `_handle_*` methods are reachable via dispatch or
  direct internal call)
- `_handle_connection`: not a dispatch handler — it is the per-client socket loop called via
  `threading.Thread`. Not dead.
- **Potential dead imports**: `ERROR_REGISTRY` is imported 7× in the import list (repeated
  `from backend.error_codes import ERROR_REGISTRY` lines) — should be deduplicated.
- **KrabError**: similarly imported 6× — deduplication opportunity (1 import line covers all).

Recommend running `audit_dead_ipc_handlers.py` per Wave 65 methodology for full Python+Swift scope.

---

## Cleanup opportunities

1. **Duplicate imports**: `ERROR_REGISTRY` imported 7×, `KrabError` imported 6× — consolidate
   to single import statements (~12 lines saved, no behavior change).
2. **`__init__` bloat**: 99 collaborator assignments in `__init__` at 400 LOC. As services are
   extracted, inject collaborators into service constructors instead of creating them in
   `BackendService.__init__`. Target: reduce to ~50 assignments (<200 LOC) post next 2 extractions.
3. **`handle_request` dispatch table** (390 LOC): will shrink naturally as handlers move to
   extracted services. Target <150 LOC for the remaining `BackendService`-owned methods.
4. **`_transcribe_paths_core`** (242 LOC): consider splitting into sub-phases matching the
   existing `_stop_recording_phase_d/e` pattern. Not an extraction candidate (recording state
   coupling) but an internal refactor opportunity.

---

## Roadmap summary

```
Wave 381 → AnalyticsService extraction   (-194 LOC, -8 dispatch entries)
Wave 382 → TextScoringService extraction (-203 LOC, -8 dispatch entries)
Wave 383 → HealthCheckService extraction (-144 LOC, -7 dispatch entries)
Wave 384 → HotwordService quick win      (-67 LOC,  -3 dispatch entries)
---
Target after Wave 384: ~4868 LOC, ~60 remaining dispatch entries in BackendService
```

At ~4500 LOC the file would be comfortable to maintain without further extraction pressure.
