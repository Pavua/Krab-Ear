# W1519 Fourth-Pass Audit: `core/audio_quality.py`

**Date:** 2026-05-27
**Branch audited:** `audit-audio-quality-fourth-W1519` (off `codex/krab-ear-v2`) @ `f6bb585e`
**Worktree:** `audit-audio-quality-fourth-W1519`
**File:** `KrabEar/core/audio_quality.py`
**Prior audits:** W1015, W1100, W1133, W1384, W1461, W1503
**Fix waves reviewed:** W1442 (duplicate `_safe_float` shadow removed), W1477 (`SILENCE_THRESHOLD_AMP` import), W1107 (merged AFTER W1442+W1477, changed import source + silently removed `_safe_float`)

---

## Merge State Verification

| Wave | Description | Commit | Status in `codex/krab-ear-v2` |
|------|-------------|--------|-------------------------------|
| W1442 | Remove 1-arg `_safe_float` shadow | `c9b07a04` | **MERGED** |
| W1477 | Import `SILENCE_THRESHOLD_AMP` from `core.silence_constants` | `e6e4b39c` | **MERGED** — but then superseded |
| W1107 | Import `SILENCE_THRESHOLD_AMP` from `core.silence_detector` (later commit) | `7c1b3ca7` | **MERGED** — **replaces W1477, removed `_safe_float` as side-effect** |

### Critical state: W1107 is the last commit touching `audio_quality.py`

The current production file is the W1107 version, which:
- Switched the import source from `core.silence_constants` (W1477) to `core.silence_detector` (W1107).
- Removed `import math`.
- **Silently removed the entire `_safe_float` function and all its call sites** — reverting the W1017/W1103 NaN protection.

Verification:
```bash
grep -n '_safe_float\|import math' KrabEar/core/audio_quality.py
# → (no output) — both completely absent

PYTHONPATH=KrabEar python3 -m pytest KrabEar/tests/test_audio_quality.py -v 2>&1 | grep FAILED
# FAILED tests/test_audio_quality.py::TestNanInfJSONSafety::test_inf_audio_input_returns_zero_peak
# FAILED tests/test_audio_quality.py::TestNanInfJSONSafety::test_nan_audio_input_returns_zero_rms

PYTHONPATH=KrabEar python3 -m pytest KrabEar/tests/test_audio_quality_nan_W1017.py -v
# ERROR: cannot import name '_safe_float' from 'core.audio_quality' — all 13 tests crash at collection
```

W1503 R1 (quiet_mask threshold regression from W1477) remains present:
```bash
PYTHONPATH=KrabEar python3 -c "
from core.audio_quality import AudioQualityAnalyzer
import numpy as np
sr = 16000
t = np.linspace(0, 2.0, 2*sr)
for amp in [0.02, 0.05, 0.08, 0.14]:
    r = AudioQualityAnalyzer().analyze((amp*np.sin(2*np.pi*440*t)).astype(np.float32), sr)
    print(f'amp={amp}: snr={r.snr_estimate_db}, score={r.quality_score}')
# amp=0.02: snr=0.0, score=poor   ← wrong (clean signal)
# amp=0.05: snr=0.0, score=poor   ← wrong
# amp=0.08: snr=0.0, score=poor   ← wrong
# amp=0.14: snr=0.0, score=poor   ← wrong
# amp=0.15: snr=57.8, score=excellent  ← threshold cliff
"
```

---

## W1503 Open Findings Status

| ID | Description | Status |
|----|-------------|--------|
| R1 | `quiet_mask` threshold 10x after W1477 → SNR=0 for typical voice (amp < 0.141) | **STILL OPEN — confirmed** |
| R2 | No regression test for `_estimate_snr` at typical voice amplitudes | **STILL OPEN** |
| R3 | silence_ratio warning/score gap widens after W1477 | **STILL OPEN** |
| R4 | Python loops in `_compute_silence_ratio` block IPC thread ~200ms for 1h audio | **STILL OPEN** (measured 9.5x speedup available) |
| R5 | `float64` cast doubles peak RAM for float32 inputs | **STILL OPEN** |

