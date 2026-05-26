# Wave 836 — AudioRecorder Thread Safety & sounddevice Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/recorder.py` (195 lines)  
**Related:** `KrabEar/backend/recording_core_service.py` (device enumeration)  
**Auditor:** wave836/audit-recorder-W836

---

## Summary

`AudioRecorder` is a well-structured, single-threaded recording worker with a
`threading.Lock` guard around all mutable state. The core start/stop contract
is correct. This audit found **5 findings** (1 medium, 4 low/info), none of
which block production today but two of which carry latent risk.

---

## Findings

### F1 — MEDIUM: `_error_bus` is never injected into `AudioRecorder`

**File:** `recorder.py:167–194` and `service.py:248–267`

`_push_buffer_overflow_error()` uses `getattr(self, "_error_bus", None)` and
silently no-ops when the attribute is absent. `BackendService.__init__` injects
`_error_bus` into `self._llm_rewriter`, `self.transcriber`, and `_mlx_sub`,
but **never into `self.recorder`**. The `RecordingCoreService` constructor also
receives no `error_bus` argument to forward.

```python
# service.py — wires error_bus to several collaborators but skips recorder:
self._llm_rewriter._error_bus = self._error_bus   # line 258
self.transcriber._error_bus   = self._error_bus   # line 262
_mlx_sub._error_bus           = self._error_bus   # line 267
# recorder._error_bus — MISSING
```

**Effect:** `audio.buffer_overflow` errors are logged (WARNING) but never reach
the `ErrorBus`, so the Sentry dedup / user-facing toast defined in Wave 60 is
dead code at runtime.

**Fix:** After creating the recorder (or inside `RecordingCoreService.__init__`
after receiving the recorder), assign `recorder._error_bus = error_bus`. One
line.

---

### F2 — LOW: No device selection parameter; always uses PortAudio default

**File:** `recorder.py:136–141`

`sd.InputStream` is opened without a `device=` argument:

```python
with sd.InputStream(
    samplerate=self.sample_rate,
    channels=self.channels,
    dtype="float32",
    blocksize=self.chunk_size,
) as stream:
```

PortAudio picks the OS default input device. The GUI exposes a device picker
(`list_audio_inputs` / `get_audio_devices` in `RecordingCoreService`), but the
selected device ID is never passed down to `AudioRecorder`. Users who change
the input device in the panel will see no effect until restart.

**Fix:** Add an optional `device: int | str | None = None` parameter to
`AudioRecorder.__init__` (stored as `self.device`), and pass it to
`sd.InputStream(device=self.device, ...)`. `RecordingCoreService` can then
re-create the recorder (or expose a `set_device()` mutator) when the user
changes device.

---

### F3 — LOW: `stop()` duration measurement includes `thread.join` wait time

**File:** `recorder.py:63–96`

```python
self._stop_event.set()
if thread is not None:
    thread.join(timeout=timeout_sec)   # up to 3 s wait

with self._lock:
    duration = max(0.0, time.monotonic() - self._started_at)
```

`time.monotonic()` is called *after* `thread.join`, so `duration` includes up
to `timeout_sec` (default 3 s) of join latency if the worker thread is
slow to exit. Typical recordings have ~100 ms worker shutdown latency, which is
negligible, but it is conceptually wrong: the recording stopped when
`_stop_event.set()` was called, not when the thread exited.

**Fix:** Capture `stopped_at = time.monotonic()` immediately after
`self._stop_event.set()` and compute `duration = stopped_at - self._started_at`.

---

### F4 — LOW: `snapshot_audio` reads `_chunks` outside `_lock` during `np.concatenate`

**File:** `recorder.py:105–119`

```python
with self._lock:
    duration = ...
    chunks = list(self._chunks)   # shallow copy of list

# Lock released here
audio = np.concatenate(chunks, axis=0)  # iterates individual chunk arrays
```

`list(self._chunks)` copies the list references; the underlying numpy arrays
are *not* copied. If the worker thread appends a new chunk and the GC reallocates
the last chunk's buffer between `list()` and `np.concatenate`, the read could
see stale or partially-initialised memory. In CPython this is vanishingly rare
because numpy buffers are reference-counted, but it violates the intent of the
lock.

The `_worker` does `self._chunks.append(data.copy())` so each chunk is an
independent array. The risk is therefore extremely low in practice, but worth
noting for correctness.

**Fix (optional):** Inside the lock, do a deep copy:
`chunks = [c.copy() for c in self._chunks]`. Adds a memcpy proportional to
current buffer size, but removes the theoretical race.

---

### F5 — INFO: `sd = None` guard silences all import errors; no start-time check

**File:** `recorder.py:18–21`

```python
try:
    import sounddevice as sd
except Exception:
    sd = None
```

If `sounddevice` or its native PortAudio dependency is absent, `sd` is silently
`None`. The `_worker` will then raise `AttributeError: 'NoneType' object has no
attribute 'InputStream'` at record-start rather than at import time. The
exception is caught by `_worker`'s outer `except Exception` block and logged,
but `_is_recording` will be left `True` (the `finally` block clears it, so
this is actually handled) — the user will see a recording start that immediately
silently stops with a log line.

`StartupDiagnostics` (`backend/startup_diagnostics.py`) should add a check for
`sd is None` and surface it in the diagnostics IPC response so the UI can warn
the user before they attempt a recording.

---

## Correct Patterns (confirmed)

| Pattern | Status |
|---------|--------|
| All `_is_recording` / `_chunks` mutations inside `self._lock` | CORRECT |
| `start()` idempotency — returns `False` if already recording | CORRECT |
| `stop()` idempotency — returns `None` if not recording | CORRECT |
| Worker thread uses `_stop_event.is_set()` poll (not busy-wait on lock) | CORRECT |
| Buffer overflow emits WARNING log + error-bus push (when wired) | CORRECT (F1 blocks wiring) |
| `on_audio_level` callback errors are caught per-call | CORRECT |
| `data.copy()` inside `_worker` before appending to `_chunks` | CORRECT |
| `_worker` `finally` block resets `_is_recording = False` on unexpected exit | CORRECT |
| `trim_tail_ms` guard against under-run (`audio.size > trim_samples`) | CORRECT |
| `snapshot_rms` reads last chunk under lock | CORRECT |

---

## Action Items

| # | Priority | Fix | Owner |
|---|----------|-----|-------|
| F1 | MEDIUM | Wire `recorder._error_bus` in `BackendService.__init__` | Backend |
| F2 | LOW | Add `device` param to `AudioRecorder.__init__`; pass from `RecordingCoreService` | Backend |
| F3 | LOW | Measure `duration` at `_stop_event.set()`, not after `thread.join` | Backend |
| F4 | LOW (optional) | Deep-copy chunks inside lock in `snapshot_audio` | Backend |
| F5 | INFO | Add `sounddevice` availability check in `StartupDiagnostics` | Backend |
