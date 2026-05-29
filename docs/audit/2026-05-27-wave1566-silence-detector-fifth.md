# W1566 — silence_detector.py Fifth-Pass Audit (post-W1531)

**Date:** 2026-05-27
**Auditor:** W1566 (fifth-pass, read-only)
**File:** `KrabEar/core/silence_detector.py`
**Baseline:** W1531 merged (two-tier threshold restoration: `SILENCE_THRESHOLD_DB_STRICT` + `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER` confirmed present)
**Status:** 5 new findings

---

## W1531 Verification

Both constants confirmed present and correct:

```
SILENCE_THRESHOLD_DB_STRICT: float = -40.0
SILENCE_THRESHOLD_DB_PRESERVE_WHISPER: float = -55.0
SILENCE_THRESHOLD_DB: float = SILENCE_THRESHOLD_DB_STRICT  # backward-compat alias
SILENCE_THRESHOLD_AMP: float = _db_to_amplitude(SILENCE_THRESHOLD_DB)  # computed 0.01
```

No regression from W1497 cherry-pick train on the two-tier split itself.

---

## Findings

### F1 — MEDIUM | Dead statement `audio.shape` survives in `trim_silence` (line 157)

**File:** `KrabEar/core/silence_detector.py`, line 157

```python
def trim_silence(self, audio, sample_rate, threshold_db=-40.0, min_silence_sec=0.5):
    audio.shape          # ← bare expression: evaluates .shape tuple, discards result
    mono = self._to_mono(audio)
```

**Issue:** `audio.shape` on line 157 is a dead standalone expression — it evaluates the shape tuple and immediately discards it. This was identified as F1 in the W1016 audit. W1016 added a behavioral test (`test_trim_silence_dead_statement_removed`) but that test verifies only that the method works on 2D input, NOT that the dead line is absent. The dead line itself was not removed.

This causes a flake8 `W0104` (pointless statement) and misleads maintainers into thinking `audio.shape` may have a side effect or assertion purpose.

**Fix:** Remove line 157: `audio.shape`.

**Test coverage gap:** Add an AST/source-level test that asserts the dead statement is not in the source, similar to the `test_no_minus_40_literal_in_silence_detector` pattern used in `test_shared_silence_threshold_W1333.py`.

---

### F2 — MEDIUM | Identical return branches in `trim_silence` (lines 197–199)

**File:** `KrabEar/core/silence_detector.py`, lines 197–199

```python
if audio.ndim > 1:
    return audio[start_sample:end_sample]   # branch A
return audio[start_sample:end_sample]       # branch B (identical)
```

**Issue:** Both return statements are textually identical. The condition `audio.ndim > 1` is dead — it provides no differentiation in behavior. The original intent was likely either:

- (a) `return audio[start_sample:end_sample].reshape(-1, audio.shape[1])` for multichannel to ensure column layout is preserved, or
- (b) the condition was left over from a refactor that simplified both branches to the same expression.

Currently the `if` branch is unreachable in effect, and readers cannot tell whether this was intentional. The dead `audio.shape` statement on line 157 reinforces the hypothesis that a reshape was originally planned but abandoned.

**Impact:** No behavioral bug today (numpy slice preserves shape for 2D arrays). But the redundant condition adds cognitive load and may mask future regressions if someone modifies one branch but not the other.

**Fix:** Collapse to a single return:
```python
return audio[start_sample:end_sample]
```

---

### F3 — LOW | Split SSOT: `silence_constants.py` and `silence_detector.py` both define `SILENCE_THRESHOLD_AMP` independently

**Files:**
- `KrabEar/core/silence_detector.py` line 53: `SILENCE_THRESHOLD_AMP: float = _db_to_amplitude(SILENCE_THRESHOLD_DB)` (computed)
- `KrabEar/core/silence_constants.py` line 33: `SILENCE_THRESHOLD_AMP: float = 0.01  # hardcoded`

**Issue:** `silence_constants.py` was introduced as the canonical SSOT for all threshold constants (W1018). However, `silence_detector.py` still independently defines `SILENCE_THRESHOLD_AMP` by computing it via `_db_to_amplitude`. Consumers are split:

| Module | Imports from |
|--------|-------------|
| `core/noise_profiler.py` | `silence_detector` |
| `core/audio_quality.py` | `silence_detector` |
| `core/audio_chunker.py` | `silence_constants` |
| `backend/realtime_silence_filter.py` | `silence_constants` |
| `core/smart_silence_skipper.py` | `silence_detector` (for `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`) |

The two `SILENCE_THRESHOLD_AMP` definitions agree numerically today (both equal 0.01). But if `SILENCE_THRESHOLD_DB_STRICT` is ever changed in `silence_detector.py`, the `_db_to_amplitude` computed value updates automatically, while `silence_constants.py`'s hardcoded `0.01` does not, silently diverging.

