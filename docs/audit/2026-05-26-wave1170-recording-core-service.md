# Audit W1170 — RecordingCoreService residual (post-W1134)

**Date:** 2026-05-26
**File:** `KrabEar/backend/recording_core_service.py` (1876 lines)
**Auditor:** W1170 sub-agent (Sonnet 4.6)
**Scope:** Residual issues after W1134 audit, focusing on:
- Phase A/B/C transitions on error (crash mid-recording)
- Double-stop idempotency and concurrent stop race
- Finalize sequence with SmartSilenceSkipper + RSF + AudioDenoiser + GainNormalizer combined
- Memory bounds on long recordings (>30 min)
- Interaction with paused state
- Test coverage gaps

---

## Prior Wave Merge State

| Wave | Fix | Merged into `codex/krab-ear-v2` |
|------|-----|--------------------------------|
| W1102 | SmartSilenceSkipper wired in engine | **NOT MERGED** (branch `wire-smart-silence-skipper-W1102`) |
| W1139 | RSF silence_ranges wired through recording pipeline | **NOT MERGED** (branch `wire-rsf-silence-ranges-W1139`) |
| W1138 | W1134 F2 fix: privacy_mode tagging on history items | **NOT MERGED** (branch `wire-rsf-silence-ranges-W1139` carries this change via `fix(wave1138)`) |
| W1144 | W1134 F5 fix: structured disk-full handler in phase E | **NOT MERGED** (same branch) |
| W1091 | GainNormalizer wired in engine | **NOT MERGED** (branch `decide-gain-normalizer-W1091`) |

All five wiring/fix PRs are unmerged. The current `codex/krab-ear-v2` baseline carries W1134
findings F1, F2, F4, and F5 as live defects.

---

## Findings

### F1 — HIGH: `_stop_recording_phase_c` has no try/except around `transcriber.transcribe()`

**File:** `recording_core_service.py` lines 947–955
**Severity:** HIGH

`_stop_recording_phase_c` calls `self.transcriber.transcribe()` directly with no exception
handling:

```python
transcribe_payload = self.transcriber.transcribe(
    audio,
    quality_profile=quality_profile,
    ...
)
```

`Transcriber.transcribe()` delegates directly to `AudioEngine.transcribe()` which can raise on:
- MLX GPU hang / subprocess watchdog timeout (raises `RuntimeError` from `mlx_subprocess.py`)
- PortAudio stream already closed
- MLX SIGSEGV caught as `RuntimeError` in the watchdog
- Out-of-memory on large audio arrays

When the exception propagates:
1. The audio buffer has **already been freed** by `recorder.stop()` in phase A — the recording
   is irrecoverably lost.
2. No `STT_FAILED` event is emitted to EventBus.
3. No error bus push occurs (no structured error code).
4. The IPC caller receives `{"ok": false, "error": {"code": "internal_error", "message": "..."}}` —
   indistinguishable from a startup failure or serialization error.
5. The Swift side has no way to distinguish "STT crashed" from "recording was never started".

Note: double-stop idempotency is **correct** — `recorder.stop()` acquires `_lock` and returns
`None` if `is_recording=False`, so a second concurrent `handle_stop_recording` returns
`early_return: already_stopped` before reaching phase C. The crash scenario is a single call that
reaches phase C and then the STT engine throws.

**Fix:** Wrap `self.transcriber.transcribe()` in try/except in `_stop_recording_phase_c`. On
exception: emit `EventType.STT_FAILED`, push an error bus event with an appropriate code (e.g.,
`stt.engine_crash`), and return a structured `{"status": "stt_failed", ...}` dict carrying
`transcript_text: ""` so Swift can show a meaningful alert rather than a raw error.

---

### F2 — MED: `AudioRecorder._chunks` list is unbounded — no enforcement of `MAX_DURATION_SEC`

**File:** `KrabEar/backend/recorder.py` lines 39, 149; `KrabEar/core/config.py` line 120
**Severity:** MED

`config.py` defines `MAX_DURATION_SEC: int = 300` (5 minutes), but this constant is **never
imported or enforced** anywhere in the recording pipeline:

- `AudioRecorder._worker` appends every chunk to `self._chunks` indefinitely.
- `RecordingCoreService.handle_start_recording` does not set a timer or sample-count cap.
- `RecordingCoreService._preview_loop` caps the *preview snapshot* to 12 s but not the underlying
  `_chunks` accumulation.

Memory impact (16 kHz, float32, 4 bytes/sample):
- 5 min (design limit): 19 MB
- 30 min: 115 MB
- 1 hour: 230 MB
- 2 hours (meeting with diarization): 460 MB

On an M4 Max (36 GB) this is unlikely to trigger an OOM kill, but:
1. The full audio array is concatenated in `recorder.stop()` (line 84) — at 460 MB, this doubles
   peak RSS during the concatenation.
2. `AudioEngine.transcribe()` then calls `np.asarray(audio)` again — another full copy in some
   paths.
3. GigaAM chunking (`_GIGAAM_MAX_CHUNK_SEC`) and MLX have their own memory pressure on top.
4. There is no user-facing warning when recording approaches the 5-minute design limit.

**Fix:** Enforce `MAX_DURATION_SEC` in `AudioRecorder._worker` by checking elapsed time and setting
`self._stop_event` when exceeded, or in `RecordingCoreService._preview_loop` which already polls
`recorder.get_duration_sec()`. Either path should push an `audio.max_duration_reached` error code.

---

### F3 — MED: Three pipeline preprocessing steps are NOT wired — silently have no effect

**File:** `recording_core_service.py` (entire file); `KrabEar/core/engine.py`
**Severity:** MED

Three preprocessing modules are fully implemented but produce zero effect when users enable them
via settings, because the wiring PRs are not merged into `codex/krab-ear-v2`:

| Module | Setting | Wiring PR | Merged? |
|--------|---------|-----------|---------|
| `SmartSilenceSkipper` | `smart_silence_skip_enabled` | W1102 | NOT MERGED |
| `RealtimeSilenceFilter` | `realtime_silence_filter_enabled` | W1139 | NOT MERGED |
| `GainNormalizer` | `gain_normalization_enabled` | W1091 | NOT MERGED |

All three settings exist in `DEFAULT_SETTINGS` / `config.py`. Users who enable them see no
difference in STT quality because the engine never calls these modules during live recording.

This is the same class of finding as W1134 F4, extended to include GainNormalizer. The combined
impact is that the entire "audio preprocessing chain" (gain → denoiser → VAD → silence-skip →
RSF) is partially operational: `AudioDenoiser` **is** wired (engine.py line 637), but the three
modules above are dead wires.

**Fix:** Merge or rebase W1102, W1139, and W1091 into `codex/krab-ear-v2`. They have no
conflicts with each other; W1102 touches only `engine.py`, W1139 touches `recording_core_service.py`
and `transcriber.py`, W1091 touches only `engine.py`.

---

### F4 — MED: No IPC method to discard/cancel a live recording

**File:** `KrabEar/backend/recording_core_service.py` (handler list); `KrabEar/backend/service.py`
**Severity:** MED

The service exposes `start_recording` and `stop_recording`, but there is no `cancel_recording`
(or `discard_recording`) IPC method. The only way for a user to undo an accidental recording is
to:
1. Call `stop_recording` — which runs the full 5-phase pipeline (STT + translation + history
   persist).
2. Then manually delete the resulting history item via `delete_history_item`.

This is a UX gap with operational consequences:
- If the user starts a recording accidentally in a noisy environment, they cannot escape without
  the background noise being STT'd and persisted.
- The Swift hotkey path (Right Option) has a 300-ms double-tap window for Voice Assistant mode;
  false activation is plausible.
- There is no way to abort a recording-in-progress if the microphone is capturing sensitive audio
  (medical conversation, password dictation).

The `AudioRecorder.stop()` API already supports discarding — it can be called and the result
ignored. A `cancel_recording` handler would call `recorder.stop()`, discard the audio array,
stop `_rt_partial` (already done in phase A), and return `{"status": "cancelled"}` without
running phases B–E.

