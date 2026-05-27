# Audit W1290 — PeriodComparisonService (period_comparison.py)

**Date:** 2026-05-26  
**Branch:** audit/period-comparison-W1290  
**File audited:** `KrabEar/backend/period_comparison.py` (277 lines)  
**Related:** `KrabEar/backend/service.py` (IPC handler), `KrabEar/tests/test_period_comparison.py` (47 tests, all pass)

---

## Summary

5 findings, 0 critical. The module is well-structured with clean `PeriodStats`/`ComparisonReport` dataclasses and solid division-by-zero protection in `_pct_change`. The main risks are: a date arithmetic bug that produces inverted period windows with `weeks_back=1`, a live IPC gap where mode=weeks/months convenience modes are not reachable, and an unbounded memory accumulation on long date ranges.

---

## Findings

### F1 — BUG (MEDIUM): `compare_weeks(weeks_back=1)` produces inverted period (start > end)

**Location:** `period_comparison.py:200`

```python
p1_start = p1_end - timedelta(days=(weeks_back - 1) * 7 - 1)
```

When `weeks_back=1`, the formula evaluates to:
`p1_start = p1_end - timedelta(days=(0) * 7 - 1) = p1_end - timedelta(days=-1) = p1_end + 1 day`

So `p1_start` is **one day after `p1_end`**, producing an inverted range. The store receives `from_ts > to_ts`, typically returning an empty result rather than an error, so the caller sees a silently wrong comparison (period1 = 0 recordings, period2 = current week data).

Verified:
```
weeks_back=1: p1_start=2026-05-25, p1_end=2026-05-24  # inverted
weeks_back=2: p1_start=2026-05-18, p1_end=2026-05-24  # correct (6 days)
```

The intended semantics for `weeks_back=1` should be "compare the previous 7 days against the current week". The formula works for `weeks_back >= 2` only.

**Fix:** add an early guard in `compare_weeks`:
```python
weeks_back = max(2, int(weeks_back))
```
or correct the formula to be unambiguous:
```python
p1_start = p1_end - timedelta(days=6)  # always exactly 7 days for weeks_back=2
```

---

### F2 — BUG (MEDIUM): No validation that start <= end for custom periods; inverted ranges silently produce empty stats

**Location:** `period_comparison.py:131-137`, `PeriodComparisonService.handle_compare_periods:244-252`

`compare_periods()` has no guard checking that `period1_start <= period1_end` or `period2_start <= period2_end`. When a caller passes an inverted range (e.g. `period1_start="2024-01-15"`, `period1_end="2024-01-07"`), the function constructs `from_ts="2024-01-15"`, `to_ts="2024-01-07T23:59:59"` and passes them straight to the store. The store's `get_history_page_filtered` is documented to accept ISO timestamps but returns silently empty results for inverted ranges rather than raising.

The caller receives `recordings=0, duration_sec=0.0` with no indication that the input was invalid — the "Нет данных для сравнения" path is not triggered since `recordings_change_pct` is always computed. The `summary` will read: `"Записей: 0 (было 0, +0.0%)"`, indistinguishable from a legitimately empty period.

**Fix:** add validation at the start of `compare_periods()`:
```python
from datetime import date as _date
p1s = _iso_date(period1_start)
p1e_d = _iso_date(period1_end)
if p1s > p1e_d:
    raise ValueError(f"period1_start ({p1s}) must not be after period1_end ({p1e_d})")
```

---

### F3 — GAP (MEDIUM): IPC handler in `service.py` does not expose `mode=weeks/months`; `PeriodComparisonService` is unused in production

**Location:** `service.py:61,1053,2799-2835`

`service.py` imports only the bare `compare_periods` function:
```python
from backend.period_comparison import compare_periods as _compare_periods_fn
```

The IPC handler `_handle_compare_periods` always calls `compare_periods()` directly with explicit dates. There is no `mode` parameter handling — callers cannot pass `{"mode": "weeks"}` or `{"mode": "months"}` via the live socket.

`PeriodComparisonService` (lines 222-254) — which does support `mode=weeks/months/custom` — is **never instantiated** in `service.py`. The Swift client (`HistoryPanelController`) must compute period boundaries client-side or always pass explicit dates, which is unnecessary complexity.

