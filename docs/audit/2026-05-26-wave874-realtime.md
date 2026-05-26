# Wave 874 — Audit: RealtimePartialTranscriber + RealtimeSilenceFilter

**Date:** 2026-05-26  
**Files audited:**
- `KrabEar/backend/realtime_partial.py`
- `KrabEar/backend/realtime_silence_filter.py`

---

## 1. Thread lifecycle

### RealtimePartialTranscriber

- **Start:** Creates a `daemon=True` thread named `"RealtimePartialTranscriber"`. Thread is idempotent — second call to `start()` returns early if already alive. `_stop_event` is cleared before spawn.
- **Stop:** Sets `_stop_event`, calls `thread.join(timeout=4.0)`, then sets `_thread = None`. Idempotent — safe to call when not running.
- **Guard:** If `transcriber` lacks `transcribe_preview`, thread is never started. This prevents infinite `AttributeError` spam on CI with stub transcribers.
- **Wiring (RecordingCoreService):** Created fresh per recording in `handle_start_recording` with UUID session id. Stopped in `_stop_recording_phase_a` under try/finally with `_rt_partial = None` in finally — ensures no stale reference even if `stop()` raises. BackendService delegates `_rt_partial` access via property proxy.

**Finding 1 (LOW) — Concurrent start race:** `start()` checks `is_running` (thread liveness) outside any lock. If two callers race on the same instance, both could see `is_running = False` and spawn two threads. In practice this cannot happen — `handle_start_recording` always stops the previous instance before creating a new one — but there is no lock guarding the check-then-spawn sequence. Low risk; worth a note.

**Finding 2 (INFO) — stop() timeout fixed at 4 s:** `stop(timeout_sec=4.0)` is hardcoded at the call site in `_stop_recording_phase_a`. If a `transcribe_preview` call blocks longer than 4 s (e.g. long audio, GPU stall), the worker thread is left alive as a daemon. The daemon flag ensures it won't block process exit, but it may emit stale events into the bus after recording ends. The `_stop_event` will eventually wake it, but the join will silently return early. No explicit warning is logged if join times out.

### RealtimeSilenceFilter

- **Start:** Thread creation is guarded by `self._lock`. Inner `if self._thread is not None and self._thread.is_alive()` check is inside the lock — correctly idempotent.
- **Stop:** Sets `_stop_event`, releases `self._lock` reference (`self._thread = None` inside lock), then calls `thread.join(timeout=max(check_sec + 1.0, 2.0))` — adaptive join timeout relative to check cadence. This is better than the fixed 4 s used by the partial transcriber.
- **Worker:** Uses `_stop_event.wait(timeout=check_sec)` as the loop sleep — cleanly interruptible.

**Finding 3 (LOW) — stop() lock ordering:** In `stop()`, `self._thread = None` is set inside `self._lock` while holding it, then `thread.join()` is called outside. A concurrent `start()` could re-enter and spawn a new thread between the `None` assignment and the join completing. The old thread is still running during that window. Not a safety hazard (daemon threads, no shared mutable state other than `_silence_ranges`), but could theoretically produce a duplicate check cycle.

---

## 2. Emit cadence

### RealtimePartialTranscriber

- Default interval: **3.0 s** (configurable via `rt_partial_interval_sec` setting).
- The worker waits the full interval using `_stop_event.wait(interval_sec)`, then snapshots, then transcribes. This means actual emit cadence = `interval_sec + transcribe_latency`. No compensation for transcription time.
- **Progress guard:** Emits are suppressed if `duration_sec - last_transcribed_duration < 0.5`. After the first successful emit, `last_transcribed_duration` advances, so subsequent ticks only emit if the recording has grown by at least 0.5 s. Prevents duplicate partials on a stalled buffer. **Finding 4 (BUG-RISK):** `last_transcribed_duration` is never reset between sessions because a new `RealtimePartialTranscriber` instance is created per session — this is correct. However, if `snapshot_audio` returns a `duration_sec` that is non-monotonic (e.g. recorder wraps or resets), `last_transcribed_duration` will be stale-higher, causing the guard to suppress all subsequent emits for that session.
- **Empty audio guard:** Checks `getattr(audio, "size", None) == 0`. This skips NumPy arrays of size 0 but does NOT handle `None` audio or non-NumPy objects that lack a `.size` attribute — those fall through to `transcribe_preview` which may then raise.
- **Event payload:** `{session_id, text, is_partial=True, ts}`. No `duration_sec`, no `confidence`, no sequence number. Receiver cannot order partials or detect gaps.

**Finding 5 (LOW) — No sequence number:** Partial events carry no monotonic sequence counter. If two events arrive out-of-order (e.g. due to event bus buffering), the UI has no way to discard the stale one. Adding `seq: int` would be low-effort.

### RealtimeSilenceFilter

- Default check interval: **5.0 s** (configurable via `rt_silence_check_sec`).
- Emits `recording.silence_detected` only when `total_silence >= max_silence_sec` AND at least one region is ≥ `max_silence_sec`. The two-level gate (total silence check first, then per-region filter) is correct but redundant — if any single region ≥ `max_silence_sec`, then `total_silence` must also be ≥ `max_silence_sec`. The early-exit optimisation on line 132 could miss cases where several small regions sum to ≥ `max_silence_sec` but none individually exceeds it (those never produce `new_ranges` anyway due to the per-region filter on line 137). **The effective contract is: only regions individually ≥ max_silence_sec are tracked.** This is probably intentional but is not stated in the docstring.

