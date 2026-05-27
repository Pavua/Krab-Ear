# Wave 1010 — ConfidenceCalibrator Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/confidence_calibrator.py`  
**Tests:** `KrabEar/tests/test_confidence_calibrator.py` + `test_property_based.py`

---

## Summary

`ConfidenceCalibrator` applies empirical additive adjustments to raw Whisper
`avg_logprob`-derived confidence scores. The implementation is simple, correct,
and well-tested for its stated scope. Five findings are documented below:
three medium-severity design gaps and two low-severity issues.

---

## Finding 1 — MEDIUM: NaN raw_confidence silently maps to 1.0

**Source:** `engine.py:1011`
```python
confidence = float(np.mean([np.exp(s.get("avg_logprob", -1.0)) for s in segments]))
```
If a Whisper segment returns `avg_logprob = NaN` (observed with corrupted audio
or empty segments in some mlx-whisper versions), `np.exp(NaN)` → `NaN`,
`np.mean([NaN])` → `NaN`, `float(NaN)` → `nan` Python float. Inside
`calibrate_detailed`, the expression `max(0.0, min(1.0, nan + delta))` silently
maps to `1.0` (Python's `min`/`max` contract with NaN returns the non-NaN
operand), making the transcript appear maximally confident regardless of actual
quality.

**Reproduction (pure Python):**
```python
>>> x = float('nan')
>>> max(0.0, min(1.0, x + 0.05))
1.0
```

**Fix:** Add a NaN/Inf guard at the top of `calibrate_detailed`:
```python
import math
if not math.isfinite(raw_confidence):
    raw_confidence = 0.0
```

**Test gap:** No existing test passes `float('nan')` or `float('inf')` as
`raw_confidence`. The edge-case class (`TestCalibratorEdgeCases`) and the
out-of-range class (`TestCalibratorOutOfRangeRaw`) only cover numeric values
in `[-5.0, 5.0]`.

---

## Finding 2 — MEDIUM: Long-recording "boost" direction is counterintuitive

**Source:** `confidence_calibrator.py:23-24`
```python
_LONG_DURATION_THRESHOLD = 60.0   # > 60s — ухудшение качества
_LONG_BOOST = +0.05               # +5% для длинных записей
```

The comment says *"quality degrades"* for recordings > 60 s, yet the constant
applies a **positive** +5% adjustment. Whisper's well-known failure mode on
long audio is **hallucination accumulation** (repetition loops, timestamp
drift), which degrades quality, not improves it. A boost here inflates
confidence for the inputs most likely to contain hallucinations.

The likely intent was to reward transcriptions with more acoustic data, but the
effect is the opposite of what the comment implies. Without empirical calibration
data from real recordings, the safe default would be zero adjustment (neutral) or
a small negative penalty symmetric with the short-recording penalty.

**Recommendation:** Remove `_LONG_BOOST` or invert it to a small penalty
(e.g., `-0.03`) until measurement data justifies a positive adjustment.

---

## Finding 3 — MEDIUM: No engine-specific calibration (GigaAM vs Whisper vs SenseVoice)

**Source:** `calibrate_detailed` model-matching logic (lines 111-117)

The calibrator only distinguishes `"balanced"` (via substring match) from
everything else. GigaAM-RNNT, SenseVoice, Parakeet-TDT, WhisperX, and Voxtral
all pass through with zero model correction. These adapters report their own
confidence scale:

- **GigaAM** returns `confidence` from its own adapter — a CTC/RNNT posterior
  score whose distribution differs substantially from Whisper's `exp(avg_logprob)`.
- **SenseVoice** returns `funasr` confidence — UFAL-style normalized posterior.
- **Parakeet-TDT** (NVIDIA NeMo) uses CTC token posteriors.

In practice the `model_used` field set on the adapter result (e.g.,
`"gigaam-rnnt"`) contains none of the word `"balanced"`, so no adjustment is
applied. The calibrated value is then used for confidence-driven multipass retry
threshold comparison (`STT_CONFIDENCE_THRESHOLD` in `engine.py:1262`), so
incorrect calibration on non-Whisper adapters may suppress or incorrectly trigger
retries.

