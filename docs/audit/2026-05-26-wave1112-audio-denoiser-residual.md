# AudioDenoiser Residual Audit — W1112

**Date:** 2026-05-26  
**Auditor:** W1112 re-audit sub-agent  
**File:** `KrabEar/core/audio_denoiser.py`  
**Baseline:** W1080 fix (percentile noise floor + bounded strong attenuation, W1062 F1+F2 HIGH)  
**Scope:** verify W1080 merge state; find NEW residual issues post-W1080  

---

## W1080 Merge State — CRITICAL DRIFT

**Status: NOT MERGED into `codex/krab-ear-v2`**

The W1080 fix (`_percentile_noise_clip`, `_STRONG_MIN_GAIN`, `_speech_band_bins`) exists on branch `fix-audio-denoiser-W1080` (commit `9879fc6b`) but has **not been merged** into `codex/krab-ear-v2`. Production code still uses the broken first-200ms noise floor estimate that W1080 was designed to fix.

Verification commands:
```bash
# Confirms fix is absent in main:
git show codex/krab-ear-v2:KrabEar/core/audio_denoiser.py | grep "_percentile_noise_clip\|_STRONG_MIN_GAIN"
# Returns empty.

# Confirms fix is on unmerged branch:
git log --oneline fix-audio-denoiser-W1080 ^codex/krab-ear-v2
# Returns: 9879fc6b fix(wave1080): audio_denoiser percentile noise floor + bounded strong attenuation (W1062 F1+F2 HIGH)
```

**Impact:** W1062 findings F1 (noise floor from first 200ms on recordings starting mid-speech) and F2 (strong mode over-attenuation destroying whisper) are still **live in production** as of v2.0.5.

**Action required:** Merge `fix-audio-denoiser-W1080` into `codex/krab-ear-v2` immediately.

---

## Residual Findings (NEW, post-W1080 analysis)

Audited against the W1080 branch version (`fix-audio-denoiser-W1080`) as the intended fixed state.

---

### F1 — MEDIUM: `_NOISE_PERCENTILE` hardcoded at 10%, not configurable via settings

**File:** `KrabEar/core/audio_denoiser.py`, line 51 (W1080 branch)  
**Symptom:** `_NOISE_PERCENTILE = 10.0` is a module-level constant with no `KRAB_EAR_*` env override.

For recordings in very noisy environments (crowded café, street), 10% of frames may not represent true background noise — they may be the quietest bursts within continuous loud noise. Conversely, for clean recordings, 10% may select frames containing faint reverb tails rather than true silence. The percentile value is a domain-sensitive parameter.

`config.py` exposes `STT_DENOISE_ENABLED`, `STT_DENOISE_SNR_THRESHOLD_DB`, and `STT_DENOISE_STRENGTH` but no `STT_DENOISE_NOISE_PERCENTILE`. The W1080 fix thus introduced a tuning parameter that the user cannot adjust without editing source code.

**Recommendation:** Add `STT_DENOISE_NOISE_PERCENTILE: float = 10.0` to `Settings` in `core/config.py` and thread it through `_percentile_noise_clip(audio, sample_rate, percentile=settings.STT_DENOISE_NOISE_PERCENTILE)`. Low priority but worth registering as a configuration debt item.

---

### F2 — MEDIUM: SmartSilenceSkipper is unwired — denoiser ordering gap when W1102 lands

**Files:** `KrabEar/core/engine.py`, `KrabEar/core/smart_silence_skipper.py`  
**Symptom:** `SmartSilenceSkipper` is implemented (`core/smart_silence_skipper.py`, config key `smart_silence_skip_enabled=False`) but is **not wired into `engine.py`** in `codex/krab-ear-v2` or in the W1102 branch (`wire-smart-silence-skipper-W1102`).

The concern is ordering: in `engine.py`, the current pipeline sequence is:
1. silence_ranges zero-out (step 2.4)
2. `AudioDenoiser._maybe_denoise()` (step 2.5)
3. VAD pre-filter
4. STT

When `SmartSilenceSkipper` is eventually wired in (W1096 audit identified this as CRITICAL), it must run **before** `AudioDenoiser`. If it runs after, the percentile noise floor in W1080's `_percentile_noise_clip` will sample from already-skipped audio — potentially selecting speech transients as "quiet frames" if silence removal changes the amplitude distribution. The W1080 fix does not account for post-skip audio characteristics.

**Recommendation:** Document the required ordering constraint in `audio_denoiser.py` docstring and in `engine.py` comments at step 2.5: "SmartSilenceSkipper must run before AudioDenoiser when wired". Flag this in the W1102/W1096 implementation ticket.

---

### F3 — LOW: "all loud audio" fallback path logs at WARNING but is not instrumented for Sentry

**File:** `KrabEar/core/audio_denoiser.py`, `_percentile_noise_clip()` function, lines ~100–107 (W1080 branch)

