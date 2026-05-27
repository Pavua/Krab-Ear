# Audit W1360: sentiment_trends.py residual re-audit (post W1295)

**Date:** 2026-05-27
**Branch:** audit/sentiment-trends-residual-W1360
**Files audited:**
- `KrabEar/backend/sentiment_trends.py` — `SentimentTrendAnalyzer`
- `KrabEar/core/emotion_detector.py` — `EmotionDetector` (W1020 fix scope)
- `KrabEar/backend/service.py:2869` — `_handle_get_sentiment_trends` IPC handler

---

## W1295 merge state

**NOT MERGED into `codex/krab-ear-v2`.**

Commit `44604801` (`fix(wave1295): sentiment_trends privacy_mode gate + local timezone date bucketing`)
exists only on branch `fix/fix-sentiment-privacy-W1295`. The `codex/krab-ear-v2` main branch
(which this worktree tracks) does not contain W1295. The handler at `service.py:2869` has NO
`privacy_mode_enabled` guard and NO `astimezone().date()` bucketing — both are missing in production.

This means W1289 F1 (privacy gate) and F4 (UTC date bucketing) are still open bugs in production.

---

## Findings (5 NEW residual issues)

### F1 — HIGH | W1295 not merged: privacy_mode gate and local-TZ bucketing absent in production

**File:** `KrabEar/backend/service.py:2869–2878`
**Status:** REGRESSION — both W1295 fixes absent on main branch

The current production handler:

```python
def _handle_get_sentiment_trends(self, params: dict[str, Any]) -> dict[str, Any]:
    days = int(params.get("days", 30))
    try:
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
    except Exception:
        items = []
    report = self._sentiment_trends.analyze_sentiment_trends(items, days=days)
    return self._sentiment_trends.to_dict(report)
```

has NO `privacy_mode_enabled` guard. Every call runs `EmotionDetector.detect()` on the full
transcript text of each history item — contrary to privacy mode intent.

The `sentiment_trends.py` module itself still uses `ts.date()` (UTC) for day bucketing, not
`ts.astimezone().date()` — W1295 only patched this on its own branch.

**Fix:** Merge `fix/fix-sentiment-privacy-W1295` into `codex/krab-ear-v2` (or cherry-pick the
two commits). Both fixes were already reviewed and tested on that branch (8 tests pass).

---

### F2 — MEDIUM | W1295 privacy response schema diverges from normal response

**File:** `KrabEar/backend/service.py:2871–2872` (W1295 branch)
**Location:** `_handle_get_sentiment_trends` privacy gate return value

The W1295 privacy gate returns:

```json
{"ok": true, "trends": [], "skipped": "privacy_mode"}
```

The normal response (via `SentimentTrendAnalyzer.to_dict`) returns:

```json
{
  "daily_sentiment": [...],
  "overall_sentiment": 0.0,
  "sentiment_distribution": {...},
  "mood_trend": "stable",
  "most_positive_day": {},
  "most_negative_day": {}
}
```

