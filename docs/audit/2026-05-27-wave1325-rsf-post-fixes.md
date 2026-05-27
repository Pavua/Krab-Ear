# W1325 — RealtimeSilenceFilter post-fix re-audit

**Date:** 2026-05-27
**Branch:** `audit/rsf-post-fixes-W1325`
**Auditor:** W1325 (sub-agent, read-only)
**File audited:** `KrabEar/backend/realtime_silence_filter.py`
**Previous waves:** W1136 (initial audit), W878 (Option A wire), W1139 (silence_ranges into transcribe), W1140 (settings + activate)

---

## Merge State of W1136 / W878 / W1139 / W1140

| Wave | Branch | PR | Merged to `codex/krab-ear-v2` |
|------|--------|----|-------------------------------|
| W1136 (initial audit docs) | `audit-realtime-silence-filter-W1136` | — | **YES** — commit `9aff75bc` (`docs(W1136): audit RealtimeSilenceFilter — 5 findings (2H/2M/1L)`) |
| W878 (Option A wire) | Not found as a separate worktree | — | **NOT CONFIRMED** — no commit referencing W878 found in `codex/krab-ear-v2` log |
| W1139 (silence_ranges into transcribe) | `wire-rsf-silence-ranges-W1139` | pending | **NOT MERGED** — worktree diverged from v2.0.5 (`6c900317`); adds `_checked_up_to_sec` activation + incremental analysis |
| W1140 (settings + activate) | `fix-rsf-settings-W1140` | pending | **NOT MERGED** — worktree diverged from v2.0.5 (`6c900317`); adds `realtime_silence_threshold_db` from settings |

**Summary:** The W1136 audit doc merged. W1139 and W1140 fixes exist as open branches with their single fix-commits each. Neither has been merged. W878 appears not to have a standalone worktree (it may have been incorporated elsewhere or its "Option A wire" was never merged).

---

## Current File State (on `codex/krab-ear-v2`)

The current production file at `KrabEar/backend/realtime_silence_filter.py` is the **pre-fix version** — it does not contain the W1139 `_checked_up_to_sec` activation or the W1140 `realtime_silence_threshold_db` setting read. The `_threshold_db` field is hardcoded to `_DEFAULT_THRESHOLD_DB` at line 47, and `_checked_up_to_sec` is set at `start()` but never updated during `_check_once()`.

The W1139 branch version of `_check_once()` contains the incremental `_checked_up_to_sec` update, but this change is not in production.

---

## New Findings (W1325)

### F1 — HIGH — RSF not instantiated or started in `RecordingCoreService`

**File:** `KrabEar/backend/recording_core_service.py`
**Status:** NEW (not identified in W1136)

`RecordingCoreService.handle_start_recording()` starts `RealtimePartialTranscriber` (line 178) but never instantiates or starts `RealtimeSilenceFilter`. The `stop_recording` pipeline (`_stop_recording_phase_c`, lines 947–955) calls `self.transcriber.transcribe(...)` without a `silence_ranges` argument, even though `engine.py::transcribe()` accepts `silence_ranges: list[tuple[float, float]] | None = None` at line 671.

This means that even if a user sets `realtime_silence_filter_enabled=True`, the filter is never started, no silence ranges are collected, and the `silence_ranges` parameter is always `None` at the transcription call site. The entire feature is silently inert at runtime — not just partially broken.

**Evidence:**
- `recording_core_service.py` imports `RealtimePartialTranscriber` (line 31) but has no import or reference to `RealtimeSilenceFilter`.
- `grep -rn "RealtimeSilenceFilter" KrabEar/backend/` returns only `realtime_silence_filter.py` itself and the lazy import inside `core/engine.py` (`zero_silence_ranges`).
- `engine.py` line 824: `if silence_ranges and isinstance(audio_data, np.ndarray) and not is_preview:` — the guard is correct but can never trigger because the caller never supplies the argument.

**Fix required:** Instantiate `RealtimeSilenceFilter` in `handle_start_recording()` (parallel to how `_rt_partial` is started), call `rsf.stop()` in `handle_stop_recording()` before Phase C, and pass the returned `silence_ranges` into `self.transcriber.transcribe(...)`.

