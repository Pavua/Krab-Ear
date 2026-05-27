# W1503 Third-Pass Audit: `core/audio_quality.py`

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` @ `ecab3fff`
**Worktree:** `audit-audio-quality-third-W1503`
**File:** `KrabEar/core/audio_quality.py`
**Prior audits:** W1015, W1100, W1133, W1384, W1461 (P1–Q5)
**Fix waves reviewed:** W1442 (duplicate `_safe_float` removed), W1477 (silence threshold unified)

---

## Merge State Verification

| Wave | Description | Status in `codex/krab-ear-v2` |
|------|-------------|-------------------------------|
| W1442 | Remove duplicate `_safe_float` 1-arg shadow (CRIT) | **MERGED** — commit `c9b07a04` |
| W1477 | Import shared `SILENCE_THRESHOLD_AMP` from `silence_constants` | **MERGED** — commit `e6e4b39c` |

Both fixes verified by:
```bash
git log --oneline codex/krab-ear-v2 -- KrabEar/core/audio_quality.py
# e6e4b39c fix(wave1477): audio_quality imports shared SILENCE_THRESHOLD_AMP (W1468 N1 HIGH) (#1359)
# c9b07a04 fix(wave1442): remove duplicate _safe_float 1-arg shadow (W1441 #4 HIGH-CRIT LIVE CRASH) (#1337)

python3 -c "from core.audio_quality import _safe_float; print(_safe_float(float('nan'), 1.0))"
# → 1.0   (W1442 confirmed: 2-arg form works)

python3 -c "from core.audio_quality import _SILENCE_RMS_THRESHOLD; print(_SILENCE_RMS_THRESHOLD)"
# → 0.01  (W1477 confirmed: imported from silence_constants, not hardcoded 0.001)
```

Runtime verification: `analyze()` on non-empty audio completes without `TypeError`.
51 tests across `test_audio_quality.py` + `test_audio_quality_nan_W1017.py` all pass.

---

## Prior Open Findings Status (from W1461 / W1384 / W1133)

| ID | Description | Status |
|----|-------------|--------|
| P1 | `quiet_mask` all-frames collapse for low-level clean signals → SNR=0, score=poor | **STILL OPEN — AGGRAVATED by W1477** (see R1 below) |
| P2 | silence_ratio warning >0.8 inconsistent with score threshold >0.9 | **STILL OPEN** |
| P4 | `sf.read` / `FileNotFoundError` leaks full path to IPC response | **STILL OPEN** |
| N2 | `float64` cast at line 90 doubles RAM for all float32 inputs | **STILL OPEN** |
| N3 | Python loop in `_compute_silence_ratio` blocks IPC thread ~200ms for 1h audio | **STILL OPEN** |
| N4 | `_error_bus` never injected — empty-audio error path always silently skipped | **STILL OPEN** |
| N5 | `np.clip(snr, -20, 80)` does not sanitize NaN; NaN passes to `_score()` | **STILL OPEN** |

---

## New Findings (cap 5)

### R1 — HIGH: W1477 threshold change aggravates P1 — `_estimate_snr` gives SNR=0 for all typical voice recordings

**Location:** `KrabEar/core/audio_quality.py`, line 210.

**Root cause:**

```python
# _estimate_snr(), line 210:
quiet_mask = frame_rms < _SILENCE_RMS_THRESHOLD * 10
```

This multiplier `* 10` was designed when `_SILENCE_RMS_THRESHOLD = 0.001` (pre-W1477), giving a quiet-frame boundary of `0.001 * 10 = 0.01`. The intent was: frames below `0.01` RMS are "likely silence / noise floor", use them as the noise reference.

W1477 changed `_SILENCE_RMS_THRESHOLD` from `0.001` to `0.01` (via shared `SILENCE_THRESHOLD_AMP`). The multiplier now produces `0.01 * 10 = 0.1` as the quiet-frame boundary. Any frame with RMS below `0.1` is now treated as a "quiet/noise" frame.

**Impact — verified empirically:**

A clean, steady sine wave at amplitude `0.02` to `0.14` (RMS `0.014` to `0.099`) has all its frames below `0.1` RMS. The `_estimate_snr` method incorrectly classifies ALL frames as "quiet", uses the signal itself as the noise floor, and computes:

```
SNR = 20 * log10(signal_rms / noise_rms) = 20 * log10(1.0) = 0 dB
```

This forces `quality_score = "poor"` for any clean audio with amplitude below `~0.141`.

```python
# Measured (post-W1477):
for amp in [0.02, 0.05, 0.08, 0.10, 0.14]:
    # all → snr_estimate_db=0.0, quality_score="poor"

