# LLMRewriter Audit — W1146

**Date**: 2026-05-26  
**Branch**: `fix/search-index-W1041` → audit worktree `audit/llm-rewriter-W1146`  
**File audited**: `KrabEar/backend/llm_rewriter.py`  
**Prior fixes in scope**: W826 (record_failure on ratio fallback), W866 (STT_GATEWAY_TOKEN), PR #396 (probe URL), PR #364 F2 (probe method GET)

---

## Summary

7 findings across: chatbot detection gaps, blocking sleeps in hot path, missing circuit-failure accounting, input size unbounded, CircuitBreaker thread-safety stale comment, `here is` false-positive risk, and `stt_gateway_token` absent from settings_backup sensitive list.

---

## Finding 1 — MEDIUM: `chatbot_response` and ratio-guard fallbacks do not call `record_failure`

**Location**: lines 758–815  
**Severity**: MEDIUM

When the LLM returns a chatbot-style response (`chatbot_response`) or a ratio-guard rejection (`output_too_short` / `output_too_long`), the code returns `ok=False` but does **not** call `self._circuit.record_failure()`. This means a model that persistently replies in assistant mode or produces malformed output will never trip the circuit breaker. Every transcript rewrite will block for the full HTTP round-trip (up to 45 s) before being rejected at the guard level, with no back-pressure.

By contrast, all genuine error paths (timeout, connection error, HTTP non-200, empty response, parse error, tool_calls_emitted) correctly call `record_failure`.

**Recommended fix**: add `self._circuit.record_failure()` before the `return` in both the chatbot-detection branch (line ~780) and both ratio-guard branches (lines ~800 and ~814).

---

## Finding 2 — MEDIUM: Blocking `time.sleep` calls in IPC hot path

**Location**: lines 548 and 601  
**Severity**: MEDIUM

Two retry paths include unconditional blocking sleeps:

- 503 JIT cold-load retry: `time.sleep(10)` — blocks the IPC handler thread for 10 seconds.
- Stream(gpu) Metal error retry: `time.sleep(2)` — blocks for 2 seconds.

The module's thread-safety comment (line 60) states "IPC server в Krab Ear однопоточный". Even in a single-IPC-thread model, a 10 s block prevents _all_ IPC methods from responding — including `handle_ping`, keyboard shortcuts, and UI updates — during LM Studio cold-load. The `_post_lock` is also held across the sleep because `time.sleep` is inside the `try:` block that runs before `with self._post_lock:`, but the lock itself is only inside the `with` — however the IPC thread blocking is still a UX degradation.

**Recommended fix**: Either move the retry to a background thread / asyncio task, or replace `time.sleep` with `self._shutdown_event.wait(timeout=10)` to at least allow clean shutdown during the pause.

---

## Finding 3 — MEDIUM: Chatbot marker `"here is"` causes Spanish false positives

**Location**: line 210, `_CHATBOT_MARKERS`  
**Severity**: MEDIUM

The marker `"here is"` will fire on valid Spanish transcript output starting with natural phrases like _"Herisson ..."_ or on any transcript that starts with the English phrase "here is" legitimately (e.g., dictated technical notes: "Here is the summary..."). More importantly, the system prompt instructs the model to preserve the input language — a Spanish transcript corrected by the model would never start with "here is" unless the model broke rule 5. The false-positive risk from common English dictation ("Here is my plan...") is real.

Additionally, several modern-model assistant phrases are missing from the list:
- `"certainly"` / `"certainly,"` — Gemma-4 and Qwen3 frequently begin refusals with this
- `"of course"` — common in instruction-tuned models
- `"understood"` — Qwen3 reasoning mode
- `"я понял"` / `"понял,"` — Russian instruction-tuned models

**Recommended fix**: replace `"here is"` with a more specific marker (`"here is the corrected"`, `"here is my"`) and add missing modern-model phrases to `_CHATBOT_MARKERS`.

---

## Finding 4 — LOW-MEDIUM: No input size cap — very long transcripts sent unbounded to LM Studio

**Location**: `_build_messages()` line 420–424, `_estimate_max_tokens()` line 408–418  
**Severity**: LOW-MEDIUM

`_rewrite_impl` sends `cleaned_input` directly to LM Studio without any character/word-count cap. `_estimate_max_tokens` caps *output* at 4096 tokens, but input is unlimited. A very long recording (10+ minute dictation = potentially 3000+ words, ~9000 tokens) will:

1. Exceed the context window of smaller models (qwen3-4b: 32 k, but gemma-4-e4b: 8 k effective), causing silent truncation or garbled output.
2. Trigger `output_too_short` ratio fallback (input 9000 chars → output 2000 chars after context-window truncation → ratio 0.22 < 0.35) — wasted 45 s round-trip.
3. Potentially leak large volumes of user speech content via a single outbound HTTP request.