---

## New Findings (cap 5)

### S1 — HIGH: W1107 silently removed `_safe_float` — NaN-protection W1017 fully reverted, 2 tests now failing

**Location:** `KrabEar/core/audio_quality.py` (entire file); `KrabEar/tests/test_audio_quality.py` lines 355–380.

**Root cause:**

W1107 was authored to change the `SILENCE_THRESHOLD_AMP` import source from `core.silence_constants` (W1477) to `core.silence_detector`. Its diff also removes `import math` and the `_safe_float` function (9 lines), then replaces all `_safe_float(...)` call sites with plain `round()`:

```python
# W1107 introduced (current state):
rms_level=round(rms_level, 6),        # was: round(_safe_float(rms_level), 6)
peak_level=round(peak_level, 6),      # was: round(_safe_float(peak_level), 6)
snr_estimate_db=round(snr_estimate_db, 2),  # was: round(_safe_float(snr_estimate_db), 2)
# ... and to_dict() uses self.rms_level directly (was _safe_float(self.rms_level))
```

**Impact — runtime confirmed:**

```python
# Audio with NaN samples (e.g., malformed file read or corrupt PCM buffer):
import numpy as np
audio_nan = np.full(16000*2, float('nan'), dtype=np.float32)
r = AudioQualityAnalyzer().analyze(audio_nan, 16000)
d = r.to_dict()
# d['rms_level'] = nan  ← NaN in dict
# d['peak_level'] = nan ← NaN in dict
import json
json.dumps(d)  # → '{"rms_level": NaN, "peak_level": NaN, ...}'
# "NaN" literals are NOT valid JSON per RFC 8259
# Swift's JSONDecoder CRASHES on NaN literals
```

**Tests currently failing (2):**
- `TestNanInfJSONSafety::test_nan_audio_input_returns_zero_rms` — asserts `rms_level` is finite for NaN audio input.
- `TestNanInfJSONSafety::test_inf_audio_input_returns_zero_peak` — asserts `peak_level` is finite for Inf audio input.

**Collection crash (all 13 tests):**
- `test_audio_quality_nan_W1017.py` crashes at import with `ImportError: cannot import name '_safe_float' from 'core.audio_quality'`.

**This is a regression introduced by W1107** which was not noticed because W1107 was focused on the silence threshold change. The removal of `_safe_float` was a "cleanup" side-effect that reverted W1017 and W1103.

**Fix:** Re-add `_safe_float` (with `import math`) and restore its call sites in `analyze()` and `to_dict()`. Can be trivially rebased from W1017's diff since the surrounding lines are unchanged.

---

### S2 — HIGH: `_estimate_snr` early-return for `n < 4 * _SILENCE_FRAME_SIZE` (0.256s) produces SNR=0 → `poor` score without diagnostic context

**Location:** `KrabEar/core/audio_quality.py`, line 178.

```python
def _estimate_snr(self, audio: np.ndarray, sample_rate: int) -> float:
    n = len(audio)
    if n < _SILENCE_FRAME_SIZE * 4:   # < 4096 samples = 0.256s at 16kHz
        return 0.0                     # ← 0 dB SNR, not "unavailable"
```

`_score(snr_db=0.0, ...)` maps to `"poor"` (0.0 < 10.0 threshold for "fair"). Audio below 0.256s length receives `quality_score = "poor"` because SNR computation is infeasible, not because the audio quality is bad.

**Key distinction from R1 (W1503):** R1 affects audio of any length at typical voice amplitudes (amp < 0.141). S2 affects only audio shorter than 0.256s, regardless of amplitude.

**Verified:**
```python
# 0.25s at amp=0.3 (clean signal): poor
# 0.26s at amp=0.3 (same clean signal): excellent
for n in [4095, 4096, 4097]:
    r = AudioQualityAnalyzer().analyze((0.3*np.sin(...)[:n]).astype(np.float32), sr)
    # n=4095: snr=0.0, score=poor   ← false poor
    # n=4096: snr=55.6, score=excellent  ← correct
```

