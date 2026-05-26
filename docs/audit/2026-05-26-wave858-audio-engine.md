# Audit: AudioEngine (core/engine.py) — Wave 858

**Date:** 2026-05-26  
**File:** `KrabEar/core/engine.py` (3093 lines)  
**Scope:** MLX lock compliance · STT fallback chain ordering · hallucination strip patterns · memory/temp-file leaks  

---

## Summary

9 findings total: 1 HIGH (hardcoded credential), 3 MEDIUM (temp-file leak, per-call allocation, VoiceCommandProcessor), 5 LOW/INFO (hallucination coverage, fallback docs, Voxtral cleanup edge, diarization error log, LCS memory).

---

## Finding 1 — HIGH: Hardcoded bearer token in `_transcribe_remote`

**Location:** `engine.py:3063`

```python
headers={"Authorization": "Bearer token_here"},  # Placeholder: local gateway не требует auth
```

**Issue:** The string `"Bearer token_here"` is a literal placeholder committed to the repository. GitGuardian or any secret-scanner will flag this. Even though the inline comment says "local gateway does not require auth", the header is sent unconditionally to whatever `settings.STT_GATEWAY_URL` resolves to — including any production remote endpoint a user might configure.

**Fix:** Replace with `settings.STT_GATEWAY_TOKEN` (already exists in config pattern) or omit the `Authorization` header entirely when the token setting is empty:

```python
headers = {}
if settings.STT_GATEWAY_TOKEN:
    headers["Authorization"] = f"Bearer {settings.STT_GATEWAY_TOKEN}"
```

---

## Finding 2 — MEDIUM: Temp-file leak in `_transcribe_voxtral` — numpy branch

**Location:** `engine.py:2608-2619`

```python
# numpy array → сохраняем во временный файл
tmp_f = _tmp.NamedTemporaryFile(suffix=".wav", delete=False)
tmp_path = tmp_f.name
tmp_f.close()
try:
    _sf.write(tmp_path, audio_arr, 16000, subtype="PCM_16")
except Exception as exc:
    raise RuntimeError(f"Voxtral: не удалось сохранить аудио: {exc}")  # ← leaks tmp_path
audio_path = tmp_path
```

When `_sf.write()` raises, the function re-raises a `RuntimeError` **before** `audio_path` is set. The `finally` block on line 2660 only deletes `audio_path` if `audio_data` is not a `str/Path`. Because `audio_path` was never assigned (the `raise` happened first), the cleanup block never runs — leaving an orphaned `.wav` in `/tmp`.

The `bytes` branch (lines 2593-2607) also has a subtle gap: if `_sf.write` raises AND `wave.open` also raises, the tmp file remains because there is no `finally` around the file-creation section.

**Fix:** Track `tmp_path` before the write and add a dedicated cleanup `finally`:

```python
tmp_path: str | None = None
try:
    tmp_f = _tmp.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp_f.name
    tmp_f.close()
    _sf.write(tmp_path, audio_arr, 16000, subtype="PCM_16")
    audio_path = tmp_path
except Exception as exc:
    if tmp_path:
        try: _os.unlink(tmp_path)
        except OSError: pass
    raise RuntimeError(f"Voxtral: не удалось сохранить аудио: {exc}")
```

---

## Finding 3 — MEDIUM: `VoiceCommandProcessor` instantiated on every `transcribe()` call

**Location:** `engine.py:932-942`

```python
from core.voice_commands import VoiceCommandProcessor  # lazy — avoid circular
_vc_processor = VoiceCommandProcessor(settings_get=self._settings_get)
_vc_result = _vc_processor.process(text, language=_vc_lang)
```

A new `VoiceCommandProcessor` object is created on **every** call to `transcribe()`. If the processor has any nontrivial `__init__` work (pattern compilation, dict building), this adds latency on the hot path. The same pattern is used for `NumberNormalizer` and `DateTimeNormalizer` on lines 951 and 962.

**Fix:** Cache as `self._vc_processor` (lazy init on first call), or move instantiation to `AudioEngine.__init__`. The processor is stateless given `settings_get` is injected — one instance per engine lifetime is correct.