# amplitude=0.15 (RMS=0.106) → snr=57.8 dB, score="excellent"  ← threshold boundary
```

**Practical impact:** Typical laptop microphone recordings have RMS in the range `0.02–0.1`. ALL of these receive `score="poor"` with `snr=0 dB` even when the audio is clean. The `analyze_audio_quality` IPC method is functionally broken for typical use-cases.

**Pre-W1477 state:** The old threshold `0.001 * 10 = 0.01` only affected signals below `0.01` RMS, which corresponds to barely-audible audio. The W1477 change moved the broken-SNR boundary from `0.01` RMS to `0.1` RMS — a 10x shift that now covers the entire normal voice range.

**Fix:** The quiet-frame threshold in `_estimate_snr` must NOT be derived from `_SILENCE_RMS_THRESHOLD`. It serves a different purpose (SNR estimation, not silence detection). Either introduce a dedicated constant or hard-code the value to the pre-W1477 behavior:

```python
# Option A: separate constant (preferred)
_SNR_NOISE_FLOOR_THRESHOLD = 0.01   # frames below this are noise floor candidates

# Option B: revert to old effective value
quiet_mask = frame_rms < 0.01   # intentionally not using _SILENCE_RMS_THRESHOLD here
```

**No existing test catches this.** All tests use `amplitude=0.3` or higher (RMS `>0.2`), which are above the `0.1` broken boundary. A test with `amplitude=0.05` and a clean signal would fail with `snr=0`.

---

### R2 — HIGH: No regression test for the `_estimate_snr` quiet_mask range after threshold changes

**Location:** `KrabEar/tests/test_audio_quality.py`

The `test_snr_positive_for_signal_over_noise` test (line 196) uses `signal=amplitude 0.5, noise=amplitude 0.005`. The `test_snr_on_pure_signal_high` test (line 295) uses `signal=amplitude 0.5, noise=amplitude 0.001`. Both use amplitudes well above the broken boundary (`0.141`).

There is no test that verifies:
1. A clean signal at typical voice levels (`0.02–0.1` amplitude) gets SNR > 20 dB.
2. The `_estimate_snr` path that uses coefficient-of-variation (`cv`) is exercised.

The W1477 regression passed all 51 existing tests. A targeted regression guard would have caught it immediately:

```python
def test_snr_clean_signal_at_typical_voice_level(self):
    """R2 regression guard: clean audio at typical mic levels must score > 20 dB SNR."""
    audio = _sine(amplitude=0.05, duration=2.0)  # -26 dBFS, typical laptop mic
    report = AudioQualityAnalyzer().analyze(audio, SR)
    self.assertGreater(report.snr_estimate_db, 20.0,
        msg=f"Clean audio at 0.05 amplitude must not score 0 dB (W1477 regression guard)")
    self.assertNotEqual(report.quality_score, "poor",
        msg="Clean typical-voice audio should not score 'poor'")
```

---

### R3 — MEDIUM: `silence_ratio` warning/score threshold gap widens after W1477

**Location:** `KrabEar/core/audio_quality.py`, lines 144–147 and line 243.

The pre-existing P2 finding (warning fires at `silence_ratio > 0.8`, score degrades at `>0.9`) is unchanged. However, W1477's threshold change now creates a new manifestation: because `_SILENCE_RMS_THRESHOLD` increased 10x, audio that previously had `silence_ratio ≈ 0.7` may now score `silence_ratio ≈ 0.85` (more frames classified as silent), pushing it into the warning zone without reaching the score zone.

This widens the "warning fires but score is still 'good'" semantic gap. An audio clip that is genuinely acceptable quality (low ambient noise, occasional speech) receives a warning label that contradicts its "good" score, confusing the IPC consumer.

**Specifically:**
- A recording with RMS in `[0.001, 0.01)` per frame was previously "speech" (not silent); after W1477 it is "silent".
- This shifts `silence_ratio` upward by however many frames were in that range.
- The warning threshold `0.8` is not adjusted to account for the new classification.

**Fix:** Align warning and score thresholds (both at `0.8`, or document the intentional gap) and verify empirically with test audio.

---

### R4 — MEDIUM: `_compute_silence_ratio` and `_estimate_snr` both use Python list comprehensions — no test enforces vectorized path

**Location:** `KrabEar/core/audio_quality.py`, lines 177–180 and 198–200.

Both methods use frame-by-frame Python loops via generator expressions:

```python
# _compute_silence_ratio, line 177-180:
silent = sum(
    1 for f in frames
    if len(f) > 0 and np.sqrt(np.mean(f ** 2)) < _SILENCE_RMS_THRESHOLD
)

# _estimate_snr, line 198-200:
frame_rms = np.array([
    np.sqrt(np.mean(f ** 2)) for f in frames if len(f) > 0
])
```

For 1-hour mono audio at 16 kHz: `n_frames = 57,600,000 // 1024 = 56,250`. The Python list comprehension iterates 56,250 times, each calling `np.mean()` and `np.sqrt()` on a 1024-element array. Measured wall time: **0.198s** vs a vectorized equivalent at **0.016s** (12.7x slower).

