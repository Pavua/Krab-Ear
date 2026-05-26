# Wave 837 — StateStore NDJSON Audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/state_store.py`  
**Lines:** 1285  
**Auditor:** Wave 837 (doc-only pass)

---

## Summary

`StateStore` is the persistence backbone of Krab Ear: append-only NDJSON history,
tombstone-based deletes, delta-journals for every mutable field, and periodic
compaction. The design is sound for single-process use. Four risk areas were found,
one of which (compaction crash-safety) warrants a fix before the next release.

| # | Area | Severity | Status |
|---|------|----------|--------|
| 1 | Compaction crash-safety (no fsync on delta-journal truncation) | MEDIUM | Open |
| 2 | Lock-file opened as `r+` — fails on first touch if file is empty | LOW | Open |
| 3 | `_compact_unlocked` does not fsync the temp file before `replace()` | MEDIUM | Open |
| 4 | `_append_ndjson` is `@staticmethod` — can be called outside the lock | LOW | Design note |
| 5 | `auto_cleanup_old` acquires/releases lock between read and delete | LOW | Open |
| 6 | `save_vocabulary` writes in-place (not atomic) | LOW | Open |
| 7 | No cross-process lock guard — `fcntl.LOCK_EX` only protects same-process threads | INFO | By-design |

---

## 1. Compaction crash-safety — MEDIUM

### Problem

`_compact_unlocked` (lines 846–862) rewrites history in three phases:

```python
# Phase 1: write active items to a temp file
with tmp_history.open("w", encoding="utf-8") as fh:
    for item in active:
        fh.write(...)
    fh.flush()            # <-- page-cache flush only

tmp_history.replace(self.history_path)  # <-- atomic rename

# Phase 2: truncate all delta journals
self.tombstones_path.write_text("", encoding="utf-8")
self.status_path.write_text("", encoding="utf-8")
self.tags_path.write_text("", encoding="utf-8")
self.favorites_path.write_text("", encoding="utf-8")
self.text_updates_path.write_text("", encoding="utf-8")
self.action_items_path.write_text("", encoding="utf-8")
```

Two issues:

**A. Missing `os.fsync()` on the temp file before rename.**  
`fh.flush()` only flushes Python's internal buffer to the OS page cache.
`os.fsync()` is required to guarantee the data is committed to stable storage before
the rename. If the process crashes between the rename and the delta-journal truncations,
history data that was still in the page cache can be lost (the old delta journals are
already overwritten on the next boot).

**B. Delta-journal truncations are not atomic as a group.**  
`write_text("")` on each delta journal calls `open("w")` which truncates the file
immediately. If the process is killed after truncating `tombstones_path` but before
truncating `status_path`, the status overrides for deleted items will be orphaned in
`status_path` but the tombstones will have been cleared — those items are now visible
again with potentially stale paste_status values.

### Fix recommendation

```python
# In _compact_unlocked, after the loop:
fh.flush()
os.fsync(fh.fileno())    # commit data before rename

tmp_history.replace(self.history_path)

# Fsync the parent directory to commit the rename itself
os.fsync(self.history_path.parent.open("r").fileno())

# Then truncate delta journals (acceptable: each is individually safe to truncate
# because compaction already embedded the final state into history.ndjson)
```

The delta truncation order does not need to be atomic as long as the rename is
durable first: on replay, loading active items re-applies all surviving delta
journals to the already-compacted history, so partial truncation is safe provided
`history.ndjson` itself is durable.

---

## 2. Lock-file `open("r+")` fails on empty file — LOW

### Problem

`_lock` (lines 108–117):

```python
with self.lock_path.open("r+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
```

The `__init__` calls `self.lock_path.touch(exist_ok=True)` which creates an empty
file. Opening an empty file as `"r+"` succeeds on macOS, but `"r+"` requires the
file to exist and be readable. On some filesystems (e.g., network volumes mounted
read-only, or if `touch` raced with a permission change) the open can fail with
`FileNotFoundError` or `PermissionError`, raising an unhandled exception inside the
context manager — locking is bypassed entirely for that caller.

### Fix recommendation

Use `"a+"` mode (creates if absent, does not truncate, supports read/write). This
is the idiomatic pattern for advisory lock files:

```python
with self.lock_path.open("a+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    ...
```

---

