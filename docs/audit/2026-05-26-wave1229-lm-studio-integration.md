# Wave 1229 — LM Studio Integration Cross-Cutting Audit

**Date:** 2026-05-26
**Branch:** `audit/lm-studio-integration-W1229`
**Files audited:**
- `KrabEar/backend/llm_rewriter.py`
- `KrabEar/backend/lm_studio_lifecycle.py`
- `KrabEar/backend/llm_probe.py`
- Cross-references: `KrabEar/backend/service.py`, `KrabEar/core/engine.py`, `KrabEar/backend/recording_core_service.py`

---

## Summary

5 findings. No critical bugs. 2 medium-severity correctness gaps, 3 low/info observations.

---

## Findings

### F1 — `status()` reports `reachable=True` in HALF_OPEN state (LOW)

**File:** `llm_rewriter.py:1255`

```python
"reachable": self._circuit.state != "open",
```

`status()` returns `reachable=True` when `circuit_state == "half_open"`. This is incorrect: in HALF_OPEN the circuit is tentatively allowing one probe request but is NOT reliably reachable. The GUI `llm_status` IPC response (and `get_diagnostics` `llm` section in `service.py:1696`) surfaces this value — users see "reachable" during the recovery probe window even if the previous failure that opened the circuit was a genuine network/model error.

**Fix:** Change to `self._circuit.state == "closed"`.

---

### F2 — `_on_settings_saved` hook propagates `lm_studio_api_key` but not `llm_model` or `llm_base_url` (MEDIUM)

**File:** `service.py:213–217`

```python
def _on_settings_saved(old: dict, new: dict) -> None:
    new_key = str(new.get("lm_studio_api_key", ""))
    if new_key != str(old.get("lm_studio_api_key", "")):
        _rewriter_ref.set_api_key(new_key)
```

Only `lm_studio_api_key` changes are hot-propagated to the running `LLMRewriter`. If the user changes `llm_model` or `llm_base_url` via `set_settings` IPC, the rewriter continues using the stale model/URL until process restart. `LLMRewriter.set_model()` already exists and handles the case correctly (resets circuit breaker, starts background warmup). No analogous hook for `llm_base_url` exists at all.

**Impact:** User changes model in GUI → rewriter silently keeps old model. The GUI `list_llm_models` dropdown shows the new model as selected, but actual inference still goes to the old one.

**Fix:** Extend `_on_settings_saved` to also call `set_model()` when `llm_model` changes, and reconstruct `_base_url` (or add `set_base_url()`) when `llm_base_url` changes.

---

### F3 — `LLMHttpProbe` does not re-check `privacy_mode_enabled` (MEDIUM)

**File:** `llm_probe.py:150`

```python
if not settings.get("llm_rewrite_enabled", False):
    return
```

The probe's `_tick()` skips when `llm_rewrite_enabled=False` but never checks `privacy_mode_enabled`. If the user enables privacy mode at runtime (after probe has started), `LLMHttpProbe` continues probing `GET /api/v1/models` against LM Studio at the configured 30-second interval. This is a local-only call (localhost), so it does not leak data, but it violates the intent of privacy mode and adds noise to LM Studio logs.

Similarly, `LLMRewriter.rewrite()` in `engine.py` is gated by `_llm_rewrite_allowed()` which reads `llm_rewrite_enabled`, but `privacy_mode_enabled` is never consulted for LLM suppression anywhere in `llm_rewriter.py`, `llm_probe.py`, or the call path through `engine.py`. A transcription with `llm_rewrite_enabled=True` and `privacy_mode_enabled=True` will still send text to LM Studio.

**Fix:** Add `privacy_mode_enabled` check to `LLMHttpProbe._tick()` and to `engine._llm_rewrite_allowed()`.

---

### F4 — Blocking `time.sleep(10)` in `rewrite()` on HTTP 503 holds `_post_lock` gap, but more critically blocks the IPC thread (LOW)

**File:** `llm_rewriter.py:548`

```python
if response.status_code == 503:
    ...
    time.sleep(10)
    start = time.monotonic()
    try:
        with self._post_lock:
            response = self._session.post(...)
```

