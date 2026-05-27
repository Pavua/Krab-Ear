# W1482 — LLMRewriter second audit (post W826/W866/W1146/W1153/W1154/W1239)

**Date**: 2026-05-27  
**Auditor**: W1482 sub-agent (Sonnet 4.6)  
**File**: `KrabEar/backend/llm_rewriter.py` (1478 lines)  
**Branch**: `codex/krab-ear-v2` (commit f7086279)

---

## Merge State Verification

| Wave | Description | Status | Commit/PR |
|------|-------------|--------|-----------|
| W826 | CircuitBreaker `record_failure()` on guard rejects (output_too_short/long) + ping URL fix | **MERGED** | PR #748 / c78995ea |
| W866 | Replace hardcoded `"Bearer token_here"` with env-based token (W858 HIGH) | **MERGED** | PR #788 / 0b4e89eb |
| W1146 | LLMRewriter audit docs — 7 findings (circuit/chatbot/sleep/input-cap/race) | **MERGED** (docs only) | PR #1056 / 86951ad6 |
| W1153 | Chatbot rejection `record_failure()` — W1146 F1 MED | **MERGED** | PR #1062 / 64040a64 |
| W1154 | `shutdown_event.wait()` replaces `time.sleep()` — W1146 F2 MED | **MERGED** | PR #1063 / a045d306 |
| W1239 | `_on_settings_saved` hot-propagates `llm_model` + `llm_base_url` (W1229 F2 MED) | **NOT MERGED** | exists only in orphan commit 7ef7e3dc |

---

## New Findings (5)

### F1 — CRIT: Double `record_failure()` in chatbot detection path

**File**: `KrabEar/backend/llm_rewriter.py` lines 818 and 820  
**Severity**: CRIT (breaks 3 existing tests, causes premature circuit opens)

