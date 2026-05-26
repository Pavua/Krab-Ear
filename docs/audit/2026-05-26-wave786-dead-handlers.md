# Dead IPC Handler Audit — Wave 786

**Date:** 2026-05-26
**Branch:** `feature/dead-handlers-W786`
**Methodology:** W65 three-scope check (Swift callers, Python test references, Python runtime calls)

---

## Summary

| Metric | Count |
|--------|-------|
| Raw grep hits (incl. false positives from return dicts) | 299 |
| False positives removed (return-dict keys matching `self._*`) | 10 |
| **Actual handler registrations in lookup table** | **289** |
| Dead candidates (zero refs in all 3 scopes) | **8** |

---

## Methodology

Per CLAUDE.md "Dead handler audit methodology":

1. **Extract handlers** — grep only lines 913–1244 of `service.py` (the `handlers` dict inside `handle_request`). The raw grep across the entire file yielded 299 hits, but 10 were false positives: keys in return-value dicts (e.g. `"size": self._context_memory.size()`, `"preview_duration_sec": self._recording_core_svc.preview_duration_sec`) match the `"[a-z_]+": self\._` pattern but are not IPC method registrations.

2. **Scope 1 — Swift callers:** `grep -rl '"<name>"' native/` — extracts all string literals from Swift source.

3. **Scope 2 — Python tests:** Extract all method strings and `handle_*`/`_handle_*` identifiers from `KrabEar/tests/`.

4. **Scope 3 — Python runtime:** Extract all `_handle_*` identifiers from `KrabEar/backend/` and `KrabEar/core/`, excluding `service.py` and test files.

5. **Deep verification:** For all candidates, run a broad string search across the entire repo (all `.py`, `.swift`, `.json`, `.sh`, `.command` files) excluding `service.py` and `docs/`.

---

## False Positives Removed

These keys matched the grep pattern but are **not IPC handlers** — they appear inside return-value dicts within handler methods:

| Key | Location | Reason |
|-----|----------|--------|
| `call_assist` | `_handle_get_metrics_dashboard` return dict | Field key, not IPC method |
| `context_words` | `_handle_get_context_memory` return dict | Field key |
| `devices` | return dict | Field key |
| `error_count` | return dict | Field key |
| `last_reset_ts` | return dict | Field key |
| `preview_duration_sec` | `_handle_get_metrics_dashboard` return dict | Field key |
| `profiles` | return dict | Field key |
| `recent_topics` | return dict | Field key |
| `size` | `_handle_get_context_memory` return dict | Field key |
| `status` | return dict | Field key |

---

## Dead Candidates

All 8 candidates confirmed dead in deep verification. Listed alphabetically.

### 1. `call_assist_add_template`
- **Registered:** `service.py:1155` → `self._call_assist.handle_add_template`
- **Implemented:** `backend/call_assist_service.py:633`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0 (no `call_assist_add_template` string anywhere outside service.py + docs)
- **Confidence:** HIGH — backend-only stub, never wired to Swift UI

### 2. `call_assist_cost_report`
- **Registered:** `service.py:1158` → `self._call_assist.handle_cost_report`
- **Implemented:** `backend/call_assist_service.py:777`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0
- **Confidence:** HIGH — distinct from `call_assist_cost_estimate` (which IS called from Swift)
- **Note:** `call_assist_cost_estimate` (line 930) is verified alive (Swift caller present). This is a separate, unreferenced handler.

### 3. `call_assist_list_templates`
- **Registered:** `service.py:1154` → `self._call_assist.handle_list_templates`
- **Implemented:** `backend/call_assist_service.py:627`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0
- **Confidence:** HIGH

### 4. `call_assist_remove_template`
- **Registered:** `service.py:1156` → `self._call_assist.handle_remove_template`
- **Implemented:** `backend/call_assist_service.py:657`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0
- **Confidence:** HIGH

### 5. `call_assist_template`
- **Registered:** `service.py:1157` → `self._call_assist.handle_template`
- **Implemented:** `backend/call_assist_service.py:671`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0
- **Confidence:** HIGH — the "send template phrase" action is never triggered from client code

### 6. `call_check_auto_end`
- **Registered:** `service.py:1161` → `self._call_auto_end.handle_check_auto_end`
- **Implemented:** `backend/call_auto_end.py:202` (docstring references IPC name)
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** Listed in `backend/ipc_throttle.py:99` as excluded from rate limiting
- **Confidence:** MEDIUM — the ipc_throttle.py entry suggests this handler was intended to be called (it's excluded from throttling because "polling calls from auto-end monitor loop"), but no actual client sends this method. The throttle exclusion entry is a forward-reference with no live caller.

### 7. `cleanup_stale_app_profiles`
- **Registered:** `service.py:1019` → `self._paste_app_memory.handle_cleanup_stale_app_profiles`
- **Implemented:** `backend/paste_app_memory.py:208` (method definition only)
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0 (paste_app_memory.py contains the implementation, not a caller)
- **Confidence:** HIGH

### 8. `delete_app_profile`
- **Registered:** `service.py:1018` → `self._paste_app_memory.handle_delete_app_profile`
- **Implemented:** `backend/paste_app_memory.py:202`
- **Swift callers:** 0
- **Python test refs:** 0
- **Python runtime refs:** 0
- **Confidence:** HIGH

---

## Removal Recommendation

Do **not** auto-remove in this wave. This audit is report-only per W786 task spec.

Priority order for a future removal wave:
1. HIGH confidence (7 handlers): `call_assist_add_template`, `call_assist_cost_report`, `call_assist_list_templates`, `call_assist_remove_template`, `call_assist_template`, `cleanup_stale_app_profiles`, `delete_app_profile`
2. MEDIUM confidence (1 handler): `call_check_auto_end` — verify with Pablo whether Call Auto End polling loop was ever intended to call this via IPC or internally; if internal, remove the ipc_throttle.py entry too.

---

## Scope Coverage Notes

- `docs/` excluded from deep verification intentionally (IPC_API_REFERENCE.md documents many handlers; doc presence alone does not constitute a live caller)
- `.command` scripts and shell scripts scanned — none reference these 8 handlers
- GitHub Actions CI files (`.github/`) not scanned (not relevant to IPC callers)
- Previous W65 audit removed 19 handlers (batch 1); subsequent batches brought active count from 306 → 296; this audit finds **8 new candidates** at the current 289-handler baseline.
