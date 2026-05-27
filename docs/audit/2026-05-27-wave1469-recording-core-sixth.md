# Audit W1469 — RecordingCoreService sixth-pass (post-W1102/W1139/W1138/W1144/W1170/W1177/W1329/W1330/W1342/W1390/W1414)

**Date:** 2026-05-27
**File:** `KrabEar/backend/recording_core_service.py` (1981 lines), `KrabEar/backend/transcriber.py`
**Auditor:** W1469 sub-agent (Sonnet 4.6)
**Scope:** Sixth-pass re-audit of RecordingCoreService after 11 claimed prior fixes. Verifies merge state of all 11 branches; checks combined 6-stage pipeline interaction (Denoiser→RSF→GainNorm→SmartSilenceSkipper→Transcribe), error propagation through pipeline, cancellation mid-pipeline, memory bound on long recordings, and test coverage of combined-failure scenarios.
**Tip commit:** `f7086279` (`codex/krab-ear-v2`)

---

## Prior Wave Merge State (verified against `codex/krab-ear-v2` tip `f7086279`)

| Wave | Fix description | Branch | Merged into `codex/krab-ear-v2` |
|------|----------------|--------|--------------------------------|
| W1102 | SmartSilenceSkipper wired into engine pipeline + VAD mutex | `wire-smart-silence-skipper-W1102` | **NOT MERGED** |
| W1139 | RSF silence_ranges wired through recording pipeline into transcribe | `wire-rsf-silence-ranges-W1139` | **NOT MERGED** |
| W1138 | privacy_mode tagging on history items in phase_e | `fix-privacy-mode-recording-W1138` | **NOT MERGED** |
| W1144 | Structured disk-full handler in phase_e (OSError + error_bus push) | `fix-recording-disk-full-W1144` | **NOT MERGED** |
| W1170 | Audit doc only | `audit-recording-core-W1170` | **MERGED** (docs commit `24ed9f66`) |
| W1177 | phase_c STT crash recovery + audio persist + error_bus push | `fix-phase-c-stt-crash-W1177` | **NOT MERGED** |
| W1329 | Wire RealtimeSilenceFilter into RecordingCoreService | `feat/wire-rsf-recording-core-W1329` | **NOT MERGED** |
| W1330 | RSF cursor advance moved after early-return guard | `fix-rsf-cursor-bug-W1330` | **NOT MERGED** |
| W1342 | Wire JobTracker.get_cancel_event in _cancel_check | `fix-wire-cancel-event-W1342` | **NOT MERGED** |
| W1390 | RSF stale silence_ranges cleared at recording start | `fix-rsf-stale-ranges-W1390` | **NOT MERGED** |
| W1414 | Transcriber wrapper silence_ranges+progress_callback+settings forwarded | `fix-transcriber-wrapper-W1414` | **CODE PRESENT** (cherry-picked without merge commit; `git diff` shows 0 lines for recording_core_service.py and transcriber.py — only CI yml diff remains) |

**Summary:** 9 fix branches are NOT merged. W1170 is merged (docs only). W1414 code changes are already on `codex/krab-ear-v2` (cherry-picked) but the branch was never formally merged. The `progress_callback` forwarding in `Transcriber.transcribe()` and the engine bypass removal from `_transcribe_paths_core` are live on main.

---

## Pipeline Component State on `codex/krab-ear-v2`

| Step | Component | Wired? | Notes |
|------|-----------|--------|-------|
| 2.4 | RSF silence zeroing | W878 wires `_rt_silence_filter` instantiation in `handle_start_recording` | YES — filter is started; ranges collected in phase_a |
| 2.5 | AudioDenoiser | YES | `STT_DENOISE_ENABLED` — wired in `engine.py` |
| 3 | VAD prefilter | YES | `STT_VAD_PREFILTER_ENABLED` |
| — | SmartSilenceSkipper (2.6) | NOT WIRED | W1102 not merged |
| — | GainNormalizer | NOT WIRED | W1091 not merged |

