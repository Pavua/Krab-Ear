# W1471 Second-Pass Audit: state_store.py post-wave verification

**Date:** 2026-05-27
**Branch:** `audit-state-store-second-W1471`
**Scope:** `KrabEar/backend/state_store.py` + `KrabEar/backend/models.py`
**Prior waves audited:** W853, W1238, W1259, W1237, W1310, W1302
**New findings:** 5

---

## Merge State (6 prior waves)

Verified against `codex/krab-ear-v2` HEAD (`f7086279`).

| Wave | Branch | Description | Status |
|------|--------|-------------|--------|
| W853 | `origin/feature/audit-state-store-fixes-W853` | `_compact_unlocked` fsync + atomic journal truncation | **MERGED** (commit `ac677c6f`) |
| W1238 | `origin/fix-history-forward-compat-W1238` | `HistoryItem._extra` forward-compat sidecar | **MERGED** (commit `bbd19270`) |
| W1237 | `origin/fix-statestore-field-forwarding-W1237` | `add_history_item` 8-field forwarding | **MERGED** (commit `c6cbb861`) |
| W1310 | `origin/fix-compact-background-W1310` | `maybe_compact_async` / `compact_async` daemon threads | **MERGED** (commit `2827416e`) |
| W1302 | `origin/audit/state-store-post-fixes-W1302` | Audit doc (6 findings) | **MERGED** (commit `1215b403`) |
| W1259 | `origin/fix-version-cascade-W1259` | Version cascade on delete/archive/compact | **NOT MERGED** |
| W1309 | *(no remote branch)* | Defer startup compact until `_transcript_versioner` wired | **NOT MERGED** |
| W862 | `origin/feature/add-test-state-store-W862` | W853 fsync atomicity tests | **MERGED** (commit `4eac6bd1`) |

**Note:** W1302 F1 (startup compact skips version cascade) and its companion fix W1309 remain unmerged.
W1302 F2 (compact_with_stats 3-read lock hold) was addressed by W1310 adding `compact_async` wrapper but
the underlying `compact_with_stats` synchronous path still holds the global lock across 3 full NDJSON reads.

---

## Findings

### F1 — MEDIUM: `auto_cleanup_old` releases lock between snapshot and delete loop

**File:** `KrabEar/backend/state_store.py` lines 733–779

`auto_cleanup_old` acquires `_lock`, loads the full active item list, then releases the lock before the delete loop:

```python
with self._lock():
    active = self._load_active_items_unlocked()     # lock released here

to_delete = [item for item in active ...]           # filter outside lock

if not dry_run:
    for item in to_delete:
        self.delete_history_item(item.id)           # re-acquires lock N times
```

Between the snapshot at line 750 and the tombstone writes at line 771, concurrent IPC threads can:
1. **Add new items**: the `remaining` count in the return value is calculated as `len(active) - len(to_delete)` using the stale snapshot, producing an incorrect value.
2. **Delete the same items**: `delete_history_item` is idempotent (appends duplicate tombstone), safe but wasteful.
3. **Re-add a deleted item** (import or add_history_item with same ID): the item would be tombstoned despite not being in the original snapshot's version of "old items".

The primary correctness risk is the stale `remaining` count and potential over-deletion if concurrent
import-then-delete sequences happen during the window.

**Fix:** Hold the lock across the entire operation, or re-snapshot inside the delete loop and skip items
that no longer appear in the live active set.

---

### F2 — MEDIUM: `import_history_ndjson` skips tombstone check — tombstoned items can be re-imported after compaction

**File:** `KrabEar/backend/state_store.py` lines 524–555

`import_history_ndjson` builds `known_ids` from `_iter_history_items_unlocked()` — the raw NDJSON
history file — rather than from the active (tombstone-filtered) view:

```python
with self._lock():
    known_ids = {item.id for item in self._iter_history_items_unlocked()}
    ...
```

Before compaction, tombstoned items are still present in `history.ndjson`, so their IDs appear in
`known_ids` and re-import is correctly blocked.

**After compaction**, `history.ndjson` only contains active items; tombstoned items have been removed.
At that point `known_ids` does not contain the deleted IDs, so a subsequent `import_history_ndjson`
call with an export file that includes those same items will silently re-import them, resurrecting
deleted history entries.

The `tombstones_path` is also cleared by `_compact_unlocked`, so there is no persistent record of
deletions to consult.

**Fix:** Build `known_ids` from `_load_active_items_unlocked()` (which applies tombstones from the live
delta journal) instead of `_iter_history_items_unlocked()`. This is correct both before and after
compaction because the delta journals are the authoritative source of tombstone state.

---

### F3 — LOW: `save_settings` writes tmp file without `fsync` before atomic rename

**File:** `KrabEar/backend/state_store.py` lines 140–151

W853 added `os.fsync()` to both `_append_ndjson` (hot write path) and `_compact_unlocked` (compact
write path). However `save_settings` uses a tmp-file + atomic rename pattern without fsyncing:

```python
tmp_path.write_text(json.dumps(settings, ...), encoding="utf-8")
tmp_path.replace(self.settings_path)
```

`write_text` calls `flush()` internally but does not fsync. A crash between the kernel buffer flush and
the physical write commits the rename but leaves `settings.json` with an incomplete/stale file on disk
(the OS page cache version was not flushed to storage before the rename completed). On macOS APFS
the risk is low due to journaling, but the fix is trivially consistent with the established W853 pattern.

**Fix:** Open tmp_path explicitly, write, `flush()`, `os.fsync(fileno())`, close, then `replace()`.

---

