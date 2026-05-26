# Wave 1061 — Audit: `core/audio_lang_id.py` AudioLanguageID

**Date:** 2026-05-26  
**Auditor:** Claude Sonnet 4.6 (W1061)  
**Scope:** `KrabEar/core/audio_lang_id.py` (285 lines), cross-referenced against `core/stt_router.py`, `core/config.py`, `KrabEar/tests/test_audio_lang_id.py`, `KrabEar/tests/test_audio_lang_id_cache_limit.py`, and `core/engine.py`.

---

## Summary

`AudioLanguageID` is a well-structured encoder-only MLX inference module. mlx_lock usage is correct. The W63 model cache bound (max 1 entry) is implemented and tested. Privacy is clean — no audio ever leaves memory. Wire status is active: `STTRouter._try_audio_lid` is the sole production caller. **6 findings**: 1 MEDIUM bug (zero-signal divide risk), 1 MEDIUM gap (no confidence threshold → noisy/music inputs accepted without filter), 2 LOW issues (missing mx.clear_cache after inference; `_model_cache` class-var shared across all instances), 1 INFO (linear resampler quality), 1 INFO (test gap for music/noise edge cases).

---

## Findings

### F1 — No guard against zero-peak audio before normalization (BUG, MEDIUM)

**File:** `core/audio_lang_id.py`, lines 243–245

```python
peak = float(np.max(np.abs(audio_norm)))
if peak > 1.0:
    audio_norm = audio_norm / peak
```

When audio is all-zeros (pure silence padded to 30 s), `peak == 0.0`. The conditional `if peak > 1.0` is false so no division occurs — this is fine numerically. However the log-mel spectrogram of all-zeros produces a valid (all-minimum) mel tensor and `detect_language()` will still run, returning whatever language the model associates with silence (often "en" or "ru" at low confidence). The result propagates to `STTRouter` as a definitive language choice, potentially routing a silent recording to the wrong STT model.

The W63 engine pattern also normalises peak (`peak = max(abs(audio))`) with a `max(peak, 1e-5)` guard to prevent exact zero. `AudioLanguageID` lacks this guard.

**Fix:** add `if peak < 1e-5: return None  # near-silence, skip LID` before the mel build. The STTRouter already handles `None` from `_try_audio_lid` correctly (falls back to "ru").

---

### F2 — No confidence threshold on detect_language result (MEDIUM)

**File:** `core/audio_lang_id.py`, lines 262–281

`detect_language()` from mlx-whisper returns a probability dict `{lang: float}`. When the result is a `dict`, the code takes `argmax` and returns the top language — but never checks the probability value. For music, background noise, or code-switched speech the top-language probability can be as low as 0.2–0.3, yet the code returns it with the same authority as a 0.98 confident detection.

`engine.py` also calls mlx-whisper's detect_language at a higher level but similarly has no confidence gate in the same path.

**Consequence:** noisy or musical inputs will silently select a low-confidence language, causing the wrong STT model to be loaded. In the code-switched RU/ES user case (primary language per CLAUDE.md), a Spanish snippet at the start of a recording could pick "es" over "ru" at 0.35 vs 0.30 confidence.

**Fix:** when `result` is a `dict`, check `max_prob = result[lang_code]` and return `None` if `max_prob < threshold` (configurable via `STT_AUDIO_LANG_ID_CONFIDENCE_THRESHOLD`, default 0.5). Add this setting to `core/config.py` and `DEFAULT_SETTINGS`.

---

### F3 — mx.clear_cache() not called after inference (LOW, W63 pattern)

**File:** `core/audio_lang_id.py`, lines 202–207