Note: W878 wired `RealtimeSilenceFilter` instantiation into `handle_start_recording` (lines 212–221). The filter runs during recording and silence ranges are collected via `_rt_silence_filter.stop()` in `_stop_recording_phase_a` (lines 835–841). These ranges are passed to `_stop_recording_phase_c` (line 250) and on to `transcriber.transcribe(silence_ranges=...)`. This is a W1329-equivalent wiring already live, though it was originally attributed to W1329 (which is not merged as a discrete branch).

---

## Findings

### F1 — HIGH (carryover W1177 NOT merged): `_stop_recording_phase_c` has no try/except around `transcriber.transcribe()`

**File:** `KrabEar/backend/recording_core_service.py`, lines 1048–1061
**Severity:** HIGH
**Status:** OPEN — W1177 fix exists in `fix-phase-c-stt-crash-W1177` but is NOT merged

```python
transcribe_payload = self.transcriber.transcribe(
    audio,
    quality_profile=quality_profile,
    ...
    diarize=True if _diarize_enabled else None,
)

return {"transcribe_payload": transcribe_payload}
```

Any exception from `Transcriber.transcribe()` (MLX GPU hang, watchdog timeout, SIGSEGV propagated from subprocess, OOM on large audio arrays) propagates unhandled through all five phases:

1. The audio buffer has already been freed by `recorder.stop()` in phase_a — recording is irrecoverably lost.
2. `handle_stop_recording` (line 250) calls `phase_c["transcribe_payload"]` — if `phase_c` has no `transcribe_payload` key (exception raised before the return), a `KeyError` would be raised; if the exception propagates out of `_stop_recording_phase_c`, the calling frame receives an unhandled exception.
3. No `STT_FAILED` event is emitted to `EventBus`.
4. No `error_bus` push occurs.
5. The IPC caller receives an opaque `internal_error` response.

The W1177 branch adds `try/except` in `_stop_recording_phase_c`, persists the audio to `failed_recordings/<uuid>.wav`, emits `EventType.STT_FAILED`, and returns a structured `{"early_return": {"status": "stt_failed", ...}}` dict. The `handle_stop_recording` orchestrator in W1177 checks for `early_return` before accessing `phase_c["transcribe_payload"]`.

**Fix:** Merge W1177 (`fix-phase-c-stt-crash-W1177`). After merge, verify `test_stop_recording_stt_crash_returns_structured_error` passes.

---

### F2 — MED (carryover W1385-F2): `_stop_recording_phase_c` makes 3 independent `cached_settings()` calls — settings snapshot inconsistency

**File:** `KrabEar/backend/recording_core_service.py`, lines 1005, 1012, 1046
**Severity:** MED
**Status:** OPEN on `codex/krab-ear-v2`

`handle_stop_recording` captures a settings snapshot at line 227 and passes it through `_load_stop_recording_settings` into the `sr` dict, ensuring phases B/D/E operate on a single consistent snapshot. However `_stop_recording_phase_c` independently fetches settings three times:

```python
_cached_settings_hw = self._settings_svc.cached_settings()  # line 1005 — hotwords
_cached_settings_ag = self._settings_svc.cached_settings()  # line 1012 — auto_glossary
_phase_c_settings = self._settings_svc.cached_settings()    # line 1046 — diarization
```

The `SettingsService` TTL cache expires every 5 seconds. If a `set_settings` call arrives during the STT pipeline (~1–4 s for short recordings), the hotwords/auto-glossary used to build the STT prompt may be drawn from a newer snapshot than the silence/translation settings applied in phase_b. The single-snapshot contract required by `handle_stop_recording` is violated.

Additionally, `_cached_settings_hw` and `_cached_settings_ag` are the same dict object (returned from the same TTL-cached instance), so the second call is redundant — both variables alias the same result. A third call (`_phase_c_settings`) may return a different snapshot if the TTL expires between the first two calls and the third.

