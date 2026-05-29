# W1567 Fourth-Pass Audit: AutoDeduplicator

**Date:** 2026-05-29
**Auditor:** W1567 sub-agent (fourth pass)
**Trigger:** post-W1537 (settings_provider inject + semantic_searcher inject) + W1540 (_text_similarity + _PRIVACY_SKIPPED restored) — both admin-merged
**Files audited:**
- `KrabEar/backend/auto_deduplication.py`
- `KrabEar/backend/recording_core_service.py`
- `KrabEar/backend/service.py`
- `KrabEar/backend/ipc_dispatch.py`
- `KrabEar/core/duplicate_detector.py`
- `KrabEar/tests/test_auto_dedup_wiring_W1247.py`
- `KrabEar/tests/test_auto_dedup_privacy_W1248.py`

---

## W1537 + W1540 merge verification

| Fix | Expected signature | Status |
|-----|--------------------|--------|
| W1537 `settings_provider` inject | `AutoDeduplicator(settings_provider=self._get_runtime_setting)` in `service.py:458` | **PRESENT** ✓ |
| W1537 `_semantic_searcher` inject | `params["_semantic_searcher"] = self._semantic_searcher` in `_handle_run_deduplication` | **PRESENT** ✓ |
| W1540 `_text_similarity` | `def _text_similarity(a, b)` at module level | **PRESENT** ✓ |
| W1540 `_PRIVACY_SKIPPED` sentinel | `_PRIVACY_SKIPPED = DedupResult(...)` at module level | **PRESENT** ✓ |
| W1540 `_check_lock` | `self._check_lock = threading.Lock()` in `__init__` | **PRESENT** ✓ |

All W1537 + W1540 signatures confirmed at HEAD `5e0bf3e6`.

---

## New findings (capped at 5)

### F1 — HIGH: `_text_similarity` (W1540 Jaccard) is dead code — `check_duplicate` still delegates to `DuplicateDetector`

**Severity:** HIGH
**File:** `KrabEar/backend/auto_deduplication.py`
**Status:** NEW (introduced by W1540 partial restore)

W1540 restored `_text_similarity` (lines 53–77), `_JACCARD_LOW`, `_JACCARD_HIGH`, and `_check_lock`. However, `check_duplicate()` (lines 136–255) still delegates entirely to `self._detector.find_duplicates()` — it never calls `_text_similarity`. The `DuplicateDetector.find_duplicates()` uses character-level `SequenceMatcher` (not Jaccard) internally (see `core/duplicate_detector.py:117`).

The W1245 design intent was that `check_duplicate()` would use a direct Jaccard loop with the 60-second temporal filter, replacing the `DuplicateDetector` delegation. W1540 restored the helper function but did not wire it into the actual comparison path.

**Consequence:** The false-positive mitigation for short Russian texts ("Привет как дела" vs "Привет как" giving ~0.91 SequenceMatcher ratio) described in W1245 is NOT active. `_check_lock` serialises concurrent `check_duplicate` calls (good), but the underlying comparison uses SequenceMatcher, not Jaccard blend.

**Evidence:** `grep -rn "_text_similarity" KrabEar/backend/auto_deduplication.py` returns only the function definition (line 53), zero call sites.

**Fix:** Wire `_text_similarity` into `check_duplicate()` as the primary comparison function for the history-scan loop, replacing the `self._detector.find_duplicates()` delegation. The loop should iterate `normalized_items`, parse timestamps, filter items outside 60 seconds, and call `_text_similarity(text, item_text)` directly.

---

### F2 — HIGH: W1247 recording-completion wiring still absent — `RecordingCoreService.__init__` lacks `auto_deduplicator` parameter

**Severity:** HIGH
**File:** `KrabEar/backend/recording_core_service.py`
**Status:** Carry-forward from W1527 R2 (unresolved)

`RecordingCoreService.__init__` (line 50) does not accept an `auto_deduplicator` keyword argument, has no `self._auto_deduplicator` attribute, and `_stop_recording_phase_e` (line 1106) makes no call to `check_duplicate`. The test `test_auto_dedup_wiring_W1247.py` passes `auto_deduplicator=dedup_mock` to the constructor (line 139) — this will raise `TypeError: __init__() got an unexpected keyword argument 'auto_deduplicator'` at runtime.

`service.py` creates `self._auto_deduplicator` at line 458 but never passes it to `RecordingCoreService` (constructed elsewhere in the `__init__` with no `auto_deduplicator=` argument).

**Consequence:** `AUTO_DEDUP_ENABLED` has no effect at record-time. Every recording is persisted without dedup check. The `check_duplicate` IPC method works as a standalone call, but the intended automatic suppression of near-identical recordings is completely inactive. The W1247 test suite will fail with `TypeError`.

**Fix (two files):**
1. `recording_core_service.py`: add `auto_deduplicator: Any = None` to `__init__`, set `self._auto_deduplicator = auto_deduplicator`, add dedup guard in `_stop_recording_phase_e` before `store.add_history_item`.
2. `service.py`: pass `auto_deduplicator=self._auto_deduplicator` when constructing `RecordingCoreService`.

---

### F3 — MED: `dedup_progress` IPC handler not registered in `ipc_dispatch.py`

**Severity:** MED
**File:** `KrabEar/backend/ipc_dispatch.py`
**Status:** NEW

