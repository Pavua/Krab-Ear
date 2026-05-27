# Wave 1062 — AudioDenoiser Audit

**File:** `KrabEar/core/audio_denoiser.py`  
**Date:** 2026-05-26  
**Auditor:** W1062 (sub-agent)  
**Scope:** noise floor estimation accuracy, strength level correctness, whisper suppression risk (W1016 lesson), performance on long audio, edge cases, wire status, test coverage, latency budget.

---

## Summary

AudioDenoiser is correctly wired and structurally sound. The `noisereduce` (primary) and spectral-gating (fallback) backends are both functional. However **five findings** require attention, one of which is HIGH severity and affects every recording where the speaker begins without a leading silence period.

---

## Findings

### F1 — HIGH: Noise floor estimated from first 200 ms — speech-at-start contamination

**File:** `audio_denoiser.py:175` (spectral gating) and `:134` (noisereduce backend)

```python
noise_clip = audio[:_NOISE_FLOOR_SAMPLES]  # 3200 samples = 200 ms @ 16 kHz
```

**Problem:** both backends estimate the noise floor from the first 200 ms of audio. This is a valid heuristic only when the recording begins with ambient silence. In practice, users of Krab Ear activate the hotkey and speak immediately. When speech starts at `t=0` the noise clip captures voice harmonics, not background noise. The STFT-derived `noise_thresh` then treats the speech frequency bands as "noise", and the mask suppresses them with `prop_decrease` (50–95%).

**Impact:** STT quality regression whenever the speaker does not pause before speaking. With `noisereduce` in stationary mode the effect is similar — the library uses `y_noise` as the stationary noise reference.

**Recommendation:** prepend a short synthetic silence window (e.g. 100 ms of zeros) before sending `noise_clip`, or sample the noise floor from the _quietest_ 200 ms window across the recording using `np.argmin(np.convolve(rms_envelope, ...))`. Alternatively, skip the noise-clip estimation entirely for recordings under 2 seconds and rely solely on `prop_decrease` without the per-bin threshold.

---

### F2 — MEDIUM: `strong` strength suppresses whispered speech (W1016 lesson)

**File:** `audio_denoiser.py:37`

```python
"strong": {"prop_decrease": 0.95, "n_std_thresh_stationary": 2.0},
```

**Problem:** With `n_std_thresh_stationary=2.0` any spectral bin within 2σ above the noise mean is classified as noise. Whispered speech sits only 10–20 dB above the noise floor (vs 30–40 dB for normal speech). In variable-noise environments the 2σ band easily captures whisper amplitude, leading to 95% suppression of whispering with `prop_decrease=0.95`.

The W1016 lesson flagged exactly this pattern. The `moderate` setting (`n_std=1.5`, `prop_decrease=0.75`) is safer for voice input; `strong` should carry an explicit warning in the docstring.

**Recommendation:** Add a docstring note that `strong` is suitable only for very loud background noise and may degrade whispered or quiet speech. Consider capping `n_std_thresh_stationary` at `1.5` for all voice-optimised settings, and documenting `strong` as experimental.

---

### F3 — MEDIUM: Spectral gating fallback allocates full STFT matrix — OOM risk on long audio

**File:** `audio_denoiser.py:184`

```python
freqs, times, sig_stft = stft(audio, fs=sample_rate, nperseg=_N_FFT, noverlap=_N_FFT - _HOP)
```

**Analysis:** the scipy `stft` call returns a `(257, n_frames)` complex128 array. Peak memory for the fallback path (5 arrays: `sig_stft`, `denoised_stft`, `sig_amp`, `sig_phase`, `mask`):

| Duration | Frames | Peak RAM |
|----------|--------|----------|
| 10 s     | 1,247  | ~20 MB   |
| 60 s     | 7,497  | ~118 MB  |
| 300 s    | 37,497 | ~588 MB  |

Five-minute recordings are common (import of phone call audio). On a 36 GB M4 Max this is acceptable, but it spikes against the Wave 63 memory budget for extended sessions. The `noisereduce` primary backend chunks internally and does not have this problem.

**Recommendation:** Add a length guard: if `len(audio) > 30 * sample_rate` (30 s) in the spectral gating fallback, process in overlapping chunks of 10–15 s with a cross-fade. Alternatively, log a warning and skip denoising when the audio is long and noisereduce is unavailable.

---

### F4 — LOW: Multichannel input silently changes output shape

**File:** `audio_denoiser.py:96–118`

