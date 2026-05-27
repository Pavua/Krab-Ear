# W1407 Re-audit: RecordingMerger Residual Issues

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` (commit `6c900317` — v2.0.5)
**Auditor:** W1407 sub-agent (read-only)
**Scope:** `KrabEar/backend/recording_merger.py`, interactions with `auto_deduplication.py`, `recording_chain.py`, `semantic_search.py`, `state_store.py`

---

## Merge State Verification

Waves W1268, W1269, W1270, and W1282 were authored on downstream branches but are **NOT merged into `codex/krab-ear-v2`** as of this audit.

| Wave | PR | Description | Merged to codex/krab-ear-v2? |
|------|----|-------------|-------------------------------|
| W1268 | #1174 | TypeError fix — separate `tags` update via `update_history_item_tags` | **NO** |
| W1269 | (no PR #) | Atomic merge with rollback on delete failure | **NO** |
| W1270 | (no PR #) | Semantic search index+remove cascade after merge | **NO** |
| W1282 | #1189 | Chain cascade — replace originals with merged in chains | **NO** |

Evidence: `git branch --contains 397b5f6a` (W1282 commit) shows `fix-ci-red-W1401`, `audit-auto-glossary-fifth-W1402`, etc. — none of which is `codex/krab-ear-v2`. The fixes live on downstream branches only.

---

## Findings

### F1 — CRITICAL: W1268 TypeError is still present on codex/krab-ear-v2

**File:** `KrabEar/backend/recording_merger.py` line 63
**Severity:** CRITICAL (production crash on every `merge_recordings` call with tagged items)

`merge_items()` passes `tags=merged_data["tags"]` as a keyword argument to `store.add_history_item()`:

```python
new_item = store.add_history_item(
    ...
    confidence=merged_data["confidence"],
    tags=merged_data["tags"],   # <-- unexpected keyword argument
)
```

`StateStore.add_history_item()` signature (lines 166–187 of `state_store.py`) does **not** include a `tags` parameter. Python raises `TypeError: add_history_item() got an unexpected keyword argument 'tags'` at runtime for any call that reaches this path — including calls with empty tag lists, because `tags=[]` is still an unexpected kwarg.

The W1268 fix (on `fix-punctuation-es-per-sentence-W1393` and siblings) strips the `tags=` kwarg and calls `store.update_history_item_tags(new_item.id, merged_data["tags"])` separately. That call is absent on `codex/krab-ear-v2`.

**Reproduction:** Call `merge_recordings` IPC with any two history items. Error is unconditional.

---

### F2 — MED: No rollback if originals deletion is partial (W1269 not merged)

**File:** `KrabEar/backend/recording_merger.py` lines 66–76
**Severity:** MED (silent data inconsistency on I/O error)

When `delete_originals=True`, the deletion loop is:

```python
deleted_ids: list[str] = []
for item in items:
    if store.delete_history_item(item.id):
        deleted_ids.append(item.id)
```

There is no `try/except` wrapper. If `state_store._append_ndjson(tombstones_path, ...)` raises (e.g., disk full, permission error) mid-loop:
- The new merged item already exists in the NDJSON history store.
- Some originals are tombstoned, others are not.
- The caller receives an unhandled exception; the partially-merged state is unrecoverable without manual NDJSON surgery.

The W1269 fix wraps this in a try/except and rolls back the merged item via an additional tombstone if any deletion fails. That logic is absent here.

Note: `state_store.delete_history_item()` itself always returns `True` (it appends a tombstone unconditionally without checking existence), so the `if store.delete_history_item(...)` guard provides weaker protection than tests suggest. The `FakeStore` in tests returns `False` for non-existent IDs — a divergence from real `StateStore` behaviour.

---

### F3 — MED: Semantic search index stale after merge (W1270 not merged)

**File:** `KrabEar/backend/recording_merger.py` (no semantic calls present)
**Severity:** MED (stale search results; deleted originals still returned by semantic search)

After a merge with `delete_originals=True`:
1. Original items are tombstoned in NDJSON — `get_history_item_by_id` returns `None` for them.
2. The in-memory `SemanticSearcher` index (built at startup via `index_all`) still holds embeddings for the deleted original IDs.
3. `SemanticSearcher.search()` can return these ghost IDs; when the Swift UI tries to `get_history_item_by_id`, it gets `None` → silent empty result or crash depending on the caller.
4. The new merged item is never indexed — semantic search cannot find it until a full `semantic_search_reindex` is triggered.

`SemanticSearcher.remove_item()` exists (added in PR #506 / wave156 for `RecordingChain unlink`). The merger does not call it. The W1270 fix adds:
```python
if self._semantic_searcher is not None:
    for item in items:
        self._semantic_searcher.remove_item(item.id)
    self._semantic_searcher.index_item(new_item.id, merged_data["text"])