The 503 retry path sleeps 10 seconds on the calling thread. Since `rewrite()` is called from within the STT transcription pipeline (which is invoked synchronously from the IPC server thread in `AudioEngine.transcribe()`), this blocks the entire IPC server for 10 seconds. During that window no other IPC requests can be processed (recording start/stop, ping, settings reads, etc.). The `_post_lock` itself is not held during the sleep (it's acquired after), so concurrent probe/warmup calls are unaffected, but IPC starvation is real.

The same pattern appears in the `stream(gpu)` retry with a 2-second sleep (`llm_rewriter.py:601`), which is less severe.

**Fix:** Move 503 retry to a background thread or use a non-blocking approach; alternatively document as known and add a log warning explicitly calling out IPC stall risk.

---

### F5 — No rewrite hit-rate / success-rate metrics exposed (INFO)

**File:** `llm_rewriter.py`, `backend/metrics_collector.py`

`MetricsCollector` tracks only STT latency and confidence. There is no counter for LLM rewrite attempts, successes, fallbacks by reason (`circuit_open`, `timeout`, `chatbot_response`, etc.), or hit rate (rewrites that improved text vs. fell back). The IPC `llm_status` only returns last latency and last error, not aggregated counts.

`engine.py` records `llm_fallback_reason` per transcription in the returned result dict, but it is not aggregated anywhere. Users/operators cannot tell what fraction of transcriptions are being improved by LLM post-processing vs. silently falling back.

**Fix:** Add `LLMRewriter` counters (`_total_rewrites`, `_successful_rewrites`, `_fallbacks_by_reason: Counter`) exposed through `status()`, and surface them in `get_metrics_dashboard` IPC response.

---

## Non-findings (audited, no issue)

- **Connection state machine correctness:** CLOSED → OPEN transition at `fail_threshold=3` consecutive failures is correct. HALF_OPEN probe is serialized via `_half_open_probe_in_flight` flag. `record_success()` in `warmup_probe()` includes a forced `_transition_to(CLOSED)` fallback if `record_success()` doesn't close it (valid since `record_success` only auto-closes from HALF_OPEN). No stuck-state risk.
- **Error transitions (network vs model vs JIT):** Network errors (`ConnectionError`) and timeouts each call `record_failure()`. HTTP 401/4xx/5xx distinct codes: 401 calls `record_failure()`, 503 + Stream(gpu) + mlx_token retries once then calls `record_failure()` if second attempt also fails. Distinction between `rewriter.timeout`, `rewriter.connection_error`, `rewriter.channel_error`, and `rewriter.gpu_stream_error` is correct.
- **Probe–rewriter race:** `LLMHttpProbe` uses `passive_health_check()` (GET /models) and never modifies circuit state. `warmup_probe()` calls `record_success()` on circuit if alive. These two paths don't race destructively because `passive_health_check` is read-only on circuit state; only `warmup_probe()` (explicitly user-triggered via `warmup_rewriter` IPC) resets it.
- **Lifecycle (unload/load) interaction:** `recording_core_service.py` unloads the brain model on `start_recording` and reloads on `stop_recording`. This is isolated to `llm_brain_model` (the Voice Assistant brain), not `llm_model` (the rewriter). The rewriter model is not touched by lifecycle calls. No interaction between lifecycle and circuit breaker.
- **`shutdown_event` propagation:** `LLMRewriter.close()` sets `_shutdown_event`, which unblocks `warmup_sync` retry loop and `_idle_keepalive_loop`. `BackendService.close()` calls `probe.stop()` on `_llm_probe`. Clean.
- **JIT cold-start wiring:** `warmup_sync` is called in a daemon thread at startup (`service.py:187`). Default `retry_delays=[5,10,20,30,60]` gives 5 attempts over ~2 min. `LLMHttpProbe` continues every 30s after that. LM Studio JIT cold-start is covered.
- **Settings provider re-reads (post W1146/W918):** `_get_runtime_setting()` reads `_settings_svc.cached_settings()` on every call (5s TTL). `llm_rewrite_enabled`, `llm_timeout_sec`, `llm_idle_keepalive_enabled`, `rewriter_warmup_timeout_sec` are all read at runtime. The `runtime_timeout_provider` lambda in `LLMRewriter.__init__` correctly calls `_get_runtime_setting("llm_timeout_sec", ...)` per request.
- **Circuit breaker thresholds:** `fail_threshold=3`, `initial_reset_sec=60`, `max_reset_sec=600` with exponential backoff in HALF_OPEN. Reasonable for a local LM Studio that can cold-load in 20–60s.
