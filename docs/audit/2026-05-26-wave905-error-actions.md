# Wave 905 — Audit: `backend/error_actions.py` ACTION_HANDLERS

**Date:** 2026-05-26
**Auditor:** Claude Sonnet 4.6 (W905)
**Scope:** `KrabEar/backend/error_actions.py`, cross-referenced against `backend/error_codes.py`, Swift `ErrorActionHandler.swift`, and existing test files.

---

## Summary

12 handlers registered in `ACTION_HANDLERS` (W740 baseline stated 8 — count grew with Wave 50, 60 additions). All 12 handlers are live and referenced; the handler ↔ error-code bidirectional invariant passes in CI. **2 bugs found**, **2 stubs confirmed**, **1 stale comment** identified.

---

## Findings

### F1 — `swift_focus_lm_studio_api_key` side_effect is never consumed by Swift (BUG)

**Severity:** medium  
**Handler:** `_open_lm_studio_settings`  
**Side_effect emitted:** `"swift_focus_lm_studio_api_key"`

`ErrorActionHandler.swift` (`handleSideEffect`) only handles two `swift_*` cases:
```swift
case "swift_focus_hf_token": ...
case "swift_focus_hotkey_tab": ...
default:
    logger.debug("side_effect '\(sideEffect)' — no Swift handler")
```

`open_lm_studio_settings` returns `side_effect = "swift_focus_lm_studio_api_key"` which falls to `default` and is silently ignored. The docstring in `error_actions.py` (line 91) says:
> "The Swift agent picks up side_effect='swift_focus_lm_studio_api_key' and highlights the LM Studio API Key field in Settings tab."

This is **not implemented**. The `open -a "LM Studio"` subprocess call runs, but the Settings panel focus never fires. Four actionable error codes (`rewriter.unauthorized`, `rewriter.warmup_timeout`, `rewriter.lm_studio_500`, `rewriter.model_unloaded`) rely on this action.

**Fix:** Add `case "swift_focus_lm_studio_api_key"` to `handleSideEffect` in `ErrorActionHandler.swift` and post a `focusLMStudioAPIKey` notification. Settings panel must observe and scroll/highlight the LM Studio API Key field.

---

### F2 — `disk.critical` and `startup.stt_model_cache_miss` pushed but missing from `ERROR_REGISTRY` (BUG)

**Severity:** medium

Both codes are actively pushed to the error bus in production code but have no `ERROR_REGISTRY` entry:

| Code | Pushed from |
|---|---|
| `disk.critical` | `backend/disk_monitor.py:_push_disk_critical_error()` (line 322) |
| `startup.stt_model_cache_miss` | `backend/startup_diagnostics.py:_push_stt_model_cache_miss()` (line 599) |

Both push sites use `ERROR_REGISTRY.get("disk.critical", {})` / `ERROR_REGISTRY.get("startup.stt_model_cache_miss", {})` — falling back to hardcoded strings when the key is absent. This means:
- `actionable` defaults to `False` (empty dict falsy) — no toast button even if one were desired
- `dedupe_seconds` is not used (each push is independent)
- The invariant test `test_error_code_key_format` in `test_error_codes_actions_invariant.py` does NOT catch missing push-side codes — it only validates codes already in the registry

Memory notes confirm Wave 82 / Wave 490 added these push sites. The `disk.critical` entry was listed as a HIGH candidate in the Wave 82 audit (`docs/audit/2026-05-27-wave715-sentry-release-stale-process.md` era memory entry) but never landed in `error_codes.py`.

**Fix:** Add `disk.critical` and `startup.stt_model_cache_miss` entries to `ERROR_REGISTRY`. Suggested:
```python
"disk.critical": {
    "user_msg_ru": "🔴 КРИТИЧНО: меньше 1 GB свободного места — немедленно освободите диск",
    "actionable": True,
    "action_id": "open_logs",
    "action_label": "Открыть папку данных",
    "severity": "critical",
    "dedupe_seconds": 60,
},
"startup.stt_model_cache_miss": {
    "user_msg_ru": "STT модель не найдена в кеше — первый запуск займёт больше времени",
    "actionable": False,
    "action_id": None,
    "action_label": "",
    "severity": "warn",
    "dedupe_seconds": 3600,
},
```

