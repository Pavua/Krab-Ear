# Audit: SettingsService post-fix residual — W1448

**File:** `KrabEar/backend/settings_service.py`
**Base branch:** `codex/krab-ear-v2` (HEAD `4eb8356f` in worktree at audit time)
**Scope:** post-fix audit of W1434 / W1435 / W1436 / W1437

---

## Merge-state verification (4 fix branches)

| Wave | Branch | Description | PR | State |
|------|--------|-------------|-----|-------|
| W1434 | `fix-import-settings-raise-W1434` | `import_settings` raises `ValueError` when `vr.valid=False` instead of silently saving (W1427 F1 HIGH) | #1329 | **MERGED** |
| W1435 | `fix-restore-backup-validate-W1435` | `restore_settings_backup` migrates + validates before saving (W1427 F2 HIGH) | #1333 | **MERGED** |
| W1436 | `fix-reload-all-paths-W1436` | `reload_settings_from_json` added to all 5 save paths (W1427 F3 HIGH) | #1331 | **MERGED** |
| W1437 | `fix-settings-save-lock-W1437` | RLock around all 5 save paths (W1427 F4 MED TOCTOU) | #1324 | **OPEN** |

Evidence for merged state:
- W1434 merged: `handle_import_settings` line ~567 raises `ValueError(f"Настройки содержат ошибки: ...")` when `not vr.valid`.
- W1435 merged: `handle_restore_settings_backup` has migration block (W1427 F2 comment) at line ~663 and validation block at line ~681, returning `{"ok": False, ...}` dicts.
- W1436 merged: `reload_settings_from_json` present in `handle_apply_profile_preset` (line ~421), `handle_set_notification_preferences` (~488), `handle_import_settings` (~586), `handle_restore_settings_backup` (~717).
- W1437 not merged: `threading` not imported; `self._save_lock` attribute not present in `__init__`; none of the 5 save paths are wrapped in `with self._save_lock:`.

---

## New findings (5, cap reached)

### F1 — HIGH: `handle_restore_settings_backup` returns `{"ok": False}` dict instead of raising — outer IPC envelope reports `ok=True`

**Location:** `settings_service.py:670–693` (migration failure path), `680–692` (validation failure path)

```python
# migration failure
return {
    "ok": False,
    "error": "Backup validation failed",
    "details": f"Schema migration ...",
}

# validation failure
return {
    "ok": False,
    "error": "Backup validation failed",
    "details": vr.errors,
}
```

`service.py:handle_request` wraps every handler return value unconditionally:

```python
result = handler(params)
return {"id": request_id, "ok": True, "result": result}
```

So when migration or validation fails the caller receives:

```json
{"id": "...", "ok": true, "result": {"ok": false, "error": "Backup validation failed", ...}}
```

The outer `ok=true` causes the Swift client (`HistoryPanelController+Settings.swift`) to treat the restore as successful. The nested `ok: false` is unlikely to be checked because no Swift code currently drills into `result.ok` for the restore path (the same pattern used by every other save path is to raise and let the outer handler convert it to `ok: false`). Contrast with `handle_import_settings` (W1434): it raises `ValueError`, which propagates through `handle_request`'s except-branch and returns `{"ok": false, "error": {...}}` at the outer level, which is correct.

The W1435 fix introduced an inconsistency: migration/validation failures in restore should `raise ValueError(...)` — not return error dicts — so the outer IPC layer handles them uniformly with all other error paths.

**Fix:** Replace both `return {"ok": False, ...}` blocks with `raise ValueError(...)` mirroring `handle_import_settings` and `handle_set_settings`.

---

### F2 — HIGH: `_maybe_migrate` write-back in `cached_settings()` bypasses `invalidate_cache()` — stale TTL cache after auto-migration on first load

**Location:** `settings_service.py:119–152` (`_maybe_migrate`), `102–117` (`cached_settings`)

When `cached_settings()` detects an old schema version, it calls `_maybe_migrate` which may write the migrated dict back to disk via `self.store.save_settings(migrated)` (line ~148). However:

