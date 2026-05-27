# W1529 — LLMRewriter regression audit (post W1497 cherry-pick)

**Date:** 2026-05-27  
**Scope:** `KrabEar/backend/llm_rewriter.py` + `KrabEar/core/engine.py`  
**Branch:** `codex/krab-ear-v2` @ `98d0d679`  
**Trigger:** Verify that W1497 cherry-pick did not revert prior wave fixes.

---

## Methodology

For each referenced fix, checked:
1. `git merge-base --is-ancestor <fix-commit> codex/krab-ear-v2`
2. Presence of the expected code pattern (grep / line read)

---

## Wave status table

| Wave | Fix description | Commit | In branch? | Code verified |
|------|----------------|--------|-----------|---------------|
| W826 | `record_failure` on guard rejects + ping URL fix | `c78995ea` | YES | YES — record_failure present in all guard paths |
| W866 | Env-based Bearer token via `_lm_studio_headers()` | `0b4e89eb` | YES | YES — conditional `Authorization: Bearer` header |
| W1146/W1153/W1154 | chatbot `record_failure` + `shutdown_event.wait` | `088d697a` | YES | YES — both patterns present |
| W1239 | `set_base_url` hot-propagate via `_on_settings_saved` | `bbfcbaa9` | YES | YES — `set_base_url()` exists, spawns background warmup |
| W1486 | Remove duplicate `record_failure` in chatbot path (CRIT) | `4e9210a9` | YES | YES — single `record_failure` at line 827 |
| W1489 | `_punctuation_pass_allowed` privacy-mode guard (MED) | `a3cdb11e` | **NO** | **MISSING** — `engine.py` lacks guard |
| W1512 | `summarize()` + `fix_punctuation_only()` privacy guard | `aa969f21` | YES | YES — W1504 N3+N4 guards present |

---

## Findings (cap 5)

### F1 — W1489 NOT merged — CRIT regression in `engine.py` (MED)

**Severity:** MED  
**File:** `KrabEar/core/engine.py:457`  
**Status:** REGRESSION (commit `a3cdb11e` not an ancestor of `codex/krab-ear-v2`)

`_punctuation_pass_allowed()` in `engine.py` has no `privacy_mode_enabled` guard:

```python
def _punctuation_pass_allowed(self) -> bool:
    """Runtime check: включён ли punctuation-only LLM pass."""
    if self._llm_rewriter is None:
        return False
    return bool(self._settings_get("stt_punctuation_llm_pass_enabled", False))
```

The W1489 fix added an early return when `privacy_mode_enabled=True`. That commit
modified `engine.py` and a test file but was never merged into `codex/krab-ear-v2`.
As a result, `fix_punctuation_only()` can be called from `engine.py` even when the
user has enabled privacy mode — exposing transcript text to LM Studio in violation of
the privacy contract. The `fix_punctuation_only()` method in `llm_rewriter.py` itself
does check `_settings_getter` (W1504 N4, line 915–919), but that guard is bypassed
because the call-site gate in `engine.py` never fires the short-circuit.

**Fix:** Cherry-pick or re-apply `a3cdb11e` (adds 6 lines to `_punctuation_pass_allowed`
and a 3-test file `test_punctuation_pass_privacy_W1489.py`).

---

### F2 — `rewrite()` itself has no privacy-mode guard (W1504 N1 carry-over)

**Severity:** MED  
**File:** `KrabEar/backend/llm_rewriter.py:433`

`rewrite()` and `_rewrite_impl()` have no `privacy_mode_enabled` short-circuit.
`fix_punctuation_only()` (line 915) and `summarize()` (line 1015) were fixed in W1512,
but the main `rewrite()` path still sends transcript text to LM Studio when privacy mode
is enabled. The W1504 audit documented this as N1 but no fix has been merged.

Four call sites in `core/engine.py` call `self._llm_rewriter.rewrite(text)` without
a prior privacy check (other than the `_is_llm_rewrite_enabled()` flag which is a
separate setting from `privacy_mode_enabled`).

**Fix:** Add the same `_settings_getter("privacy_mode_enabled", False)` early-return
pattern to `rewrite()` or `_rewrite_impl()` (lines 433–453).

---

### F3 — `RewriterFallbackChain._call_fallback` mutates `_last_latency_ms` / `_last_error` (W1504 N5 carry-over)

**Severity:** LOW  
**File:** `KrabEar/backend/llm_rewriter.py:1468`

`_call_fallback` swaps `_model` and `_circuit` on the primary rewriter but does NOT
save/restore `_last_latency_ms` and `_last_error`. After a fallback call, these
attributes reflect the fallback model's result, not the primary's. This pollutes the
`status()` IPC response and any Sentry context that reads `_last_error`.

```python
# current — missing save/restore:
original_model = self._primary._model
original_circuit = self._primary._circuit
# _last_latency_ms and _last_error not saved
```

**Fix:** Save and restore `_last_latency_ms` and `_last_error` around the fallback call.

---

## All-clear waves

The following waves were verified present and correct in `codex/krab-ear-v2`:

- **W826** — `record_failure` fires in all four guard-reject paths (circuit_open, tool_calls_emitted, empty_response, ratio guards). Ping URL (`/api/v1/models`) correct.
- **W866** — `_lm_studio_headers()` conditionally adds `Authorization: Bearer <token>` only when `api_key` is non-empty. `_lm_studio_get_headers()` mirrors this. `set_api_key()` exists and resets circuit.
- **W1146 F1 (W1153/W1164)** — single `record_failure()` at line 827 in chatbot path; `_last_error = "chatbot_response"` set correctly. No duplicate call present (W1486 CRIT fix confirmed effective).
- **W1146 F2 (W1154)** — `_shutdown_event.wait(timeout=10.0)` replaces `time.sleep(10)` in 503 retry (line 584); `_shutdown_event.wait(timeout=2.0)` in Stream(gpu) retry (line 641); keepalive loop uses `_shutdown_event.wait` (line 335).
- **W1239** — `set_base_url()` exists (line 1320), normalises trailing slash, resets circuit, logs, spawns `threading.Thread(target=self.warmup, daemon=True)`.
- **W1486** — chatbot detection block (lines 815–843) contains exactly ONE `record_failure()` call at line 827. The stale duplicate that W1153 introduced (the `_consecutive_failures` double-count CRIT) is gone.
- **W1512** — `fix_punctuation_only()` has `_settings_getter("privacy_mode_enabled")` guard at line 915–920; `summarize()` has the same guard at line 1015–1021.

---

## Summary

1 regression confirmed: **W1489** (`engine.py:_punctuation_pass_allowed` privacy guard) is NOT in `codex/krab-ear-v2`.  
2 carry-over open findings from W1504: **N1** (rewrite privacy guard) and **N5** (FallbackChain state pollution).  
No other waves regressed. W1486 CRIT (double record_failure) is fully in place.
