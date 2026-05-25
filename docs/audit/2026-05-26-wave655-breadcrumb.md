# Wave 655 — Sentry Breadcrumb Audit (5 services)

**Date:** 2026-05-26  
**Branch:** wave655/breadcrumb-audit  
**Scope:** history_service, settings_service, translation_service, call_assist_service, recording core (service.py)

---

## Methodology

grep for `add_breadcrumb` vs `def handle_*` / `def _handle_*` in each file.  
Only state-mutating and error-prone methods are considered high-value targets.

---

## 1. `history_service.py`

**Current coverage:** 1 breadcrumb (`handle_cleanup_old_history`).  
~40 handlers uncovered.

| Priority | Method | Why it matters |
|----------|--------|----------------|
| HIGH | `handle_delete_history_item` | Irreversible delete; crash here loses data silently |
| HIGH | `handle_import_history_ndjson` | Bulk import; failure hard to trace without crumb |
| MED  | `handle_export_history` / `handle_export_history_srt` | Long-running; user-visible failures |

---

## 2. `settings_service.py`

**Current coverage:** 0 breadcrumbs across all 11 handlers.

| Priority | Method | Why it matters |
|----------|--------|----------------|
| HIGH | `handle_set_settings` | Every config change; root cause for most misbehavior crashes |
| HIGH | `handle_import_settings` | Overwrites all settings; irreversible if crash mid-write |
| MED  | `handle_apply_profile_preset` | Changes multiple keys atomically; silent failure confusing |

---

## 3. `translation_service.py`

**Current coverage:** 1 breadcrumb (`handle_translate_text`).

| Priority | Method | Why it matters |
|----------|--------|----------------|
| HIGH | `handle_translate_selection` | AX-API path; paste-back failure hard to attribute |
| MED  | `handle_set_translation_glossary_item` | Glossary mutation; missing = no trail for glossary corruption |
| MED  | `handle_get_glossary_suggestions` | Triggers NLP pipeline; latency spikes invisible |

---

## 4. `call_assist_service.py`

**Current coverage:** 2 breadcrumbs (`handle_start`, `handle_stop`).  
~13 handlers uncovered.

| Priority | Method | Why it matters |
|----------|--------|----------------|
| HIGH | `handle_summary` | Post-call summary; LLM failure here is a user-facing loss |
| HIGH | `handle_timeline_to_history` | Writes call session into history store; silent corruption risk |
| MED  | `handle_cost_estimate` | Pre-call; error here blocks call start without trace |

---

## 5. Recording core (`service.py` recording handlers)

**Current coverage:** `_handle_start_recording` ✓, `_handle_stop_recording` ✓ (transcribe path) ✓.

| Priority | Method | Why it matters |
|----------|--------|----------------|
| HIGH | `_handle_cancel_transcribe_job` | Job cancellation; async state machine; crash leaves ghost jobs |
| MED  | `_handle_get_recording_state` | Polled by Swift; silent mismatch vs backend state hard to diagnose |
| MED  | `_handle_get_recording_stats` | Aggregates metrics; error here breaks dashboard silently |

---

## Recommended next step

Add `add_breadcrumb(category="history|settings|translation|call|recording", message="method_name", data={...})` at the **start** of each HIGH-priority handler (before the main try-block). Data fields: operation identifier only — no transcript text (privacy rule from PR #238).
