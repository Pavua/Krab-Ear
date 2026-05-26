# Wave 820 — DataMigrator Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/data_migrator.py`  
**Related:** `KrabEar/backend/settings_validator.py`

---

## Summary

`DataMigrator` is a standalone class that handles **history data** schema migrations between
versions. It operates on `history.ndjson` (NDJSON flat file). A parallel but separate migration
mechanism exists in `SettingsValidator` for **settings** schema (`settings.json`). The two
systems share the same version labels (`"1.0"` / `"2.0"`) but are completely independent in
code and are not co-ordinated at runtime.

---

## Schema Versions

| Constant | Value | Location |
|---|---|---|
| `LATEST_VERSION` | `"2.0"` | `data_migrator.py:22` |
| `CURRENT_SCHEMA_VERSION` | `"2.0"` | `settings_validator.py:17` |

Both agree that `"2.0"` is current. No `"3.0"` or higher exists anywhere in the codebase.

---

## History Data Migrations (`data_migrator.py`)

### Defined migration paths

| From | To | Handler | Fields added |
|---|---|---|---|
| `"1.0"` | `"2.0"` | `_migrate_v1_to_v2()` | `tags=[]`, `favorite=False`, `annotation=""` |

This is the **only** coded path. The `migrate()` method guards `target_version != "2.0"` at
entry (line 184) and raises `ValueError` for any other target. A catch-all branch (lines
204–212) logs a warning and returns a zero-count result for any unrecognised `current → target`
pair — no data is written.

### Version detection

`_detect_version_from_items()` (line 43) heuristically detects version from the live records:

- Any record missing `tags` or `favorite` → `"1.0"`.
- All records present or history empty → `LATEST_VERSION` (`"2.0"`).

There is **no version field stored in the NDJSON file itself**. Version is re-detected on every
call by scanning active (non-tombstoned) records.

### IPC surface

| Method | IPC key | Description |
|---|---|---|
| `handle_check_migration` | `check_migration` | Returns `migration_needed`, `current_version`, `target_version`, `plan` |
| `handle_run_migration` | `run_migration` | Executes migration, returns counts and `backup_path` |
| `rollback_migration` | _(no IPC wrapper)_ | Python-only; restores files from backup dir |

`rollback_migration` has no IPC handler — it can only be called programmatically (e.g. from
tests or a future recovery flow). This is a gap if rollback needs to be triggered from the Swift
agent.

### Backup strategy

`_create_backup()` copies the following files (if present) into
`<data_dir>/backups/migration_backup_<ts>/` before any migration:

- `history.ndjson`
- `history_tombstones.ndjson`
- `history_status.ndjson`
- `history_tags.ndjson`
- `history_favorites.ndjson`
- `history_annotations.ndjson`
- `settings.json`

A `migration_meta.json` index is written into the backup dir. Backup is always created even when
no migration is needed (version already `"2.0"`).

---

## Settings Migrations (`settings_validator.py`)

### Defined migration paths

| From | To | Operations |
|---|---|---|
| `"1.0"` | `"2.0"` | 1 rename + 15 `add_default` ops (see table below) |

Operations for `("1.0", "2.0")`:

| Op | Key (old → new or key) | Default |
|---|---|---|
| rename | `history_limit` → `history_policy` | — |
| add_default | `overlay_opacity_percent` | `45` |
| add_default | `call_budget_usd` | `2.0` |
| add_default | `call_notify_default` | `True` |
| add_default | `call_auto_summary` | `True` |
| add_default | `llm_rewrite_enabled` | `False` |
| add_default | `auto_save_transcripts` | `False` |
| add_default | `notifications_enabled` | `True` |
| add_default | `notify_on_low_confidence` | `True` |
| add_default | `notify_confidence_threshold` | `0.5` |
| add_default | `notify_on_llm_failure` | `True` |
| add_default | `notify_on_import_complete` | `True` |
| add_default | `notify_sound_enabled` | `True` |
| add_default | `capture_source_mode` | `"mic"` |
| add_default | `ui_last_tab` | `"history"` |
| add_default | `history_focus_mode` | `True` |

`SettingsValidator._build_migration_chain()` (line 266) supports chained hops (e.g.
`1.0 → 1.1 → 2.0`) but only one entry exists in `_MIGRATIONS`, so multi-hop is untested.

---

## Version Gaps

### History data (`DataMigrator`)

| Gap | Severity | Notes |
|---|---|---|
| No `"1.0"` → `"1.1"` | N/A — no such version was ever introduced | Acceptable |
| No `"2.0"` → `"3.0"` | N/A — `"2.0"` is latest | Acceptable |
| Unknown `current` version | **Medium** | Falls through to no-op warning branch. If a future data format introduces `"3.0"` without a migration entry, the migrator silently does nothing. |
| Version not stored in file | **Low** | Re-detected by field scan; correct for binary 1.0/2.0 distinction, but fragile if a v3 format happens to include all v2 fields (false-positive detection). |

### Settings (`SettingsValidator`)

| Gap | Severity | Notes |
|---|---|---|
| No `"1.1"` intermediate | N/A | Only two versions exist |
| Multi-hop chain code exists but untested | **Low** | `_build_migration_chain` has a loop but no test covers a 3-hop chain |

