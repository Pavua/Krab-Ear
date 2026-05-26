# Audit: RecordingMerger — W1266

**Date:** 2026-05-26
**Branch:** audit/recording-merger-W1266
**File audited:** `KrabEar/backend/recording_merger.py`
**Test file:** `KrabEar/tests/test_recording_merger.py`

## Summary

5 findings. 1 is a **critical production bug** (always-TypeError in the real store). 2 are medium-severity gaps (non-atomic delete phase, stale semantic index). 2 are low-severity design gaps (diarization timestamp offset, no privacy_mode gate).

---

## Findings

### F1 — CRITICAL: `tags=` kwarg passed to `StateStore.add_history_item` which does not accept it

**Severity:** critical
**Location:** `recording_merger.py:62` → `state_store.py:166`

`merge_items` calls `store.add_history_item(... tags=merged_data["tags"])`. The real `StateStore.add_history_item` has no `tags` parameter and no `**kwargs`. Python raises `TypeError: add_history_item() got an unexpected keyword argument 'tags'` unconditionally, so **every `merge_recordings` IPC call fails in production**.

The bug is hidden in tests: `FakeStore.add_history_item` (line 74 of test file) accepts `**kwargs`, silently absorbing `tags=`. All 40 test cases pass against the fake but would all fail against the real store.

**Fix:** Remove `tags=merged_data["tags"]` from the `store.add_history_item()` call inside `merge_items`. Tags are stored separately in `history_tags.ndjson` via `store.update_history_item_tags(new_item.id, merged_data["tags"])` — add a call to that method after the item is created. Also update `FakeStore` to not accept `**kwargs` so the test mock matches the real signature.

---

### F2 — MEDIUM: Non-atomic delete phase leaves partial state on partial failure

**Severity:** medium
**Location:** `recording_merger.py:66-76`

When `delete_originals=True`, originals are deleted one-by-one in a loop. If `store.delete_history_item()` raises an exception mid-loop (e.g., lock timeout, I/O error), the merged item already exists in the store but only some originals have been deleted. The undeleted originals and the merged item both live in history — duplicated content with no indication of which items have already been tombstoned.

There is no rollback or compensating delete of the already-created merged item. Since NDJSON is append-only, a true rollback is not possible, but the code should at minimum catch partial failure and surface it in the return dict (e.g., `partially_deleted_ids`) so callers can detect the inconsistency.

**Fix:** Wrap the delete loop in a try/except, collect both `deleted_ids` and `failed_ids`, always return both in the result dict, and log a warning when `len(failed_ids) > 0`.

---

### F3 — MEDIUM: Merged item not indexed in SemanticSearcher; deleted originals leave stale embeddings

**Severity:** medium
**Location:** `recording_merger.py:51-86` (no call to `_semantic_searcher`)

`merge_items` writes a new item to the history store but never calls `SemanticSearcher.index_item()` to index its text. The merged item is invisible to `semantic_search` queries until a manual `semantic_search_reindex` is triggered.

Conversely, when `delete_originals=True`, the deleted items' embeddings remain in the semantic index (no call to `SemanticSearcher.remove_item()`). These ghost embeddings return stale results for queries that match the now-deleted originals.

`SemanticSearcher.remove_item()` exists and is thread-safe (`backend/semantic_search.py:211`). The merger has no access to `_semantic_searcher` because it is owned by `BackendService`, not passed into the merger. The integration must be done at the `BackendService` level in `service.py` (similar to how indexing is done after recording in other flows), not inside the merger itself.

**Fix:** In `BackendService`, after delegating to `self._merger.merge_items(...)`, call `self._semantic_searcher.index_item(new_id, merged_text)` and, if `delete_originals=True`, call `self._semantic_searcher.remove_item(iid)` for each deleted ID. Return the list of deleted IDs from `merge_items` to make this straightforward.

---

### F4 — LOW: Diarization `speaker_segments` timestamps are not offset; cross-recording segments overlap at `0.0`

**Severity:** low
**Location:** `recording_merger.py:246-262` (`_merge_diarization`)

When diarization from N recordings is merged, segments from each recording all have `start`/`end` values relative to the start of their own recording (typically `start=0.0` for the first segment). The merger concatenates them as-is via `merged_segments.extend(segs)`, so segments from recording 2 onward overlap timestamps with segments from recording 1.

For example: recording A has `[{speaker: "A", start: 0.0, end: 5.0}]` and recording B also has `[{speaker: "B", start: 0.0, end: 3.0}]`. The merged output is `[{start: 0.0, end: 5.0}, {start: 0.0, end: 3.0}]` — both starting at 0. Any downstream consumer that sorts or renders these by time will produce nonsense output.

The correct approach is to accumulate a running time offset equal to the sum of `audio_duration_sec` of all previous recordings, and add that offset to `start`/`end` of each segment from the N-th recording. This is only accurate when `audio_duration_sec` is known for all items; if it is `None` for some items the offset cannot be computed exactly.

**Fix:** In `_merge_diarization`, accept the items list (or a list of per-item durations) as a second argument and apply cumulative offset to `start`/`end` in each segment batch. Fall back to current flat merge (with a `"timestamps_absolute": false` flag in the result) when durations are unavailable.

---

### F5 — LOW: No `privacy_mode_enabled` gate; merge persists PII content unconditionally

**Severity:** low
**Location:** `recording_merger.py:29-87`

`privacy_mode_enabled` is a first-class setting in `DEFAULT_SETTINGS` (`core/config.py:987`). `TranslationService` checks it before logging translated text (lines 96, 201 in `translation_service.py`). The merger has no awareness of privacy mode.

When `privacy_mode_enabled=True`, calling `merge_recordings` still writes the merged transcript to `history.ndjson` without any guard. This is inconsistent with how translation respects the setting. In privacy mode the expected behaviour is that merging should either be blocked entirely (return an error) or — more usefully — be allowed but with the merged item written with the same privacy markers/flags that the originals carry.

No `PrivacyAuditLogger` entry is written for the merge event either, so there is no audit trail for merge operations involving potentially private transcripts.

**Fix:** In `handle_merge_recordings`, read `privacy_mode_enabled` from runtime settings (via `store`'s settings accessor or passed as a param). If true, either refuse the operation with a descriptive error, or log a `PrivacyAuditLogger` event and allow it. Consistency with `translation_service.py` is the guide.

---

## Test Coverage Assessment

40 test cases across 8 test classes. All scenarios exercise only `FakeStore`. Key gaps:

- No integration test against real `StateStore` — F1 (tags TypeError) is fully invisible.
- No test for the partial-delete failure scenario (F2).
- No test asserting that `SemanticSearcher` is updated or that ghost embeddings do not persist after `delete_originals=True` (F3).
- No test asserting that diarization timestamps are correctly offset across recordings (F4).
- The "concurrent merge" test (test_concurrent_merge_safe) exercises thread safety of `RecordingMerger` itself, but `FakeStore` is not thread-safe — the test does not validate real store thread safety.

## Version Cascade (W1254) and Chain Cascade (W1260)

Neither `TranscriptVersionManager` nor `RecordingChainManager` is notified by the merger. `TranscriptVersionManager` is not expected to be called on merge (a new item's version history starts empty — acceptable). `RecordingChainManager` has no auto-unlink when items are deleted; if a merged original was part of a chain and is deleted with `delete_originals=True`, its `item_id` persists as a dangling reference inside the chain's `item_ids` list. This is a shared concern with any delete path and pre-dates this module.
