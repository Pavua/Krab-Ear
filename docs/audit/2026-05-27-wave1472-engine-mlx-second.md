# W1472 Second-Pass Audit: engine.py MLX/STT Pipeline

**Date:** 2026-05-27
**Branch audited:** `codex/krab-ear-v2` (HEAD `f7086279`)
**Scope:** `KrabEar/core/engine.py` — post-W1303/W1304/W1305/W1306/W1307/W1391 state verification plus new residual findings.
**Prior audit docs:** `docs/audit/2026-05-26-wave1303-engine-fallback-chain.md`, `docs/audit/2026-05-27-wave1358-engine-mlx-cross-cutting.md`

---

## Prior Wave Merge State

| Wave | Commit | Description | Merged to `codex/krab-ear-v2`? |
|------|--------|-------------|-------------------------------|
| W1303 | `005d10da` | Audit doc (6 findings) | YES (docs only) |
| W1304 | — | _unavailable_models TTL | **NOT MERGED** — no commit exists |
| W1305 | `70805dab` | WhisperX position after Parakeet + SenseVoice | **YES** — merged |
| W1306 | `93c07d2f` | Parakeet language gate EN/auto only | **YES** — merged |
| W1307 | `90e91e37` | ThreadPoolExecutor non-blocking shutdown | **PARTIALLY merged** — see F2 |
| W1391 | `dde08d33` | Preprocess order Denoiser→RSF→GainNorm→SSS | **YES** — merged |

Additional prior waves verified merged:
- W1223 (`226c312b`) — Voxtral `mlx_lock()` wrapper: **YES**
- W1225 (`fe9024be`) — Parakeet inter-process lock comment: **YES**
- W1235 (`61712a8a`) — pyannote double-checked lazy lock: **YES**
- W1117 (`055f84bd`) — `audio_lang_id` `mx.clear_cache()`: **YES**
- W1366 (`0914b235`) — MLX watchdog holds lock until daemon completes: **YES**

---

## Findings (5)

### F1 — HIGH: `_load_voxtral_model()` has no threading lock — concurrent double-load risk

**File:** `KrabEar/core/engine.py`, lines 2560–2595
**Status:** New finding — not covered by any prior wave

All other lazy-loaded adapters (SenseVoice at line 2138, Parakeet at line 2261, WhisperX at line 2369, pyannote at line 3071) follow the double-checked-locking pattern with per-instance `threading.RLock` instances declared in `__init__` (lines 361, 368, 375, 354). Voxtral's `_load_voxtral_model()` does not:

```python
def _load_voxtral_model(self) -> Any:
    if getattr(self, "_voxtral_model", None) is not None:   # check
        return self._voxtral_model
    if getattr(self, "_voxtral_load_error", None):
        raise RuntimeError(self._voxtral_load_error)
    # ... no lock before act ...
    self._voxtral_model = (model, tokenizer)   # act
```

Two IPC threads calling `_transcribe_voxtral()` concurrently (e.g., multipass retry + a live_subs ingest) both pass the fast-path check and both invoke `snapshot_download()` + `_VoxtralTransformer.from_folder()`. The Voxtral model is ~2–3 GB; a double-load under memory pressure can cause `MemoryError` or Metal GPU assertion failure during the MLX `from_folder` init.

Additionally, `_voxtral_model` and `_voxtral_load_error` are never set in `__init__` — unlike `_sensevoice_model = None` (line 359), `_parakeet_model = None` (line 366), etc. This means `getattr(self, "_voxtral_model", None)` is the only guard, which is safe individually but means that if a future refactor removes the `getattr` form, `AttributeError` would surface.

**Fix:**
1. In `__init__`, add:
   ```python
   self._voxtral_model = None
   self._voxtral_load_error: str | None = None
   self._voxtral_load_lock: threading.RLock = threading.RLock()
   ```
2. In `_load_voxtral_model()`, add the double-checked lock around model loading:
   ```python
   with self._voxtral_load_lock:
       if self._voxtral_model is not None:
           return self._voxtral_model
       if self._voxtral_load_error:
           raise RuntimeError(self._voxtral_load_error)
       # ... snapshot_download + from_folder ...
   ```

---

### F2 — HIGH: W1307 fix is incomplete — adapter branch still uses blocking `with ThreadPoolExecutor(...) as _pool:` form

**File:** `KrabEar/core/engine.py`, lines 1911–1919
**Status:** W1307 partially applied — Whisper branch (lines 1944–1955) and multipass retry branch (lines 1390–1403) were fixed, but the adapter dispatch branch was not

The W1307 fix converted `_transcribe_with_fallback_impl` (Whisper branch) and `_maybe_multipass_retry` to use explicit `ThreadPoolExecutor` construction + `shutdown(wait=False, cancel_futures=True)`. However the adapter dispatch branch (SenseVoice / Parakeet / WhisperX / Voxtral / GigaAM / RU-finetune) at lines 1911–1919 still uses the context-manager form:

```python
# STILL BLOCKING — W1307 not applied here
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
    _fut = _pool.submit(adapter_fn)
    try:
        adapter_result = _fut.result(timeout=_adapter_timeout)
    except concurrent.futures.TimeoutError:
        _fut.cancel()
        raise TimeoutError(f"{span_pfx} adapter таймаут {_adapter_timeout}s — GPU stall?")
# ^ ThreadPoolExecutor.__exit__ calls shutdown(wait=True) here
# If the GPU-stuck thread is still running, this blocks for the full thread lifetime
```

When `_fut.result(timeout=...)` raises `TimeoutError`, `_fut.cancel()` is called, then the `with` block exits — calling `shutdown(wait=True)`, which blocks until the hung adapter thread completes. For a GPU-stuck Voxtral or SenseVoice thread, this is the full model-inference time (potentially minutes).

The `TimeoutError` is then re-raised, which is caught by the outer `except Exception` at line 1922, allowing the chain to continue — but only after the blocking wait completes. The practical consequence is that adapter GPU stalls serialize the entire fallback chain.

**Also:** The W1307 test file `test_engine_executor_shutdown_W1307.py` tests `engine._transcribe_with_fallback_chain` (line 147, 255) which **does not exist** — the actual method is `_transcribe_with_fallback_impl`. Python's `getattr` raises `AttributeError`, which is silently swallowed inside the `try/except Exception: pass` block at lines 146–149 and 253–257. The fallback-chain shutdown tests pass vacuously and provide zero coverage.

**Fix:**
Replace the `with _pool:` form in the adapter branch with explicit construction + non-blocking shutdown:
```python
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
try:
    _fut = _pool.submit(adapter_fn)
    try:
        adapter_result = _fut.result(timeout=_adapter_timeout)
    except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
        _pool.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"{span_pfx} adapter таймаут {_adapter_timeout}s — GPU stall?")
    except Exception:
        _pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        _pool.shutdown(wait=False)
```
Also fix `test_engine_executor_shutdown_W1307.py` to call `_transcribe_with_fallback_impl` (not the non-existent `_transcribe_with_fallback_chain`).

---

### F3 — MEDIUM: `set_quality_profile()` calls `mx.clear_cache()` outside `mlx_lock()`

**File:** `KrabEar/core/engine.py`, lines 567–571
**Status:** Open — originally identified in W1358 F3 but not yet fixed

```python
def set_quality_profile(self, profile: str) -> bool:
    ...
    self.current_model = new_model
    try:
        import mlx.core as _mx
        _mx.clear_cache()          # <-- NOT under mlx_lock()
    except (ImportError, AttributeError):
        pass
    return True
```

`mx.clear_cache()` deallocates Metal GPU heap buffers. If called while another thread holds `mlx_lock()` and is mid-inference in `mlx_whisper.transcribe()`, the cache flush invalidates Metal heap allocations that the GPU kernel is actively using. On Apple Silicon this can manifest as `IOGPUMetal` assertion failures or silent memory corruption.

The in-pipeline `clear_cache()` at line 1032 (inside `transcribe()`, after STT completes) runs outside the `mlx_lock()` context — that is a separate ordering issue documented in W1358 F3 but accepted as low-risk for single-user desktop. However `set_quality_profile()` can be called from the IPC thread at any time, including concurrently with an ongoing transcription.

**Fix:**
```python
try:
    import mlx.core as _mx
    with mlx_lock():
        _mx.clear_cache()
except (ImportError, AttributeError):
    pass
```

---

### F4 — MEDIUM: `_unavailable_models` TTL never implemented — transient failures permanently blacklist models for entire session

**File:** `KrabEar/core/engine.py`, line 348
**Status:** W1304 (supposed to add TTL) was never written — confirmed by `grep -rn "TTL\|_unavailable_models_time\|_model_failure_time\|unavailable_until"` returning zero matches

```python
self._unavailable_models: set[str] = set()
```

Once any model or adapter marker is added to `_unavailable_models` (transient `TimeoutError`, `MLXTimeoutError`, or first-call `ImportError`), it remains permanently blacklisted for the entire engine lifetime (process restart required). This is by design for hard errors (OOM, ImportError), but is incorrect for transient errors:

- A network timeout on first model download → model blacklisted forever in that session
- A 1s MLX watchdog timeout on a slow GPU → model blacklisted, user must restart backend to recover
- A GigaAM subprocess cold-start timeout (~30s) on first use → GigaAM permanently disabled for session

No eviction path exists: `_unavailable_models` is never cleared, has no TTL, and has no IPC method to reset it (though `reload_engine` in `BackendService` creates a new `AudioEngine` instance, which achieves the same effect indirectly).

**Recommendation:** Replace `set[str]` with `dict[str, float]` mapping marker → fail_timestamp. For recoverable errors (timeouts, non-OOM exceptions), set TTL to `STT_UNAVAILABLE_TTL_SEC` (suggested: 900 s). For permanent errors (OOM, ImportError), use `math.inf`. Add a helper `_is_unavailable(marker)` that evicts stale entries before checking.