---

## Finding 4 — MEDIUM: `gc.collect()` called inline in hot STT path

**Location:** `engine.py:915-923`

```python
import gc as _gc
_gc.collect()
try:
    import mlx.core as _mx
    _mx.clear_cache()
except (ImportError, AttributeError):
    pass
```

`gc.collect()` is a full Python GC cycle and can take 1-20 ms depending on object graph size. Calling it synchronously after every STT transcription adds unpredictable latency on the dictation path. The `mx.clear_cache()` is correct and necessary (Wave 63 fix); the Python GC call is unnecessary because MLX Metal buffers are managed through `mx.clear_cache()`, not CPython reference counting.

**Fix:** Remove `gc.collect()` from the hot path. If GC pressure is a concern during long sessions, schedule it in a background thread or use `gc.collect(0)` (generation 0 only, much cheaper).

---

## Finding 5 — LOW: Hallucination patterns cover only RU phrases — no EN/ES equivalents

**Location:** `core/utils.py:383-401` (`_HALLUCINATION_PATTERNS`)

The pattern list covers 15 Russian YouTube/video-style endings (e.g. "спасибо за просмотр", "подписывайтесь на канал") but has zero English or Spanish equivalents. Since `cleanup_transcript()` is called for all languages including EN/ES dictation and import paths, common Whisper hallucinations in those languages pass through unstripped:

- EN: "Thank you for watching", "Please subscribe", "See you in the next video", "Music", "[Music]", "(Music)", "[Applause]"
- ES: "Gracias por ver", "Suscríbete", "Hasta la próxima"
- Whisper bracket patterns: `[MUSIC]`, `[APPLAUSE]`, `(Music)` — all language-neutral

These are well-known Whisper artefacts on audio with background music or at video boundaries. The existing `_strip_hallucinations()` function already has the right structure; adding EN/ES patterns is a one-liner per phrase.

**Recommendation:** Add at minimum the bracket patterns `\[MUSIC\]`, `\[APPLAUSE\]`, `\[BLANK_AUDIO\]` (language-neutral, extremely common Whisper noise) and at least the most frequent EN trailing phrases.

---

## Finding 6 — LOW: Fallback chain comment at line 1644-1645 is slightly misleading

**Location:** `engine.py:1644-1645`

```python
# Порядок когда оба включены: GigaAM → RU-finetune → Whisper balanced → max → remote.
```

The actual built chain when all adapters are enabled is:

```
GigaAM → RU-finetune → balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates → remote
```

The comment omits Parakeet, SenseVoice, WhisperX, and Voxtral, which were added in Phase 4. A reader following the comment to understand the chain will miss four adapters. This is a documentation gap, not a bug.

**Fix:** Update the comment to reflect the full Phase 4 chain.

---

## Finding 7 — LOW: `_run_diarization_impl` writes error log to hardcoded `/tmp` path

**Location:** `engine.py:2909`

```python
error_log_path = "/tmp/krab_ear_diarization_error.log"
```

The diarization "Black Box" block (`engine.py:2906-2917`) writes a detailed traceback to a **hardcoded** `/tmp` path. This has two issues:

1. The path does not use `settings.DATA_DIR` / `settings.LOGS_DIR` like every other log path in the project.
2. Writing to `/tmp` is fine for debugging but inconsistent with the structured logging policy. The block also uses `f"` string formatting in `logging.error()` (line 2910) rather than `%`-formatting.

**Fix (minor):** Replace the hardcoded `/tmp` path with `settings.DATA_DIR / "logs" / "diarization_errors.log"` or simply let the existing `logger.exception()` at the call site handle it without the separate file write. If the file-based trace is needed for crash investigation, use `settings.LOGS_DIR`.

---

## Finding 8 — LOW: `_lcs_length` O(m×n) memory when called from chunked path

**Location:** `engine.py:1317-1333`

