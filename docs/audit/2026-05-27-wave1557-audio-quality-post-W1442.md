# audio_quality post-W1442 audit — residual findings (W1557)

**Date:** 2026-05-27
**Auditor:** W1557 sub-agent
**Scope:** `KrabEar/core/audio_quality.py` post-W1442 production state
**Branch:** codex/krab-ear-v2 @ HEAD (70463af9)

---

## W1442 Merge Verification — CONFIRMED OK

W1442 (PR #1401) is confirmed merged. `_safe_float` helper is defined at line 34 and wraps
all 5 numpy output values in `analyze()`:

| Line | Field | Expression |
|------|-------|-----------|
| 138 | `rms_level` | `_safe_float(float(np.sqrt(np.mean(...))))` |
| 141 | `peak_level` | `_safe_float(float(np.max(np.abs(...))))` |
| 147 | `clipping_ratio` | `_safe_float(clipping_samples / max(n_samples, 1))` |
| 150 | `silence_ratio` | `_safe_float(self._compute_silence_ratio(...))` |
| 153 | `snr_estimate_db` | `_safe_float(self._estimate_snr(...))` |

All 5 produce finite Python `float` values on degenerate (all-NaN, all-Inf, empty) input.
`duration_sec = n_samples / max(sample_rate, 1)` is not wrapped and does not need to be —
integer/integer division with denominator guard; result is always finite.

---

## Findings (5 total)

### F1 — LOW: `_safe_float` isinstance guard silently coerces `np.float32` to default

**File:** `KrabEar/core/audio_quality.py:36`
**Severity:** LOW (latent footgun; current callers unaffected)

`_safe_float` uses `isinstance(v, (int, float))` as its type guard. In Python 3,
`numpy.float64` is a subclass of `float` (passes), but `numpy.float32` is NOT (fails →
returns `default` silently). Verified:

```python
>>> isinstance(np.float32(3.14), float)  # False — returns 0.0, not 3.14!
>>> isinstance(np.float64(3.14), float)  # True — correct
```

All five current callsites explicitly wrap with `float()` before passing to `_safe_float`
(lines 138–153), so production behaviour is correct today. However any future caller passing
a raw `np.float32` scalar directly would silently get `0.0` instead of the actual value
with no error raised.

**Recommendation:** Add `numpy.floating` to the isinstance check:
```python
try:
    import numpy as np
    _NUMERIC_TYPES = (int, float, np.floating)
except ImportError:
    _NUMERIC_TYPES = (int, float)
```
Or simply: `isinstance(v, (int, float)) or hasattr(v, '__float__')`.

---

### F2 — LOW (SEMANTIC): `_compute_silence_ratio` returns `0.0` for all-NaN audio

**File:** `KrabEar/core/audio_quality.py:196–200`
**Severity:** LOW (no JSON safety risk; quality_score is still `poor`)

`_compute_silence_ratio` counts frames where `np.sqrt(np.mean(f**2)) < _SILENCE_RMS_THRESHOLD`.
For a frame containing only `NaN`, `np.sqrt(np.mean(NaN**2)) = NaN`, and `NaN < 0.01` is
`False` in Python — so NaN frames are never counted as silent. Result: all-NaN audio
returns `silence_ratio = 0.0` instead of the semantically correct `1.0`.

This is not a JSON safety issue (`0.0` is valid JSON). The `quality_score` is still correctly
`"poor"` because `rms_level = 0.0 < 1e-6` triggers the degenerate guard in `_score()`.

**Verified at runtime:**
```python
nan_audio = np.array([np.nan] * 16000, dtype=np.float32)
r = AudioQualityAnalyzer().analyze(nan_audio, 16000)
# r.silence_ratio = 0.0  (not 1.0 as expected for unusable audio)
# r.quality_score = "poor"  (correctly degraded via rms_level check)
```

**Recommendation (optional):** Add a NaN-check inside `_compute_silence_ratio`:
```python
if np.any(np.isnan(audio)):
    return 1.0
```

---

### F3 — CONFIRMED OK: W1510 SNR threshold decoupling intact

**File:** `KrabEar/core/audio_quality.py:56`

`_SNR_NOISE_FLOOR_THRESHOLD = 0.01` is hardcoded, correctly decoupled from
`_SILENCE_RMS_THRESHOLD` as intended by W1510. Even if `SILENCE_THRESHOLD_AMP` were to
change again (as happened in W1477 when it moved from `0.001` to `0.01`), the SNR noise
floor threshold stays fixed at `0.01`. The W1503 regression (all clean-signal frames
classified as noise floor → SNR=0 → score="poor") cannot recur.

Both constants currently equal `0.01`, but their independence ensures future-safety.

---

### F4 — CONFIRMED OK: W1531 silence tier constants not leaked into audio_quality

**File:** `KrabEar/core/silence_detector.py`, `KrabEar/core/audio_quality.py`

W1531 added two-tier silence thresholds: `SILENCE_THRESHOLD_DB_STRICT` (-40 dB, for analytics)
and `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` (-55 dB, for STT paths). `audio_quality.py` correctly
imports `SILENCE_THRESHOLD_AMP` (the STRICT/analytics tier) from `core.silence_detector`.
`SmartSilenceSkipper` correctly uses `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`. No coupling
regression found between the two tiers in the audio quality module.

---

### F5 — INFORMATIONAL: 5 `_safe_float` calls per `analyze()` — not a hot path

`analyze()` is a pre-flight check called once per audio file before STT, not in a tight
inference loop. 5 lightweight guard calls (each: `isinstance` + `math.isnan` + `math.isinf`
+ `float()`) add negligible overhead in this context. No performance concern.

---

## IPC Contract — Swift Caller Check

`grep -r "analyze_audio_quality\|AudioQuality\|rms_level\|peak_level" native/` returns no
results. The `analyze_audio_quality` IPC method is exposed by `AudioAnalyticsService` and
calls `analyze_file()` → `report.to_dict()`. No Swift caller was found parsing individual
fields as required keys. The dict always includes all 8 fields (`rms_level`, `peak_level`,
`snr_estimate_db`, `clipping_ratio`, `silence_ratio`, `duration_sec`, `quality_score`,
`warnings`).

---

## Summary

| Finding | Severity | Status | Action needed |
|---------|----------|--------|---------------|
| W1442 merged, 5 values wrapped | — | CONFIRMED OK | None |
| `_safe_float` `np.float32` footgun | LOW | Latent | Optional hardening |
| `_compute_silence_ratio` NaN semantic | LOW | Latent | Optional fix |
| W1510 SNR decoupling intact | — | CONFIRMED OK | None |
| W1531 silence tier isolation intact | — | CONFIRMED OK | None |
| Performance: 5 calls/analyze | — | INFO | None |

**Blocking issues: 0. Production state: healthy.**
