# W1179 Audit — lm_studio_lifecycle.py

**Branch:** audit/lm-studio-lifecycle-W1179  
**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/lm_studio_lifecycle.py` — `load_model_async()` / `unload_model_async()` REST + CLI lifecycle management for LM Studio brain model memory management.  
**Related files audited:** `KrabEar/backend/recording_core_service.py` (call sites), `KrabEar/tests/test_lm_studio_lifecycle.py` (test coverage), `KrabEar/backend/llm_rewriter.py` (circuit breaker context), `KrabEar/backend/settings_validator.py` (input validation).

---

## Summary

The module is intentionally minimal (152 lines) and fire-and-forget by design. Core safety properties hold: daemon threads, short timeouts, never-raise contract. Five findings were identified — one MED security/correctness issue and four LOW operational gaps.

---

## Findings

### F-1 — MED: JSON body built with f-string instead of `json.dumps` (injection + correctness)

**File:** `lm_studio_lifecycle.py:61`

```python
body = f'{{"model":"{model_id}"}}'.encode()
```

A `model_id` containing a double-quote character (e.g. `my-model"extra`) produces invalid JSON that LM Studio's parser rejects with a 400 error. A model_id containing `"},{"injected":"val` would produce structurally malformed JSON. Since `model_id` comes from user-controlled settings and is not validated by `settings_validator.py`, this is a realistic breakage vector when LM Studio model identifiers contain special characters (e.g. HuggingFace paths like `lmstudio-community/Qwen3-30B-A3B"s`).

The `_try_rest_unload` function builds the URL via f-string interpolation (`f"{api_root}/api/v0/models/{model_id}/unload"`), which is also affected by model_ids containing URL path separators (`/`, `..`), though LM Studio is a localhost server so practical SSRF risk is low.

**Fix:** Replace line 61 with `json.dumps({"model": model_id}).encode()`. Use `urllib.parse.quote(model_id, safe="")` for the unload URL path segment.

---

### F-2 — LOW: No "already loaded" guard — redundant load on every stop_recording

**File:** `lm_studio_lifecycle.py:126-151`, `recording_core_service.py:776-781`

`load_model_async()` unconditionally fires a REST `POST /api/v0/models/load` on every `stop_recording` call, even if the brain model is already loaded. LM Studio handles idempotent reload gracefully (returns 2xx), but this triggers unnecessary network activity (and potentially causes LM Studio to briefly evict then reload the model depending on version). Over a long session with many short recordings, this generates repeated load requests.

No module-level state tracks whether a load request is already in flight or whether the model is known-loaded. The test suite verifies that concurrent loads do not raise but does not assert idempotency (i.e., that only one actual HTTP request is sent when called N times for the same model).

**Fix:** Add a module-level `threading.Lock` and `_last_loaded: str | None` variable; skip the REST call if `_last_loaded == model_id` and no explicit unload has been requested since. Alternatively, document the current "always reload" behavior as intentional and add a test asserting the count of HTTP calls per session.

---

### F-3 — LOW: Load/unload race when recording stops very quickly after starting

**File:** `lm_studio_lifecycle.py:96-151`

Both `unload_model_async()` (called on `start_recording`) and `load_model_async()` (called on `stop_recording`) are fire-and-forget daemon threads with no coordination between them. If recording is started and immediately stopped (e.g., accidental tap, < 200 ms), two threads are spawned in rapid succession:

1. Unload thread: `POST /api/v0/models/{model}/unload` — REST timeout up to 1.5 s
2. Load thread: `POST /api/v0/models/{model}/load` — REST timeout up to 1.5 s

Due to network jitter or LM Studio scheduling, the load request may arrive at LM Studio *before* the unload completes, leaving the brain model in an unloaded state contrary to user intent. The `stop_recording` idempotency guard in `recording_core_service.py` (`recorder.start()` returns False if already recording) prevents double-start, but a legitimate fast record+stop creates this race.

**Fix:** Introduce a module-level sequence counter or a `threading.Event`-based handshake so that any pending unload thread is joined before the load thread begins. Given fire-and-forget semantics, a simpler fix is to store the last-dispatched action (`"load"` / `"unload"`) and skip a `load` if no `unload` has been dispatched since the last `load`.

---

### F-4 — LOW: `privacy_mode_enabled` not checked before `load_model_async`

**File:** `recording_core_service.py:776-781`

When `privacy_mode_enabled=True` is set, the translation service and Sentry init both gate their operations on this flag (see `translation_service.py:96`, `observability.py:122`). However, `load_model_async()` is called unconditionally after `stop_recording` regardless of privacy mode. In strict privacy mode a user may not want the backend to make any outbound connections, including to `localhost:1234`. More critically, loading the brain model pre-warms it for Voice Assistant use — which may be undesirable in privacy mode where LLM processing of content should be suppressed.

The `lm_studio_lifecycle` module itself has no privacy awareness and receives no settings context; the guard must live in `recording_core_service.py`.

**Fix:** Add `and not settings.get("privacy_mode_enabled")` to the `if brain_model and preload_enabled` guard at `recording_core_service.py:778`.

---

### F-5 — LOW: No interaction with W1146 `_shutdown_event` — load thread can fire during backend teardown

**File:** `lm_studio_lifecycle.py:126-151`

The `LLMRewriter` warmup retry loop was fixed in W1146 to use `self._shutdown_event.wait(timeout=delay)` so it exits cleanly when the backend shuts down (`llm_rewriter.py:1147`). The `lm_studio_lifecycle` worker threads have no equivalent awareness: if `load_model_async()` is triggered just before backend shutdown (e.g., from a last `stop_recording` call during graceful shutdown), the daemon thread will be abruptly killed mid-HTTP-request. This is safe because daemon threads die with the process, but it means LM Studio receives an incomplete POST and may log spurious errors.

More practically: `GracefulShutdownHandler` (`backend/shutdown_handler.py`) orchestrates backend teardown but does not join or cancel in-flight `lm_studio_lifecycle` threads. No `shutdown_event` or cancellation token is passed into the lifecycle module.

This is a LOW-severity polish item — the fire-and-forget design means the worst case is a spurious log line in LM Studio. However, consistency with the W1146 pattern (using a shutdown event to abort waiting) would be cleaner.

**Fix:** Pass an optional `shutdown_event: threading.Event | None = None` parameter to both public functions. In `_worker()`, check `shutdown_event.is_set()` before each REST/CLI attempt.

---

## Test coverage assessment

`test_lm_studio_lifecycle.py` (Wave 143, 369 lines) covers the main happy paths and failure modes well:
- REST 2xx success, 404/405 silent fallback, 500 error
- CLI fallback when REST fails, CLI not-found, non-zero exit
- Both functions are no-ops on empty model_id
- Both functions do not raise when LM Studio is offline
- Concurrent calls (8 threads) complete without error
- Timeout constants are passed to `urlopen` and `subprocess.run`
- Worker threads are daemon threads

**Coverage gaps:**
- No test for model_id containing special characters (quotes, slashes) — F-1
- No test asserting single HTTP call per model per session (idempotency) — F-2
- No test for load/unload ordering when called in rapid succession — F-3
- No test for privacy_mode suppression — F-4
- No test for shutdown_event cancellation — F-5

---

## Non-findings (checked, not actionable)

- **REST timeout / hang**: `_REST_TIMEOUT_SEC = 1.5` is passed as a socket-level timeout to `urllib.request.urlopen`. While this is technically per-operation (not total-request), LM Studio responses are small JSON payloads that arrive in a single TCP segment; the per-operation timeout is sufficient in practice.
- **CLI injection**: `subprocess.run([lms, action, model_id], ...)` uses a list (not shell=True), so shell injection via model_id is not possible.
- **Error propagation**: Both public functions catch all exceptions in the worker and log at DEBUG level. The recording flow is never blocked. This is intentional and correct.
- **Circuit breaker interaction**: `lm_studio_lifecycle` does not interact with `LLMRewriter`'s `CircuitBreaker`. The circuit breaker gates rewrite calls; load/unload are independent lifecycle operations. No coupling is needed.
