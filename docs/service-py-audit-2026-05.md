# service.py audit 2026-05 (Wave 161)

## Current state
- **LOC**: 5 765 (was 5 777 before this PR; down ~600 from pre-Wave-73 baseline)
- **Active dispatch entries**: 101
- **Total `_handle_` methods**: 102 (101 wired + 1 internal helper)
- **`__init__` collaborators**: ~65 instantiated objects

## Service extractions to date
| Wave | PR | Service | Handlers extracted |
|------|----|---------|--------------------|
| 73 | #420 | `CallSessionService` | ~12 |
| 74 | #432 | `AudioAnalyticsService` | 8 |
| 75 | #433 | `VocabularyService` (Wave 75) | ~9 |
| 76 | #434 | `ReportingService` | ~4 |
| 88 | #435 | `IntegrationService` | ~5 |
| 65 | #410 batch 1 | Dead handler removal | 19 removed |
| 65 | #411 batch 2 | Dead handler removal | ~40 removed |
| 65 | #412 batch 3 | Dead handler removal | `_handle_get_calendar_link`, `_handle_search_by_calendar_event`, `_handle_get_speaker_statistics` |

## Next extraction candidates (>8 related handlers)

### 1. RecordingCoreService — 13 handlers
**Collaborators involved**: `_transcriber`, `_recorder`, `_rt_partial`, `_job_tracker`, `_transcription_queue`, `_cost_estimator`, `_session_tracker`

Handlers:
```
start_recording, stop_recording, get_recording_state, get_recording_stats,
transcribe_paths, transcribe_paths_async, get_transcribe_progress,
cancel_transcribe_job, preview_transcribe_paths, estimate_recording_cost,
warmup_stt, get_recording_insights, compare_recordings
```

**Notes**: `_handle_stop_recording` (616 LOC) and `_handle_preview_transcribe_paths` (664 LOC) are the two largest methods in the file — both are prime refactor targets after extraction.

### 2. AnalyticsService — 10 handlers (excluding already-extracted)
**Collaborators**: `_analytics_dashboard`, `_quality_trends`, `_activity_calendar`, `_sentiment_trends`, `_stats_report`, `_daily_digest`, `_usage_tracker`

Handlers:
```
get_analytics_dashboard, get_sentiment_trends, get_quality_trends (implicit),
get_activity_calendar, generate_daily_digest, get_usage_stats,
compare_periods, get_daily_cost_summary, generate_stats_report,
generate_mini_stats_report
```

### 3. TextProcessingService — 11 handlers
**Collaborators**: `_text_postprocessor`, `_readability_scorer`, `_transcription_scorer`, `_text_comparator`, `_abbreviation_expander`, `_term_extractor`, `_topic_tracker`, `_emotion_detector`

Handlers:
```
summarize_text, summarize_item, warmup_rewriter, extract_terms,
compare_texts, score_readability, score_transcription, detect_emotion,
expand_abbreviations, post_process_text, list_post_process_steps
```

## Method length outliers (>100 LOC)

| Method | Start line | LOC | Notes |
|--------|-----------|-----|-------|
| `_handle_preview_transcribe_paths` | ~3769 | 664 | Audio import pipeline — candidate for RecordingCoreService |
| `_handle_stop_recording` | ~1365 | 616 | Core STT pipeline — biggest refactor target |
| `_handle_semantic_search_reindex` | ~692 | 424 | Semantic index rebuild — complex but self-contained |
| `_handle_transcribe_paths` | ~3311 | 249 | Sync transcription job — pairs with async variant |
| `_handle_connection` | ~5573 | 205 | IPC connection loop — infrastructure, keep in service |
| `_handle_start_recording` | ~1208 | 157 | Recording setup — pairs with stop_recording |
| `_handle_transcribe_paths_async` | ~3560 | 154 | Async transcription job |
| `_handle_report_reconnect` | ~2504 | 136 | Auto-reconnect handling |
| `_handle_set_paste_status` | ~2018 | 124 | Paste status updates |

## Cleanup applied this PR (Wave 161)

### Removed 5 unused imports
```
from backend.speaker_statistics import SpeakerStatisticsAnalyzer   # handler removed Wave 65
from core.text_anonymizer import TextAnonymizer                      # delegated via TextPostProcessor
from core.speech_pace import SpeechPaceAnalyzer                      # handler removed Wave 65
from core.hallucination_manager import HallucinationManager           # handler removed Wave 65
from backend.calendar_link import CalendarLinker                     # handler removed Wave 65 batch 3
```

### Removed 5 unused `__init__` instantiations
```python
self._speaker_statistics = SpeakerStatisticsAnalyzer()
self._hallucination_manager = HallucinationManager(data_dir=self.store.data_dir)
self._speech_pace_analyzer = SpeechPaceAnalyzer()
self._text_anonymizer = TextAnonymizer()
self._calendar_linker = CalendarLinker(cache_minutes=int(settings.CALENDAR_LINK_CACHE_MIN))
```

**Root cause**: Wave 65 batches 1-3 removed the corresponding dispatch handlers but left behind the import and instantiation lines. These 5 collaborators had zero references outside `__init__`.

**LOC removed**: 12 lines total (5 imports + 5 `__init__` assignments + 2 continuation lines for CalendarLinker).

## Validation
- `flake8 KrabEar/backend/service.py --select=F401` — clean
- `pytest test_backend_service.py test_dispatch_complete.py` — **284 passed**
