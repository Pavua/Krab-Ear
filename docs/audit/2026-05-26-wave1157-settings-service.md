# Audit: SettingsService — W1157

**Date:** 2026-05-26
**File:** `KrabEar/backend/settings_service.py`
**Branch:** `audit/settings-service-W1157`
**Scope:** 5s TTL cache, SettingsValidator integration, profile preset atomicity, runtime overrides, sensitive field handling, IPC handler completeness, test coverage.

---

## Summary

5 findings (1 HIGH, 2 MED, 2 LOW). TTL cache and W58 runtime-vs-static pattern are correct. IPC handler wiring is complete (all 10 methods registered in `service.py`). No W929 settings_backup findings reproduced.

---

## F1 — HIGH: `_SENSITIVE_FIELDS` divergence — secrets leak to export files

**File:** `KrabEar/backend/settings_service.py:386-391`

`SettingsService._SENSITIVE_FIELDS` contains only 4 fields:

```python
_SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "voice_gateway_api_key",
    "hf_token",
    "rest_api_key",
    "lm_studio_api_key",
})
```

`settings_backup._SENSITIVE` (the W941 unified set) contains 9 fields — including `telnyx_api_key`, `twilio_account_sid`, `twilio_auth_token`, `sentry_dsn`, `stt_gigaam_hf_token`.

`handle_export_settings` and `handle_import_settings` both filter using `_SENSITIVE_FIELDS`, not the backup module's set. A user calling `export_settings` will have `telnyx_api_key` and `twilio_auth_token` written to the JSON file in cleartext. `handle_import_settings` similarly skips only the 4-field set, so an import of a malicious file could overwrite `sentry_dsn`.

**Fix:** Consolidate to a single source of truth. Either import `_SENSITIVE` from `settings_backup` or define the canonical set in `settings_service.py` and use it in both places. Simplest one-liner fix:

```python
from backend.settings_backup import _SENSITIVE as _SENSITIVE_FIELDS
```

or equivalently expand `_SENSITIVE_FIELDS` to match the backup module's set.

---

## F2 — MED: `handle_restore_settings_backup` bypasses `_after_save_hooks`

**File:** `KrabEar/backend/settings_service.py:531`

```python
restored = self._backup.restore_backup(backup_id)
self.store.save_settings(restored)   # direct — no hooks, no pre-write backup
self.invalidate_cache()
```

`handle_set_settings` fires `_after_save_hooks` after every save (line 305) so live collaborators (LLMRewriter API key, hot-reload of pydantic Settings) stay in sync. `handle_restore_settings_backup` bypasses all hooks. After a restore, `LLMRewriter` still holds the pre-restore `api_key`, `reload_settings_from_json()` is never called, etc.

Also missing: `_backup.create_backup(current, reason="before_restore")` — there is no rollback point if the restore itself is incorrect.

**Fix:** Delegate through `handle_set_settings(restored)` (which already performs backup + validation + hooks + hot-reload + breadcrumb), or manually replicate the post-save sequence.

---

## F3 — MED: `handle_apply_profile_preset` bypasses pre-write backup and `_after_save_hooks`

**File:** `KrabEar/backend/settings_service.py:323-344`

```python
settings = self.cached_settings()
settings.update(preset)
settings["active_preset"] = profile
result = self.store.save_settings(settings)   # no backup, no hooks, no validation
```

Same bypass pattern as F2: no `_backup.create_backup()` before write, no `_after_save_hooks` fired after. Since preset values are constrained to known-good fields (low risk), the missing validation pass (see F4) and the missing hooks are the primary concerns.

**Fix:** Either add `_backup.create_backup(old_settings, reason="before_preset")` + hook firing, or route through `handle_set_settings(preset)` so all side-effects are guaranteed.

---

## F4 — LOW: `handle_apply_profile_preset` skips `SettingsValidator`

**File:** `KrabEar/backend/settings_service.py:323-344`

`handle_set_settings` runs `self._validator.validate(settings)` twice (once on load via `cached_settings()`, once before save at line 275). `handle_apply_profile_preset` runs zero validator passes. If a future preset key is added that fails schema validation, the error is silent and the invalid state is persisted.

**Fix:** Add `vr = self._validator.validate(settings)` before the `store.save_settings()` call, consistent with `handle_set_settings`.

---

## F5 — LOW: No test coverage for `register_after_save_hook` / `_after_save_hooks` firing

**Files:** `KrabEar/tests/test_settings_service.py`, `KrabEar/tests/test_settings_backup.py`

Zero tests across all settings test files exercise:
- `register_after_save_hook(fn)` registration
- Hooks being called with `(old_settings, new_settings)` after `handle_set_settings`
- Hooks NOT being called after `handle_apply_profile_preset` or `handle_restore_settings_backup` (the gap in F2/F3)
- Hook exception isolation (the `except Exception` guard at line 308 is untested)

Given that `BackendService` registers a hook to propagate `api_key` to `LLMRewriter`, a regression in hook firing would be silent.

**Fix:** Add `TestAfterSaveHooks` test class with cases covering registration, invocation with correct args, exception isolation, and verifying that preset/restore bypasses (F2/F3) are either fixed or explicitly documented.

---

## Confirmed-OK

- **5s TTL cache correctness** — `time.monotonic()` used correctly; copy-on-read via `dict(self._cache)` prevents cache mutation; `invalidate_cache()` sets `_cache = None` and `_cache_ts = 0.0`. Tests in `TestCachingBasics` cover hit/miss/invalidate with mock clock.
- **SettingsValidator integration in `handle_set_settings`** — runs validate twice (load + pre-save), promotes `vr.fixed`, raises on hard errors. W928 finding fully addressed.
- **Runtime overrides vs static defaults (W58 lesson)** — `handle_set_settings` calls `reload_settings_from_json()` after every save. `cached_settings()` reads from `store.load_settings()` (runtime file) not `DEFAULT_SETTINGS`. The only `DEFAULT_SETTINGS` usage is for `text_templates` fallback (correct pattern: runtime-first, static as ultimate fallback).
- **IPC handler completeness** — all 10 `SettingsService` methods wired in `service.py` handler table (lines 923–996).
- **`voice_gateway_url` SSRF guard** — localhost/HTTPS check present in `handle_set_settings` (line 193–194).