`engine.py` calls `mx.clear_cache()` immediately after every `mlx_whisper.transcribe()` call to prevent Metal buffer growth during long sessions (W63 fix, PR #405). `AudioLanguageID._detect_with_mlx` does not call `mx.clear_cache()` after `log_mel_spectrogram` + `detect_language`. On long recording sessions with frequent LID calls (e.g. every file import), Metal buffer residue accumulates.

**Severity context:** LID is encoder-only (~50 ms, no decode), so the allocation per call is smaller than a full transcribe. This is LOW rather than MEDIUM. Still, aligning with the W63 pattern closes the leak path.

**Fix:** add `import mlx.core as mx; mx.clear_cache()` after `detect_language()` succeeds inside `_detect_with_mlx`, mirroring `engine.py` lines 545–546 and 920–921.

---

### F4 — `_model_cache` is a class-level variable shared across all instances (LOW)

**File:** `core/audio_lang_id.py`, line 43

```python
_model_cache: Dict[str, Any] = {}
```

This is a class variable, not an instance variable. Multiple `AudioLanguageID` instances (e.g. one in `STTRouter`, a future one in a background batch worker) share the same cache dict. The W63 bound (max 1 entry) still holds, but concurrent access by two instances using different model paths will cause the cache to thrash: instance A loads model-a, instance B evicts it and loads model-b, instance A evicts model-b, etc.

Currently there is only one production instantiation (`STTRouter._lang_id`), so this is dormant. But it is an invisible contract that is not enforced and will bite if a second instance is ever added.

**Fix:** document the single-instance assumption in the class docstring, or convert `_model_cache` to an instance variable (`self._model_cache = {}`) so each instance manages its own 1-entry cache independently. The class-level cache as a "shared singleton" is implicit and fragile.

---

### F5 — Linear resampler quality gap for downsampling from high sample rates (INFO)

**File:** `core/audio_lang_id.py`, lines 172–188

The `_resample` static method uses `np.interp` (linear interpolation). For downsampling from 44100 Hz or 48000 Hz to 16000 Hz, linear interpolation without a prior anti-aliasing low-pass filter introduces aliasing artefacts. The docstring acknowledges this ("грубый спектральный отпечаток") and says it is sufficient for LID.

This is accurate for high-confidence clean speech. However, for music or narrowband phone audio the aliasing may produce spurious high-frequency energy in the mel spectrogram, further degrading confidence (compounding F2). For the project's primary use case (16 kHz mic input), `sample_rate == 16000` so the resample path is never triggered — the risk is limited to imported audio files.

**Recommendation (INFO, no fix required):** add a one-line comment noting that this path is only exercised during audio file import, not live mic capture, to prevent future confusion.

---

### F6 — Missing test coverage for music/noise edge cases and zero-peak guard (INFO)

**Files:** `KrabEar/tests/test_audio_lang_id.py` (29 tests), `KrabEar/tests/test_audio_lang_id_cache_limit.py` (4 tests)

Existing test coverage is strong for:
- Too-short audio, empty array, None input
- Cache hit / miss / eviction boundary
- mlx_lock called per inference (concurrent test)
- mlx_whisper ImportError, log_mel failure, runtime errors
- Stereo→mono, resample, disabled flag

Missing coverage:
1. **Zero-peak (all-zeros) audio 2 s long** — should currently succeed and return a language (F1 unguarded), or return None after fix.
2. **Low-confidence dict result** — verify behaviour when max probability < 0.5 (F2; no `STT_AUDIO_LANG_ID_CONFIDENCE_THRESHOLD` setting yet).
3. **Tuple result format from detect_language** — the code handles `(lang_code, probs_dict)` tuples but there is no test exercising this branch (only the `str` and `dict` branches are tested).

---

## Wire Status

`AudioLanguageID` is wired and active:

- **Production caller:** `STTRouter._try_audio_lid()` → `STTRouter._detect_language_from_audio()` → called on every routing decision when `STT_AUDIO_LANG_ID_ENABLED=True` (default).
- **Setting guards:** `STT_AUDIO_LANG_ID_ENABLED` and `STT_AUDIO_LANG_ID_PREVIEW_SEC` both present in `core/config.py` and `DEFAULT_SETTINGS` dict.
- **No direct IPC handler** — LID is internal to the STT routing layer, not exposed as a standalone IPC method. This is correct architecture.
- **Privacy:** audio PCM arrays are processed in-memory only. No persistence, no logging of audio content, no disk writes. Privacy posture is clean.

---

## mlx_lock Compliance

**PASS.** `mlx_lock()` is correctly applied in `_run_detect` (line 203), wrapping both the model load and the `detect_language` call inside `_detect_with_mlx`. The import of `mlx_lock` is at module top-level (line 25). The `_to_mono` and `_resample` static methods are pure numpy and correctly outside the lock.

---

## Model Cache Compliance (W63)

**PASS.** `_model_cache` is bounded to 1 entry (lines 222–224). Eviction is triggered before any new model is loaded. This matches the W63 lesson. `test_audio_lang_id_cache_limit.py` explicitly tests the eviction boundary with 10 sequential model paths.

---

## Action Items

| # | Severity | Fix |
|---|----------|-----|
| F1 | MEDIUM | Add `if peak < 1e-5: return None` guard before mel build |
| F2 | MEDIUM | Gate on `max_prob >= STT_AUDIO_LANG_ID_CONFIDENCE_THRESHOLD` (default 0.5); add setting to config |
| F3 | LOW | Call `mx.clear_cache()` after inference in `_detect_with_mlx` |
| F4 | LOW | Document single-instance assumption or convert `_model_cache` to instance variable |
| F5 | INFO | Add comment noting resample path is import-only, not live-mic |
| F6 | INFO | Add tests for zero-peak, low-confidence dict, and tuple result format |
