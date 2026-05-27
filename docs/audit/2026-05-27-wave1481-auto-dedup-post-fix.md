# W1481 Post-fix Audit: AutoDeduplicator

**Date:** 2026-05-27
**Auditor:** W1481 sub-agent
**File:** `KrabEar/backend/auto_deduplication.py`
**Branch base:** `codex/krab-ear-v2` (HEAD `50901466` after fetch — W1245 is the latest merge)

---

## Wave merge state

| Wave | PR | Title | State |
|------|----|-------|-------|
| W1243 | #1143 | docs: audit AutoDeduplicator — 5 findings | MERGED (docs only) |
| W1245 | #1150 | Jaccard hybrid + `_check_lock` for race (F1+F5 MED) | **MERGED** ✓ |
| W1247 | — | Wire into recording-completion flow (F3 HIGH) | branch `wire-auto-dedup-W1247`, **NOT merged** |
| W1248 | #1154 | privacy_mode skip + settings_provider injection (F4 MED) | **MERGED** ✓ |
| W1249 | — | Scan cap + background offload + progress IPC (F2 HIGH) | branch `fix-dedup-scan-cap-W1249`, **NOT merged** |
| W1406 | #1306 | re-audit doc — 5 residual findings | **MERGED** (docs) |
| W1412 | — | settings_provider + semantic_searcher inject (W1406 N1+N2) | branch `fix-autodedup-settings-provider-W1412`, **NOT merged** |

### Summary
- W1245: MERGED — Jaccard hybrid `_text_similarity`, `_check_lock` serialization. Present in HEAD.
- W1247: NOT merged — deduplicator still dead at recording-completion path.
- W1248: MERGED — privacy gate + `settings_provider` param in `AutoDeduplicator.__init__`.
- W1249: NOT merged — `run_deduplication` still O(n²), no scan cap.
- W1406 N1+N2 (= W1412): NOT merged — `settings_provider=None` in service.py, `_semantic_searcher` not injected.

---

## New findings (capped at 5)

### N1 — CRIT: `settings_provider=None` still in production — W1412 (W1406 N1) NOT merged

**Severity:** CRIT
**File:** `KrabEar/backend/service.py:523`

W1412 was created to fix the W1406 N1 gap but is not merged. The production instantiation remains:

```python
# service.py line 523 — settings_provider NOT passed
self._auto_deduplicator = AutoDeduplicator()
```

`_privacy_mode_enabled()` will always return `False` regardless of the runtime `privacy_mode_enabled`
setting because `self._settings_provider is None` (the guard short-circuits to `False`). Users who
enable privacy mode expect deduplication to stop loading transcript text from the store; it never does.

All other services that accept `settings_provider` (e.g. `MetadataEnricher`, `AutoGlossaryBuilder`,
`ObsidianSyncManager`) correctly receive `self._cached_settings` or `self._get_runtime_setting`.

**Fix:** Change `service.py:523` to:
```python
self._auto_deduplicator = AutoDeduplicator(
    settings_provider=self._get_runtime_setting,
)
```
Then merge PR for W1412.

---

### N2 — HIGH: `_semantic_searcher` not injected in `_handle_run_deduplication` — W1412 (W1406 N2) NOT merged

**Severity:** HIGH
**File:** `KrabEar/backend/service.py` (`_handle_run_deduplication`)

W1248 added the stale-embedding cleanup loop to `handle_run_deduplication`. The loop reads
`params.get("_semantic_searcher")` and calls `remove_item(dup_id)` for each duplicate found.
W1412 was meant to wire it in, but is unmerged. Current code:

```python
def _handle_run_deduplication(self, params):
    params["_store"] = self.store
    # _semantic_searcher NOT injected — cleanup loop unreachable
    return self._auto_deduplicator.handle_run_deduplication(params)
```

`self._semantic_searcher` is available on `BackendService` (initialised at line 559, used at
lines 606, 650). The stale-embedding fix is code-complete in `auto_deduplication.py` but
silently dead in every `run_deduplication` IPC call.

**Fix:** Add before delegation:
```python
params["_semantic_searcher"] = self._semantic_searcher
```

---

### N3 — HIGH: W1245 Jaccard rewrite dropped 60-second time-window filter from `check_duplicate`

**Severity:** HIGH
**File:** `KrabEar/backend/auto_deduplication.py:211–219`

W1245 replaced the `DuplicateDetector.find_duplicates` delegation with a direct iteration loop
using `_text_similarity`. The old delegation enforced a 60-second timestamp window via
`DuplicateDetector.DEFAULT_TIME_WINDOW_SECONDS`. The new loop has **no time filter**:

```python
for item in items:
    candidate_text = str(item.get("text") or ...).strip()
    if not candidate_text:
        continue
    sim = _text_similarity(text, candidate_text)   # no timestamp check
    if sim >= threshold and sim > best_similarity:
        ...
```