---

### F2 — HIGH — `_checked_up_to_sec` updated before filtering, causing false-zero on first tick

**File:** `KrabEar/backend/realtime_silence_filter.py` (W1140 branch version)
**Status:** NEW logic bug in the proposed fix (W1140 branch)

In the W1140 branch version of `_check_once()` (lines 151–152), `self._checked_up_to_sec` is updated to `total_duration` **before** the `new_ranges` filtering and accumulation:

```python
# Update _checked_up_to_sec to the end of the current window …
with self._lock:
    self._checked_up_to_sec = total_duration   # line 152 — too early

if total_silence < self._max_silence_sec:
    return                                      # early return after already advancing cursor
```

If `total_silence < self._max_silence_sec` (the most common case — no silence to record), the cursor advances but no ranges are stored. On the very next tick:

1. `checked_up_to = total_duration_from_last_tick`
2. `already_analyzed_in_window` may be ≥ new `audio_window.size` (especially when `snapshot_audio` returns the same-length window because the recording is still short)
3. The `if skip_samples >= audio_window.size: return` guard fires, and the tick is entirely skipped.

The result is that after the very first non-silence tick the filter effectively **stops running** until the recording grows past `window_sec` beyond the last cursor position. In a 5-second `window_sec` configuration this creates gaps of up to 5 seconds of unanalyzed audio.

**Correct fix:** Only advance `_checked_up_to_sec` after confirming the tick will not early-return, or — better — unconditionally advance the cursor but do so **after** the `total_silence < _max_silence_sec` check so the early return path does not poison the cursor.

---

### F3 — MED — `_merge_ranges` not called when `total_silence < _max_silence_sec` but existing ranges already stored

**File:** `KrabEar/backend/realtime_silence_filter.py` (current production version, line 132–133)

```python
if total_silence < self._max_silence_sec:
    return
```

This guard is evaluated against `total_silence` — the sum of ALL silence regions in the current window — but individual regions are filtered again at line 137 (`if region.duration_sec < self._max_silence_sec: continue`). The guard is intentionally conservative: if the total silence in the window is less than the per-region threshold, no individual region can qualify, so the early return is mathematically correct.

However, the guard does NOT consider existing `_silence_ranges` from previous ticks. Consider this scenario:

- Tick N: records silence range (0.0, 9.0). `_silence_ranges = [(0.0, 9.0)]`.
- Recording continues. At Tick N+k the window now starts at `window_start_sec=8.0` (because the recording is long), so the 9-second range's tail (8.0–9.0) is within the new window but the dominant silence starts from 8.0 again. `total_silence` only covers the 1-second tail → `total_silence (1.0) < _max_silence_sec (8.0)` → early return.

The existing stored range `(0.0, 9.0)` may now be stale if the user starts speaking at second 9, but the filter will never re-evaluate it. This is a pre-existing issue that neither W1139 nor W1140 addresses — the filter never trims or invalidates stored ranges when speech resumes in the area they cover.

**Impact:** Silence regions that were correct at recording time but become incorrect after speech resumes in their tail will still be passed to `zero_silence_ranges()`, causing those audio samples to be silenced in STT even though speech occurred there.

---

### F4 — MED — `zero_silence_ranges` applies RSF coordinates against the full-length audio but RSF snapshots only a `window_sec` tail

**File:** `KrabEar/backend/realtime_silence_filter.py` (`zero_silence_ranges`, line 170–194) and `KrabEar/core/engine.py` (line 824–833)

`RealtimeSilenceFilter` computes absolute silence coordinates (`abs_start`, `abs_end`) relative to the full recording start (via `window_start_sec + region.start_sec`). This is by design. `zero_silence_ranges()` applies those same coordinates to `audio_data` using `int(start_sec * sample_rate)` indexing.

The engine always passes the **full** captured audio to `transcribe()`. As long as `abs_start` and `abs_end` are correctly computed from the full recording timeline, the indexing is correct.

