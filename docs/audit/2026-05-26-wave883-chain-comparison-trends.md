# Wave 883 — Audit: recording_chain · period_comparison · quality_trends

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/recording_chain.py`, `KrabEar/backend/period_comparison.py`, `KrabEar/backend/quality_trends.py`  
**Focus:** Data integrity, time-window correctness, edge cases.

---

## 1. `recording_chain.py` — RecordingChainManager

### What it does
Maintains named chains of history-item IDs (for multi-part meetings / session series).
Persisted to `{data_dir}/recording_chains.json` via atomic tmp→replace write.

### Findings

#### RC-1 · MEDIUM — `get_chain` drops the lock before resolving store items (TOCTOU)
`get_chain` copies `item_ids` inside the lock, then resolves each ID against the store **outside** the lock (lines 150–168).  
If another thread calls `unlink_recording_from_chain` on the same chain between the lock release and the store lookups, the returned `item_ids` list and the `items` detail list diverge: `item_ids` still contains the ID that was concurrently removed, but the store may return `None` for it, producing the fallback `{"id": iid}` sentinel.  
The divergence is cosmetic (no data loss), but callers relying on `item_ids` and `items` being consistent will see a stale ID in `item_ids` with no corresponding data in `items["text"]`.  
**Recommendation:** Document the known inconsistency in the docstring or move store resolution inside the lock (if the store lock is re-entrant).

#### RC-2 · LOW — No upper bound on `item_ids` list length
A chain accepts an unlimited number of item IDs.  A caller who iterates all IDs through `get_chain` will call `get_history_item_by_id` N times under a single IPC round-trip, with no pagination.  On large chains (thousands of recordings) this can produce very large JSON responses and long latency.  
**Recommendation:** Add a configurable `max_items` guard (e.g., 1 000) or return a paginated `get_chain` variant.

#### RC-3 · LOW — `list_chains` sorts by string comparison of ISO timestamps
`list_chains` sorts by `c.get("created_at", "")` as a plain string (line 185).  This works correctly for well-formed ISO-8601 strings in the same timezone offset (which the code always produces via `datetime.now(timezone.utc).isoformat()`), but would silently misfeed if a manually patched JSON file contained mixed-offset or non-ISO `created_at` values, since Python string-sorts `"2026-05-26T10:00:00+02:00"` before `"2026-05-26T09:00:00+00:00"` even though they represent the same instant.  
**Recommendation:** Parse to `datetime` before sorting, or accept the current behavior as-is with a documentation note.

#### RC-4 · LOW — `unlink_recording_from_chain` allows unlinking from an already-ended chain
`end_chain` marks the chain as completed, but `unlink_recording_from_chain` does not check `ended_at` (lines 121–130).  Items can therefore be removed from a finalized chain.  Whether this is intentional is not documented.  
**Recommendation:** Either guard against mutations on ended chains (consistent with `add_to_chain` behavior) or add an explicit docstring note that ended chains can still be modified by unlink.

#### RC-5 · LOW — Corrupted JSON silently resets all chains
If `recording_chains.json` contains invalid JSON, `_load` catches the exception and silently starts from `{"chains": {}}` (line 63), discarding all previously persisted chains.  There is no backup or recovery attempt.  
**Recommendation:** Copy the corrupted file to `.bak` before resetting, matching the pattern used by other store modules.

#### What is well-handled
- Atomic write via temp-file rename prevents partial writes.
- Duplicate-item guard in `add_to_chain` is correct.
- `end_chain` is idempotent.
- Thread-safety is covered for all mutating operations via `threading.Lock`.
- IPC handlers validate required params before delegating.

---

## 2. `period_comparison.py` — PeriodComparisonService / compare_periods

### What it does
Compares two arbitrary date-range periods (recordings count, duration, words, avg confidence, languages) from the history store, returning a human-readable delta summary.

### Findings

#### PC-1 · MEDIUM — `compare_weeks` period-1 length is asymmetric
`compare_weeks` computes period-1 as:
```python
p1_end   = week_start - timedelta(days=1)           # Sunday of last week
p1_start = p1_end - timedelta(days=(weeks_back - 1) * 7 - 1)
```
With the default `weeks_back=2`, `p1_start` is `p1_end - 6 days` — correct, a full Monday→Sunday week.  
With `weeks_back=3`, `p1_start` is `p1_end - 13 days` — a 14-day window spanning two past weeks.  The parameter name `weeks_back` suggests "use the week N weeks ago", but the formula actually extends the window to cover all weeks from `week_start - weeks_back*7` forward.  The docstring says "N недель назад" (N weeks ago) but the implementation captures `N-1` full weeks rather than a single week.  
**Recommendation:** Clarify semantics in the docstring (or fix to always produce a single-week window regardless of `weeks_back`).

#### PC-2 · LOW — End-of-period timestamp uses local-time midnight, not UTC
`compare_periods` appends `T23:59:59` directly to the date string:
```python
p1_end = _iso_date(period1_end) + "T23:59:59"
```
The resulting string has no timezone offset.  If `get_history_page_filtered` parses it as UTC and history items are stored in local time (or vice-versa), recordings at the very end of a day may be excluded.  The risk depends on `StateStore` parsing behavior.  Additionally, `T23:59:59` misses the last second's sub-second events; using `T23:59:59.999999` or switching to the next-day `T00:00:00` exclusive bound would be more correct.  
**Recommendation:** Append `T23:59:59.999999Z` or accept a fully closed interval from the caller.

#### PC-3 · LOW — `_pct_change` returns 0 when `old == 0`, masking genuine growth
When `period1` has zero recordings (cold start or empty range), `_pct_change` returns `0.0` instead of signaling infinite / undefined growth (lines 40–44).  The generated `summary` line will then show "+0.0%" even when period2 has many recordings.  
**Recommendation:** Return `None` or a sentinel (e.g., `float("inf")`) for the undefined case, and adjust the summary formatter accordingly.

#### PC-4 · LOW — Unbounded pagination may block the IPC thread
`_collect_stats` pages through the store until `cursor is None`, accumulating all matching items in memory (lines 59–71).  For a range covering years of data, this can hold tens of thousands of items and block the IPC handler thread for multiple seconds.  
**Recommendation:** Add a maximum-items cap (e.g., 10 000) with a warning log, or run the query asynchronously.

#### PC-5 · INFO — `audio_duration_sec` field vs `duration_sec`
`_collect_stats` reads `item.get("audio_duration_sec")` (line 81) while `recording_chain.get_chain` reads `d.get("duration_sec", 0)`.  If history items consistently use one or the other key, one module silently gets zero duration.  Which key is canonical is not documented.  
**Recommendation:** Unify on a single field name across all analytics modules and document it in `backend/models.py`.

#### What is well-handled
- `compare_months` correctly handles month boundaries (January → December wraps to December of the previous year via `p1_end = p2_start - timedelta(days=1); p1_start = p1_end.replace(day=1)`).
- Language delta (`new_languages`) is correctly computed as set difference.
- `_iso_date` handles `date`, `datetime`, and string inputs gracefully.
- Summary is only generated when data is present.

---

## 3. `quality_trends.py` — QualityTrendAnalyzer

### What it does
Groups confidence scores by calendar day over a configurable window, runs a numpy-free linear regression on daily averages, and classifies the trend as `improving` / `stable` / `declining`.

### Findings

#### QT-1 · MEDIUM — Histogram buckets have overlapping upper/lower bounds
The `_BUCKETS` list defines adjacent bands whose boundaries overlap at the shared edge:
```python
("0.9-1.0", 0.9, 1.0),
("0.8-0.9", 0.8, 0.9),   # 0.9 appears in BOTH "0.9-1.0" and "0.8-0.9"
```
`_build_distribution` uses `lo <= c <= hi` with `break` on first match (lines 203–207), so a value of exactly `0.9` lands in `"0.9-1.0"` (correct) while exactly `0.8` lands in `"0.8-0.9"` (correct).  However a value of exactly `1.0` matches `"0.9-1.0"` (correct), and a value slightly above `1.0` (e.g., from a miscalibrated confidence score) falls through all buckets and is silently dropped.  
**Recommendation:** Change the lowest-bucket upper bound check to handle out-of-range values, or add a catch-all with `max(0.0, min(c, 1.0))` clamping before bucketing.

#### QT-2 · LOW — Trend slope threshold is fixed at ±0.001 per index, not per day
`_SLOPE_IMPROVING = 0.001` and `_SLOPE_DECLINING = -0.001` are applied to the regression over daily averages indexed 0…N-1.  The slope unit is "confidence per day index", which equals "confidence per calendar day" only when there are no gaps in the daily data.  On a sparse dataset with entries only on Mondays, the effective temporal rate is `slope × 7`, making the threshold 7× more sensitive than intended.  
**Recommendation:** Use actual day-offsets from the first date (in days since epoch) as x-values instead of 0-based indices, so the slope has consistent units (confidence per calendar day) regardless of data density.

#### QT-3 · LOW — `cutoff` uses wall-clock `datetime.now(timezone.utc)` — no dependency injection
`analyze_trends` computes `cutoff = datetime.now(timezone.utc) - timedelta(days=days)` (line 65).  This makes the function non-deterministic in tests, requiring real-time item timestamps.  
**Recommendation:** Accept an optional `reference_ts: datetime | None = None` parameter (default `datetime.now(timezone.utc)`) to enable deterministic unit testing.

#### QT-4 · INFO — `best_day` / `worst_day` are undefined when all days share the same average
When all days have identical avg confidence, `max` and `min` both return the first/last element respectively (Python's stable `min`/`max`).  The returned `best_day` and `worst_day` will be different dict objects pointing to different dates even though the values are identical, which may mislead callers that present them as meaningful extremes.  
**Recommendation:** Return `{}` (or add an `is_meaningful: bool` field) when `best_day["avg"] == worst_day["avg"]`.

#### QT-5 · INFO — No IPC handler wrapper in `quality_trends.py`
The module exports only `QualityTrendAnalyzer` without an IPC-facing `handle_*` method.  Dispatch is done inline in `service.py`.  This is inconsistent with the service-extraction pattern used by other modules.  Not a bug, but a consistency gap.

#### What is well-handled
- `_get_ts` correctly handles naive datetimes, epoch floats, ISO strings with `Z`, and `None`.
- `_get_confidence` safely coerces non-float values without crashing.
- `_linear_regression_slope` returns `0.0` on degenerate inputs (`n < 2`, zero denominator).
- Empty-items fast path returns a fully-populated `TrendReport` with safe defaults.
- `to_dict` serialization is straightforward and lossless.

---

## Summary Table

| ID | Module | Severity | Issue |
|----|--------|----------|-------|
| RC-1 | recording_chain | MEDIUM | TOCTOU gap between lock release and store resolution in `get_chain` |
| PC-1 | period_comparison | MEDIUM | `compare_weeks(weeks_back=N>2)` expands window to N-1 weeks instead of 1 week |
| QT-1 | quality_trends | MEDIUM | Confidence values >1.0 silently dropped by histogram bucketing |
| RC-2 | recording_chain | LOW | No upper bound on chain length → unbounded IPC response |
| RC-3 | recording_chain | LOW | String-based ISO sort in `list_chains` breaks on mixed-offset timestamps |
| RC-4 | recording_chain | LOW | Ended chains can still have items unlinked (undocumented) |
| RC-5 | recording_chain | LOW | Corrupted JSON silently resets all chains with no backup |
| PC-2 | period_comparison | LOW | `T23:59:59` end-timestamp has no timezone offset; misses sub-second events |
| PC-3 | period_comparison | LOW | `_pct_change` returns 0 for undefined growth (old==0) |
| PC-4 | period_comparison | LOW | Unbounded pagination blocks IPC thread for large date ranges |
| PC-5 | period_comparison | INFO | Field name `audio_duration_sec` vs `duration_sec` inconsistency across modules |
| QT-2 | quality_trends | LOW | Slope threshold is index-based, not calendar-day-based; misleading on sparse data |
| QT-3 | quality_trends | LOW | `datetime.now()` in `analyze_trends` prevents deterministic unit testing |
| QT-4 | quality_trends | INFO | `best_day`/`worst_day` unspecified when all days share same avg |
| QT-5 | quality_trends | INFO | No IPC handler wrapper; dispatch handled inline in service.py |

**Total: 15 findings** (3 MEDIUM · 7 LOW · 5 INFO).  No CRITICAL or data-loss bugs found.
