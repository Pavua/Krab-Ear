# Wave 1361 — QualityTrendAnalyzer audit

**Date:** 2026-05-27
**Module:** `KrabEar/backend/quality_trends.py`
**Scope:** post-W1289 (sentiment privacy) full audit of QualityTrendAnalyzer

---

## Summary

6 findings. 3 bugs fixed inline (F1, F2, F3). 3 observations documented only (F4–F6).

---

## F1 — NaN propagates to daily avg → invalid JSON in IPC response (HIGH)

**File:** `quality_trends.py` lines 88–94, `_get_confidence` lines 136–147

**Root cause:** `_get_confidence` calls `float(val)` which silently accepts `float('nan')`
and `float('inf')`. A single history item whose `confidence` field holds a NaN value
(e.g., written by a pre-calibrator code path that computed `math.exp(avg_logprob)` and
got overflow) propagates through `daily.setdefault(...).append(confidence)` into the daily
aggregation. `sum(vals) / len(vals)` with a NaN element returns NaN. The IPC layer
encodes the response with `json.dumps(..., ensure_ascii=False)` which produces the literal
`NaN` token — invalid per RFC 8259. Swift's `JSONDecoder` rejects the entire response,
causing a silent history-panel blank on the day the corrupted item falls into.

**Impact:** One corrupted history item silently blanks the analytics panel for the entire day
containing it. The bug existed since the module was created; the W1025 NaN guard added to
`confidence_calibrator.py` (clamp at [0.0, 1.0]) does not guard values already stored
in the NDJSON history before that fix.

**Fix:** Add `math.isfinite()` guard in `_get_confidence` (see fix F1 in source).

---

## F2 — Out-of-range confidence values silently dropped from distribution (MED)

**File:** `quality_trends.py` `_build_distribution` lines 200–208

**Root cause:** The bucket loop uses `lo <= c <= hi` with buckets covering `[0.0, 1.0]`.
Values outside this range (e.g., `1.1` from a future STT adapter that returns raw log-prob
confidence) match no bucket and are silently skipped. The distribution totals less than
`len(all_confidences)`. This makes the distribution count appear lower than the
`total_recordings_in_window` count without any signal to the caller.

**Fix:** Values outside `[0.0, 1.0]` are already skipped by the `math.isfinite()` guard
in F1 for NaN/inf. For values in `(1.0, ∞)` or `(-∞, 0.0)` that are finite, add clamping
before bucket lookup so they fall into the boundary bucket (`0.9-1.0` for >1.0,
`0.0-0.6` for <0.0).

---

## F3 — `days` parameter accepts 0 and negative values; silently returns empty result (MED)

**File:** `audio_analytics_service.py` line 98, `quality_trends.py` line 65

**Root cause:** `handle_analyze_quality_trends` does `days = int(params.get("days", 30))`
with no lower-bound clamp. With `days=0`, `cutoff = now - timedelta(days=0) = now`, and
only items at the exact millisecond of the call (practically zero) survive. With `days=-5`,
`cutoff = now + timedelta(days=5)` (5 days in the future), and every item is older than
the cutoff — all items excluded. Both cases silently return `daily_confidence=[]` and
`overall_trend="stable"`, which looks like valid data. The IPC layer returns success with
an empty dataset, misleading dashboards and tests alike.

**Fix:** Clamp `days = max(1, days)` in `audio_analytics_service.handle_analyze_quality_trends`
(mirrors the clamp already present in `analytics_dashboard.py` line 49).

---

## F4 — No privacy_mode guard (sister to W1289 sentiment fix) (MED, observation)

**File:** `audio_analytics_service.py` `handle_analyze_quality_trends`

`SentimentTrendAnalyzer` and `TranslationService` both check `privacy_mode_enabled` before
reading history and log a `PrivacyAuditLogger` event. `QualityTrendAnalyzer` has no such
guard. In privacy mode the user expects analytics derivation from transcription history to
be suppressed, but `analyze_quality_trends` reads the full active history and returns
per-day confidence statistics. Confidence statistics are metadata-only (no transcript text),
so the risk is lower than sentiment, but the asymmetry is a compliance gap relative to
the W1289 pattern.

**Fix pattern:** Same as `translation_service.py` lines 96–105 — check `_get_runtime_setting`
for `privacy_mode_enabled` at the top of `handle_analyze_quality_trends`; if True, return
the same empty TrendReport that the empty-history path returns.

---

## F5 — Small-sample trend instability: n=2 days triggers meaningful trend labels (LOW)

**File:** `quality_trends.py` `_linear_regression_slope` + `analyze_trends`

With only two days of data the OLS slope reduces to `y[1] - y[0]`. The `_SLOPE_IMPROVING`
threshold of `0.001` means any confidence change of more than 0.001 between the two days
is labelled `"improving"` or `"declining"`. A user who records two sessions on consecutive
days with avg confidence 0.85 → 0.86 sees "improving" despite this being statistical noise.
The sentiment_trends module in this codebase uses the same thresholds and the same n<2
guard. This is an accepted design tradeoff (simple rule-based system), not a correctness bug.

**No fix required** — acknowledge design decision. Adding a minimum-day requirement (e.g.,
`n < 3 → "stable"`) would be a non-breaking enhancement wave.

---

## F6 — Wire status: QualityTrendAnalyzer is NOT used by AnalyticsDashboard (observation)

**File:** `analytics_dashboard.py`

`AnalyticsDashboard._build_dashboard` computes its own confidence trend via the local
`_calc_trend` helper (line 198–213) using raw per-day averages accumulated in the same
single-pass loop. It does not call `QualityTrendAnalyzer.analyze_trends`. This means
the GUI analytics dashboard and the `analyze_quality_trends` IPC method return independent
quality metrics computed by two different code paths. The bucket distribution
(`confidence_distribution`) and the `best_day`/`worst_day` fields are only available
via the explicit `analyze_quality_trends` IPC call, not through `get_analytics_dashboard`.

This duplication is harmless (both paths produce consistent trend direction for the same
data) but means the F1 NaN fix must be applied to `analytics_dashboard.py` line 172–175
as well — that path also does `float(conf)` without an `isfinite` guard and the result
propagates to the `quality.avg_confidence` field in the dashboard response.

**No fix required for this audit** — document the duplication; the analytics_dashboard
NaN guard is a separate targeted fix.

---

## Test coverage assessment

Existing coverage in `test_quality_trends.py` + `test_quality_trends_extras.py`:
- Happy path (improving/declining/stable): good
- Empty history: good
- NaN/inf confidence: **missing** (F1 gap — `float('nan')` passed as confidence is not tested)
- Out-of-range confidence (>1.0, <0.0): **missing** (F2 gap)
- `days=0`, `days=-1`: **missing** (F3 gap)
- 1000-item same-day aggregation: covered

New regression tests added for F1, F2, F3 in `test_quality_trends_extras.py`.

---

## Files changed

| File | Change |
|------|--------|
| `KrabEar/backend/quality_trends.py` | F1: `isfinite` guard in `_get_confidence`; F2: clamp OOB confidence in `_build_distribution` |
| `KrabEar/backend/audio_analytics_service.py` | F3: `days = max(1, days)` clamp |
| `KrabEar/tests/test_quality_trends_extras.py` | Regression tests for F1, F2, F3 |
| `docs/audit/2026-05-27-wave1361-quality-trends.md` | This document |
