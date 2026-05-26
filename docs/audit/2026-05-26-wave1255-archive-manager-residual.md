# W1255 — ArchiveManager Residual Audit (post-W1047/W896)

**Date:** 2026-05-26  
**Branch:** `audit/archive-manager-residual-W1255`  
**Scope:** `KrabEar/backend/archive_manager.py` re-audit after W1047 (metadata preservation) and W896 (dual-store atomicity)  
**Method:** Read-only static analysis

---

## Merge State of Prior Waves

| Wave | PR | Status on `codex/krab-ear-v2` |
|------|----|-------------------------------|
| W890 | #812 | MERGED — audit doc only, no code changes |
| W896 | #823 | **NOT MERGED** — fix for dual-store atomicity (write-first + rollback on archive_items, per-item restore + deferred rewrite on unarchive_items) |
| W1038 | #955 | **NOT MERGED** — audit doc only, 6 findings documented |
| W1047 | #968 | **NOT MERGED** — fix for metadata loss + UUID replacement in unarchive_items; adds `restore_history_item_raw` to StateStore |

**Critical:** The current `archive_manager.py` on `codex/krab-ear-v2` is the pre-W896 version. Both W896 and W1047 fixes exist only on their respective remote branches and have open PRs. All 4 residual findings below are observed against the **unfixed** baseline code.

---

## Findings

### F1 — MEDIUM: Semantic search index leaks stale embeddings on archive

**File:** `KrabEar/backend/archive_manager.py:112–125` (archive_items), `KrabEar/backend/semantic_search.py:211` (remove_item exists but is never called from archive path)

**Description:**  
`archive_items` calls `_store.delete_history_item(clean_id)` which writes a tombstone to `history_tombstones.ndjson`. However, `SemanticSearcher.remove_item(item_id)` is never invoked. The in-memory embedding matrix (`_index` list + `_embeddings` numpy array) and the persisted `embeddings.npy`/`embeddings_index.json` files retain the archived item's vector indefinitely.

Consequence: `semantic_search` and `keyword_fallback_search` can surface archived items in results even though those items have been tombstoned in the active store. The `keyword_fallback_search` fallback queries `store._load_active_items_with_lock()` which excludes tombstoned items, so keyword mode is safe — but semantic mode queries the embedding index directly and will return stale IDs.

`service.py:686–688` and `693–696` show that the `keyword_fallback_search` is called with active items only; the `SemanticSearcher.search()` at line 691 returns raw `(id, score)` pairs from the embedding index without filtering against active items.

The `remove_item` method (line 211 of `semantic_search.py`) is defined but has zero callers outside tests.

**Reproduction:** Archive an item → run `semantic_search` with a query matching the archived text → the archived item's ID appears in results.

**Fix direction:** Call `self._semantic_searcher.remove_item(item_id)` inside `archive_items` after the successful tombstone, or at the `service.py` dispatch layer wrapping `handle_archive_items`.

---

### F2 — MEDIUM: No cross-process file lock on `archive.ndjson` writes

**File:** `KrabEar/backend/archive_manager.py:70–75` (`_append_ndjson`), `KrabEar/backend/state_store.py:109–117` (`_lock()` uses `fcntl.flock`)

**Description:**  
`StateStore._lock()` protects `history.ndjson` with a POSIX `fcntl.flock(LOCK_EX)` against concurrent processes. `ArchiveManager._append_ndjson` and `_rewrite_archive` use only an in-process `threading.Lock`. If two processes (e.g., the production launchd backend and a dev `python KrabEar/main.py --data-dir` instance sharing the same data dir) both call `archive_items` simultaneously, both can `open("a")` on `archive.ndjson` concurrently, interleaving partial JSON lines and corrupting the file.

The concurrent `_rewrite_archive` scenario is worse: two processes each write to `archive.ndjson.tmp` and both call `tmp.replace(archive.ndjson)` — one silently overwrites the other's results.

This is distinct from the W890/W896 finding (which addressed the intra-process TOCTOU between archive-write and active-store-delete). That fix added rollback logic but did not add cross-process locking to the archive file itself.

**Note:** The two-binary drift scenario (launchd `native/runtime/KrabEarAgent` vs Dock `Krab Ear.app`) is known infrastructure (MEMORY.md). Both paths spawn the same Python backend pointing at the same `data_dir`, making this a plausible concurrent-write scenario in production.

**Fix direction:** Add `fcntl.flock(fh.fileno(), fcntl.LOCK_EX)` inside `_append_ndjson` and `_rewrite_archive`, mirroring the `StateStore._lock()` pattern. A dedicated `archive.lock` file (parallel to `history.lock`) is the cleanest approach.

---

### F3 — LOW: `unarchive_items` restores to active store but does not re-index in semantic search

