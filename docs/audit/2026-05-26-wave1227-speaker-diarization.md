# Speaker Diarization Audit — W1227

**Date:** 2026-05-26  
**Branch:** audit/speaker-diarization-W1227  
**Scope:** `KrabEar/core/engine.py` (`_load_diarization_pipeline`, `_run_diarization_impl`, `_estimate_num_speakers`, `_maybe_run_diarization`), `KrabEar/backend/speaker_manager.py`

---

## Summary

5 findings: 1 HIGH, 2 MEDIUM, 2 LOW.  
No existing test covers the two most critical paths (race condition on lazy pipeline load; embedding size DoS in `handle_register_speaker`).

---

## Findings

### F1 — HIGH: Race condition in `_load_diarization_pipeline` (no lock)

**File:** `KrabEar/core/engine.py:2854–2877`

`_load_diarization_pipeline` uses a double-checked pattern (`if self._diarization_pipeline is not None: return`) with no lock. If two threads call `transcribe()` concurrently while the pipeline is still `None` (common in REST + IPC hybrid paths), both threads pass the guard simultaneously. Both call `Pipeline.from_pretrained()` in parallel, both call `.to(device)` on two separate pipeline objects, and the second write to `self._diarization_pipeline` silently leaks the first. On MPS this can cause a Metal GPU assertion failure because two PyTorch sessions share the same device concurrently.

**Evidence:** The `AudioEngine` instance is shared (created once in `BackendService.__init__`); `IPCServer` handles requests from multiple threads; the REST server at port 5005 can also call `transcribe()` concurrently.

**Fix:** Wrap the load section in `threading.Lock()` stored as `self._diarization_pipeline_lock`:

```python
# in __init__
self._diarization_pipeline_lock = threading.Lock()

# in _load_diarization_pipeline
with self._diarization_pipeline_lock:
    if self._diarization_pipeline is not None:
        return self._diarization_pipeline
    # ... existing load logic ...
```

---

### F2 — MEDIUM: `handle_register_speaker` accepts unbounded embedding list (DoS)

**File:** `KrabEar/backend/speaker_manager.py:301–308`

The IPC handler checks only `len(emb_raw) == 0` but imposes no upper bound. A malicious or buggy client can submit a list of millions of floats; `np.array(emb_raw, dtype=np.float32)` + `.tolist()` for JSON persistence will allocate gigabytes and stall the IPC server.

```python
# current — no upper bound
if not isinstance(emb_raw, list) or len(emb_raw) == 0:
    raise ValueError("Параметр embedding обязателен (list[float])")
```

The expected dimension is 512 (`_EMBEDDING_DIM = 29`). Any embedding larger than, say, 2048 elements is invalid.

**Fix:** Add `if len(emb_raw) > 2048: raise ValueError(...)` before the `np.array` call. Also log a warning if `len(emb_raw) != _EMBEDDING_DIM` because a mis-sized embedding will produce silently wrong cosine-similarity scores.

---

### F3 — MEDIUM: Voice fingerprint IPC handlers active despite `VOICE_FINGERPRINT_ENABLED=False`

**Files:** `KrabEar/backend/speaker_manager.py:301–327`, `KrabEar/backend/service.py:1155–1157`

`VOICE_FINGERPRINT_ENABLED` is declared in `config.py` (default `False`) and documented as an opt-in gate. However, `handle_register_speaker`, `handle_delete_speaker_fingerprint`, and `handle_list_speaker_fingerprints` exist in `SpeakerManager` but are **never wired** into the `service.py` handler dispatch table. This means:

- The fingerprint IPC handlers are dead code today (not reachable from IPC).
- Conversely, `resolve_speaker_for_segment()` calls `register_speaker()` with `auto_register=True` regardless of the flag — if a caller ever invokes this with fingerprint data and the flag is off, biometric data is silently persisted to `speaker_fingerprints.json`.