When all audio frames are above the 10th-percentile RMS threshold (i.e., `not np.any(quiet_mask)`), the function logs:
```python
logger.warning("[Denoiser] нет тихих фреймов в аудио (всё громкое); fallback на первые 200 мс для noise floor")
```
and falls back to the original broken behavior (first 200ms). This is exactly the class of recordings where the pre-W1080 noise floor estimate was worst — yet the fallback silently re-introduces the bug without any Sentry signal.

Additionally, no test in the existing test suite (`test_audio_denoiser.py` via commit `75375d6a`) covers this fallback path: synthetic "all loud audio" scenarios are not present.

**Recommendation:** 
1. Add `self._push_error("denoiser.all_loud_fallback", ...)` or at minimum call `capture_message` via `backend/observability.py` when this path is hit.
2. Add a unit test with a pure-sine wave (constant amplitude, no quiet frames) to verify fallback behavior.

---

### F4 — LOW: `_denoise_noisereduce` does not apply `_STRONG_MIN_GAIN` speech-band protection

**File:** `KrabEar/core/audio_denoiser.py`, `_denoise_noisereduce()` method (W1080 branch)

W1080 F2 fix added `_STRONG_MIN_GAIN = 0.25` protection for the speech band (300–3000 Hz) in `strong` mode, but only in the `_denoise_spectral_gating()` fallback path. The `noisereduce` backend (`_denoise_noisereduce()`) passes `prop_decrease=0.95` (from `_STRENGTH_PARAMS["strong"]`) directly to `nr.reduce_noise()` with no speech-band floor.

When `noisereduce` is installed (the preferred backend), the whisper-protection fix is **silently absent**. A user who has `noisereduce` installed and uses `strong` mode gets worse behavior than one without it.

**Recommendation:** After `nr.reduce_noise()` returns, apply a post-hoc speech-band minimum gain:
```python
if strength == "strong":
    result = _apply_speech_band_floor(result, sample_rate)
```
where `_apply_speech_band_floor` uses STFT/ISTFT to lift the 300–3000 Hz band. This is the same logic as W1080's `_denoise_spectral_gating` step 4, extracted into a helper.

Alternatively, document that `noisereduce`'s `prop_decrease` parameter doesn't map directly to a per-band floor and lower `"strong"` to 0.80 in `_STRENGTH_PARAMS` when `noisereduce` is present.

---

### F5 — LOW: Performance regression: percentile computation runs even when `noisereduce` is unavailable and scipy is also unavailable

**File:** `KrabEar/core/audio_denoiser.py`, `_denoise_spectral_gating()` (W1080 branch)

`_percentile_noise_clip()` is called unconditionally at the start of `_denoise_spectral_gating()` before checking `scipy` availability:
```python
# 1. W1062 F1: Noise floor estimate по тихим фреймам (10-й перцентиль RMS)
noise_clip = _percentile_noise_clip(audio, sample_rate)

_, _, noise_stft = stft(...)  # ImportError if scipy absent
```

If `scipy` is not installed, the function catches the `ImportError` and returns the original `audio` unchanged — but `_percentile_noise_clip` has already spent O(n_frames) computing RMS values and a percentile that are discarded. For a 60-second audio clip at 16 kHz this is ~1860 frame RMS computations wasted.

This is a minor performance issue but represents unnecessary work in a graceful-degradation path.

**Recommendation:** Guard the scipy import check before calling `_percentile_noise_clip`:
```python
try:
    from scipy.signal import stft, istft
except ImportError:
    logger.warning("[Denoiser] scipy не установлен, spectral gating пропущен")
    return audio

noise_clip = _percentile_noise_clip(audio, sample_rate)
# ... rest of function
```

---

## Summary Table

| # | Severity | Finding | File | Merge Required? |
|---|----------|---------|------|-----------------|
| CRITICAL | CRITICAL | W1080 fix NOT merged into codex/krab-ear-v2 | `audio_denoiser.py` | Yes — `fix-audio-denoiser-W1080` |
| F1 | MEDIUM | `_NOISE_PERCENTILE=10.0` hardcoded, no config override | `audio_denoiser.py:51` | No (enhancement) |
| F2 | MEDIUM | SmartSilenceSkipper ordering undocumented (W1102 ordering risk) | `engine.py` / `audio_denoiser.py` | No (documentation) |
| F3 | LOW | "all loud audio" fallback: no Sentry signal + missing test | `audio_denoiser.py:100` | No (observability) |
| F4 | LOW | `noisereduce` backend missing `_STRONG_MIN_GAIN` speech-band floor | `audio_denoiser.py:_denoise_noisereduce` | No (W1080 fix gap) |
| F5 | LOW | Percentile computed before scipy availability check — wasted O(n_frames) | `audio_denoiser.py:_denoise_spectral_gating` | No (minor perf) |

**Total new findings: 5 (F1–F5). W1080 merge state: CRITICAL DRIFT.**
