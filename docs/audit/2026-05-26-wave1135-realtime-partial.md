# Wave 1135 Audit — RealtimePartialTranscriber

**File audited:** `KrabEar/backend/realtime_partial.py`  
**Date:** 2026-05-26  
**Auditor:** W1135 (sub-agent, read-only)

---

## Summary

`RealtimePartialTranscriber` is a daemon thread that, during an active recording session,
periodically snapshots the audio buffer, runs a "balanced" STT preview pass, and emits
`realtime.partial_transcript` events via the `EventBus`. It is instantiated and wired by
`RecordingCoreService.handle_start_recording` and stopped in `_stop_recording_phase_a`.

Overall quality is **good**: the lifecycle is clean, error isolation is solid, tests are
comprehensive, and the module is correctly wired. Five findings are listed below.

---

## Findings

### F1 — Race condition on `_session_id` / `_sample_rate` between caller thread and worker thread (LOW)

**Location:** `realtime_partial.py:91-99` (`start()`), `realtime_partial.py:172` (`_worker()`)

`start()` assigns `self._session_id` and `self._sample_rate` on the calling thread, then
immediately starts the worker thread. The worker reads `self._session_id` inside `_worker()`
at every emit. No lock or `threading.Event` barrier guarantees the assignment is visible to
the worker before the first iteration. In CPython with the GIL this is safe in practice, but
the pattern is technically unsound: if the interval is extremely short (`≥ 0.1 s` is the
minimum) the worker can enter its first loop iteration before Python's memory model has flushed
the write to `_session_id`. The existing `_stop_event.wait(interval_sec)` before the first
body of the loop acts as a de-facto barrier in practice, making this a **LOW** risk, not a
**CRITICAL** one.

**Recommendation:** Either document the implicit barrier or move the assignments before
`_stop_event.clear()` + add a brief `_start_event.set()` pattern, or use `threading.Event`
as a synchronisation point.

---

### F2 — `transcribe_preview` bypasses denoiser and SmartSilenceSkipper (INFORMATIONAL — by design, but undocumented)

**Location:** `KrabEar/backend/transcriber.py:96-100`, `KrabEar/core/engine.py:838-842`

In `engine.transcribe()`, the `AudioDenoiser` is applied only when `not is_preview`
(line 839). `transcribe_preview` always calls `engine.transcribe(..., is_preview=True)`,
so preview STTs receive **raw, noisy audio**.

`SmartSilenceSkipper` (`core/smart_silence_skipper.py`) is not wired into the backend at
all (no import in `recording_core_service.py` or `service.py`), so it does not affect
either the partial or the final STT path.

Partial transcript quality during noisy recordings can therefore diverge significantly from
the final result. This is a **deliberate trade-off for speed** but is not documented in the
module docstring.

**Recommendation:** Add a note to the `transcribe_preview` docstring and the module-level
docstring explaining that denoising is intentionally skipped. If quality becomes an issue,
a lightweight denoiser pass can be gated on `is_preview=True` + a dedicated flag.

---

### F3 — No upper bound on consecutive errors before giving up (LOW)

**Location:** `realtime_partial.py:182-187` (`_log_error`), `realtime_partial.py:124-180` (`_worker`)

The worker loop never terminates due to errors. After 5 errors the log level escalates to
`WARNING`, but the loop keeps running indefinitely, calling `snapshot_audio` and
`transcribe_preview` forever even if the recorder or transcriber is permanently broken.

On a GPU hang scenario (e.g. MLX lock held indefinitely in a sibling thread), each
`transcribe_preview` call would block and then raise, incrementing `error_count` without
bound. The worker would keep retrying at `interval_sec` cadence, generating a WARNING log
flood and consuming CPU/GPU resources.

**Recommendation:** Add a max-consecutive-error threshold (e.g. 20) after which the worker
logs an ERROR and breaks out of the loop, setting `self._stop_event`. This mirrors the
circuit-breaker pattern used elsewhere in the codebase (`BackendSupervisor`).

---

### F4 — `realtime.final_transcript` event type constant defined but never used by the module (INFORMATIONAL)

**Location:** `realtime_partial.py:28` (`_REALTIME_FINAL_TYPE = "realtime.final_transcript"`)

The constant `_REALTIME_FINAL_TYPE` is defined in `realtime_partial.py` but is **never used
within this module**. The actual `realtime.final_transcript` event is emitted by
`RecordingCoreService._stop_recording_phase_e` (`recording_core_service.py:1186-1196`) with
a hardcoded string literal.

