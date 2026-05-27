# Audit W1168 — SettingsService (`backend/settings_service.py`)

**Date:** 2026-05-26  
**Branch:** `audit/settings-service-W1168`  
**Scope:** `KrabEar/backend/settings_service.py` (596 lines), `settings_backup.py` (216 lines), `settings_validator.py` (305 lines), `state_store.py` (save/load paths), `service.py` (delegation + hook registration).  
**Context:** post-W897 sensitive-field unification audit. 6 findings (1 CRIT, 3 HIGH, 1 MED, 1 LOW).

---

## F1 — CRIT: `_SENSITIVE_FIELDS` in `settings_service.py` diverged from `settings_backup._SENSITIVE` after W897

**File:** `KrabEar/backend/settings_service.py:386–391`

`SettingsService._SENSITIVE_FIELDS` contains only 4 fields:
```python
frozenset({"voice_gateway_api_key", "hf_token", "rest_api_key", "lm_studio_api_key"})
```

`settings_backup._SENSITIVE` (updated in W58 follow-up) contains 9 fields — 5 more:
```
telnyx_api_key, twilio_account_sid, twilio_auth_token, sentry_dsn, stt_gigaam_hf_token
```

These 5 missing fields are **exported in plaintext** via:
- `handle_export_settings` → writes JSON file to disk (`~/krabear_settings_<ts>.json`)
- `handle_import_settings` → skips keys in `_SENSITIVE_FIELDS` — but if the user re-imports the exported file, these 5 fields flow through as regular keys and get persisted

`settings_backup._SENSITIVE` was updated (W58 follow-up comment in the file) specifically because "users can also override via IPC `set_settings` which persists to settings.json". The same logic applies to export. W897 apparently created `_SENSITIVE_FIELDS` on `SettingsService` without syncing it with `settings_backup._SENSITIVE`.

**Fix:** Replace the hardcoded `_SENSITIVE_FIELDS` frozenset with a reference to `settings_backup._SENSITIVE` (or extract a shared constant from a `ipc_constants.py` or `models.py`).

---

## F2 — HIGH: `handle_import_settings` persists settings even when validation returns hard errors

**File:** `KrabEar/backend/settings_service.py:456–467`

```python
vr = self._validator.validate(merged)
if not vr.valid:
    errors.extend(vr.errors)      # ← error collected
if vr.warnings:
    ...
    errors.extend(vr.warnings)
merged = vr.fixed

imported = len(incoming) - skipped
self.store.save_settings(merged)  # ← ALWAYS called, even if valid=False
```

`SettingsValidator.validate()` only returns `valid=False` for hard errors (currently only `voice_gateway_url` pointing to a non-localhost, non-HTTPS host). When this condition is triggered, the offending URL is **not** replaced by `vr.fixed` (the validator appends to `errors` but leaves the URL unchanged in `fixed`). The import saves the invalid URL and the caller only sees an error in the returned `errors` list — it does not raise.

By contrast, `handle_set_settings` **raises** `ValueError` when `not vr.valid` (line 277).

**Fix:** Add `if not vr.valid: raise ValueError(...)` before `self.store.save_settings(merged)` in `handle_import_settings`, matching the behaviour of `handle_set_settings`.

---

## F3 — HIGH: `handle_restore_settings_backup` bypasses validation, after-save hooks, and hot-reload

**File:** `KrabEar/backend/settings_service.py:517–535`

```python
restored = self._backup.restore_backup(backup_id)
self.store.save_settings(restored)   # direct write — no validation
self.invalidate_cache()
```

Three gaps:

1. **No `SettingsValidator.validate()`**: a backup file with a corrupted `voice_gateway_url` (or other hard-error field) is accepted and written to disk without error.

2. **No `after_save_hooks`**: `BackendService` registers a hook in `service.py:213–217` that propagates `lm_studio_api_key` changes to the live `LLMRewriter`. Restoring a backup that changes the API key silently leaves `LLMRewriter` running with the old key until process restart.

3. **No `reload_settings_from_json()`**: Pydantic `Settings` singleton (used by `engine.py` for feature flags like `STT_GIGAAM_ENABLED`) is not hot-reloaded after restore.

Additionally, the handler does not create a backup of the **current** settings before overwriting them. A mistaken restore is irreversible unless the user previously created a manual backup.

**Fix:** Run `self._validator.validate(restored)` and raise on hard errors; call `reload_settings_from_json()` and `self._after_save_hooks`; optionally save a `"before_restore"` auto-backup first.

---

## F4 — HIGH: `privacy_mode_enabled` is not normalized by `handle_set_settings` or `SettingsValidator`