---

### F5 — LOW: `post_inference clear_cache()` at line 1032 runs outside `mlx_lock()` critical section

**File:** `KrabEar/core/engine.py`, lines 1030–1034
**Status:** New finding — distinct from W1358 F3 (`set_quality_profile`)

```python
result = self._transcribe_with_fallback(audio_data, ...)
...
# After STT + diarization — освобождаем MLX Metal cache (W63).
try:
    import mlx.core as _mx
    _mx.clear_cache()          # outside mlx_lock()
except (ImportError, AttributeError):
    pass
```

This `clear_cache()` runs in `transcribe()` after `_transcribe_with_fallback()` returns — i.e., after `mlx_lock()` in `_transcribe_model()` has been released. A second transcription call on another IPC thread can acquire `mlx_lock()` and start inference **before** this `clear_cache()` runs, resulting in:

1. Thread A: releases `mlx_lock()` after transcription
2. Thread B: acquires `mlx_lock()`, starts `mlx_whisper.transcribe()`
3. Thread A: calls `_mx.clear_cache()` — flushes Metal heap buffers that Thread B is actively using

On the current IPC architecture (single `BackendService` handling requests sequentially from one socket connection), two concurrent transcriptions are unlikely in normal use. However, `rest_server.py` runs independently and can trigger concurrent transcriptions via `POST /transcribe`. The risk is LOW for single-user desktop use but becomes HIGH if concurrent REST + IPC transcriptions overlap.

**Fix (minimal):** Move `clear_cache()` to inside the `with mlx_lock():` block in `_transcribe_model()`, immediately after `mlx_whisper.transcribe()` returns and before the lock is released:
```python
with mlx_lock():
    result = mlx_whisper.transcribe(audio_data, **params)
    try:
        import mlx.core as _mx
        _mx.clear_cache()
    except (ImportError, AttributeError):
        pass
    return result
```
This ensures the Metal heap is freed atomically with the transcription, before any other thread can start a new inference.

---

## Non-issues (confirmed correct post-wave fixes)

- **W1305 (F1 HIGH):** WhisperX insertion now scans `{PARAKEET_MARKER, SENSEVOICE_MARKER}` set correctly (line 1834). Order: `[balanced, Parakeet, SenseVoice, WhisperX, Voxtral, max-candidates]` when all enabled.
- **W1306 (F3 MED):** Parakeet chain-build gated to `_effective_lang in {"en", "auto"}` (line 1800). RU/ES audio no longer routes through Parakeet.
- **W1307 (F4 MED) — Whisper branch:** Lines 1944–1955 correctly use `_executor.shutdown(wait=False, cancel_futures=True)` on timeout. Only the adapter branch (F2 above) was missed.
- **W1391 (F4 MED):** Preprocess order is now `2.4=Denoiser → 2.5=RSF → 2.6=GainNorm → 2.7=SSS` (lines 888–955). Denoiser sees original audio before RSF zeros applied.
- **W1223 / Voxtral `mlx_lock`:** `_voxtral_generate()` at line 2846 is correctly wrapped in `with mlx_lock():`. Only the loader lock (F1) is missing.
- **W1235 / pyannote double-checked lock:** `_load_diarization_pipeline()` (lines 3066–3094) uses correct double-checked `with self._diarization_load_lock:` pattern.
- **W1117 / `audio_lang_id` `mx.clear_cache()`:** `AudioLanguageID._run_detect()` calls `mx.clear_cache()` after LID inference (commit `055f84bd` merged).
- **`mlx_lock()` in `_transcribe_model()`:** Lines 2050–2091 correctly serialize all `mlx_whisper.transcribe()` variants through `with mlx_lock():`.
- **GigaAM, SenseVoice, Parakeet, WhisperX:** All use PyTorch MPS — correctly do NOT use `mlx_lock()`.

---

## Summary

| # | Severity | Finding | Fix exists? |
|---|----------|---------|-------------|
| F1 | HIGH | `_load_voxtral_model()` has no threading lock — concurrent double-load risk | No fix yet |
| F2 | HIGH | W1307 adapter branch still uses blocking `with ThreadPoolExecutor as pool:` + test calls non-existent method | No fix yet |
| F3 | MEDIUM | `set_quality_profile()` calls `mx.clear_cache()` outside `mlx_lock()` | No (W1358 F3 open) |
| F4 | MEDIUM | `_unavailable_models` has no TTL — transient failures permanently blacklist models | No (W1304 not written) |
| F5 | LOW | Post-inference `clear_cache()` at line 1032 runs outside `mlx_lock()` — concurrent REST+IPC transcription risk | No fix yet |

**Prior wave merge state summary:** W1303 (docs only) + W1305 + W1306 + W1391 fully merged. W1307 partially merged (Whisper branch fixed, adapter branch missed). W1304 was never written. W1358 F3 (`set_quality_profile` lock) remains open.
