# W1527 Regression Audit: AutoDeduplicator

**Date:** 2026-05-27
**Auditor:** W1527 sub-agent (regression sweep)
**Trigger:** W1497 cherry-pick suspected to have reverted prior fixes
**Files audited:**
- `KrabEar/backend/auto_deduplication.py`
- `KrabEar/backend/service.py`
- `KrabEar/backend/recording_core_service.py`
- `KrabEar/tests/test_auto_deduplication.py`

---

## Wave merge state (HEAD = `98d0d679` on `codex/krab-ear-v2`)

| Wave | Fix | Expected signature | Status |
|------|-----|-------------------|--------|
| W1245 | Jaccard hybrid + `_check_lock` | `_text_similarity`, `_JACCARD_LOW`, `_JACCARD_HIGH`, `_check_lock` in `auto_deduplication.py` | **REGRESSED** |
| W1247 | Wire into recording-completion | `auto_deduplicator` kwarg + `_persist_lock` in `recording_core_service.py` | **REGRESSED** |
| W1248 | Privacy gate + `settings_provider` | `settings_provider` param + `_privacy_mode_enabled()` in `auto_deduplication.py` | PRESENT |
| W1249 | Scan cap async + job IPC | `_MAX_DEDUP_SCAN`, `_MISSING_TS_PLACEHOLDER`, `run_deduplication_async()`, `handle_dedup_progress()` | PRESENT |
| W1412 | `settings_provider` inject in `service.py` | `AutoDeduplicator(settings_provider=self._get_runtime_setting)` at line 452 | **REGRESSED** |
| W1487 | 60-second time-window filter | `_DEDUP_WINDOW_SEC`, `_parse_ts()` in `auto_deduplication.py` | **REGRESSED** |
| W1488 | `_feature_flags` init order in `service.py` | `if self._llm_rewriter is not None: self._llm_rewriter._feature_flags = self._feature_flags` after line 468 | **REGRESSED** |
| W1513 | `PrivacyModeGuardTestCase` 2-arg lambda | `lambda k, d=False: True if k == "privacy_mode" else d` in test file | PRESENT |

---

## Regression findings (capped at 5)

### R1 — CRIT: W1245 Jaccard hybrid completely absent — W1249 overwrite is the culprit

**Severity:** CRIT
**File:** `KrabEar/backend/auto_deduplication.py`
**Root cause:** W1249 commit (`640eb1f7`) rewrote `auto_deduplication.py` starting from a base
that predated W1245 and W1487. The diff shows W1249 deleted 86 lines including all of:
- `from difflib import SequenceMatcher`
- `_JACCARD_LOW: float = 0.7`
- `_JACCARD_HIGH: float = 0.85`
- `_DEDUP_WINDOW_SEC: int = 60`
- `def _parse_ts(ts_value: Any) -> float | None`
- `def _text_similarity(text_a: str, text_b: str) -> float`
- `self._check_lock = threading.Lock()`

W1249 was correctly bringing scan-cap + async job infra, but it based its diff on the W1249
branch tip (pre-W1245 merge) and overwrote the Jaccard work.

**Consequence:** `check_duplicate()` now delegates to `DuplicateDetector.find_duplicates()` which
uses character-level `SequenceMatcher` with no `_check_lock` serialisation. The false-positive
rate for short Russian texts with common prefixes is elevated, and concurrent check+insert is
not serialised (race window open).

**Also absent:** `_DEDUP_WINDOW_SEC` and `_parse_ts` (W1487 work). The 60-second temporal
window is now enforced only by `DuplicateDetector.DEFAULT_TIME_WINDOW_SECONDS` inside
`find_duplicates()`, which is correct for the `run_deduplication` path but not for the direct
Jaccard loop that W1245 introduced in `check_duplicate()`.

**Fix:** Re-apply W1245 + W1487 content on top of current HEAD. Specifically:
1. Add `from difflib import SequenceMatcher` import.
2. Add `_JACCARD_LOW`, `_JACCARD_HIGH`, `_DEDUP_WINDOW_SEC` module constants.
3. Add `_parse_ts()` and `_text_similarity()` module functions.
4. Add `self._check_lock = threading.Lock()` to `__init__`.
5. Replace `DuplicateDetector.find_duplicates()` delegation in `check_duplicate()` with the
   direct Jaccard loop + 60-second temporal filter from W1487.

---

### R2 — HIGH: W1247 recording-completion wiring absent — W1138 overwrote it

