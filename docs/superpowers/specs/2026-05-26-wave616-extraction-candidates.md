# Wave 616 — service.py Extraction Candidates

**Date:** 2026-05-26
**Branch:** wave616/extract-analysis
**Status:** DOC ONLY — no code changes

## Baseline

| Metric | Value |
|--------|-------|
| `service.py` LOC | 5 478 |
| Active handlers | 86 |
| Services extracted (prior) | 9 |

---

## Handler Cluster Analysis

Clusters formed by semantic prefix + domain cohesion.

### Cluster 1 — `FileTranscribeService` ⭐ Highest priority

**Handlers (5), ~1 122 LOC:**

| Handler | Line | LOC |
|---------|------|-----|
| `_handle_transcribe_paths` | 3258 | 249 |
| `_handle_transcribe_paths_async` | 3507 | 154 |
| `_handle_get_transcribe_progress` | 3661 | 43 |
| `_handle_cancel_transcribe_job` | 3704 | 12 |
| `_handle_preview_transcribe_paths` | 3716 | 664 |

**Proposed file:** `backend/file_transcribe_service.py`

**Dependencies:** `JobTracker`, `TranscriptionQueue`, `AudioConverter`, `AudioEngine` (via `self._transcribe_paths_core`). The internal helper `_transcribe_paths_core` (~300 LOC, line ~3758) must move with the cluster.

**LOC saved in service.py:** ~1 400 (handlers + core helper).

**Risks:**
- `_transcribe_paths_core` calls `self._engine`, `self._store`, `self._settings_service`, `self._error_bus` — constructor injection of 4 collaborators required.
- `_handle_preview_transcribe_paths` (664 LOC) is the largest single handler; edge cases around iCloud path copy (errno 11 workaround) must be preserved exactly.
- `_handle_transcribe_paths_async` spawns a `threading.Thread` — thread lifecycle stays internal to service; no new surface.

---

### Cluster 2 — `AppleIntegrationService` ⭐⭐ Medium priority

**Handlers (6), ~300 LOC:**

| Handler | Line | LOC |
|---------|------|-----|
| `_handle_send_to_telegram` | 4594 | 59 |
| `_handle_create_apple_note` | 4653 | 42 |
| `_handle_create_apple_reminder` | 4695 | 52 |
| `_handle_create_calendar_event` | 4747 | 59 |
| `_handle_send_imessage` | 4806 | 49 |
| `_handle_list_telegram_chats` | 4855 | 39 |

**Proposed file:** `backend/apple_integration_service.py`

**Dependencies:** `TelegramBridge`, `osascript` subprocess calls (Notes, Calendar, Reminders, iMessage). No heavy engine deps — collaborators: `self._telegram_bridge` + `subprocess`.

**LOC saved in service.py:** ~300.

**Risks:**
- Low — all handlers are thin wrappers around osascript/TelegramBridge. No shared state beyond `self._telegram_bridge`.
- `_handle_send_to_telegram` has retry logic; move as-is.

---

### Cluster 3 — `STTManagementService` ⭐⭐⭐ Medium priority

**Handlers (6), ~195 LOC:**

| Handler | Line | LOC |
|---------|------|-----|
| `_handle_add_stt_hotword` | 4894 | 30 |
| `_handle_remove_stt_hotword` | 4924 | 19 |
| `_handle_list_stt_hotwords` | 4943 | 18 |
| `_handle_warmup_stt` | 2666 | 19 |
| `_handle_get_stt_routing_decision` | 2979 | 68 |
| `_handle_select_model` | 5114 | 41 |

**Proposed file:** `backend/stt_management_service.py`

**Dependencies:** `VocabularyStore`, `Transcriber`, `STTRouter`, `SmartModelSelector`. Constructor receives `self._transcriber`, `self._vocabulary_store`, `self._stt_router`.

**LOC saved in service.py:** ~195.

**Risks:**
- `_handle_warmup_stt` touches `self._stt_warmup_done` flag (thread-safety: access under lock or pass flag by reference).
- `_handle_select_model` mutates `self._settings_service` — needs injection.
- Handlers are non-contiguous in file (lines 2666, 2979, 4894–5155); no ordering risk.

---

## Excluded Clusters

| Cluster | Handlers | LOC | Reason excluded |
|---------|----------|-----|-----------------|
| `semantic_search` | 3 | 464 | Heavy: `_handle_semantic_search_reindex` (421 LOC) touches engine + store; high blast radius |
| `action_items` | 3 | 105 | Only 3 handlers; sub-threshold for extraction overhead |
| `dedup` | 3 | 34 | Only 34 LOC; extraction overhead > benefit |

---

## Recommended Execution Order

1. **Cluster 2** (AppleIntegrationService) — fewest deps, safest first ship.
2. **Cluster 3** (STTManagementService) — moderate deps, non-contiguous but clean.
3. **Cluster 1** (FileTranscribeService) — highest LOC reduction, highest complexity; do last.

**Projected post-extraction service.py LOC:** ~3 983 (−1 495, −27%).

---

## Implementation Notes

- Follow established pattern: new service takes collaborators in `__init__`; `BackendService` imports + delegates via `handle_request` lookup table.
- Tests: each cluster needs `tests/test_<service_name>.py` with `FakeRecorder`/`FakeStore` stubs.
- CI gate: all 86 handlers must remain registered in `handle_request`; add invariant assertion to `test_backend_service.py`.