The `timestamp` parameter is accepted by `check_duplicate` but never used. Consequence: a
transcription made today can be flagged as a duplicate of a recording made weeks ago, as long
as the text is similar (≥0.9). This produces false-positive `is_duplicate=True` with
`action="skipped"`, silently discarding a recording.

Proved by the existing test `test_skip_old_items_outside_window` which **fails in CI**:
the test creates a store item with `ts="2020-01-01T00:00:00+00:00"` and expects the new
recording with a current timestamp not to be flagged as a duplicate — but it is.

**Fix:** In the `check_duplicate` loop, parse the candidate item's timestamp and skip items
outside 60 seconds from the new recording's `timestamp`. Mirror `DuplicateDetector._get_timestamp`.

---

### N4 — HIGH: IPC integration tests fail with `AttributeError: '_feature_flags'` — BackendService init order bug

**Severity:** HIGH
**File:** `KrabEar/backend/service.py:315`

`BackendService.__init__` wires `_feature_flags` into `_llm_rewriter` at line 315:

```python
self._llm_rewriter._feature_flags = self._feature_flags
```

But `self._feature_flags = FeatureFlags(...)` is only assigned at line 539 — **224 lines later**.
Any code that reaches line 315 will raise `AttributeError: 'BackendService' object has no
attribute '_feature_flags'`.

This causes all 6 `AutoDedupIPCTestCase` tests to fail because they instantiate `BackendService`
directly (without mocking `_llm_rewriter`). The `AttributeError` fires in `__init__` before
any dedup logic runs.

Reproduced: `pytest KrabEar/tests/test_auto_deduplication.py -k IPC` → 6 FAILED,
all with `AttributeError: 'BackendService' object has no attribute '_feature_flags'`.

This is a **pre-existing regression** introduced when W1245's init-order merged before W979's
`_feature_flags` block was relocated. It blocks all `BackendService`-backed integration tests
for the dedup module.

**Fix:** Move the `self._llm_rewriter._feature_flags = self._feature_flags` assignment to
AFTER `self._feature_flags = FeatureFlags(...)` (line 539+), or initialise `_feature_flags`
earlier in `__init__`.

---

### N5 — HIGH: `run_deduplication` still O(n²) — W1249 NOT merged, no scan cap

**Severity:** HIGH
**File:** `KrabEar/backend/auto_deduplication.py:281–300`

W1249 (branch `fix-dedup-scan-cap-W1249`) is not merged. `run_deduplication` currently pages
through the **entire history** with no upper bound:

```python
while True:
    page, next_cursor = store.get_history_page(cursor=cursor, limit=200)
    all_items.extend(page)
    ...
```

All collected items are then passed to `DuplicateDetector.find_duplicates` which is O(n²)
in items sharing a 60-second window. This is a synchronous IPC call that blocks the GIL
and all other IPC requests for its duration. On a 10k-item store: ~50 million comparisons.

**Fix:** Merge branch `fix-dedup-scan-cap-W1249` (adds `_MAX_DEDUP_SCAN=1000`, async
background offload, and a `dedup_progress` polling handler).

---

## Coverage summary

| Concern | Coverage | Gap |
|---------|----------|-----|
| Jaccard hybrid unit tests | Good (11 tests in W1245) | None — all pass |
| Privacy gate unit (settings_provider=mock) | Good (19 tests in W1248) | None — all pass |
| 60-second time-window in `check_duplicate` | 1 test — **FAILS** | N3: window lost in W1245 rewrite |
| `settings_provider` wired in service.py | None | N1 CRIT (W1412 unmerged) |
| `_semantic_searcher` injected in IPC handler | None | N2 HIGH (W1412 unmerged) |
| IPC integration tests pass | **6 FAIL** | N4: `_feature_flags` AttributeError |
| Scan cap on `run_deduplication` | None | N5 HIGH (W1249 unmerged) |
| Wire into recording-completion path | None | W1247 unmerged |

---

## Action items

| # | Severity | Action | Blocks |
|---|----------|--------|--------|
| N1 | CRIT | Wire `settings_provider=self._get_runtime_setting` at `service.py:523`; merge W1412 | Privacy compliance |
| N2 | HIGH | Inject `_semantic_searcher` in `_handle_run_deduplication`; merge W1412 | Stale embeddings |
| N3 | HIGH | Restore 60-second timestamp filter in `check_duplicate` direct-iteration loop | False-positive skips |
| N4 | HIGH | Fix `_feature_flags` init-order in `BackendService.__init__` | CI / IPC test failures |
| N5 | HIGH | Merge W1249 (scan cap + async) | Perf / IPC stall on large stores |
