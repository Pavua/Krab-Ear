# W1406 Re-audit: AutoDeduplicator residual findings

**Date:** 2026-05-27  
**Auditor:** W1406 sub-agent  
**File:** `KrabEar/backend/auto_deduplication.py`  
**Branch base:** `codex/krab-ear-v2` (HEAD `8bc868bd`)

---

## Wave merge state (W1243 / W1247–W1250)

| Wave | PR | Title | State |
|------|----|-------|-------|
| W1243 | #1143 | docs: audit AutoDeduplicator — 5 findings | OPEN (docs only, expected) |
| W1245 | — | Jaccard hybrid + check_lock for race (F1+F5 MED) | branch `fix-dedup-algorithm-race-W1245`, **NOT merged** |
| W1247 | #1151 | Wire into recording-completion flow (F3 HIGH) | **OPEN / NOT merged** |
| W1248 | #1154 | privacy_mode skip + settings_provider injection (F4 MED) | **MERGED** (2026-05-27T01:39:50Z) |
| W1249 | #1155 | Scan cap + background offload + progress IPC (F2 HIGH) | **OPEN / NOT merged** |

### Summary
- W1243: audit doc only (OPEN expected)
- W1245: code fix NOT merged — Jaccard hybrid algorithm not shipped
- W1247: wire into recording-completion NOT merged — deduplicator still never called at persist time
- W1248: MERGED — `_PRIVACY_SKIPPED` sentinel + `settings_provider` injected into `AutoDeduplicator`
- W1249: scan cap NOT merged — `run_deduplication` still loads unlimited history O(n²)

---

## New findings (capped at 5)

### N1 — CRIT: `settings_provider=None` in production — W1248 privacy gate is dead

**Severity:** CRIT  
**File:** `KrabEar/backend/service.py:504`

W1248 added `settings_provider` support to `AutoDeduplicator.__init__`, and the merged code in
`auto_deduplication.py` correctly reads `privacy_mode_enabled` through it. However `service.py`
instantiates the deduplicator without providing the provider:

```python
# service.py line 504 — MISSING settings_provider
self._auto_deduplicator = AutoDeduplicator()
```

Every other collaborator that has a `settings_provider` parameter (e.g. `MetadataEnricher` at
line 499-501) correctly passes `settings_provider=self._cached_settings`. `AutoDeduplicator` is
the sole exception. Result: `_privacy_mode_enabled()` always returns `False` regardless of the
user's privacy setting, exposing transcription texts to in-memory comparison in privacy mode.

**Fix:** Add `settings_provider=self._get_runtime_setting` to the `AutoDeduplicator()` call at
`service.py:504`.

---

### N2 — HIGH: `_handle_run_deduplication` drops `_semantic_searcher` — stale-embedding fix is dead

**Severity:** HIGH  
**File:** `KrabEar/backend/service.py:2156–2166`

W1248 adds semantic-searcher cleanup to `handle_run_deduplication` (calls
`semantic_searcher.remove_item(dup_id)` for each duplicate found). The parameter is read from
`params.get("_semantic_searcher")`. But `service.py._handle_run_deduplication` never injects
`_semantic_searcher` into `params`:

```python
def _handle_run_deduplication(self, params):
    params["_store"] = self.store
    # _semantic_searcher NOT injected — remove_item loop is unreachable
    return self._auto_deduplicator.handle_run_deduplication(params)
```

`self._semantic_searcher` exists in `BackendService` and is already used elsewhere. The stale-
embedding fix in W1248 is shipped in `auto_deduplication.py` but silently dead in production.

**Fix:** Add `params["_semantic_searcher"] = self._semantic_searcher` before the delegation call.

---

### N3 — HIGH: `run_deduplication` still O(n²) on 10k+ history — W1249 not merged

**Severity:** HIGH  
**File:** `KrabEar/backend/auto_deduplication.py:246–261`