---

### F3 — Stale dispatch table comment says "9 entries" (minor)

**File:** `error_actions.py`, line 107  
**Content:** `# Dispatch table (9 entries)`  
**Actual count:** 12

The comment was not updated when Wave 50 added `open_pyannote_hf_page` + `open_terminal_make_release`, and Wave 60 added `open_logs`. Low risk — the comment is purely informational — but causes confusion when auditing.

**Fix:** Update comment to `# Dispatch table (12 entries)`.

---

## Stubs Confirmed (not bugs, by design)

### S1 — `_kill_lm_studio_via_telegram` — permanently feature-disabled

Returns `{"executed": False, "reason": "feature_disabled", "side_effect": None}`. The comment says "Real Telegram bridge integration pending separate spec" (Phase B.1). Only `mlx.oom` references it. The test `test_kill_lm_studio_via_telegram_feature_disabled` guards this behaviour. **No action needed unless Phase B.1 is revived.**

### S2 — `_retry_history_save` — `store.retry_pending_writes()` not implemented in `StateStore`

The handler comment says "method to be added in B.2". `StateStore` (691+ lines) has no `retry_pending_writes` method. Calling this action with a real `StateStore` instance raises `AttributeError`, caught by `handle_action`'s outer except, returning `executed=False, reason="handler_raised: ..."`. The test only passes because it uses `MagicMock()`. **No action needed until Phase B.2.**

---

## Handler Count vs W740 Baseline

| Wave | Count | Notes |
|---|---|---|
| W740 baseline | 8 | CLAUDE.md states "8 action handlers" |
| Current (W905) | 12 | +`open_pyannote_hf_page`, `open_terminal_make_release` (Wave 50), `open_lm_studio_settings`, `switch_to_stable_rewriter` (added post-W740) |

CLAUDE.md `backend/error_actions.py` bullet still reads "8 action handlers". Should be updated to 12 when CLAUDE.md is next revised.

---

## Coverage

Test coverage is thorough across 3 test files (59 test methods total):

| File | Tests | Covers |
|---|---|---|
| `test_error_actions.py` | 8 | core dispatch, cross-reference invariant |
| `test_error_actions_extras.py` | 27 | all 12 handlers individually + concurrency |
| `test_error_codes_actions_invariant.py` | 16 | bidirectional invariant, schema, format |

All 51 tests pass. The bidirectional invariant (every action_id in ERROR_REGISTRY has a handler; no orphan handlers) is machine-checked in CI and currently passes because `disk.critical` / `startup.stt_model_cache_miss` use `ERROR_REGISTRY.get(…, {})` fallback rather than referencing a real registry entry.

---

## Side Effect Protocol Summary

All 12 handlers emit one of: `settings_updated`, `profile_switched`, `history_retried`, `swift_focus_hf_token`, `swift_focus_hotkey_tab`, `swift_focus_lm_studio_api_key` (unhandled in Swift — see F1), `opened:<url>`, `opened_finder_at:<path>`, `opened_terminal_at:<path>`, or `None`.

Swift `handleSideEffect` only acts on `swift_focus_hf_token` and `swift_focus_hotkey_tab`. All other side_effects (including `swift_focus_lm_studio_api_key`) fall to `default: logger.debug`.

---

## Action Items

| ID | Severity | File | Action |
|---|---|---|---|
| F1 | medium | `ErrorActionHandler.swift` | Add `case "swift_focus_lm_studio_api_key"` to `handleSideEffect` |
| F2a | medium | `error_codes.py` | Add `disk.critical` entry |
| F2b | medium | `error_codes.py` | Add `startup.stt_model_cache_miss` entry |
| F3 | minor | `error_actions.py:107` | Update comment `(9 entries)` → `(12 entries)` |
| CLAUDE.md | minor | `CLAUDE.md` | Update "8 action handlers" → "12" |