`handle_dedup_progress` is implemented in `AutoDeduplicator` (lines 557–582) and tested in `test_dedup_scan_cap_W1249.py`. However, neither `ipc_dispatch.py` nor `service.py` registers a `"dedup_progress"` entry in the dispatch table.

`ipc_dispatch.py` currently registers only:
```
"check_duplicate": svc._handle_check_duplicate   (line 264)
"run_deduplication": svc._handle_run_deduplication  (service.py:1180)
"get_dedup_stats": svc._handle_get_dedup_stats   (service.py:1181)
```

The async job infrastructure (`run_deduplication_async` + `get_dedup_job`) is fully implemented but unreachable via IPC. Any caller that launches `run_deduplication` and tries to poll its status with `dedup_progress` will receive `{"ok": false, "error": "unknown method"}`.

**Fix:**
1. In `service.py` `_handle_check_duplicate` section, add:
   ```python
   def _handle_dedup_progress(self, params):
       return self._auto_deduplicator.handle_dedup_progress(params)
   ```
2. Add `"dedup_progress": self._handle_dedup_progress` to the dispatch table in both `service.py` and `ipc_dispatch.py`.

---

### F4 — MED: `run_deduplication` in `handle_run_deduplication` is synchronous — the async job path (`run_deduplication_async`) is never called from IPC

**Severity:** MED
**File:** `KrabEar/backend/auto_deduplication.py` (line 541) + `KrabEar/backend/service.py` (line 3892)

`handle_run_deduplication` calls `self.run_deduplication(store=store, threshold=threshold)` synchronously (line 541). The docstring at lines 524–526 says: "W1243 F2: запускает сканирование синхронно и возвращает полный результат. Для асинхронного режима используйте `run_deduplication_async`."

The problem: `run_deduplication` iterates up to 1000 history items with O(n²) pairwise SequenceMatcher comparisons. On a 1000-entry history, worst-case ~500k string comparisons block the IPC handler thread for multiple seconds. The W1243 F2 fix was supposed to make this async; `run_deduplication_async` and the job registry were implemented for exactly this purpose, but `handle_run_deduplication` was never updated to use them.

Combined with F3 (no `dedup_progress` IPC), the async path is doubly unreachable: not called from the handler, and not pollable even if it were.

**Fix:** Update `handle_run_deduplication` to call `self.run_deduplication_async(store, threshold)` and return `{"ok": True, "job_id": job_id}` immediately. Callers poll via `dedup_progress` (F3 fix required first).

---

### F5 — MED: `total_in_store` in `run_deduplication` response misreports real history size when capped

**Severity:** MED
**File:** `KrabEar/backend/auto_deduplication.py`, lines 298–321

The docstring for `run_deduplication` states `total_in_store: int — реальный размер истории (до ограничения)`. However, the implementation only accumulates `total_in_store += len(page)` for pages fetched before the `_MAX_DEDUP_SCAN=1000` cap is reached. When `capped=True`, `total_in_store` equals exactly 1000, not the actual store size. There is no additional pagination to count remaining records.

The comment at line 319–320 acknowledges this: "Упрощение: если страниц было меньше _MAX_DEDUP_SCAN — `total_in_store` == `len(all_items)`". However the docstring still promises the real count.

**Consequence:** UI consumers relying on `total_in_store` to display "X of Y items scanned" will show incorrect data for stores larger than 1000 entries. `capped=True` is the only signal that the count is wrong, but the magnitude of the real store is unknown.

**Fix (minimal):** Update the docstring to say `total_in_store` is the count of items fetched (capped at `_MAX_DEDUP_SCAN`), not the real store size. Rename to `items_fetched` in a future wave. Optional: do one additional `store.get_history_page(cursor=last_cursor, limit=1)` to detect whether more pages exist, then count via a fast path.

---

## Summary table

| Finding | Severity | File | Root cause |
|---------|----------|------|------------|
| F1: `_text_similarity` dead code | HIGH | `auto_deduplication.py` | W1540 restored function but did not replace `DuplicateDetector` delegation in `check_duplicate` |
| F2: W1247 recording-completion absent | HIGH | `recording_core_service.py` + `service.py` | Carry-forward from W1527 R2; neither `auto_deduplicator` kwarg nor dedup guard in `_stop_recording_phase_e` |
| F3: `dedup_progress` not in dispatch | MED | `ipc_dispatch.py` + `service.py` | Handler implemented in `AutoDeduplicator` but never registered in dispatch table |
| F4: `handle_run_deduplication` synchronous | MED | `auto_deduplication.py:541` | W1243 async infra implemented but `handle_run_deduplication` never updated to use `run_deduplication_async` |
| F5: `total_in_store` misreports when capped | MED | `auto_deduplication.py:314` | Acknowledged in code comment but docstring promises real count |

## Resolved since W1527

| Wave | Fix | Status |
|------|-----|--------|
| W1537 | `settings_provider=self._get_runtime_setting` in `service.py` | RESOLVED ✓ |
| W1537 | `_semantic_searcher` inject in `_handle_run_deduplication` | RESOLVED ✓ |
| W1540 | `_text_similarity`, `_PRIVACY_SKIPPED`, `_check_lock` restored | PARTIALLY resolved — function present but not called (F1) |

## Still open from W1527

| W1527 finding | Current status |
|---------------|---------------|
| R2: W1247 recording-completion wiring | Open → this report F2 |
| R5: W1488 `_llm_rewriter._feature_flags` absent | Open (out of scope for dedup audit, tracked in W1527) |