W1249 (PR #1155, OPEN) was the fix for the O(n²) scan cap. Without it `run_deduplication` pages
through the entire history (`limit=200` per page, unlimited pages) and passes all items to
`DuplicateDetector.find_duplicates`. The inner loop is O(n²) in the number of items that share
a 60-second timestamp window. Worst-case on a 10k-item store with 1 recording/second bursts:
~49.9 million comparisons (~500 s wall clock). Even at 1 recording/minute the call blocks the
IPC event loop for ~6 s (synchronous, holds the GIL).

The 60-second window mitigates this for the normal case but does not cap total load time.  
`run_deduplication` is called from a synchronous IPC handler — it blocks all other IPC requests
for its duration.

**Fix:** Merge PR #1155 (scan cap `_MAX_DEDUP_SCAN=1000` + async offload via daemon thread).

---

### N4 — MED: `check_duplicate` counter increment not under lock on privacy-skipped path

**Severity:** MED  
**File:** `KrabEar/backend/auto_deduplication.py:122–136`

The privacy gate (W1248) returns early before `self._total_checked` is incremented. This is
correct behavior (privacy skip should not count as a check). However the counter increment on
the normal path (line 135–136) occurs OUTSIDE the store-read critical section but INSIDE a
`with self._lock` block that is distinct from the duplicate-found block at line 182–185. This
means concurrent calls can read the store concurrently and both find a match, then both
increment `_duplicates_found`, producing double-counting.

Concretely: two threads T1 and T2 call `check_duplicate` with the same new text. Both read the
same last-50 items. Both find the same duplicate. Both execute `with self._lock: self._duplicates_found += 1`. The net result is `_duplicates_found += 2` for one logical duplicate.

In practice the window is 60 seconds and `check_duplicate` is called from the recording
completion path (serialised by the audio pipeline), so concurrent duplicate discoveries are rare.
But the counter is used to report `dedup_rate` via IPC so systematic double-counting is
observable if bulk import (W1044) uses parallel threads.

**Fix:** Use a single `with self._lock` block that covers the full detect-and-increment logic,
or use atomic compare-and-swap semantics on the counter.

---

### N5 — MED: No test covers `settings_provider` injection path through `service.py` IPC

**Severity:** MED  
**File:** `KrabEar/tests/test_auto_deduplication.py` + `KrabEar/tests/test_auto_dedup_privacy_W1248.py`

The W1248 test file (`test_auto_dedup_privacy_W1248.py`) tests `AutoDeduplicator` in isolation
with a manually supplied `settings_provider`. No test exercises the end-to-end path:

```
BackendService.handle_request("run_deduplication") →
  _handle_run_deduplication →
    auto_deduplicator.handle_run_deduplication →
      _privacy_mode_enabled() via settings_provider
```

Because `service.py` passes `settings_provider=None` (N1), any integration test that sets
`privacy_mode_enabled=True` via `set_settings` and then calls `run_deduplication` via IPC would
prove that the privacy gate is bypassed. No such test exists. The gap means N1 went undetected
through code review and CI.

**Fix:** Add an integration test in `test_auto_deduplication.py` (`AutoDedupIPCTestCase`) that:
1. Creates `BackendService` and enables `privacy_mode_enabled` via `set_settings`.
2. Calls `run_deduplication` via `handle_request`.
3. Asserts `result["skipped_reason"] == "privacy_mode"`.

---

## Coverage summary (post-W1248 merge)

| Concern | Coverage | Gap |
|---------|----------|-----|
| Isolation unit (DedupResult, check_duplicate, run_dedup, stats) | Good (29 tests baseline + 19 W1248 privacy tests) | None |
| Privacy gate unit (settings_provider=mock) | Good (W1248 tests) | Yes — IPC integration missing (N5) |
| settings_provider wired in service.py | None | N1 CRIT |
| _semantic_searcher injected in IPC handler | None | N2 HIGH |
| Scan cap / perf on 10k history | None | N3 HIGH (W1249 unmerged) |
| Concurrent double-count on bulk import | None | N4 MED |

---

## Action items

| # | Severity | Action | Blocks |
|---|----------|--------|--------|
| N1 | CRIT | Wire `settings_provider=self._get_runtime_setting` at `service.py:504` | Privacy compliance |
| N2 | HIGH | Inject `_semantic_searcher` in `_handle_run_deduplication` | Stale embeddings |
| N3 | HIGH | Merge PR #1155 (W1249) | Perf on large stores |
| N4 | MED | Single-lock dedup+count; or document exclusion from bulk-import path | Stats accuracy |
| N5 | MED | Add IPC integration test for privacy gate | Regression detection |
