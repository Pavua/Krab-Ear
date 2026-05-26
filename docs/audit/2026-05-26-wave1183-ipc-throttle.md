# Audit W1183: IPCThrottle — per-method rate limiting

**Branch:** `audit/ipc-throttle-W1183`
**Date:** 2026-05-26
**File audited:** `KrabEar/backend/ipc_throttle.py`
**Related:** `KrabEar/backend/service.py` (dispatch integration), `KrabEar/tests/test_ipc_throttle.py`, `KrabEar/tests/test_ipc_throttle_extras.py`

---

## Summary

5 findings. Token bucket math is correct and integration into `handle_request` is properly placed (before handler dispatch). Observability and test coverage are solid. The main gap is coverage drift: 7+ expensive handlers added after the throttle config was written now fall through to the `light` tier (120/min), providing no meaningful protection.

---

## Finding 1 — MEDIUM: Coverage drift — 7 expensive handlers missing from HEAVY/MEDIUM sets

**Severity:** Medium  
**File:** `KrabEar/backend/ipc_throttle.py`, lines 27–73

Seven dispatched IPC methods that perform CPU/memory-intensive operations are absent from both `HEAVY_METHODS` and `MEDIUM_METHODS`. They fall through to the default `light` classification (120 calls/minute), which provides no meaningful rate protection.

Confirmed missing from throttle config (actual dispatch table names from `service.py`):

| Method | Operation | Falls to |
|---|---|---|
| `semantic_search` | Embedding index scan — O(N) over full history via multilingual-e5-base | `light` (120/min) |
| `semantic_search_reindex` | Full re-embed of ALL history items — most expensive single IPC call | `light` (120/min) |
| `export_html_report` | Full analytics HTML report over entire history | `light` (120/min) |
| `generate_html_report` | Alias for `export_html_report` wired in dispatch table (line 973) | `light` (120/min) |
| `get_timeline_view` | Full history scan + temporal grouping | `light` (120/min) |
| `generate_stats_report` | Full Markdown statistics report over history | `light` (120/min) |
| `get_sentiment_trends` | Linear regression over daily sentiment aggregates | `light` (120/min) |

Note: `export_obsidian`, `batch_export`, and other exports ARE in `HEAVY_METHODS`, making the omission of `export_html_report` / `generate_html_report` an inconsistency.

**Fix:** Add the seven methods to appropriate sets:
```python
# HEAVY_METHODS additions:
"semantic_search_reindex",   # most expensive — full re-embed
"export_html_report",        # heavy analytics report
"generate_html_report",      # alias — same cost

# MEDIUM_METHODS additions:
"semantic_search",           # embedding scan — expensive but not batch
"get_timeline_view",         # history scan + grouping
"generate_stats_report",     # markdown report
"get_sentiment_trends",      # regression analysis
```

---

## Finding 2 — LOW: `reset_stats()` does NOT reset token buckets — misleading name

**Severity:** Low  
**File:** `KrabEar/backend/ipc_throttle.py`, lines 280–286

`reset_stats()` clears call counters (`_call_counts`, `_throttled_counts`, totals) but leaves `self._buckets` intact. A caller who calls `reset_stats()` after a throttling episode expecting to get a "clean slate" will find the method still throttled — the token bucket is still drained.

The existing test `test_reset_stats_does_not_clear_buckets_refill` explicitly verifies this behavior, but the method name `reset_stats` does not communicate the intentional asymmetry. The docstring says only "Сбрасывает статистику (полезно для тестов)" — a reader could reasonably infer it resets throttle state.

**Fix:** Either rename to `reset_counters()` / `clear_stats()`, or add an explicit note in the docstring: "Бакеты (token buckets) не сбрасываются — только счётчики."

---

## Finding 3 — LOW: No per-client isolation — single shared bucket per method

**Severity:** Low  
**File:** `KrabEar/backend/ipc_throttle.py`, lines 183–232

`IPCThrottle` maintains one `_TokenBucket` per method name, shared across all concurrent IPC connections. The IPC server spawns a thread per connection (`_handle_connection` is called per-socket in service.py line 3667), but there is no per-connection or per-client-identity segregation in `IPCThrottle`.

