# W1505 Third-Pass Audit: AutoDeduplicator

**Date:** 2026-05-27
**Auditor:** W1505 sub-agent (third pass)
**File:** `KrabEar/backend/auto_deduplication.py`
**Branch base:** `codex/krab-ear-v2` HEAD `94f0222f` (post-W1487 merge)

---

## Wave merge state

| Wave | PR | Title | State |
|------|----|-------|-------|
| W1245 | #1150 | Jaccard hybrid + `_check_lock` (W1243 F1+F5) | **MERGED** ✓ |
| W1247 | — | Wire into recording-completion path (F3 HIGH) | **NOT merged** |
| W1248 | #1154 | Privacy gate + `settings_provider` injection (F4 MED) | **MERGED** ✓ |
| W1249 | — | Scan cap + background offload (F2 HIGH) | **NOT merged** |
| W1481 | #1367 | Audit docs — 5 findings (N1 CRIT through N5) | **MERGED** (docs) |
| W1487 | — | Jaccard 60s time-window filter (W1481 N3 HIGH) | **MERGED** ✓ (commit `b32f8b3a`) |
| W1488 | — | `_feature_flags` init order in service.py (W1481 N4 HIGH) | **NOT merged** (PR #1373) |
| W1412 | — | `settings_provider` + `_semantic_searcher` inject (W1406 N1+N2 CRIT+HIGH) | **NOT merged** (PR #1315) |

### Summary

W1487 is confirmed merged at commit `b32f8b3a` — the `_DEDUP_WINDOW_SEC = 60` constant and
`_parse_ts()` helper are present in HEAD. `check_duplicate` now filters candidates outside the
60-second window. `test_skip_old_items_outside_window` was fixed.

W1488 (service.py `_feature_flags` init-order) and W1412 (`settings_provider` wiring +
`_semantic_searcher` injection) remain unmerged. Both W1481 N1 CRIT and N4 HIGH are still live.

---

## New findings (capped at 5)

### N1 — HIGH: `PrivacyModeGuardTestCase` uses zero-arg callable incompatible with W1248 interface

**Severity:** HIGH
**File:** `KrabEar/tests/test_auto_deduplication.py`, class `PrivacyModeGuardTestCase`
**Status:** NEW (not reported in W1481 or W1406)

W1248 changed `settings_provider` to `Callable[[str, Any], Any]` — a two-argument callable
`(key, default) → Any` matching `BackendService._get_runtime_setting`. The `_privacy_mode_enabled()`
helper calls:

```python
return bool(self._settings_provider("privacy_mode_enabled", False))
```

But `PrivacyModeGuardTestCase` (lines 397–500) still uses the old zero-arg interface:

```python
def privacy_settings() -> dict:
    return {"privacy_mode": True}

deduplicator = AutoDeduplicator(settings_provider=privacy_settings)
```

When the code calls `privacy_settings("privacy_mode_enabled", False)`, Python raises
`TypeError: privacy_settings() takes 0 positional arguments but 2 were given`.
The `except Exception` block in `_privacy_mode_enabled()` swallows it and returns `False`.
Result: privacy mode is never activated in these tests — the test proceeds to call
`store.get_history_page()` and returns a real `DedupResult`.

This causes two assertion failures per test:
1. `assertEqual(result.action_taken, "kept")` — actual is `"skipped"` or `"merged"`.
2. `mock_store.get_history_page.assert_not_called()` — it was called once.

Compound: even if the callable signature were fixed, the test expects `action_taken="kept"` but the
`_PRIVACY_SKIPPED` sentinel has `action_taken="privacy_skipped"`. That assertion would still fail.

**Affected tests** (4 tests currently failing silently or diverged):
- `test_auto_dedup_skips_in_privacy_mode`
- `test_auto_dedup_active_when_privacy_mode_disabled`
- `test_privacy_mode_no_settings_provider_runs_dedup`
- `test_privacy_mode_settings_provider_exception_safe`

**Fix:**
1. Update the test callables to the two-arg signature: `lambda key, default=None: True if key == "privacy_mode_enabled" else default`.
2. Update assertions to accept `action_taken="privacy_skipped"` (or `in {"kept", "privacy_skipped"}`).

---

### N2 — HIGH: `_feature_flags` init-order bug (W1481 N4) still live — W1488 NOT merged

**Severity:** HIGH
**File:** `KrabEar/backend/service.py:315`
**Status:** Carry-forward from W1481 N4 (W1488 not merged)

`BackendService.__init__` at line 315:

```python
self._llm_rewriter._feature_flags = self._feature_flags
```

`self._feature_flags` is assigned 224 lines later at line 539:

```python
self._feature_flags = FeatureFlags(data_dir=self.store.data_dir)
```

Any `BackendService()` instantiation with `_llm_rewriter is not None` raises
`AttributeError: 'BackendService' object has no attribute '_feature_flags'`.
This blocks all 6 `AutoDedupIPCTestCase` tests that construct `BackendService` directly.

W1488 (PR #1373, branch `fix-feature-flags-order-W1488`) is the fix but remains unmerged.

**Fix:** Merge PR #1373. Moving the wiring to after line 539 resolves the `AttributeError`.

---

### N3 — CRIT: `settings_provider=None` in production — W1412 NOT merged

**Severity:** CRIT
**File:** `KrabEar/backend/service.py:523`
**Status:** Carry-forward from W1481 N1 (W1412 not merged)

```python
self._auto_deduplicator = AutoDeduplicator()  # settings_provider=None
```

`_privacy_mode_enabled()` short-circuits to `False` when `self._settings_provider is None`.
Users who enable `privacy_mode_enabled=True` in settings get no privacy protection — deduplication
loads and compares full transcript texts regardless.

All other services that need runtime settings (e.g. `MetadataEnricher`, `AutoGlossaryBuilder`,
`ObsidianSyncManager`) correctly receive `self._get_runtime_setting` or `self._cached_settings`.

W1412 (PR #1315, branch `fix-autodedup-settings-provider-W1412`) fixes this but is unmerged.

**Fix:** Merge PR #1315. Change `service.py:523` to:
```python
self._auto_deduplicator = AutoDeduplicator(
    settings_provider=self._get_runtime_setting,
)
```

---

### N4 — HIGH: `_semantic_searcher` not injected in `_handle_run_deduplication` — W1412 NOT merged

**Severity:** HIGH
**File:** `KrabEar/backend/service.py` (`_handle_run_deduplication`)
**Status:** Carry-forward from W1481 N2 (W1412 not merged)

`handle_run_deduplication` accepts `params.get("_semantic_searcher")` and calls
`semantic_searcher.remove_item(dup_id)` to clean up stale embeddings after deduplication.
The IPC dispatch in `service.py` never injects the instance:

```python
def _handle_run_deduplication(self, params):
    params["_store"] = self.store
    # _semantic_searcher never set → cleanup loop unreachable
    return self._auto_deduplicator.handle_run_deduplication(params)
```

`self._semantic_searcher` is initialised at line 559 and wired into `HistoryService` and
`ArchiveManager` — but not into the dedup IPC path. Every `run_deduplication` call silently
leaves stale embeddings in the semantic index for each detected duplicate.

W1412 (PR #1315) is the fix but remains unmerged.

**Fix:** Add before delegation:
```python
params["_semantic_searcher"] = self._semantic_searcher
```

---

### N5 — MED: Time-window filter bypassed when `new_ts is None` — `check_duplicate` accepts non-ISO timestamps silently

**Severity:** MED
**File:** `KrabEar/backend/auto_deduplication.py:238–250`
**Status:** NEW (introduced by W1487)

The W1487 time-window fix applies only when `new_ts is not None` (line 245):

```python
new_ts = _parse_ts(timestamp)
for item in items:
    ...
    if new_ts is not None:
        item_ts = _parse_ts(...)
        if item_ts is not None and abs(new_ts - item_ts) > _DEDUP_WINDOW_SEC:
            continue
    sim = _text_similarity(text, candidate_text)
```

If the caller passes `timestamp=""` (empty string — the IPC handler generates a fresh
`datetime.now().isoformat()` so this requires an explicit bad value) or a non-ISO string,
`_parse_ts` returns `None` and the `if new_ts is not None` guard is never entered.
All 50 history items are then compared with no time filter, regardless of their age.

The `handle_check_duplicate` IPC handler fills an empty timestamp with `datetime.now()`
so the production path is safe. However, direct Python callers of `check_duplicate()` (unit
tests, integration code, potential future callers) that omit or pass an invalid `timestamp`
silently bypass the 60-second protection window, potentially producing false-positive
`is_duplicate=True` for semantically similar but temporally distant recordings.

**Fix:** In `check_duplicate`, if `new_ts is None` after `_parse_ts(timestamp)`, log a warning
and either (a) skip all comparisons (conservative — treat missing timestamp as out-of-window)
or (b) fall back to comparing only the most recent item. The IPC handler path is already safe.

---

## Coverage summary (post W1487 merge)

| Concern | Coverage | Gap |
|---------|----------|-----|
| Jaccard hybrid algorithm | Good (W1245) | None |
| 60-second time window in `check_duplicate` | Fixed by W1487 | N5: bypassed when `new_ts=None` |
| Privacy gate unit tests (`PrivacyModeGuardTestCase`) | 4 tests — **FAIL** | N1 HIGH: zero-arg callable incompatibility |
| `settings_provider` wired in `service.py` | None | N3 CRIT (W1412 unmerged) |
| `_semantic_searcher` injected in IPC handler | None | N4 HIGH (W1412 unmerged) |
| IPC integration tests pass (`AutoDedupIPCTestCase`) | 6 tests — **FAIL** | N2 HIGH (W1488 unmerged) |
| Scan cap on `run_deduplication` | None | W1249 unmerged (carry-forward) |
| Wire into recording-completion path | None | W1247 unmerged (carry-forward) |

---

## Action items

| # | Severity | Action | PR |
|---|----------|--------|-----|
| N1 | HIGH | Fix `PrivacyModeGuardTestCase`: update callables to `(key, default)` signature; update `action_taken` assertions to accept `"privacy_skipped"` | — |
| N2 | HIGH | Merge W1488 PR #1373 — fix `_feature_flags` init-order in `service.py` | #1373 |
| N3 | CRIT | Merge W1412 PR #1315 — wire `settings_provider=self._get_runtime_setting` | #1315 |
| N4 | HIGH | Merge W1412 PR #1315 — inject `_semantic_searcher` in `_handle_run_deduplication` | #1315 |
| N5 | MED | Guard `check_duplicate` when `new_ts is None` — log warning + skip comparisons | — |