**Fix:** In `silence_detector.py`, replace the independent computation with an import from `silence_constants`:
```python
from core.silence_constants import (
    SILENCE_THRESHOLD_DB,
    SILENCE_THRESHOLD_DB_STRICT,
    SILENCE_THRESHOLD_DB_PRESERVE_WHISPER,
    SILENCE_THRESHOLD_AMP,
)
```
Retain `_db_to_amplitude` as a local helper only (it is used inline for `threshold_db` parameter conversion).

---

### F4 — LOW | `frame_rms` computed via Python list comprehension in all three public methods (no vectorization)

**File:** `KrabEar/core/silence_detector.py`, lines 102–105, 170–173, 226–229

All three public methods (`detect_silence`, `trim_silence`, `get_speech_ratio`) contain an identical Python loop:

```python
frame_rms = np.array([
    float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) if len(f) > 0 else 0.0
    for f in frames
])
```

**Issue:** `np.array_split` returns a Python list of numpy sub-arrays, and the list comprehension iterates in Python, calling numpy on each small sub-array. For long audio (30-minute call at 16 kHz = 28.8M samples → ~56,250 frames), three such calls produce ~168,750 Python-level numpy calls. This is O(n_frames) Python overhead, not O(1) numpy vectorized work.

**Vectorized alternative:** Pad-and-reshape to a 2D matrix:
```python
# Pad audio to an exact multiple of _FRAME_SIZE, then reshape:
padded = np.pad(audio, (0, (-n_samples) % _FRAME_SIZE))
matrix = padded.reshape(-1, _FRAME_SIZE).astype(np.float64)
frame_rms = np.sqrt(np.mean(matrix ** 2, axis=1))
```
This is a single numpy operation with no Python-level loop.

**Priority:** Low — STT paths are bottlenecked on Whisper GPU inference, not RMS computation. However, the same pattern appears 3× and is a maintenance pain point.

---

### F5 — LOW | Missing test: `trim_silence` multichannel output shape preservation with both-ends trim

**File:** `KrabEar/tests/test_silence_detector.py`

**Issue:** `TestTrimSilence.test_multichannel_audio` (line 180) only checks that `len(trimmed) < len(stereo)` — it does not assert `trimmed.ndim == 2`. The only test that verifies `ndim == 2` output is `TestThresholdConstants.test_trim_silence_dead_statement_removed`, but that test only trims the leading end (silence + speech, no trailing silence). There is no test covering the case where both leading and trailing silence are trimmed from a multichannel array, specifically verifying that `trimmed.shape == (expected_samples, n_channels)`.

**Impact:** A future refactor that accidentally collapses multichannel to 1D on trimming (e.g., slicing on `mono` instead of `audio`) would not be caught by any existing test.

**Fix:** Add test:
```python
def test_multichannel_trim_both_ends_preserves_ndim(self):
    mono = _concat(_make_silence(0.5), _make_speech(1.0), _make_silence(0.5))
    stereo = np.stack([mono, mono], axis=1)
    trimmed = self.detector.trim_silence(stereo, SAMPLE_RATE, min_silence_sec=0.3)
    self.assertLess(len(trimmed), len(stereo))
    self.assertEqual(trimmed.ndim, 2, "trim_silence must preserve 2D shape for multichannel")
    self.assertEqual(trimmed.shape[1], 2, "channel count must be preserved")
```

---

## Summary Table

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| F1 | MEDIUM | `silence_detector.py:157` | Dead `audio.shape` statement not removed despite W1016 flag |
| F2 | MEDIUM | `silence_detector.py:197-199` | Identical `if audio.ndim > 1` return branches — dead condition |
| F3 | LOW | `silence_detector.py:53` vs `silence_constants.py:33` | Split SSOT: `SILENCE_THRESHOLD_AMP` defined twice (computed vs hardcoded) |
| F4 | LOW | `silence_detector.py:102,170,226` | Python-loop RMS computation not vectorized (3× duplicated) |
| F5 | LOW | `tests/test_silence_detector.py` | Missing multichannel both-ends trim `ndim==2` assertion |

---

## Scope of Prior Waves

| Wave | Fix | Status |
|------|-----|--------|
| W1016 | Two-tier threshold design, `audio.shape` flagged as F1 | Test added, dead line NOT removed → **F1 persists** |
| W1018 | `silence_constants.py` SSOT created | Mixed import pattern remains → **F3 persists** |
| W1531 | Two-tier restoration after W1497 regression | Both constants confirmed present |

No prior wave addressed F2 (dead ndim branch), F4 (vectorization), or F5 (test gap).
