# Wave 818 Audit: `backend/llm_rewriter.py`

**Date:** 2026-05-26  
**File:** `KrabEar/backend/llm_rewriter.py` (1437 LOC)  
**Scope:** Code quality, CircuitBreaker correctness, length ratio guard, chatbot detection, warmup retry, error-bus coverage.

---

## Summary

The module is overall well-structured and adheres to the "never raises" contract throughout. Six issues were found: two medium-severity correctness bugs, two low-severity design gaps, and two minor style/documentation notes.

---

## 1. CircuitBreaker State Machine

### 1.1 HALF_OPEN probe flag not cleared on CLOSED→OPEN transition (BUG, medium)

**Location:** `record_failure()` lines 113–135  
**Description:**  
When the breaker is in `CLOSED` state and fails enough times to trip open, `_transition_to(CircuitState.OPEN)` is called. Inside `_transition_to`, only the `CLOSED` branch resets `_half_open_probe_in_flight`. The `OPEN` branch does not touch the flag. This means if a HALF_OPEN probe fails (`_half_open_probe_in_flight` is set True, then cleared by `record_failure` correctly via line 114), and then another probe from the re-opened state also fails and causes a CLOSED→OPEN transition (which cannot normally happen because CLOSED→OPEN requires `_consecutive_failures >= _fail_threshold`, but `_consecutive_failures` is incremented regardless of state), the flag state is always consistent because `record_failure` explicitly clears it at line 114 **before** checking which state to transition to.

Actually, reviewing carefully: `record_failure()` always does `self._half_open_probe_in_flight = False` at line 114 as its first statement, so the flag is always cleared on any failure. This is correct.

**Verdict:** No bug here. The early unconditional clear at line 114 is the right design.

### 1.2 `warmup_probe` forces OPEN→CLOSED by bypassing the state machine (BUG, medium)

**Location:** `warmup_probe()` lines 1105–1113  
**Code:**
```python
if ok and self._circuit.state != "closed":
    self._circuit.record_success()  # HALF_OPEN → CLOSED if applicable
    # Force CLOSED if still OPEN (record_success only transitions from HALF_OPEN)
    if self._circuit.state == "open":
        self._circuit._transition_to(CircuitState.CLOSED)
```
**Problem:**  
`_transition_to` is a private method. Calling it from outside `CircuitBreaker` bypasses the intended encapsulation boundary. More importantly, `record_success()` is designed to only transition from `HALF_OPEN→CLOSED`, not from `OPEN→CLOSED` — that transition should require the cooldown timer to elapse and `allow_request()` to be called, which itself promotes OPEN→HALF_OPEN. By forcing OPEN→CLOSED directly, `warmup_probe` skips the `_consecutive_failures` and `_opened_at` reset that `_transition_to(CLOSED)` does handle, but it also bypasses the intent of the state machine: a warmup success should advance the probe to HALF_OPEN first, not jump straight to CLOSED.

The comment acknowledges this is intentional for UX reasons (user loaded model, circuit should close). However, calling `_transition_to` from outside the class is a violation of the class contract documented in its own docstring ("Thread safety: not required — IPC server is single-threaded"). The method is considered internal. A cleaner approach would be a `force_close()` public method on `CircuitBreaker`.

**Recommendation:** Add `force_close()` public method to `CircuitBreaker` and call it from `warmup_probe` instead of accessing `_transition_to` directly.

### 1.3 `record_success()` in CLOSED state does not reset `_current_reset_sec` (design gap, low)

**Location:** `record_success()` lines 106–111  
**Code:**
```python
def record_success(self):
    self._half_open_probe_in_flight = False
    if self._state == CircuitState.HALF_OPEN:
        logger.info("Circuit breaker: HALF_OPEN -> CLOSED (проба успешна)")
        self._transition_to(CircuitState.CLOSED)
    self._consecutive_failures = 0
```
`_transition_to(CircuitState.CLOSED)` resets `_current_reset_sec` back to `_initial_reset_sec`. Good. But if `record_success()` is called while the state is already `CLOSED` (normal operation), `_consecutive_failures` is zeroed but `_current_reset_sec` is unchanged. This only matters if: (1) HALF_OPEN probe succeeds → CLOSED (resets), then (2) CLOSED has some failures below threshold, then (3) CLOSED succeeds — the `_current_reset_sec` is already reset from step 1. No real bug, just a minor observation.