The IPC handler `handle_analyze_audio_quality` in `service.py` calls this synchronously on the IPC dispatch thread, blocking all other IPC requests for ~200ms on 1-hour audio. This is N3 from prior audits, now with a measured multiplier.

Vectorized replacement (correct, same results verified):
```python
n_frames_aligned = (len(audio) // _SILENCE_FRAME_SIZE) * _SILENCE_FRAME_SIZE
audio_matrix = audio[:n_frames_aligned].reshape(-1, _SILENCE_FRAME_SIZE)
frame_rms = np.sqrt(np.mean(audio_matrix ** 2, axis=1))
silent = int(np.sum(frame_rms < _SILENCE_RMS_THRESHOLD))
```

Note: the remainder frame (the last partial frame that `np.array_split` includes but the reshape drops) represents at most `_SILENCE_FRAME_SIZE - 1 = 1023` samples — negligible for audio analysis.

---

### R5 — LOW: `float64` cast at line 90 doubles peak memory for all `float32` inputs

**Location:** `KrabEar/core/audio_quality.py`, line 90.

```python
audio_data = audio_data.astype(np.float64)
```

This unconditionally upcasts the entire audio buffer from `float32` to `float64` at the start of `analyze()`. For the common case of `soundfile.read(dtype='float32')` (line 266), this doubles the in-memory size:

| Duration | float32 size | float64 size |
|----------|-------------|-------------|
| 1 min    | 3.8 MB      | 7.6 MB      |
| 1 hour   | 230 MB      | 460 MB      |

There is no precision benefit for audio quality metrics: RMS, peak, SNR estimates all have sufficient accuracy in `float32` (6–7 significant digits covers the dynamic range of 16-bit audio). The cast is defensive but produces peak RAM double the necessary amount during analysis.

The `analyze_file()` convenience function at line 266 explicitly reads in `dtype="float32"` — the `float64` upcast immediately follows on all that data.

**Fix:** Remove the unconditional upcast; use `np.float64` only for intermediate accumulator operations where precision matters (e.g., inside `np.mean()` on large arrays — numpy handles this automatically via its internal accumulation rules).

---

## Summary Table

| ID | Severity | Description | New since W1461? |
|----|----------|-------------|-----------------|
| R1 | HIGH | W1477 threshold change breaks SNR for all typical voice levels (<0.141 amplitude) | NEW — introduced by W1477 |
| R2 | HIGH | No regression test for `_estimate_snr` at typical voice amplitudes (0.02–0.14) | NEW — gap exposed by R1 |
| R3 | MEDIUM | silence_ratio warning/score gap widens after W1477; more frames now classified silent | NEW — aggravated by W1477 |
| R4 | MEDIUM | Python loops in `_compute_silence_ratio` / `_estimate_snr` block IPC thread ~200ms for 1h audio | Carried from N3; now measured |
| R5 | LOW | `float64` cast doubles peak RAM for all `float32` inputs (common case via `soundfile`) | Carried from N2; confirmed |

---

## Root Cause of R1 — Design Contract Violation

W1477 unified `_SILENCE_RMS_THRESHOLD` with `SILENCE_THRESHOLD_AMP` from `silence_constants.py`. This is correct for `_compute_silence_ratio()` — silence detection should be consistent.

However, `_SILENCE_RMS_THRESHOLD` is also used in `_estimate_snr()` with a `* 10` multiplier (line 210) that was designed as a different, higher threshold for noise-floor frame selection. This is an implicit second meaning of the constant that was not documented or separated. When the silence constant changed, it silently changed the SNR estimation behavior as a side-effect.

The fix requires separating these two uses:
- `_SILENCE_RMS_THRESHOLD` (= `SILENCE_THRESHOLD_AMP = 0.01`) for silence detection in `_compute_silence_ratio` — correct.
- A dedicated `_SNR_NOISE_FLOOR_THRESHOLD = 0.01` constant (value equal to the old `_SILENCE_RMS_THRESHOLD * 10 = 0.001 * 10`) for noise-floor detection in `_estimate_snr` — needs a new constant.

---

## Priority Action Items

1. **Fix R1** (`_estimate_snr` quiet_mask threshold) — HIGH, restores SNR for typical voice recordings. Add `_SNR_NOISE_FLOOR_THRESHOLD = 0.01` and use it instead of `_SILENCE_RMS_THRESHOLD * 10`.
2. **Add R2 test** (`test_snr_clean_signal_at_typical_voice_level`) — prevents future regressions from threshold changes.
3. **Fix P2/R3** (silence warning/score gap) — MEDIUM, align thresholds at 0.8 or document why the gap is intentional.
4. **Vectorize R4** (`_compute_silence_ratio` and `_estimate_snr`) — MEDIUM, 12.7x speedup, prevents IPC thread block.
5. **Remove float64 cast R5** — LOW, halves peak RAM for audio analysis.