**Finding 6 (INFO) — Threshold not configurable:** `_threshold_db = -40.0` is hardcoded as an instance variable; it is NOT pulled from `settings`. The setting key `rt_silence_check_sec` / `rt_silence_window_sec` / `rt_silence_max_sec` are read from settings, but the energy threshold is fixed. Noisy environments with low SNR will have difficulty hitting −40 dB silence.

---

## 3. Silence filter accuracy

- Uses `core.silence_detector.SilenceDetector.detect_silence()` with frame size 512 (~32 ms at 16 kHz). Frame-based RMS detection.
- Threshold at −40 dB = ~0.01 amplitude. Very sensitive to low-level background noise. Background noise above −40 dB (common in office environments) will prevent any regions from being classified as silence.
- The filter operates on a **windowed snapshot** (`max_duration_sec = window_sec`). On long recordings (> `window_sec`), only the most recent window is checked. Silence ranges are accumulated with absolute timestamps (`window_start_sec + region.start_sec`). This is correct for long recordings but means early silence in a very long recording will never be re-checked or refined.
- `_merge_ranges` correctly handles overlapping and adjacent ranges, and is called on every `_check_once()` to merge new ranges with existing ones. This prevents unbounded growth of the list.
- **Finding 7 (BUG) — Silence filter not integrated with partial transcriber:** `RealtimeSilenceFilter` accumulates silence ranges, but `RealtimePartialTranscriber` does not consume them. The partial transcriber snapshots raw audio and sends it to STT without zeroing silence regions. The intent (per module docstring) is that silence ranges are used "при финальной транскрибации" (at final transcription), but `stop_recording_phase_c` in `RecordingCoreService` does not call `zero_silence_ranges()` either. The `zero_silence_ranges` utility function exists in the module but is never called by any production code path. The `RealtimeSilenceFilter` is not instantiated anywhere in `RecordingCoreService` or `BackendService`. **The silence filter is effectively dead code in the current production wiring.** Tests pass because they test the module in isolation.

---

## 4. Shutdown cleanup

### RealtimePartialTranscriber

- `stop()` always sets `_stop_event` even if `_thread is None`. Safe.
- `_thread = None` assignment happens after `join()`, not inside a finally. If `join()` raises (unexpected), `_thread` would retain a reference to the dead thread. This is a cosmetic issue only.
- The `finally: self._rt_partial = None` in `RecordingCoreService._stop_recording_phase_a` ensures the BackendService reference is cleared even if `stop()` raises. Correct pattern.

### RealtimeSilenceFilter

- `stop()` returns `silence_ranges` — caller is responsible for consuming them. If caller discards the return value, ranges are lost silently. No warning.
- `self._thread = None` is set inside `self._lock` before `thread.join()`. After join, the filter is in clean state.
- No `close()` / `__del__` finalizer. Long-lived processes that repeatedly create/destroy filters without calling `stop()` would leak daemon threads until they naturally die with the process.

---

## 5. Test coverage summary

| Test file | Classes | Key gaps |
|-----------|---------|----------|
| `test_realtime_partial.py` | 6 | No test for `transcriber.transcribe_preview` returning `None` (not dict); no test for non-monotonic `duration_sec` |
| `test_realtime_silence.py` | 7 | No test for configurable threshold; no test for `zero_silence_ranges` being called by production wiring |
| `test_realtime_adaptive_backoff.py` | 5 | Python mirror of Swift adaptive backoff logic — not testing Python modules directly |

Overall coverage is solid for lifecycle and happy-path scenarios. The main gap is the disconnect between silence detection and actual use.

---

## 6. Findings summary

| # | Severity | File | Description |
|---|----------|------|-------------|
| 1 | LOW | `realtime_partial.py` | `start()` check-then-spawn not locked — theoretical concurrent spawn race |
| 2 | LOW | `realtime_partial.py` | `stop()` join timeout (4 s) not logged if exceeded; stale thread emits after recording ends |
| 3 | LOW | `realtime_silence_filter.py` | Lock ordering in `stop()` allows brief window where old thread runs alongside newly spawned thread |
| 4 | LOW | `realtime_partial.py` | Non-monotonic `duration_sec` from recorder would permanently suppress partial emits for session |
| 5 | INFO | `realtime_partial.py` | No sequence number in partial event payload; UI cannot detect/discard stale partials |
| 6 | INFO | `realtime_silence_filter.py` | Energy threshold hardcoded at −40 dB; not configurable via settings |
| 7 | **BUG** | `realtime_silence_filter.py` | `RealtimeSilenceFilter` is never instantiated in production (`RecordingCoreService`). `zero_silence_ranges()` is never called. Silence filter is effectively dead code. |

**Total findings: 7** (1 BUG, 4 LOW, 2 INFO)

---

## 7. Recommendations

1. **Finding 7 (BUG):** Wire `RealtimeSilenceFilter` into `RecordingCoreService.handle_start_recording` / `_stop_recording_phase_a`, or remove the module and its tests until the feature is intentionally enabled.
2. **Finding 6:** Add `rt_silence_threshold_db` to `DEFAULT_SETTINGS` and read it in `RealtimeSilenceFilter.__init__`.
3. **Finding 2:** Log a WARNING if `_thread.join()` times out in `stop()` — otherwise operators have no signal that a stuck transcription thread was abandoned.
4. **Finding 5:** Add `seq` field to partial event payload (monotonic counter per session, increment on each emit).
