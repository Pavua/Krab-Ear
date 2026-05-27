# Audit W1449: history_service.py post-fix — W1431/W1432/W1433 merge state + residual findings

**Date:** 2026-05-27  
**Branch:** audit-history-service-post-fix-W1449 (off codex/krab-ear-v2)  
**File:** `KrabEar/backend/history_service.py` (2911 lines on W1431 branch)  
**Scope:** Verify W1431/W1432/W1433 merge state; find NEW residual issues post-fixes.

---

## Fix Merge State

| Wave | Description | PR | Status |
|------|-------------|----|--------|
| W1431 | `HistoryService` semantic_searcher inject + delete cascade | #1323 | **OPEN** — branch `fix-history-service-semantic-W1431`; branched off v2.0.5 base (commit `6c900317`), NOT off current `codex/krab-ear-v2` |
| W1432 | export handlers `output_dir` allowlist — module-level `_is_safe_export_dir` predicate | #1325 | **MERGED** into `codex/krab-ear-v2` (2026-05-27T03:30:16Z) |
| W1433 | SRT export contiguous sequence numbers | #1330 | **MERGED** into `codex/krab-ear-v2` (2026-05-27T03:40:56Z) |

**Critical note on W1431 merge gap:** W1431 branched from v2.0.5 (`6c900317`) which predates
W1176 (export path allowlist, commit `63d5e7c6`, PR #1084, merged 2026-05-26T20:48:26Z).
The W1431 branch therefore does NOT contain the W1176 `_resolve_export_dir` instance-method
guard for `handle_export_obsidian` and `handle_batch_export`. When W1431 merges onto current
`codex/krab-ear-v2` this will not be a problem (W1176 is already in the target), but the
branch-in-isolation is unguarded on those two handlers.

---

## New Residual Findings (capped at 5)

### F1 — MEDIUM: `archive_items` bypasses semantic index purge on delete

**Location:** `KrabEar/backend/archive_manager.py:169`

W1431 correctly adds `_semantic_searcher.remove_item(item_id)` to
`handle_delete_history_item` in `HistoryService`. However, `ArchiveManager.archive_items()`
calls `_store.delete_history_item(clean_id)` directly (bypassing `HistoryService`). Because
`ArchiveManager` has no reference to `SemanticSearcher`, archiving an item leaves its
embedding in the semantic index permanently.

Steps to reproduce:
1. Add item → semantic index populated via `add_to_index`.
2. Call `archive_items([item_id])` → store tombstone written, item removed from active history.
3. Call `semantic_search(query)` → archived item still appears in results.

**Fix:** `ArchiveManager.archive_items()` should accept an optional `semantic_searcher=`
kwarg and call `semantic_searcher.remove_item(clean_id)` after successful store delete,
mirroring the W1431 pattern. Alternatively, inject `HistoryService.handle_delete_history_item`
as the delete callable so the cascade runs through the single code path.

---

### F2 — MEDIUM: W1431 SRT fix (W1433) NOT on the open W1431 branch — merge will reintroduce gap bug

**Location:** `KrabEar/backend/history_service.py:767`

The W1431 branch (`fix-history-service-semantic-W1431`) branched from v2.0.5 — before W1433
was committed. The current code at line 767 on that branch still uses:

```python
for seq, turn in enumerate(turns, start=1):
    ...
    if not turn_text:
        continue
    srt_lines.append(str(seq))   # seq still reflects the enumerate counter, not written count
```

W1433 (merged into `codex/krab-ear-v2` via PR #1330) replaced this with a manual `idx`
counter. If W1431 is rebased/merged without resolving this conflict, the SRT gap bug will be
re-introduced on the merge.

**Risk classification:** LOW for `codex/krab-ear-v2` target (W1433 is already in the target
branch, so a clean merge/rebase will preserve it), but a manual `git merge` that takes
W1431's file wholesale would regress.

**Fix:** Before merging PR #1323, rebase W1431 onto `codex/krab-ear-v2` to pick up the
W1433 contiguous-sequence fix.

---

### F3 — LOW: `_is_safe_export_dir` (W1432) is a predicate but is NOT called by any export handler

**Location:** `KrabEar/backend/history_service.py` — module-level (W1432 addition)

W1432 adds `_EXPORT_ALLOWED_ROOTS` and `_is_safe_export_dir(out_dir)` as a module-level
predicate. The commit message states "W1176 already added `_resolve_export_dir`" as the
enforcement guard, but inspection of the W1431 branch reveals `_resolve_export_dir` is NOT
present there (it only exists on `codex/krab-ear-v2` after W1176). The predicate added by
W1432 is therefore not called from `handle_export_obsidian` or `handle_batch_export` on the
W1432 branch itself — it is a free function that tests can import but which has no production
call sites until the branch is merged onto a W1176-carrying base.

After merge onto `codex/krab-ear-v2`, `_resolve_export_dir` (W1176) enforces path safety and
`_is_safe_export_dir` (W1432) remains a testable module-level helper. The two are parallel,
not integrated. There is no single place that calls both.

**Recommendation (LOW):** Wire `_is_safe_export_dir` into `_resolve_export_dir` as a
delegating step (or consolidate) so there is one canonical enforcement path. Currently the
predicate is tested in isolation (`test_export_obsidian_allowlist_W1432.py`) but production
enforcement goes through `_resolve_export_dir` only.

---

### F4 — LOW: `segments` metadata in SRT response reports total turns, not written segments

**Location:** `KrabEar/backend/history_service.py:784-786`

```python
return self._finalize_srt_export(
    params, srt_content, item_id,
    speakers=len(speakers), segments=len(turns),   # <-- turns includes skipped empty turns
)
```

After the W1433 fix, the `idx` counter tracks only non-empty turns (the actual written
subtitle entries). However `segments=len(turns)` still passes the total diarization turn
count (including empty turns). If a recording has 10 turns with 3 empty, the response
reports `segments: 10` while the SRT file contains 7 entries. This misleads callers and any
analytics that rely on the `segments` field.

**Fix:** Pass `idx` (accumulated after the loop) as `segments` instead of `len(turns)`.
After W1433 is applied:
```python
idx = 0
for turn in turns:
    ...
    if not turn_text:
        continue
    idx += 1
    ...
srt_content = "\n".join(srt_lines)
return self._finalize_srt_export(
    params, srt_content, item_id,
    speakers=len(speakers), segments=idx,   # actual written count
)
```

---

### F5 — LOW: No test for archive-path semantic index leak (W1431 scope gap)

**Location:** `KrabEar/tests/` — coverage gap

W1431 ships 7 tests covering `HistoryService.handle_delete_history_item` + semantic cascade
(`test_history_service_semantic_W1431.py`). However, the `archive_items` path
(`ArchiveManager.archive_items` → `store.delete_history_item`) has no test asserting that
the semantic index is NOT left stale after archiving. This coverage gap means F1 above will
go undetected until a user observes archived items appearing in semantic search results.

**Fix:** Add a test in a new file (e.g. `test_archive_semantic_W1449.py`) that:
1. Constructs a `HistoryService` with a mock `SemanticSearcher`.
2. Injects the mock into an `ArchiveManager` (once F1 fix lands).
3. Calls `archive_items([item_id])`.
4. Asserts `mock_searcher.remove_item.assert_called_once_with(item_id)`.

---

## Summary

| Finding | Severity | Category |
|---------|----------|----------|
| F1 — `archive_items` bypasses semantic index purge | MEDIUM | Correctness / data staleness |
| F2 — W1431 branch misses W1433 SRT fix; rebase risk | MEDIUM | Merge hygiene |
| F3 — `_is_safe_export_dir` predicate has no call sites | LOW | Dead code / integration gap |
| F4 — `segments` metadata counts skipped empty turns | LOW | API response accuracy |
| F5 — No test for archive semantic index leak | LOW | Test coverage gap |

**Action required before merging PR #1323 (W1431):** Rebase onto `codex/krab-ear-v2` to
pick up W1432 + W1433 (already merged). W1176 (`_resolve_export_dir`) will also be picked
up, closing the export path guard gap present on the isolated branch.
