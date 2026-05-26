# Audit: engine.py STT Fallback Chain (W1137)

Date: 2026-05-26  
Branch: `audit-engine-stt-fallback-W1137`  
Auditor: sub-agent W1137  
Scope: `KrabEar/core/engine.py` — `_transcribe_with_fallback_impl`, `_transcribe_model`, `_transcribe_remote`, `_unavailable_models` set, multipass retry, mlx_lock interactions, observability.

---

## Summary

5 findings, severity: 1 HIGH, 2 MEDIUM, 2 LOW.

---

## F1 — HIGH: `TRANSCRIBE_TIMEOUT_SEC` default is 3600 s — no practical guard per-model attempt

**File:** `KrabEar/core/config.py:125`, `KrabEar/core/engine.py:1792–1797`

`TRANSCRIBE_TIMEOUT_SEC` defaults to **3600 seconds** (1 hour). This is the `future.result(timeout=...)` ceiling used for every whisper model attempt in the fallback chain. The intent (comment at config line 121-124) is to accommodate 1-hour file imports on the max profile. However, the same timeout value is reused for **all** models in the chain, including the balanced model used for live recordings.

In practice, for a 30-second live voice recording:
- A hung balanced model blocks the chain for up to 3600 s before advancing.
- The separate `MLX_TRANSCRIBE_TIMEOUT_SEC` (default 120 s) in `_transcribe_model` applies only when `MLX_CRASH_RECOVERY_ENABLED=True` via the watchdog path.
- The outer `concurrent.futures.ThreadPoolExecutor + future.result(timeout=TRANSCRIBE_TIMEOUT_SEC)` acts as a backstop but at 3600 s it provides no practical protection for interactive use.

The two timeout values are not coordinated: `MLX_TRANSCRIBE_TIMEOUT_SEC=120s` (watchdog) fires first and surfaces as `MLXTimeoutError` which is correctly handled. But if the watchdog is disabled (`MLX_CRASH_RECOVERY_ENABLED=False`), the only guard is `TRANSCRIBE_TIMEOUT_SEC=3600s`.

**Risk:** With watchdog disabled, a single hung model attempt can freeze the transcription path for up to 1 hour, effectively DoS-ing the backend for interactive use.

**Recommendation:** Add a separate `TRANSCRIBE_TIMEOUT_LIVE_SEC` (e.g. 180 s) for numpy/live-recording paths, distinct from file import. Alternatively document the dependency: `TRANSCRIBE_TIMEOUT_SEC` is only safe because `MLX_CRASH_RECOVERY_ENABLED=True` (watchdog) is the first-line guard — add an assertion or warning at startup if both `MLX_CRASH_RECOVERY_ENABLED=False` and `TRANSCRIBE_TIMEOUT_SEC > 300`.

---

## F2 — MEDIUM: `_unavailable_models` set grows unbounded and never resets across session lifetime

**File:** `KrabEar/core/engine.py:336, 1775, 1805, 1813, 1816, 1832, 1846, 1849`

`_unavailable_models` is an instance-level `set[str]` initialized empty at construction (`__init__:336`) and accumulates any model or adapter marker that fails for **any** reason: `TimeoutError`, `MLXTimeoutError`, `MemoryError`, `OSError`, or a generic `Exception`. There is no TTL, no periodic reset, no IPC method to clear it at runtime.

Consequence: a transient failure (e.g., temporary OOM due to a one-off memory spike, or a network timeout for the remote adapter) permanently removes a model from the chain for the rest of the process lifetime. For long-running backends (launchd KeepAlive), this means:

1. A model evicted by a single transient `OSError(errno=12)` never comes back.
2. All 6 adapter markers (`sensevoice:adapter`, `parakeet:adapter`, `whisperx:adapter`, `voxtral:adapter`, `ru_finetune:adapter`, `gigaam:adapter`) are treated identically to model strings — a transient adapter failure permanently disables it.
3. The multipass retry path (`_maybe_multipass_retry`) also writes to `_unavailable_models` on exception (line 1306), compounding the issue.

The set itself is small (at most ~10 entries), so memory pressure is not a concern, but the behavioural consequence — permanently degraded chain after transient errors — is a correctness issue.

**Recommendation:**
- Add `clear_unavailable_models()` IPC handler (callable via diagnostics) so operators can recover without restart.
- Add optional TTL: evicted models get a `time.monotonic()` timestamp; re-admit after configurable interval (e.g. 5 min) for transient error categories (`Exception`, non-OOM `OSError`). Keep permanent eviction only for `MemoryError` / OOM `OSError` (errno 12).
- Distinguish permanent failures (OOM, model truly missing) from transient ones (timeout, generic exception) at the eviction site.

---

## F3 — MEDIUM: Remote STT timeout is hardcoded to 60 s and not configurable

**File:** `KrabEar/core/engine.py:3067`

```python
resp = requests.post(
    settings.STT_GATEWAY_URL,
    ...
    timeout=60,  # hardcoded
)
```