**Recommended fix**: add a `MAX_INPUT_CHARS = 4000` (configurable) guard in `_rewrite_impl` before building the payload; log a warning and return the original text when exceeded, or split into chunks.

---

## Finding 5 — LOW: CircuitBreaker thread-safety comment is stale

**Location**: line 59–61  
**Severity**: LOW (documentation / future-risk)

```python
# Thread safety: не требуется — IPC server в Krab Ear однопоточный.
# Если появится multi-threaded access, обернуть в threading.Lock.
```

This comment is no longer accurate. `LLMRewriter` is constructed with `_post_lock = threading.Lock()` (line 273) and the idle keepalive runs on a daemon thread that calls `warmup_probe()` → `record_success()` via the circuit breaker. Meanwhile `_rewrite_impl` calls `record_failure()` / `record_success()` from the IPC thread. `set_model()` (line 1217) spawns another daemon thread calling `warmup()`. The `CircuitBreaker` itself has no lock, so concurrent `record_failure()` + `record_success()` calls from keepalive + IPC thread are a data race.

The race is low-probability (keepalive fires every 25 minutes) but the comment creates false confidence against future callers adding more concurrency.

**Recommended fix**: add `threading.Lock` to `CircuitBreaker.__init__` and wrap all state-mutating methods, or update the comment to accurately describe what is and is not thread-safe.

---

## Finding 6 — LOW: `stt_gateway_token` absent from `settings_backup._SENSITIVE`

**Location**: `KrabEar/backend/settings_backup.py` lines 27–41  
**Severity**: LOW

`settings_backup._SENSITIVE` correctly redacts `voice_gateway_api_key`, `hf_token`, `rest_api_key`, `lm_studio_api_key`, `telnyx_api_key`, `twilio_account_sid`, `twilio_auth_token`, `sentry_dsn`, `stt_gigaam_hf_token`. However, if a `stt_gateway_token` key is ever persisted to `settings.json` (mentioned in W866 context), it would not be redacted in backup files written to `settings_backups/`. Currently the field does not appear in `config.py` or `DEFAULT_SETTINGS`, but it should be pre-emptively added to `_SENSITIVE` as a defense-in-depth measure alongside the other API tokens.

**Recommended fix**: add `"stt_gateway_token"` to `_SENSITIVE` in `settings_backup.py`.

---

## Finding 7 — INFO: No HMAC / request signing on LM Studio calls; no per-method rate limiting for `rewrite`

**Location**: `_lm_studio_headers()` line 339–349  
**Severity**: INFO (by design for localhost)

LM Studio calls use Bearer token auth only (`Authorization: Bearer <key>`), with no HMAC request signing. `RequestSigner` (from `backend/request_signing.py`) is not wired to `LLMRewriter`. This is acceptable for a localhost-only endpoint but worth documenting: if `LLM_BASE_URL` is ever pointed to a remote endpoint (e.g., an internal network LM Studio server or a forwarded port), the Bearer token alone provides no replay protection.

Additionally, there is no `IPCThrottle` entry for `rewrite_transcript` or similar LLM-backed IPC methods. A rapid-fire sequence of IPC calls could trigger many concurrent `rewrite()` calls; the `_post_lock` serialises them but queues them up, potentially holding the IPC thread for `N × 45 s` if all queue up during a cold-load.

**Recommended fix**: document the localhost-only assumption in the module docstring. Consider adding `IPCThrottle` rate-limit for `rewrite_transcript` (e.g., 1 req/5 s) to prevent accidental queue build-up.

---

## What is Working Well

- **Never-raises contract**: all code paths return `LLMRewriteResult` without raising.
- **Bearer token auth**: correctly omitted when `api_key` is empty (backward compat with LM Studio < 0.3).
- **Connection pooling**: `requests.Session()` reuse reduces per-call latency.
- **Breadcrumb privacy**: Sentry breadcrumbs contain only metadata (chars count, circuit state, model) — no transcript text.
- **settings_backup redaction**: `lm_studio_api_key` correctly included in `_SENSITIVE`.
- **W826 fix confirmed**: both `output_too_short` and `output_too_long` paths include `_push_error` with severity `info`.
- **Idle keepalive**: uses `_shutdown_event.wait()` for clean stop — no bare `time.sleep`.
- **PR #396 / PR #364 fixes confirmed**: `passive_health_check` uses `GET /api/v1/models` (correct LM Studio path), probe does not trigger JIT reload.

---

## Files Changed

- `docs/audit/2026-05-26-wave1146-llm-rewriter.md` (this file, new)
