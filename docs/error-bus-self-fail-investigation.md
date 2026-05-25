# error_bus.push Self-Failing Investigation (Wave 206)

**Reported:** Wave 202 PR #550 bonus finding  
**Investigated:** 2026-05-19  
**Status:** Root cause CONFIRMED AND FIXED (PR #376, 2026-05-05 21:05 UTC+2)

---

## Summary

21 occurrences of `error_bus.push failed for code=rewriter.timeout` logged between
19:19 and 23:47 on 2026-05-05. The exception was swallowed by the `except Exception`
guard in `LLMRewriter._push_error()`, keeping the backend alive but leaving Sentry blind
to the failures.

**One root cause confirmed. Already fixed. Zero recurrences after 2026-05-05.**

---

## Hypotheses (ranked by evidence)

### H1: `_dedupe_window_for()` returned a dict instead of float — CONFIRMED ROOT CAUSE

**Probability:** 10/10 — direct stack trace in log, reproducible locally.

**Mechanism:**  
Before PR #376, `ErrorBus._dedupe_window_for()` was a one-liner:

```python
# BEFORE (broken)
def _dedupe_window_for(self, code: str) -> float:
    return self._registry.get(code, self._default_dedupe_window_sec)
```

`ERROR_REGISTRY["rewriter.timeout"]` is a full `_Entry` TypedDict:
```python
{
    "user_msg_ru": "Rewriter недоступен — raw text вставлен",
    "actionable": True, "action_id": "disable_rewriter",
    "severity": "warn", "dedupe_seconds": 60,
}
```

So `_dedupe_window_for("rewriter.timeout")` returned the entire dict.
Then inside `push()`:
```python
if last is not None and (now - last) < window:  # window is a dict!
# TypeError: '<' not supported between instances of 'float' and 'dict'
```

**Confirmed from log stack trace (19:19:41 occurrence):**
```
File ".../error_bus.py", line 171, in push
    if last is not None and (now - last) < window:
TypeError: '<' not supported between instances of 'float' and 'dict'
```

**Fix (PR #376):**
```python
# AFTER (fixed)
def _dedupe_window_for(self, code: str) -> float:
    entry = self._registry.get(code)
    if entry is None:
        return self._default_dedupe_window_sec
    if isinstance(entry, dict):
        value = entry.get("dedupe_seconds", self._default_dedupe_window_sec)
        return float(value)
    return float(entry)
```

**Regression test added:** `test_push_dedupe_with_canonical_error_registry_entry` in
`KrabEar/tests/test_error_bus.py` (committed alongside the fix).

---

### H2: Sentry SDK network timeout propagating through push() — SECONDARY, HISTORICAL ARTIFACT

**Probability:** The 19:19:39 and all 23:46+ traces show a CHAINED exception:
LM Studio HTTP `ReadTimeoutError` (port 1234) → `During handling... another exception occurred`
→ `_push_error` → `error_bus.push` → `TypeError`.

The urllib3/Sentry stack frames appear because the Sentry stdlib integration **monkey-patches
`http.client.HTTPConnection.getresponse`** to record breadcrumbs. When LM Studio's HTTP
request times out, the patched path is in the traceback — but the raised exception is still
`ReadTimeoutError` from `_rewrite_impl`, not from Sentry's own network call.

**The TypeError (H1) was always the final exception.** The Sentry patched frames are
noise from Python's exception chaining (`During handling of the above exception`).

**Current code status:** `_route_to_sentry()` calls `sentry_sdk.capture_message()` which
uses a **background worker thread** for HTTP dispatch — network errors do not propagate
synchronously into `push()`. This path is safe post-fix.

---

### H3: Pydantic validation failure in `KrabError()` constructor — NOT TRIGGERED

`context={"model": self._model, "base_url": self._base_url}` — both are strings set at
`LLMRewriter.__init__()`. `KrabError.model_dump(mode="json")` handles str dicts natively.
`Component` literal includes `"rewriter"`. Severity `"warn"` is in `Severity` literal.
No validation path can fail for these inputs. Confirmed by local test.

---

### H4: WarnBatcher buffer overflow or lock contention — NOT TRIGGERED

`WarnBatcher` uses its own independent `threading.Lock()`. Deadlock with `ErrorBus._lock`
impossible (no nested acquisition). Buffer is unbounded per-code list. No evidence in logs.

---

### H5: JSON serialization of non-serializable context values — NOT TRIGGERED

Context dict only ever contains `str` values for `rewriter.timeout` pushes.
`model_dump(mode="json")` converts bytes/other types gracefully in Pydantic v2.

---

## Actual log evidence

| Time (2026-05-05) | Exception | Notes |
|---|---|---|
| 19:19:39 | TypeError (dict vs float) | Chained from LM Studio ReadTimeout |
| 19:19:41 | TypeError (dict vs float) | Standalone |
| 19:20–24 | TypeError (dict vs float) | 9 more occurrences |
| 23:46:03–47:57 | TypeError (dict vs float) | 11 occurrences; backend not yet restarted with fix |
| **After 2026-05-05** | **None** | Fix active after backend restart |

**Total: 21 TypeError occurrences.** All on `rewriter.timeout` code. None after PR #376.

---

## Remaining Risk: Silent Sentry Blind-Spot

Even though the root cause is fixed, the `_push_error` pattern has a structural weakness:

```python
except Exception:  # never raise from rewriter
    logger.exception("error_bus.push failed for code=%s", code)
```

This logs to file but **does not send to Sentry**. Future `push()` failures (from any new
bug) will be invisible to Sentry until someone reads the log file.

### Recommended Fix (scope: small, additive)

Add a `capture_exception` fallback inside the `except` block:

```python
except Exception:  # never raise from rewriter
    logger.exception("error_bus.push failed for code=%s", code)
    # Ensure Sentry sees push() failures even when error_bus itself is broken
    try:
        from backend.observability import capture_exception
        capture_exception()
    except Exception:
        pass  # absolute last resort
```

This closes the blind spot with no risk of raising.

---

## Recommended Test Plan

1. **Regression test already exists:** `test_push_dedupe_with_canonical_error_registry_entry`
   in `KrabEar/tests/test_error_bus.py` directly covers H1. Passes post-fix.

2. **New test: Sentry blind-spot guard** — add to `KrabEar/tests/test_error_bus.py`:
   ```python
   def test_push_failure_does_not_raise_from_push_error(self):
       """If error_bus.push raises, _push_error must catch and log, not propagate."""
       rewriter = LLMRewriter(...)
       bus = MagicMock()
       bus.push.side_effect = RuntimeError("bus broken")
       rewriter._error_bus = bus
       # Must not raise
       rewriter._push_error("rewriter.timeout", "test message")
       bus.push.assert_called_once()
   ```
   (Equivalent test already exists in `test_vocabulary_store_errors.py` for VocabularyStore —
   port the pattern to llm_rewriter.)

3. **New test: _dedupe_window_for with full ERROR_REGISTRY** — verify no TypeError
   for every code currently in `ERROR_REGISTRY`. Prevents regression when new codes are added.

---

## Conclusion

The Wave 202 bonus finding is a **closed issue** (fixed by PR #376 on 2026-05-05).
The sole root cause was `_dedupe_window_for()` returning the raw `_Entry` dict from
`ERROR_REGISTRY` instead of extracting the `dedupe_seconds` float, causing a `TypeError`
in the `(now - last) < window` comparison inside `push()`.

The fix is already in production. Recommended follow-up: add Sentry fallback in the
`except Exception` guard to eliminate the blind-spot for any future `push()` failures.
