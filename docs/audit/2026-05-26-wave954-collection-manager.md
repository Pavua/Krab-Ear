# Wave 954 — CollectionManager Audit

**File:** `KrabEar/backend/collection_manager.py`  
**Tests:** 4 test files, ~120 test methods  
**Date:** 2026-05-26  
**Auditor:** W954 sub-agent

---

## Summary

`CollectionManager` is a straightforward, well-tested module. It manages named collections of history-item IDs persisted in `collections.json`. Concurrency is guarded by a single `threading.Lock`. Five findings are identified below, ordered by severity.

---

## Findings

### F1 — MEDIUM: Naïve `write_text` — no tmp+fsync+rename atomic save

**Location:** `_save()` (line 61–70)

```python
self._collections_path.write_text(
    json.dumps(self._data, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
```

`Path.write_text()` truncates the existing file then writes. A crash or `KeyboardInterrupt` mid-write produces a truncated (corrupt) `collections.json`. The correct pattern used elsewhere in the codebase (e.g. `StateStore`, `SettingsBackup`) is:

```python
tmp = self._collections_path.with_suffix(".tmp")
tmp.write_text(...)
tmp.replace(self._collections_path)   # atomic on POSIX
```

Risk: single crash at write time loses all collection metadata silently on next load (falls back to empty state). No data deduplication or recovery possible.

---

### F2 — MEDIUM: Stale item_ids not purged from collection on `get_collection_items` reads

**Location:** `get_collection_items()` (line 229–258)

When a history item is deleted from the store, its ID remains in `col["item_ids"]` indefinitely. The method correctly skips the orphan at read time (F2a is handled), but:

- `item_count` in `list_collections()` reflects the raw `len(item_ids)` including orphans, so the UI shows an inflated count (e.g. "5 items" when only 3 still exist in history).
- The stale IDs accumulate forever — a collection that has had 1000 items added and deleted will carry 1000 dead IDs on disk, growing without bound.

Test `test_item_count_does_not_drop_when_history_deleted` in `test_collection_manager_extras.py` (line 352) explicitly documents and asserts this behaviour as intentional, but it is a known divergence between displayed count and actual accessible items.

No lazy GC or purge mechanism exists.

---

### F3 — LOW: No limit on items per collection or total collections

**Location:** `create_collection()`, `add_to_collection()`

No maximum is enforced for:
- Number of named collections (unbounded dict growth in memory + file).
- Number of item_ids per collection (unbounded list).

A malicious or buggy IPC client could create thousands of collections or add millions of IDs. This would cause:
- Increasing `_save()` latency (the entire JSON is rewritten on every single `add_to_collection` call).
- Memory pressure from the in-memory `_data` dict.

The `_save()` call on every `add_to_collection` (line 158) combined with no upper bound makes bulk-add of N items produce N full JSON rewrites.

---

### F4 — LOW: Bulk operation atomicity — no transaction; partial state persisted on exception

**Location:** `add_to_collection()` called N times in a loop

There is no bulk-add API. Callers loop over `add_to_collection` individually. If an exception occurs on item K of N (e.g. disk full during `_save()`), items 0..K-1 are already persisted and item K onward is not. No rollback mechanism exists.

The exception path in `_save()` only logs the error (`logger.error`) and returns silently (line 69–70), so the in-memory `_data` contains the new item but the file does not — creating a silent in-memory/on-disk split until the next successful save.

---

### F5 — INFO: No privacy_mode guard — collections persist regardless of privacy mode

**Location:** `create_collection()`, `add_to_collection()`, `_save()`

`CollectionManager` has no reference to `privacy_mode`. When the backend operates in privacy mode (no history persistence), adding a history item to a collection still writes the item's ID to `collections.json` on disk. This leaks which history items existed (their IDs) even after a privacy-mode purge clears `history.ndjson`.

The item IDs themselves are UUIDs and don't contain transcript text, but the existence of an ID in a collection file after a privacy purge is a minor integrity gap — cross-referencing with other logs could reveal session timing.

---

## Checklist Summary

| # | Concern | Status |
|---|---------|--------|
| 1 | Atomic persist (tmp+fsync+rename) | ❌ Naïve `write_text` — F1 |
| 2 | Item reference integrity (deleted items) | ⚠️ Skipped at read; stale IDs not purged — F2 |
| 3 | Bulk atomicity | ⚠️ No bulk API; partial persist on exception — F4 |
| 4 | Concurrency / lock | ✅ Single `threading.Lock` wraps all CRUD |
| 5 | Name uniqueness | ✅ Enforced in `create_collection` and `rename_collection` |
| 6 | Privacy mode | ⚠️ No guard — F5 |
| 7 | Test coverage | ✅ ~120 methods across 4 files; concurrency, bulk, unicode, IPC covered |
| 8 | Maximum size | ❌ No limit enforced — F3 |
| 9 | Non-ASCII serialization | ✅ `ensure_ascii=False` + UTF-8 encoding; emoji/CJK tested |
| 10 | Cascade delete | ✅ Deleting a collection removes its entry; no reverse cascade needed (item refs are one-directional) |

---

## Recommended Fixes (priority order)

1. **F1 (MEDIUM):** Replace `_save()` with tmp+replace atomic write, matching the pattern used in `StateStore`.
2. **F2 (MEDIUM):** Add a `prune_stale_ids()` helper that calls `_store.get_history_item_by_id()` for each stored ID and removes missing ones, triggered on load or on explicit IPC call. Alternatively fix `_collection_to_dict` to return live count vs stored count separately.
3. **F3 (LOW):** Add configurable caps: `MAX_COLLECTIONS = 500`, `MAX_ITEMS_PER_COLLECTION = 10_000`. Raise `ValueError` when exceeded.
4. **F4 (LOW):** Add a `bulk_add_to_collection(collection_name, item_ids)` method that appends all IDs at once and calls `_save()` once at the end.
5. **F5 (INFO):** In `_save()`, check `self._store` for a `privacy_mode` attribute and skip disk write (or write an empty collections file) when active.