**Recommendation:** Add named engine branches:
```python
ENGINE_PENALTIES = {
    "gigaam": 0.0,      # GigaAM posteriors already well-scaled for RU
    "sensevoice": -0.05, # FunASR over-confident on non-clean audio
    "parakeet": 0.0,
}
```
Even a documented no-op entry makes the gap explicit and easier to calibrate
with real measurements.

---

## Finding 4 — LOW: `transcribe_chunked` bypasses calibration entirely

**Source:** `engine.py:1586-1598` (return from `transcribe_chunked`)
```python
"confidence": round(avg_confidence, 3),
"raw_confidence": round(avg_confidence, 3),
"confidence_adjustments": [],
```

The `transcribe_chunked` path (used for long audio splits) computes
`avg_confidence` from per-chunk results and returns it directly — skipping
`_confidence_calibrator.calibrate_detailed`. The `confidence_adjustments` field
is always an empty list.

For a 120-second chunked recording this means: the long-duration boost (Finding
2, +5%) is never applied, the non-primary-language penalty is never applied, and
the `confidence_adjustments` audit trail is absent. The chunked path is called
by GigaAM's internal long-form handler as well (`_transcribe_gigaam` lines
2503+), so GigaAM on audio > 30 s is doubly uncalibrated.

**Recommendation:** Call `_confidence_calibrator.calibrate_detailed` on
`avg_confidence` before returning from `transcribe_chunked`.

---

## Finding 5 — LOW: `None` raw_confidence raises unhandled TypeError

**Source:** `calibrate_detailed` line 119
```python
calibrated = max(0.0, min(1.0, raw_confidence + delta))
```

If `raw_confidence=None` is passed (possible if a caller passes
`result.get("confidence")` on a dict that has an explicit `None` value),
`None + delta` raises `TypeError: unsupported operand type(s) for +: 'NoneType'
and 'float'`. The exception would propagate out of `transcribe()` and surface
as a crash.

Engine code guards against this with `result.get("confidence", 0.0)`, but the
calibrator itself has no defensive check, making it fragile to any future caller
that does not pre-validate.

**Fix:** Coerce at entry: `raw_confidence = float(raw_confidence or 0.0)`.

---

## Wire Status

The calibrator is correctly wired into the main `transcribe()` path
(`engine.py:1017`) and is instantiated once per `AudioEngine`. It is **not**
wired into:

- `transcribe_chunked()` — returns raw avg confidence with empty adjustments.
- `_maybe_multipass_retry()` — reads `result["confidence"]` which comes from
  the adapter result before calibration is applied (multipass uses the
  pre-calibrated value from `result`, not the post-calibrated value).

---

## Test Coverage

| Area | Status |
|------|--------|
| Short duration penalty | Covered |
| Long duration boost | Covered |
| Primary/non-primary language | Covered |
| Balanced model penalty | Covered |
| Clamping [0, 1] | Covered |
| Monotonicity | Covered (property test) |
| Thread safety | Covered |
| Stats / reset | Covered |
| `NaN` raw_confidence | **MISSING** |
| `None` raw_confidence | **MISSING** |
| `float('inf')` | **MISSING** |
| Engine-specific calibration | N/A (not implemented) |
| `transcribe_chunked` calibration | N/A (not wired) |

Test count: ~40 test methods across 9 classes in `test_confidence_calibrator.py`
plus 3 property tests in `test_property_based.py`. Coverage of the existing
logic is thorough. The gaps are all in unimplemented or unguarded paths.

---

## Recommendations (priority order)

1. **[MEDIUM]** Add `math.isfinite` guard for NaN/Inf inputs (Finding 1).
2. **[MEDIUM]** Remove or invert `_LONG_BOOST` (Finding 2).
3. **[MEDIUM]** Add engine-type branches for GigaAM/SenseVoice (Finding 3).
4. **[LOW]** Wire calibration into `transcribe_chunked` return path (Finding 4).
5. **[LOW]** Add `None` coercion guard at calibrator entry (Finding 5).