In the current single-user local deployment (only the Swift agent connects), this is not a real-world risk. However, if a second client connects (e.g., a test harness running alongside the production agent, the REST server, or a future multi-tenant scenario), one client's burst will starve the other client even for non-malicious access patterns.

The `_buckets` dict keys are method names only; a `(client_id, method)` keyed structure would isolate clients. Memory cost: O(clients × methods) — acceptable for local use.

**Status:** Acceptable for current single-client topology. Document the design assumption.

---

## Finding 4 — LOW: Privacy-adjacent operations subject to throttle without exclusion

**Severity:** Low  
**File:** `KrabEar/backend/ipc_throttle.py`, lines 78–100

`EXCLUDED_METHODS` contains lifecycle operations (`start_recording`, `set_settings`, `live_subs_ingest`, Phase 3 polling calls) but does not include privacy-related operations:

- `get_privacy_audit_log` (dispatch line 1222) — subject to `light` throttle
- `clear_privacy_audit_log` (dispatch line 1223) — subject to `light` throttle

The `light` limit (120/min) is unlikely to trigger in normal use. However, the audit requirement pattern (and guidance in the CLAUDE.md pattern note "don't throttle privacy operations") suggests these should be explicitly excluded for consistency — especially `clear_privacy_audit_log`, which is a data-hygiene operation that a user or compliance workflow might call during a purge sequence that also calls other throttled methods.

**Fix:** Add to `EXCLUDED_METHODS`:
```python
"get_privacy_audit_log",
"clear_privacy_audit_log",
```

---

## Finding 5 — INFO: `_classify_method` docstring contradiction — returns `None` claim is false

**Severity:** Info  
**File:** `KrabEar/backend/ipc_throttle.py`, lines 112–122

The docstring for `_classify_method` says: "Возвращает None для методов из EXCLUDED_METHODS (throttling не применяется)." But the implementation never checks `EXCLUDED_METHODS` and always returns a string (`"heavy"`, `"medium"`, or `"light"`). The `EXCLUDED_METHODS` short-circuit is handled upstream in `check_rate()` (line 221) and `get_wait_time()` (line 239), not in `_classify_method`.

The `None` claim is a documentation error — it misleads readers who might expect `_classify_method` to handle exclusion and write guards like `if category is None: return True`.

**Fix:** Remove the `None` sentence from the docstring:
```python
def _classify_method(method: str) -> str:
    """Возвращает категорию метода: 'heavy', 'medium' или 'light'.

    Методы из EXCLUDED_METHODS обрабатываются в check_rate/get_wait_time,
    а не здесь — эта функция всегда возвращает строку.
    """
```

---

## Non-findings (confirmed OK)

- **Token bucket correctness:** `rate = capacity / 60.0` is correct — a capacity-N bucket refills fully in exactly 60 seconds. `min(capacity, tokens + elapsed * rate)` caps correctly. No overflow or underflow risk.
- **Dispatch placement:** Throttle check (service.py lines 1253–1281) runs AFTER auth/signing validation and BEFORE the handler lookup — correct ordering. A throttled request is rejected before any handler runs.
- **Response on throttle:** Returns a structured `{"ok": false, "error": {"code": "rate_limit_exceeded", "message": "... retry in Xs"}}` with wait time. Not a silent drop. Error bus push (ipc.rate_limit_exceeded) also fires. This is the correct 429-equivalent behavior.
- **Memory bound:** Buckets are per-method (not per-client, see Finding 3). With ~318 total handlers and lazy creation, the dict will contain at most ~318 `_TokenBucket` instances (each ~100 bytes via `__slots__`). Bounded.
- **Observability:** `get_throttle_stats` IPC method returns `{total_calls, total_throttled, methods: {m: {calls, throttled, category, limit_per_minute}}}`. Error bus emits `ipc.rate_limit_exceeded` with method + wait_sec context. `logger.warning` fires on each throttle event. Good coverage.
- **Test coverage:** `test_ipc_throttle.py` (389 lines) and `test_ipc_throttle_extras.py` (288 lines) cover: classification, token bucket refill, thread safety, wait time math, stats tracking, reset behavior, custom limits, integration with `BackendService.handle_request`. Coverage is thorough.
- **`IPC_THROTTLE_ENABLED` default:** `True` in `core/config.py` line 225 — throttle is on by default and can be disabled via `KRAB_EAR_IPC_THROTTLE_ENABLED=false`.
