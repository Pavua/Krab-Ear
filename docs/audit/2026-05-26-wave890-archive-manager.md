# Wave 890 — ArchiveManager audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/archive_manager.py`  
**Auditor:** wave890 / Claude Sonnet 4.6  
**Scope:** move atomicity, threshold logic, race with active store

---

## Summary

3 findings of medium/low severity. No data-loss path under normal usage, but two
race conditions exist under concurrent access and one design gap means there is no
automatic age-based archiving at all.

---

## F1 — MEDIUM: archive_items holds two separate locks, creating a TOCTOU window

**Location:** `archive_items`, lines 111-125

```python
with self._lock:                        # ArchiveManager lock only
    item = _store.get_history_item_by_id(clean_id)   # reads StateStore
    ...
    self._append_ndjson(self._archive_path, item_dict) # writes archive
    _store.delete_history_item(clean_id)               # writes StateStore (acquires _store._lock internally)
```

`ArchiveManager._lock` (threading.Lock) and `StateStore._lock` (fcntl LOCK_EX) are
acquired separately and in sequence, never together. Between
`_append_ndjson(archive)` and `_store.delete_history_item()` a concurrent IPC call
(e.g. `delete_history_item` directly, or a compaction) could also tombstone the same
item. The result is the item appears in both the archive and a tombstone, which is
harmless on read (tombstoned items are filtered) but wastes space and confuses
`get_archive_stats` counts.

More critically, if `_store.delete_history_item` raises after `_append_ndjson`
already wrote to the archive file, the item is **archived but not removed from the
active store** — effectively duplicated. Because `_append_ndjson` has no
compensating rollback, this window is unrecoverable without manual repair.

**Risk:** low probability (concurrent archiving is uncommon), medium impact (silent
duplication if the exception path is hit). No data loss — the item is preserved in
both places — but integrity is broken.

**Recommendation:** wrap the read→write→delete sequence inside `_store._lock()` as
well (or expose a `move_to_archive` method on StateStore that holds the file-lock for
the whole sequence), then only call `_append_ndjson` inside that combined critical
section.

---

## F2 — LOW: unarchive_items loses the item permanently if add_history_item raises

**Location:** `unarchive_items`, lines 163-178

```python
try:
    _store.add_history_item(text=..., ...)   # may raise
    unarchived_count += 1
except Exception as exc:
    logger.error(...)
    remaining.append(item)   # item stays in archive — OK
```

When `add_history_item` raises, `remaining.append(item)` correctly keeps the item in
the archive. This part is safe. However, `_rewrite_archive(remaining)` is called
**after the loop** (line 183), while the loop itself modifies `found_ids` in memory.
If the process crashes between the loop exit and `_rewrite_archive`, the archive file
still contains items whose `add_history_item` succeeded — those items are now in both
the active store and the archive, which requires manual deduplication.

The tmp-file rename inside `_rewrite_archive` is itself atomic (line 83: `tmp.replace`),
so partial writes are not possible. The gap is only the window between the last
successful `add_history_item` call and the `_rewrite_archive` rename.

**Risk:** low probability (process kill during unarchive), low-to-medium impact
(duplicated items, not lost items).

**Recommendation:** rewrite the loop to build `remaining` speculatively, then call
`_rewrite_archive` before calling `add_history_item`, or alternatively keep a
per-item journal so a recovery pass can detect and deduplicate.

---

## F3 — LOW: no age-based threshold — "archive old items" is caller-driven only

**Location:** public API overall

The module is described as "move old history entries into a separate archive.ndjson
to keep the main store lean." However, `ArchiveManager` has no method that selects
items by age (e.g. older than N days) and archives them automatically. All four
public methods (`archive_items`, `unarchive_items`, `list_archived`, `get_archive_stats`)
require the caller to supply explicit `item_ids`.

There is no IPC method, background task, or scheduled job anywhere in the codebase
that automatically selects old items and calls `archive_items`. The auto-archiving
described in CLAUDE.md exists only conceptually — in practice the archive stays empty
unless the caller explicitly provides IDs.

**Risk:** none for data integrity, but the feature does not fulfil its stated purpose
of keeping the active store lean.

**Recommendation:** add an `archive_older_than(days: int) -> ArchiveResult` method
that queries the store for items with `ts < now - days`, then calls `archive_items`
internally. Wire it to a periodic IPC method `archive_old_history` and optionally
to the existing compaction path in `StateStore`.

---

## What is correct

- `_rewrite_archive` is properly atomic: writes to `.ndjson.tmp` then renames.
  Crash during rewrite leaves the original intact.
- `_append_ndjson` uses open-mode `"a"` — safe for incremental writes to the archive
  file; no truncation risk.
- `ArchiveManager._lock` (threading.Lock) prevents concurrent Python threads from
  interleaving reads/writes to the archive file. This protects the in-process case.
- `list_archived` clamps `limit` to 1–500, preventing unbounded reads.
- IPC handlers validate `item_ids` type before delegating.
- `unarchive_items._rewrite_archive` correctly excludes successfully restored items.

---

## Findings table

| ID | Severity | Location | Description |
|----|----------|----------|-------------|
| F1 | MEDIUM | `archive_items` L111-125 | Two separate locks create TOCTOU: item can end up in both archive and active store if `delete_history_item` raises after archive append |
| F2 | LOW | `unarchive_items` L163-183 | Process kill between `add_history_item` success and `_rewrite_archive` rename duplicates items |
| F3 | LOW | public API | No age-threshold selector — auto-archiving is not implemented; feature does not self-activate |