Schema divergence: the privacy response uses `"trends"` (empty list), but the normal response
uses `"daily_sentiment"`. Any Swift client reading `result["daily_sentiment"]` will crash with a
KeyError / nil-access when `skipped == "privacy_mode"` is returned, because the key is absent.
Additionally, the normal response has no `"ok"` key (the IPC envelope adds it, but the raw
`to_dict` output doesn't), making client-side schema checks inconsistent.

**Fix:** Align privacy gate response to use `"daily_sentiment": []` (matching the normal schema)
and drop `"trends"`. Add `"skipped": "privacy_mode"` as an additional field in the same envelope
— do not replace the standard keys.

Corrected privacy response:

```python
return {
    "daily_sentiment": [],
    "overall_sentiment": 0.0,
    "sentiment_distribution": {"positive": 0, "negative": 0, "neutral": 0},
    "mood_trend": "stable",
    "most_positive_day": {},
    "most_negative_day": {},
    "skipped": "privacy_mode",
}
```

No test currently verifies that the privacy response and normal response share the same schema
keys — this is a test gap introduced by W1295.

---

### F3 — MEDIUM | W1020 negation fix residual: phrase "не нравится" is dead code in tokenizer

**File:** `KrabEar/core/emotion_detector.py:26`
**Location:** `_NEGATIVE_WORDS["ru"]` list + `_match_words()` tokenization

Note: W1020 (`fix-emotion-detector-negation-W1020`, PR #941) is also NOT merged into
`codex/krab-ear-v2`. In the current production code, `"не"` and `"нет"` are still in
`_NEGATIVE_WORDS["ru"]` (not yet dropped by W1020). However, even in the W1020-patched version
on its branch, `"не нравится"` as a multi-word phrase in `_NEGATIVE_WORDS["ru"]` remains dead
code because `_match_words()` operates on individual word tokens produced by:
produced by:

```python
_RE_WORD_TOKENS = re.compile(r"[А-Яа-яёЁA-Za-zÀ-ÿ]+")
```

This regex can only match **single words** — multi-word phrases are split into separate tokens.
The string `"не нравится"` is therefore **never matched** by `_match_words()`.

Verified empirically:

```python
EmotionDetector().detect("мне не нравится", language="ru")
# → EmotionResult(primary_emotion="positive", ...)  # WRONG, should be negative
```

The actual token-level behavior:
- `"не"` → hits `_NEGATIVE_WORDS["ru"]` (single-word entry, still present)
- `"нравится"` → hits `_POSITIVE_WORDS["ru"]` (single-word entry)
- Both scores equal → Python dict tie-breaking: `"positive"` key is iterated before
  `"negative"` → `max()` returns `"positive"`

The phrase entry `"не нравится"` in `_NEGATIVE_WORDS["ru"]` is dead code and provides no
protection.

**Fix:** Remove the dead phrase entry `"не нравится"` from `_NEGATIVE_WORDS["ru"]` (it is
unreachable). To correctly classify "не нравится" as negative, either:
(a) Add phrase-level detection in `detect()` using regex on the raw text before tokenization, or
(b) Accept the limitation and remove the misleading dead entry.

Option (b) is the minimal safe fix. Option (a) requires a more significant change to the
detection loop.

No test currently exercises the `"мне не нравится"` case and verifies the expected `"negative"`
outcome — this is a test gap.

---

### F4 — LOW | 2-point linear regression produces slope of magnitude (v1−v0), not per-day slope

**File:** `KrabEar/backend/sentiment_trends.py:237–252`
**Location:** `_linear_regression_slope()` with `n=2`

For exactly 2 data points, the OLS formula reduces to:
`slope = (v1 − v0)` (the full difference, not a per-day rate).

With the trend threshold `_SLOPE_IMPROVING = 0.005` and `_SLOPE_DECLINING = -0.005`, any two-day
window where today's sentiment differs from yesterday's by more than 0.005 (essentially always,
since `_EMOTION_SCORE` values are in `{-0.9, -0.7, -0.1, 0.0, 0.7, 0.9}`) will be classified
as either "improving" or "declining" — never "stable".

Example: 2 days with emotions `["neutral", "positive"]` → scores `[0.0, 0.7]` → slope = 0.7,
classified as "improving". This is a valid trend signal for 2 days. However, for users who
record only 2 days in a 30-day window, the "improving/declining" classification has essentially
zero statistical significance (R² = 1.0 by definition with only 2 points).

The existing threshold was calibrated for multi-point data. With 2 points, **every non-equal
pair produces a strong trend signal**, which can mislead the analytics UI.

**Fix (optional):** Apply a minimum data point guard: if `n < 3`, classify as `"stable"` by
default. This prevents spurious trend labels when data is too sparse for meaningful regression.

No test currently covers the exact 2-point boundary behavior or documents the expected
classification for 2-point data.

---

### F5 — LOW | No `days` validation: negative or zero values cause empty/incorrect results

**File:** `KrabEar/backend/service.py:2873` + `KrabEar/backend/sentiment_trends.py:81`
**Location:** `_handle_get_sentiment_trends` params parsing + `analyze_sentiment_trends` cutoff

The handler does:

```python
days = int(params.get("days", 30))
```

No bounds check. If a caller passes `days=0` or `days=-1`, the cutoff becomes:
```python
cutoff = datetime.now(timezone.utc) - timedelta(days=0)   # = now
cutoff = datetime.now(timezone.utc) - timedelta(days=-1)  # = tomorrow
```

With `days=0`, all items have `ts < cutoff` and the result is an empty report with
`mood_trend="stable"` — silently hiding all data. With `days=-1`, cutoff is in the future,
so again all items are excluded.

These are silent failures — no error is raised, no warning is logged.

The W1295 branch preserves this: the handler still does unchecked `int(params.get("days", 30))`.

**Fix:** Add `days = max(1, days)` guard in the handler (or raise `ValueError` for `days < 1`).
Add test covering `days=0` and `days=-1` behavior.

---

## Wire status

| IPC method | Handler | Service | Status (codex/krab-ear-v2) |
|---|---|---|---|
| `get_sentiment_trends` | `_handle_get_sentiment_trends` | `BackendService` (service.py:2869) | WIRED, NO PRIVACY GATE |

W1295 branch has privacy gate wired but is NOT merged.

---

## Regression correctness (post-W1295 branch)

OLS slope formula in `_linear_regression_slope()` is mathematically correct for all `n >= 2`.
Edge cases handled: `n < 2 → 0.0`, denominator = 0 → 0.0 (all-same values). F4 above is
a correctness concern about statistical significance at `n=2`, not a formula error.

---

## Test coverage gaps (post-W1295)

| Gap | Covered by existing tests |
|---|---|
| W1295 NOT merged — privacy gate absent in codex/krab-ear-v2 | NO (W1295 tests only run on fix/ branch) |
| Privacy response schema vs normal schema key consistency | NO |
| "не нравится" → negative classification | NO |
| 2-point regression → stable (or not) | NO |
| `days=0` or `days=-1` → silent empty result | NO |
