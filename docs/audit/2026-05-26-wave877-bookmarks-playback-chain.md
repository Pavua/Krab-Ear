# Wave 877 Audit: bookmarks.py / playback_tracker.py / recording_chain.py

**Date:** 2026-05-26  
**Scope:** Data integrity, NDJSON tombstone handling, concurrency  
**Files audited:**
- `KrabEar/backend/bookmarks.py`
- `KrabEar/backend/playback_tracker.py`
- `KrabEar/backend/recording_chain.py`

---

## 1. bookmarks.py — BookmarkManager

### Storage format
Append-only NDJSON (`bookmarks.ndjson`). Tombstone deletion via `{"id": "...", "deleted": true}`.  
Active set reconstructed on every read by replaying the log in `_load_active()`.

### Findings

#### BUG-1 (Medium): TOCTOU race in `delete()`
`delete()` calls `_load_active()` outside the lock to check existence, then calls `_append()` (which acquires the lock). Between these two operations a concurrent delete for the same ID can write a tombstone first, then this call writes a duplicate tombstone. Duplicate tombstones are harmless to correctness (already-absent key is popped idempotently) but add noise to the log.

```python
# Current (lines 126-132):
active = self._load_active()          # <- lock released here
exists = any(b["id"] == clean_id ...)
if not exists:
    return False
self._append({"id": clean_id, "deleted": True})  # <- lock re-acquired here
```

Fix: move both the existence check and the append inside a single `with self._lock:` block, or use a dedicated internal method that does both under one lock.

#### BUG-2 (Medium): TOCTOU race in `update_session_id()`
`update_session_id()` calls `_load_active()` to get the bookmarks to migrate, then loops calling `_append()` per bookmark. Each `_append` acquires and releases the lock individually. A concurrent write can insert a new bookmark with `old_session_id` between the snapshot and the loop, leaving it unmigrated without any warning. This is a real risk when recording finalisation (which calls `update_session_id`) races with a new bookmark being added during recording stop.

#### INFO-1: Unbounded log growth
The file is append-only with no compaction. Long-running sessions accumulate tombstones indefinitely. There is no compaction method and no max-size guard. For a voice assistant producing many short bookmarks and deletions over weeks, the file will grow and `_load_active()` scan time increases linearly.

#### INFO-2: `add()` stores `"deleted": False` explicitly
The NDJSON delta records contain `"deleted": false` for live entries. This means the tombstone check `obj.get("deleted")` correctly treats `False` as falsy, but any tooling that looks for the key `"deleted"` in the file will see it even on live entries. Not a bug but increases verbosity; removing the field on write and relying on absence would be cleaner.

#### INFO-3: Bare `except Exception: pass` in `handle_jump_to_bookmark`
Lines 264-272 swallow all errors from the event bus silently. Acceptable for optional feature, but the silence makes it hard to diagnose why seek events are not delivered in tests or production.

### Concurrency assessment
`_append` is lock-guarded. `_load_active` reads under the lock but releases before parsing (parses outside). This is safe for the read path but creates the TOCTOU windows noted in BUG-1/BUG-2. No deadlock risk (lock is not reentrant and `_load_active` is not called from `_append`).

---

## 2. playback_tracker.py — PlaybackTracker

### Storage format
Flat JSON dict (`playback_stats.json`). Not NDJSON; no tombstone concept. Stats are updated in-place; the entire file is overwritten on each `record_playback()` call.

### Findings

#### BUG-3 (High): Non-atomic write — data loss on crash
`_save()` uses `Path.write_text()` (lines 67-70), which is not atomic: it truncates then writes. A crash or `KeyboardInterrupt` between truncation and completion produces a zero-byte or partial JSON file. On next startup `_load()` will fail silently (logs a warning) and all playback stats are lost.

```python
# Current (line 68-70):
self._path.write_text(
    json.dumps(self._stats, ...),
    encoding="utf-8",
)
```

Fix: write to a `.tmp` sibling and `replace()` atomically, matching the pattern used by `RecordingChainManager._save()`.

#### INFO-4: `_save()` called inside the lock in `record_playback()`
The file I/O happens while `self._lock` is held (lines 91-99). This is correct for consistency but means any I/O delay blocks concurrent reads via `get_playback_stats`. For a stats file written on every play event this is a minor perf concern, not a correctness issue.

#### INFO-5: `get_most_replayed()` releases lock before sorting
Lines 137-146 iterate `self._stats.items()` inside the lock to build `items`, then sort outside the lock. Safe — sorting operates on a local list copy, not on the shared dict.

#### INFO-6: `get_never_played()` takes `store` as a parameter
The dependency on `StateStore` is not stored at construction time (unlike `RecordingChainManager` which receives `store` in `__init__`). Callers must pass it on every call. This is an intentional design choice to avoid holding a reference, but creates a risk of callers passing `None` or an incompatible object.

#### INFO-7: No `handle_get_never_played` IPC method
`get_never_played()` has no corresponding `handle_*` IPC handler. If it is in the handler lookup table it will fail with an `AttributeError`; if it is not wired it is dead public API.

