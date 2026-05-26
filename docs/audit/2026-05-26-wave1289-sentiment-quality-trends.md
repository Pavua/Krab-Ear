# Audit W1289: sentiment_trends.py + quality_trends.py

**Date:** 2026-05-26  
**Branch:** audit/sentiment-quality-W1289  
**Files audited:**
- `KrabEar/backend/sentiment_trends.py` — `SentimentTrendAnalyzer`
- `KrabEar/backend/quality_trends.py` — `QualityTrendAnalyzer`

---

## Summary

5 findings across both analyzers. Both modules are wired to live IPC handlers
(`get_sentiment_trends`, `analyze_quality_trends`). Regression math is correct.
Tests are comprehensive but miss the gaps identified below.

---

## Findings

### F1 — MEDIUM | sentiment_trends: no privacy_mode guard

**File:** `KrabEar/backend/sentiment_trends.py`  
**Location:** `SentimentTrendAnalyzer.analyze_sentiment_trends()` + handler
`_handle_get_sentiment_trends` in `service.py:2870`

`SentimentTrendAnalyzer` calls `EmotionDetector.detect(text, ...)` on the full
transcript text of every item in the window. This is a form of text analysis
that should be suppressed when `privacy_mode_enabled=True`.

The `translation_service.py` establishes the project pattern (lines 96, 201):

```python
if settings.get("privacy_mode_enabled"):
    return {"error": "privacy_mode_enabled", ...}
```

`_handle_get_sentiment_trends` has no equivalent check. A user who enables
privacy mode to prevent text from being processed will still have all recent
transcripts run through emotion detection on every `get_sentiment_trends` call.

`analyze_quality_trends` is NOT affected — it only reads numeric `confidence`
fields, no text.

**Fix:** Add a `privacy_mode_enabled` guard in `_handle_get_sentiment_trends`
before calling `analyze_sentiment_trends`.

---

### F2 — LOW | quality_trends: bucket labels are misleading (off-by-one at boundaries)

**File:** `KrabEar/backend/quality_trends.py`  
**Location:** `_BUCKETS` definition (lines 45–51), `_build_distribution` (lines 200–208)

The bucket loop uses `if lo <= c <= hi: ... break` (inclusive on both ends).
With buckets ordered from high to low, a value exactly at a shared boundary
hits the **higher** bucket first and stops. This is the correct runtime
behavior, but the bucket labels are misleading:

| Value | Label matched | Expected by label |
|-------|--------------|-------------------|
| `0.9` | `0.9-1.0` | correct |
| `0.8` | `0.8-0.9` | correct |
| `0.7` | `0.7-0.8` | correct |

The **effective** range of bucket `"0.8-0.9"` is `[0.8, 0.9)` not `[0.8, 0.9]`
because `0.9` is consumed by the preceding bucket. This means:

- The label `"0.8-0.9"` never actually holds value `0.9`.
- The label `"0.6-0.7"` never holds `0.7`.

The code is functionally correct and consistent — no value is double-counted
or dropped — but any UI rendering the label as a true closed interval `[lo, hi]`
will mislead users. No test exercises the misleading label interpretation.

**Fix (optional):** Either change labels to `"0.8-<0.9"` / half-open notation,
or add a comment in `_BUCKETS` documenting the first-match semantics. No
runtime behavior change needed.

---

### F3 — LOW | both modules: index-based regression ignores actual date gaps

**File:** `sentiment_trends.py:237–252`, `quality_trends.py:183–198`  
**Location:** `_linear_regression_slope()` (identical in both files)

The regression x-axis is the **list index** (0, 1, 2, …) of sorted daily
aggregates, not the actual ordinal date distance. When recordings are sparse
(e.g. data on Jan 1, Jan 2, then nothing until Mar 15), the gap of 72 days
is treated identically to a 1-day gap. This inflates the slope for sparsely
distributed data.

Example: three data points at day-indices 0, 1, 90 with confidence values
`[0.5, 0.55, 0.9]` produces slope `+0.20`, classified as "improving" — the
same result as three consecutive daily recordings. A user who records
infrequently will see "improving" trends over multi-month patterns where
a date-aware regression would show a far lower slope.

