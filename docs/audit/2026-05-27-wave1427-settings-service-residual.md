# Audit: SettingsService residual — W1427

**File:** `KrabEar/backend/settings_service.py`
**Base commit:** `1032d17f` (origin/codex/krab-ear-v2, 2026-05-27)
**Prior waves audited:** W1168, W1173, W1174, W1178, W1308, W1341

---

## Merge-state verification (6 prior fixes)

| Wave | PR | Description | State |
|------|----|-------------|-------|
| W1168 | docs | Initial audit — 6 findings documented | DOCS ONLY |
| W1173 | #1165 | Unified `SENSITIVE_FIELDS` via import from `settings_backup` | **MERGED** |
| W1174 | #1083 | `privacy_mode_enabled` bool-coerce + validator | **MERGED** |
| W1178 | — | `import_settings` raise on `valid=False` + restore validation + pre-restore backup | **NOT MERGED** |
| W1308 | #1210 | `_fire_after_save_hooks` on all 5 save paths | **MERGED** |
| W1341 | — | `reload_settings_from_json` on all 5 save paths (introduces `_reload_and_fire_hooks`) | **NOT MERGED** |

Evidence:
- W1173 merged: line 21 `from backend.settings_backup import SENSITIVE_FIELDS as _SETTINGS_SENSITIVE_FIELDS`; line 450 `_SENSITIVE_FIELDS: frozenset[str] = _SETTINGS_SENSITIVE_FIELDS`.
- W1174 merged: `privacy_mode_enabled = self._coerce_bool(...)` at line 287–289.
- W1308 merged: `_fire_after_save_hooks` method at line 86; called from all 5 save paths (lines 363, 384, 444, 529, 621).
- W1178 not merged: `handle_import_settings` still saves on `vr.valid=False` (line 526 extends errors but never raises); no pre-import backup call; `handle_restore_settings_backup` has no validation gate.
- W1341 not merged: `reload_settings_from_json` called only in `handle_set_settings` (lines 356–361); absent from `handle_apply_profile_preset`, `handle_import_settings`, `handle_set_notification_preferences`, `handle_restore_settings_backup`.

---

## New findings (5, cap reached)

### F1 — HIGH: `handle_import_settings` saves settings when `SettingsValidator.valid=False`

**Location:** `settings_service.py:516–527`

```python
vr = self._validator.validate(merged)
if not vr.valid:
    errors.extend(vr.errors)   # ← appends to errors list
# ... no raise ...
self.store.save_settings(merged)   # ← saves invalid data
```

W1168 F2 documented this bug. W1178 (not merged) adds `raise ValueError(...)` when `not vr.valid`. Currently an import file containing an invalid `voice_gateway_url` (e.g. `"http://evil.local"`) is persisted to `settings.json` — the `errors` key in the response is the only signal that anything went wrong, but the data is written regardless. Effect: persisting an invalid URL can break the Voice Gateway connection silently; the IPC caller sees `errors=[...]` but the bad settings are already saved.

**Fix:** Mirror `handle_set_settings` line 300–301: `raise ValueError(...)` when `not vr.valid`, before calling `store.save_settings`.

---

### F2 — HIGH: `handle_restore_settings_backup` has no backup validation gate

**Location:** `settings_service.py:598–621`

```python
restored = self._backup.restore_backup(backup_id)
# ... credential preservation ...
self.store.save_settings(restored)   # ← no validator.validate(restored) call
```

W1168 F3 documented that restore bypasses the validator. W1178 (not merged) adds: pre-restore snapshot backup, `SettingsValidator.validate(restored)`, rollback on `valid=False`, and `reload_settings_from_json()` call. Currently a tampered or corrupted backup file (e.g. one written before settings schema v2.0 migration, containing an invalid `network_mode`) is written verbatim to `settings.json` with no schema check. This can cause STT/translation features to silently revert to defaults on next load because the validator's `cached_settings()` auto-fixes on read but the file on disk stays corrupt.

**Fix:** Add `vr = self._validator.validate(restored)` before `save_settings`; raise with rollback on `vr.valid=False`.

---

### F3 — HIGH: `reload_settings_from_json` called only from `handle_set_settings` — pydantic singleton stale after the other 4 save paths

**Location:** `settings_service.py:352–361` (only caller)

`core.config.settings` (the pydantic singleton) is refreshed from `settings.json` only when `handle_set_settings` runs. The other four save paths — `handle_apply_profile_preset`, `handle_import_settings`, `handle_set_notification_preferences`, `handle_restore_settings_backup` — write to `settings.json` but never call `reload_settings_from_json`. As a result:

- Applying the `call_recording` preset (`quality_profile: "max"`) does not update `settings.MAX_BALANCED_MODEL` in memory; `engine.py` continues using the pre-preset model.
- Importing a settings file that changes `STT_GIGAAM_ENABLED` to `true` does not activate GigaAM on the next recording until a full restart.
- Restoring a backup does not propagate `sentry_dsn` to the Sentry SDK (already in-process).

W1341 (not merged) introduces `_reload_and_fire_hooks()` that calls `reload_settings_from_json` first, then fires hooks, and wires all 5 paths through it.

**Fix:** Extract `_reload_and_fire_hooks(old, new)` that calls `reload_settings_from_json()` then `_fire_after_save_hooks(old, new)`; replace bare `_fire_after_save_hooks` calls in all 4 remaining paths.

---

### F4 — MED: Read-modify-write TOCTOU race in all 5 save paths — no `_rw_lock`

**Location:** `settings_service.py:131–138`, `378–382`, `506–527`, `420–442`, `597–621`

The IPC server uses thread-per-connection (`ipc_server.py:81`). Each save path performs an unprotected read-modify-write sequence:

```python
old = self.cached_settings()    # read
settings = dict(old)
settings.update(params)         # modify
self.store.save_settings(settings)  # write (file-lock only)
```

`state_store.save_settings` acquires a file lock (protecting the file write), but the in-memory `old = cached_settings() → merge → write` span is not atomic. If two IPC threads execute concurrently:

- Thread A reads `{quality_profile: "balanced"}`, updates to `{quality_profile: "max"}`.
- Thread B reads `{quality_profile: "balanced"}` (stale TTL cache), updates to `{translation_mode: "auto"}`.
- Thread B writes `{quality_profile: "balanced", translation_mode: "auto"}` — drops Thread A's change.

W1168 F5 (MED) documented this as "TTL cache unprotected; thread-per-connection IPC enables lost-update race" but no fix was proposed or merged. In practice concurrent `set_settings` calls are rare in the Swift client (single-user app), but `apply_profile_preset` can be triggered from both HistoryPanel background refresh and user UI tap simultaneously.

**Fix:** Add `self._rw_lock = threading.Lock()` in `__init__`; wrap all 5 save paths with `with self._rw_lock:` around the read-modify-write block.

---

### F5 — LOW: `handle_import_settings` lacks pre-import auto-backup

**Location:** `settings_service.py:477–543`

`handle_set_settings` creates an auto-backup before applying changes (line 133: `create_backup(old_settings, reason="before_set")`). `handle_restore_settings_backup` was similarly updated by W1345 to detect credential drops. However `handle_import_settings` takes no pre-import snapshot. If the import overwrites valid settings with partially-valid ones (the current bug: saves even when `valid=False`), there is no auto-recovery path. The user can call `create_manual_settings_backup` explicitly, but this requires knowing to do so before import.

W1178 (not merged) adds `create_backup(old_settings, reason="before_import")` before the save call.

**Fix:** Add `self._backup.create_backup(old_settings, reason="before_import")` inside `handle_import_settings` before `store.save_settings`, mirroring `handle_set_settings`.

---

## Test coverage gaps (post-fix)

| Gap | File to add |
|-----|-------------|
| `import_settings` raises on `vr.valid=False`; no save happens | `test_import_restore_validation_W1178.py` (pending W1178) |
| `restore_settings_backup` validates + rolls back on corrupt backup | same |
| `reload_settings_from_json` called from all 5 paths | `test_settings_reload_hooks_W1341.py` (pending W1341) |
| Concurrent `set_settings` / `apply_preset` interleave does not lose updates | new — no test currently exists |

No new regression tests exist for the F3 (reload) gap in the current test suite — `test_settings_service_hooks_W1308.py` patches `reload_settings_from_json` in its 3 test cases but only to suppress the import side-effect; it does not assert that the function is or is not called from preset/import/restore paths.

---

## Summary

| Finding | Severity | Status | Fix wave |
|---------|----------|--------|----------|
| F1: import_settings saves invalid data | HIGH | Open (W1178 not merged) | W1178 |
| F2: restore_backup has no validation gate | HIGH | Open (W1178 not merged) | W1178 |
| F3: reload_settings_from_json missing from 4/5 paths | HIGH | Open (W1341 not merged) | W1341 |
| F4: TOCTOU race across all 5 save paths | MED | Open (no fix merged) | new |
| F5: import_settings lacks pre-import backup | LOW | Open (W1178 not merged) | W1178 |