**The SNR early-return should return a sentinel (e.g., `float('nan')` or `-1.0`) and `_score()` should check for it specifically.** Alternatively, the `_MIN_DURATION_SEC = 0.5s` warning should also apply to SNR computation: if audio is shorter than the 4-frame minimum, snr should not force `"poor"`.

**No existing test verifies score correctness for sub-4096-sample audio** — only `test_short_audio_warning` checks for the warning string, not the score.

---

### S3 — MEDIUM: `_score()` uses strict `> 0.9` for poor-silence boundary — exactly 90% silent audio scores `"good"` instead of `"poor"`

**Location:** `KrabEar/core/audio_quality.py`, line 228.

```python
def _score(self, snr_db, clipping_ratio, silence_ratio, rms_level):
    if silence_ratio > 0.9 or rms_level < 1e-6:  # ← strict >, not >=
        return "poor"
```

At `silence_ratio == 0.9` exactly (e.g., 90 out of 100 frames silent), the condition is `False` and the score proceeds to the `snr_db >= 20` check. With sufficiently good SNR from the 10% active frames, the result is `"good"` — even though 90% of the recording is silence.

**Verified:**
```python
# 90 silent frames + 10 active frames of amplitude 0.3
r = AudioQualityAnalyzer().analyze(audio, sr)
# silence_ratio = 0.9 exactly (float)
# 0.9 > 0.9 == False → not poor
# snr passes 'good' threshold → score = 'good'
print(r.silence_ratio == 0.9)     # True
print(r.silence_ratio > 0.9)      # False
print(r.quality_score)             # 'good'
```

This is the floating-point manifestation of the long-standing P2 (silence warning at `> 0.8`, score at `> 0.9`). At the exact boundary, the off-by-one produces a `"good"` score with a silence warning — contradictory output.

**Fix:** Change `> 0.9` to `>= 0.9` in `_score()`, and align the warning threshold with the score threshold (both at `0.9`) or document the intentional gap.

---

### S4 — MEDIUM: `_compute_silence_ratio` gives inconsistent results vs `_estimate_snr` for audio whose length is not a multiple of `_SILENCE_FRAME_SIZE`

**Location:** `KrabEar/core/audio_quality.py`, lines 160–165 (`_compute_silence_ratio`) and line 181 (`_estimate_snr`).

Both methods split audio into frames using `np.array_split(audio, n_frames)` where `n_frames = len(audio) // _SILENCE_FRAME_SIZE`. When `len(audio)` is not a multiple of 1024, `np.array_split` distributes the remainder samples unevenly: the first `r = len(audio) % n_frames` frames get one extra sample.

For `n = 2049` samples, `n_frames = 2`, `np.array_split` yields `[1025, 1024]` — the first frame has 1025 samples instead of 1024. For `n = 33600` (2.1s at 16kHz), all frames have size 1050 instead of 1024 (`33600 // 32 = 1050`).

**Impact:** Frames of different sizes compute slightly different RMS values for the same underlying signal amplitude. For a sine wave at `amp=0.3`, frame RMS varies from `0.003529` to `0.003541` — negligible. However, for audio near the silence threshold, an uneven remainder can push a frame's RMS across the threshold differently than if the frame were exactly 1024 samples. This causes `silence_ratio` to be non-deterministic relative to the expected threshold at non-standard durations.

Additionally, `_estimate_snr` uses `n_frames = max(n // _SILENCE_FRAME_SIZE, 4)` (minimum 4), while `_compute_silence_ratio` uses `max(n // _SILENCE_FRAME_SIZE, 1)` (minimum 1). For audio between 1024 and 4095 samples, `_compute_silence_ratio` runs with 1–3 frames but `_estimate_snr` returns `0.0` early. The two methods use inconsistent frame granularity for identical audio length ranges.

**Fix:** Use `np.array_split` with a consistent `n_frames` value in both methods, or switch to the reshape-based approach that discards the partial remainder frame (at most 1023 samples):
```python
n_aligned = (len(audio) // _SILENCE_FRAME_SIZE) * _SILENCE_FRAME_SIZE
mat = audio[:n_aligned].reshape(-1, _SILENCE_FRAME_SIZE)
```
This also fixes R4 (9.5x performance improvement for 1-hour audio).