This creates two risks:
1. If the constant is renamed in `realtime_partial.py` the caller in `recording_core_service.py`
   drifts silently.
2. Importing `_REALTIME_FINAL_TYPE` from `realtime_partial.py` in the test file
   (`test_realtime_partial.py:26`) gives a false sense that the two are linked.

**Recommendation:** Either use `_REALTIME_FINAL_TYPE` in `recording_core_service.py`
(import it from `realtime_partial`) or move both constants to a shared `ipc_constants.py`
or `event_types.py`.

---

### F5 — MLX thread-safety: `transcribe_preview` relies on `engine.transcribe`'s `mlx_lock` — no direct violation, but no explicit documentation (INFORMATIONAL)

**Location:** `KrabEar/core/engine.py:1892` (`with mlx_lock():`), `KrabEar/backend/transcriber.py:96-100`

`transcribe_preview` delegates to `engine.transcribe(..., is_preview=True)`, which wraps the
`mlx_whisper.transcribe` call inside `with mlx_lock()`. The lock is an `RLock`, so re-entrant
calls from the same thread are safe.

However, the partial transcriber thread is a **separate** OS thread from any ongoing
`stop_recording` STT (which also holds `mlx_lock`). The `stop_recording` call, triggered by
the user releasing the hotkey, can race with the ongoing partial STT for the `mlx_lock`.
Because `mlx_lock` is a non-timed `RLock.acquire()`, the partial worker will **block** inside
`transcribe_preview` until the final STT completes. This is the correct serialisation
behaviour, but it can cause the partial worker to delay joining for the `timeout_sec=4.0`
given in `stop()`, effectively causing `_stop_recording_phase_a` to wait up to 4 s before
proceeding. On recordings of 1-2 s (typical short dictation), the final STT completes in < 1 s,
so the join succeeds. On longer slow MLX models this could delay phase A.

**Recommendation:** Document the `stop()` timeout dependency on MLX inference time.
Consider reducing `stop()` default timeout to 2 s (sufficient for fast models) or making it
configurable, with a `logger.warning` if the join times out.

---

## Wire Status

`RealtimePartialTranscriber` is **correctly wired**:
- Instantiated in `RecordingCoreService.handle_start_recording` (line 178), gated on
  `realtime_partial_enabled` setting (default `True`).
- Stopped in `_stop_recording_phase_a` (line 740), with `finally: self._rt_partial = None`.
- `realtime.final_transcript` emitted in `_stop_recording_phase_e` (line 1186).

---

## Test Coverage

`KrabEar/tests/test_realtime_partial.py` — 5 test classes, ~25 test methods:
- `TestRealtimePartialTranscriberLifecycle` — start/stop/idempotence.
- `TestRealtimePartialEmission` — event fields, empty-text suppression, no-delta guard.
- `TestRealtimePartialErrorHandling` — snapshot error, transcribe error, disabled flag.
- `TestRealtimePartialSessionIsolation` — session_id in payload, sample_rate storage.
- `TestRealtimePartialThreadSafety` — concurrent start/stop, repeated exception survival.

Coverage is **good**. Missing: test for the case where `stop()` join times out (F5), and a
test verifying that a permanently-erroring transcriber eventually stops retrying (F3).

---

## Interaction with Adjacent Modules

| Module | Interaction | Notes |
|--------|-------------|-------|
| `AudioRecorder` | `snapshot_audio()` — thread-safe (holds `_lock`, returns copy of chunks) | Safe |
| `AudioDenoiser` | Skipped on `is_preview=True` | By design; undocumented (F2) |
| `SmartSilenceSkipper` | Not wired into backend; irrelevant to partial path | No interaction |
| `RealtimeSilenceFilter` | Independent thread, also calls `snapshot_audio`; no shared state | Safe (independent readers) |
| `mlx_lock` | Serialises partial vs. final STT; can delay `stop()` join | Correct, see F5 |
| `EventBus` | `emit()` used; thread-safe (EventBus has internal lock) | Safe |

---

## Verdict

No HIGH or CRITICAL issues. The module is well-structured with solid error isolation.
Recommended actions: document the denoiser bypass (F2), add a circuit-breaker exit for
permanent errors (F3 — LOW), and consolidate the `_REALTIME_FINAL_TYPE` constant (F4).
