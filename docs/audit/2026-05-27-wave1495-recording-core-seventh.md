# Audit W1495 — RecordingCoreService seventh-pass (post-W1102/W1138/W1139/W1144/W1170/W1177/W1329/W1330/W1342/W1390/W1414/W1469)

**Date:** 2026-05-27
**File:** `KrabEar/backend/recording_core_service.py` (1981 lines)
**Auditor:** W1495 sub-agent (Sonnet 4.6)
**Scope:** Seventh-pass re-audit after 12 claimed prior fixes. Verifies merge state of all 12 branches; confirms W1469-F4 (`handle_transcribe_paths_async` silent-drop) is still unpatched; searches for NEW residual issues. Cap: 5 NEW findings.
**Tip commit:** `f7086279` (`codex/krab-ear-v2`)

---

## Prior Wave Merge State (verified against `codex/krab-ear-v2` tip `f7086279`)

| Wave | Fix description | Branch | Merged into `codex/krab-ear-v2` |
|------|----------------|--------|--------------------------------|
| W1102 | SmartSilenceSkipper wired into engine pipeline + VAD mutex | `wire-smart-silence-skipper-W1102` | **NOT MERGED** |
| W1139 | RSF silence_ranges wired through recording pipeline | `wire-rsf-silence-ranges-W1139` | **NOT MERGED** |
| W1138 | privacy_mode tagging on history items in phase_e | `fix-privacy-mode-recording-W1138` | **NOT MERGED** |
| W1144 | Structured disk-full handler in phase_e | `fix-recording-disk-full-W1144` | **NOT MERGED** |
| W1170 | Audit doc only | `audit-recording-core-W1170` | **MERGED** (docs commit `24ed9f66`) |
| W1177 | phase_c STT crash recovery + audio persist + error_bus push | `fix-phase-c-stt-crash-W1177` | **NOT MERGED** |
| W1329 | Wire RealtimeSilenceFilter into RecordingCoreService | `feat/wire-rsf-recording-core-W1329` | **NOT MERGED** |
| W1330 | RSF cursor advance moved after early-return guard | `fix-rsf-cursor-bug-W1330` | **NOT MERGED** |
| W1342 | Wire JobTracker.get_cancel_event in _cancel_check | `fix-wire-cancel-event-W1342` | **NOT MERGED** |
| W1390 | RSF stale silence_ranges cleared at recording start | `fix-rsf-stale-ranges-W1390` | **NOT MERGED** |
| W1414 | Transcriber wrapper silence_ranges+progress_callback+settings | `fix-transcriber-wrapper-W1414` | **NOT MERGED** (branch exists, tip `1b7898e5`) |
| W1469 | Sixth-pass audit doc | `audit-recording-core-seventh-W1495` N/A | **MERGED** (PR #1355, commit `aa207901`) |

**Summary:** 10 fix branches are NOT merged. W1170 and W1469 are merged (docs only). The RealtimeSilenceFilter IS wired via W878 (lines 212–221 `handle_start_recording`, lines 835–841 `_stop_recording_phase_a`) — this W878 wiring is live on main and is distinct from the W1329 branch.

---

## W1469-F4 Follow-up: `handle_transcribe_paths_async` Silent Drop — NOT PATCHED

**Status: OPEN — unchanged since W1469**

Lines 368–371 of `recording_core_service.py` still contain the silent-drop:

```python
for p in selected_raw:
    resolved = Path(p).expanduser().resolve()
    if any(resolved.is_relative_to(root) for root in allowed_roots):
        selected.append(str(resolved))
    # ← no else branch; paths outside allowlist silently disappear
```

The synchronous `_transcribe_paths_core` at line 1385 hard-stops with an explicit error for the same condition. No fix branch exists for the async divergence.

---

## Findings

### F1 — MED (NEW): `handle_get_recording_state` reads `_session_tracker._active_session` without holding `SessionTracker._lock` — data race

**File:** `KrabEar/backend/recording_core_service.py`, line 288
**Severity:** MED
**Status:** OPEN — NEW finding, no prior wave identified this

```python
# line 288 — no lock held:
active_session = self._session_tracker._active_session
session_id = (active_session.get("session_id", "__live__") if active_session else "__live__")
```

`SessionTracker` guards all reads and writes to `_active_session` with `self._lock` (confirmed: `start_session` line 66, `end_session` line 92, `get_sessions` line 139, `get_session_stats` line 145 all use `with self._lock`). The IPC server is thread-per-connection (`ipc_server.py` line 81): `handle_get_recording_state` can run concurrently with `handle_start_recording` → `session_tracker.start_session()` or `handle_stop_recording` → `session_tracker.end_session()`.

The read at line 288 bypasses the lock entirely:

1. `end_session` holds `_lock` and sets `self._active_session = None` followed by writing the finalized dict.
2. Concurrently, `handle_get_recording_state` reads the unguarded `_active_session` reference.
3. In CPython, dict-reference assignment is effectively atomic (STORE_ATTR under the GIL), so the race typically produces a stale session_id rather than a crash — but the read is conceptually incorrect and becomes unsafe under no-GIL Python 3.13+.

The correct fix is one of:
- Add a `get_active_session_id()` method to `SessionTracker` that acquires `_lock`.
- Read `_active_session` inside `with self._session_tracker._lock:`.

**Fix:** Add `SessionTracker.get_active_session_id() -> str | None` that acquires `_lock` and returns `session.get("session_id")`. Replace the direct `_active_session` access at line 288.

---

### F2 — LOW (NEW): `handle_get_transcribe_progress` ETA formula is doubly wrong — uses completed audio, not remaining; hardcoded 10× RTF

**File:** `KrabEar/backend/recording_core_service.py`, lines 526–533
**Severity:** LOW
**Status:** OPEN — NEW finding, no prior wave identified this

```python
total_audio = 0.0
for it in items_raw:
    dur = it.get("audio_duration_sec") if isinstance(it, dict) else None
    if isinstance(dur, (int, float)):
        total_audio += float(dur)
if total_audio > 0:
    eta_sec = max(0.0, total_audio * 10.0 - elapsed_sec)
```

`items_raw = list(state.get("items") or [])` — this is the list of **completed** items (set only when `status in ("done", "failed", "cancelled")`; during active jobs this comes from `state["items"]` accumulated in `_on_file_done`). So `total_audio` is the sum of audio duration for already-processed files.

Two errors compound:

1. **Wrong operand**: ETA should use *remaining* audio (total - completed), not completed audio. `total_audio * 10.0 - elapsed_sec` produces increasingly large ETAs as more files are processed.
2. **Hardcoded 10.0× RTF**: For short recordings, actual RTF is 0.1–0.5× (Whisper MLX is fast). For 60 s audio with 10.0× RTF, the system claims it will take 600 s — 10–100× the real time.

**Concrete scenario** (10-file job, each file 60 s audio, actual RTF 0.2×):
- After 3 files done in ~36 s: `total_audio = 180 s`, `elapsed_sec = 36 s`
- `eta_sec = max(0, 180 * 10 - 36) = 1764 s` (29 min) — wrong
- Correct: `(10 - 3) * 60 * 0.2 = 84 s` (1.4 min)

**Fix:** Compute `remaining_audio` as `total_estimated_audio - total_audio` (estimating total from `total_files * avg_per_file`) and derive RTF from `elapsed_sec / max(total_audio, 1.0)`. Alternatively: `remaining_files = total_files - len(items_raw)`, `avg_audio = total_audio / max(len(items_raw), 1)`, `eta = remaining_files * avg_audio * (elapsed_sec / max(total_audio, 0.001))`.

---

### F3 — HIGH (carryover W1177 NOT merged): `_stop_recording_phase_c` has no try/except around `transcriber.transcribe()`

**File:** `KrabEar/backend/recording_core_service.py`, lines 1048–1061
**Severity:** HIGH
**Status:** OPEN — W1177 fix in `fix-phase-c-stt-crash-W1177` (tip `29b851a7`) NOT merged; unchanged since W1385/W1469

```python
transcribe_payload = self.transcriber.transcribe(
    audio,
    quality_profile=quality_profile,
    ...
    diarize=True if _diarize_enabled else None,
)
return {"transcribe_payload": transcribe_payload}
```

Any exception (MLX GPU hang, watchdog timeout, OOM on large arrays) propagates unhandled through all five phases. Audio was already freed at recorder.stop() in phase_a — irrecoverable loss. No `STT_FAILED` event is emitted; IPC caller receives opaque `internal_error`.

The W1177 branch adds try/except, persists failed audio to `failed_recordings/<uuid>.wav`, emits `EventType.STT_FAILED`, and returns `{"early_return": {"status": "stt_failed", ...}}`.

**Fix:** Merge W1177 (`fix-phase-c-stt-crash-W1177`).

---

### F4 — MED (carryover W1342 NOT merged): `_cancel_check` uses lock-contended dict polling instead of threading.Event

**File:** `KrabEar/backend/recording_core_service.py`, lines 454–456
**Severity:** MED
**Status:** OPEN — W1342 fix in `fix-wire-cancel-event-W1342` (tip `e328bf53`) NOT merged; unchanged since W1385/W1469

```python
def _cancel_check() -> bool:
    state = self._job_tracker.get(job_id)
    return bool(state and state.get("cancel_requested"))
```

`JobTracker.get()` acquires `_lock` and copies the full state dict on every call. On a 100-file batch job with concurrent `get_transcribe_progress` polling from the Swift progress bar, lock contention adds latency between `cancel()` and detection. W1342 adds per-job `threading.Event`; `_cancel_check` calls `event.is_set()` (lock-free).

`JobTracker` on `codex/krab-ear-v2` has no `get_cancel_event()` method — the `_cancel_events` dict infrastructure is absent.

**Fix:** Merge W1342 (`fix-wire-cancel-event-W1342`).

---

### F5 — MED (carryover W1385-F2 / W1469-F2 NOT fixed): `_stop_recording_phase_c` makes 3 independent `cached_settings()` calls — violates single-snapshot contract

**File:** `KrabEar/backend/recording_core_service.py`, lines 1005, 1012, 1046
**Severity:** MED
**Status:** OPEN — identified in W1385-F2, confirmed in W1469-F2; no fix branch exists

`handle_stop_recording` captures a single settings snapshot at line 227, propagated through `_load_stop_recording_settings` → `sr` dict (used in phases B/D/E). However `_stop_recording_phase_c` bypasses this:

```python
_cached_settings_hw = self._settings_svc.cached_settings()  # line 1005 — hotwords
_cached_settings_ag = self._settings_svc.cached_settings()  # line 1012 — auto_glossary
_phase_c_settings = self._settings_svc.cached_settings()    # line 1046 — diarization
```

The `SettingsService` TTL cache expires every 5 s. A `set_settings` IPC call arriving during STT (1–4 s) can produce a mix where silence-guard thresholds and translation mode use the pre-call snapshot while STT hotwords and diarization use a post-call snapshot. Additionally, `_cached_settings_hw` and `_cached_settings_ag` alias the same returned dict — the second call is redundant. A third call at line 1046 may receive a different TTL snapshot than the first two.

**Fix:** Thread the `settings` dict from `handle_stop_recording` through to `_stop_recording_phase_c`. Extend `_load_stop_recording_settings` to include `stt_hotwords_enabled`, `stt_hotwords`, `auto_glossary_enabled`, `auto_glossary_window_days`, `auto_glossary_top_n`, and `diarization_enabled`. Remove the three independent `cached_settings()` calls from `_stop_recording_phase_c`.

---

## W1469-F4 Status (F4 follow-up as tasked)

Confirmed: **NOT patched**. Lines 368–371 of `handle_transcribe_paths_async` still silently drop paths outside the allowlist. No fix branch exists as of tip `f7086279`. The issue is distinct from `_transcribe_paths_core`'s hard-stop (line 1385): the pre-filter in the async handler strips bad paths silently before `_worker` spawns, so `_transcribe_paths_core`'s own allowlist check never fires for those paths and cannot report them. The two behaviors remain inconsistent.

---

## Test Coverage Gaps

| Gap | Severity | Finding |
|-----|----------|---------|
| `_active_session` lock bypass (F1) | MED | No test for concurrent `handle_get_recording_state` + `start_session`/`end_session` |
| ETA formula (F2) | LOW | No test verifying eta_sec decreases as files complete (in fact it increases — anti-pattern) |
| phase_c STT exception (F3) | HIGH | No test where `transcriber.transcribe()` raises; no `STT_FAILED` event emitted (carryover) |
| cancel detection lag (F4) | MED | No test for lock contention between cancel + concurrent progress polls (carryover) |
| phase_c settings snapshot inconsistency (F5) | MED | No test for settings TTL expiry during active stop_recording pipeline (carryover) |
| async path silent-drop (W1469-F4) | MED | No test verifying out-of-allowlist paths produce errors in async job (carryover) |

---

## Finding Count

| Severity | Count | Findings |
|---|---|---|
| HIGH | 1 | F3 (phase_c STT crash, carryover W1177 not merged) |
| MED | 3 | F1 (session_tracker._active_session lock bypass, NEW), F4 (_cancel_check dict polling, carryover W1342), F5 (settings snapshot inconsistency, carryover W1385-F2/W1469-F2) |
| LOW | 1 | F2 (ETA formula doubly wrong, NEW) |
| **Total NEW** | **2** | F1 (MED), F2 (LOW) |
| **Total** | **5** | (2 NEW + 3 carryover HIGH/MED) |