```
That injection and those calls are absent on `codex/krab-ear-v2`.

---

### F4 — MED: Ghost item_ids in chains after delete_originals=True (W1282 not merged)

**File:** `KrabEar/backend/recording_chain.py` (missing `find_chains_containing`, `replace_items_in_chain`)
**Severity:** MED (chains permanently corrupted after merge+delete)

`RecordingChainManager` stores ordered `item_ids` lists in `recording_chains.json`. When `merge_recordings` tombstones the originals, chains that referenced those IDs are not updated. `get_chain()` then fetches the deleted IDs via `get_history_item_by_id`, receives `None`, and appends `{"id": iid}` stub dicts to `items_detail` — breaking `total_duration_sec`, `total_word_count`, and any downstream chain text assembly.

The W1282 fix adds `find_chains_containing(original_ids) → {chain_id: [matched_ids]}` and `replace_items_in_chain(chain_id, old_ids, new_id)` to `RecordingChainManager`, then calls them from `merge_items`. Neither method exists on `codex/krab-ear-v2` (`grep -n "find_chains_containing" recording_chain.py` → empty).

---

### F5 — LOW: No idempotency guard — double merge creates duplicates

**File:** `KrabEar/backend/recording_merger.py` (no dedup guard in `merge_items`)
**Severity:** LOW (data quality; not a crash)

Calling `merge_recordings` twice with the same `item_ids` and `delete_originals=False` produces two distinct merged items with identical text. There is no check for an existing merged item covering the same source IDs. The `auto_deduplicator.run_deduplication()` will eventually flag the pair as duplicates (similarity 1.0), but:

- No automatic cleanup occurs.
- The `merged_from` field is not indexed or queried during merge initiation.
- The test suite has no idempotency test (search for "idempoten" in `test_recording_merger.py` → zero results).

A minimal guard would be to check if any existing history item already has `merged_from` containing the same set of IDs before writing a new one, or at minimum document the non-idempotent behaviour clearly in the docstring.

---

## Test Coverage Assessment

The existing test suite (`KrabEar/tests/test_recording_merger.py`, 638 lines, 40 test methods) covers:

- Basic merge mechanics, chronological sort, metadata aggregation ✓
- `delete_originals=True/False` ✓
- Diarization merging including `segments` fallback key ✓
- Translation field merging ✓
- Edge cases: empty tags, None confidence/duration, custom separator ✓
- Concurrency safety (`test_concurrent_merge_safe`) ✓

**Not covered:**

- TypeError from `tags=` kwarg (F1) — `FakeStore.add_history_item` accepts `**kwargs`, masking the production crash
- Rollback on partial delete failure (F2) — no test for I/O exception mid-loop
- Semantic search index state after merge (F3) — no `SemanticSearcher` stub in tests
- Chain integrity after delete (F4) — no `RecordingChainManager` stub in tests
- Idempotent merge behaviour (F5) — zero tests for repeat-call scenario

The `FakeStore.add_history_item` accepts `**kwargs` (line 87), which silently absorbs the `tags=` kwarg that crashes the real `StateStore`. This masks F1 entirely in CI.

---

## Summary Table

| ID | Severity | Root cause | W-fix | On codex/krab-ear-v2? |
|----|----------|------------|-------|------------------------|
| F1 | CRITICAL | `tags=` kwarg to `add_history_item` → TypeError | W1268 | NO |
| F2 | MED | No rollback on partial delete failure | W1269 | NO |
| F3 | MED | Semantic index not updated after merge | W1270 | NO |
| F4 | MED | Ghost item_ids in chains after delete | W1282 | NO |
| F5 | LOW | No idempotency guard; no test | (new) | NO |

**Total new findings: 5** (F1 is a regression reconfirm — the CRITICAL bug from W1266 is still active on main; F5 is a newly identified gap not addressed by any prior wave).
