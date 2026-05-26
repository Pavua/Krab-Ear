# Wave 596 — Sentry Breadcrumb Coverage Audit

**Date:** 2026-05-26  
**Scope:** 5 extracted backend services  
**Status:** Audit-only (no code changes)

---

## Summary

| Service | `handle_` methods | `add_breadcrumb` calls | Coverage |
|---|---|---|---|
| `history_service.py` | 46 | 1 (`cleanup_old_history`) | ~2% |
| `settings_service.py` | 11 | 0 | 0% |
| `translation_service.py` | 6 | 1 (`translate_text`) | ~17% |
| `call_assist_service.py` | 19 | 2 (`call_dial`, `call_hangup`) | ~11% |
| `call_session_service.py` | 6 | 0 | 0% |

Note: `recording_core_service.py` referenced in task spec does not exist in this codebase; `call_session_service.py` is the analogous 6th extracted service (Wave 73, #589).

---

## Top 3 Missing Breadcrumbs per Service

### `history_service.py` (46 handlers, 1 breadcrumb)

1. **`handle_delete_history_item`** — destructive write; critical for crash attribution when a delete triggers a compaction or race. Recommend: `category="history"`, `data={"item_id": ..., "has_audio": ...}`.
2. **`handle_export_history`** / **`handle_export_history_srt`** / **`handle_export_history_csv`** — export operations are long-running and user-visible; a breadcrumb scoped to `category="history"`, `message="export_history"`, `data={"format": ..., "item_count": ...}` gives context on crash during I/O.
3. **`handle_auto_summarize_batch`** — calls LLM rewriter in a loop; failures mid-batch are opaque without a breadcrumb tracking `{"batch_size": ..., "completed": ...}`.

### `settings_service.py` (11 handlers, 0 breadcrumbs)

1. **`handle_set_settings`** — every user settings change should leave a breadcrumb. Key metadata: `{"keys_changed": [...], "preset": None}`. No PII risk — only key names.
2. **`handle_apply_profile_preset`** — changes many settings atomically; `{"preset_name": ...}` alone disambiguates common misconfiguration crashes.
3. **`handle_import_settings`** — reads external JSON; parse errors and schema mismatches should be preceded by a breadcrumb with `{"source": "import", "schema_version": ...}`.

### `translation_service.py` (6 handlers, 1 breadcrumb)

1. **`handle_translate_selection`** — secondary translate path (Cmd+Shift+T AX flow); has distinct failure modes from `translate_text` but shares no breadcrumb. Recommend same shape as existing `translate_text` breadcrumb.
2. **`handle_set_translation_glossary_item`** — glossary mutations persist to disk; `{"term": ..., "action": "set"}` (term only, no translation text) is PII-safe and useful.
3. **`handle_get_glossary_suggestions`** — triggers `GlossaryAutoLearn`; long or failing runs leave no trace. `{"suggestion_count": ...}` suffices.

### `call_assist_service.py` (19 handlers, 2 breadcrumbs)

1. **`handle_summary`** — LLM-backed call summary; most likely to fail after `call_hangup` breadcrumb; `{"transcript_segments": ..., "engine": ...}` closes the gap.
2. **`handle_quick_phrase`** — user-visible real-time action; failures between `call_dial` and `call_hangup` are invisible. `{"phrase_id": ..., "mode": ...}`.
3. **`handle_cost_estimate`** — called before dial; if gateway rejects the call, the cost estimate breadcrumb `{"provider": ..., "estimated_minutes": ...}` is the only pre-dial context.

### `call_session_service.py` (6 handlers, 0 breadcrumbs)

1. **`handle_call_session_create`** — creates persistent session record; `{"provider": ..., "call_id": ...}` anchors subsequent session-lifecycle breadcrumbs.
2. **`handle_call_session_end`** — terminal state transition; `{"call_id": ..., "duration_sec": ..., "final_status": ...}` mirrors pattern already in `call_assist_service`.
3. **`handle_call_session_update_status`** — intermediate state transitions (`dialing→connected→talking`) invisible without breadcrumbs; `{"call_id": ..., "new_status": ...}`.

---

## Recommended Add List (priority order)

| Priority | Service | Method | `category` | Key `data` fields |
|---|---|---|---|---|
| HIGH | settings_service | `handle_set_settings` | `settings` | `keys_changed` |
| HIGH | history_service | `handle_delete_history_item` | `history` | `item_id`, `has_audio` |
| HIGH | call_session_service | `handle_call_session_create` | `call` | `provider`, `call_id` |
| HIGH | call_session_service | `handle_call_session_end` | `call` | `call_id`, `duration_sec`, `final_status` |
| MED | settings_service | `handle_apply_profile_preset` | `settings` | `preset_name` |
| MED | history_service | `handle_auto_summarize_batch` | `history` | `batch_size`, `completed` |
| MED | translation_service | `handle_translate_selection` | `translation` | `source_lang`, `target_lang`, `engine` |
| MED | call_assist_service | `handle_summary` | `call` | `transcript_segments`, `engine` |
| LOW | settings_service | `handle_import_settings` | `settings` | `source`, `schema_version` |
| LOW | history_service | `handle_export_history` | `history` | `format`, `item_count` |
| LOW | call_assist_service | `handle_quick_phrase` | `call` | `phrase_id`, `mode` |
| LOW | call_session_service | `handle_call_session_update_status` | `call` | `call_id`, `new_status` |

---

## Privacy Notes

- No transcript text in any `data` dict — only metadata (counts, languages, method names).
- Phone numbers: use existing `mask_phone()` helper (already imported in `call_assist_service`).
- Settings keys are safe to log (no values).
- `item_id` is a UUID — no PII.