**Fix:** Thread the `settings` dict from `handle_stop_recording` through to `_stop_recording_phase_c` as an additional argument, or extend `sr` in `_load_stop_recording_settings` to include `stt_hotwords_enabled`, `stt_hotwords`, `auto_glossary_enabled`, `auto_glossary_window_days`, `auto_glossary_top_n`, and `diarization_enabled` keys. Remove the three independent `cached_settings()` calls in `_stop_recording_phase_c`.

---

### F3 — MED (carryover W1342 NOT merged): `_cancel_check` uses lock-contended dict polling instead of threading.Event

**File:** `KrabEar/backend/recording_core_service.py`, lines 454–456
**Severity:** MED
**Status:** OPEN — W1342 fix in `fix-wire-cancel-event-W1342` is NOT merged

```python
def _cancel_check() -> bool:
    state = self._job_tracker.get(job_id)
    return bool(state and state.get("cancel_requested"))
```

`JobTracker.get()` acquires `_lock`, makes a full dict copy (all job state fields), and releases. On a 100-file batch with concurrent `get_transcribe_progress` polling from Swift's progress bar (3–5 polls/second), the `_lock` contention adds latency between the `cancel_requested` flag being set and `_cancel_check()` detecting it. Under worst-case IPC congestion, cancel detection can lag by 1–2 STT cycles (each 1–4 s for short audio).

The W1342 branch adds a `threading.Event` per job: `cancel()` calls `event.set()`, and `_cancel_check()` calls `event.is_set()` (lock-free, O(1)). Dict-polling remains as fallback for pruned-event cleanup paths.

`JobTracker` on `codex/krab-ear-v2` has no `get_cancel_event()` method — the `_cancel_events` dict infrastructure is entirely absent.

**Fix:** Merge W1342 (`fix-wire-cancel-event-W1342`).

---

### F4 — MED (NEW): `handle_transcribe_paths_async` silently drops invalid paths — inconsistent security contract with synchronous path

**File:** `KrabEar/backend/recording_core_service.py`, lines 362–374 vs lines 1380–1386
**Severity:** MED
**Status:** OPEN on `codex/krab-ear-v2` — NEW finding

`handle_transcribe_paths_async` applies the allowlist but silently discards paths outside the allowed roots:

```python
# handle_transcribe_paths_async (line 362–373)
for p in selected_raw:
    resolved = Path(p).expanduser().resolve()
    if any(resolved.is_relative_to(root) for root in allowed_roots):
        selected.append(str(resolved))
    # ← no else branch; invalid paths are silently dropped
```

`_transcribe_paths_core` (used by the synchronous `handle_transcribe_paths`) applies a hard-stop:

```python
# _transcribe_paths_core (line 1380–1386)
for p in selected_raw:
    resolved = Path(p).expanduser().resolve()
    if any(resolved.is_relative_to(root) for root in allowed_roots):
        selected.append(str(resolved))
    else:
        return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
```

The security contract diverges:
1. **Async path**: a Swift caller sending `["/etc/passwd", "/tmp/real.m4a"]` gets a successful job that transcribes only `/tmp/real.m4a` with no error indicating `/etc/passwd` was rejected. The caller has no way to distinguish "file was rejected" from "file was not found".
2. **Sync path**: the same request fails immediately with an explicit error message for the invalid path.
3. **Confusion**: batch jobs with mixed valid/invalid paths appear to succeed with lower-than-expected file counts, making path traversal probing detectable only via file count discrepancy.

The async allowlist pre-filter in `handle_transcribe_paths_async` is additionally redundant with the one in `_transcribe_paths_core` (lines 1378–1385, called from `_worker`). This double-filter creates a gap: `handle_transcribe_paths_async` strips bad paths silently, then `_transcribe_paths_core` never sees them and cannot report them as errors.

**Fix:** In `handle_transcribe_paths_async`, change the silent drop to an explicit error collection. When a path is outside allowed roots, add it to a pre-rejection list and propagate these as pre-flight errors to the job's initial state (or return `400 Bad Request` immediately before spawning the thread). Align behavior with the sync path.

