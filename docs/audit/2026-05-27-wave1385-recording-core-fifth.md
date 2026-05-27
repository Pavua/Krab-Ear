# Audit W1385 — RecordingCoreService fifth-pass (post-W1102/W1139/W1138/W1144/W1170/W1177/W1329/W1330/W1342)

**Date:** 2026-05-27
**File:** `KrabEar/backend/recording_core_service.py` (1877 lines), `KrabEar/core/engine.py`, `KrabEar/backend/realtime_silence_filter.py`
**Auditor:** W1385 sub-agent (Sonnet 4.6)
**Scope:** Fifth-pass re-audit of RecordingCoreService after 9 claimed fixes. Verifies merge state of all 9 commits; checks interaction between SmartSilenceSkipper + RSF + GainNormalizer + AudioDenoiser combined pipeline; error propagation when any of 6 preprocessors fail; test coverage of combined-failure scenarios.

---

## Prior Wave Merge State (verified against `codex/krab-ear-v2` tip `6c900317`)

| Wave | Fix description | Branch | Merged into `codex/krab-ear-v2` |
|------|----------------|--------|--------------------------------|
| W1102 | SmartSilenceSkipper wired into engine pipeline + VAD mutex | `wire-smart-silence-skipper-W1102` | **NOT MERGED** |
| W1139 | RSF silence_ranges wired through recording pipeline into transcribe | `wire-rsf-silence-ranges-W1139` | **NOT MERGED** |
| W1138 | privacy_mode tagging on history items in phase_e | `fix-privacy-mode-recording-W1138` | **NOT MERGED** |
| W1144 | Structured disk-full handler in phase_e (OSError + error_bus push) | `fix-recording-disk-full-W1144` | **NOT MERGED** |
| W1170 | Audit doc only | `audit-recording-core-W1170` | **NOT MERGED** |
| W1177 | phase_c STT crash recovery + audio persist + error_bus push | `fix-phase-c-stt-crash-W1177` | **NOT MERGED** |
| W1329 | Wire RealtimeSilenceFilter into RecordingCoreService | `feat/wire-rsf-recording-core-W1329` | **NOT MERGED** |
| W1330 | RSF cursor advance moved after early-return guard | `fix-rsf-cursor-bug-W1330` | **NOT MERGED** |
| W1342 | Wire JobTracker.get_cancel_event in _cancel_check | `fix-wire-cancel-event-W1342` | **NOT MERGED** |

**All 9 fix commits remain unmerged.** The current `codex/krab-ear-v2` carries none of the above fixes.

---

## Pipeline Component State on `codex/krab-ear-v2`

| Step | Component | Wired? | Notes |
|------|-----------|--------|-------|
| 2.4 | RSF silence_ranges zeroing | Dead — no caller passes `silence_ranges` (W1139/W1329 not merged) | `realtime_silence_filter_enabled` |
| 2.5 | AudioDenoiser spectral gating | YES | `STT_DENOISE_ENABLED` |
| 3 | VAD prefilter | YES | `STT_VAD_PREFILTER_ENABLED` |
| — | SmartSilenceSkipper (would be 2.6) | NOT WIRED (W1102 not merged) | `SMART_SILENCE_SKIP_ENABLED` |
| — | GainNormalizer | NOT WIRED (W1091 not merged) | `gain_normalization_enabled` |

---

## Findings

### F1 — MED (NEW): W1329 does not clear `_last_silence_ranges` at recording start — stale RSF ranges applied to next recording

**File:** `KrabEar/backend/recording_core_service.py` (W1329 branch: `feat/wire-rsf-recording-core-W1329`)
**Severity:** MED
**Status:** Latent bug in W1329 branch (not yet on main, but will activate when W1329 merges)

In W1329, `handle_start_recording` sets `self._rsf = None` then optionally starts a new `RealtimeSilenceFilter`. However, it does **not** clear `self._last_silence_ranges`:

```python
# W1329 branch: handle_start_recording (does NOT reset _last_silence_ranges)
self._rsf = None
if bool(settings.get("realtime_silence_filter_enabled", False)):
    ...
    self._rsf.start()
```

In `_stop_recording_phase_a`, the RSF stop writes to `self._last_silence_ranges = silence_ranges` (or `[]` if RSF was None). Then `_stop_recording_phase_c` reads:

```python
_silence_ranges = getattr(self, "_last_silence_ranges", None) or None
```

The `or None` guard converts `[]` to `None` (correct), but if `realtime_silence_filter_enabled` changes from `True` to `False` between two recordings:
- Recording 1 (RSF enabled): `_last_silence_ranges` = `[(0.2, 1.5), (3.0, 4.1)]`
- Recording 2 start: `_last_silence_ranges` is NOT cleared because `handle_start_recording` doesn't reset it
- Recording 2 stop: `_last_silence_ranges` still contains `[(0.2, 1.5), (3.0, 4.1)]`
- These stale second-recording timestamps map to wrong positions in the new audio

