# Wave 1315 Audit — RealtimePartialTranscriber (re-audit post W1200/W1140/W1139)

**File audited:** `KrabEar/backend/realtime_partial.py`
**Date:** 2026-05-27
**Auditor:** W1315 (sub-agent, read-only)
**Branch:** `audit/realtime-partial-W1315` off `codex/krab-ear-v2`

---

## Merge State of Prerequisite Fixes

| Wave | PR | Branch | Status |
|------|----|--------|--------|
| W1200 — privacy_mode emit gate | #1111 | `fix-realtime-sse-privacy-W1200` | **NOT MERGED** |
| W1140 — RSF settings-driven threshold + `_checked_up_to_sec` | #1050 | `fix-rsf-settings-W1140` | **NOT MERGED** |
| W1139 — wire RSF `silence_ranges` into `transcribe()` | #1060 | `wire-rsf-silence-ranges-W1139` | **NOT MERGED** |
| W1143 — circuit breaker exit on 10 consecutive errors | #1052 | `fix-realtime-partial-circuit-W1143` | **NOT MERGED** |

All four prerequisite fixes are on open PRs and have not been merged into `codex/krab-ear-v2`.
The analysis below audits the code **as it exists on `codex/krab-ear-v2`** (v2.0.5 release head).

---

## Summary

`RealtimePartialTranscriber` is a daemon thread that snapshots the audio buffer every
`rt_partial_interval_sec` (default 3.0 s), runs a preview STT pass, and emits
`realtime.partial_transcript` events via `EventBus`. It is instantiated and started in
`RecordingCoreService.handle_start_recording` (gated on `realtime_partial_enabled=True`)
and stopped in `_stop_recording_phase_a`.

The module is **well-structured** with clean idempotent start/stop, per-error logging
escalation, and good test coverage. However, four unmerged fixes leave real issues in
production, plus one new residual finding is identified below.

---

## Findings

### F1 — privacy_mode gate absent: partial transcripts emitted during privacy-mode recording (HIGH — W1200 NOT merged)

**Location:** `KrabEar/backend/realtime_partial.py:127–180` (`_worker`),
`KrabEar/backend/recording_core_service.py:171–191` (`handle_start_recording`)

`handle_start_recording` checks `realtime_partial_enabled` (line 171) but does **not** check
`privacy_mode_enabled`. When the user activates privacy mode mid-session or starts a recording
with privacy mode already on, `RealtimePartialTranscriber` is launched unconditionally and
emits `realtime.partial_transcript` events containing the raw transcript text.