1. `_maybe_migrate` does not call `self.invalidate_cache()` after the write-back.
2. `cached_settings()` immediately stores the pre-write-back `raw` in `self._cache` via `result_v = self._validator.validate(raw)` / `self._cache = result_v.fixed` — this happens in the callers `cached_settings()` method, not before `_maybe_migrate` returns.
3. Actually `cached_settings()` stores `result_v.fixed` based on the return value of `_maybe_migrate`, so the in-memory cache is correct. However, if `_maybe_migrate` triggers a disk write that fails (`save_settings` raises), `_maybe_migrate` swallows the exception (`_log.warning("migration write-back failed: %s", exc)`) and returns the migrated dict. The cache is then stamped as correct while the disk still has the old schema version. On the next process startup the migration runs again — harmless but noisy.

More critically: `_maybe_migrate` writes to disk without holding any lock. Under W1437 (not yet merged), all 5 explicit save paths will be serialised by `_save_lock`, but `_maybe_migrate` — called from `cached_settings()` which is also invoked inside the locked sections — will perform an additional unserialized disk write outside the lock's coverage. When W1437 lands, any concurrent `cached_settings()` call from a non-save-path (e.g. `handle_get_settings`) will race with the locked save paths on the migration write-back. Since W1437 is not merged yet this is a future interaction defect, but the design gap exists now.

**Fix:** In `_maybe_migrate`, after `self.store.save_settings(migrated)` succeeds, call `self.invalidate_cache()`. Also stamp `self._cache_ts = 0.0` before the write so that a concurrent reader does not serve a stale pre-migration snapshot. When W1437 lands, `_maybe_migrate` should either be called before acquiring `_save_lock` (outside save paths) or the write-back should use a separate path that does not re-acquire the lock.

---

### F3 — MED: W1437 (OPEN) — TOCTOU race still unmitigated; W1437 diff removes `privacy_mode_enabled` coerce and three other coerce calls from `_set_settings_locked`

**Location:** `fix-settings-save-lock-W1437` diff of `settings_service.py`

The W1437 branch removes three `_coerce_bool` calls from what was `handle_set_settings`:

```diff
-        settings["privacy_mode_enabled"] = self._coerce_bool(
-            settings.get("privacy_mode_enabled", False), default=False
-        )
-        settings["llm_rewrite_enabled"] = self._coerce_bool(
-            settings.get("llm_rewrite_enabled", False), default=False
-        )
-        settings["auto_save_transcripts"] = self._coerce_bool(
-            settings.get("auto_save_transcripts", False), default=False
-        )
```

