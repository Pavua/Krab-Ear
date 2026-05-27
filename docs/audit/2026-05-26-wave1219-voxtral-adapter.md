# Audit W1219 — Voxtral STT Adapter (Phase 4.4)

**Date:** 2026-05-26  
**Branch:** audit/audio-quality-residual-W1100 / codex/krab-ear-v2  
**Scope:** `core/engine.py` — `_load_voxtral_model()`, `_transcribe_voxtral()`, fallback-chain insertion (lines 2367-2680); config `core/config.py`; tests `KrabEar/tests/test_voxtral_adapter.py`  
**Auditor:** W1219 (sub-agent, read-only)

---

## Summary

Voxtral adapter exists **inside `core/engine.py`** — there is no separate `stt_voxtral.py` file. The adapter is implemented as two methods on `AudioEngine`: `_load_voxtral_model()` and `_transcribe_voxtral()`, with a marker injected into the fallback chain at position 5 (after WhisperX, before max-candidates). Opt-in only (`VOXTRAL_ENABLED=False` default). 6 findings identified, 3 HIGH / 2 MEDIUM / 1 LOW.

---

## Findings

### F1 - HIGH: No `mlx_lock` around `_voxtral_generate` inference

**File:** `core/engine.py:2641`  
**Observation:** `_voxtral_generate(input_ids, model, max_tokens=..., eos_id=...)` is called without `with mlx_lock():`. The `mistral_inference.generate.generate` function drives its own transformer forward pass which can use Metal/MLX internals.

**Risk:** The CLAUDE.md invariant is explicit: *"ALL MLX inference must be serialized through `core.mlx_lock.mlx_lock()`"* to prevent SIGSEGV from concurrent GPU access. If Voxtral is enabled alongside concurrent MLX-Whisper calls (e.g. during a re-transcription job), this races on the GPU.

**Fix:** Wrap the generate call:
```python
from core.mlx_lock import mlx_lock
with mlx_lock():
    out_tokens, _ = _voxtral_generate(input_ids, model, ...)
```
Note: `_VoxtralTransformer.from_folder()` in `_load_voxtral_model()` also loads weights into Metal memory and should likewise be wrapped.

---

### F2 - HIGH: No timeout on `_transcribe_voxtral` — hangs silently

**File:** `core/engine.py:1763-1776`  
**Observation:** Adapter branches in the fallback chain loop call `adapter_fn()` directly (line 1770) without any timeout wrapper. The `concurrent.futures.ThreadPoolExecutor` + `future.result(timeout=...)` guard (lines 1795-1797) is applied only to the **Whisper** branches, not adapter branches. `_voxtral_generate` can run for several seconds to minutes on long audio or GPU stall.

**Risk:** A Metal GPU stall or an unusually long generation (e.g. large audio + `VOXTRAL_REASONING_ENABLED=True`) will block the IPC thread indefinitely — the same class of hang that AGENT-H / AppHang issues were traced to. Whisper adapters have `MLXTimeoutError` protection via the MLX watchdog; Voxtral has none.

**Fix:** Wrap `adapter_fn()` for adapter branches in a `ThreadPoolExecutor` future with `settings.TRANSCRIBE_TIMEOUT_SEC`, or apply the existing MLX subprocess watchdog pattern.

---

### F3 - HIGH: `VOXTRAL_MODEL` setting has no allowlist — arbitrary HuggingFace repo injection

**File:** `core/engine.py:2384`, `core/config.py:308`  
**Observation:** `snapshot_download(repo_id=settings.VOXTRAL_MODEL)` downloads whatever repo ID is stored in `settings.json`. There is no validator, prefix check, or revision pin. Any string value can be injected via the `set_settings` IPC method.

**Risk:** An attacker with IPC access (local socket, no auth by default) can set `voxtral_model` to an arbitrary HuggingFace repo, causing the backend to download and execute arbitrary model weights (model config + weight deserialization). This is the same SSRF-via-HF-download class of vulnerability applicable to other STT adapters.

**Mitigation options:**
1. Add a Pydantic `field_validator` that enforces the value starts with `"mlx-community/"` or a configurable allowlist.
2. Pass `revision=` with a pinned commit SHA (prevents supply-chain model swaps even for trusted repos).
3. Add `local_files_only=True` when `NETWORK_MODE == "offline_strict"`.

---

