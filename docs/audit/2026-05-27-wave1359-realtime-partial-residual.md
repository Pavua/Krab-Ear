# Wave 1359 Audit — RealtimePartialTranscriber residual (post W1200/W1323/W1143)

**File audited:** `KrabEar/backend/realtime_partial.py`  
**Related:** `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/transcriber.py`, `KrabEar/core/engine.py`, `KrabEar/backend/event_bus.py`  
**Date:** 2026-05-27  
**Auditor:** W1359 (sub-agent, read-only)  
**Branch:** `audit-realtime-partial-residual-W1359` off `codex/krab-ear-v2`

---

## Merge State of Prerequisite Fixes

| Wave | Fix description | Branch | In `codex/krab-ear-v2`? |
|------|----------------|--------|------------------------|
| W1200 | privacy_mode gate at start + emit (PR #1111) | `fix-realtime-sse-privacy-W1200` | **NOT MERGED** |
| W1323 | stop() 30 s join timeout + `_stop_requested` flag | `fix/realtime-partial-stop-timeout-W1323` | **NOT MERGED** |
| W1143 | circuit breaker exit after 10 consecutive errors | `fix-realtime-partial-circuit-W1143` | **NOT MERGED** |

All three prerequisite fixes remain on open branches. The analysis below audits code
**as it stands on `codex/krab-ear-v2`** (v2.0.5 head, commit 6c900317) and identifies
residual issues that are **NEW relative to the W1315 audit** (which already documented
W1200/W1143/F3 as its F1/F2/F3).

---

## W1315 Re-audit Summary (not repeated here)

W1315 found:
- **F1 HIGH**: privacy_mode gate absent (→ W1200, not merged).
- **F2 MED**: circuit breaker absent — worker runs forever on permanent failure (→ W1143, not merged).
- **F3 MED**: stop() join 4 s timeout too short under mlx_lock contention (→ W1323, not merged).
- **F4 LOW**: progress guard 0.5 s threshold effectively dead at default interval.
- **F5 MED**: RSF silence_ranges wiring absent (→ W1139/W1140, not merged).

The present audit treats all five as **known residuals**. New findings below are independent.

---

## New Findings

### F1 — `set_quality_profile` called unsynchronised from partial worker during stop-recording overlap (HIGH)

**Location:** `KrabEar/backend/transcriber.py:99`, `KrabEar/core/engine.py:528–541`,
`KrabEar/backend/recording_core_service.py:738–744` (phase A), `KrabEar/backend/recording_core_service.py:947–955` (phase C)

**Description:**

`transcribe_preview()` in `Transcriber` unconditionally calls `self.engine.set_quality_profile("balanced")` before invoking `engine.transcribe(is_preview=True)`. The `AudioEngine` instance is **shared** between the partial worker thread and the main recording pipeline.

The stop sequence is:
1. `_stop_recording_phase_a` calls `self._rt_partial.stop()` with a 4-second join timeout (current code, W1323 not merged).
2. After `stop()` returns — whether the thread actually finished or the join timed out — `_stop_recording_phase_c` calls `self.transcriber.transcribe(quality_profile=quality_profile)`, which calls `self.engine.set_quality_profile(quality_profile)` internally (e.g., `"max"` for long recordings).
3. With the current 4 s join timeout, the partial worker can still be alive (blocked inside `mlx_lock()`) when phase C runs. When it eventually acquires `mlx_lock`, it calls `set_quality_profile("balanced")` **after** phase C has set it to `"max"`.

`set_quality_profile` is not protected by any lock. It modifies `engine.quality_profile` and `engine.current_model` and calls `_mx.clear_cache()` outside of `mlx_lock`. A partial worker thread racing against phase C therefore:
- Switches the model back to `balanced` (clearing the Metal cache) mid-way through a `"max"` transcription.
- At minimum causes the final transcript to run on the wrong model; at worst causes a GPU state corruption.

**Impact:** On recordings with `quality_profile="max"` and `rt_partial_interval_sec` smaller than the max-profile inference time, the final transcript silently uses the `balanced` model. This is a correctness bug triggered on any recording >4 s where the partial worker overlaps with the stop pipeline.

**Fix priority:** HIGH. Merge W1323 (30 s join timeout) as a mitigation; add a dedicated lock or move `set_quality_profile` inside `mlx_lock` scope as a complete fix.

---

### F2 — Memory growth from `recorder._chunks` is unbounded during long recordings; snapshot copies the full accumulation (MED)

**Location:** `KrabEar/backend/recorder.py:39` (`self._chunks: list[np.ndarray]`), `KrabEar/backend/recorder.py:114` (`snapshot_audio`)

**Description:**

`AudioRecorder._worker` appends every incoming audio chunk to `self._chunks` (line 149), which grows for the entire duration of the recording. `snapshot_audio` (line 114) concatenates all chunks, then slices the last `max_duration_sec * sample_rate` samples. The slice is correct for the return value, but `np.concatenate` first allocates a **full-recording-length array** in memory before discarding all but the tail.

For a 30-minute recording at 16 kHz float32:
- `self._chunks` holds approximately `30 × 60 × 16000 × 4 bytes = 115 MB` of audio.
- Each `snapshot_audio` call allocates another ~115 MB temporary array for the concatenation.
- With a 3-second interval, this creates ~20 MB/min of short-lived allocations that Python's GC must reclaim.

On the M4 Max (36 GB RAM) this is not a crash risk, but on a 16 GB device a 2-hour recording session accumulates ~460 MB of `_chunks` plus per-tick allocation spikes, contributing to the memory growth pattern observed in production.

**Note:** This is a pre-existing issue in `recorder.py`, but it directly amplifies the impact of `RealtimePartialTranscriber` — without partial transcription, `snapshot_audio` is called once at stop; with it, it is called every 3 seconds for the entire recording duration.

**Fix recommendation:** Slice `_chunks` during concatenation: compute `max_samples`, count backwards through `self._chunks` (most recent first) to find the minimum set of chunks whose total sample count meets `max_samples`, and concatenate only those. This avoids the full-recording allocation entirely.

---

### F3 — `error_count` resets on every success, allowing sustained intermittent failures to escape WARNING threshold indefinitely (LOW)

**Location:** `KrabEar/backend/realtime_partial.py:166` (`error_count = 0`), lines 182–187 (`_log_error`)

**Description:**

The `_ERROR_WARN_THRESHOLD = 5` log-escalation mechanism resets `error_count = 0` on every successful transcription. If the partial worker encounters a repeating pattern of 4 errors followed by 1 success (e.g., a flaky MLX model that succeeds ~20% of the time), the error counter never reaches 5 and all errors remain at DEBUG level. In a production session where the user does not monitor DEBUG logs, these failures are invisible.

For example: errors at ticks 1, 2, 3, 4 (count=4, DEBUG) → success at tick 5 (count reset to 0) → errors at 6, 7, 8, 9 (count=4, DEBUG) → success again — this pattern can persist for hours without a single WARNING.

The W1143 circuit-breaker fix (not merged) addresses the infinite-loop case but uses the same `error_count` variable. Even after W1143 is merged, the 10-consecutive-error threshold could be defeated by the same intermittent pattern.

**Fix recommendation:** Track a separate `total_error_count` or use a sliding window counter (last N ticks) independent of the reset-on-success logic. Alternatively, emit a WARNING after `total_errors > 20` regardless of recent successes.

---

### F4 — Zero-subscriber emit calls still invoke `event_bus.emit()` regardless of subscriber count (LOW)

**Location:** `KrabEar/backend/realtime_partial.py:169–177` (`_worker` emit block), `KrabEar/backend/event_bus.py:62–81` (`emit`)

**Description:**

`event_bus.emit()` acquires a lock, copies the subscriber list, and iterates over it on every call. When there are zero SSE subscribers (the common case — the Swift overlay is not always open), the worker still:
1. Takes the snapshot (calls `snapshot_audio`, full-buffer concatenation — see F2).
2. Runs `transcribe_preview` (acquires `mlx_lock`, calls Whisper).
3. Calls `event_bus.emit()` which does nothing but acquire/release the lock.

There is no fast path to skip the entire pipeline when `event_bus.subscriber_count() == 0`. For a 30-minute background recording where the user has closed the overlay after the first few seconds, all ~600 partial transcription passes are wasted.

**Scope clarification:** This is distinct from the W1315/F4 finding (which concerned the progress guard threshold). This finding concerns the absence of a subscriber-count check before engaging the STT pipeline.

**Fix recommendation:** At the top of the `_worker` loop, after the `stop_event.wait(interval_sec)` but before `snapshot_audio`, add:
```python
if self._event_bus.subscriber_count() == 0:
    continue
```
This is a pure optimisation — no correctness change — but on long background recordings it eliminates 100% of wasted preview STT calls when no UI is connected.

---

### F5 — Test gap: no coverage for `set_quality_profile` race (F1) in merged test suite (LOW)

**Location:** `KrabEar/tests/test_realtime_partial.py` (188 lines, 21 test methods)

**Description:**

The existing test file covers lifecycle, emission, error resilience, session isolation, and thread safety. However:
- No test verifies that stopping `RealtimePartialTranscriber` before calling `transcriber.transcribe()` with a different quality profile leaves the engine in the expected state.
- No test exercises the concurrent scenario where `stop()` join times out and the partial thread is still alive when a final transcription begins.
- No test verifies `subscriber_count == 0` short-circuit (F4 above).

The W1143/W1200/W1323 branch tests (not merged) add ~1050 lines covering the circuit breaker, privacy gate, and stop timeout — but the `set_quality_profile` race (F1) has no test anywhere.

---

## Updated Wire Status Summary

| Aspect | Status in `codex/krab-ear-v2` |
|--------|-------------------------------|
| Privacy mode gate at start (W1200) | **ABSENT** (PR #1111 open) |
| Privacy mode gate at emit (W1200) | **ABSENT** (PR #1111 open) |
| Circuit breaker on ≥10 errors (W1143) | **ABSENT** (PR #1052 open) |
| stop() 30 s join + warning (W1323) | **ABSENT** (branch open) |
| `set_quality_profile` race on stop overlap | **PRESENT** (new F1, no PR) |
| Unbounded `_chunks` allocation in snapshot | **PRESENT** (new F2, no PR) |
| Intermittent error not escalating to WARNING | **PRESENT** (new F3, no PR) |
| Zero-subscriber skip before STT pipeline | **ABSENT** (new F4 optimisation) |
| Test coverage for quality_profile race | **ABSENT** (new F5 gap) |

---

## Verdict

All three prerequisite fixes (W1200, W1323, W1143) remain unmerged. This audit adds 5 new
residual findings independent of those PRs:

- **F1 HIGH**: `set_quality_profile` TOCTOU race between partial worker and final STT pipeline — correctness bug, silently uses wrong model on stop overlap.
- **F2 MED**: unbounded `_chunks` full-buffer copy in `snapshot_audio` amplified by 3-second partial interval — memory growth on long recordings.
- **F3 LOW**: intermittent error pattern defeats WARNING escalation threshold.
- **F4 LOW**: zero-subscriber short-circuit absent — wasted STT calls on background recordings.
- **F5 LOW**: no test coverage for F1 race scenario.

**Priority merge order:** W1200 (privacy, HIGH) → W1143 (circuit breaker, MED) → W1323 (timeout, MED + enables partial fix for F1) → new F1 fix (quality_profile lock) → F2 recorder optimisation → F3/F4 hardening.