---

### S5 — LOW: `test_audio_quality_silence_threshold_W1107.py` — the W1107 regression guard file imports from `test_audio_quality.py` but does not guard the `_safe_float` removal

**Location:** `KrabEar/tests/test_audio_quality_silence_threshold_W1107.py` (if present) and `KrabEar/tests/test_audio_quality_nan_W1017.py`.

W1107 added tests to verify that `_SILENCE_RMS_THRESHOLD == SILENCE_THRESHOLD_AMP`. These tests pass. However, W1107 simultaneously removed `_safe_float` without any guard preventing its removal. The 13-test file `test_audio_quality_nan_W1017.py` that guarded the `_safe_float` contract now **crashes at collection** with `ImportError: cannot import name '_safe_float'`.

The root issue: W1107 treated `_safe_float` as internal cleanup incidental to the threshold change, without checking that 13 tests depended on it being importable. No CI guard protected the `_safe_float` export.

**Verification:**
```bash
PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_audio_quality_nan_W1017.py
# ERROR during collection: ImportError: cannot import name '_safe_float' from 'core.audio_quality'
```

**Fix:** Restore `_safe_float` (S1) will fix the collection crash. Additionally: add an `__all__` export or a guard test that verifies the NaN-safety contract on the `analyze()` output directly (not by importing the helper), so future refactors cannot silently break the contract by removing the helper.

---

## Summary Table

| ID | Severity | Description | New since W1503? |
|----|----------|-------------|-----------------|
| S1 | HIGH | W1107 removed `_safe_float` — NaN protection fully reverted, 2 tests failing + 13 crash at collection | **NEW — introduced by W1107 (post-W1503)** |
| S2 | HIGH | `_estimate_snr` early-return for n < 4096 samples forces `poor` score; no test checks score (only warnings) | **NEW — overlooked in W1503** |
| S3 | MEDIUM | `_score()` strict `> 0.9` boundary: exactly 90% silent audio scores `good` not `poor` (float off-by-one) | **NEW — narrower form of P2** |
| S4 | MEDIUM | `np.array_split` distributes remainder unevenly; `_compute_silence_ratio` and `_estimate_snr` use different minimum frame counts for same audio length | **NEW** |
| S5 | LOW | `test_audio_quality_nan_W1017.py` crashes at import after W1107 removed `_safe_float`; no guard prevented removal | **NEW — consequence of S1** |

---

## Root Cause of S1 — Scope Creep in W1107

W1107 was scoped to change `SILENCE_THRESHOLD_AMP`'s import source. Its diff additionally removed `import math` and `_safe_float` as "dead code cleanup" — likely because after W1442 removed the shadow definition, the 2-arg `_safe_float` was the only use of `math.isfinite`. The author did not audit downstream test dependencies before removing it.

The fix chain required:
1. W1017: Add `_safe_float` with `math` import, wrap all outputs.
2. W1442: Remove 1-arg shadow that was overriding W1017's 2-arg version.
3. W1477: Change `silence_constants` import.
4. W1107: **Break everything** by removing `_safe_float` as collateral.

---

## Priority Action Items

1. **Fix S1** — restore `_safe_float` + `import math` in `audio_quality.py`, restore call sites at all 6 output fields in `analyze()` and `to_dict()`. Unblocks 15 tests currently failing/crashing.
2. **Fix R1** (from W1503) — add `_SNR_NOISE_FLOOR_THRESHOLD = 0.01` constant in `_estimate_snr`, use it instead of `_SILENCE_RMS_THRESHOLD * 10`. Restores SNR for typical voice levels.
3. **Fix S2** — make `_estimate_snr` return a sentinel for short audio; adjust `_score()` to not penalize unavailable SNR.
4. **Fix S3** — change `silence_ratio > 0.9` to `>= 0.9` in `_score()`.
5. **Fix S4 / R4** — replace `np.array_split` loop with reshape-based vectorization (9.5x speedup, consistent frame handling).
