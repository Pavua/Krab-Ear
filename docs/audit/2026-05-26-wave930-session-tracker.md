# Audit: session_tracker.py — Wave 930

**Date:** 2026-05-26
**Auditor:** Sub-agent W930 (read-only)
**File:** `KrabEar/backend/session_tracker.py` (197 lines)
**Tests:** `KrabEar/tests/test_session_tracker.py` (356 lines)

---

## Summary

`SessionTracker` is a well-structured, thread-safe class with good test coverage, but it has **one critical production gap** and four lower-severity findings.

---

## Findings

### F1 — CRITICAL: session_tracker is never wired into the recording pipeline

**Severity:** Critical (dead code in production)

`SessionTracker` is instantiated in `BackendService.__init__` (line 340) and passed to `RecordingCoreService` (line 503), but neither `start_session()` nor `end_session()` is ever called from production code:

```bash
$ grep -rn "\.start_session\|\.end_session" KrabEar/backend/ | grep -v test_
# (no output — zero production call sites)
```

`RecordingCoreService.handle_start_recording()` (line 137) does not call `session_tracker.start_session()`. `handle_stop_recording()` and its 5 phase helpers likewise never call `end_session()`. The only use of `_session_tracker` in `recording_core_service.py` is a read of `._active_session` for the `get_recording_state` IPC method (line 256), which will always return `None` (fallback: `"__live__"`).

**Impact:** `sessions.ndjson` is never written. `get_session_stats` always returns zero totals. `get_sessions` always returns `[]`. Analytics built on top of this (session rates, latency averages) are all silent zeros.

**Fix:** Call `self._session_tracker.start_session(audio_device=..., quality_preset=..., stt_model=...)` at the end of `handle_start_recording`, and call `self._session_tracker.end_session(result_payload)` at the end of `_stop_recording_phase_e` (after `result_payload` is assembled, line ~1155).

---

### F2 — Session leak: abandoned session when recording errors mid-pipeline

**Severity:** Medium

`_active_session` is set during `start_session()` and cleared only inside `end_session()`. If `handle_stop_recording()` raises an unhandled exception before reaching `_stop_recording_phase_e` (e.g., audio read error in phase B, STT crash in phase C), `end_session()` is never called and `_active_session` remains set permanently. The next recording's `start_session()` will silently overwrite it — no warning is emitted.

`end_session()` does log a warning if called without an active session, but the reverse (start overwriting a leaked session) is silent:

```python
def start_session(self, ...):
    # No check here — overwrites existing _active_session silently
    with self._lock:
        self._active_session = {...}
```

**Fix:** Log a warning in `start_session()` if `self._active_session is not None` before overwriting. Additionally, wrap the recording pipeline in a `try/finally` to call `end_session({"paste_status": "error"})` on exception.

---

### F3 — No file lock on sessions.ndjson write

**Severity:** Low

`_persist()` appends to `sessions.ndjson` using a plain `open(..., "a")` without `fcntl.flock()`. The main history store (`StateStore`) uses `fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)` for all writes. `sessions.ndjson` is currently written only from `end_session()` which is always called from the request-handling thread, so concurrent writes are unlikely in normal operation. However, if the pattern evolves (e.g., batch imports calling `end_session()` from a threadpool), interleaved writes could corrupt the NDJSON file.

**Fix:** Wrap the `open()` in `_persist()` with `fcntl.flock()`, matching the `StateStore` pattern.

---

### F4 — Privacy mode not respected: session metadata persisted unconditionally

**Severity:** Medium

When `privacy_mode_enabled=True`, `observability.py` skips Sentry init and `translation_service.py` redacts translation logs. However, `SessionTracker._persist()` has no privacy-mode check — it always writes device name, STT model, session timestamps, and duration to `sessions.ndjson`.

Specifically, `audio_device` (e.g., `"Rode NT-USB"`, `"AirPods Pro"`) and `quality_preset` reveal recording equipment and session frequency even without transcript content. This is low-entropy but potentially meaningful metadata under privacy mode.

**Fix:** Either skip `_persist()` entirely when `privacy_mode_enabled=True` (pass settings into `SessionTracker`) or strip `audio_device` before persisting in privacy mode.

---

### F5 — No Sentry breadcrumbs on session start/end

**Severity:** Low

`handle_start_recording()` already emits a Sentry breadcrumb via `add_breadcrumb(category="recording", message="started", ...)` (line 162 in `recording_core_service.py`). `SessionTracker.start_session()` and `end_session()` emit only `logger.debug()` — no `add_breadcrumb()` calls. If a crash occurs during STT, the Sentry report will lack session duration, device, and confidence metadata that would help triage.

**Fix:** Add `add_breadcrumb(category="session", message="started", data={"audio_device": audio_device, "stt_model": stt_model})` in `start_session()` and `add_breadcrumb(category="session", message="ended", data={"duration_sec": session["duration_sec"], "confidence": session["confidence"]})` in `end_session()`.

---

## Items not flagged (per audit checklist)

| Check | Result |
|-------|--------|
| **Session ID generation** | UUID4 — cryptographically random, not predictable, no MAC address leakage (UUID4 vs UUID1). OK. |
| **Concurrent sessions** | Strict single-active-session design, clearly documented in `test_concurrent_shared_tracker`. `_active_session` is `Optional` — a second `start_session()` replaces the first. Intentional. |
| **Persistence** | NDJSON append on `end_session()`. In-memory `deque(maxlen=1000)` for stats. Survives restart only via file (but F1 means file is never written). |
| **Timestamp precision** | `datetime.now(timezone.utc).isoformat()` — microsecond precision, UTC-aware. Adequate for duration analytics. |
| **Device change mid-session** | `audio_device` is captured only at `start_session()` time. Mid-session device swaps (AirPods hot-plug) are not recorded, but this is acceptable given the session captures the device at recording start. |
| **Mode mutation** | `quality_preset`/`stt_model` can be overwritten in `end_session(result)` if the result dict contains them (lines 123–128). This is a deliberate "final model wins" merge — no event sourcing needed. |

---

## Test coverage assessment

Coverage is **good for a module that is never called in production**. The test suite (356 lines, 25+ cases across 5 test classes) covers:

- UUID format validation
- Start/end lifecycle (including double-end guard)
- Stats aggregation (empty, single, multi-session)
- NDJSON persistence and reload
- Buffer eviction (maxlen)
- Unicode device names
- Concurrency (per-instance and shared-tracker sequential)

Missing tests:
- Privacy-mode suppression (not implemented, hence untestable)
- Session leak scenario (start → crash → start → end — only `end_session_finalizes` covers the benign case)
- Sentry breadcrumb emission (not implemented)

---

## Recommended priority

1. **F1** (critical) — wire `start_session`/`end_session` into `handle_start_recording` / `_stop_recording_phase_e`. Without this, the entire module is dead code.
2. **F4** (medium) — privacy mode guard in `_persist()`.
3. **F2** (medium) — silent overwrite warning + `try/finally` in recording pipeline.
4. **F5** (low) — Sentry breadcrumbs.
5. **F3** (low) — file lock in `_persist()`.