## 3. `_append_ndjson` fsync — correct, but `save_vocabulary` and `compact` delta truncation are not — LOW/MEDIUM

### Positives

`_append_ndjson` (lines 1113–1119) correctly calls `fh.flush()` followed by
`os.fsync(fh.fileno())` before returning. Every append to history, tombstones,
status, tags, favorites, text_updates, action_items, calendar_links, and
annotations is durable.

### Gap: `save_vocabulary`

`save_vocabulary` (lines 160–164) writes the vocabulary file in-place:

```python
self.vocabulary_path.write_text("\n".join(unique_words) + "\n", encoding="utf-8")
```

`Path.write_text` is not atomic — it opens the file with `"w"` and writes. A crash
mid-write truncates the vocabulary without a backup. This is lower risk than the
history store because vocabulary loss is annoying but not data-loss for transcriptions.

Recommendation: write to a `.tmp` file and rename, same pattern as `save_settings`.

### Gap: `save_settings` is atomic but not fsynced

`save_settings` (lines 139–150) correctly uses a temp-file + rename pattern:

```python
tmp_path.write_text(json.dumps(settings, ...), encoding="utf-8")
tmp_path.replace(self.settings_path)
```

But `Path.write_text` does not fsync. On a sudden power loss, the renamed file can
contain zero bytes even though the rename itself succeeded (the page cache had the
data but the inode was not yet committed). Low probability on SSDs with battery-backed
write cache, but not zero.

Recommendation: open the tmp file explicitly, write, flush, fsync, close, then rename.

---

## 4. `_append_ndjson` is a `@staticmethod` — callers must hold the lock — LOW

### Observation

`_append_ndjson` is declared `@staticmethod` (line 1113). This is correct for a
pure utility function, but it means the method has no access to the instance lock
and cannot enforce that it is called inside `with self._lock()`.

Current callers:
- `add_history_item` — holds lock. OK.
- `set_paste_status` — holds lock. OK.
- `delete_history_item` — holds lock. OK.
- `import_history_ndjson` — holds lock. OK.
- `update_history_item_tags` — holds lock. OK.
- `update_history_item_favorite` — holds lock. OK.
- `set_annotation` — holds lock. OK.
- `delete_annotation` — holds lock. OK.
- `update_history_item_calendar` — holds lock. OK.
- `update_history_item_action_items` — uses its own `open("a")` directly (line 970), NOT `_append_ndjson`. OK but inconsistent.
- `update_history_item_text` — uses its own `open("a")` directly (line 991), NOT `_append_ndjson`. OK but inconsistent.

The two methods that bypass `_append_ndjson` do not get the `os.fsync()` that the
static helper provides. This means text and action_items overrides are **not
fsynced to disk** before the method returns.

### Fix recommendation

Replace the manual `open("a")` blocks in `update_history_item_text` (line 991) and
`update_history_item_action_items` (line 970) with calls to
`self._append_ndjson(path, entry)`.

---

## 5. `auto_cleanup_old` TOCTOU — LOW

### Problem

`auto_cleanup_old` (lines 623–669):

```python
with self._lock():
    active = self._load_active_items_unlocked()   # read under lock

to_delete = [item for item in active if item.ts < threshold_dt]

if not dry_run:
    for item in to_delete:
        self.delete_history_item(item.id)          # re-acquires lock per item
```

The lock is released between the `_load_active_items_unlocked` snapshot and the
sequence of `delete_history_item` calls. Each `delete_history_item` call correctly
acquires the lock for its tombstone write. However, a concurrent caller could add
new history items between the snapshot and the deletions. If a newly added item
happens to receive an ID that was also in the snapshot's `to_delete` list (which
requires UUID collision — effectively impossible), or if another caller compacts
between the snapshot and the deletes (benign — the tombstone append is still safe
post-compaction), this is harmless in practice.

A more theoretical concern: the `oldest_age_days` computation also runs outside
the lock (`datetime.fromisoformat(item.ts)` on a potentially stale `active`
snapshot). No write occurs here, so it is safe.

**Risk level: LOW** — no real-world scenario causes data corruption. Noted for
completeness.

---

## 6. Tombstone semantics — correct

### Analysis

Tombstone design:

- `delete_history_item` appends `{"id": item_id}` to `history_tombstones.ndjson`.
- `_load_deleted_ids_unlocked` builds a `set[str]` of all tombstoned IDs.
- `_load_active_items_unlocked` filters out any item whose ID appears in the
  deleted set before applying delta overrides.
