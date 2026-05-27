# Audit: SettingsService third-pass — W1518

**File:** `KrabEar/backend/settings_service.py`
**Base branch:** `codex/krab-ear-v2` (HEAD `f6bb585e` in worktree at audit time)
**Current file HEAD commit:** `caa105ac` (wave941 — settings_backup path-traversal guard)
**Scope:** third-pass post-fix verification of W1437/W1454/W1457 and W1434+W1435+W1436+W1448 (7 prior fix commits)

---

## Merge-state verification (7 fix commits)

All 7 fix commits ARE ancestors of the current branch HEAD (`git merge-base --is-ancestor` returns true for all).
However, commit `caa105ac` (wave941, dated 2026-05-26) is the **most recent commit to touch this file** and its diff
partially overwrites the content introduced by W1308/W1341/W1434/W1435/W1436/W1437/W1454/W1457.

| Wave | Commit | Description | Ancestor? | Effect live? |
|------|--------|-------------|-----------|-------------|
| W1308 | `65402fb6` | `_fire_after_save_hooks` on all 5 save paths | YES | **REGRESSED** — helper removed by caa105ac |
| W1341 | `b974f3cb` | `_reload_and_fire_hooks` (reload + fire) on all 5 paths | YES | **REGRESSED** — removed by caa105ac |
| W1434 | `9f1d9ac5` | `import_settings` raises `ValueError` when `vr.valid=False` | YES | **REGRESSED** — raise removed by caa105ac |
| W1435 | `979fa74e` | `restore_settings_backup` migrate+validate before save | YES | **REGRESSED** — migration+validation block removed by caa105ac |
| W1436 | `00699e3b` | `reload_settings_from_json` in all 5 save paths | YES | **REGRESSED** — removed from 4/5 paths by caa105ac |
| W1437 | `d557dc9b` | `threading.RLock` around all 5 save paths | YES | **REGRESSED** — `_save_lock` removed by caa105ac |
| W1454 | `32a44ce7` | `handle_restore_settings_backup` raise `ValueError` not dict | YES | **REGRESSED** — entire validation block removed by caa105ac |
| W1457 | `522f13ec` | `_maybe_migrate` invalidates cache after schema write-back | YES | **REGRESSED** — `_maybe_migrate` removed entirely by caa105ac |

**Root cause:** `caa105ac` (wave941, PR for settings_backup path-traversal + SENSITIVE_FIELDS rename) was
squashed/rebased onto the branch AFTER all these fix commits and its content snapshot pre-dates those fixes.
The commit is the final state of the file on disk and overwrote ~200 lines of accumulated fixes.

**Evidence (direct):**
- `threading` is NOT imported — no `_save_lock`.
- `_fire_after_save_hooks` method does NOT exist.
- `_reload_and_fire_hooks` method does NOT exist.
- `_maybe_migrate` method does NOT exist; `cached_settings()` line 94 calls `self._validator.validate(raw)` directly without any schema migration step.
- `handle_import_settings` lines 452-463: appends to `errors` list when `not vr.valid` but does NOT raise — calls `store.save_settings(merged)` regardless.
- `handle_restore_settings_backup` lines 513-531: calls `restore_backup` + `save_settings` with no migration, no validation, no credential preservation.
- `reload_settings_from_json` appears ONLY at lines 298-303 (inside `handle_set_settings`), absent from the other 4 save paths.

**Test evidence (runtime confirmation):**

```
$ PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_settings_service_hooks_W1308.py \
    KrabEar/tests/test_settings_reload_hooks_W1341.py -q --tb=no
31 failed, 23 passed
```

The 31 failures are in `test_settings_service_hooks_W1308.py` (18 failures — after_save_hook not called from
4/5 paths) and `test_settings_reload_hooks_W1341.py` (13 failures — `reload_settings_from_json` not called
from 4/5 paths).

---

## New findings (5, cap reached)

### F1 — CRIT: wave941 (caa105ac) regresses W1308/W1341/W1434/W1435/W1436/W1437/W1454/W1457 in one shot — 31 tests failing

**Location:** `KrabEar/backend/settings_service.py` (entire file), commit `caa105ac`

Commit `caa105ac` is a squashed rebase of wave941 (settings_backup path-traversal + SENSITIVE_FIELDS rename).
Its content snapshot predates the fix waves listed above. When rebased as the newest commit touching this file
it silently discarded all accumulated fixes:

