# Audit W1134 — RecordingCoreService

**Date:** 2026-05-26  
**File:** `KrabEar/backend/recording_core_service.py` (1876 lines)  
**Extracted:** Wave 172 (marathon W683/W691/W734 batch)  
**Auditor:** W1134 sub-agent (Sonnet 4.6)

---

## Scope

Read-only audit of `RecordingCoreService` covering:

- Thread-safety of start/stop transitions
- Error handling (mic permission denied, disk full, encoder crash)
- Interaction with W948 `SessionTracker` wiring
- Interaction with W874+W878 `RealtimeSilenceFilter`
- Interaction with W1102 `SmartSilenceSkipper`
- `history_item` persistence atomicity
- Privacy mode interaction
- Test coverage
- IPC handler wire status

---

## Findings

### F1 — HIGH: `_preview_error_count` is modified by preview thread without lock

**File:** `recording_core_service.py` lines 584, 615, 626–628  
**Severity:** HIGH

`_preview_error_count` is read and written from the preview background thread (`_preview_loop`) without holding `_preview_lock`. The `_preview_lock` protects `_preview_text` and `_preview_duration_sec`, but `_preview_error_count` is accessed bare:

```python
self._preview_error_count += 1          # line 584 (no lock)
self._preview_error_count += 1          # line 615 (no lock)
if self._preview_error_count > 0:       # line 626 (no lock)
    self._preview_error_last_reset_ts = time.time()
self._preview_error_count = 0           # line 628 (no lock)
```

`BackendService._handle_get_diagnostics` reads `preview_error_count` (via the property at `service.py:2207`) from the IPC thread simultaneously. CPython GIL makes simple int read/write atomic in practice, but the compound `+= 1` on line 584/615 is not atomic (read-modify-write), and the reset-then-read sequence at lines 626–628 is a separate TOCTOU window. Under heavy concurrent IPC polling this can produce a negative count or miss a reset.

**Fix:** Guard `_preview_error_count` with `_preview_lock`, or replace with `threading.local` or an `atomics`-style wrapper. The minimal fix is to take `_preview_lock` around the increment+check+reset block.

---

### F2 — HIGH: `privacy_mode_enabled` is not checked in `handle_start_recording` or `handle_stop_recording`

**File:** `recording_core_service.py` lines 137–192, 194–245  
**Severity:** HIGH

`TranslationService.translate()` checks `privacy_mode_enabled` and skips cloud translation (lines 96, 201 of `translation_service.py`). `observability.py` skips Sentry when `privacy_mode_enabled=True`. However, `RecordingCoreService.handle_start_recording` and `handle_stop_recording` never read `privacy_mode_enabled` from settings at all.

This means:
- Recording starts normally when privacy mode is enabled — audio is captured and the full STT pipeline runs.
- The transcript text is persisted to `history.ndjson` without redaction.
- Only translation and Sentry events are suppressed; the raw transcript and `source_text` are stored unredacted.

The expected contract (per `privacy_audit.py` and Sentry init) is that privacy mode prevents sensitive data from leaving the device. But `source_text` and `display_text` are written to history unconditionally (`state_store.add_history_item`, lines 1105–1124).

Whether this is intentional (local STT is "on-device therefore private") or a gap depends on the privacy policy. If user intent is "don't store transcriptions in privacy mode", this is a HIGH gap. Currently there is **no documentation** in `recording_core_service.py` clarifying the design decision.

**Fix:** Add a docstring comment in `handle_start_recording` explicitly stating what privacy mode means for local recording. If "don't persist" is desired, add a guard in phase E.

---

### F3 — MED: `SessionTracker` is accessed via direct attribute `._active_session` — bypasses its own lock

**File:** `recording_core_service.py` line 256  
**Severity:** MED

```python
active_session = self._session_tracker._active_session
```

`SessionTracker` has its own `threading.Lock` (`_lock`) that gates `start_session` and `end_session`. Direct access to `._active_session` bypasses that lock. If a `stop_session` call modifies `._active_session = None` concurrently on another thread (e.g., a `stop_recording` IPC call racing a `session_end` IPC call), `handle_get_recording_state` can read a partially-written dict or a None reference mid-loop.

`SessionTracker` should expose a thread-safe `get_active_session_id()` method, and `handle_get_recording_state` should call it rather than accessing the private attribute.

**Fix:** Add `def get_session_id(self) -> str` to `SessionTracker` that returns under `self._lock`. Update `recording_core_service.py` line 256 to use it.

---

### F4 — MED: `RealtimeSilenceFilter` and `SmartSilenceSkipper` are not wired through `RecordingCoreService`

**File:** `recording_core_service.py` (entire file)  
**Severity:** MED

Both modules exist and have their own unit tests, but `RecordingCoreService` has no reference to either:

- `RealtimeSilenceFilter` — runs a background thread during recording that detects silence ranges. Its output (`silence_ranges`) is consumed by `AudioEngine.transcribe()` (`core/engine.py` lines 689, 819–821), but only when passed explicitly. The service never creates or passes a `RealtimeSilenceFilter` instance. The setting `realtime_silence_filter_enabled` is defined in `DEFAULT_SETTINGS` (`config.py:202`) but ignored during `start_recording`.