These coerce calls are present on `codex/krab-ear-v2` main branch (lines ~313–322). Merging W1437 would silently drop them. The `SettingsValidator._BOOL_FIELDS` dict does include `llm_rewrite_enabled` and `auto_save_transcripts` (so the validator's pass would coerce those), but `privacy_mode_enabled` is **not** in `SettingsValidator._BOOL_FIELDS` — it is coerced only by the explicit `_coerce_bool` call in `handle_set_settings`. If W1437 is merged as-is, sending `privacy_mode_enabled: "true"` (a string from a JSON payload) through `set_settings` would bypass the string→bool coerce, resulting in the string `"true"` being persisted to `settings.json`. The pydantic `reload_settings_from_json` call that follows would fail or silently override it, but the persisted file would contain the string.

Additionally, the W1437 diff also removes `_fire_after_save_hooks` from the helper method and inlines only the hook-loop for `_set_settings_locked` — the other three save paths wrapped in `with self._save_lock:` have no after-save-hooks wiring at all in the W1437 diff, leaving `apply_profile_preset`, `set_notification_preferences`, and `import_settings` without after-save hook notification.

**Fix:** Before merging W1437, restore the three `_coerce_bool` calls and retain `_fire_after_save_hooks` calls in all locked paths. Alternatively re-add `privacy_mode_enabled` to `SettingsValidator._BOOL_FIELDS` before removing the explicit coerce.

---

### F4 — MED: `handle_import_settings` still lacks pre-import auto-backup (W1427 F5 OPEN)

**Location:** `settings_service.py:527–600`

W1427 F5 (LOW) documented that `handle_import_settings` takes no pre-import snapshot. W1434 merged the raise-on-invalid fix but did not add a `create_backup` call. The current code:

```python
# No backup created here
old_settings = self.cached_settings()
merged = dict(old_settings)
...
self.store.save_settings(merged)
```

Contrast with `handle_set_settings` which does:

```python
self._backup.create_backup(old_settings, reason="before_set")
```

If `import_settings` is called with a file that has many valid but undesirable keys (e.g. overrides all quality/translation settings) and the user wants to undo, there is no auto-recovery point. The only recovery is a pre-existing manual backup. Since W1434 now correctly raises on hard validation errors, the W1427 F5 concern is reduced but not eliminated — a valid-but-unwanted import still has no automatic safety net.

**Fix:** Add `self._backup.create_backup(old_settings, reason="before_import")` at the start of `handle_import_settings` (after `old_settings = self.cached_settings()`), matching the `handle_set_settings` pattern.

---

### F5 — LOW: Zero test coverage for W1434/W1435/W1436 fixes — no regression guards against merge-footgun revert

**Location:** `KrabEar/tests/test_settings_service.py`, `KrabEar/tests/test_settings_migration_deep.py`

None of W1434, W1435, or W1436 added tests. Specifically:

- **W1434 regression absent:** No test asserts that `handle_import_settings` raises `ValueError` (instead of returning `errors=[...]`) when `voice_gateway_url` contains an invalid non-localhost, non-HTTPS URL. The existing `test_import_settings_calls_add_breadcrumb` only verifies breadcrumb emission with a valid file.
- **W1435 regression absent:** No test exercises `handle_restore_settings_backup` with a backup file whose `schema_version` is `"1.0"` (triggering migration) or with a backup containing `voice_gateway_url: "http://evil.local"` (triggering validation rejection). `test_settings_migration_deep.py` tests `SettingsValidator.migrate()` directly but never calls `handle_restore_settings_backup`.
- **W1436 regression absent:** No test asserts that `reload_settings_from_json` is called from `handle_apply_profile_preset`, `handle_set_notification_preferences`, `handle_import_settings`, or `handle_restore_settings_backup`. `test_settings_service_hooks_W1308.py` patches the function to suppress side-effects but does not assert it is called.

If a future merge conflict resolves by keeping the pre-fix code (classic footgun scenario documented for W970/W1340/W1416 in translator.py), these regressions will be silent — the test suite will still pass. The pattern that caught those translator.py regressions (AST-level structural tests) should be applied here.

**Fix:** Add `test_settings_service_post_fix_W1448.py` with:
1. `test_import_settings_raises_on_invalid_url` — verifies `ValueError` raised; `store.save_settings` not called.
2. `test_restore_backup_rejects_invalid_backup` — verifies corrupt backup returns `ValueError` (or `ok: False` at result level, once F1 is fixed to raise).
3. `test_reload_called_from_all_5_save_paths` — patches `reload_settings_from_json` and verifies it is called from each of the 5 paths.

---

## Test coverage summary (post W1434/W1435/W1436)

| Fix | Regression test | State |
|-----|----------------|-------|
| W1434: import raises on invalid | None | MISSING |
| W1435: restore migrates+validates | None | MISSING |
| W1436: reload in all 5 paths | None | MISSING |
| W1437: RLock TOCTOU | None (W1437 OPEN) | MISSING |

---

## Summary

| Finding | Severity | Status |
|---------|----------|--------|
| F1: restore returns `{"ok": False}` dict instead of raising — outer envelope lies | HIGH | New post-W1435 |
| F2: `_maybe_migrate` write-back races with future W1437 lock + no `invalidate_cache` after write | HIGH | Pre-existing design gap |
| F3: W1437 (open) diff drops `privacy_mode_enabled` coerce + after-save hooks from 3 paths | MED | New pre-merge defect |
| F4: `import_settings` still lacks pre-import auto-backup (W1427 F5 not fixed by W1434) | MED | W1427 F5 carry-over |
| F5: Zero regression tests for W1434/W1435/W1436 — footgun risk | LOW | New coverage gap |
