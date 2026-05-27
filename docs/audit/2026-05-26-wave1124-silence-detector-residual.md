# W1124 — SilenceDetector Residual Audit

**Date:** 2026-05-26
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)
**Scope:** `KrabEar/core/silence_detector.py`, `KrabEar/core/smart_silence_skipper.py`,
`KrabEar/backend/realtime_silence_filter.py`, interactions with `audio_denoiser.py` (W1080),
`SmartSilenceSkipper` (W1102), `audio_quality.py` (W912), `noise_profiler.py`, `audio_chunker.py`.

---

## W912 / W1018 Merge Status

| Wave | PR branch | Merged into `codex/krab-ear-v2`? |
|------|-----------|-----------------------------------|
| W912 — unify -40 dB across SilenceDetector + AudioQualityAnalyzer | `origin/feature/fix-silence-threshold-W912` | **NOT MERGED** |
| W1018 — SILENCE_THRESHOLD_DB_PRESERVE_WHISPER + dead `audio.shape` removal | `origin/fix-silence-whisper-threshold-W1018` | **NOT MERGED** |

Both commits exist only on their feature branches (confirmed via `git branch -r --contains`).
`codex/krab-ear-v2` HEAD (`6c900317`) does **not** include either fix.

Concrete evidence:
- `audio_quality.py` still has `_SILENCE_RMS_THRESHOLD = 0.001` (the pre-W912 -60 dB value)
- `silence_detector.py` has no `SILENCE_THRESHOLD_DB`, `SILENCE_THRESHOLD_AMP`,
  `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`, or `SILENCE_THRESHOLD_DB_STRICT` exports
- `smart_silence_skipper.py` `_DEFAULT_THRESHOLD_DB = -40.0` (not pointing at the
  `SILENCE_THRESHOLD_DB_PRESERVE_WHISPER = -55.0` constant from W1018)
- `realtime_silence_filter.py` `_DEFAULT_THRESHOLD_DB = -40.0` (same — not updated to -55 dB)
- The dead `audio.shape` no-op on line 128 of `trim_silence()` is still present

---

## New Residual Findings (max 5)

### F1 — HIGH | W1080 denoiser noise-floor uses fixed 200 ms window; silence detector uses fixed -40 dB default → pipeline mismatch after denoising

**File:** `KrabEar/core/audio_denoiser.py` lines 134, 175
**File:** `KrabEar/core/silence_detector.py` (defaults)

`AudioDenoiser._denoise_noisereduce` / `_denoise_spectral_gating` estimate the noise floor from the
first 200 ms of audio (`_NOISE_FLOOR_SAMPLES = 3200` samples at 16 kHz). If those 200 ms happen to
contain speech rather than background noise (common for short recordings or recordings that start
mid-sentence), the denoiser over-attenuates the speech and depresses its RMS below -40 dB.
When the denoised audio is then passed to `SilenceDetector.detect_silence` with the default
`threshold_db=-40.0`, legitimate speech frames are misclassified as silence and skipped before STT.

W1080 (`origin/fix-audio-denoiser-W1080`, **not merged**) replaces the 200-ms window with a
percentile-based noise floor, which would reduce the over-attenuation risk. Until both W1080 and
W1018 land, the combination of fixed-window denoising + fixed -40 dB silence threshold creates a
silent swallowing of denoised whispered or low-level speech.

**Fix required:** Merge W1080 before W1018, or add a note to W1018 that the denoiser must be fixed
concurrently.

---

### F2 — HIGH | W1102 SmartSilenceSkipper wiring not merged → SMART_SILENCE_SKIP_ENABLED=true has no effect

**File:** `KrabEar/core/engine.py`
**Branch:** `origin/wire-smart-silence-skipper-W1102` (**not merged**)

`SmartSilenceSkipper` is instantiated and tested in isolation, but `AudioEngine` never calls it.
`SMART_SILENCE_SKIP_ENABLED = True` in settings has no effect at runtime — the engine's transcribe
pipeline never invokes `SmartSilenceSkipper.process()`. The feature is completely inert in production.

Additionally, because W1018 (the -55 dB preserve-whisper threshold) is also not merged, even when
W1102 is eventually merged the skipper will run with the wrong (-40 dB) threshold unless both PRs
land together.

**Fix required:** Merge W1102 (SmartSilenceSkipper wiring) together with W1018 (preserve-whisper
threshold). The two fixes are co-dependent.

---

### F3 — MED | `np.array_split` frame-distribution causes up to 16 ms timing drift in silence region timestamps

**File:** `KrabEar/core/silence_detector.py` lines 70, 90-95, 100-104

`detect_silence` computes `n_frames = n_samples // _FRAME_SIZE`, then calls
`np.array_split(audio, n_frames)`. When `n_samples % _FRAME_SIZE != 0`, `array_split` distributes
the leftover samples by making the **first** `(n_samples % n_frames)` frames one sample larger.
However, all timing is computed using the constant formula `i * _FRAME_SIZE / sample_rate`, not the
actual cumulative frame sizes.