1. **After-save hooks** (`_fire_after_save_hooks`, W1308): `apply_profile_preset`, `import_settings`,
   `set_notification_preferences`, and `restore_settings_backup` no longer fire registered hooks. The
   `lm_studio_api_key` hot-propagation hook registered by `BackendService` is therefore NOT called when
   settings are changed through these 4 paths — the LLMRewriter stale-key bug that W1308 fixed is live again.

2. **pydantic hot-reload** (`reload_settings_from_json`, W1341/W1436): only `handle_set_settings` calls
   `reload_settings_from_json`. The other 4 paths write to `settings.json` but the pydantic singleton
   (`core.config.settings`) stays stale until the next `set_settings` call or backend restart.
   Consequence: applying the `call_recording` preset does not update `settings.MODEL_BALANCED` in-process;
   importing a settings file that sets `STT_GIGAAM_ENABLED=true` does not activate GigaAM.

3. **Schema migration** (`_maybe_migrate`, W1457): `cached_settings()` no longer calls `_maybe_migrate`.
   Old-schema settings.json files (schema_version="1.0") are loaded and run through `_validator.validate`
   without migration — the validator's `add_default` migrations fire warnings but do not rename the
   `history_limit` → `history_policy` key. Any deployment upgrading from settings schema 1.0 loses the
   rename silently.

4. **Import validation raise** (W1434): `handle_import_settings` extends `errors` but does not raise when
   `vr.valid=False`. An import file with `voice_gateway_url: "http://evil.local"` is persisted to
   `settings.json` with only an `errors` key in the response.

5. **Restore backup validation** (W1435/W1454): `handle_restore_settings_backup` calls
   `_backup.restore_backup(backup_id)` then immediately `store.save_settings(restored)` with no schema
   migration, no validation, no credential preservation. A corrupt or old-schema backup is written verbatim.

**Fix:** Re-apply all regressed fixes onto the current file. The cleanest path is to produce a single
consolidation commit that restores `_fire_after_save_hooks`, `_reload_and_fire_hooks` (or equivalent),
`_maybe_migrate`, the `raise ValueError` in `import_settings`, and the migration+validation gate in
`restore_settings_backup`. The existing tests (31 failing) serve as regression gates once the fixes land.

---

### F2 — HIGH: `handle_import_settings` still saves invalid data — W1434 regression confirmed by test run

**Location:** `settings_service.py:452-463`

```python
vr = self._validator.validate(merged)
if not vr.valid:
    errors.extend(vr.errors)   # appends errors
# ... no raise ...
merged = vr.fixed
imported = len(incoming) - skipped
self.store.save_settings(merged)  # saves regardless of validity
```

When `voice_gateway_url` (or any other hard-error field) in the import file is invalid, `vr.valid=False`,
errors are added to the list, but `store.save_settings(merged)` is called. `vr.fixed` may still contain an
empty-string or default value for the invalid field (the validator coerces to default on hard errors), so the
persisted file may differ from the import in ways the caller does not expect. The IPC response has non-empty
`errors` but `ok=true` at the outer envelope level.

**Fix:** Insert `raise ValueError(f"Настройки содержат ошибки: {'; '.join(vr.errors)}")` immediately after
`errors.extend(vr.errors)` (matching the pattern in `handle_set_settings` lines 277).

---

### F3 — HIGH: `_after_save_hooks` NOT fired from 4/5 save paths — LLMRewriter api_key hot-propagation broken for preset/import/notification/restore paths

**Location:** `settings_service.py:305-309` (only in `handle_set_settings`)

`register_after_save_hook` is called by `BackendService` to propagate `lm_studio_api_key` to the live
`LLMRewriter` instance (W1239 pattern). The current file's hook loop exists only in `handle_set_settings`.
If a user changes `lm_studio_api_key` via `apply_profile_preset` (e.g., a preset that includes the key) or
`import_settings`, the `LLMRewriter.set_api_key()` hook is not invoked. The rewriter continues using the
old key until either `set_settings` is called or the backend restarts.

The same gap applies to the Sentry disable-on-privacy path (W1199, line ~373 in the W1454 state): the
privacy mode Sentry flush is only wired inside `handle_set_settings`. If `privacy_mode_enabled` is set
through `import_settings` or `restore_settings_backup`, Sentry is NOT flushed and remains active.

