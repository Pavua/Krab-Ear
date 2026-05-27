# Wave 1278 — RecordingChainManager Third-Pass Audit

**Date:** 2026-05-26
**File audited:** `KrabEar/backend/recording_chain.py` (and cross-cutting: `history_service.py`, `archive_manager.py`, `state_store.py`, `recording_merger.py`)
**Auditor:** W1278 (sub-agent, read-only)
**Base commit:** `62df2ec9` (origin/codex/krab-ear-v2, latest as of 2026-05-26)
**Prior waves checked:** W877, W883, W1039 (audits); W900 (TOCTOU fix); W1046 (limit guard + privacy cascade); W1253 (second-pass audit); W1260 (ghost cleanup); W1259 (version cascade)

---

## Merge State Verification

| Wave | PR/Branch | Merged into `origin/codex/krab-ear-v2`? |
|------|-----------|------------------------------------------|
| W900 (TOCTOU fix) | `origin/feature/fix-recording-chain-toctou-W900` (#818) | **YES** — commit `70e3cb7a` present |
| W1046 (limit guard + privacy cascade) | `origin/fix-recording-chain-W1046` | **NO — OPEN** |
| W1253 (audit doc) | `origin/audit-recording-chain-residual-W1253` | NOT merged (doc only) |
| W1260 (ghost item_ids cascade) | `origin/fix-ghost-chain-itemids-W1260` | **NO — OPEN** |
| W1259 (version cascade in archive/merger/compact) | `origin/fix-version-cascade-W1259` | **NO — OPEN** |

**Note on W1253 audit accuracy:** The W1253 audit document stated W900 was merged and W1046 was not. This was correct for the local `codex/krab-ear-v2` branch at the time but the audit was performed against a stale local copy that was 165 commits behind `origin/codex/krab-ear-v2`. W900 (#818) is confirmed merged into the remote branch. This audit uses `origin/codex/krab-ear-v2` at `62df2ec9` as the authoritative base.

---

## Scope of This Audit (NEW findings only)

W1253 already documented RC-1 through RC-5. This audit focuses exclusively on NEW findings not previously identified, specifically:
- Coverage gaps in the W1260 ghost cascade fix (not yet merged)
- New TOCTOU in `list_chains()` not addressed by W900
- Interaction between W1259 version cascade and missing chain cascade at compaction
- Test coverage gaps introduced by the new paths

---

## Findings

### RC-A [MEDIUM] — W1260 ghost cascade misses `RecordingMerger.merge_items(delete_originals=True)`

**File:** `KrabEar/backend/recording_merger.py`, `KrabEar/backend/recording_chain.py`

The W1260 fix (`fix-ghost-chain-itemids-W1260`) wires `remove_item_from_all_chains()` into three deletion paths: `HistoryService.handle_delete_history_item`, `HistoryService.handle_cleanup_old_history`, and `ArchiveManager.archive_items`. It does NOT wire the cascade into `RecordingMerger.merge_items(delete_originals=True)`.

When `merge_items` is called with `delete_originals=True`:
1. N original history items are tombstoned via `store.delete_history_item()` (lines 66–70 of `recording_merger.py`).
2. A new merged item is created with a new ID.
3. If any of the N original items were members of a recording chain, their `item_ids` entries become ghost references pointing to tombstoned items.
4. The new merged item is NOT added to any of those chains.

This is both a ghost accumulation issue AND a semantic correctness issue: recording chains are the primary use-case for multi-part recordings, and merging parts of a chain without updating the chain breaks the chain's integrity silently. `merge_chain_text()` called after merging would return empty text for ghost stubs, silently losing content.

**Reproduce:**
```python
chain_id = mgr.start_chain("Interview")
mgr.add_to_chain(chain_id, "item-A")
mgr.add_to_chain(chain_id, "item-B")
# Merge items A+B with delete_originals=True
merger = RecordingMerger()
merger.merge_items(["item-A", "item-B"], store, delete_originals=True)
# Chain still has ["item-A", "item-B"] as item_ids but both are tombstoned
data = mgr.get_chain(chain_id)
# items = [{"id": "item-A"}, {"id": "item-B"}]  (ghost stubs)
# total_word_count = 0, total_duration_sec = 0.0  (silently wrong)
```

**Recommended fix:** When W1260 merges, extend the late-injection pattern to `RecordingMerger`: accept `_recording_chain_mgr` attribute; in `merge_items` when `delete_originals=True`, call `remove_item_from_all_chains()` for each deleted original ID after deletion.

---

### RC-B [MEDIUM] — `list_chains()` shallow-copy TOCTOU not fixed by W900

**File:** `KrabEar/backend/recording_chain.py` lines 186–201

The W900 TOCTOU fix (commit `70e3cb7a`) snapshots chain metadata inside the lock in `get_chain()`. However, `list_chains()` at line 188–189 acquires the lock only to copy the **list of chain dict references**:

```python
with self._lock:
    chains = list(self._data["chains"].values())  # shallow copy — chain dicts are still shared references
```

After the lock is released, the loop at lines 192–200 reads `c["name"]`, `c["created_at"]`, `c.get("ended_at")`, and `c.get("item_ids", [])` from those references without any lock protection. If a concurrent call to `end_chain()` sets `chain["ended_at"]` between the lock release and the `c.get("ended_at")` read, the returned summary contains stale `ended_at = None` for a chain that is already ended.

Similarly, a concurrent `add_to_chain()` can mutate `chain["item_ids"]` while `len(c.get("item_ids", []))` computes `item_count`. Under the GIL, `list.append` and `len()` are individually atomic, but the snapshot could reflect an in-progress state that does not correspond to any consistent point in time.

This is a parallel issue to the TOCTOU W900 fixed in `get_chain()`, but `list_chains()` was not updated at the same time.

**Recommended fix:** Deep-copy each chain dict inside the lock, or snapshot the relevant fields (`name`, `created_at`, `ended_at`, `item_count`) while the lock is held, mirroring the W900 fix pattern applied to `get_chain()`.

---

### RC-C [LOW] — `StateStore._compact_unlocked` is not covered by W1260 ghost cascade; interaction gap with W1259

**File:** `KrabEar/backend/state_store.py` (`_compact_unlocked`), `KrabEar/backend/recording_chain.py`

`StateStore._compact_unlocked()` (line 846) compresses the history NDJSON by removing all tombstoned items. After compaction, the tombstoned items are permanently gone from the store. Any `item_ids` in recording chains that pointed to tombstoned items become ghost references after compaction, exactly as in other deletion paths.

The W1260 ghost cascade fix covers three deletion paths but explicitly omits compaction (the fix's commit message lists only `HistoryService.handle_delete_history_item`, `handle_cleanup_old_history`, and `ArchiveManager.archive_items`).

The W1259 version cascade fix (also unmerged) handles compaction correctly for transcript versions: it captures tombstoned IDs before clearing the tombstones file, then calls `purge_versions_for_item()` for each. An analogous pattern is needed for chain cleanup but is absent from both W1259 and W1260.

**Concrete scenario:** A user records a 10-part meeting, adds all parts to a chain, then runs `handle_compact_history`. After compaction, all 10 history items remain (they are not tombstoned). This is fine. But if the user then deletes items 1–3 and runs compaction again, items 1–3 are gone from the store. The chain still lists them as `item_ids`. `get_chain()` returns ghost stubs. The compaction does not notify the chain manager.

**Recommended fix:** When W1260 merges, extend the late-injection pattern to `StateStore` in parallel with W1259: before clearing tombstones in `_compact_unlocked`, collect the tombstoned IDs and call `remove_item_from_all_chains()` for each. This requires injecting `_recording_chain_mgr` into `StateStore` analogously to `_transcript_versioner`.

---

### RC-D [CRITICAL] — W1046 and W1260 both unmerged; three confirmed bugs in production

**Files:** `KrabEar/backend/recording_chain.py`, `KrabEar/backend/service.py`, `KrabEar/backend/history_service.py`

As of base commit `62df2ec9` (current `origin/codex/krab-ear-v2`), both W1046 and W1260 remain unmerged. This re-confirms three production bugs from prior audits:

1. **Negative `limit` in `list_chains()`** (W1046 F1): `list_chains(limit=-1)` returns `N-1` items via Python negative slice. An IPC caller sending `{"method": "list_chains", "params": {"limit": -1}}` receives all chains except the oldest. No guard at line 193.

2. **`recording_chains.json` not purged on privacy wipe** (W1046 F5): `_handle_clear_privacy_audit_log()` (line 1641 of `service.py`) does not call `delete_all_chains()` (which doesn't exist in the current codebase). A full privacy wipe leaves all recording chains intact.

3. **Ghost `item_ids` after any history deletion** (W1260): `remove_item_from_all_chains()` does not exist in the current codebase. All three deletion paths (`delete_history_item`, `cleanup_old_history`, `archive_items`) accumulate ghost references in chains silently.

**Required action:** Merge PR for W1046 and PR for W1260 before the next release.

---

### RC-E [LOW] — No test coverage for list_chains() concurrent access or merger-path ghost accumulation

**File:** `KrabEar/tests/test_recording_chain.py`

Current test coverage gaps identified for the new findings:

| Scenario | Covered? |
|----------|----------|
| `list_chains()` concurrent modification (RC-B TOCTOU) | No |
| Ghost `item_ids` after `RecordingMerger.merge_items(delete_originals=True)` (RC-A) | No |
| Ghost `item_ids` after `StateStore` compaction (RC-C) | No |
| `list_chains()` with negative limit (W1046, still open) | No |
| W1260 `remove_item_from_all_chains()` — compaction path | No |

The existing concurrent test (`test_concurrent_unlink_thread_safe`) exercises `unlink_recording_from_chain` only. No test exercises concurrent `end_chain` + `list_chains` or concurrent `add_to_chain` + `list_chains`.

**Recommended fix:** Add at minimum: (a) a threading test that calls `end_chain` and `list_chains` concurrently and asserts `ended_at` is never `None` after `end_chain` completes; (b) an integration test that merges items with `delete_originals=True` and asserts the chain contains no ghost stubs after cascade cleanup (to be written when RC-A fix is implemented).

---

## Summary Table

| ID | Severity | Description | Prior Wave | New in W1278? |
|----|----------|-------------|------------|---------------|
| RC-A | MEDIUM | W1260 ghost cascade misses `RecordingMerger.merge_items(delete_originals=True)` | — | YES |
| RC-B | MEDIUM | `list_chains()` shallow-copy TOCTOU; W900 only fixed `get_chain()` | — | YES |
| RC-C | LOW | `StateStore._compact_unlocked` not covered by W1260; interaction gap with W1259 | — | YES |
| RC-D | CRITICAL | W1046 + W1260 both unmerged; 3 confirmed production bugs | W1253 RC-1+RC-3 | Updated |
| RC-E | LOW | No tests for concurrent `list_chains()` or merger-path ghost accumulation | — | YES |

5 findings total: 4 new (RC-A, RC-B, RC-C, RC-E) + 1 updated merge-state report (RC-D).

---

## Prior W1253 Findings Status

| W1253 ID | Description | Status |
|----------|-------------|--------|
| RC-1 | W1046 fixes not merged | Still open (RC-D above) |
| RC-2 | No `delete_chain` IPC; unbounded accumulation | Still open |
| RC-3 | Ghost item_ids (3 paths) | W1260 open; RC-A+RC-C add 2 more missed paths |
| RC-4 | No name length cap; unguarded `int()` cast | Still open |
| RC-5 | `_save()` swallows persist failures | Still open |
