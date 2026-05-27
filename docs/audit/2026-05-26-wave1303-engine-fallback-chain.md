# W1303 — Audit: engine.py STT fallback chain

**Date:** 2026-05-26  
**Branch:** `audit/engine-fallback-chain-W1303`  
**Scope:** `KrabEar/core/engine.py` `_transcribe_with_fallback_impl`, `_unavailable_models`, per-adapter timeout, error propagation, language routing, STTRouter interaction, memory bound, test coverage.  
**Prior waves checked:** W1217 (Parakeet), W1218 (SenseVoice), W1219 (Voxtral), W1220 (WhisperX), W1216 (GigaAM), W1235 (pyannote double-checked lock), W1141 (_unavailable_models TTL).

---

## Findings (6)

### F1 — HIGH: WhisperX insertion ignores Parakeet marker when SenseVoice is disabled

**File:** `KrabEar/core/engine.py:1694–1701`

The WhisperX insertion logic scans for `_SENSEVOICE_MARKER` only to determine the insertion point:

```python
insert_pos = 1
for i, c in enumerate(candidates):
    if c == self._SENSEVOICE_MARKER:
        insert_pos = i + 1
        break
candidates = candidates[:insert_pos] + [self._WHISPERX_MARKER] + candidates[insert_pos:]
```

When `SENSEVOICE_ENABLED=False` and `PARAKEET_ENABLED=True`, the loop never finds `_SENSEVOICE_MARKER`, so `insert_pos` stays at `1`. WhisperX is inserted **before** Parakeet:

```
Actual:   [balanced, whisperx:adapter, parakeet:adapter, max1]
Expected: [balanced, parakeet:adapter, whisperx:adapter, max1]
```

The Voxtral insertion loop correctly handles all adapter markers via a set — the same fix is needed for WhisperX. Parakeet is EN-optimised and should precede WhisperX per the documented chain order.

**Fix:** Include `_PARAKEET_MARKER` in the WhisperX insertion scan:

```python
_pre_whisperx = {self._PARAKEET_MARKER, self._SENSEVOICE_MARKER}
insert_pos = 1
for i, c in enumerate(candidates):
    if c in _pre_whisperx:
        insert_pos = i + 1
```

---

### F2 — HIGH: W1141 _unavailable_models TTL was never implemented

**File:** `KrabEar/core/engine.py:336`

```python
self._unavailable_models: set[str] = set()
```

The set is a plain `set` with no timestamp tracking. Once a model or adapter marker is added (transient ImportError, timeout, OOM), it remains permanently blacklisted for the entire engine lifetime. No TTL eviction code exists anywhere in the codebase (confirmed via `grep -rn "TTL\|_unavailable_models_time\|_model_failure_time"` across all `.py` files — zero matches).

Consequences:
- A transient network-timeout or cold-start failure permanently evicts a model for the session.
- A Parakeet `ImportError` on first call (nemo not installed) correctly blacklists; but a recoverable timeout should re-enable after N minutes.
- `_unavailable_models` is never cleared unless the engine is restarted.

W1141 was referenced in the audit prompt as "TTL added" but no commit matching `W1141` or any `unavailable.*ttl` pattern exists in `codex/krab-ear-v2` history.

**Recommendation:** Replace `set[str]` with `dict[str, float]` mapping marker → fail_timestamp. Re-enable after configurable `STT_UNAVAILABLE_TTL_SEC` (suggested: 900 s). Permanent errors (OOM, ImportError) can use `float('inf')`.

---

### F3 — MEDIUM: Parakeet adapter has no language gate at chain-build time

**File:** `KrabEar/core/engine.py:1666–1670`

```python
if settings.PARAKEET_ENABLED and self._PARAKEET_MARKER not in self._unavailable_models:
    if len(candidates) >= 1:
        candidates = [candidates[0], self._PARAKEET_MARKER] + candidates[1:]
```

Parakeet-TDT-1.1B (NeMo) is EN-only — its `_transcribe_parakeet` hardcodes `"language": "en"` in the return dict (line 2157). However the chain-build code inserts the marker unconditionally for any `_effective_lang`, including `"ru"` and `"es"`.

When language is "ru" or "es", Parakeet will always produce low-quality/wrong output and waste latency (2–5s for model load + inference). The adapter never raises on non-EN input — it just returns garbage text with `"language": "en"`. Because the result succeeds, the chain stops there without trying GigaAM or Whisper.

Contrast with GigaAM and RU-finetune which both guard `_effective_lang == "ru"` at chain-build time (lines 1637, 1650).

**Fix:** Add `_effective_lang in (None, "en", "und")` guard to the Parakeet insertion block, or add a runtime language check inside `_transcribe_parakeet` that raises if language is not `"en"`.

---

### F4 — MEDIUM: ThreadPoolExecutor per-model creates thread-leak risk when watchdog is disabled

**File:** `KrabEar/core/engine.py:1795–1797`

```python
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(self._transcribe_model, ...)
    result = future.result(timeout=timeout)  # timeout = TRANSCRIBE_TIMEOUT_SEC = 3600s
```

