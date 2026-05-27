# W1384 Fourth-pass Re-audit: `core/audio_quality.py`

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` @ `6c900317` (v2.0.5)
**File:** `KrabEar/core/audio_quality.py`
**Prior audits:** W1015 (6 findings), W1100 (5 residual), W1133 (5 residual N1–N5)
**Waves under review:** W1017/W1103 (NaN safety), W1107 (silence threshold), W1320 (denoiser percentile), W1322 (noisereduce strong-mode), W1333 (shared silence constant)

---

## Merge State of Prior Fix Waves

| Wave | Description | Status in `codex/krab-ear-v2` |
|------|-------------|-------------------------------|
| W1017 | NaN/Inf JSON safety (`_safe_float`) | **NOT MERGED** — PR open, commit `48126441` not in main |
| W1103 | Re-apply W1017 NaN/Inf JSON safety | **NOT MERGED** — commit `dfcfe313` not in main |
| W1107 | Unify silence threshold via `SILENCE_THRESHOLD_AMP` | **NOT MERGED** — commit `21513ad8` not in main |
| W1133 | Residual audit docs (N1–N5) | **NOT MERGED** — docs only, commit `fb3d4432` not in main |
| W1320 | Denoiser percentile strict-lt + zero-selection skip | **NOT MERGED** — commit `826863ad` not in main |
| W1322 | noisereduce `prop_decrease` via params | **NOT MERGED** — commit `5096ebf6` not in main |
| W1333 | Shared silence constant `silence_constants.py` | **NOT MERGED** — commit `0f2ee1f5` not in main |

Verification:
```bash
git merge-base --is-ancestor dfcfe313 codex/krab-ear-v2  # → exit 1 (NOT ancestor)
git merge-base --is-ancestor 826863ad codex/krab-ear-v2  # → exit 1
git merge-base --is-ancestor 0f2ee1f5 codex/krab-ear-v2  # → exit 1
```

All six W1015/W1100/W1133 findings (F1–F5, R1–R5, N1–N5) remain open in production.
`silence_constants.py` does not exist in `codex/krab-ear-v2`; `_SILENCE_RMS_THRESHOLD = 0.001`
is still the local constant.

---

## W1015/W1100/W1133 Open Finding Status

| ID | Description | Status |
|----|-------------|--------|
| F1/R4 | NaN/Inf propagation into JSON output | **OPEN** (W1017/W1103 not merged) |
| F2 | Swift JSONDecoder crash on `NaN` token | **OPEN** |
| F3/R1 | Dual silence thresholds 0.001 vs 0.01 | **OPEN** (W1107 not merged) |
| F4/R2 | SNR=0.0 for audio <4096 samples | **OPEN** |
| F5/R3 | `_error_bus` never injected in production | **OPEN** |
| N1 | Quality reports raw audio, not denoised | **OPEN** |
| N2 | `float64` cast doubles RAM for long audio | **OPEN** |
| N3 | Python silence/SNR loops block IPC thread | **OPEN** |
| N4 | `analyze_file` `_error_bus` always None | **OPEN** |
| N5 | `np.clip` does not sanitize NaN in `_estimate_snr` | **OPEN** |

---

## New Findings (cap 5)

### P1 — HIGH: `_estimate_snr` `quiet_mask` selects ALL frames for low-level signals, collapsing SNR to ~0 dB

**Location:** `_estimate_snr`, line 191

```python
quiet_mask = frame_rms < _SILENCE_RMS_THRESHOLD * 10   # 0.001 * 10 = 0.01
```

When a recording's audio level is uniformly below 0.01 RMS (e.g. distant microphone,
quiet room at moderate amplification, or a clean sine wave at amplitude ≤ 0.014), every
frame satisfies `quiet_mask`. Then:

```python
noise_rms = float(np.mean(frame_rms[quiet_mask]))  # = mean of ALL frames = signal RMS
snr = 20 * log10(signal_rms / noise_rms)           # = 20 * log10(1.0) = 0 dB
```

A clean 440 Hz sine at amplitude 0.005 (RMS ≈ 0.00354) produces SNR ≈ 0 dB → `quality_score
= "poor"`, even though the true SNR is effectively infinite (no noise). This is
structurally identical to the W1320 bug fixed in `AudioDenoiser._percentile_noise_clip`:
W1320 introduced strict `<` and a fallback to the CV path when all frames are selected.
`audio_quality._estimate_snr` has no such guard.

**Demonstrated:**
```python
import numpy as np
audio = np.sin(2*np.pi*440*np.arange(16000*3)/16000, dtype=np.float64) * 0.005
# frame_rms all ≈ 0.00354 < 0.01 → quiet_mask = all True
# noise_rms = 0.00354 = signal_rms → SNR = 0.0 dB → "poor"
```

Affected use-cases: quiet room recordings, distant microphone, audio normalised post-recording
to a low headroom, or recordings from low-sensitivity ADCs.

**Severity:** HIGH — clean, valid audio is misclassified as "poor", suppressing STT
pre-flight warnings from reaching Swift while producing a false negative on quality score.

**Fix:** Apply the W1320 pattern:
```python
# If ALL frames fall below the threshold, fall through to the CV-based path
if np.sum(quiet_mask) >= 2 and np.sum(quiet_mask) < len(frame_rms):
    noise_rms = float(np.mean(frame_rms[quiet_mask]))
    ...
