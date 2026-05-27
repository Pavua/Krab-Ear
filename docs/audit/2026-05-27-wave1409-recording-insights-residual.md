# W1409 — RecordingInsightsGenerator Residual Audit

**Date:** 2026-05-27  
**Module:** `KrabEar/backend/recording_insights.py`  
**Triggered by:** W1267 original audit + W1273/W1274 fixes (both still OPEN)  
**Auditor:** Sub-agent W1409 (re-audit)

---

## W1274 Merge State

W1274 split into two PRs, **neither merged** into `codex/krab-ear-v2` as of 2026-05-27:

| PR | Wave | Title | State |
|----|------|-------|-------|
| #1176 | W1273 | `recording_comparison min-2 + recording_insights privacy gate` | **OPEN** |
| #1178 | W1274 | `wire get_daily_insight IPC + topic tokens cap` | **OPEN** |

Consequence: the current `codex/krab-ear-v2` branch has neither the `privacy_mode_enabled` guard on `_handle_get_recording_insights` (service.py line 2859) nor `get_daily_insight` wired as an IPC handler.

---

## Findings (5 NEW)

### F1 — CRIT: `_handle_get_recording_insights` exposes transcript text in privacy mode (W1273 not merged)

**File:** `KrabEar/backend/service.py` line 2859  
**Severity:** HIGH (matches W1267 F2 — fix is in PR #1176, still OPEN)

The handler loads all items and passes them through six heuristic analyzers — including `_compute_most_discussed_topic` which tokenizes transcript text and `_compute_speaking_pace_change` which reads raw `text` length. No `privacy_mode_enabled` check exists. Calling `get_recording_insights` while `privacy_mode_enabled=True` processes transcript content against the user's intent.

Three other export-style handlers (`_handle_export_timeline_svg`, two others) already have the guard at lines 3656, 3703, 3748. The pattern is established; the gap is an unmerged PR.

**Fix:** Add to `_handle_get_recording_insights` before the store lock:
```python
settings = self._cached_settings()
if settings.get("privacy_mode_enabled"):
    return {"ok": True, "insights": [], "skipped": "privacy_mode"}
```

---

### F2 — MED: `get_daily_insight` IPC handler not wired (W1274 not merged)

**File:** `KrabEar/backend/service.py` handler table (line ~1055)  
**Severity:** MEDIUM

`RecordingInsightsGenerator.get_daily_insight()` exists and is fully tested, but there is no `"get_daily_insight"` entry in the `handle_request` dispatch table. The method is unreachable from Swift / any IPC client. PR #1178 adds this wiring.

Verified by `grep -n "get_daily_insight" KrabEar/backend/service.py` → no output.

---

### F3 — MED: `_compute_most_discussed_topic` accumulates unbounded token list — memory spike on large history

**File:** `KrabEar/backend/recording_insights.py` line 499  
**Severity:** MEDIUM

`all_tokens: list[str] = []` is extended for every item without a cap. Measured:

| Input | Time | Peak RAM |
|-------|------|----------|
| 1 000 items × 1 000 words | 675 ms | **74.6 MB** |
| 100 000 items × 6 words | 376 ms | ~6 MB |

A user with a long dictation history where individual recordings are verbose can trigger a RAM spike purely from tokenization. The fix (a per-item token cap, e.g. 500 tokens) is described in PR #1178 (W1274) but not yet merged.

The call site in `generate_insights()` passes `recent` (window-filtered), so in practice this runs only on the last `days` worth of items — limiting exposure to active users. Still, a 7-day burst of long dictation sessions can spike to tens of MB in this call alone.

---

### F4 — LOW: `_compute_language_shift` silently skips new-language adoption (false negative)

**File:** `KrabEar/backend/recording_insights.py` line 344  
**Severity:** LOW

The change-percent calculation skips any `lang` where `prev == 0.0`:

```python
if prev == 0.0:
    continue
```

This means if a user starts using a brand-new language that was entirely absent in the previous window, the shift is not reported. The intent was to avoid division-by-zero and infinite-percent false positives, but the guard is too broad: a language that was 0% → 40% of recordings is a meaningful shift worth surfacing.

An alternative: treat `prev==0.0` as a new-adoption event with a fixed confidence of 0.5 and a distinct title ("Новый язык — …").

No test covers this scenario. The adjacent test `test_language_shift_detected_when_lang_grows` always starts with non-zero prev.

---

### F5 — LOW: `_compute_recording_streak` receives ALL history items, not the window-filtered `recent` list — `total_days_with_recordings` is misleading

**File:** `KrabEar/backend/recording_insights.py` line 217, `generate_insights()` line 217  
**Severity:** LOW

`generate_insights()` passes the unfiltered `items` list (full history) to `_compute_recording_streak()`. This means:

1. `total_days_with_recordings` in the returned `data` dict counts all historical days, not just those within the `days` window. A user with 2 years of history gets `total_days_with_recordings: 365+` even for a `days=7` query.
2. The streak itself is measured correctly from today/yesterday backward and is unaffected by the window.

By contrast `_compute_peak_productivity` and `_compute_most_discussed_topic` receive `recent` (window-filtered). The inconsistency can confuse callers interpreting `total_days_with_recordings` as "days in the requested window".

**Fix:** Replace `total_days_with_recordings` with `total_days_in_window` counted only from `dates` intersected with the `days` window, or add a separate `all_time_days_with_recordings` key.

---

## W1369 Interaction Check

`RecordingInsightsGenerator` and `SentimentTrendAnalyzer` are fully independent: no imports cross between the two modules, and the IPC handlers (`get_recording_insights` vs `get_sentiment_trends`) return disjoint response schemas. W1369 (PR #1278, OPEN) fixes sentiment schema consistency but has zero interaction with recording insights output.

---

## Existing Coverage Assessment (post-W1274 audit)

The test file `KrabEar/tests/test_recording_insights.py` covers:
- All 6 insight types with happy-path and data-field assertions
- Empty/too-few-items guards
- Thread-safety (concurrent `generate_insights`)
- `MockHistoryItem` object-attribute compatibility
- Large dataset smoke test (100 items)

Gaps (not covered by existing tests):
- Privacy mode gate (blocked by PR #1176 not merged)
- `get_daily_insight` IPC wiring (blocked by PR #1178 not merged)
- New-language adoption case for `language_shift` (F4)
- `total_days_with_recordings` reflects full history, not window (F5)

---

## Summary

| ID | Severity | Status | Blocker |
|----|----------|--------|---------|
| F1 | HIGH | Unmerged fix (PR #1176) | merge train |
| F2 | MED | Unmerged fix (PR #1178) | merge train |
| F3 | MED | No fix yet | needs per-item token cap |
| F4 | LOW | No fix yet | behavioral gap |
| F5 | LOW | No fix yet | cosmetic/schema |

W1274 merge state: **NOT MERGED** (both PRs #1176 and #1178 are OPEN).