**Files:** `settings_service.py:179–191`, `settings_validator.py:54–75`, `core/config.py:987`

`privacy_mode_enabled` is present in `DEFAULT_SETTINGS` (default `False`) and is checked by `translation_service.py` and `observability.py` with plain truthiness tests:

```python
# translation_service.py:96, 201
if settings.get("privacy_mode_enabled"):
    ...
```

Neither `handle_set_settings` (which explicitly `bool()`-coerces ~14 other bool fields, lines 179–191) nor `SettingsValidator._BOOL_FIELDS` includes `privacy_mode_enabled`.

**Impact:** A client sending `{"privacy_mode_enabled": "false"}` (string) stores the string `"false"` to disk. On the next read `settings.get("privacy_mode_enabled")` returns `"false"` — a non-empty string — which is truthy in Python. Privacy mode is then permanently stuck ON despite the user's intent to disable it.

**Fix:** Add `privacy_mode_enabled` to `SettingsService.handle_set_settings` bool coercion block (or add it to `SettingsValidator._BOOL_FIELDS`).

---

## F5 — MED: TTL cache has no lock; concurrent IPC threads can produce a lost-update

**File:** `KrabEar/backend/settings_service.py:89–102`, `KrabEar/backend/service.py:3655–3659`

The IPC server runs **thread-per-connection** (`service.py:3655`). `SettingsService._cache` and `_cache_ts` are plain instance attributes with no mutex.

Classic lost-update pattern with two simultaneous `set_settings` callers:

1. Thread A: `old = cached_settings()` → snapshot S₀
2. Thread B: `old = cached_settings()` → snapshot S₀ (same, within TTL)
3. Thread A: merges S₀ + paramsA → SA; `store.save_settings(SA)` (fcntl lock acquired/released)
4. Thread B: merges S₀ + paramsB → SB; `store.save_settings(SB)` (fcntl lock acquired/released)

Thread A's changes are silently lost. `store.save_settings` serialises disk writes (fcntl) but does not prevent the read-then-write race at the service level.

Additionally, `_cache` is read at line 92 (`if self._cache is not None`) and written at line 100 without any lock, which can cause `_cache_ts` to be written after `_cache` is already visible to another thread (torn write on non-atomic Python attribute assignment — low risk in CPython due to GIL, but semantically incorrect).

**Fix:** Add a `threading.Lock` protecting `_cache`/`_cache_ts` reads and writes; consider using a read-modify-write lock for `handle_set_settings` to linearise concurrent writers.

---

## F6 — LOW: `handle_apply_profile_preset` does not create a pre-apply backup

**File:** `KrabEar/backend/settings_service.py:312–344`

`handle_set_settings` creates a `"before_set"` backup (line 119) before every write.  
`handle_restore_settings_backup` does not (gap noted in F3).  
`handle_apply_profile_preset` also does not — there is no backup created before the preset overwrites the current settings.

Applying a preset (e.g. `call_recording`) irreversibly changes `quality_profile`, `realtime_preview_enabled`, and `auto_paste` without any automatic rollback path.

**Fix:** Add `self._backup.create_backup(self.cached_settings(), reason="before_preset")` at the start of `handle_apply_profile_preset`, matching the pattern in `handle_set_settings`.

---

## Test coverage gaps

| Scenario | Covered |
|---|---|
| `after_save_hooks` fired by restore / import / apply_profile_preset | No |
| `handle_restore_settings_backup` runs validator | No |
| `handle_import_settings` raises on `valid=False` | No |
| `privacy_mode_enabled` string coercion | No |
| Concurrent `cached_settings` + `handle_set_settings` (lost-update) | No |
| Backup created before `apply_profile_preset` | No |

The existing test suite (`test_settings_service.py`) covers TTL expiry, cache invalidation, enum normalisation, coerce helpers, and event emission well. The gaps are all in the write-path correctness and security checks listed above.

---

## Summary

| # | Severity | File | Description |
|---|---|---|---|
| F1 | CRIT | `settings_service.py:386` | `_SENSITIVE_FIELDS` missing 5 keys — secrets leak via export |
| F2 | HIGH | `settings_service.py:457–467` | `import_settings` saves despite hard validation errors |
| F3 | HIGH | `settings_service.py:517–535` | `restore_backup` bypasses validation, hooks, hot-reload |
| F4 | HIGH | `settings_service.py:179–191` | `privacy_mode_enabled` not bool-coerced → string "false" = truthy |
| F5 | MED | `settings_service.py:89–102` | Cache read-modify-write not locked; concurrent IPC lost-update |
| F6 | LOW | `settings_service.py:312–344` | `apply_profile_preset` has no pre-apply backup |