`_lcs_length` allocates two lists of length `n+1` per word in `a`. For `transcribe_chunked` with `overlap_words = max(1, int(overlap_sec * 3))` (typically 6 words for a 2-second overlap), this is negligible. However if `overlap_sec` is set large (e.g. 10 s → 30 words) and chunk text is long, the `_stitch_overlap` caller passes `overlap_words * 2` as `head` length. The current code is correct but the inline comment at line 1471 saying "~3 words/s as heuristic" understates actual speech rate; Russian averages 4-5 words/s and English 2.5-3. The overlap window underestimates word count for RU which may cause stitching misses at the seam.

**Recommendation:** Use 4-5 words/s for Russian, or derive `overlap_words` from a language hint if available.

---

## MLX Lock Compliance

**Status: COMPLIANT.** All direct `mlx_whisper.transcribe()` calls (lines 1900, 1905) are inside `with mlx_lock():` (line 1892). The `warmup()` method (line 429) also correctly acquires the lock before calling `mlx_whisper.transcribe()`. `mx.clear_cache()` calls (lines 545-548, 920-923) are outside the lock which is correct — `clear_cache` does not call into Metal inference. Non-MLX adapters (GigaAM via PyTorch MPS, SenseVoice, Parakeet, WhisperX) correctly bypass the lock with inline comments explaining why.

---

## Fallback Chain Ordering

**Status: CORRECT.** The chain construction in `_transcribe_with_fallback_impl` (lines 1618-1760) correctly orders adapters:

1. GigaAM-RNNT (RU only, position 0)
2. RU-finetune Whisper (RU only, position 1)
3. Whisper balanced (primary)
4. Parakeet (EN-focused, inserted after balanced)
5. SenseVoice (multi-lang + emotion, inserted after Parakeet)
6. WhisperX (word timestamps + diarization, inserted after SenseVoice)
7. Voxtral (STT+reasoning, inserted after WhisperX)
8. Whisper max candidates
9. Remote STT (if not offline_strict)

Memory gate (`_HEAVY_MODEL_MIN_FREE_GB = 4.0 GB`) correctly applies only to non-balanced Whisper models (line 1782), skipping the check for adapters (handled by their own load guards). `_unavailable_models` set correctly prevents re-attempting failed models within a session.

---

## Memory Leak Assessment

**Status: MOSTLY CLEAN, one issue.**

- `mx.clear_cache()` after each STT (Wave 63 fix) is in place.
- `torch.mps.empty_cache()` is called after both `_run_diarization_impl` and `_estimate_num_speakers`.
- `gc.collect()` in hot path (Finding 4) is unnecessary but not a leak.
- Voxtral numpy branch temp file (Finding 2) is the one confirmed leak path.
- Chunked transcription correctly pops `"audio"` from chunk dicts after stitching (line 1578-1579), preventing numpy array retention.
- Lazy model loading (`_sensevoice_model`, `_parakeet_model`, `_whisperx_model`, `_voxtral_model`, `_diarization_pipeline`) — models are never released once loaded. This is by design (one load per session) and matches the pattern in the rest of the codebase.

---

## Findings Table

| # | Severity | Finding | File:Line |
|---|----------|---------|-----------|
| 1 | HIGH | Hardcoded `"Bearer token_here"` in `_transcribe_remote` | `engine.py:3063` |
| 2 | MEDIUM | Temp-file leak in `_transcribe_voxtral` numpy branch | `engine.py:2608-2619` |
| 3 | MEDIUM | `VoiceCommandProcessor` re-instantiated on every call | `engine.py:932` |
| 4 | MEDIUM | `gc.collect()` in hot STT path | `engine.py:915` |
| 5 | LOW | Hallucination patterns: no EN/ES patterns, missing bracket patterns | `utils.py:383-401` |
| 6 | LOW | Fallback chain comment omits 4 Phase-4 adapters | `engine.py:1644` |
| 7 | LOW | Diarization error log hardcoded to `/tmp` path | `engine.py:2909` |
| 8 | LOW | `overlap_words` heuristic underestimates RU word rate | `engine.py:1471` |
| — | INFO | MLX lock compliance: PASS | — |
| — | INFO | Fallback chain ordering: CORRECT | — |
| — | INFO | Memory/temp cleanup: mostly clean (1 gap) | — |