W1153 (PR #1062) added `self._circuit.record_failure()` at line 818 to fix the missing failure recording for chatbot responses. However, the original code already had `record_failure()` at line 820 — the fix inserted a NEW call before the existing one instead of deduplicating it. Result: every chatbot response increments `_consecutive_failures` by 2 instead of 1.

**Impact**:
- With default `fail_threshold=3`, circuit opens after 2 chatbot responses (not 3).
- 3 existing tests fail (`test_llm_rewriter_chatbot_circuit_W1153.py`):
  - `test_chatbot_rejection_calls_record_failure` — expects `before+1`, gets `before+2`
  - `test_chatbot_rejection_russian_marker_records_failure` — same
  - `test_five_consecutive_chatbot_opens_circuit` — circuit opens on 3rd chatbot response (at `i=2`), so 4th and 5th calls return `circuit_open` instead of `chatbot_response`

**Fix**: Remove the duplicate `record_failure()` at line 820.

```python
# BROKEN (current):
self._circuit.record_failure()      # line 818 — W1153 addition
self._last_error = "chatbot_response"
self._circuit.record_failure()      # line 820 — original duplicate, should be removed

# CORRECT:
self._circuit.record_failure()
self._last_error = "chatbot_response"
```

---

### F2 — MED: `_punctuation_pass_allowed()` ignores `privacy_mode_enabled`

**File**: `KrabEar/core/engine.py` lines 474-478  
**Severity**: MED (privacy regression — text sent to LM Studio when privacy mode is on)

`_llm_rewrite_allowed()` correctly returns `False` when `privacy_mode_enabled=True` (documented in W1229 F3 MED). However `_punctuation_pass_allowed()`, which gates the `fix_punctuation_only()` call at line 1084, does NOT check privacy mode:

```python
def _punctuation_pass_allowed(self) -> bool:
    if self._llm_rewriter is None:
        return False
    return bool(self._settings_get("stt_punctuation_llm_pass_enabled", False))
    # MISSING: if self._settings_get("privacy_mode_enabled", False): return False
```

When a user enables both `privacy_mode_enabled=True` and `stt_punctuation_llm_pass_enabled=True`, transcript text is sent to LM Studio via `fix_punctuation_only()` in violation of the privacy guarantee. The full rewrite path is correctly blocked; only the punctuation pass escapes.

**Test coverage gap**: `test_llm_privacy_mode_W1240.py` tests `_llm_rewrite_allowed` but has no test case for `_punctuation_pass_allowed` with `privacy_mode_enabled`.

**Fix**: Add privacy guard to `_punctuation_pass_allowed()`:

```python
def _punctuation_pass_allowed(self) -> bool:
    if self._llm_rewriter is None:
        return False
    if self._settings_get("privacy_mode_enabled", False):
        return False
    return bool(self._settings_get("stt_punctuation_llm_pass_enabled", False))
```

---

### F3 — MED: W1239 not merged — `set_base_url()` and `llm_base_url` hot-propagation absent

**File**: `KrabEar/backend/llm_rewriter.py`, `KrabEar/backend/service.py`  
**Severity**: MED (user UX — changing `llm_base_url` in settings requires backend restart to take effect)

W1239 (commit 7ef7e3dc, not merged) added:
1. `LLMRewriter.set_base_url()` method — mirrors `set_model()` pattern: normalises trailing slash, resets circuit breaker, spawns background warmup.
2. Extension of `_on_settings_saved` hook in `BackendService.__init__` to detect `llm_base_url` changes and call `_rewriter_ref.set_base_url(new_url)` without requiring a restart.

Current state: `_on_settings_saved` hook (service.py line 234) only hot-propagates `lm_studio_api_key` changes. `llm_model` changes (via `set_model()`) are also not hot-propagated by the hook, though `set_model()` itself exists and is callable manually. Changing the LM Studio endpoint URL (`llm_base_url`) requires a full backend restart.

**Fix**: Cherry-pick or re-apply W1239 diff into `codex/krab-ear-v2`.

---

### F4 — LOW: `warmup_probe` uses `_circuit._transition_to()` private method — bypasses OPEN→CLOSED invariant

**File**: `KrabEar/backend/llm_rewriter.py` line 1141  
**Severity**: LOW (minor correctness issue in warmup-forced CLOSED path)

In `warmup_probe()`, after a successful warmup while circuit state is `"open"`:

```python
self._circuit.record_success()  # HALF_OPEN → CLOSED if applicable
# Force CLOSED if still OPEN (record_success only transitions from HALF_OPEN)
if self._circuit.state == "open":
    self._circuit._transition_to(CircuitState.CLOSED)   # line 1141
```

`_transition_to(CircuitState.CLOSED)` correctly resets `_opened_at` and `_consecutive_failures` (see `_transition_to()` at line 137-145). However, calling a private `_transition_to` directly bypasses the public API contract and creates a brittle coupling between `warmup_probe` and `CircuitBreaker` internals. If `_transition_to` is ever extended (e.g., to emit metrics on state change), `warmup_probe` will silently skip it.

**Fix**: Add a public `force_close()` method on `CircuitBreaker` (or simply call `_transition_to` via a new public `reset()` method):

```python
def reset(self) -> None:
    """Force-close the circuit — used after explicit warmup success."""
    self._transition_to(CircuitState.CLOSED)
    self._half_open_probe_in_flight = False
```

---

### F5 — LOW: Chatbot rejection path does not call `_push_error()` — silent in ErrorBus

**File**: `KrabEar/backend/llm_rewriter.py` lines 810-835  
**Severity**: LOW (monitoring gap — chatbot mode switches are invisible in error bus/Sentry)

Every other guard in `_rewrite_impl()` that returns `ok=False` calls `_push_error()`:
- `empty_response` → `_push_error("rewriter.empty_response", ...)`
- `tool_calls_emitted` → `_push_error("rewriter.tool_calls_emitted", ...)`
- `parse_error` → `_push_error("rewriter.parse_error", ...)`
- `output_too_short/long` → `_push_error("rewriter.output_ratio_fallback", ...)`

The chatbot detection path (step 8) does NOT call `_push_error()` — only `logger.warning()`. This means persistent chatbot mode (model stuck in assistant persona) is invisible to the ErrorBus/Sentry pipeline and won't surface as a user-facing toast or Sentry issue.

There is no `"rewriter.chatbot_response"` code in `ERROR_REGISTRY` (`error_codes.py`).

**Fix**:
1. Add `"rewriter.chatbot_response"` to `ERROR_REGISTRY`.
2. Add `self._push_error("rewriter.chatbot_response", f"chatbot marker='{marker}'")` in the chatbot detection branch (after the single correct `record_failure()`, once F1 is fixed).

---

## Summary

| Finding | File | Lines | Severity | Fix scope |
|---------|------|-------|----------|-----------|
| F1: Double `record_failure()` chatbot | `llm_rewriter.py` | 820 | **CRIT** | 1-line deletion |
| F2: `_punctuation_pass_allowed` ignores privacy mode | `engine.py` | 474-478 | **MED** | 2-line addition |
| F3: W1239 not merged (set_base_url + hot-propagate llm_base_url) | `llm_rewriter.py`, `service.py` | — | **MED** | cherry-pick W1239 |
| F4: `warmup_probe` uses `_circuit._transition_to()` private | `llm_rewriter.py` | 1141 | LOW | add `reset()` public method |
| F5: Chatbot path missing `_push_error()` | `llm_rewriter.py` | 810-835 | LOW | add error code + push |

**Merge state**: W826 MERGED, W866 MERGED, W1146 MERGED (docs), W1153 MERGED (but introduced F1 regression), W1154 MERGED, W1239 NOT MERGED.

**Failing tests**: 3 tests in `KrabEar/tests/test_llm_rewriter_chatbot_circuit_W1153.py` fail due to F1 double record_failure.