**Fix:** Restore `_fire_after_save_hooks(old_settings, new_settings)` call at the end of each of the 4
remaining save paths. The W1308 test file (`test_settings_service_hooks_W1308.py`) provides 18 regression
tests; all must pass.

---

### F4 — HIGH: `reload_settings_from_json` absent from 4/5 save paths — pydantic singleton stale after preset/import/notification/restore

**Location:** `settings_service.py:298-303` (only in `handle_set_settings`)

The pydantic singleton `core.config.settings` is the source of truth for `_get_model_path()`,
`STT_GIGAAM_ENABLED`, `STT_LANGUAGE_ROUTING_ENABLED`, and other engine-level flags. After any save through
`apply_profile_preset`, `import_settings`, `set_notification_preferences`, or `restore_settings_backup`
the singleton is not refreshed. A quality-profile change via preset takes effect in the UI but the in-process
engine continues using the pre-preset model path until the next `set_settings` call.

The 13 failing tests in `test_settings_reload_hooks_W1341.py` are live regression evidence.

**Fix:** Call `reload_settings_from_json()` (with exception guard) from each of the 4 remaining save paths
before firing after-save hooks. The W1341 test file provides 13 regression tests; all must pass.

---

### F5 — MED: `handle_restore_settings_backup` has no schema migration or validation — corrupt/old-schema backups written verbatim

**Location:** `settings_service.py:513-531`

```python
restored = self._backup.restore_backup(backup_id)
self.store.save_settings(restored)   # no migration, no validate()
self.invalidate_cache()
```

A backup created under settings schema 1.0 (before the `history_limit` → `history_policy` rename) is
restored as-is. The validator's `cached_settings()` call that follows will auto-fix on next read, but the
file on disk contains stale schema. More critically, a backup with `voice_gateway_url: "http://evil.local"`
or an invalid `translation_mode` value bypasses the URL and enum guards that `handle_set_settings` enforces.

Additionally, credential fields (`lm_studio_api_key`, `telnyx_api_key`, etc.) present in the current live
settings but absent from the backup are silently dropped — the W1345 credential-preservation logic was in the
W1435 state but is gone from the current file.

**Fix:** Before `store.save_settings(restored)`:
1. Run `self._validator.validate(restored)` and raise `ValueError` on `not vr.valid`.
2. Re-preserve credential fields from current settings if absent in backup (W1345 pattern).
3. Call `reload_settings_from_json()` and `_fire_after_save_hooks` after save.

---

## Test failure summary

| Test file | Failures | Root cause |
|-----------|---------|------------|
| `test_settings_service_hooks_W1308.py` | 18 | `_fire_after_save_hooks` removed from 4/5 paths (W1308 regressed) |
| `test_settings_reload_hooks_W1341.py` | 13 | `reload_settings_from_json` absent from 4/5 paths (W1341/W1436 regressed) |
| **Total** | **31** | caa105ac (wave941) merge-footgun |

---

## Merge-state summary

| Fix | Wave | Commit | Ancestor? | In-effect? |
|-----|------|--------|-----------|-----------|
| after_save_hooks on 5 paths | W1308 | 65402fb6 | YES | NO — regressed by caa105ac |
| reload_and_fire_hooks on 5 paths | W1341 | b974f3cb | YES | NO — regressed by caa105ac |
| import_settings raise on invalid | W1434 | 9f1d9ac5 | YES | NO — regressed by caa105ac |
| restore_backup migrate+validate | W1435 | 979fa74e | YES | NO — regressed by caa105ac |
| reload in all 5 save paths | W1436 | 00699e3b | YES | NO — regressed by caa105ac |
| RLock around 5 save paths | W1437 | d557dc9b | YES | NO — regressed by caa105ac |
| restore raise ValueError not dict | W1454 | 32a44ce7 | YES | NO — regressed by caa105ac |
| _maybe_migrate invalidate_cache | W1457 | 522f13ec | YES | NO — regressed by caa105ac |
| SENSITIVE_FIELDS import (9 fields) | W1173/W929 | caa105ac | — | YES — introduced by caa105ac |
| path-traversal guard in backup | W941 | caa105ac | — | YES — introduced by caa105ac |