**The residual risk** is a precision mismatch: `snapshot_audio(max_duration_sec=window_sec)` returns the last `window_sec` worth of audio, and `total_duration` is derived from `len(self._audio) / sample_rate` inside `FakeRecorder` — but the production `AudioRecorder.snapshot_audio()` may return `total_duration` as "elapsed since start of recording" rather than "total samples captured." If `total_duration` in production is based on wall-clock elapsed time (not sample count), a small drift accumulates over long recordings due to sounddevice jitter. After 10 minutes of recording at 16 kHz, a 0.1% clock drift produces ~600 ms of coordinate shift — enough to misalign the zeroed range against real speech.

No test covers the production `AudioRecorder.snapshot_audio()` return value semantics; the existing tests only use `FakeRecorder` where `total_duration = len(audio) / sample_rate` is exact.

---

### F5 — LOW — No integration test for RSF + `SmartSilenceSkipper` co-activation

**File:** `KrabEar/tests/test_realtime_silence.py`, `KrabEar/core/smart_silence_skipper.py`
**Status:** Test gap; no production code interaction either (both disabled by default)

`RealtimeSilenceFilter` zeros samples in-place before Whisper. `SmartSilenceSkipper` (when `SMART_SILENCE_SKIP_ENABLED=True`) **removes** silence regions entirely (compacts audio, changes array length). If both are enabled simultaneously:

1. RSF zeros some regions → Whisper sees silence in those positions.
2. SmartSilenceSkipper runs on the same audio (checking after RSF) and finds the zeroed regions as silence → removes them, shortening the array.
3. The `zero_silence_ranges()` call in engine.py at line 824 occurs **before** any SmartSilenceSkipper step (which is not wired in engine.py at all — `SmartSilenceSkipper` is only referenced in config but has no production call site in engine.py or recording_core_service.py).

Checking `KrabEar/core/engine.py` and `KrabEar/backend/recording_core_service.py`: `SmartSilenceSkipper` is NOT wired into the production pipeline (the class exists and is tested standalone but is never instantiated in service or engine code). Therefore the dual-activation conflict does not currently manifest, but the feature-flag documentation (`SMART_SILENCE_SKIP_ENABLED`) implies it should be usable and the interaction is undefined.

No test covers both `realtime_silence_filter_enabled=True` and `smart_silence_skip_enabled=True`.

---

## Summary Table

| # | Severity | Component | Description |
|---|----------|-----------|-------------|
| F1 | HIGH | `recording_core_service.py` | RSF never instantiated/started — feature is 100% inert at runtime |
| F2 | HIGH | `realtime_silence_filter.py` (W1140 branch) | `_checked_up_to_sec` advanced before early-return check → filter stops after first non-silence tick |
| F3 | MED | `realtime_silence_filter.py` | Stored ranges never invalidated when speech resumes in their tail; stale zeroing possible |
| F4 | MED | `realtime_silence_filter.py` + `engine.py` | Coordinate precision depends on `total_duration` semantics in production `AudioRecorder`; no test covers this |
| F5 | LOW | Test suite | No test for RSF + SmartSilenceSkipper co-activation; SmartSilenceSkipper itself has no production call site |

**Total:** 5 findings (2 HIGH, 2 MED, 1 LOW)

---

## Recommendations

1. **F1 (blocking):** Wire RSF into `RecordingCoreService.handle_start_recording()` alongside `_rt_partial`, store the instance as `self._rsf`, call `self._rsf.stop()` before Phase C in `handle_stop_recording()`, and pass the returned list to `self.transcriber.transcribe(silence_ranges=...)`.

2. **F2 (blocking for W1140):** Move the `_checked_up_to_sec` update to after the `total_silence < _max_silence_sec` early-return check. Alternatively, only update the cursor in the branch where new ranges are actually added.

3. **F3 (deferred):** Add a speech-resume invalidation pass: after each tick, trim stored ranges that overlap with newly-detected speech regions. This can be a follow-up wave.

4. **F4 (deferred):** Add a production integration test that exercises `snapshot_audio()` on the real `AudioRecorder` to verify `total_duration` matches sample-based duration.

5. **F5 (deferred):** Add a unit test that sets both `realtime_silence_filter_enabled=True` and `smart_silence_skip_enabled=True` and confirms no exception and reasonable output.