**Verdict:** Not a bug in practice.

### 1.4 Thread-safety disclaimer is stale (low, doc)

**Location:** `CircuitBreaker` class docstring line 59  
```
Thread safety: не требуется — IPC server в Krab Ear однопоточный.
```
`LLMRewriter` now has `_idle_keepalive_thread` (daemon thread calling `warmup_probe` → `record_success`) and `set_model()` spawns `threading.Thread(target=self.warmup)`. The keepalive loop calls `warmup_probe()` which calls `self._circuit.record_success()` and `self._circuit._transition_to()` from a daemon thread while `rewrite()` may call `self._circuit.allow_request()` / `record_failure()` from the IPC thread. The comment saying no thread-safety is needed is incorrect for the current design.

In practice, on M-series CPUs the GIL makes simple attribute reads/writes atomic, so there is no crash risk for the boolean and integer fields used. But the comment misleads future authors into adding non-GIL-safe operations.

**Recommendation:** Update docstring to note GIL-only safety, or add a `threading.Lock` to be explicit.

---

## 2. Length Ratio Guard

**Location:** `_rewrite_impl()` lines 812–843  
**Bounds:** `ratio < 0.35` (too short) or `ratio > 3.0` (too long). Guard only applied when `input_len > 20`.

### 2.1 Bounds are correct and well-justified

The 0.35 lower bound (35%) catches complete truncation and most hallucinated-empty outputs. The 3.0 upper bound (300%) catches verbosity explosions. The 20-character floor prevents false positives on very short inputs like "Да" → "Да." (expansion due to punctuation).

### 2.2 `output_too_short` does not call `record_failure()` (BUG, medium)

**Location:** lines 817–829  
**Code:**
```python
if ratio < 0.35:
    ...
    self._last_error = "output_too_short"
    self._push_error(...)
    return LLMRewriteResult(ok=False, ...)
```
Neither `output_too_short` nor `output_too_long` (lines 830–843) call `self._circuit.record_failure()` before returning. This means repeated truncated/hallucinated responses from the LLM do **not** trip the circuit breaker. The circuit breaker was designed to protect against a degraded LLM; a model that consistently returns <35% of input is degraded and should be treated as a failure.

By contrast, `empty_response` (line 776) **does** call `record_failure()`:
```python
self._circuit.record_failure()
self._last_error = "empty_response"
```

The asymmetry is unintentional. An `output_too_short` outcome is functionally similar to `empty_response` — the model failed to perform its task.

**Recommendation:** Add `self._circuit.record_failure()` before the `return` in both `output_too_short` and `output_too_long` branches.

### 2.3 Guard is not applied in `fix_punctuation_only` or `summarize`

`fix_punctuation_only` uses word-count and word-set guards instead (stricter, appropriate for its contract). `summarize` uses no ratio guard at all — a summary can legitimately be much shorter. Both are consistent with their documented contracts.

---

## 3. Chatbot Detection

**Location:** `_CHATBOT_MARKERS` lines 200–214, applied at lines 787–809.

### 3.1 Marker list is prefix-only (potential false negative)

All markers are matched via `cleaned_lower.startswith(marker)`. A chatbot response that starts with "Конечно, вот исправленный текст:" would be caught by `"конечно,"` only if `conechno,` is a prefix. But "Вот исправленный текст" would pass because it does not start with any listed marker. `"вот исправленный"` is in the list — covers this case. However `"Вот:"` would not be caught unless `"вот:"` is in `_EXPLANATORY_PREFIXES` (it is not). Minor coverage gap.

### 3.2 English markers are minimal (low risk)

English markers are: `"i'm sorry"`, `"i apologize"`, `"here is"`, `"sure,"`. Models running in Russian mode (Qwen3, gemma-4) rarely respond in English, so this is low-risk but worth noting for completeness.