- `SmartSilenceSkipper` — post-processes audio before STT (removes pauses >1 s). Setting `smart_silence_skip_enabled=False` is the default, but the module is never imported or called in `RecordingCoreService`. Integration would belong in phase C (before `self.transcriber.transcribe()` is called).

The existing `silence_guard` and `background_guard` in phase B are coarse pre-filters (reject/pass whole recording), not the fine-grained silence range tagging that `RealtimeSilenceFilter` provides for Whisper initial_prompt seeding.

**Impact:** Users who enable `realtime_silence_filter_enabled=True` or `smart_silence_skip_enabled=True` see no effect from recording via the IPC hotkey path; the features silently have zero effect.

**Fix:** Wire `RealtimeSilenceFilter` into `handle_start_recording` / `_stop_recording_phase_a` (start/collect ranges), and optionally wire `SmartSilenceSkipper` in phase C.

---

### F5 — MED: `store.add_history_item` exception propagates uncaught through phase E — IPC caller gets raw exception string on disk-full

**File:** `recording_core_service.py` lines 1105–1124 in `_stop_recording_phase_e`  
**Severity:** MED

`StateStore.add_history_item` raises after pushing `history.write_fail` to the error bus on disk-full or permission-denied (`state_store.py:223`, `raise` at end). Phase E calls it without a try/except:

```python
item = self.store.add_history_item(...)   # line 1105 — no wrapping try/except
```

If the disk is full, the exception propagates all the way through `_stop_recording_phase_e` → `handle_stop_recording` → `BackendService._handle_stop_recording` → IPC response as `{"ok": false, "error": "..."}`. This is after audio capture has already been discarded — the user loses the recording with no chance to retry.

Note that `history.write_fail` error code IS pushed (good), but the IPC response shape will be an error rather than a structured "disk_full" result. The Swift side cannot distinguish "recording failed to start" from "recording captured but failed to persist".

**Fix:** Wrap `store.add_history_item` in a try/except in phase E. On failure, return a structured result with `status: "persist_failed"` plus `text`, `original_text` fields so the Swift layer can still paste the transcription even if storage failed.

---

### F6 — LOW: Test mock sets up `vocab.get_words` but service calls `vocabulary.load()`

**File:** `KrabEar/tests/test_recording_core_service.py` line 110  
**Severity:** LOW

```python
vocab = MagicMock()
vocab.get_words.return_value = []   # line 110
```

But `RecordingCoreService._stop_recording_phase_c` calls `self.vocabulary.load()` (line 903), and `_transcribe_paths_core` also calls `self.vocabulary.load()` (line 1280). `MagicMock().load()` returns a new `MagicMock` (truthy) rather than a list, so `user_vocabulary = self.vocabulary.load() or []` would evaluate to the mock rather than `[]`. The test passes because the mock transcriber ignores `extra_vocabulary`, but any test that inspects vocabulary behavior will silently receive the mock object.

**Fix:** Change line 110 in the test file to `vocab.load.return_value = []`.

---

### F7 — LOW: `handle_start_recording` does not guard against `recorder.start()` raising (e.g., PortAudio / mic permission error)

**File:** `recording_core_service.py` line 138  
**Severity:** LOW

```python
started = self.recorder.start()
```

`AudioRecorder._worker` catches exceptions in the background thread and logs them, but `AudioRecorder.start()` itself (which only sets flags and launches the thread) can still raise if `threading.Thread.start()` fails (unlikely but possible). More importantly, if a future recorder implementation raises `PermissionError` or `sounddevice.PortAudioError` synchronously inside `start()`, the exception propagates out of `handle_start_recording` as an unhandled IPC error with no breadcrumb and no structured error code.

The `recorder.py:_worker` pushes `audio.buffer_overflow` to the error bus, but there is no error code for `audio.mic_permission_denied` being surfaced from `start()`.

**Fix:** Wrap `self.recorder.start()` in a `try/except (PermissionError, Exception)` block in `handle_start_recording` and return a structured error response, or push an appropriate error code.

---

## Integration Status Summary

| Integration | Status |
|---|---|
| IPC handlers wired (all 8) | OK — all verified in `service.py` dispatch table |
| SessionTracker W948 | PARTIAL — injected but only `._active_session` read directly (no `start_session`/`end_session` calls from `RecordingCoreService`) |
| RealtimeSilenceFilter W874/W878 | NOT WIRED — service unaware of the filter |
| SmartSilenceSkipper W1102 | NOT WIRED — service unaware of the skipper |
| Privacy mode | NOT CHECKED — recording and persistence proceed regardless |
| history_item atomicity | OK — StateStore uses POSIX flock; raises on failure (see F5) |
| Test coverage | GOOD baseline (495 lines, covers start/stop/state/guards/helpers) |

---

## Finding Count

| Severity | Count |
|---|---|
| HIGH | 2 (F1, F2) |
| MED | 3 (F3, F4, F5) |
| LOW | 2 (F6, F7) |
| **Total** | **7** |