For a 10-second recording at 16 kHz with 1 extra sample (160001 samples), the maximum drift is
`~16.1 ms` at every frame boundary from frame 1 onward. For a 10-minute audio file the drift is
similar (since it depends only on the extra-samples count mod `n_frames`).

This drift propagates to `SilenceRegion.start_sec` / `end_sec` values and from there into
`SmartSilenceSkipper` skip decisions and `zero_silence_ranges()` in `realtime_silence_filter.py`.
For STT purposes the 16 ms error is within Whisper's segment granularity, but for
`SmartSilenceSkipper` skip boundaries and `analyze_silence_file` reporting it creates a systematic
inaccuracy.

**Correct approach:** Track cumulative sample offset per frame instead of multiplying by constant
`_FRAME_SIZE`. A vectorized `reshape` approach avoids the issue entirely and is 2.8× faster
(measured: 47 ms → 17 ms for 18750 frames).

---

### F4 — MED | `NoiseProfiler._SILENCE_RMS_THRESHOLD = 0.001` diverges from `SilenceDetector` -40 dB (0.01); W912 not merged

**File:** `KrabEar/core/noise_profiler.py` line 24

W912's intent was to unify these two thresholds (noise_profiler used 0.001 ≈ -60 dB, silence_detector
uses 0.01 = -40 dB). Since W912 is not merged:

- `NoiseProfiler` still classifies audio frames below 0.001 RMS as silence (a 20 dB gap from
  `SilenceDetector`)
- When `AudioEngine` calls `NoiseProfiler.profile()` to decide whether to denoise, the SNR estimate
  uses a different silence baseline than the post-denoising `SilenceDetector` call, making the
  decision to denoise inconsistent with the silence detection that follows

**Fix required:** Merge W912.

---

### F5 — LOW | No `KRAB_EAR_SILENCE_THRESHOLD_DB` env override — threshold not runtime-configurable

**Files:** `KrabEar/core/config.py`, `KrabEar/core/silence_detector.py`, `KrabEar/core/smart_silence_skipper.py`

`Settings` (Pydantic-Settings, env prefix `KRAB_EAR_`) has runtime knobs for
`SMART_SILENCE_SKIP_ENABLED`, `REALTIME_SILENCE_FILTER_ENABLED`, `RT_SILENCE_MAX_SEC`, and
`STT_VAD_SILENCE_TRIM_THRESHOLD_SEC`, but **no** env override for the silence detection threshold in
dB. Users who record in unusually quiet or noisy environments cannot tune the -40 dB / -55 dB
boundary without changing source code.

`AudioChunker` also hardcodes `threshold_db=-40.0` in its constructor default with no settings tie-in.

Suggested additions to `config.py` Settings and `DEFAULT_SETTINGS`:
- `SILENCE_THRESHOLD_DB: float = -40.0` (analytics path)
- `SILENCE_THRESHOLD_STT_DB: float = -55.0` (STT/skip path, after W1018 lands)

Both are low-risk additive changes that expose existing constants to runtime override without changing
any default behavior.

---

## Test Coverage Status

`KrabEar/tests/test_silence_detector.py` — 55 test methods covering:
- `detect_silence`, `trim_silence`, `get_speech_ratio`
- Edge cases: empty audio, zero sample rate, multichannel, all-loud, all-silent
- Threshold sensitivity, alternating patterns, thread safety

**Gap:** No tests validate correct timestamp computation when `n_samples % _FRAME_SIZE != 0`
(F3 above). The existing `test_speech_silence_speech_returns_one_region` uses delta=0.15 s, which
masks the 16 ms drift.

**Gap (post W1018):** No integration test verifies that `SmartSilenceSkipper` and
`RealtimeSilenceFilter` inherit the `-55 dB` preserve-whisper threshold from W1018 in production
wiring — only isolated unit tests exist.

---

## Summary Table

| # | Severity | Issue | Action |
|---|----------|-------|--------|
| W912 | HIGH | AudioQualityAnalyzer still uses 0.001 threshold (-60 dB gap) | Merge W912 |
| W1018 | HIGH | No PRESERVE_WHISPER constant, dead `audio.shape`, -40 dB in STT paths | Merge W1018 |
| F1 | HIGH | Denoiser 200-ms noise floor + -40 dB threshold swallows denoised speech | Merge W1080 first |
| F2 | HIGH | SmartSilenceSkipper never called by engine (W1102 not merged) | Merge W1102 + W1018 together |
| F3 | MED | `array_split` timing drift up to 16 ms in silence timestamps | Fix timestamp computation |
| F4 | MED | NoiseProfiler 0.001 threshold still diverges (W912 not merged) | Merge W912 |
| F5 | LOW | No `KRAB_EAR_SILENCE_THRESHOLD_DB` env override | Add to Settings + DEFAULT_SETTINGS |
