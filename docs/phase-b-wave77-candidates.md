# Phase B Wave 77 — New Error Code Candidates

**Date**: 2026-05-19  
**Source**: `/logs/krab-ear-backend.out.log` (86 679 lines, analysed last 2 000 + full grep)  
**Method**: frequency analysis of WARN/ERROR patterns not already pushed to `error_bus`.

---

## Summary

| Rank | Pattern (log message fragment) | Total occurrences | Already wired? |
|------|-------------------------------|:-----------------:|:--------------:|
| 1 | `IPC rate limit exceeded: method=…` | **2 779** | No |
| 2 | `GigaAM transcribe failed … worker not started or crashed` | **3 829** | Partial* |
| 3 | `Критическая ошибка распознавания` (engine.py catch-all) | **68** | No |

> *`stt.gigaam_worker_timeout` exists for the subprocess timeout path; the "worker not started or
> crashed" branch (`is_loaded() == False`) is a different condition and is NOT wired.

---

## Candidate 1 — `ipc.rate_limit_exceeded`

**Trigger**: `service.py:1093` — `IPCThrottle.check_rate()` returns `False`; the handler
`logger.warning("IPC rate limit exceeded: method=%s wait=%.2fs", method, wait_sec)` fires but
`error_bus.push()` is never called.  
**Root cause**: `get_call_assist_state` is polled by the Swift overlay at ~3 Hz during a call,
hammering the 1 req/s token bucket (2 779 occurrences total).  
**Severity**: `warn` — the client retries automatically; no data loss.

Proposed `ERROR_REGISTRY` entry:

```python
# ipc.rate_limit_exceeded — IPC method throttled (token bucket exhausted)
"ipc.rate_limit_exceeded": {
    "severity": "warn",
    "title": "IPC rate limit exceeded",
    "description": "A method was called faster than its configured rate limit allows.",
    "action": None,
    "sentry_tier": "breadcrumb",  # high-volume; use breadcrumb, not event
},
```

---

## Candidate 2 — `stt.gigaam_worker_crashed`

**Trigger**: `core/pipeline/stt_gigaam.py:589` — `_GigaAMSubprocessSession.transcribe()` called
when `is_loaded()` is `False`; raises `RuntimeError("worker not started or crashed")`, caught by
`AudioEngine.transcribe()` which logs `WARNING: GigaAM transcribe failed … worker not started or
crashed` (3 829 occurrences — highest absolute count in the log).  
**Root cause**: GigaAM subprocess exits silently after an OOM or `LocalEntryNotFoundError`; the
engine falls back to Whisper but never surfaces the crash to the user or Sentry.  
**Severity**: `error` — silent GigaAM outage means RU accuracy degrades to Whisper without notice.

Proposed entry:

```python
# stt.gigaam_worker_crashed — GigaAM subprocess not loaded when transcribe was attempted
"stt.gigaam_worker_crashed": {
    "severity": "error",
    "title": "GigaAM worker не запущен",
    "description": "GigaAM subprocess вышел или не был запущен; STT упал на Whisper.",
    "action": "restart_gigaam_worker",   # new action: respawn subprocess
    "sentry_tier": "event",
},
```

---

## Candidate 3 — `stt.critical_recognition_error`

**Trigger**: `core/engine.py:1046` — broad `except Exception` in `AudioEngine.transcribe()`;
logs `logger.exception("Критическая ошибка распознавания")` (68 occurrences). This is the
**catch-all** for any unexpected exception in the full STT pipeline (including OOM, file corruption,
Metal assertion failures not captured by more specific handlers).  
**No `error_bus.push()` call** exists in this branch; Sentry never sees these crashes.  
**Severity**: `critical` — catches unknown failures; each one is potentially a novel bug.

Proposed entry:

```python
# stt.critical_recognition_error — unexpected exception in AudioEngine.transcribe() catch-all
"stt.critical_recognition_error": {
    "severity": "critical",
    "title": "Критическая ошибка STT",
    "description": "Необработанное исключение в AudioEngine.transcribe().",
    "action": None,
    "sentry_tier": "event",   # always send — these are novel failures
},
```

---

## Already-wired patterns (excluded)

| Pattern | Code | Occurrences |
|---------|------|:-----------:|
| `Переполнение аудиобуфера` | `audio.buffer_overflow` | 119 |
| `LLM rewriter failure` | `rewriter.timeout` / `rewriter.circuit_open` / etc. | 88 |
| `Circuit breaker` | `rewriter.circuit_open` | 4 |

---

## Recommended implementation order

1. **`stt.gigaam_worker_crashed`** — highest frequency (3 829), actionable (restart worker).
2. **`ipc.rate_limit_exceeded`** — highest total (2 779) but low severity; add as breadcrumb only.
3. **`stt.critical_recognition_error`** — low frequency (68) but highest impact; every occurrence
   is an unknown bug that currently never reaches Sentry.
