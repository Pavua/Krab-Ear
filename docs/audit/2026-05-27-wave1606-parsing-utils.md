# Audit: core/parsing_utils.py — First Pass (W1606)

**Date:** 2026-05-27  
**Wave:** W1606  
**Auditor:** sub-agent W1606  
**File:** `KrabEar/core/parsing_utils.py` (76 LOC)  
**Test file:** `KrabEar/tests/test_parsing_utils.py` (193 LOC, 18 test methods)  
**Status:** First-pass, never previously audited

---

## Overview

`safe_json_loads` / `safe_json_dumps` are shared helpers that wrap `json.loads` / `json.dumps` with graceful fallback to a configurable `default` value on error. They consolidate the repeated `try/except json.loads` pattern across the backend.

**Return-on-failure contract:** returns the caller-supplied `default` (which defaults to `None`) — never raises. Empty/falsy input also short-circuits to `default` before even attempting a parse.

**Active callers (6 modules):**

| Caller | Default supplied | Context supplied |
|--------|-----------------|-----------------|
| `state_store.py:125` | `None` (settings load) | `"settings.json"` |
| `state_store.py:1170` | implicit `None` (NDJSON iter) | no |
| `vocabulary_store.py:92` | `None` | `"vocabulary.json"` |
| `history_service.py:2486/2541` | `None` | `"backup_meta.json"` |
| `feature_flags.py:104` | `{}` | `"feature_flags.json"` |
| `call_session_store.py:341` | implicit `None` | no |

All 6 callers correctly check for `None`/falsy result before using the parsed value.

---

## Findings (5)

---

### F1 — LOW: No input size limit — potential DoS via massive JSON string

**Location:** `parsing_utils.py:45–52`

`safe_json_loads` accepts arbitrarily large input strings. While the IPC server (`ipc_server.py`) enforces `IPC_MAX_MESSAGE_BYTES` before calling `json.loads` directly, callers that read from disk (e.g., `state_store`, `vocabulary_store`, `feature_flags`) could theoretically receive an adversarially crafted multi-GB file and cause OOM or a prolonged parse stall in the main thread.

In the current deployment (local macOS app, no remote untrusted input) the risk is low. However, as the function is described as a shared utility and future callers may trust it to be safe, a missing size guard is a latent footgun.

**Recommendation:** Add an optional `max_bytes: int | None = None` parameter (e.g., default 64 MB for file contexts, `None` for NDJSON-line contexts) and raise/return `default` if `len(data) > max_bytes`.

---

### F2 — LOW: LLM output safety gap — `action_items_extractor.py` does not use `safe_json_loads`

**Location:** `backend/action_items_extractor.py:269`

The extractor calls `json.loads(json_str)` directly (not via `safe_json_loads`) when parsing LLM responses. While it wraps the call in a `try/except (json.JSONDecodeError, ValueError)` — which is structurally equivalent — it misses two common LLM output defects that `safe_json_loads` also does not handle:

1. **Trailing commas** — `json.loads` raises `JSONDecodeError`; caught fine.
2. **Markdown code-fence wrappers** — LLMs sometimes emit `` ```json\n{...}\n``` ``. The extractor's `text.find("{")` / `text.rfind("}")` boundary detection handles this partially (it extracts the brace-delimited JSON), but `safe_json_loads` has no such strip logic either.

The deeper issue is that **neither `safe_json_loads` nor any caller performs LLM-specific sanitization** (strip code fences, convert `//` comments, handle single-quoted keys). This is not currently causing production failures because the LLM prompt explicitly requests strict JSON output, but it is undefended depth.

**Recommendation:** For LLM-response parse sites, add a thin `_strip_code_fence(text: str) -> str` helper (regex `^\s*\`\`\`(?:json)?\s*` / `\s*\`\`\`\s*$`) before calling `safe_json_loads`. This could live in `parsing_utils.py` as an exported helper or in `llm_rewriter.py`.

---

### F3 — LOW: 48 raw `json.loads` call sites remain outside `safe_json_loads`

**Location:** 22 distinct backend/core files (identified via `grep -rn "json\.loads"`)

Representative uncovered sites include:

