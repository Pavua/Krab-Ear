# WhisperX STT Adapter — Re-audit (W1214)

**Date:** 2026-05-26
**Branch:** audit/whisperx-W1214
**Source files audited:**
- `KrabEar/core/engine.py` (lines 74-80, 352-356, 1613-1776, 2161-2357)
- `KrabEar/core/config.py` (lines 271-293)
- `KrabEar/tests/test_whisperx_adapter.py`

---

## Summary

WhisperX adapter is implemented in Phase 4.3 as an opt-in member of the `AudioEngine` STT fallback chain (disabled by default: `WHISPERX_ENABLED = False`). The adapter uses `m-bain/whisperX` (torch/MPS, not MLX) and adds word-level timestamps via forced phoneme alignment plus optional pyannote diarization. Five findings follow.

---

## Finding 1 — CRITICAL: `aligned` NameError when word alignment fails before diarization

**File:** `KrabEar/core/engine.py`, lines 2255–2293

The variable `aligned` is assigned only inside the `try` block (lines 2262-2268). If the `try` block raises (e.g. unsupported language code for `load_align_model`, which is common for languages other than EN/RU), the `except` on line 2280 swallows the error and `word_timestamps` remains `None`. Control then falls to the diarization block (line 2285). At line 2293:

```python
if word_timestamps is not None and aligned is not None:
```

`aligned` is an unbound name at this point — the `and` short-circuit only saves the condition if `word_timestamps is None`, which is correct in the failure path. However, if `WHISPERX_WORD_TIMESTAMPS=True` and the alignment succeeds partially (word_timestamps is not None) **but** `aligned` was reset to `None` by some future change or if there is an implicit assumption that `aligned` exists for diarization, the `NameError` would propagate. More critically: in a scenario where `aligned` was assigned and then `word_timestamps` is not None (the success path), this works — but the logic implicitly relies on `aligned` always being defined when `word_timestamps is not None`, which is not guaranteed by construction. A defensive fix is to initialize `aligned = None` before the try block.

**Risk:** If a future edit re-orders the logic or the try block partially succeeds, a `NameError: name 'aligned' is not defined` escapes the try/except, propagates uncaught past `_transcribe_whisperx`, and marks the `_WHISPERX_MARKER` as permanently unavailable (line 1775), silently breaking word timestamps for the session.

**Fix:** Add `aligned = None` immediately before line 2256 (before the `if settings.WHISPERX_WORD_TIMESTAMPS` block).

---

## Finding 2 — MEDIUM: Alignment model reloaded on every call — no caching

**File:** `KrabEar/core/engine.py`, lines 2258-2261

```python
align_model, metadata = _whisperx.load_align_model(
    language_code=detected_lang or "en",
    device=settings.WHISPERX_DEVICE,
)
```

`load_align_model` downloads and instantiates the wav2vec2 alignment model (~200 MB) on each invocation of `_transcribe_whisperx`. The main `_whisperx_model` is cached on `self._whisperx_model` (lazy-loaded once), but the alignment model is not. On a session with many short recordings, this adds 0.5–2 s per transcription and RAM pressure (the model is garbage collected between calls).

The `DiarizationPipeline` on line 2287 has the same issue — instantiated fresh each call.

**Fix:** Add `self._whisperx_align_cache: dict[str, tuple] = {}` in `__init__` and cache by `language_code`. Similarly cache the `DiarizationPipeline` instance.

---

## Finding 3 — MEDIUM: Adapter dispatch branch bypasses `TRANSCRIBE_TIMEOUT_SEC` guard

**File:** `KrabEar/core/engine.py`, lines 1763-1776

All non-adapter (MLX whisper) model paths wrap the call in a `ThreadPoolExecutor` with `future.result(timeout=settings.TRANSCRIBE_TIMEOUT_SEC)` (line 1797). Adapter branches (including WhisperX) bypass this entirely:

```python
if model_name in _adapter_map:
    ...
    with _profiler.start_span(span_name):
        adapter_result = adapter_fn()   # direct call, no timeout
```

WhisperX on 36 GB RAM with `batch_size=16` and a 60-minute audio file can take several minutes. If the call hangs (e.g. MPS deadlock on first use, Metal resource contention), there is no watchdog — the IPC call blocks indefinitely until the OS kills the process or user force-quits.

