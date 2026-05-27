# Wave 1217 — Parakeet STT Adapter Audit

**Date:** 2026-05-26
**Auditor:** W1217 (sub-agent)
**Scope:** `KrabEar/core/pipeline/stt_parakeet.py` (parakeet-mlx adapter) + `KrabEar/core/engine.py` NeMo branch (`_load_parakeet_model`, `_transcribe_parakeet`) + `STTRouter` / `STTRouterFactory` integration + test coverage
**Findings:** 5
**Status:** No blockers. All findings are LOW/MED; none block v2.0.5 production.

---

## Context

There are **two distinct Parakeet integration paths** in the codebase:

| Path | File | Backend | Device |
|------|------|---------|--------|
| A — NeMo branch | `KrabEar/core/engine.py` `_transcribe_parakeet()` | NVIDIA NeMo / PyTorch | MPS auto-selected by NeMo |
| B — parakeet-mlx branch | `KrabEar/core/pipeline/stt_parakeet.py` | `parakeet-mlx` (MLX) | Apple Silicon MLX GPU |

Path A is the legacy engine fallback chain path (`_PARAKEET_MARKER`). Path B is the newer pipeline-router adapter (`ParakeetSTTAdapter`). Both are controlled independently via `PARAKEET_ENABLED` (Path A, `engine.py`) and `STT_PARAKEET_ENABLED` (Path B, `config.py:362`, router factory). Both default to `False`.

---

## Findings

### F-1 — OOM/MemoryError not caught in adapter branch of fallback chain (LOW)

**File:** `KrabEar/core/engine.py`, lines 1763–1776

The main STT fallback loop has explicit `MemoryError` and `OSError(errno=12)` handlers with `_push_error` calls, but only for the **Whisper MLX model branch** (lines 1814–1846). The **adapter branch** (which dispatches Parakeet NeMo, GigaAM, SenseVoice, etc.) catches only `Exception` generically at line 1773:

```python
except Exception as exc:
    logger.warning("%s adapter не сработал: %s — продолжаю chain", span_pfx, exc)
    self._unavailable_models.add(model_name)
    continue
```

If NeMo loads a 2.3 GB model on a memory-constrained machine (e.g., LM Studio + Whisper already loaded), a `MemoryError` or `OSError(errno=12)` will be swallowed silently — no `stt.load_fail` or `stt.oom_model_evicted` error bus events are pushed. Fallback to the next adapter still occurs correctly, but the OOM is invisible in Sentry and the UI error toast.

**Severity:** LOW — fallback chain continues correctly; only the error bus notification is missing.

**Fix:** Add a `MemoryError` guard inside the adapter branch similar to lines 1814–1826.

---

### F-2 — `mlx_inter_process_lock` not used in `ParakeetSTTAdapter.transcribe()` (LOW)

**File:** `KrabEar/core/pipeline/stt_parakeet.py`, lines 149–154

`ParakeetSTTAdapter.transcribe()` correctly acquires `mlx_lock()` (intra-process RLock) before calling `self._model.transcribe(audio)`. However, when the REST server (`backend/rest_server.py`) runs as a separate process alongside the IPC backend, both processes may issue concurrent MLX calls. The intra-process `mlx_lock` does not help in that case.

`core/mlx_inter_lock.py` provides `mlx_inter_process_lock()` (POSIX flock) for exactly this scenario, and `core/mlx_lock.py` documents the usage pattern:

```python
with mlx_inter_process_lock():  # outer: cross-process flock
    with mlx_lock():            # inner: intra-process RLock
        mlx_whisper.transcribe(...)
```

`ParakeetSTTAdapter` (Path B) only acquires the inner lock. `_transcribe_parakeet()` in `engine.py` (Path A) also only acquires the inner lock (line 1892 is for Whisper; NeMo transcription at line 1770 has no explicit lock at all — see F-3).

**Severity:** LOW — inter-process lock is opt-in (`KRAB_EAR_MLX_INTER_PROCESS_LOCK=1` env var), and parakeet-mlx is disabled by default. Affects only setups with `KRAB_EAR_MLX_INTER_PROCESS_LOCK=1` + `STT_PARAKEET_ENABLED=True` + concurrent REST + IPC processes.

**Fix:** Wrap `ParakeetSTTAdapter.transcribe()` inference with the documented two-level pattern (same as Whisper MLX).

---

### F-3 — NeMo `_transcribe_parakeet()` has no `mlx_lock` protection (MED)

**File:** `KrabEar/core/engine.py`, lines 2107–2159

The NeMo Parakeet path (Path A) calls `model.transcribe(audio_paths)` without any `mlx_lock()` guard. NeMo uses **PyTorch + MPS** (not MLX directly), so the MLX `_mlx_lock` is not strictly required — and the code comments on SenseVoice correctly note "PyTorch MPS — `mlx_lock` не нужен". However, NeMo on Apple Silicon with `mps` device does route computation through Metal GPU, which is the same hardware as MLX. The comment at line 2076 says "MPS работает нативно на M-серии" and "NeMo сам управляет инференсом" without addressing potential Metal resource contention with concurrent MLX (Whisper) calls.

In practice, NeMo manages its own MPS device context via PyTorch, which is separate from MLX's Metal commandqueue. Concurrent PyTorch+MPS and MLX calls are **not known to cause SIGSEGV** (that was an MLX–MLX race). But this is an undocumented assumption that is not validated by any test — there is no test covering `_transcribe_parakeet()` running concurrently with an MLX Whisper call.

**Severity:** MED — potential Metal resource contention on Apple Silicon when both NeMo (MPS) and mlx-whisper (MLX) run simultaneously. No confirmed crash, but no explicit validation either. The SenseVoice adapter has the same property and was explicitly documented ("uses PyTorch MPS — no mlx_lock required") whereas Parakeet NeMo is silent on this.