**Severity:** HIGH
**File:** `KrabEar/backend/recording_core_service.py`
**Root cause:** `git bisect` confirmed commit `18ce1bbf` (W1138 — "RecordingCoreService tag
history items in privacy_mode") as the first bad commit. W1138 rewrote
`_stop_recording_phase_e` and the constructor from its own branch base, discarding W1247's:
- `auto_deduplicator: Any = None` kwarg
- `self._auto_deduplicator = auto_deduplicator`
- `self._persist_lock = threading.Lock()`
- The `with self._persist_lock:` guard block around dedup-check + persist

`service.py` no longer passes `auto_deduplicator=self._auto_deduplicator` to
`RecordingCoreService`, so even if W1247 were re-applied to the service file the constructor
would silently receive no argument and dedup would never fire.

**Consequence:** every recording completion bypasses `AutoDeduplicator.check_duplicate()`.
The auto-dedup feature is completely dead in the recording pipeline. The `check_duplicate` IPC
method still works (it's a separate handler) but `AUTO_DEDUP_ENABLED` has no effect at
record-time.

**Fix:** Re-apply W1247 on top of current HEAD in both files:
- `recording_core_service.py`: restore `auto_deduplicator` kwarg, `_persist_lock`, dedup
  guard in `_stop_recording_phase_e` and `_transcribe_paths_core`.
- `service.py`: pass `auto_deduplicator=self._auto_deduplicator` at
  `RecordingCoreService(...)` construction.

---

### R3 — CRIT: W1412 `settings_provider` not injected in `service.py`

**Severity:** CRIT
**File:** `KrabEar/backend/service.py`, line 452
**Status:** Carry-forward from W1505 N3, W1481 N1

Current code:
```python
self._auto_deduplicator = AutoDeduplicator()  # settings_provider=None
```

The W1412 commit (`8d982944`) was merged and IS in the git history of `codex/krab-ear-v2`.
However, inspection of the service.py diff shows only 3 lines were added in W1412 (import
changes and the `_semantic_searcher` injection in `_handle_run_deduplication`) — the
`settings_provider=self._get_runtime_setting` injection was carried in the same PR but appears
to have been lost (not present at the merge base used by W1412's diff).

Result: `_privacy_mode_enabled()` always returns `False` because `self._settings_provider is None`.
Privacy-mode users get full transcript comparison despite having `privacy_mode_enabled=True`.

**Fix:**
```python
self._auto_deduplicator = AutoDeduplicator(
    settings_provider=self._get_runtime_setting,
)
```

---

### R4 — HIGH: W1487 60-second window absent from `check_duplicate()` Jaccard loop

**Severity:** HIGH
**File:** `KrabEar/backend/auto_deduplication.py`
**Status:** Consequence of R1 (same W1249 overwrite)

This is a direct consequence of R1 — `_DEDUP_WINDOW_SEC` and `_parse_ts` were added by W1487
as part of the Jaccard loop that W1249 then overwrote. The current `check_duplicate()` uses
`DuplicateDetector.find_duplicates()` which internally applies a 60-second window, so the
protection is technically present but only via the DuplicateDetector path, not via the
dedicated `check_duplicate()` direct-comparison path.

When W1245 is re-applied (R1 fix), `_DEDUP_WINDOW_SEC` + `_parse_ts` MUST be re-applied
simultaneously (W1487) or the restored Jaccard loop will compare all 50 history items
regardless of their age.

**Fix:** Apply R1 fix in a single atomic commit that includes both W1245 and W1487 content
to prevent partial re-regression.

---

### R5 — HIGH: W1488 `_feature_flags` guard not injected into `_llm_rewriter`

**Severity:** HIGH
**File:** `KrabEar/backend/service.py`
**Status:** Carry-forward from W1505 N2, W1481 N4

W1488 commit (`39bbc6cf`) is in the git history and was merged, but the guard is absent:

```python
# Expected (W1488) — NOT present in current HEAD:
if self._llm_rewriter is not None:
    self._llm_rewriter._feature_flags = self._feature_flags
```

`_feature_flags` is initialised at line 468. The `_llm_rewriter._feature_flags = ...`
assignment that should follow it is absent. The LLMRewriter therefore never receives
`FeatureFlags` and cannot honour `llm_rewrite_enabled` feature-flag toggles at runtime.

The W1505 audit (docs commit `dfd54e80`) already documented this as N2 HIGH (unmerged),
but the W1488 commit IS in the branch history with a merged PR #1373. This is contradictory —
the commit landed in the branch but the change is not reflected in the file. Most likely the
W1488 diff was rebased on an older `service.py` that already lacked this block, making the
merge a no-op.

**Fix:**
After line 468 (`self._feature_flags = FeatureFlags(...)`), add:
```python
if self._llm_rewriter is not None:
    self._llm_rewriter._feature_flags = self._feature_flags
```

---

## Summary table

| Finding | Wave | Severity | File | Missing signature |
|---------|------|----------|------|------------------|
| R1 | W1245+W1487 | CRIT | `auto_deduplication.py` | `_text_similarity`, `_JACCARD_LOW`, `_DEDUP_WINDOW_SEC`, `_parse_ts`, `_check_lock` |
| R2 | W1247 | HIGH | `recording_core_service.py` | `auto_deduplicator` kwarg, `_persist_lock`, dedup guard |
| R3 | W1412 | CRIT | `service.py:452` | `settings_provider=self._get_runtime_setting` |
| R4 | W1487 | HIGH | `auto_deduplication.py` | Consequence of R1, fix together |
| R5 | W1488 | HIGH | `service.py` | `_llm_rewriter._feature_flags` assignment |

**Waves confirmed present (not regressed):**
- W1248 — `settings_provider` field + `_privacy_mode_enabled()` in `auto_deduplication.py`
- W1249 — scan cap + async job infra in `auto_deduplication.py`
- W1513 — `PrivacyModeGuardTestCase` 2-arg lambda in test file

**Root causes:**
- W1249 merge based on pre-W1245 file → clobbered Jaccard hybrid (R1, R4)
- W1138 rewrote `recording_core_service.py` from its own base → clobbered W1247 wiring (R2)
- W1412 `service.py` diff silently lost `settings_provider` injection (R3)
- W1488 `service.py` diff silently lost `_feature_flags` injection (R5)