**Fix:** Add `handle_cancel_recording` to `RecordingCoreService`. Register it in the dispatch
table. Implement as: stop preview worker, stop `_rt_partial`, call `_stop_recorder_guarded()`,
discard audio, return `{"status": "cancelled", "is_recording": False}`.

---

### F5 — LOW: Test coverage misses phase_c exception path, max-duration scenario, and RSF start-fail

**File:** `KrabEar/tests/test_recording_core_service.py` (495 lines)
**Severity:** LOW

Three test gaps identified:

1. **Phase_c STT crash path** — there is no test for the case where `transcriber.transcribe()`
   raises during `handle_stop_recording`. The existing `test_stop_with_speech_returns_ok_or_empty_status`
   uses a mock that always succeeds. A test that injects a `RuntimeError` from the mock transcriber
   would currently confirm the raw exception propagates (regression test for F1).

2. **Long-recording memory** — no test verifies that `_chunks` grows without bound or that
   `MAX_DURATION_SEC` is enforced. A unit test with a mock recorder that accumulates a large chunk
   count would document the contract (or catch a future fix).

3. **RSF start-fail in `handle_start_recording`** — the W1139 wiring adds a try/except for
   `RealtimeSilenceFilter()` construction, but there is no test that verifies the start_recording
   IPC call still returns `{"status": "recording"}` when RSF construction raises. This is a
   standard fault-injection test that would have caught the missing `except` block if it were
   missing.

**Fix:** Add three test methods to `test_recording_core_service.py`:
- `test_stop_recording_stt_crash_returns_error_not_exception`
- `test_recorder_chunks_grow_unbounded_without_max_duration_cap` (documents current behavior)
- `test_start_recording_with_rsf_start_failure_still_returns_recording` (after W1139 merge)

---

## Integration Status Summary (post-W1134, current `codex/krab-ear-v2`)

| Integration | Status |
|---|---|
| IPC handlers wired (8 handlers) | OK |
| Double-stop idempotency | OK — `recorder.stop()` returns `None` when idle |
| Concurrent stop race | OK — `recorder._lock` serializes; second call returns `already_stopped` |
| Start-then-stop race (phase E + start overlap) | OK — `recorder._is_recording` gate prevents data mix |
| RealtimeSilenceFilter wiring | NOT WIRED (W1139 not merged) |
| SmartSilenceSkipper wiring | NOT WIRED (W1102 not merged) |
| GainNormalizer wiring | NOT WIRED (W1091 not merged) |
| privacy_mode tagging on history items | NOT WIRED (W1138 not merged) |
| disk-full structured error in phase E | NOT WIRED (W1144 not merged) |
| phase_c STT crash handling | MISSING — exception propagates raw (F1 above) |
| MAX_DURATION_SEC enforcement | NOT ENFORCED (F2 above) |
| cancel_recording IPC handler | MISSING (F4 above) |
| Test coverage | ADEQUATE for happy path; 3 gaps identified (F5 above) |

---

## Finding Count

| Severity | Count | Findings |
|---|---|---|
| HIGH | 1 | F1 (phase_c STT crash) |
| MED | 3 | F2 (unbounded memory), F3 (3 unwired pipeline steps), F4 (no cancel_recording) |
| LOW | 1 | F5 (test gaps) |
| **Total** | **5** | |

---

## W1134 Finding Carryover Status

| W1134 Finding | Fix PR | Status |
|---|---|---|
| F1 — `_preview_error_count` unsynchronized | None assigned | STILL OPEN |
| F2 — privacy_mode not checked | W1138 | NOT MERGED — fix exists, pending merge |
| F3 — `SessionTracker._active_session` direct access | None assigned | STILL OPEN |
| F4 — RSF + SmartSilenceSkipper not wired | W1102 + W1139 | NOT MERGED — fixes exist, pending merge |
| F5 — disk-full propagates raw | W1144 | NOT MERGED — fix exists, pending merge |
| F6 — test mock uses `vocab.get_words` | None assigned | STILL OPEN |
| F7 — `recorder.start()` not guarded | None assigned | STILL OPEN |
