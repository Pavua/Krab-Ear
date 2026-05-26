# Audit: DataMigrator — Wave 1026

**File:** `KrabEar/backend/data_migrator.py`  
**Date:** 2026-05-26  
**Auditor:** W1026 sub-agent

---

## Summary

`DataMigrator` is a versioned schema migrator for `history.ndjson`. It handles v1.0 → v2.0 migration (adds `tags`, `favorite`, `annotation` fields). Overall the implementation is solid — backup-before-write, atomic tmp-file rename, and idempotent logic are all present. Five concrete gaps identified below.

---

## Findings

### F1 — No startup auto-migration (MEDIUM)

**Status:** Migration is never called automatically. At startup (`KrabEar/main.py`) there is no call to `check_migration_needed` or `migrate`. The two IPC handlers `check_migration` / `run_migration` are registered in the dispatch table (service.py lines 1098–1099), but a user or Swift client must call them explicitly.

**Risk:** A v1.0 data directory upgraded from an older install will silently be left in v1.0 state. New fields (`tags`, `favorite`) will be missing from history items returned to Swift, potentially causing UI nil-dereference or missing-feature bugs.

**Recommendation:** Add a startup check in `BackendService.__init__` (or `main.py`) after the data dir is resolved:

```python
migrator = DataMigrator()
if migrator.check_migration_needed(data_dir):
    logger.info("Auto-migrating data dir %s", data_dir)
    migrator.migrate(data_dir)
```

---

### F2 — No dry-run mode (LOW)

**Status:** `migrate()` and `handle_run_migration()` have no `dry_run=True` parameter. There is a `get_migration_plan()` method that describes what *would* change, but no way to do a non-destructive trial run of the actual code path.

**Risk:** Low — `get_migration_plan()` partially covers this use case. However, for large stores or future multi-step migrations, operators cannot safely rehearse before committing.

**Recommendation:** Add `dry_run: bool = False` to `migrate()`. When `True`, compute `updated_lines` but skip `tmp_path.write_text()` and `tmp_path.replace()`. Return the same `MigrationResult` with `items_migrated` populated. Wire `dry_run` param through `handle_run_migration`.

---

### F3 — `rollback_migration` not exposed as IPC handler (MEDIUM)

**Status:** `rollback_migration()` is a public method and is fully tested, but it is **not registered in the IPC dispatch table** in `service.py`. There is no `"rollback_migration"` key alongside `check_migration` / `run_migration`.

**Risk:** The Swift client (or CLI operator) cannot trigger a rollback via IPC. If migration fails mid-write (e.g., disk full after backup but before rename completes — theoretically, but possible across NFS mounts), the only recovery path is manual shell access.

**Recommendation:** Add an IPC handler `handle_rollback_migration` to `DataMigrator` (takes `data_dir` + `backup_path` params) and register it in `service.py`:

```python
"rollback_migration": self._data_migrator.handle_rollback_migration,
```

---

### F4 — Version detection relies on field presence heuristic, not persisted metadata (LOW)

**Status:** `_detect_version_from_items()` infers schema version by checking whether `tags`/`favorite` fields are present in active history items. There is no `schema_version.json` or equivalent metadata file written to disk.

**Risk:**
- A store with zero active items (all tombstoned) always returns `LATEST_VERSION` regardless of actual historical format — test `test_tombstoned_items_excluded_from_version_check` confirms this behavior. A v1.0 store where every record was deleted will be incorrectly detected as v2.0.
- Adding a v3.0 migration later requires adding another heuristic field check; the absence of a canonical version file makes the chain fragile.

**Recommendation:** Write a small `schema_version.json` (e.g., `{"version": "2.0"}`) to `data_dir` after successful migration completion. `get_schema_version` should check this file first, falling back to field-presence heuristic only when the file is absent (legacy compatibility).

---

### F5 — No POSIX file lock during migration write (LOW)

**Status:** `_migrate_v1_to_v2` writes through a tmp file and uses `tmp_path.replace(history_path)` — the atomic rename is correct. However, it does **not** acquire the `history.lock` POSIX flock used by `StateStore` (`backend/state_store.py` lines 109–117 use `fcntl.flock`).

**Risk:** If a migration runs concurrently with a live `StateStore` append (e.g., user triggers migration while a transcription is being written), the lock-free migration rename can race with a non-atomic StateStore append that is still in-progress. On APFS this is unlikely to corrupt data (atomic rename wins), but on NFS/SMB shares the behavior is undefined.

**Risk level:** Low in single-user macOS deployment; higher for future NAS/multi-process setups.

**Recommendation:** Before writing the tmp file, acquire the same `history.lock`:

```python
lock_path = data_dir / "history.lock"
lock_path.touch(exist_ok=True)
with lock_path.open("r+") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(history_path)
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
```

---

## Checklist

| Criterion | Status |
|---|---|
| Idempotency (re-run on already-migrated store) | PASS — `migrate()` detects v2.0, returns early with `items_migrated=0` |
| Rollback on failure | PARTIAL — `rollback_migration()` exists and is tested; not exposed via IPC (F3) |
| Version detection | PASS with caveat — heuristic-based, no persisted metadata file (F4) |
| Dry-run mode | MISSING — no `dry_run` parameter (F2) |
| Backup before migration | PASS — `_create_backup()` always called before any write |
| Atomicity of migration write | PASS — `tmp_path.replace(history_path)` atomic rename |
| Wire status (startup auto-migration) | MISSING — no startup call (F1) |
| Test coverage | GOOD — 40+ test cases across 9 test classes in `test_data_migrator.py` |
| Schema evolution path documented | PARTIAL — IPC_API_REFERENCE.md mentions the two IPC methods; no changelog or upgrade-path doc |
| Edge case: empty store | PASS — returns `LATEST_VERSION`, migrate is no-op |
| Edge case: partial migration (mixed v1/v2 items) | PASS — `test_migrate_partial_v2_items_counted_correctly` covers this |
| Edge case: tombstone-only store | PASS (but see F4) — treated as v2.0 |

---

## Priority

| Finding | Severity | Effort |
|---|---|---|
| F1 — No startup auto-migration | MEDIUM | Small (3–5 lines in main.py or BackendService.__init__) |
| F3 — rollback not IPC-exposed | MEDIUM | Small (add handler + dispatch entry) |
| F4 — no persisted version metadata | LOW | Medium (write schema_version.json on migrate) |
| F5 — missing POSIX lock | LOW | Small (wrap with fcntl.flock) |
| F2 — no dry-run | LOW | Medium (add param + test) |