The session note for W951 mentions a "voice embeddings privacy gate" that was intended to block fingerprint operations when `VOICE_FINGERPRINT_ENABLED=False`. That gate is missing at the `resolve_speaker_for_segment` and `compute_embedding` call sites.

**Fix:** In `resolve_speaker_for_segment`:
```python
from core.config import settings
if not settings.VOICE_FINGERPRINT_ENABLED:
    return local_speaker_id
```
And wire (or explicitly remove) the three fingerprint IPC handlers in `service.py` dispatch — if the feature is not ready, the handlers should not exist in the live dispatch table.

---

### F4 — LOW: `_run_diarization_impl` writes traceback to world-readable `/tmp/krab_ear_diarization_error.log`

**File:** `KrabEar/core/engine.py:2906–2917`

On any pipeline exception the code opens `/tmp/krab_ear_diarization_error.log` for writing. The error message already contains `type(e).__name__` and `str(e)`, which may leak internal paths or model names. The file is at a predictable world-writable location — a local attacker can pre-create a symlink to redirect writes.

Additionally, this "Krab's Black Box" block duplicates what `_maybe_run_diarization` already does (catch exception → log warning → push error bus). The black box write provides no value not already covered by structured logging.

**Fix:** Remove the manual `/tmp` write entirely. The structured logger + error bus handle the same purpose without the security or duplication issue. If a persistent trace is needed, use `logger.exception("diarization pipeline crash")` which includes the full traceback in the log stream.

---

### F5 — LOW: No minimum-duration guard before calling pyannote on very short clips

**File:** `KrabEar/core/engine.py:2793–2841` (`_maybe_run_diarization`)

pyannote/speaker-diarization-3.1 uses a 10-second sliding window for speaker embedding. Clips shorter than ~3 seconds produce zero speaker segments (empty annotation), causing `_run_diarization` to return an empty list and every Whisper segment to be assigned `SPEAKER_UNKNOWN`. This is silently accepted without warning.

More importantly, `_estimate_num_speakers` calls the full pipeline on very short numpy arrays (e.g., 0.5 s preview clips that somehow bypass the `is_preview` guard via direct calls). The pipeline runs and allocates ~1.5 GB of pyannote model memory for a clip that cannot produce meaningful output.

No existing test covers the short-clip path in `_maybe_run_diarization`.

**Fix:** Add an early guard:
```python
# in _maybe_run_diarization, after resolving audio_path
if audio_path:
    try:
        import soundfile as _sf
        info = _sf.info(audio_path)
        if info.duration < 3.0:
            logger.debug("Diarization пропущена: clip %.1fs < 3s минимум", info.duration)
            return base_result
    except Exception:
        pass
```

---

## Test Coverage Gap

No test exists for:
- Concurrent calls to `_load_diarization_pipeline` (F1)
- Oversized embedding rejection in `handle_register_speaker` (F2)
- `VOICE_FINGERPRINT_ENABLED=False` blocking `resolve_speaker_for_segment` (F3)
- Short-clip early-return in `_maybe_run_diarization` (F5)

Tests that DO exist and pass:
- `test_engine_diarization.py` — segment overlap assignment and turn merging
- `test_engine_extended.py::DiarizationDeviceTests` — MPS/CUDA/CPU device selection
- `test_engine_error_bus.py` — `diarization.pipeline_fail` push on exception
- `test_engine_speaker_prompt.py` — `_estimate_num_speakers` caching and prompt building

---

## HF Gated Model Note (sister to W1216)

`pyannote/speaker-diarization-3.1` is a gated HuggingFace model (requires TOS accept at huggingface.co/pyannote/speaker-diarization-3.1 and a valid `HF_TOKEN`). If token is absent, `_load_diarization_pipeline` logs the load error into `_diarization_load_error` and every subsequent call raises immediately — this is the correct fail-fast behaviour. No fix needed here; existing behaviour matches W1216 pattern.