`ThreadPoolExecutor.__exit__` calls `shutdown(wait=True)`, which **blocks until the submitted thread finishes**. When `future.result(timeout=...)` raises `concurrent.futures.TimeoutError`, the code continues to the except block, but the `with` exit then waits for the hung thread (empirically confirmed — a test blocked for the full thread duration).

In normal operation, the MLX watchdog (120s) exits the thread early via `MLXTimeoutError` which propagates through `future.result()` before the 3600s outer timeout fires. But when `MLX_CRASH_RECOVERY_ENABLED=False` (config line 567), the watchdog is bypassed and a GPU-stuck thread blocks the `ThreadPoolExecutor` exit for up to 3600s, effectively serialising all subsequent chain fallback attempts.

**Fix:** Pass `cancel_futures=True` to `shutdown()` (Python 3.9+) or use `executor.shutdown(wait=False)` after `future.cancel()`. The MLX watchdog should remain enabled (`MLX_CRASH_RECOVERY_ENABLED=True` by default) as the primary protection; this is a defence-in-depth for the disabled-watchdog case.

---

### F5 — LOW: STT_PARAKEET_ENABLED and STT_SENSEVOICE_ENABLED are dead settings

**File:** `KrabEar/core/config.py:362,374`

Two settings are defined in `config.py` but never read by any Python module:
- `STT_PARAKEET_ENABLED` (line 362) — described as "STTRouter integration for MLX Parakeet"
- `STT_SENSEVOICE_ENABLED` (line 374) — described as "STTRouter integration"

The STTRouter (`stt_router.py`) only provides `get_gigaam_adapter()` and `select_adapter_scored()`. It does not read these flags. The engine.py fallback chain uses `PARAKEET_ENABLED` and `SENSEVOICE_ENABLED` (distinct settings at lines 268 and 255). Verified via `grep -rn "STT_PARAKEET_ENABLED|STT_SENSEVOICE_ENABLED"` — zero callers.

Both settings are documented as opt-in flags for a "future STTRouter integration" that was never completed. They waste `KRAB_EAR_STT_PARAKEET_ENABLED` env-var namespace and may mislead operators into believing toggling them affects the chain.

**Recommendation:** Remove from config or add a TODO comment noting that they are reserved for a not-yet-implemented scored-router path.

---

### F6 — LOW: No test for all-adapters-fail with offline_strict raises RuntimeError

**File:** `KrabEar/tests/test_engine_edge_cases.py` and `test_audio_engine.py`

`test_all_local_fail_online_uses_remote` (test_engine_edge_cases.py:136) covers the online path. `test_audio_engine.py:171` covers `offline_strict` with a single model. However, there is no test that exercises the full scenario where **all adapter markers and all Whisper model candidates fail** with `NETWORK_MODE="offline_strict"`, confirming that `RuntimeError("Все доступные STT-движки вышли из строя.")` is raised at line 1857.

The existing coverage only fails the balanced model; tests for WhisperX + SenseVoice exhaustion (test_whisperx_adapter.py:162) set `NETWORK_MODE="offline_strict"` but do not assert the final RuntimeError.

**Recommended test:**

```python
def test_all_adapters_exhausted_offline_strict_raises():
    engine = AudioEngine()
    engine._unavailable_models = {
        engine._GIGAAM_MARKER, engine._RU_FINETUNE_MARKER,
        engine._PARAKEET_MARKER, engine._SENSEVOICE_MARKER,
        engine._WHISPERX_MARKER, engine._VOXTRAL_MARKER,
    }
    with patch("core.engine.settings") as mock_cfg:
        mock_cfg.MODEL_BALANCED = "balanced"
        mock_cfg.model_max_list = ["balanced"]
        mock_cfg.NETWORK_MODE = "offline_strict"
        mock_cfg.PARAKEET_ENABLED = True
        mock_cfg.SENSEVOICE_ENABLED = True
        mock_cfg.WHISPERX_ENABLED = True
        mock_cfg.VOXTRAL_ENABLED = True
        mock_cfg.STT_GIGAAM_ENABLED = False
        mock_cfg.STT_USE_RU_FINETUNE = False
        mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 5
        with patch.object(engine, "_transcribe_model", side_effect=RuntimeError("fail")):
            with pytest.raises(RuntimeError, match="Все доступные"):
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "ru")
```

---

## Summary

| # | Severity | Finding |
|---|----------|---------|
| F1 | HIGH | WhisperX inserted before Parakeet when SenseVoice disabled — chain order violation |
| F2 | HIGH | W1141 TTL for `_unavailable_models` never implemented — transient failures permanently blacklist |
| F3 | MEDIUM | Parakeet added to chain regardless of language — EN-only model runs on RU/ES audio |
| F4 | MEDIUM | ThreadPoolExecutor shutdown blocks thread when watchdog disabled (`MLX_CRASH_RECOVERY_ENABLED=False`) |
| F5 | LOW | `STT_PARAKEET_ENABLED` + `STT_SENSEVOICE_ENABLED` are dead config settings |
| F6 | LOW | No test coverage for all-adapters-exhausted + offline_strict → RuntimeError |

**Chain ordering (correct when all adapters enabled):**
`GigaAM → RU-finetune → balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates → remote`

F1 only misfires when SenseVoice is disabled while Parakeet and WhisperX are both enabled — a realistic configuration for English-primary deployments.