elif np.sum(quiet_mask) == len(frame_rms):
    # All frames quiet: fall through to CV path — signal is low-level but clean
    pass
```

---

### P2 — MEDIUM: Warning threshold (>0.8) and score threshold (>0.9) for silence_ratio are inconsistent

**Location:** `analyze()` lines 125–127 and `_score()` line 224

```python
# analyze() — warning path:
if silence_ratio > 0.8:
    warnings.append(f"Высокая доля тишины: {silence_ratio * 100:.0f}% фреймов")

# _score() — poor score path:
if silence_ratio > 0.9 or rms_level < 1e-6:
    return "poor"
```

When `silence_ratio` is in the range `(0.8, 0.9]`, the warning fires ("Высокая доля
тишины") but `_score()` does **not** return `"poor"`. If `snr_db >= 20`, the
quality score is `"good"`. A Swift caller receives contradictory diagnostics:
`{"quality_score": "good", "warnings": ["Высокая доля тишины: 85% фреймов"]}`.

For a recording where 85% of frames are silence, a `"good"` quality score is misleading:
the STT engine will likely see very little speech data.

**Demonstrated:**
```python
silence_ratio, snr_db, clip = 0.85, 25.0, 0.0
# warning fires (> 0.8)
# _score: not > 0.9 → not "poor"; snr >= 20 → "good"
# Result: {"quality_score": "good", "warnings": ["Высокая доля тишины..."]}
```

**Severity:** MEDIUM — contradictory diagnostics mislead callers; no crash.

**Fix:** Align thresholds or gate the `"good"/"excellent"` path on `silence_ratio < 0.8`:
```python
if snr_db >= 20 and clipping_ratio < 0.01 and silence_ratio < 0.8:
    return "good"
```

---

### P3 — MEDIUM: W1107 merge will silently break `silence_ratio` semantics for quiet-speech recordings

**Location:** `_compute_silence_ratio` line 160 / W1107 fix branch `21513ad8`

W1107 (unmerged) replaces `_SILENCE_RMS_THRESHOLD = 0.001` with `SILENCE_THRESHOLD_AMP = 0.01`
(from `core.silence_detector`). This is correct for aligning the SNR noise-floor threshold
(which currently uses `0.001 * 10 = 0.01`), but it also changes `_compute_silence_ratio`
from `< 0.001` to `< 0.01` — a **10× increase** in the silence detection boundary.

Impact: recordings with speech at amplitude `0.001–0.01` (RMS ≈ `0.0007–0.007`):
- Before W1107: frames classified as **speech** → `silence_ratio` ≈ 0 → quality `"good"` or `"fair"`
- After W1107: frames classified as **silence** → `silence_ratio` ≈ 1.0 → quality `"poor"`

This range covers:
- Distant microphone recordings (common in meeting rooms)
- Recordings after applying gain reduction
- Recordings from budget ADCs with 16-bit headroom at moderate volume

No existing test exercises amplitude in the range `0.001–0.01`. `test_active_signal_low_silence`
uses `amplitude=0.3` (RMS ≈ 0.212) which is well above both thresholds and passes both before
and after W1107. The transition range is a blind spot.

**Severity:** MEDIUM — W1107 merge creates a silent regression for quiet-speech recordings;
requires test coverage before merge.

**Fix:** Before merging W1107, add a test:
```python
def test_quiet_speech_not_fully_silent_post_w1107(self):
    # amplitude 0.005 (RMS ≈ 0.0035) — above 0.001 but below 0.01
    audio = _sine(amplitude=0.005, duration=2.0)
    report = AudioQualityAnalyzer().analyze(audio, SR)
    # After W1107: ALL frames will be silent → silence_ratio = 1.0 → "poor"
    # This test documents the intentional behavior change for reviewers
    self.assertIsInstance(report.quality_score, str)  # at minimum no crash