**Fix:** Wrap adapter calls in the same `ThreadPoolExecutor` + `future.result(timeout=...)` pattern used for whisper model calls.

---

## Finding 4 — MEDIUM: pyannote VAD gated dependency — same W1216 family

**File:** `KrabEar/core/config.py` lines 288-290; `KrabEar/core/engine.py` lines 2285-2337

`WHISPERX_DIARIZATION = True` by default. The `DiarizationPipeline` requires `pyannote/speaker-diarization-3.1`, which is a HuggingFace gated model requiring manual TOS acceptance. Without `HF_TOKEN` the code logs a warning and skips diarization (line 2333-2337) — correct behaviour.

However, the default value of `WHISPERX_DIARIZATION` is `True` (config.py line 290), so any user who enables `WHISPERX_ENABLED=True` and sets `HF_TOKEN` will immediately attempt to download the gated model on first transcription. If they have not accepted the TOS on `huggingface.co/pyannote/speaker-diarization-3.1`, whisperx raises `requests.exceptions.HTTPError: 401` or a gated-repo error. This is caught by the broad `except Exception` at line 2326 and logged as a warning — but since the `DiarizationPipeline` is re-instantiated on every call (Finding 2), the gated-model download is re-attempted on every transcription, adding 100-500 ms overhead for the HTTP 401 roundtrip.

**Fix:** On diarization 401/gated error, cache the failure with a `_whisperx_diarize_error` flag (same pattern as `_whisperx_load_error`) and set `WHISPERX_DIARIZATION = False` at runtime to avoid repeated failed attempts. Additionally, consider defaulting `WHISPERX_DIARIZATION = False` to avoid surprising new users.

---

## Finding 5 — LOW: `batch_size=16` hardcoded — unsafe on machines with <8 GB free RAM

**File:** `KrabEar/core/engine.py`, line 2250

```python
result = model.transcribe(audio_array, batch_size=16, language=lang_param)
```

`batch_size=16` is annotated as "безопасный дефолт для 36 GB RAM" (safe default for 36 GB). On the target machine (M4 Max 36 GB) this is fine. However, the config allows `WHISPERX_ENABLED=True` on any machine (no RAM precondition check at load time). On a system with 8–16 GB total RAM (e.g. base M2 MacBook Air), `batch_size=16` combined with `whisper-large-v3` (~3 GB) + alignment model (~200 MB) can trigger macOS memory pressure and cause the process to be killed, or Metal GPU OOM. WhisperX itself recommends `batch_size=4` for machines with <16 GB.

The `_HEAVY_MODEL_MIN_FREE_GB = 4.0` guard (line 1784) applies to regular whisper model selection, but not to adapter branches (Finding 3) — so even if free RAM is 3 GB, WhisperX will still be invoked.

**Fix:** Add a `WHISPERX_BATCH_SIZE` config setting (default `16`, recommended `4` for <16 GB), or derive it from `_get_available_memory_gb()` at call time. Also apply the `_HEAVY_MODEL_MIN_FREE_GB` check before invoking adapter branches.

---

## Test Coverage Summary

`KrabEar/tests/test_whisperx_adapter.py` covers:
- Disabled state (marker not inserted when `WHISPERX_ENABLED=False`)
- Fallback chain ordering (WhisperX after SenseVoice)
- No-retry after marker marked unavailable
- `_load_whisperx_model` error caching
- `HistoryItem` schema (`word_timestamps`, `speaker_turns` fields)

Not covered:
- Finding 1 (`aligned` unbound NameError scenario)
- Finding 2 (alignment model reload on each call)
- Finding 3 (no timeout on adapter calls)
- Finding 4 (diarization gated-model retry loop)
- Finding 5 (`batch_size` memory pressure)

---

## Findings Table

| # | Severity | Issue | Location |
|---|----------|-------|----------|
| 1 | CRITICAL | `aligned` potentially unbound NameError before diarization step | `engine.py:2293` |
| 2 | MEDIUM | Alignment model + DiarizationPipeline reloaded on every call | `engine.py:2258,2287` |
| 3 | MEDIUM | Adapter dispatch has no `TRANSCRIBE_TIMEOUT_SEC` guard | `engine.py:1765-1776` |
| 4 | MEDIUM | Gated pyannote model re-attempted on every call after 401 | `engine.py:2285-2337` |
| 5 | LOW | `batch_size=16` hardcoded — unsafe on <16 GB machines | `engine.py:2250` |