- `ipc_server.py:105` — IPC socket reader (already has `except Exception` catch-all but uses raw `json.loads`)
- `call_assist_service.py:46/77/107/134` — four VoiceGateway HTTP response parse sites using `json.loads(raw) if raw else {}`
- `auto_backup.py:75/138`, `obsidian_sync.py:455`, `speaker_manager.py:83/107`, `webhook_manager.py:166` — persistent-store loaders with bare `json.loads` that raise `JSONDecodeError` uncaught at their call site

The IPC server case is intentional (it wants to return an error response, not swallow the exception), but the persistent-store loaders should be migrated to `safe_json_loads` for consistency with the existing pattern.

**Recommendation:** Migrate persistent-store loaders (`auto_backup`, `obsidian_sync`, `speaker_manager`, `webhook_manager`, `paste_app_memory`, `recording_scheduler`) to `safe_json_loads` as a follow-up wave. IPC/network-level parse sites (where error propagation to the caller is desired) may keep raw `json.loads`.

---

### F4 — INFO: `safe_json_loads` warning log does not include a raw-text snippet

**Location:** `parsing_utils.py:51`

```python
logger.warning("JSON parse failed%s: %s", ctx, exc)
```

The warning includes the `JSONDecodeError` message (which contains the position and surrounding character) but does not include a truncated snippet of the original input. When diagnosing a corrupt NDJSON file in production logs, a 60-char prefix of the raw line is far more useful than just `"Expecting ',' delimiter: line 1 column 47 (char 46)"`.

The omission is intentional (privacy — callers could pass transcript text), but for file-path contexts like `"settings.json"` or `"vocabulary.json"`, a brief snippet would be safe and diagnostic.

**Recommendation:** Add an optional `log_snippet: bool = False` parameter. When `True`, append `repr(data[:80])` to the warning. Default stays `False` to preserve the privacy guarantee for IPC/transcript callers.

---

### F5 — INFO: `TypeVar T` generic annotation is misleading at runtime

**Location:** `parsing_utils.py:14–22`

```python
T = TypeVar("T")

def safe_json_loads(
    data: "str | bytes",
    default: T = None,
    *,
    context: str = "",
) -> "Any | T":
```

The `TypeVar T` is used to propagate the `default` type to the return type, which is the right intent. However:

1. The return annotation `"Any | T"` resolves to `Any` under static analysis (mypy/pyright), because `Any` subsumes `T`. The correct annotation that makes type narrowing work is `T | Any`, written as an overload: one overload for `default: None` returning `Any | None`, another for `default: T` returning `Any | T`.
2. Using a bare `TypeVar` with a non-`None` default value (`default: T = None`) violates PEP 696 semantics (Python 3.13+ `TypeVar` with defaults); on earlier Python the type checker simply accepts `T = TypeVar("T")` as unconstrained, making the narrowing no-op in practice.

This is a type-annotation quality issue, not a runtime bug. The function behaves correctly.

**Recommendation:** Replace the `TypeVar` approach with `@overload` stubs to give callers meaningful type narrowing:
```python
@overload
def safe_json_loads(data: str | bytes, default: None = ..., *, context: str = ...) -> Any: ...
@overload
def safe_json_loads(data: str | bytes, default: T, *, context: str = ...) -> Any | T: ...
```

---

## Test coverage assessment

The existing test file covers:
- Happy path: dict, list, bytes, string scalar
- Failure: invalid JSON, empty string/bytes, `None` input
- Logging: context label in warning, warning emitted without context
- Concurrency: 50-thread parallel parse
- Unicode/Cyrillic round-trip
- `safe_json_dumps` serialization + failure path

**Not covered:**
- `safe_json_loads` with very large input (F1 gap)
- LLM-style malformed JSON (trailing commas, single quotes, code fences) — F2 gap
- `safe_json_dumps` with custom `ensure_ascii=True` flag (the function hardcodes `False`)

Overall coverage is good for a utility module. The missing cases align with the findings above.

---

## Summary table

| # | Severity | Title | Fix effort |
|---|----------|-------|-----------|
| F1 | LOW | No input size limit — potential DoS | 15 min |
| F2 | LOW | LLM output safety gap — no code-fence strip | 30 min |
| F3 | LOW | 48 raw `json.loads` sites not using utility | 2–3 h migration |
| F4 | INFO | Warning log lacks raw-text snippet | 10 min |
| F5 | INFO | `TypeVar` annotation misleading for type checkers | 20 min |
