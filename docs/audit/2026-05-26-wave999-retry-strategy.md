# Audit W999 — `RetryStrategy` / `core/retry_strategy.py`

**Date:** 2026-05-26  
**Scope:** `KrabEar/core/retry_strategy.py` (152 lines) + `KrabEar/tests/test_retry_strategy.py` (294 lines)  
**Branch:** `wave736/conflict-triage` → base `codex/krab-ear-v2`

---

## Summary

`RetryStrategy` is a clean, minimal synchronous retry wrapper with exponential backoff, an
allow-list exception classifier, and per-instance stats.  Five issues merit attention.

---

## Findings

### F-1 MEDIUM — No backoff cap; delay grows unbounded

`get_delay(attempt)` returns `backoff_factor ** attempt` with no upper bound.

```python
def get_delay(self, attempt: int) -> float:
    return self.config.backoff_factor ** attempt   # no cap
```

With the default `backoff_factor=1.5` and `max_retries=2` the longest delay is
`1.5^1 = 1.5 s` — harmless.  But callers passing `max_retries=10, backoff_factor=2.0`
would reach a 512 s sleep on the last attempt.  No `max_delay` field exists in
`RetryConfig`.

**Recommendation:** add `max_delay: float = 30.0` to `RetryConfig` and clamp in
`get_delay`: `return min(self.config.backoff_factor ** attempt, self.config.max_delay)`.

---

### F-2 MEDIUM — No jitter; thundering herd risk

`get_delay` is fully deterministic.  When multiple threads (e.g. parallel STT workers)
fail simultaneously and retry in lock-step they all sleep for the same duration and
hammer the GPU / model loader at the same instant.  The test suite even asserts
determinism explicitly (`test_jitter_added` checks that two strategies return *identical*
delays and notes "jitter must be added externally").

No external jitter wrapper exists anywhere in the codebase — the feature is documented
as "caller's responsibility" but never implemented.

**Recommendation:** add `jitter: bool = True` to `RetryConfig`; in `get_delay` apply
`random.uniform(0, delay)` (full jitter) when `jitter=True`.

---

### F-3 LOW — No total-time budget; only retry count guards execution length

`RetryConfig` has `max_retries` (count) but no `timeout_budget` (wall-clock seconds).
A slow function with `max_retries=2, backoff_factor=10.0` can block a thread for
`10 + 100 = 110 s` plus however long each attempt itself takes, without any deadline.

**Recommendation:** add `budget_seconds: float | None = None` to `RetryConfig`.  Track
`start = time.monotonic()` at the top of `execute_with_retry`; check
`time.monotonic() - start >= budget_seconds` before each `time.sleep`.

---

### F-4 LOW — No cancellation path; `time.sleep` blocks uninterruptibly mid-backoff

The only way to stop a running `execute_with_retry` call is to terminate the thread.
There is no `threading.Event` or similar cancel signal.  For a max-sleep of 1.5 s with
defaults this is benign, but with higher `backoff_factor` or a future budget feature
the process cannot cleanly shut down mid-backoff.

**Recommendation:** add `cancel_event: threading.Event | None = None` to
`execute_with_retry`; replace `time.sleep(delay)` with:

```python
if cancel_event and cancel_event.wait(timeout=delay):
    raise InterruptedError("retry cancelled")
time.sleep(delay)  # fallback when no event supplied
```

---

### F-5 HIGH — Completely unwired; dead utility code

A `grep -rn "RetryStrategy\|retry_strategy\|RetryConfig" KrabEar/` across the entire
Python source (excluding `retry_strategy.py` itself) returns **zero matches**.  The
class is never imported or instantiated anywhere in the production codebase — not in
`service.py`, `engine.py`, `transcriber.py`, `rest_server.py`, or any extracted
service.

The STT fallback chain in `core/engine.py` uses its own bare `try/except` loops; the
LLM rewriter uses `CircuitBreaker` (not `RetryStrategy`).

**Recommendation:** either wire `RetryStrategy` into `engine.py`'s STT fallback chain
(the documented use-case in the docstring) or mark the module `# UNUSED — candidate for
removal` until a concrete call-site lands.

---

## Checklist summary

| # | Item | Status |
|---|------|--------|
| 1 | Backoff algorithm | Pure exponential (`factor^attempt`), **no jitter**, **no cap** |
| 2 | Max retries off-by-one | Correct: loop `range(max_retries + 1)` → initial + N retries |
| 3 | Total time budget | **Absent** — count only |
| 4 | Exception classification | String allow-list (`retry_on`); `_classify_error` covers `TimeoutError`, `MemoryError`, `OSError`, `RuntimeError`; everything else → `"unknown"` (not retried) |
| 5 | Blocking | `time.sleep` on calling thread; comment acknowledges "sync OK: callers run in threads" |
| 6 | Logging | Each retry logged at `WARNING` with delay and attempt count; non-retryable bail-out logged at `DEBUG` |
| 7 | Test coverage | Good: 28 test methods covering config, delay math, classification, stats, concurrency isolation; jitter absence explicitly noted |
| 8 | Wire status | **Zero production callers** (F-5) |
| 9 | Idempotency assumption | Assumed but undocumented; no comment warns callers |
| 10 | Cancellation | **Absent** (F-4) |

---

## Priority

| Priority | Finding |
|----------|---------|
| HIGH | F-5 — never wired into any production code |
| MEDIUM | F-1 — no backoff cap |
| MEDIUM | F-2 — no jitter (thundering herd) |
| LOW | F-3 — no time budget |
| LOW | F-4 — no cancellation |