W1200 (PR #1111) adds: (a) a start-time guard that skips thread launch when
`privacy_mode_enabled=True`, and (b) a `privacy_getter` closure passed to the constructor
that suppresses `event_bus.emit()` within the worker loop if privacy mode is toggled ON
mid-recording. Neither guard is present in the current `codex/krab-ear-v2` code.

**Impact:** Any SSE subscriber (Swift overlay, REST client) receives partial transcripts in
plain text even when the user expects privacy mode to suppress output. This contradicts the
privacy guarantee already enforced for export/clipboard handlers (e.g., `service.py:3656`).

**Fix:** Merge PR #1111 (W1200).

---

### F2 — circuit breaker absent: worker runs indefinitely on permanent STT failure (MED — W1143 NOT merged)

**Location:** `KrabEar/backend/realtime_partial.py:137–159` (`_worker`), lines 182–187 (`_log_error`)

After `_ERROR_WARN_THRESHOLD` (5) consecutive errors, the log level escalates from DEBUG to
WARNING — but the loop **never terminates**. On a GPU hang or permanent `mlx_lock` deadlock,
`transcribe_preview` blocks and then raises repeatedly. The worker keeps retrying at
`interval_sec` (3.0 s) cadence forever, flooding WARNING logs and holding the MLX GPU queue.

W1143 (PR #1052) adds `_MAX_CONSECUTIVE_ERRORS = 10`: after 10 consecutive errors, the worker
breaks out of the loop and emits `realtime.partial_disabled` so the Swift overlay can notify
the user. This fix is not merged.

**Impact:** On a production GPU hang (e.g., MLX hash-table corruption), the partial worker
consumes GPU resources indefinitely without recovery, potentially worsening the hang.
The `stop()` call from `_stop_recording_phase_a` will eventually fire but the 4 s join
timeout may expire before the blocked `transcribe_preview` returns, leaving the thread alive
past `stop()`.

**Fix:** Merge PR #1052 (W1143).

---

### F3 — `stop()` join timeout (4 s) insufficient when mlx_lock contention extends STT beyond 4 s (MED)

**Location:** `KrabEar/backend/realtime_partial.py:107–116` (`stop()`),
`KrabEar/backend/recording_core_service.py:738–744` (`_stop_recording_phase_a`)

`stop()` sets `_stop_event` and calls `self._thread.join(timeout=4.0)`, then unconditionally
sets `self._thread = None` regardless of whether the join succeeded. If the partial worker
is blocked inside `transcribe_preview` waiting for `mlx_lock` (held by the final STT in
`_stop_recording_phase_c`), and the final STT itself takes longer than 4 s (common for the
`max` quality profile on long audio), the join times out. At that point:

1. `self._rt_partial = None` and `self._thread = None` are set (the service thinks the
   worker is gone).
2. The daemon thread is **still alive** and continues to run — it will acquire `mlx_lock`
   after the final STT releases it, run another `transcribe_preview` pass, and call
   `event_bus.emit()` with the **already-completed session_id**.
3. The Swift overlay receives a spurious partial event after `realtime.final_transcript`
   has already been emitted, potentially replacing the final text with a stale partial.

The W1135 audit (2026-05-26) documented this as F5-informational, but examination of the
stop path confirms it can produce observable stale-emit bugs on recordings using the `max`
quality profile.

**Recommendation:** After `join(timeout=4.0)`, log a warning if the thread is still alive.
Optionally increase timeout for `max` profile or add a post-join sentinel check that
suppresses any further `event_bus.emit()` calls from the thread (e.g., via the `privacy_getter`
closure pattern introduced in W1200).

---

### F4 — `last_transcribed_duration` progress guard is a no-op for default interval (LOW)

**Location:** `KrabEar/backend/realtime_partial.py:142–144`

```python
if (duration_sec - last_transcribed_duration) < 0.5:
    continue
```

This guard is intended to skip ticks where the recorder has not advanced (e.g., recorder is
stuck or audio feed paused). With the default `rt_partial_interval_sec = 3.0 s`, the recorder
always advances by at least 3.0 s between ticks, so `duration_sec - last_transcribed_duration`
is always ≥ 3.0 after the first transcription. The 0.5 s threshold is never triggered under
normal operation.

The practical effect: for a 5-hour recording (18000 s / 3 s = 6000 ticks), `transcribe_preview`
is called on **every single tick**, including ticks where the last 8 s of audio is entirely
silence (user paused). The RSF integration (W1139, not merged) would have zeroed silence
regions before STT, providing some relief. Without it, 6000 STT calls are made unconditionally
regardless of audio content.

**Recommendation:** Raise the progress threshold to at least `interval_sec * 0.5` (e.g., 1.5 s
for default interval), or track the previous `text` hash and skip re-emit when text is
unchanged (which would also suppress duplicate partial events at the UI layer).

---

### F5 — RSF `silence_ranges` not wired: silence-region STT waste persists (MED — W1139 NOT merged)

**Location:** `KrabEar/backend/recording_core_service.py:171–191` (`handle_start_recording`),
`KrabEar/backend/transcriber.py` (`transcribe`), `KrabEar/core/engine.py` (`transcribe`)

`RealtimeSilenceFilter` (RSF) accumulates `silence_ranges` — `(start_sec, end_sec)` tuples
for detected long-silence regions — in its `_silence_ranges` list throughout the recording.
W1139 (PR #1060) wires these ranges from RSF's `stop()` return value through
`_stop_recording_phase_a` → `_stop_recording_phase_c` → `transcriber.transcribe(silence_ranges=...)`.

This wiring is absent in `codex/krab-ear-v2`. Even when `realtime_silence_filter_enabled=True`,
the collected `silence_ranges` are discarded on stop, so the final STT call receives the full
unfiltered audio array. `AudioEngine.transcribe` does contain `zero_silence_ranges()` logic
(from Wave 878) but it is never invoked because `silence_ranges` is never passed in.

Additionally, RSF itself has two unmerged fixes: W1140 (threshold hardcoded at -40.0 dB
instead of reading `realtime_silence_threshold_db` setting) and the `_checked_up_to_sec`
field that was initialised but never updated (dead state preventing incremental analysis).

**Impact:** When `realtime_silence_filter_enabled=True`, users expect silence regions to be
excluded from the final transcript. The full audio (including silence) is passed to STT,
increasing inference time and hallucination risk on long recordings.

**Fix:** Merge PR #1060 (W1139), PR #1050 (W1140).

---

## Test Coverage Assessment (post-fix state)

| Test file | Covers | Status |
|-----------|--------|--------|
| `test_realtime_partial.py` | Lifecycle, emission, error resilience, session isolation, thread safety | Present (good) |
| `test_realtime_silence.py` | RSF start/stop, silence detection, threshold | Present (W1140 tests in unmerged branch) |
| `test_realtime_partial_circuit_breaker_W1143.py` | Circuit breaker exit after 10 errors | Unmerged (on W1143 branch) |
| `test_realtime_partial_privacy_W1200.py` | Privacy gate start + emit guard | Unmerged (on W1200 branch) |
| `test_rsf_silence_ranges_wiring_W1139.py` | RSF→transcribe silence_ranges propagation | Unmerged (on W1139 branch) |

The four unmerged test files total ~1050 lines of test coverage that exist on fix branches but
are not in the main codebase. Merging the four open PRs would bring all tests into `codex/krab-ear-v2`.

Missing even after merges: a test that verifies `stop()` join timeout logs a warning when the
thread is still alive (F3).

---

## Wire Status Summary

| Component | Status |
|-----------|--------|
| `RealtimePartialTranscriber` instantiated in `RecordingCoreService` | Correct |
| Privacy mode gate at recording start | **ABSENT** (W1200 not merged) |
| Privacy mode gate at emit time | **ABSENT** (W1200 not merged) |
| Circuit breaker on ≥10 consecutive errors | **ABSENT** (W1143 not merged) |
| RSF `silence_ranges` passed to `transcriber.transcribe()` | **ABSENT** (W1139 not merged) |
| RSF `realtime_silence_threshold_db` setting read | **ABSENT** (W1140 not merged) |
| RSF `_checked_up_to_sec` incremental analysis | **ABSENT** (W1140 not merged) |
| `stop()` join timeout warning on alive thread | **ABSENT** (no PR) |
| `_REALTIME_FINAL_TYPE` constant used in `recording_core_service.py` | **ABSENT** (hardcoded string; W1135 F4) |

---

## Verdict

**4 of 5 findings are directly caused by unmerged PRs** (#1111, #1052, #1060, #1050). The
module code itself is correct; the gap is at the merge-train level. The single new residual
finding (F3: stale emit after join timeout) is a LOW–MED risk requiring documentation and
a one-line log warning, not a code overhaul.

Priority for the merge train: W1200 (HIGH — privacy) > W1143 (MED — circuit breaker) =
W1139+W1140 (MED — RSF wiring) > F3 join-timeout warning (LOW).