### Concurrency assessment
All mutating state (`self._stats`) is guarded by `self._lock`. Read methods acquire the same lock. No deadlock risk. The non-atomic write is the only correctness hazard (BUG-3).

---

## 3. recording_chain.py — RecordingChainManager

### Storage format
Flat JSON (`recording_chains.json`) with `{"chains": {<chain_id>: {...}}}` structure. Not NDJSON; no tombstones. Entire file rewritten on each mutation via atomic `tmp_path.replace()`.

### Findings

#### INFO-8: No hard delete of chains
Once a chain is `end_chain()`-ed its `ended_at` is set but the chain record remains forever. There is no method to delete a chain. Over time an unlimited number of ended chains accumulate. For long-lived installations this is a minor growth issue.

#### INFO-9: `get_chain()` releases lock before fetching item details
`get_chain()` acquires the lock to copy `item_ids` (lines 146-148), then releases it before querying the store (lines 151-165). A concurrent `unlink_recording_from_chain()` or `add_to_chain()` could modify the live chain while the detail fetch runs. The stale `item_ids` snapshot is used for the detail loop, which is safe for read-only detail enrichment, but the returned `item_ids` list in the response matches the snapshot, not the current state. This is a minor staleness window; unlikely to matter in practice.

#### INFO-10: `_save()` called inside `self._lock` with file I/O
All mutating methods call `_save()` while holding `self._lock`. The atomic write (tmp + replace) is correct; the I/O-under-lock concern is the same as INFO-4.

#### INFO-11: `store.data_dir` resolution falls back to `"."`
`RecordingChainManager.__init__` uses `getattr(store, "data_dir", ".")` (line 45). If a future refactor renames `StateStore.data_dir` or passes a store object without that attribute, chains are silently written to the current working directory instead of the data dir.

#### INFO-12: No input length limits on `name` or `item_ids`
`start_chain(name)` strips whitespace but places no upper bound on name length. `add_to_chain` appends `item_id` without checking whether the same item already appears in multiple chains (cross-chain membership is not prevented).

### Concurrency assessment
All mutations hold `self._lock`. `_save()` uses atomic rename, so no data loss on crash. The staleness window in `get_chain()` (INFO-9) is the only non-trivial concurrency note.

---

## Summary table

| ID | Severity | Module | Description |
|----|----------|--------|-------------|
| BUG-1 | Medium | bookmarks.py | TOCTOU in `delete()`: existence check and tombstone append not atomic |
| BUG-2 | Medium | bookmarks.py | TOCTOU in `update_session_id()`: snapshot + per-item append allows concurrent inserts to escape migration |
| BUG-3 | **High** | playback_tracker.py | Non-atomic write in `_save()` — `write_text()` truncates before write; crash → data loss |
| INFO-1 | Low | bookmarks.py | No compaction; `_load_active()` scans full log O(N) on each read |
| INFO-2 | Low | bookmarks.py | `"deleted": false` stored explicitly in live entries (verbose) |
| INFO-3 | Low | bookmarks.py | `handle_jump_to_bookmark` silently swallows event-bus errors |
| INFO-4 | Low | playback_tracker.py | File I/O inside lock in `record_playback()` |
| INFO-5 | OK | playback_tracker.py | `get_most_replayed()` sorts outside lock — safe (local copy) |
| INFO-6 | Low | playback_tracker.py | `get_never_played()` takes `store` per-call, no construction-time reference |
| INFO-7 | Low | playback_tracker.py | `get_never_played()` has no IPC handler — unreachable from Swift |
| INFO-8 | Low | recording_chain.py | No chain deletion; ended chains accumulate forever |
| INFO-9 | Low | recording_chain.py | `get_chain()` releases lock before enriching items — stale snapshot window |
| INFO-10 | Low | recording_chain.py | File I/O inside lock (correct but blocks concurrent reads) |
| INFO-11 | Low | recording_chain.py | `store.data_dir` fallback to `"."` silently misconfigures path |
| INFO-12 | Low | recording_chain.py | No name length limit; no prevention of cross-chain item membership |

**Total findings: 3 bugs (1 High, 2 Medium) + 12 informational.**

---

## Recommended actions

1. **BUG-3 (High, quick fix):** In `PlaybackTracker._save()`, replace `write_text()` with the atomic tmp-replace pattern already used in `RecordingChainManager._save()`. One-liner change, no API impact.

2. **BUG-1 (Medium):** In `BookmarkManager.delete()`, combine the existence check and tombstone append under a single `with self._lock:` block by calling `_load_active()` inline inside the lock (note: `_load_active` currently also acquires the lock — extract a lockless `_load_active_nolock()` helper first).

3. **BUG-2 (Medium):** In `BookmarkManager.update_session_id()`, hold the lock across the entire snapshot + append loop, using the lockless helper from BUG-1 fix.

4. **INFO-1 (Low):** Add a `compact()` method to `BookmarkManager` that rewrites the file with only live entries; wire to a cron/manual IPC call.

5. **INFO-7 (Low):** Add `handle_get_never_played` to `PlaybackTracker` (passing `store` from `BackendService`), or remove the public method if unused.