For the primary use case (active daily users over a 30-day window) this is
rarely noticeable. It becomes significant for custom large windows (`days=365`)
or users with irregular recording patterns.

**Fix (optional):** Replace `enumerate(values)` index with
`(date - first_date).days` as x-coordinate in `_linear_regression_slope`.
Requires threading dates into the helper or inlining the regression.

---

### F4 — LOW | both modules: UTC date bucketing shifts midnight for non-UTC users

**File:** `sentiment_trends.py:102`, `quality_trends.py:80`  
**Location:** `ts.date().isoformat()` called on a UTC datetime

All timestamps are normalised to UTC (correctly). `ts.date()` therefore returns
the **UTC date**, not the user's local date. For the primary target audience
(Moscow, UTC+3), a recording made at 00:30 local time is stored at 21:30 UTC
the previous day and appears in the **previous day's** bucket.

This causes the "most active/best day" display to be off by one calendar day
for early-morning recordings. During the DST transition period (when offset
changes from UTC+3 to UTC+3 — Russia no longer observes DST — this specific
offset is fixed, but Spanish/European users in UTC+1/+2 are affected).

**Fix (optional):** Accept an optional `tz` parameter in `analyze_trends` /
`analyze_sentiment_trends` and convert UTC datetime to local date before
bucketing. Default `None` preserves current behavior.

---

### F5 — LOW | both modules: `_load_active_items_unlocked()` loads full history before windowing

**File:** `service.py:2872–2876`, `audio_analytics_service.py:98–102`  
**Location:** Both trend handlers

Both IPC handlers call `store._load_active_items_unlocked()` which returns
**all** history items, then pass the full list to the analyzer which filters by
the `days` cutoff. For a user with 2+ years of history (e.g. 30 recordings/day
× 730 days = 21,900 items), the full NDJSON is parsed and all items are
allocated in memory before windowing discards most of them.

At ~1 KB per HistoryItem dict, 20,000 items = ~20 MB per trend call. This is
the same pattern used elsewhere in the backend (`BackendService` for other
history operations), so it is not unique to these modules. However, trend
calls may be triggered frequently by the GUI analytics panel.

The analyzer internals themselves are memory-safe (O(window_days) after
filtering), but the store-load stage is O(total history).

**Fix (optional):** Add a `since` parameter to `_load_active_items_unlocked`
that filters at read time (already available as a pattern in `HistoryService`).
Both trend handlers can then pass `since=cutoff` to avoid loading stale items.

---

## Wire status

| IPC method | Handler | Service | Status |
|---|---|---|---|
| `get_sentiment_trends` | `_handle_get_sentiment_trends` | `BackendService` (service.py:2870) | WIRED |
| `analyze_quality_trends` | `handle_analyze_quality_trends` | `AudioAnalyticsService` (audio_analytics_service.py:96) | WIRED |

Both methods registered in the dispatch table at `service.py:1052, 1056`.

---

## Regression correctness

The OLS slope formula in both modules (identical implementation) is
mathematically correct. Verified:

- `x_mean = (n-1)/2.0` is the exact centroid of `[0, 1, ..., n-1]`.
- Numerator/denominator match the standard OLS closed form.
- Edge cases: `n < 2` returns `0.0`; all-same-y returns `0.0` (denominator
  guard prevents division by zero).

R² is not computed — trend classification relies solely on slope threshold.
Given the thresholds are small (`±0.001` / `±0.005`) and the behavior is
documented, this is acceptable.

---

## Test coverage

Tests are comprehensive for happy paths and basic edge cases:

| Scenario | Sentiment | Quality |
|---|---|---|
| Empty history | COVERED | COVERED |
| All items outside window | COVERED | COVERED |
| Multiple items same day | COVERED | COVERED |
| ISO / epoch / naive datetime | COVERED | COVERED |
| improving / declining / stable trend | COVERED | COVERED |
| Bucket boundaries | n/a | COVERED |

**Gaps (matching findings above):**

- F1: No test for privacy_mode_enabled behaviour on `get_sentiment_trends`.
- F3: No test for sparse/non-uniform day distribution showing slope distortion.
- F4: No test for non-UTC timezone midnight bucketing.
- F5: No test for memory/performance with large history before windowing.
