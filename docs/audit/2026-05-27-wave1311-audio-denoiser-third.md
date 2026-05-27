# AudioDenoiser Third-Pass Audit — W1311

**Date:** 2026-05-27
**Auditor:** W1311 third-pass sub-agent
**File:** `KrabEar/core/audio_denoiser.py`
**Baseline fixes:** W1062 (initial audit), W1080 (percentile noise floor + bounded strong attenuation, PR #1002 MERGED)
**Prior audits:** W1062 (PR #981 OPEN), W1112 (PR #1020 OPEN)
**Scope:** New residual issues after W1080 merge; focus on percentile threshold sensitivity on uniform-loud audio, all-silence edge case, >30 min performance, SmartSilenceSkipper chain ordering, post-fix test coverage.

---

## W1080 / W1062 Merge State

| Fix | Branch | PR | Status |
|-----|--------|----|--------|
| W1080 (percentile noise floor + strong mode cap) | `fix-audio-denoiser-W1080` | #1002 | **MERGED** — commit `70f21713` confirmed in `origin/codex/krab-ear-v2` |
| W1071 (strong-mode whisper downgrade) | `fix-audio-denoiser-whisper-W1071` | #990 | OPEN (not merged) |
| W1067 (quietest-window noise sampling) | `fix-audio-denoiser-noise-floor-W1067` | #989 | OPEN (not merged) |
| W1062 initial audit doc | `audit-audio-denoiser-W1062` | #981 | OPEN doc-only |
| W1112 residual audit doc | `audit-audio-denoiser-residual-W1112` | #1020 | OPEN doc-only |

**W1080 is the canonical fix for W1062 F1+F2 and supersedes W1067/W1071. Those older branches are redundant and can be closed.**

Verification:
```bash
git show origin/codex/krab-ear-v2:KrabEar/core/audio_denoiser.py | grep "_percentile_noise_clip\|_STRONG_MIN_GAIN"
# Returns: both symbols present — W1080 fix confirmed in main.
```

---

## Residual Findings (NEW — post-W1080)

Audited against `origin/codex/krab-ear-v2` (commit `62df2ec9`), with W1080 fully applied.

---

### F1 — HIGH: `quiet_mask = rms_per_frame <= threshold` selects ALL frames for uniform-loud audio

**File:** `KrabEar/core/audio_denoiser.py`, `_percentile_noise_clip()`, line computing `quiet_mask`
**Reproducible:**
```python
audio = np.ones(16000 * 60, dtype=np.float64) * 0.5  # constant 0.5
# np.percentile([0.5, 0.5, ...], 10.0) = 0.5
# rms_per_frame <= 0.5 → ALL frames qualify as "quiet"
# noise_clip = entire 60s audio (= 58 MB for float64)
```

**Root cause:** `np.percentile(x, 10.0)` returns the 10th percentile value, which equals the minimum RMS (and all values) when audio has uniform amplitude. The `<=` comparison then selects every frame as "quiet", including loud speech frames that happen to all have the same energy.

**Impact matrix:**

| Audio type | quiet_mask | noise_clip | Denoising outcome |
|-----------|------------|------------|-------------------|
| Normal mixed speech+silence | ~10% of frames | ~10% of audio | Correct |
| All-silence (zeros) | 100% of frames | Entire audio (220 MB for 30 min) | OOM risk, correct acoustically |
| Uniform loud (constant tone, music, pink noise, crowd) | 100% of frames | Entire audio | Noise floor = loud speech → denoiser REMOVES all speech |
| Near-uniform (AC hum, HVAC noise) | Up to 100% | Near-entire audio | Same catastrophic suppression |

**Severity escalation vs W1112 F1 (which noted this as MEDIUM):** The effect is not merely "miscalibrated noise floor" — it is complete speech removal for any continuous-noise recording (teleconference background hum, live event, street recording). This is a correctness regression from W1080 in those environments.

**Fix:** Change `<=` to `<` in the quiet_mask line:
```python
# Current (buggy for uniform audio):
quiet_mask = rms_per_frame <= threshold

# Fixed:
quiet_mask = rms_per_frame < threshold
# If quiet_mask is empty (all frames tie at percentile), falls through to existing
# "no quiet frames" fallback which returns first 200ms — correct behavior.
```

**Verification test missing:** `test_percentile_noise_clip_fallback_all_loud` in the test suite uses `np.ones(sr*2) * 0.9` and correctly expects the fallback path, but asserts `rms > 0.5` which passes with BOTH the buggy and fixed behavior (because when all frames are loud, returning first 200ms gives rms ~0.9 either way). The test does NOT verify that `quiet_mask` was NOT all-True before the fallback. The `<=` vs `<` bug is invisible to the existing test.

---

### F2 — MEDIUM: `_percentile_noise_clip` allocates O(N) for uniform/all-silence audio on long recordings

**File:** `KrabEar/core/audio_denoiser.py`, `_percentile_noise_clip()`, `frames[quiet_mask].ravel()`
**Benchmark results (M4 Max, 16 kHz float64):**

| Audio duration | Normal (10% quiet) | All-silence / uniform-loud |
|---------------|---------------------|---------------------------|
| 5 min (max live recording) | 11 MB noise_clip, ~8 ms | 110 MB noise_clip (100% frames) |
| 30 min (file import) | 22 MB noise_clip, ~48 ms | 220 MB noise_clip, ~100 ms |
| 60 min (file import) | 44 MB noise_clip, ~43 ms | 439 MB noise_clip |

For the STFT path: `sig_stft` matrix for 30 min audio is **882 MB (complex128)**. When `noisereduce` is absent and scipy STFT processes the full 30 min signal (no duration guard), peak memory exceeds **7 GB** on the 36 GB M4 Max but would swap or OOM on 8–16 GB systems. This applies to all recordings, not just silence/uniform cases.

**The `_maybe_denoise` docstring states "applied only to numpy arrays, i.e. live recordings" but there is no duration cap.** The `STT_STREAMING_MIN_AUDIO_SEC` default is 30 s and `STT_STREAMING_ENABLED = False`, meaning a 5-minute numpy recording (from `AudioRecorder`, max `MAX_DURATION_SEC = 300 s`) goes through the full STFT path with a 882 MB intermediate matrix. This is reproducible today on any 5-minute recording with the spectral gating fallback active.

**Fix:** Add a `_MAX_NOISE_CLIP_SAMPLES` cap in `_percentile_noise_clip`:
```python
_MAX_NOISE_CLIP_SAMPLES = 16000 * 5  # 5 seconds max noise sample (80 KB)

# After quiet frames selection:
noise_clip = frames[quiet_mask].ravel()
if len(noise_clip) > _MAX_NOISE_CLIP_SAMPLES:
    noise_clip = noise_clip[:_MAX_NOISE_CLIP_SAMPLES]
```

This keeps the noise sample representative (5 s of quiet frames is more than sufficient for spectral profiling) while capping the `nr.reduce_noise(y_noise=...)` and STFT call overhead.

---

### F3 — MEDIUM: `_denoise_noisereduce` does not receive `strength` — speech-band floor missing

**File:** `KrabEar/core/audio_denoiser.py`, `_denoise_noisereduce()` signature and `denoise()` call site

W1080 added `_STRONG_MIN_GAIN = 0.25` speech-band protection but only for `_denoise_spectral_gating`. The `_denoise_noisereduce` method still receives only `params` (which has `prop_decrease=0.95` for strong mode) and applies no post-hoc speech-band floor.

```python
# denoise():
if self._has_noisereduce:
    denoised = self._denoise_noisereduce(mono, sample_rate, params)  # strength NOT passed
else:
    denoised = self._denoise_spectral_gating(mono, sample_rate, params, strength)  # strength passed
```

When `noisereduce` is installed (the PREFERRED backend), a user using `strong` mode on whispering gets the pre-W1080 behavior: unlimited attenuation, up to 95% signal reduction across all bands including speech. The W1080 fix for F2 is silently absent for the majority of production installs.

This is the same finding as W1112 F4, but confirmed as still unresolved in `origin/codex/krab-ear-v2`.

**Fix (minimal):** Pass `strength` to `_denoise_noisereduce` and cap `prop_decrease` when strength is `"strong"`:
```python
@staticmethod
def _denoise_noisereduce(audio, sample_rate, params, strength="moderate"):
    ...
    prop = params["prop_decrease"]
    # W1062 F2: strong mode — mirror spectral gating cap (0.25 floor = 75% max decrease)
    if strength == "strong":
        prop = min(prop, 0.75)
    result = nr.reduce_noise(y=audio, sr=sample_rate, y_noise=noise_clip,
                             prop_decrease=prop, ...)
```

---

### F4 — LOW: RealtimeSilenceFilter zero_silence_ranges runs before denoiser — percentile noise sampling is biased by zeroed regions

**File:** `KrabEar/core/engine.py`, steps 2.4 and 2.5; `KrabEar/core/audio_denoiser.py`

The engine pipeline in `transcribe()` is:
```
Step 2.4: zero_silence_ranges(audio_data, silence_ranges)  ← silence regions set to 0.0
Step 2.5: _maybe_denoise(audio_data)                       ← percentile noise sampling
```

`zero_silence_ranges` zeroes out detected silence spans in-place. When `RealtimeSilenceFilter` has identified 20 s of silence in a 60 s recording, the resulting `audio_data` has a large zero region. `_percentile_noise_clip` will preferentially select these zeroed frames as the "quietest frames" (RMS = 0.0), producing a noise floor estimate of ~0.0.

With a near-zero noise floor:
- `noise_thresh = 0 + n_std * 0 = 0`
- `mask = np.where(sig_amp >= 0, 1.0, ...) = 1.0` everywhere (no suppression)
- Denoiser becomes a no-op for the non-silence audio segments

**Impact:** When `realtime_silence_filter_enabled=True` (opt-in, default False), the denoiser silently does nothing for recordings with detected silence regions — the noise suppression that the user requested is bypassed with no log message.

**Recommendation:** Strip or ignore zero-valued frames in `_percentile_noise_clip` before computing the percentile, or add a minimum RMS floor check:
```python
# Filter out zero frames (from zero_silence_ranges) before noise floor estimation
nonzero_mask = rms_per_frame > 1e-6  # exclude zeroed silence ranges
if np.any(nonzero_mask):
    rms_for_percentile = rms_per_frame[nonzero_mask]
else:
    rms_for_percentile = rms_per_frame
threshold = float(np.percentile(rms_for_percentile, _NOISE_PERCENTILE))
```

---

### F5 — LOW: `_speech_band_bins` raises `ZeroDivisionError` when called with `sample_rate=0`

**File:** `KrabEar/core/audio_denoiser.py`, `_speech_band_bins()`

```python
def _speech_band_bins(sample_rate: int) -> tuple[int, int]:
    bin_low = int(round(_SPEECH_BAND_LOW_HZ * _N_FFT / sample_rate))  # ZeroDivisionError if sr=0
    bin_high = int(round(_SPEECH_BAND_HIGH_HZ * _N_FFT / sample_rate))
```

`sample_rate` is hardcoded to `16000` in `_maybe_denoise`, but `_denoise_spectral_gating` accepts an arbitrary `sample_rate` parameter and is called with `sample_rate` from the caller. If `sample_rate=0` is passed (e.g., from a corrupted `sounddevice` buffer or a mock in tests), `_speech_band_bins` raises uncaught `ZeroDivisionError` inside the `strong` mode branch.

The outer `try/except Exception` in `_maybe_denoise` catches this and falls back to unprocessed audio, so there is no crash — but the silent fallback masks the corrupted input without any specific diagnostic.

**Fix:** Add a guard at the start of `_speech_band_bins`:
```python
def _speech_band_bins(sample_rate: int) -> tuple[int, int]:
    if sample_rate <= 0:
        logger.warning("[Denoiser] sample_rate=%d неверный, используем sr=16000", sample_rate)
        sample_rate = 16000
    ...
```

---

## Test Coverage Assessment (Post-W1080)

| Scenario | Test exists? | Adequate? |
|----------|-------------|-----------|
| strength="off" passthrough | Yes (`test_strength_off_*`) | Yes |
| All-silence input | Yes (`test_handles_silence`) — shape only | Inadequate: no RMS assertion |
| Uniform-loud input (F1 bug) | `test_percentile_noise_clip_fallback_all_loud` — but uses `np.ones * 0.9` | **MISSING**: does not assert that speech-level audio is NOT all treated as quiet (tests fallback path exists, not correctness) |
| All frames tied at percentile (F1 root cause) | None | **MISSING** |
| Noise clip size cap for long audio (F2) | `test_percentile_performance_60s_audio` — time only, no size check | Inadequate |
| noisereduce backend with `strong` mode (F3) | None | **MISSING** |
| zero_silence_ranges interaction (F4) | None | **MISSING** |
| sample_rate=0 guard (F5) | None | **MISSING** |

**4 missing test scenarios** (F1, F3, F4, F5) with no coverage at all.

---

## Summary Table

| # | Severity | Finding | Location | New vs W1112? |
|---|----------|---------|----------|---------------|
| F1 | HIGH | `<=` threshold includes all frames for uniform-loud audio → noise floor = speech → complete suppression | `_percentile_noise_clip()` line `quiet_mask = ...` | NEW (W1112 called it MEDIUM config; this identifies it as a correctness bug) |
| F2 | MEDIUM | No noise_clip size cap: all-silence/uniform 30 min → 220 MB noise_clip + 7+ GB STFT intermediate | `_percentile_noise_clip()` + `_denoise_spectral_gating()` | NEW scope |
| F3 | MEDIUM | `_denoise_noisereduce` never receives `strength` — W1080 F2 speech-band floor absent for noisereduce backend | `denoise()` call + `_denoise_noisereduce()` | Confirmed unresolved W1112 F4 |
| F4 | LOW | `zero_silence_ranges` zeros out audio BEFORE `_percentile_noise_clip` — zeroed frames bias noise floor to 0, denoiser becomes no-op | `engine.py` steps 2.4→2.5 ordering + `_percentile_noise_clip()` | NEW |
| F5 | LOW | `_speech_band_bins(0)` raises ZeroDivisionError — no guard | `_speech_band_bins()` | NEW |

**Total new findings: 5. W1080 merge state: MERGED (confirmed). W1062/W1112 doc PRs: OPEN (doc-only, can be merged or closed).**