### F4 - MEDIUM: File-path audio case silently passes wrong sample rate to Voxtral

**File:** `core/engine.py:2588-2589`  
**Observation:** When `audio_data` is a `str` or `Path`, the code resolves the path and passes it directly as `audio_path` without reading the actual sample rate or resampling. Voxtral's audio encoder expects **16 kHz mono** audio. If the caller passes a file at 44.1 kHz or 48 kHz (common from imported audio or macOS system capture), the model silently receives a pitch-shifted / time-compressed signal, degrading recognition quality with no warning logged.

**Contrast:** The `bytes` branch always writes at `16000` (line 2598/2605); the `numpy` branch always writes at `16000` (line 2616). Only the `str/Path` branch trusts the file as-is.

**Fix:** Use `soundfile` to read and verify `samplerate` for the file-path branch; resample with `librosa.resample` or `scipy.signal.resample_poly` if not 16 kHz. Log a warning when resampling is required.

---

### F5 - MEDIUM: `privacy_mode` not respected — reasoning path not gated

**File:** `core/engine.py:2623-2629`  
**Observation:** There is no check for `settings.PRIVACY_MODE` (or `network_mode == "offline_strict"`) before enabling the `VOXTRAL_REASONING_ENABLED` path. When privacy mode is active, the LLM rewriter is suppressed to avoid sending content to any model. Voxtral's reasoning path is semantically equivalent — it runs an LLM over transcript content — and should be similarly gated.

**Additionally:** There is no `_push_error` / error bus event when Voxtral fails (unlike GigaAM which has `stt.gigaam_hf_cache_miss` at line 2534). A failed Voxtral invocation produces only a `logger.warning` and silently drops back to Whisper.

**Fix:** Add a privacy guard before enabling reasoning:
```python
if settings.VOXTRAL_REASONING_ENABLED:
    in_privacy = getattr(settings, "PRIVACY_MODE", False) or \
                 getattr(settings, "NETWORK_MODE", "") == "offline_strict"
    if in_privacy:
        # suppress LLM reasoning in privacy mode
        prompt_text = "Transcribe the audio accurately."
    else:
        prompt_text = "Transcribe the audio accurately. Then provide a brief summary."
```

---

### F6 - LOW: Test coverage missing for file-path branch and privacy-mode gate

**File:** `KrabEar/tests/test_voxtral_adapter.py`  
**Observation:** The test file covers: disabled flag, chain position, successful transcription when balanced is unavailable, model-load error caching, `HistoryItem.reasoning` field roundtrip, and `VOXTRAL_REASONING_ENABLED=True` path. It does **not** cover:
- The `str/Path` audio input branch in `_transcribe_voxtral` (F4 above).
- Privacy-mode suppression of reasoning (F5 above).
- The memory-guard bypass (adapter branches skip the `avail_gb` check at lines 1782-1789).
- Error bus emission on Voxtral failure (no `_push_error` call exists).

**Severity:** LOW — existing coverage is otherwise solid for the main paths; gaps correspond directly to the missing implementation behaviors (F3-F5).

---

## Not Found / Not Applicable

- **Torch device selection:** Voxtral uses `mistral-inference` (CPU/Metal via the library's own device dispatch), not PyTorch+MPS or MLX directly. No explicit `torch.device()` call — device is implicit in `_VoxtralTransformer.from_folder()`.
- **Memory bound for model load:** The `_HEAVY_MODEL_MIN_FREE_GB` check (lines 1782-1789) applies only to Whisper-branch models, not to adapter branches. Voxtral (~2-3 GB for 4-bit quant) loads without a free-memory pre-check. Addressed implicitly by F2 (no timeout) but not separately actionable.

---

## Verdict

| ID | Severity | Title |
|----|----------|-------|
| F1 | HIGH | No `mlx_lock` around Voxtral generate — SIGSEGV race |
| F2 | HIGH | No timeout on adapter branch — IPC hang risk |
| F3 | HIGH | Arbitrary HF repo injection via `voxtral_model` setting |
| F4 | MEDIUM | File-path branch skips sample-rate normalisation |
| F5 | MEDIUM | `VOXTRAL_REASONING_ENABLED` not gated by privacy mode |
| F6 | LOW | Test coverage missing for file-path branch and privacy gate |

All findings are in `core/engine.py`. No separate adapter file exists; the implementation is inline on `AudioEngine`.