The `requests.post` timeout in `_transcribe_remote` is a magic constant `60` not drawn from any config setting. There is no `STT_GATEWAY_TIMEOUT_SEC` in `config.py`. For slow/remote gateways (e.g., a self-hosted Whisper API under load), 60 s may be insufficient, causing the remote fallback to raise an exception and surface as a failed transcription rather than a slow success.

Additionally, `timeout=60` in `requests.post` is a connect+read combined timeout (not a per-byte read timeout), so large audio uploads over slow connections may time out prematurely.

**Recommendation:** Add `STT_GATEWAY_TIMEOUT_SEC: float = 60.0` to `config.py` and reference it: `timeout=settings.STT_GATEWAY_TIMEOUT_SEC`. Document it alongside `STT_GATEWAY_URL`.

---

## F4 — LOW: No Sentry breadcrumbs at fallback-chain model switches

**File:** `KrabEar/core/engine.py:1763–1849`

`add_breadcrumb` is called at transcription start (line 704) and finish (line 1033), but **not** at each model switch within `_transcribe_with_fallback_impl`. When a model fails and the chain advances, the breadcrumb trail shows only `transcribe_start` → `transcribe_finish` with no record of which models were tried, which failed, and why.

During debugging a crash report, it is impossible to determine from Sentry whether the final transcription used balanced or a fallback model, whether remote STT was hit, or which specific adapter triggered an OOM.

The `transcribe_finish` breadcrumb does record `engine` (from `result.get("engine", "mlx-whisper")`) but this is the engine of the **successful** result only. Failed attempts are invisible.

**Recommendation:** Add a breadcrumb at each `_unavailable_models.add()` call inside the fallback chain loop:
```python
_add_bc(category="stt_fallback", message="model_evicted", level="warning",
        data={"model": model_name, "reason": type(e).__name__, "error": str(e)[:120]})
```
This is privacy-safe (no transcript text) and follows the existing pattern at lines 704/1033.

---

## F5 — LOW: `audio_lang_id` RLock cascade is safe but implicit — no assertion

**File:** `KrabEar/core/audio_lang_id.py:203`, `KrabEar/core/engine.py:1892`

`AudioLanguageID._run_detect()` acquires `mlx_lock()` (an `RLock`) for encoder inference. If `AudioLanguageID.detect()` were called **from within** `_transcribe_model` (which also holds `mlx_lock()`), the nested acquisition would succeed because `mlx_lock` is an `RLock` (reentrant). This is documented in `_transcribe_model` docstring (line 1863-1864): "RLock позволяет повторный захват из того же потока (fallback chain)."

However, `AudioLanguageID` is not imported or instantiated anywhere in `engine.py` — it is called from `backend/service.py` and the STT router, **before** the transcription call, so the cascade does not actually occur in production. The concern from the W1116 task is a future risk if someone moves the language-ID call inside the fallback chain.

The gap: there is no assertion or comment at the `_transcribe_model` entry point stating that callers must not hold `mlx_lock()` when calling `AudioLanguageID.detect()` from outside the chain. If a future refactor moves `_resolve_language` (or adds audio-based lang-ID inside `_transcribe_with_fallback_impl`) this implicit invariant will be silently violated.

**Recommendation:** Add a comment at `_transcribe_with_fallback_impl` entry: "AudioLanguageID.detect() must not be called from inside this method — it acquires mlx_lock() which would form an RLock re-entry cascade (safe for same-thread but serializes LID + STT). Resolve language before calling this method." Low priority since the current code is correct; this is a defensive documentation fix.

---

## Test Coverage Assessment

| Path | Coverage |
|---|---|
| Fallback chain order (adapter sequence) | `test_adapter_benchmark.py::test_fallback_chain_order` — source-text inspection; adequate |
| `_unavailable_models` skips on re-entry | `test_audio_engine.py::test_fallback_chain_skips_unavailable_model` — covered |
| Remote called when all local fail | `test_audio_engine.py::test_remote_called_when_all_local_fail` — covered |
| Timeout → evict model | Not directly tested; watchdog path has `test_mlx_subprocess.py` coverage |
| Remote STT timeout (hardcoded 60s) | **No test** — `_transcribe_remote` not unit-tested for timeout behaviour |
| `_unavailable_models` TTL / reset | **No test** — there is no reset mechanism to test |
| Sentry breadcrumb on model eviction | **No test** |

---

## Conclusion

The fallback chain logic itself is **correct**: it advances to each successive model on any exception, gates remote STT behind `NETWORK_MODE`, and uses `RLock` correctly for MLX thread safety. The primary actionable gaps are:

1. (F1 HIGH) Watchdog-disabled path has no practical per-model timeout for live recordings.
2. (F2 MEDIUM) Transient failures permanently evict models for the process lifetime — add TTL or IPC reset.
3. (F3 MEDIUM) Remote STT timeout is not configurable.