**File:** `KrabEar/backend/archive_manager.py:162–174` (unarchive_items restore loop), `KrabEar/backend/service.py:709–722` (semantic_search_reindex handler)

**Description:**  
The symmetric counterpart to F1. When `unarchive_items` calls `_store.add_history_item(...)` to restore a record to the active store, `SemanticSearcher.index_item(item_id, text)` is never called. The restored item is invisible to semantic search until the user explicitly calls `semantic_search_reindex` (a manual, full rebuild operation).

This gap is compounded by the W1047 metadata loss bug (PR #968, unmerged): the current `add_history_item` call drops the original `id` and generates a new UUID — making even a manual `semantic_search_reindex` index the wrong ID. Both issues need to land together (W1047 + F3 fix) to fully close the semantic search round-trip.

**Fix direction:** After `_store.add_history_item(...)` succeeds, call `self._semantic_searcher.index_item(restored_id, text)` if a `SemanticSearcher` reference is available. The `ArchiveManager` constructor currently takes only `store`; it would need an optional `semantic_searcher` parameter or the call should be done at the `service.py` dispatch layer.

---

### F4 — LOW: `archive_items` holds `_lock` across O(N) `store.get_history_item_by_id` round-trips

**File:** `KrabEar/backend/archive_manager.py:111–125` (archive_items inner loop)

**Description:**  
`archive_items` acquires `self._lock` once at line 111 and holds it across the entire loop over `item_ids`. Each iteration calls `_store.get_history_item_by_id(clean_id)` which internally acquires the StateStore's `fcntl.flock` lock (an exclusive file lock), then calls `_store.delete_history_item(clean_id)` which acquires it again. For a batch of N items, the outer `threading.Lock` is held for the entire duration of 2N file-lock acquisitions.

During this time any other IPC thread trying to call `list_archived`, `get_archive_stats`, or `unarchive_items` blocks on the `threading.Lock`. On large batches (e.g., a 200-item `cleanup_old_history` feeding into `archive_items`), this can stall IPC for several seconds on a slow disk.

This finding was documented in W1038 (F3 LOW). It remains unaddressed in the current code.

**Fix direction:** Release the threading.Lock between items (process one item per lock acquisition) or batch-fetch all items first, then append to archive outside the lock, and finally delete from active store under a single lock per item.

---

### F5 — LOW: No test coverage for semantic search stale-index after archive/unarchive

**File:** `KrabEar/tests/test_archive_manager.py`

**Description:**  
The existing 37 tests (pre-W1047) cover: basic archive/unarchive round-trips, persistence, concurrency, corrupted-file recovery, IPC handlers, and Unicode. None test the interaction with a semantic searcher.

Specifically missing:
- Test that `archive_items` calls `semantic_searcher.remove_item` (F1)
- Test that `unarchive_items` calls `semantic_searcher.index_item` on restore (F3)
- Test that archived item IDs do not appear in mock search results after archiving

The `FakeStore` in the test file has no `semantic_searcher` concept, and the `ArchiveManager` constructor takes no searcher parameter — so these tests cannot be added without a design change (F1/F3 fix). Coverage gap is a downstream symptom of F1 and F3 being unimplemented.

---

## Summary Table

| ID | Severity | Short Description | Prior wave? | Requires code change? |
|----|----------|-------------------|-------------|----------------------|
| F1 | MEDIUM | `archive_items` does not call `semantic_searcher.remove_item` — stale embeddings resurface archived items in semantic search results | New | Yes |
| F2 | MEDIUM | `_append_ndjson`/`_rewrite_archive` lack cross-process `fcntl.flock` — two concurrent backend processes can corrupt `archive.ndjson` | New | Yes |
| F3 | LOW | `unarchive_items` does not re-index restored item in semantic search | New | Yes |
| F4 | LOW | `archive_items` holds `threading.Lock` across O(N) file-lock acquisitions | W1038 F3 (unmerged) | Yes |
| F5 | LOW | No test coverage for semantic-search interaction after archive/unarchive | New | Yes (requires F1/F3 first) |

**Total new findings: 5**  
(F4 was first identified in W1038 F3 but W1038 PR #955 is unmerged, so it is still a live residual.)

---

## What is correct

- `_rewrite_archive` uses `tmp.replace()` atomic rename — safe against crash mid-rewrite within a single process.
- `_append_ndjson` uses append mode — no truncation risk from concurrent appenders (data is not corrupted in the append case; lines may interleave but each line is written by a single `write()` call that is atomic for small payloads on APFS).
- In-process `threading.Lock` prevents Python-thread interleaving on the archive file within one process.
- IPC handlers validate input types (`item_ids` must be list).
- Corrupted JSON lines are skipped without exception (graceful degradation in `_read_archive`).