### F4 — LOW: `annotations_path` and `calendar_links_path` accumulate orphaned entries after compaction

**File:** `KrabEar/backend/state_store.py` lines 983–996

`_compact_unlocked` clears these delta journals on compact:
`tombstones_path`, `status_path`, `tags_path`, `favorites_path`, `text_updates_path`, `action_items_path`.

However `annotations_path` and `calendar_links_path` are NOT cleared on compact. They are last-write-wins
journals whose data is read by `get_annotation` / `get_history_item_calendar` — not embedded in the
compacted `HistoryItem` serialisation. After multiple compaction cycles with deletions, these two files
accumulate entries for tombstoned items indefinitely.

Unlike the delta journals that ARE cleared (which have their state baked into the compacted history),
annotations and calendar links for deleted items become orphaned noise. They cannot be "fixed" by
a future compact because the deleted item's ID no longer appears anywhere — `get_annotation` and
`get_history_item_calendar` both guard with an active-item check, so the orphaned entries are never
returned to callers. However the files will grow unboundedly over time on active installations.

**Fix:** After `tmp_history.replace(self.history_path)`, load `active_ids = {item.id for item in active}`
(already available from the pre-compact call to `_load_active_items_unlocked`). Then compact
`annotations_path` and `calendar_links_path` in-place, keeping only entries whose `id` is in
`active_ids`.

---

### F5 — LOW: W1302 F1 remains open — startup compact fires without `_transcript_versioner` wired

**File:** `KrabEar/backend/service.py` lines 2238–2248

W1302 F1 identified that `build_service()` calls `store.maybe_compact_async()` before
`BackendService.__init__` runs, so `_transcript_versioner` is `None` during any startup compact.
W1309 (fix: defer compact until after versioner is wired) is NOT merged into `codex/krab-ear-v2`.
W1259 (fix: version cascade in `_compact_unlocked`) is also NOT merged.

Current state:

```python
def build_service(data_dir):
    store = StateStore(data_dir=data_dir)
    store.save_settings(...)
    store.maybe_compact_async()          # fires before BackendService.__init__
    return BackendService(store=store)   # _transcript_versioner wired here
```

The async thread in `maybe_compact_async` races against `BackendService.__init__`. If the compaction
thread acquires the file lock before `BackendService.__init__` completes, `_transcript_versioner` is
still `None` and any version cascade logic in `_compact_unlocked` (once W1259 is merged) will silently
no-op, leaving orphaned version records for items that were just tombstone-cleared by compaction.

Even today (without W1259), the ordering is architecturally unsound: the compact runs on a StateStore
that has no knowledge of the versioning subsystem.

**Fix:** Call `store.maybe_compact_async()` from inside `BackendService.__init__` **after**
`self._transcript_versioning` is assigned and `store._on_compact_hook` is wired (as W1309 prescribes),
or accept W1259 + W1309 together as a single coherent fix.

---

## W1302 F2 Residual Status

W1302 F2 (compact_with_stats holds global lock across 3 NDJSON reads) was **partially addressed** by
W1310 which added `compact_async` and `maybe_compact_async` wrappers so callers can use fire-and-forget
semantics. However the underlying `compact_with_stats` (called by `compact_async._worker`) still holds
`_lock` across:

1. `_history_stats_unlocked()` — full NDJSON read (pre-compact stats)
2. `_compact_unlocked()` — full NDJSON read + write
3. `_history_stats_unlocked()` — full NDJSON read (post-compact stats)

The async wrapper moves the lock contention off the IPC startup path but does not reduce total lock
hold time. An explicitly IPC-triggered `compact_history` call (from Swift HistoryPanel) still holds the
lock for the full 3-read duration. This is considered a known limitation (see W1302 F2) and not a new
finding.

---

## Test Coverage Status

| Behavior | Test file | Covered |
|----------|-----------|---------|
| W853 fsync before rename | `test_state_store_w853_fsync_atomicity.py` | Yes (5 tests) |
| W853 atomic delta-journal truncation | `test_state_store_w853_fsync_atomicity.py` | Yes |
| W1238 `_extra` round-trip through compact | `test_statestore_field_forwarding_W1237.py` | Partial |
| W1237 8-field forwarding | `test_statestore_field_forwarding_W1237.py` | Yes |
| W1310 compact_async returns immediately | `test_compact_async_W1310.py` | Yes (13 tests) |
| F1 auto_cleanup_old race | None | **No** |
| F2 import re-inserts tombstoned+compacted items | None | **No** |
| F3 save_settings fsync | None | **No** |
| F4 annotations/calendar orphan accumulation | None | **No** |
| F5 W1309 startup compact ordering | None | **No** |

---

## Interaction Analysis

| Fix pair | Composes correctly? | Notes |
|----------|---------------------|-------|
| W853 + W1237 | Yes | Independent |
| W853 + W1238 | Yes | `_extra` survives compact |
| W853 + W1310 | Yes | W1310 builds on W853-correct compact |
| W1237 + W1238 | Yes | Orthogonal |
| W1259 + W1309 | **Must merge together** | W1309 defers compact until versioner wired; W1259 adds the cascade logic. Merging W1259 alone without W1309 means the cascade fires but with `_transcript_versioner = None` at startup. |
| F2 fix + compact | Yes | Use `_load_active_items_unlocked()` instead of `_iter_history_items_unlocked()` in import; no impact on compact path |

---

## Corruption Recovery Status

`_read_ndjson_unlocked` still handles all original cases (truncated line, empty line, non-dict JSON).
No regressions observed. Post-compact count verification (W1302 F4) remains unimplemented but is low
priority as W853 guarantees fsync before rename.