**Impact:** Silent portions from recording 1 are zeroed out at the wrong positions in recording 2, corrupting the STT input and potentially causing transcription errors or hallucinations. The bug is masked when RSF is always enabled (each stop overwrites `_last_silence_ranges`) but activates on the first recording after RSF is disabled.

**Fix:** Add `self._last_silence_ranges = []` to `handle_start_recording` before the RSF block (mirrors `self._rsf = None`). One line.

---

### F2 — MED (NEW): `_stop_recording_phase_c` calls `cached_settings()` twice independently — hotwords may use a different TTL snapshot than silence/translation settings

**File:** `KrabEar/backend/recording_core_service.py`, lines 906, 913
**Severity:** MED
**Status:** OPEN on `codex/krab-ear-v2`

`handle_stop_recording` captures a settings snapshot at line 196 and propagates it through `_load_stop_recording_settings` → `sr` dict. All silence/translation/quality settings used in phases B/D/E come from this single snapshot.

However, `_stop_recording_phase_c` does not receive the `sr` dict or the parent `settings`. It independently re-fetches settings twice:

```python
# line 906
_cached_settings_hw = self._settings_svc.cached_settings()  # hotwords
# line 913
_cached_settings_ag = self._settings_svc.cached_settings()  # auto-glossary
```

The `SettingsService` TTL cache expires every 5 seconds. If a user `set_settings` call lands during the ~1-3 second STT pipeline (between phase A/B and phase C), the hotwords/auto-glossary used in STT might differ from the silence-guard thresholds and translation mode already applied. This violates the single-snapshot contract and can produce confusing results: e.g., silence guard ran with old thresholds but STT ran with freshly updated vocabulary.

**Fix:** Thread the `settings` dict (or `sr`) through to `_stop_recording_phase_c` as an argument (it already receives `sr`). Use `sr` to read `stt_hotwords_enabled`, `stt_hotwords`, `auto_glossary_*` keys (adding them to `_load_stop_recording_settings`). Removes two `cached_settings()` calls from phase_c entirely.

---

### F3 — HIGH (carryover W1170-F1, W1177 NOT merged): `_stop_recording_phase_c` has no try/except around `transcriber.transcribe()`

**File:** `KrabEar/backend/recording_core_service.py`, lines 947–955
**Severity:** HIGH
**Status:** OPEN — W1177 fix exists in `fix-phase-c-stt-crash-W1177` but is NOT merged

```python
transcribe_payload = self.transcriber.transcribe(
    audio,
    quality_profile=quality_profile,
    ...
)
```

Any exception from `AudioEngine.transcribe()` (MLX GPU hang, watchdog timeout, OOM) propagates unhandled through all 5 phases, crashing the `handle_stop_recording` IPC call. The audio buffer passed to phase_c was already freed at recorder stop time. No `STT_FAILED` event is emitted, no error bus push, and the Swift side receives an opaque `internal_error`. The W1177 fix (try/except + audio persistence to `failed_recordings/<uuid>.wav` + structured error return) has been validated but remains unmerged.

Additionally: `handle_stop_recording` at line 217–219 does not check for an `early_return` key from `phase_c`, so even if W1177 were to return `{"early_return": ...}`, the current orchestrator would crash on `phase_c["transcribe_payload"]` with a `KeyError`.

**Fix:** Merge W1177 (`fix-phase-c-stt-crash-W1177`). The branch correctly adds both the try/except in `_stop_recording_phase_c` AND the `if "early_return" in phase_c: return phase_c["early_return"]` guard in `handle_stop_recording`.

---

### F4 — MED (NEW): RSF ordering relative to AudioDenoiser in `engine.py` — noise-floor corruption when W1329+W1139 merge

**File:** `KrabEar/core/engine.py`, lines 824–833 (step 2.4), lines 839–845 (step 2.5)
**Severity:** MED
**Status:** Latent — step 2.4 dead code currently (W1139/W1329 not merged). Will activate immediately when W1139 merges.

The engine preprocessing pipeline applies RSF silence zeroing (step 2.4) BEFORE AudioDenoiser (step 2.5). The denoiser's noise-floor estimation uses the first `_NOISE_FLOOR_SAMPLES = 3200` samples (~200 ms at 16 kHz) as the noise template. If RSF has already zeroed that prefix (e.g., recording started with 2 s of silence), `noise_power ≈ 0`, every frequency bin appears "above" the noise floor, and the spectral mask is never applied — the denoiser has no effect on genuinely noisy speech.

Correct order: **Denoiser → RSF zeroing → VAD/SmartSilenceSkipper**.

