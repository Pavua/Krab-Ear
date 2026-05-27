# W1504 — LLMRewriter third-pass audit (post W1482/W1486/W1489)

**Date**: 2026-05-27  
**Auditor**: W1504 sub-agent (Sonnet 4.6)  
**File**: `KrabEar/backend/llm_rewriter.py` (1485 lines), `KrabEar/core/engine.py`  
**Branch**: `codex/krab-ear-v2` (commit 4a43e5bb)

---

## Merge State Verification

| Wave | Description | Status | Evidence |
|------|-------------|--------|----------|
| W1482 | LLMRewriter second audit — 5 findings (F1 CRIT double record_failure, F2 MED privacy, F3 MED set_base_url, F4 LOW reset, F5 LOW chatbot ErrorBus) | **MERGED (docs only)** | PR #1366 / d578e0e6 |
| W1486 | Remove duplicate `record_failure()` in chatbot path (W1482 F1 CRIT) | **NOT MERGED** | commit ca46db9b exists but NOT on `codex/krab-ear-v2`; 3 tests still fail |
| W1489 | `_punctuation_pass_allowed()` privacy guard (W1482 F2 MED) | **NOT MERGED** | commit a3cdb11e exists but NOT on `codex/krab-ear-v2`; `engine.py` still missing guard |

**Confirmed failing tests** (run 2026-05-27):
```
FAILED test_llm_rewriter_chatbot_circuit_W1153.py::ChatbotRejectionRecordsFailureTestCase::test_chatbot_rejection_calls_record_failure
FAILED test_llm_rewriter_chatbot_circuit_W1153.py::ChatbotRejectionRecordsFailureTestCase::test_chatbot_rejection_russian_marker_calls_record_failure
FAILED test_llm_rewriter_chatbot_circuit_W1153.py::ChatbotCircuitOpensAfterThresholdTestCase::test_five_consecutive_chatbot_opens_circuit
```
All 3 failures trace to lines 824+826: two `record_failure()` calls per chatbot response.

---

## New Findings (5)

### N1 — CRIT: W1486 not merged — double `record_failure()` still active (W1482 F1 regression)

**File**: `KrabEar/backend/llm_rewriter.py` lines 824 and 826  
**Severity**: CRIT (W1486 fix commit exists but was never merged to `codex/krab-ear-v2`)

The duplicate `record_failure()` identified in W1482 F1 was addressed by W1486 (commit ca46db9b), but that commit was NOT merged into the main branch. The code at lines 822–826 still reads:

```python
self._circuit.record_failure()      # line 824 — W1153 addition
self._last_error = "chatbot_response"
self._circuit.record_failure()      # line 826 — original, should have been removed by W1486
```

**Impact**: Every chatbot detection increments `_consecutive_failures` by 2. With default `fail_threshold=3`, circuit opens after 2 chatbot responses instead of 3 (effective threshold halved). Three tests in `test_llm_rewriter_chatbot_circuit_W1153.py` actively fail.

**Fix**: Cherry-pick or re-apply W1486 (commit ca46db9b): delete the one `record_failure()` at line 826.

---

### N2 — MED: W1489 not merged — `_punctuation_pass_allowed()` ignores `privacy_mode_enabled` (W1482 F2 regression)

**File**: `KrabEar/core/engine.py` lines 490–495  
**Severity**: MED (W1489 fix commit exists but was never merged to `codex/krab-ear-v2`)

The privacy guard added by W1489 (commit a3cdb11e) in `engine.py::_punctuation_pass_allowed()` was NOT merged. The current code:

```python
def _punctuation_pass_allowed(self) -> bool:
    if self._llm_rewriter is None:
        return False
    return bool(self._settings_get("stt_punctuation_llm_pass_enabled", False))
    # MISSING: privacy_mode guard — transcript sent to LM Studio when privacy=True
```

When a user enables both `privacy_mode_enabled=True` and `stt_punctuation_llm_pass_enabled=True`, transcript text is sent to LM Studio via `fix_punctuation_only()` despite the privacy guarantee. The full `_llm_rewrite_allowed()` path correctly returns `False` when `privacy_mode_enabled`; only the punctuation path escapes.

**Fix**: Cherry-pick or re-apply W1489 (commit a3cdb11e): add `if self._settings_get("privacy_mode_enabled", False): return False` before the `stt_punctuation_llm_pass_enabled` check.

---

### N3 — MED: `summarize()` has no `privacy_mode_enabled` guard — transcripts sent to LM Studio in privacy mode

**File**: `KrabEar/backend/llm_rewriter.py` lines 999–1087  
**Severity**: MED (privacy bypass via a different path than F2/N2 — missed in W1482)

`rewrite()` is protected at the call site by `_llm_rewrite_allowed()` in `engine.py` (which checks `privacy_mode_enabled`). However `summarize()` is called directly by four services without any privacy check:

- `KrabEar/backend/history_service.py` lines 1576, 2180 — `auto_summarize_batch` and `export_obsidian`
- `KrabEar/backend/recording_core_service.py` line 1571 — `_generate_summary` in import transcription pipeline
- `KrabEar/backend/text_processing_service.py` line 113 — `_generate_summary` in `handle_summarize_item`