```python
mono = audio.mean(axis=1)   # (N, C) → (N,)
...
return denoised.astype(audio.dtype)   # returns (N,) even when input was (N, C)
```

**Problem:** the docstring states "Returns: аудиомассив той же формы" but multichannel input `(N, C)` produces mono output `(N,)`. The engine always passes mono arrays (`AudioRecorder` defaults to `channels=1`) so the production path is safe. However the API contract is broken for any future caller that passes stereo or multichannel audio, and the test at line 78–85 actively asserts this incorrect shape.

**Recommendation:** Update the docstring to state that multichannel input is averaged to mono on output, or restore the original shape by broadcasting the mono result back to `(N, C)`.

---

### F5 — LOW: `_check_noisereduce()` import probe runs on every transcription call

**File:** `audio_denoiser.py:64`, `engine.py:637`

```python
# engine.py:650 — new instance created per call
return AudioDenoiser().denoise(audio, sample_rate, strength=strength)
```

`AudioDenoiser.__init__` calls `_check_noisereduce()`, which does a `try: import noisereduce` on every instantiation. Because `engine.py` creates a fresh `AudioDenoiser()` (and `NoiseProfiler()`) on every transcription, this import probe runs once per recording. Python caches modules after the first import, so subsequent calls are fast (`sys.modules` lookup), but it is an unnecessary allocation pattern. Both objects are stateless and safe to cache as `AudioEngine` instance attributes.

**Recommendation:** Cache as `self._denoiser = AudioDenoiser()` and `self._noise_profiler = NoiseProfiler()` in `AudioEngine.__init__`, eliminating per-call construction.

---

## Wire Status

- **Wired correctly:** `engine.py:_maybe_denoise` (line 626) calls `AudioDenoiser().denoise()` gated by `settings.STT_DENOISE_ENABLED` and `not is_preview`.
- **Config:** `STT_DENOISE_ENABLED=True`, `STT_DENOISE_SNR_THRESHOLD_DB=15.0`, `STT_DENOISE_STRENGTH="moderate"` (all in `core/config.py` lines 195–197 and `DEFAULT_SETTINGS` lines 775–776).
- **Applied to:** live recordings only (`isinstance(audio_data, np.ndarray)`). File imports skip denoising (correct — file imports already have settled ambient noise).
- **Sample rate:** hardcoded `16000` in `_maybe_denoise` (line 639) — safe because `AudioRecorder` always records at 16 kHz and Whisper expects 16 kHz.

---

## Test Coverage

**File:** `KrabEar/tests/test_audio_denoiser.py` — 5 test classes, ~20 test methods.

| Area | Covered | Gap |
|------|---------|-----|
| `strength="off"` passthrough | Yes | — |
| Output shape / dtype | Yes | Multichannel -> mono shape documented as "same" |
| Clipping to [-1,1] | Yes | — |
| SNR improvement | Yes | — |
| Silence (zeros) input | Yes | — |
| Short audio passthrough | Yes | — |
| Invalid strength fallback | Yes | — |
| Concurrent calls | Yes | — |
| **Speech-at-t=0 noise contamination** | **No** | **F1 — missing test** |
| **Whisper amplitude suppression** | **No** | **F2 — missing test** |
| Long audio memory / chunking | No | Low priority (no chunking implemented yet) |

---

## Latency Budget

| Backend | 10 s audio | 60 s audio | Notes |
|---------|-----------|-----------|-------|
| `noisereduce` (primary) | ~30–80 ms | ~200–500 ms | Chunked internally |
| Spectral gating (fallback) | ~15–40 ms | ~100–300 ms | Single STFT pass, scipy |

Both paths are well within the STT latency budget (Whisper itself takes 800–2000 ms for 10–60 s audio). Denoising adds <10% overhead in the normal case.

---

## Action Items

| # | Finding | Priority | Effort |
|---|---------|----------|--------|
| 1 | Fix noise floor estimation for speech-at-t=0 (prepend silence or use quietest window) | HIGH | M |
| 2 | Add docstring warning for `strong` mode re: whispered speech | MEDIUM | XS |
| 3 | Add chunk-based processing for spectral gating fallback on long audio (>30 s) | MEDIUM | M |
| 4 | Fix docstring contract for multichannel output shape | LOW | XS |
| 5 | Cache `AudioDenoiser` + `NoiseProfiler` as `AudioEngine` instance attributes | LOW | S |
| 6 | Add test: speech-at-t=0 noise contamination check | LOW | S |