---

## Test Coverage

### `test_data_migrator.py` — 607 lines

| Test class | # tests | Scenarios covered |
|---|---|---|
| `TestDetectVersionFromItems` | 4 | empty, missing tags, full v2, mixed |
| `TestGetSchemaVersion` | 4 | empty dir, v1 data, v2 data, tombstone exclusion |
| `TestCheckMigrationNeeded` | 3 | v1 needs migration, v2 does not, empty dir |
| `TestGetMigrationPlan` | 4 | no-migration, v1 plan fields, backup mention, version numbers |
| `TestMigrateV1ToV2` | 11 | adds fields, counts, idempotent, backup, meta file, MigrationResult dataclass, field preservation, unsupported version raises, partial v2, empty history, from_version matches, schema_version after |
| `TestIpcHandlers` | 6 | check_migration missing dir, return keys, detects v1, run_migration missing dir, return keys, executes, invalid version |
| `TestRollbackMigration` | 3 | restores file, invalid backup raises, return keys |
| `TestReadNdjson` | 3 | reads all lines, skips empty lines, nonexistent file |
| `TestInvalidVersionHandling` | 4 | invalid string, empty string, spaces, unknown version in IPC |
| `TestRollbackOnMigrationFailure` | 3 | restores after failure, nonexistent path, path-to-file raises |
| `TestConcurrentMigration` | 1 | 4 threads, data not corrupted (atomic rename) |
| `TestUnicodeDataPreserved` | 5 | Cyrillic, Spanish, emoji, mixed, backup meta UTF-8 |
| **Total** | **51** | |

### `test_settings_migration_deep.py`

Covers every individual operation in the `("1.0", "2.0")` settings migration table (16 ops
tested individually) plus edge cases: unknown schema version handled, extra custom keys
preserved, `schema_version` field updated after migration.

### `test_migration_scripts.py`

Covers shell-script migration (launchd plist replacement flow), not `DataMigrator` directly.

### `test_stt_adapter_migration.py`

Covers STT adapter config migration (adapter name renames), not `DataMigrator` directly.

---

## Gaps and Recommendations

### Gap 1 — `rollback_migration` has no IPC handler

**Risk:** Low. Recovery from a failed migration requires direct Python call or manual file
restoration. If a user-facing "undo migration" button is ever added to the Settings panel, an
IPC handler (`rollback_migration`) must be added to `service.py`.

**Recommendation:** Add `rollback_migration` to the IPC dispatch table, or document explicitly
that rollback is a developer-only operation.

### Gap 2 — Version not persisted in NDJSON

**Risk:** Low–Medium. The version is re-derived each time from record field presence. Works
correctly for the current binary 1.0/2.0 split. A future schema version that is a superset of
v2 fields could be mis-detected as v2.

**Recommendation:** Persist `{"_schema_version": "2.0"}` as the first line (or a sidecar
`.schema_version` file) so detection is O(1) and independent of data content.

### Gap 3 — No `"2.0"` → `"3.0"` path prepared

**Risk:** None today. The gap will be relevant when a v3 history schema is introduced.

**Recommendation:** When introducing v3, add the `("2.0", "3.0")` entry to both
`DataMigrator._migrate()` and `SettingsValidator._MIGRATIONS`, and bump both `LATEST_VERSION`
constants together (they are currently in separate files and must be kept in sync manually).

### Gap 4 — Backup not cleaned up automatically

**Risk:** Low. Backups in `<data_dir>/backups/` accumulate unboundedly. A 100 k-item history
file could produce large backups on each migration call.

**Recommendation:** `AutoBackupManager` (`backend/auto_backup.py`) already has a rolling copy
limit — consider routing migration backups through it, or at minimum cap the number of
`migration_backup_*` dirs to 5.

### Gap 5 — `SettingsValidator` and `DataMigrator` are not co-ordinated

**Risk:** Low. Both are invoked independently (settings migration in `SettingsService`, history
migration via explicit IPC call). There is no startup gate that ensures both are at `"2.0"`
before the service accepts requests.

**Recommendation:** `StartupDiagnostics` (`backend/startup_diagnostics.py`) could call
`DataMigrator.check_migration_needed()` and surface a warning if history is still at v1.

---

## Git History

```
a47014fb test(backend): CostEstimator + DataMigrator + ExportScheduler coverage (#171)
6d829a90 feat(ear): v2.0.0 release — Liquid Glass GUI, Swift 6 concurrency, 4485 tests
```

`DataMigrator` was introduced in the v2.0.0 batch and received dedicated test coverage in PR
#171. No subsequent changes — the module is stable.

---

## Conclusion

- **Latest schema version:** `"2.0"` (both `LATEST_VERSION` in `data_migrator.py` and
  `CURRENT_SCHEMA_VERSION` in `settings_validator.py`).
- **Covered migration paths:** `"1.0"` → `"2.0"` (history data) and `"1.0"` → `"2.0"`
  (settings), both fully tested.
- **Missing migration paths:** none for current versions; no intermediate versions (`"1.1"`,
  `"1.2"`, etc.) were ever defined.
- **Primary gaps:** no IPC handler for rollback; version not persisted in file; no startup
  check wires `DataMigrator` into `StartupDiagnostics`.