- After compaction, `history.ndjson` contains only active items (tombstoned items
  are excluded), and `history_tombstones.ndjson` is truncated.

**Correctness:** A tombstone for a non-existent ID is silently ignored (no error).
This is correct and idempotent — double-deletes are safe.

**Gap:** There is no "undelete" capability. Once an item is tombstoned, it cannot
be recovered unless the user restores from a backup. This is by design and
documented implicitly through the compaction that purges tombstones. No issue.

**Gap:** The annotations journal (`history_annotations.ndjson`) uses an empty
`note` string as a tombstone for annotation deletion (line 1046). This is a
different convention from the main tombstone (`{"id": ...}` in a separate file).
The `_load_annotation_overrides_unlocked` method correctly filters out empty notes
(line 1071). Consistent within itself, but the dual tombstone convention (file vs
empty-value) could confuse future contributors. Low risk.

---

## 7. Cross-process lock — INFO (by design)

`fcntl.LOCK_EX` is an advisory, per-process lock. It protects concurrent threads
within the same Python process. It also protects against a second Python process
opening the same lock file. However:

- The lock is NOT held during reads (`_read_ndjson_unlocked`, `_iter_history_items_unlocked`
  are called within `with self._lock()` blocks — so reads ARE protected).
- The lock file is opened as `"r+"` which means it does not prevent a different
  process from opening it as `"w"` and truncating it (though no current code path
  does this).
- The integrity checker (`backend/integrity_checker.py`) reads NDJSON files
  independently and does not use the same lock. If `IntegrityChecker` runs a repair
  concurrently with a `StateStore` compaction, files can be observed in an
  inconsistent mid-compaction state. **Recommendation:** integrity check + repair
  should acquire the same lock file via `fcntl.flock` before reading or writing.

---

## 8. Search index race — INFO

`search_history` (lines 305–415) rebuilds the `SearchIndex` from `active` items
(line 339) and then calls `self._search_index.search(...)` outside the lock (the
lock was released at line 327). The `SearchIndex` rebuild and search are logically
isolated to that call's `active` snapshot, so the race is benign for correctness.
However, if two threads call `search_history` concurrently, they will both call
`self._search_index.build_index(...)` simultaneously on the shared `self._search_index`
instance. `SearchIndex.build_index` likely replaces an internal dict — not atomic.

**Risk level: INFO** — worst case is a stale search result on the second thread, not
data corruption. The `_recent_search_index` cache is only mutated inside the lock,
so its consistency is maintained.

---

## Good practices observed

- Every `_append_ndjson` call is inside `with self._lock()`.
- `_append_ndjson` always calls `flush()` + `os.fsync()` before returning.
- `save_settings` uses atomic temp-file + rename.
- All `_load_*_unlocked` helpers are called only from within `_lock()` context.
- Method naming convention (`_unlocked` suffix) clearly signals which methods require
  the caller to hold the lock — reduces accidental unguarded calls.
- `_parse_cursor` safely clips out-of-range cursor values, preventing index
  out-of-range panics.
- `safe_json_loads` in `_read_ndjson_unlocked` silently skips malformed lines — reads
  are degradation-tolerant.
- `HistoryItem.from_dict` exceptions in `_iter_history_items_unlocked` are caught and
  skipped — forward-compatible with schema evolution.
- Compaction is idempotent: running it twice gives the same result.

---

## Action items

| Priority | Item | Location |
|----------|------|----------|
| MEDIUM | Add `os.fsync()` to temp file in `_compact_unlocked` before `replace()` | L852–856 |
| MEDIUM | Replace `write_text("")` delta truncations in `_compact_unlocked` with fsync-safe pattern | L857–862 |
| LOW | Change `_lock` to open lock file as `"a+"` instead of `"r+"` | L112 |
| LOW | Route `update_history_item_text` and `update_history_item_action_items` through `_append_ndjson` to get fsync | L970, L991 |
| LOW | Make `save_vocabulary` atomic (temp-file + rename) | L164 |
| LOW | Add `os.fsync` after `save_settings` tmp write before rename | L148 |
| INFO | Require integrity checker to acquire the same lock file before repair | `integrity_checker.py` |
| INFO | Document dual tombstone convention (file-based vs empty-value) in docstring | L1062 |