```

Also add a corresponding note in the W1107 commit message / PR description.

---

### P4 — LOW: `analyze_file` leaks internal file path in `SoundFileError` exception message to IPC callers

**Location:** `analyze_file()` line 247 / `audio_analytics_service.py` line 74

```python
# audio_quality.py
audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)

# audio_analytics_service.py
report = analyze_file(file_path)  # no try/except
```

`soundfile.SoundFileError` messages include the full file path in their text:
`"Error opening '<full-path>': format not recognised"`. `service.py` line 1292
catches all exceptions and converts them via `str(exc)`, returning the message
verbatim to the Swift IPC caller:
```json
{"ok": false, "error": {"code": "internal_error", "message": "Error opening '/Users/.../...': format not recognised"}}
```

While local paths on a single-user system may not be sensitive, this can expose:
- Absolute paths including username (`/Users/<username>/...`)
- Vault or data directory structure
- Presence of specific files (oracle attack)

Compare: `handle_analyze_silence` delegates to `analyze_silence_file` which also lacks
protection; the same applies to `handle_get_waveform` and `handle_get_audio_info`.

**Severity:** LOW — information disclosure to an authenticated local IPC client;
low practical risk in single-user macOS deployment but inconsistent with the path-
sanitization pattern established by `InputSanitizer.sanitize_path`.

**Fix:** Wrap `sf.read` in `analyze_file` with a generic re-raise:
```python
try:
    audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
except Exception as exc:
    raise ValueError(f"Не удалось прочитать аудиофайл: {path.name}") from exc
```
This preserves the original exception for logging while hiding the full path from the
IPC response.

---

### P5 — LOW: No test coverage for W1320-analog: clean low-level signal classified as "poor"

**Location:** `KrabEar/tests/test_audio_quality.py`

The test suite has no test case demonstrating (or preventing) the P1 regression:
clean low-level audio misclassified as "poor" due to all-frames quiet_mask. The closest
test is `test_snr_on_pure_signal_high` (Wave 108), which uses `amplitude=0.5` — well above
the 0.01 quiet_mask threshold, so it cannot trigger P1.

A regression test would:
1. Document the current (broken) behavior so P1 is visible in CI when fixed.
2. Prevent P1 from being silently re-introduced after W1107/W1133 merges change the thresholds.

Missing cases:
- `test_low_level_clean_signal_snr_not_zero`: `amplitude=0.005`, 3s, 16kHz → `snr_estimate_db` must be `> 20 dB` (currently fails, documents P1)
- `test_silence_ratio_boundary_0_8_to_0_9`: `silence_ratio` in `(0.8, 0.9]` → `quality_score` consistency with `warnings` (documents P2)
- `test_quiet_speech_amplitude_range`: amplitudes in `[0.001, 0.01)` → verify `silence_ratio` matches expected semantics

**Severity:** LOW — missing regression tests; no production crash.

**Fix:** Add the three test cases above to `test_audio_quality.py`.

---

## Summary Table

| ID | Severity | Description | New since W1133? |
|----|----------|-------------|-----------------|
| P1 | HIGH | `quiet_mask` selects all frames for low-level clean signals → SNR=0 → "poor" | Yes — W1320 analog, not in any prior audit |
| P2 | MEDIUM | `silence_ratio` warning (>0.8) contradicts score "poor" threshold (>0.9) | Yes — threshold inconsistency not in F1–N5 |
| P3 | MEDIUM | W1107 merge changes `silence_ratio` semantics for 0.001–0.01 RMS signals without test coverage | Yes — W1107 interaction, not in any prior audit |
| P4 | LOW | `sf.read` exception leaks full file path to IPC response | Yes — new since W1133 |
| P5 | LOW | No test for low-level clean signal SNR misclassification (P1 regression guard) | Yes — extends test coverage gap |

---

## Priority Action Items

1. **Fix P1** (quiet_mask all-frames collapse) — add W1320-style guard in `_estimate_snr`.
   Prerequisite for correct behavior after W1107 merge.
2. **Fix P2** (silence warning/score inconsistency) — gate `"good"` path on `silence_ratio < 0.8`.
3. **Add P3 test** before merging W1107 — document the intentional behavior change at 0.01 boundary.
4. **Fix P4** (path leak) — wrap `sf.read` in `analyze_file` with generic re-raise.
5. **Add P5 tests** — three test cases in `test_audio_quality.py`.
6. **Merge W1103 + W1107** — highest-priority deferred fixes (F1/R4 HIGH, F3/R1 MEDIUM).