### 3.3 Chatbot detection does not call `record_failure()` (BUG, medium)

**Location:** lines 806–809  
```python
return LLMRewriteResult(
    ok=False, text=None, fallback_reason="chatbot_response", latency_ms=latency_ms
)
```
Similar to the ratio guard issue: a chatbot response means the LLM ignored its system prompt. This is a model-level failure mode that should register as a failure on the circuit breaker. Repeated chatbot responses will never trip the breaker as currently coded.

**Recommendation:** Add `self._circuit.record_failure()` before the `chatbot_response` return.

---

## 4. Warmup Retry Backoff

**Location:** `warmup_sync()` lines 1133–1192  
**Default delays:** `[5, 10, 20, 30, 60]` — 5 total attempts, ~125 seconds total wait.

### 4.1 Backoff is correct but not exponential

The delays `[5, 10, 20, 30, 60]` are sub-linear (not strictly exponential). This is pragmatic: exponential from 5s would reach 5×16=80s or 5×32=160s, which overshoots for a typical LM Studio cold-start scenario (20–60s). The chosen delays are reasonable for the use case.

### 4.2 Shutdown event integration is correct

`self._shutdown_event.wait(timeout=delay)` is used instead of `time.sleep(delay)`. This ensures the retry loop exits cleanly if the backend shuts down mid-warmup. Correct pattern.

### 4.3 No `_push_error` on warmup_sync exhaustion (low)

After all retries are exhausted, `logger.warning(...)` is called but no error is pushed to `_error_bus`. The user sees no toast. `warmup_probe` (called by `warmup`) does push `rewriter.warmup_timeout` on `requests.Timeout` exceptions, so network-level timeouts are covered. But a non-exception HTTP failure (e.g., 503 every time) would only log, not surface to the UI.

**Recommendation:** Push `rewriter.warmup_timeout` (or a new `rewriter.warmup_exhausted` code) at the end of `warmup_sync` when all retries are exhausted.

---

## 5. Error Bus Coverage

### 5.1 All HTTP failure paths push to error_bus — correct

Every `return LLMRewriteResult(ok=False, ...)` in `_rewrite_impl` is preceded by a `_push_error()` call **except** for the three cases identified above (output_too_short, output_too_long, chatbot_response). The `_push_error` implementation itself is well guarded — it catches all exceptions and routes to Sentry if the error_bus push itself fails (lines 380–387).

### 5.2 `fix_punctuation_only` never pushes to error_bus

`fix_punctuation_only` returns `None` on failure without any error_bus notification. This is acceptable because the method is a best-effort enhancement (callers treat `None` as "use original text"), and noisy toasts for punctuation-only mode would be distracting. The contract is documented.

### 5.3 `summarize` never pushes to error_bus

Same reasoning applies as `fix_punctuation_only`. `summarize` failures are silent to the user, which is appropriate since it is an optional enhancement.

### 5.4 `_push_error` late-injection pattern is fragile (low)

```python
error_bus = getattr(self, "_error_bus", None)
```
`_error_bus` is injected post-construction (attribute set externally). If `LLMRewriter` is constructed and used before `_error_bus` is injected, all errors are silently dropped. The CLAUDE.md notes this pattern is intentional. It works but introduces a temporal coupling that could cause silent failures during startup if the injection order is wrong. A constructor parameter with `Optional[ErrorBus] = None` default would be safer.

---

## 6. `RewriterFallbackChain` Issues

### 6.1 `_call_fallback` mutates primary rewriter's `_model` and `_circuit` under a lock (design gap, low)

**Location:** lines 1377–1400  
```python
with self._lock:
    original_model = self._primary._model
    original_circuit = self._primary._circuit
    self._primary._model = model
    self._primary._circuit = breaker
    try:
        result = self._primary.rewrite(text)
    ...
    finally:
        self._primary._model = original_model
        self._primary._circuit = original_circuit
```
This temporarily mutates the primary rewriter's identity. If any other thread calls `self._primary.status()` or `self._primary.rewrite()` concurrently (e.g., keepalive thread), it may observe the wrong `_model` or `_circuit`. The `self._lock` protects the mutation but `_primary.rewrite()` also acquires `_post_lock` internally — no deadlock risk since they are different locks. However, the keepalive thread calls `warmup_probe()` which accesses `self._circuit` directly without going through `_call_fallback`'s `self._lock`.

