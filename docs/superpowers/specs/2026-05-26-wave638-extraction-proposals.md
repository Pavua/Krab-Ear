# Wave 638 — service.py Extraction Proposals

**Date:** 2026-05-26  
**Baseline:** service.py = 5478 LOC, 86 active handlers

## Cluster analysis (top 3 ≥6 handlers or high LOC)

### 1. TranscriptionJobService — NEW file `backend/transcription_job_service.py`

**Handlers (5, ~1122 LOC):**
| Method | Line | LOC |
|--------|------|-----|
| `_handle_transcribe_paths` | 3258 | 249 |
| `_handle_transcribe_paths_async` | 3507 | 154 |
| `_handle_get_transcribe_progress` | 3661 | 43 |
| `_handle_cancel_transcribe_job` | 3704 | 12 |
| `_handle_preview_transcribe_paths` | 3716 | 664 |

**Dependencies:** `TranscriptionQueue`, `JobTracker`, `Transcriber`, `StateStore`  
**Estimated service.py reduction:** ~1122 LOC (~20%)  
**Priority:** HIGH — largest contiguous block, strongly cohesive

---

### 2. ExternalIntegrationsService — NEW file `backend/external_integrations_service.py`

**Handlers (6, ~283 LOC):**
| Method | Line | LOC |
|--------|------|-----|
| `_handle_send_to_telegram` | 4594 | 42 |
| `_handle_create_apple_note` | 4653 | 42 |
| `_handle_create_apple_reminder` | 4695 | 52 |
| `_handle_create_calendar_event` | 4747 | 59 |
| `_handle_send_imessage` | 4806 | 49 |
| `_handle_list_telegram_chats` | 4855 | 39 |

**Dependencies:** `TelegramBridge`, osascript helpers (Notes/Reminders/Calendar)  
**Estimated service.py reduction:** ~283 LOC (~5%)  
**Priority:** HIGH — 6 methods, zero overlap with other services, clear domain boundary

---

### 3. STTManagementService — NEW file `backend/stt_management_service.py`

**Handlers (6, ~195 LOC):**
| Method | Line | LOC |
|--------|------|-----|
| `_handle_add_stt_hotword` | 4894 | 30 |
| `_handle_remove_stt_hotword` | 4924 | 19 |
| `_handle_list_stt_hotwords` | 4943 | 18 |
| `_handle_select_model` | 5114 | 41 |
| `_handle_warmup_stt` | 2666 | 19 |
| `_handle_get_stt_routing_decision` | 2979 | 68 |

**Dependencies:** `VocabularyStore`, `SmartModelSelector`, `STTRouter`, `Transcriber`  
**Estimated service.py reduction:** ~195 LOC (~4%)  
**Priority:** HIGH — 6 methods tightly coupled to STT layer, isolated from recording/history

---

## Honourable mentions (< 6 handlers, smaller LOC)

| Cluster | Methods | LOC | Suggested file |
|---------|---------|-----|----------------|
| SemanticSearch | 3 | ~464 | delegate to existing `semantic_search.py` |
| ActionItems | 3 | ~105 | delegate to existing `action_items_extractor.py` |
| Deduplication | 3 | ~34 | delegate to existing `auto_deduplication.py` |

---

## Combined impact (top 3)

- Handlers moved out: **17**
- LOC removed from service.py: **~1600** (5478 → ~3878, −29%)
- Pattern: constructor injects `store` + specific collaborators; `BackendService` delegates via `self._<service>.<handle_*>(params)`

## Next step

Implement in order: TranscriptionJobService → ExternalIntegrationsService → STTManagementService  
Each extraction = independent PR (file-isolated, low merge conflict risk).