**Fix:** Add a comment in `_transcribe_parakeet()` explicitly stating why `mlx_lock` is not needed (PyTorch manages its own MPS command queue, separate from MLX), matching the style of SenseVoice. If there is a known risk, add a guarded `with mlx_lock():` wrapper regardless.

---

### F-4 — `model_path` accepts arbitrary HuggingFace repo IDs without sanitization (LOW)

**File:** `KrabEar/core/pipeline/stt_parakeet.py`, lines 61–62 and 128

The `model_path` constructor argument is forwarded directly to `parakeet_mlx.from_pretrained(self._model_path)` without validation. Any caller that controls the `stt_parakeet_model` setting can point `from_pretrained` at an arbitrary HuggingFace repo:

```python
def __init__(self, model_path: str | None = None) -> None:
    self._model_path = model_path or _DEFAULT_MODEL
```

The same issue exists in `stt_router_factory.py:67` which reads the setting from `cfg.get("stt_parakeet_model", None)`. NeMo's equivalent in `engine.py:2097` reads from `settings.PARAKEET_MODEL`.

While `parakeet-mlx.from_pretrained` is a HuggingFace hub download — not a local path traversal — passing an attacker-controlled model ID can result in loading a malicious model or excessive bandwidth use. This is the same issue class as noted in audio_lang_id audit (W1109).

**Severity:** LOW — model loading is guarded by `PARAKEET_ENABLED=False` default, and settings are authenticated over the IPC socket. A path-traversal or remote-code-execution through `from_pretrained` is HuggingFace's concern, not this adapter's.

**Fix:** Document the trust boundary. Optionally add a regex allowlist for known repo ID formats (e.g. `r'^[a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+$'`) and log a warning for non-conforming IDs.

---

### F-5 — `privacy_mode_enabled` has no effect on Parakeet transcription path (LOW)

**File:** `KrabEar/core/pipeline/stt_parakeet.py`, `KrabEar/core/engine.py`

`privacy_mode_enabled` is respected in `TranslationService` (guards network translation calls) and `observability.py` (blocks Sentry init). However, there is no privacy mode guard anywhere in the Parakeet adapter (either Path A or Path B). When privacy mode is active, Parakeet still:
- Downloads model weights from HuggingFace on first load (`parakeet_mlx.from_pretrained` / `_nemo_asr.models.ASRModel.from_pretrained`) — a network call that could reveal the user's IP and model preferences to HuggingFace servers.
- Logs transcription character counts and model names.

Note: the model download only happens once (weights are cached). Post-download, Parakeet is fully offline. The concern is limited to first-run model download, not transcription content leakage.

**Severity:** LOW — Parakeet is disabled by default (`PARAKEET_ENABLED=False`). Model weights are cached locally after first download. Privacy mode primarily targets content not being sent externally, and transcription content itself is never sent. The HuggingFace download is the only external network call.

**Fix:** In `_load_parakeet_model()` (Path A) and `ParakeetSTTAdapter.warmup()` (Path B), check whether `settings.get("privacy_mode_enabled")` is true and whether the model is already cached locally before issuing a download. This matches the pattern in `TranslationService`.

---

## Test Coverage Summary

| Scenario | Path A (NeMo/engine.py) | Path B (parakeet-mlx) |
|----------|------------------------|----------------------|
| Adapter disabled (flag off) | `test_parakeet_adapter.py::TestParakeetAdapterDisabled` | `TestRouterFactoryParakeet::test_factory_excludes_parakeet_when_disabled` |
| Successful transcription | `TestParakeetAdapterEnabled::test_parakeet_reached_when_balanced_unavailable` | `TestParakeetMLXTranscribe::test_transcribe_returns_stt_result` |
| Chain ordering (Parakeet before SenseVoice) | `TestParakeetAdapterEnabled::test_parakeet_marker_inserted_before_sensevoice` | (N/A — router ordering not tested) |
| No-retry after failure | `TestParakeetAdapterEnabled::test_parakeet_marker_not_retried_after_failure` | `test_unload_resets_load_failed_flag` |
| Missing NeMo/library | `TestParakeetLoadNoNemo` | `TestParakeetMLXAvailability::test_is_available_when_parakeet_not_installed` |
| `mlx_lock` wrapping | Not tested | `TestParakeetMLXTranscribe::test_transcribe_under_mlx_lock` |
| OOM handling | Not tested (F-1) | Not tested |
| Privacy mode | Not tested (F-5) | Not tested (F-5) |
| Concurrent MLX+MPS | Not tested (F-3) | Not tested (F-3) |

Coverage is adequate for basic happy-path and fallback-chain scenarios. The three untested scenarios (F-1, F-3, F-5) are all LOW/MED severity and do not block current usage.

---

## Summary Table

| # | Severity | File(s) | Finding |
|---|----------|---------|---------|
| F-1 | LOW | `engine.py:1763–1776` | OOM errors not reported to error bus in adapter branch |
| F-2 | LOW | `stt_parakeet.py:149–154` | `mlx_inter_process_lock` not applied in parakeet-mlx path |
| F-3 | MED | `engine.py:2107–2159` | No comment/validation that NeMo+MPS is safe concurrent with MLX |
| F-4 | LOW | `stt_parakeet.py:61`, `stt_router_factory.py:67` | Arbitrary HuggingFace repo ID accepted without sanitization |
| F-5 | LOW | `stt_parakeet.py`, `engine.py` | `privacy_mode_enabled` does not gate model download network calls |

No findings block v2.0.5 production. Recommend addressing F-3 with a code comment first (trivial), then F-1 OOM guard in a follow-up wave.