**Fix:** In `engine.py`, swap steps 2.4 and 2.5 so `_maybe_denoise()` runs first on the raw signal. Renumber step comments. This is a one-reorder fix in the `try:` block at line ~817.

---

### F5 — MED (carryover W1342 NOT merged): `_cancel_check` uses lock-contended dict polling instead of threading.Event

**File:** `KrabEar/backend/recording_core_service.py`, lines 392–394
**Severity:** MED
**Status:** OPEN — W1342 fix in `fix-wire-cancel-event-W1342` is NOT merged

```python
def _cancel_check() -> bool:
    state = self._job_tracker.get(job_id)
    return bool(state and state.get("cancel_requested"))
```

`JobTracker.get()` acquires `_lock`, makes a full dict copy, and releases. On a 100-file batch job with concurrent `get_transcribe_progress` polling (e.g., from Swift progress bar), the `_lock` contention adds latency between cancel signal and detection. W1342 adds `threading.Event` per job: `cancel()` sets the event; `_cancel_check()` calls `event.is_set()` (lock-free, O(1)) with dict-polling as fallback when the event is pruned.

Confirmed: `JobTracker` on main branch has no `get_cancel_event()` method — the entire `_cancel_events` dict infrastructure is absent.

**Fix:** Merge W1342 (`fix-wire-cancel-event-W1342`).

---

### F6 — LOW (NEW): `_stop_recording_phase_e` auto_save_transcripts write is unguarded against ENOSPC

**File:** `KrabEar/backend/recording_core_service.py`, lines 1198–1213
**Severity:** LOW
**Status:** OPEN on `codex/krab-ear-v2`; distinct from W1144 (which targets NDJSON write)

When `auto_save_transcripts=True`, phase_e writes a `.md` transcript file via `TranscriptWriter.write_transcript()`. The outer `except Exception` at line 1213 only logs an exception:

```python
except Exception:
    logger.exception("Не удалось автосохранить транскрибацию в .md")
```

This does not:
1. Distinguish ENOSPC from other I/O errors.
2. Push any error code to `event_bus` / `error_bus`.
3. Set a flag in `result_payload` so the Swift side can notify the user.

W1144 (not merged) fixes phase_e's NDJSON write but not this transcript write. When disk is full, the user sees the transcription in history (NDJSON write also fails unguarded pre-W1144) but gets no actionable notification about the missing `.md` file.

**Fix:** Same pattern as W1144: detect `ENOSPC` (via `getattr(exc, "errno", None) == errno.ENOSPC`), set `result_payload["transcript_save_failed"] = True`, and optionally push `history.write_fail` via `event_bus.emit("krab.error", ...)`.

---

## Combined Pipeline Ordering Summary (when all pending fixes merged)

Correct order when W1091/W1102/W1139/W1329 are merged:

1. (2.1–2.3) Audio load, format conversion, iCloud copy
2. **(2.4) GainNormalizer** — RMS normalization on raw signal (W1091 — not yet assigned)
3. **(2.5) AudioDenoiser** — noise floor on **raw** signal ← MUST come before RSF
4. **(2.6) RSF silence zeroing** — zero silence regions from RSF ← MUST come AFTER denoiser
5. **(2.7) SmartSilenceSkipper** — physically remove silence segments (W1102)
6. (3) VAD prefilter — skipped if `_smart_silence_active` flag set (W1096 F3 mutex)
7. STT inference

Current `engine.py` has step 2.4 (RSF) before step 2.5 (Denoiser) — wrong order when RSF activates.

---

## Test Coverage Gaps

| Gap | Severity | Description |
|-----|----------|-------------|
| `_last_silence_ranges` stale across recordings (F1) | MED | No test for "RSF enabled then disabled → stale ranges on next stop" |
| `_stop_recording_phase_c` STT crash (F3) | HIGH | No test for STT exception path (W1177 unmerged) |
| Denoiser-before-RSF ordering (F4) | MED | No integration test for `zero_silence_ranges` + AudioDenoiser sequence |
| `_cancel_check` thread-safety (F5) | MED | No test for concurrent cancel + progress_poll under lock contention |
| `auto_save_transcripts` ENOSPC (F6) | LOW | No test for `TranscriptWriter` write failure propagation to result_payload |

---

## Finding Count

| Severity | Count | Findings |
|---|---|---|
| HIGH | 1 | F3 (STT crash, carryover W1177 not merged) |
| MED | 4 | F1 (stale `_last_silence_ranges`, NEW), F2 (settings snapshot inconsistency, NEW), F4 (RSF/denoiser ordering, carryover+confirmed), F5 (cancel_check dict polling, carryover W1342 not merged) |
| LOW | 1 | F6 (auto_save_transcripts ENOSPC, NEW) |
| **Total** | **6** | |