None of these callers check `privacy_mode_enabled` before calling `summarize()`. When privacy mode is on, `rewrite()` is correctly blocked, but `summarize()` will still send transcript text to LM Studio if called.

`summarize()` itself has no internal privacy guard — only a `circuit.allow_request()` check.

**Fix**: Either (a) add a privacy guard inside `summarize()` itself:
```python
def summarize(self, text: str, max_sentences: int = 3) -> LLMRewriteResult:
    feature_flags = getattr(self, "_feature_flags", None)
    # privacy guard — same pattern as rewrite()
    if feature_flags is not None:
        # caller should check privacy; but guard here as defence-in-depth
        pass
```
Or more cleanly (b) add `privacy_mode_enabled` checks in each caller service, consistent with the `_llm_rewrite_allowed()` pattern.

---

### N4 — LOW: `fix_punctuation_only()` and `summarize()` lack `feature_flags` guard — `llm_rewrite` flag disable ignored

**File**: `KrabEar/backend/llm_rewriter.py` lines 897–997 (`fix_punctuation_only`), 999–1087 (`summarize`)  
**Severity**: LOW (feature flag disable doesn't fully cut off LLM calls)

`rewrite()` has an explicit FeatureFlags check at line 440–448:
```python
if feature_flags is not None and not feature_flags.is_enabled("llm_rewrite"):
    return LLMRewriteResult(ok=False, text=text, fallback_reason="feature_flag_disabled", ...)
```

Neither `fix_punctuation_only()` nor `summarize()` perform this check. When a user or admin disables the `llm_rewrite` feature flag (e.g. for A/B testing or emergency kill-switch), `rewrite()` is correctly blocked but both alternate paths continue to make HTTP calls to LM Studio.

**Fix**: Add feature-flag guards to both methods at their entry points, mirroring the `rewrite()` pattern. For `fix_punctuation_only()`, return `None`; for `summarize()`, return `LLMRewriteResult(ok=False, fallback_reason="feature_flag_disabled")`.

---

### N5 — LOW: `RewriterFallbackChain._call_fallback()` pollutes primary rewriter state (`_last_latency_ms`, `_last_error`)

**File**: `KrabEar/backend/llm_rewriter.py` lines 1425–1448  
**Severity**: LOW (incorrect diagnostics/monitoring state after fallback use)

`_call_fallback()` temporarily swaps `_primary._model` and `_primary._circuit` to run a fallback model through the primary rewriter. After the call, it restores `_model` and `_circuit` in the `finally` block. However it does NOT save and restore `_primary._last_latency_ms` and `_primary._last_error`:

```python
with self._lock:
    original_model = self._primary._model
    original_circuit = self._primary._circuit
    self._primary._model = model
    self._primary._circuit = breaker
    # _last_latency_ms and _last_error NOT saved
    try:
        result = self._primary.rewrite(text)  # mutates _last_latency_ms, _last_error
    finally:
        self._primary._model = original_model
        self._primary._circuit = original_circuit
        # _last_latency_ms and _last_error NOT restored
```

After a fallback call, `self._primary.status()` returns `_last_latency_ms` and `_last_error` from the fallback model's HTTP call, not from the primary. The IPC `llm_status` method then reports stale/incorrect diagnostics to the frontend (e.g. user sees "last error: timeout from fallback-model" when primary was actually healthy).

**Fix**: Save and restore `_last_latency_ms` and `_last_error` in `_call_fallback()`:
```python
original_last_latency_ms = self._primary._last_latency_ms
original_last_error = self._primary._last_error
try:
    result = self._primary.rewrite(text)
finally:
    self._primary._model = original_model
    self._primary._circuit = original_circuit
    self._primary._last_latency_ms = original_last_latency_ms
    self._primary._last_error = original_last_error
```

---

## Summary

| Finding | Severity | File | Lines | Root Cause |
|---------|----------|------|-------|------------|
| N1: W1486 not merged — double record_failure still active | **CRIT** | `llm_rewriter.py` | 824, 826 | W1486 commit not cherry-picked to main |
| N2: W1489 not merged — `_punctuation_pass_allowed` no privacy guard | **MED** | `engine.py` | 490–495 | W1489 commit not cherry-picked to main |
| N3: `summarize()` no privacy guard — 4 callers bypass privacy mode | **MED** | `llm_rewriter.py` | 999–1087 | New gap, not covered by W1482/W1489 |
| N4: `fix_punctuation_only`/`summarize` ignore `llm_rewrite` feature flag | LOW | `llm_rewriter.py` | 897, 999 | Feature flag guard only in `rewrite()` |
| N5: `_call_fallback` state pollution (`_last_latency_ms`, `_last_error`) | LOW | `llm_rewriter.py` | 1425–1448 | save/restore incomplete |

**Merge state**: W1486 NOT merged (CRIT still active, 3 tests fail). W1489 NOT merged (MED privacy bypass still active).

**Recommended fix order**: N1 (unblock failing tests) → N2 + N3 (privacy chain complete) → N4 → N5.