**Recommendation:** Consider giving `_call_fallback` a cloned rewriter with the fallback model/circuit rather than mutating the primary's internal state.

### 6.2 Fallback breakers are seeded from primary's config at construction time only

If the primary rewriter's `circuit_fail_threshold` or `circuit_initial_reset_sec` is changed at runtime (e.g., via settings update), fallback breakers retain stale values. Given the current codebase does not expose a settings path to change breaker parameters at runtime, this is low risk.

---

## 7. Minor Issues

### 7.1 503 retry uses `time.sleep(10)` — blocks IPC thread

**Location:** lines 561–600  
The 503 retry path calls `time.sleep(10)` on the IPC handler thread. Since the IPC server in Krab Ear is single-threaded, this blocks all other IPC requests for 10 seconds. While 503 on LM Studio is transient and rare, a non-blocking approach (return circuit_open immediately and let the keepalive probe recover) would be preferable.

**Severity:** Low — rare path, LM Studio cold-load is the only realistic trigger.

### 7.2 `ping()` uses `f"{self._base_url}/models"` not `/api/v1/models`

**Location:** `ping()` line 1271  
`passive_health_check()` correctly uses `/api/v1/models` (with the `_re.sub` to strip `/v1` suffix). But `ping()` uses `f"{self._base_url}/models"` which, given `_base_url` ends with `/v1`, resolves to `/v1/models` — the wrong endpoint for LM Studio (Wave 68 identified this). `ping()` is documented as "used only at backend startup" but if LM Studio returns 404 on `/v1/models`, `ping()` returns `False` spuriously.

**Severity:** Low — `ping()` is startup-only and the failure is non-critical (logged, not circuit-counted).

---

## Issue Summary

| # | Severity | Location | Description |
|---|----------|----------|-------------|
| 1 | **Medium** | `warmup_probe` L1113 | Calls private `_transition_to` to force OPEN→CLOSED, bypasses public API |
| 2 | **Medium** | `_rewrite_impl` L817–843 | `output_too_short` / `output_too_long` do not call `record_failure()` |
| 3 | **Medium** | `_rewrite_impl` L806–809 | `chatbot_response` does not call `record_failure()` |
| 4 | Low | `CircuitBreaker` docstring | Thread-safety disclaimer is stale (keepalive thread exists) |
| 5 | Low | `warmup_sync` L1186 | No `_push_error` when all warmup retries exhausted |
| 6 | Low | `_rewrite_impl` L561 | `time.sleep(10)` on 503 blocks IPC thread for 10 seconds |
| 7 | Low | `ping()` L1271 | Uses `/v1/models` not `/api/v1/models` — wrong LM Studio endpoint |
| 8 | Low | `_push_error` L358 | `_error_bus` late-injection pattern; silent drop if not injected before first call |

**Total: 3 medium, 5 low.**

---

## Recommended Fixes (Priority Order)

1. **Issues 2 + 3**: Add `self._circuit.record_failure()` before returning in `output_too_short`, `output_too_long`, and `chatbot_response` branches. One-line fix each, high correctness impact.

2. **Issue 1**: Add `CircuitBreaker.force_close()` public method that calls `self._transition_to(CircuitState.CLOSED)`, and update `warmup_probe` to use it.

3. **Issue 5**: Push `rewriter.warmup_timeout` (severity `warn`) in `warmup_sync` when the retry loop is exhausted.

4. **Issue 7**: Update `ping()` to use `passive_health_check()` logic or at minimum strip the `/v1` suffix before appending `/api/v1/models`.

5. **Issues 4, 6, 8**: Documentation and low-risk architecture notes — address in a later cleanup wave.