---

### F5 — LOW (carryover W1170-F1, still open): `_preview_error_count` written without lock — data race between preview thread and IPC reader thread

**File:** `KrabEar/backend/recording_core_service.py`, lines 671, 702, 713, 715 (preview thread); line 127 (`preview_error_count` property)
**Severity:** LOW
**Status:** OPEN — W1170 found this; no fix branch exists for it

`_preview_error_count` is mutated exclusively in `_preview_loop` (which runs on the daemon preview thread) without holding `_preview_lock`:

```python
# preview thread — no lock:
self._preview_error_count += 1          # line 671 (compound read-modify-write)
...
self._preview_error_count = 0           # line 715
```

`preview_error_count` property (line 127) reads without a lock, which is consistent with the write. However:
1. The compound `+= 1` is a non-atomic read-modify-write on CPython (requires GIL acquisition for each bytecode operation). While CPython's GIL makes single-assignment of integers effectively atomic, the pattern is fragile under alternative Python runtimes (PyPy, no-GIL Python 3.13+) and makes the field's threading contract unclear.
2. More importantly, `_preview_error_last_reset_ts` (written at line 714 under no lock: `self._preview_error_last_reset_ts = time.time()`) is used by diagnostics code (`preview_error_last_reset_ts` property at line 131, read without lock). This pair is not atomically updated — a diagnostic snapshot reading both `_preview_error_count` and `_preview_error_last_reset_ts` can see a torn state where `count == 0` but `last_reset_ts == None` (or vice versa).

**Fix:** Acquire `self._preview_lock` around the `_preview_error_count` increment, reset, and `_preview_error_last_reset_ts` assignment in `_preview_loop`. Four lines. The lock is already held for `_preview_text` and `_preview_duration_sec` updates immediately adjacent.

---

## Combined Pipeline Ordering Status (post-W878, current `codex/krab-ear-v2`)

The W878-wired `RealtimeSilenceFilter` is active. Current pipeline order at end of recording:

1. `_stop_recording_phase_a`: `_rt_silence_filter.stop()` → `silence_ranges` collected
2. `_stop_recording_phase_a`: `recorder.stop()` → raw audio array returned
3. `_stop_recording_phase_b`: silence guard + background guard on raw audio
4. `_stop_recording_phase_c`: `transcriber.transcribe(..., silence_ranges=silence_ranges)` → engine zeroes silence regions then runs denoiser → STT

The RSF→Denoiser ordering issue (W1385 F4) applies to the in-engine pipeline in `core/engine.py` (step 2.4 RSF zeroing precedes step 2.5 AudioDenoiser). This is unchanged — still latent.

SmartSilenceSkipper (W1102) and GainNormalizer (W1091) remain unwired.

---

## Test Coverage Gaps (post-W1385)

| Gap | Severity | Description |
|-----|----------|-------------|
| phase_c STT exception (F1) | HIGH | No test where `transcriber.transcribe()` raises; raw exception propagates |
| `_cancel_check` latency under concurrent polling (F3) | MED | No lock-contention test for cancel detection lag |
| Async path silent-drop vs sync hard-stop inconsistency (F4) | MED | No test verifying async path reports error for out-of-allowlist paths |
| `_preview_error_count` torn read with `_preview_error_last_reset_ts` (F5) | LOW | No test covering diagnostic snapshot consistency |

---

## Finding Count

| Severity | Count | Findings |
|---|---|---|
| HIGH | 1 | F1 (phase_c STT crash, carryover W1177 not merged) |
| MED | 3 | F2 (settings snapshot inconsistency, carryover W1385-F2), F3 (cancel_check dict polling, carryover W1342 not merged), F4 (async path silently drops invalid paths, NEW) |
| LOW | 1 | F5 (preview_error_count unsynchronized, carryover W1170-F1) |
| **Total** | **5** | |