Additionally, `service.py`'s `_handle_compare_periods` manually reconstructs the response dict (30+ lines) instead of calling the already-available `_report_to_dict()` helper, creating a maintenance divergence risk if `ComparisonReport` gains new fields.

**Fix:** wire `PeriodComparisonService` into `BackendService.__init__` and delegate `_handle_compare_periods` to it:
```python
self._period_comparison_svc = PeriodComparisonService(self.store)
# in handler:
return self._period_comparison_svc.handle_compare_periods(params)
```

---

### F4 — GAP (LOW): `_collect_stats` accumulates all result pages into memory with no size cap

**Location:** `period_comparison.py:58-71`

```python
all_items = list(items)
while cursor is not None:
    items, cursor = store.get_history_page_filtered(...)
    all_items.extend(items)
```

Every item dict from the store is appended to `all_items` before any aggregation. For a multi-year date range on a heavy user's history (e.g. 5,000+ entries), all dicts remain in memory simultaneously. The `while cursor` loop has no page limit.

The aggregation (duration, words, confidence) is purely additive, so it could be computed incrementally page-by-page without accumulation — only `languages` (a `set`) and the final counts require state. As implemented, a 5-year range with 500-item pages runs 10+ iterations and holds all records in RAM during the comparison.

This is LOW severity because individual item dicts are small (~200-500 bytes) and 5,000 items is ~2 MB. However, the pattern is inconsistent with how other analytics services cap their history scans.

**Fix (incremental aggregation):**
```python
recordings = 0
duration_sec = 0.0
words = 0
conf_sum = 0.0
conf_count = 0
langs: set[str] = set()

cursor = None
while True:
    items, cursor = store.get_history_page_filtered(cursor=cursor, limit=500, ...)
    for item in items:
        # accumulate in-place
        ...
    if cursor is None:
        break
```

---

### F5 — GAP (LOW): `_pct_change` returns `0.0` for zero-baseline even when `new != 0`; summary is misleading

**Location:** `period_comparison.py:40-44`

```python
def _pct_change(old: float, new: float) -> float:
    if old == 0.0:
        return 0.0
    return round((new - old) / old * 100, 2)
```

When period1 has no recordings (old=0) and period2 has 10 (new=10), `_pct_change` returns `0.0`. The generated summary then reads:

> `"Записей: 10 (было 0, +0.0%)"`

The counts (10 vs 0) are present but the `+0.0%` change label is factually wrong — it implies no change. This is the correct behavior to avoid `ZeroDivisionError`, but the display should distinguish "no baseline available" from "no change". The existing test `test_compare_periods_empty_to_full` asserts `recordings_change_pct == 0.0` — correct per spec, but the summary format is misleading.

No privacy-mode interaction issue was found: `compare_periods` reads transcription metadata (duration, word counts, confidence, language codes) but not transcript text. `privacy_mode_enabled` is only enforced for Sentry init and remote translation — no enforcement is needed here since no text content is surfaced. This is correct behavior.

**Fix (optional):** return `None` or a sentinel from `_pct_change` for zero-baseline cases and render `"н/д"` (not applicable) in the summary instead of `+0.0%`.

---

## Wire status

| Method | Wired in service.py | Notes |
|--------|---------------------|-------|
| `compare_periods` (custom mode) | YES — `"compare_periods"` at line 1053 | Correct |
| `compare_weeks` (mode=weeks) | NO | PeriodComparisonService not wired |
| `compare_months` (mode=months) | NO | PeriodComparisonService not wired |

## Test coverage

47 tests in `KrabEar/tests/test_period_comparison.py`, all passing. Coverage includes: `_pct_change` zero-baseline, empty periods, pagination, `PeriodComparisonService` mode dispatch (weeks/months/custom), overlapping periods, unicode safety. Gaps: no test for `weeks_back=1` producing inverted range (F1), no test for inverted custom period dates (F2).

## DST / timezone

No DST exposure: all date arithmetic uses `datetime.date` and naive `timedelta` — no timezone-aware `datetime` objects. The `T23:59:59` suffix appended to end dates is naive local time. Since KrabEar stores timestamps in ISO format (confirmed from `state_store.py`) and comparisons are string-lexicographic within the same locale, DST boundary crossings do not affect correctness. Not a finding.
