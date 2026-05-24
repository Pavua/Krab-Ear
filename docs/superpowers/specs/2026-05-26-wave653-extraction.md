# Wave 653 — service.py Extraction Candidates

**Date:** 2026-05-26  
**Branch:** wave636/extract-analysis  
**Baseline:** service.py = 5 478 LOC, 86 active handlers, 9 services extracted (prior)  
**Prior art:** wave616 spec (`docs/superpowers/specs/2026-05-26-wave616-extraction-candidates.md`), wave638 spec.

---

## Cluster Analysis (top 3 ≥ 5 handlers / high LOC)

### Cluster 1 — `FileTranscribeService` ⭐ HIGHEST priority

**File:** `backend/file_transcribe_service.py`  
**Handlers (5) + 1 core helper:**

| Symbol | Line | LOC |
|--------|------|-----|
| `_handle_transcribe_paths` | 3258 | 249 |
| `_handle_transcribe_paths_async` | 3507 | 154 |
| `_handle_get_transcribe_progress` | 3661 | 43 |
| `_handle_cancel_transcribe_job` | 3704 | 12 |
| `_handle_preview_transcribe_paths` | 3716 | 664 |
| `_transcribe_paths_core` (helper) | 3265 | ~246 |
| `_collect_audio_paths` (helper) | 3758 | ~30 |
| `_extract_transcribed_text` (helper) | 4339 | ~24 |
| `_extract_transcribed_error` (helper) | 4363 | ~17 |

**Total LOC saved:** ~1 440 (~26% of service.py)  
**Dependencies (inject in constructor):** `Transcriber`, `JobTracker`, `TranscriptionQueue`, `AudioConverter`, `StateStore`, `SettingsService`, `ErrorBus`  
**Risks:**
- `_handle_preview_transcribe_paths` (664 LOC) contains iCloud errno 11 path-copy workaround — must move verbatim.
- `_transcribe_paths_core` shares `_safe_callback` (L1283) and `_format_text_with_speakers` (L2107) with other clusters; those stay in `BackendService` as static/module helpers, or are duplicated as private statics in the new service.
- `_handle_transcribe_paths_async` spawns `threading.Thread` — thread lifecycle is self-contained; no new public surface needed.
- `_cached_settings` access pattern: use `SettingsService.get()` instead of direct `self._cached_settings`.

---

### Cluster 2 — `AppleIntegrationService` ⭐⭐ MEDIUM priority

**File:** `backend/apple_integration_service.py`  
**Handlers (6):**

| Symbol | Line | LOC |
|--------|------|-----|
| `_handle_send_to_telegram` | 4594 | 59 |
| `_handle_create_apple_note` | 4653 | 42 |
| `_handle_create_apple_reminder` | 4695 | 52 |
| `_handle_create_calendar_event` | 4747 | 59 |
| `_handle_send_imessage` | 4806 | 49 |
| `_handle_list_telegram_chats` | 4855 | 39 |

**Total LOC saved:** ~300  
**Dependencies:** `TelegramBridge`, `subprocess` (osascript). No engine deps.  
**Risks:** Low — thin osascript wrappers + `TelegramBridge` calls. Safest first ship.

---

### Cluster 3 — `STTManagementService` ⭐⭐⭐ MEDIUM priority

**File:** `backend/stt_management_service.py`  
**Handlers (6):**

| Symbol | Line | LOC |
|--------|------|-----|
| `_handle_warmup_stt` | 2666 | 19 |
| `_handle_get_stt_routing_decision` | 2979 | 68 |
| `_handle_add_stt_hotword` | 4894 | 30 |
| `_handle_remove_stt_hotword` | 4924 | 19 |
| `_handle_list_stt_hotwords` | 4943 | 18 |
| `_handle_select_model` | 5114 | 41 |

**Total LOC saved:** ~195  
**Dependencies:** `VocabularyStore`, `Transcriber`, `STTRouter`, `SmartModelSelector`, `SettingsService`  
**Risks:**
- `_handle_warmup_stt` reads `self._stt_warmup_done` flag — pass as `threading.Event` or `bool` ref in constructor.
- `_handle_select_model` writes settings via `SettingsService` — inject.
- Handlers non-contiguous (lines 2666, 2979, 4894–5155); reorder freely in new file.

---

## Excluded Clusters

| Cluster | Handlers | LOC | Reason |
|---------|----------|-----|--------|
| `semantic_search` | 3 | 464 | `_handle_semantic_search_reindex` (421 LOC) touches engine + store + rebuild thread; high blast radius |
| `action_items` | 3 | 105 | Below 5-handler threshold |
| `dedup` | 3 | 34 | 34 LOC; overhead > benefit |

---

## Recommended Order

1. Cluster 2 (AppleIntegrationService) — fewest deps, safest.
2. Cluster 3 (STTManagementService) — moderate deps.
3. Cluster 1 (FileTranscribeService) — max LOC reduction, highest complexity.

**Projected post-extraction service.py LOC:** ~3 543 (−1 935, −35%).

---

## Implementation Notes

- Pattern: new service `__init__(self, dep1, dep2, ...)`, methods named `handle_<method>`, `BackendService` delegates via `handle_request` lookup table.
- Each cluster needs `tests/test_<service_name>.py` with stub collaborators.
- CI invariant: 86 handlers must remain registered; assert in `test_backend_service.py`.
