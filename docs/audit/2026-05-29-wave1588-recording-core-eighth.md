# Audit W1588 — RecordingCoreService eighth-pass (post-W1572 AutoDeduplicator wiring)

**Date:** 2026-05-29
**File:** `KrabEar/backend/recording_core_service.py` (1974 lines)
**Auditor:** W1588 sub-agent (eighth pass)
**Scope:** Post-W1572 (AutoDeduplicator finally wired — W1247 closed). Verifies W1571 (`_text_similarity` live) and W1572 (constructor + phase_e dedup guard). Finds NEW residual issues. Cap: 5 findings.
**Tip commit:** `7bdbfdfb` (`codex/krab-ear-v2`)

---

## W1571 + W1572 Merge Verification

| Wave | Fix description | Status |
|------|----------------|--------|
| W1571 | Wire `_text_similarity` into `check_duplicate` (W1567 F1 HIGH) | **MERGED** (commit `f64ba99d`, PR #1434) |
| W1572 | Wire `AutoDeduplicator` into `RecordingCoreService.__init__` + phase_e guard (W1567 F2 HIGH) | **MERGED** (commit `a04d03bf`, PR #1433) |

Signature checks:
- `recording_core_service.py:72` — `auto_deduplicator: "AutoDeduplicator | None" = None` present ✓
- `recording_core_service.py:90` — `self._auto_deduplicator = auto_deduplicator` present ✓
- `recording_core_service.py:1144` — `if self._auto_deduplicator is not None and _dedup_enabled and not _privacy_mode:` present ✓
- `auto_deduplication.py:53` — `def _text_similarity(a, b)` present ✓
- `auto_deduplication.py:248` — `sim = _text_similarity(text, cand_text)` call site present ✓

Both fixes confirmed at HEAD.

---

## Findings

### F1 — MED (NEW): `_persist_lock` is declared but never acquired — double-write race window persists

**Severity:** MED
**File:** `KrabEar/backend/recording_core_service.py`, lines 93, 1184
**Status:** NEW — identified for the first time in W1588

`RecordingCoreService.__init__` (line 93) allocates:

```python
# Serializes history persistence in phase_e to prevent double-write races
self._persist_lock = threading.Lock()
```

The comment documents an explicit intent to guard `store.add_history_item(...)` (line 1184) against concurrent invocations. However, `_stop_recording_phase_e` never acquires the lock — no `with self._persist_lock:` block exists anywhere in the file:

```python
# grep result: grep -n "with self._persist_lock" recording_core_service.py → no output
```

The IPC server (`ipc_server.py`) spawns a thread per client connection. If two IPC clients call `stop_recording` concurrently (e.g., realtime partial transcriber + main UI stop), both threads reach `store.add_history_item()` simultaneously and both persist, resulting in two history entries for the same recording.

`store.add_history_item` itself is file-lock-protected at the NDJSON level (StateStore uses `fcntl.flock`), so corruption is prevented. However, the AutoDeduplicator check at line 1144 and the `store.add_history_item` at line 1184 are not atomic — a second thread can pass the dedup check before the first thread has written its item.

**Fix:** Wrap lines 1137–1260 (dedup check through `self.store.add_history_item(...)` and clipboard append) with `with self._persist_lock:`. The lock was purposely created for this role but never wired.

---

### F2 — MED (NEW): `transcribe_paths` bypasses the dedup gate — batch imports silently skip duplicate suppression

**Severity:** MED
**File:** `KrabEar/backend/recording_core_service.py`, line 1438
**Status:** NEW — introduced by W1572 which only wired dedup into `_stop_recording_phase_e`, not into `_transcribe_paths_core`

W1572 added the AutoDeduplicator guard to `_stop_recording_phase_e` (the live-recording path). The batch-import path `_transcribe_paths_core` (lines 1346–1590) calls `self.store.add_history_item(...)` at line 1438 with no dedup check:

```python
history_item = self.store.add_history_item(   # line 1438 — no check_duplicate guard
    text=display_text,
    ...
)
```

When a user imports the same audio file twice (or re-imports a file whose transcript already exists in history), the batch path persists both copies. The `auto_dedup_enabled` setting the user configured applies only to the microphone path.

**Consequence:** Users who rely on `auto_dedup_enabled` to prevent duplicate history entries will find duplicates accumulate silently when importing audio files via the GUI file picker, even with the setting enabled.

**Fix:** Mirror the phase_e pattern in `_transcribe_paths_core` before line 1438: if `self._auto_deduplicator is not None` and `auto_dedup_enabled` is true and privacy mode is off, call `check_duplicate()` and skip `add_history_item` on `is_duplicate=True`.

---

### F3 — LOW (NEW): Dedup timestamp uses local time; history items use `datetime.now().isoformat()` — both local but no UTC marker causes timezone-shift false negatives

**Severity:** LOW
**File:** `KrabEar/backend/recording_core_service.py`, line 1147; `KrabEar/backend/models.py`, line 115
**Status:** NEW

The dedup timestamp passed to `check_duplicate` (line 1147):

```python
_ts_now = _time_mod.strftime("%Y-%m-%dT%H:%M:%S")
```

uses `time.strftime` — local time, no UTC offset, no `Z` suffix.

`HistoryItem.create` (models.py line 115) generates:

```python
ts=datetime.now().isoformat(timespec="seconds"),
```

also local time, no offset.

Both are consistent during normal operation. However, `AutoDeduplicator.check_duplicate` parses timestamps with:

```python
datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
```

This replaces a `Z` suffix with `+00:00` (UTC). When neither the incoming `_ts_now` nor stored `item.ts` contains a `Z` or offset, `fromisoformat` returns a naive datetime in local time, which is compared by `.timestamp()`. The math is correct.

The risk: if a user changes their system timezone between two recordings (e.g., macOS time zone auto-update at 14:04 CEST documented in MEMORY.md as a known production event), stored history items will have timestamps in the old zone and the new recording will have a timestamp in the new zone. `fromisoformat` on a naive datetime string cannot compensate for the zone shift, so both `.timestamp()` calls interpret the strings as current-zone seconds-since-epoch. A 1-hour shift pushes a same-minute recording 3600 seconds outside the 60-second `_TIME_WINDOW_SECONDS` filter, causing the dedup guard to miss the duplicate entirely.

**Fix:** Use `datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")` for `_ts_now` (matches W1243 F2 placeholder pattern); update `HistoryItem.create` to use `datetime.now(timezone.utc).isoformat(timespec="seconds")` consistently. The `AutoDeduplicator` already handles the `Z` → `+00:00` replacement.

---

### F4 — LOW (NEW): Exception swallow in dedup guard is too broad — blocks per-item Sentry reporting

**Severity:** LOW
**File:** `KrabEar/backend/recording_core_service.py`, lines 1181–1182
**Status:** NEW — W1572 introduced the guard with a bare `except Exception` swallow

The dedup guard catches all exceptions silently:

```python
except Exception:
    logger.exception("AutoDedup: check_duplicate завершился с исключением, продолжаем запись")
```

`logger.exception` logs a traceback, which is good. However, `AutoDeduplicator.check_duplicate` can raise for at least three distinct root causes:

1. `store.get_history_page()` raises `OSError` (disk full, file lock timeout) — these indicate backend health problems, not dedup bugs.
2. `datetime.fromisoformat()` raises on malformed ts values — indicates history corruption.
3. `_text_similarity` raises `MemoryError` on very long texts (SequenceMatcher on 10 000-character transcripts allocates O(n²) memory).

All three are swallowed identically, producing the same log line with no error code, no Sentry capture, and no distinction. The Phase B error bus (`backend/error_bus.py`) is not called; there is no `_push_error` invocation. This means production Sentry will never see dedup failures, making silent dedup degradation invisible.

**Fix:** Re-raise `MemoryError` (should crash visibly); call `capture_exception()` from `backend/observability.py` for `OSError` and generic `Exception`; add a `_push_error` call with an appropriate error code (e.g., a new `dedup.check_failed` code in `error_codes.py`) so the toast system can alert the user that dedup is degraded.

---

### F5 — LOW (NEW): `auto_dedup_enabled` is inaccessible from the Swift settings panel — the feature has no UI toggle

**Severity:** LOW
**File:** `native/KrabEarAgent/` (entire directory), `docs/IPC_API_REFERENCE.md`
**Status:** NEW — W1572 wired the backend logic but added no Swift UI counterpart

`auto_dedup_enabled` exists in `DEFAULT_SETTINGS` (config.py line 851, default `False`) and is read in phase_e (recording_core_service.py line 1142). The setting is writable via `set_settings { "auto_dedup_enabled": true }` IPC.

However:
- No Swift file in `native/KrabEarAgent/` references `auto_dedup_enabled` or `autoDedup`.
- `HistoryPanelController+Settings.swift` (the Settings tab) does not include a toggle for this setting.
- The feature is permanently off for all users unless they send a raw IPC command, making it effectively dead for production use.

By contrast, `auto_dedup_threshold` also exists in `DEFAULT_SETTINGS` (config.py line 852) with default `0.82` and is similarly unreachable from the UI.

**Consequence:** The entire dedup infrastructure built over W1243–W1572 (~30 waves) is silently disabled for all production users because there is no UI entrypoint to enable it.

**Fix:** Add an `autoDeduplication` `CollapsibleSectionView` (or at minimum a toggle inside the existing "History" section) in `HistoryPanelController+Settings.swift` that calls `set_settings { "auto_dedup_enabled": true/false }`. Optionally expose the threshold slider (0.7–0.99). This is a 1-file Swift change following the existing extension pattern.

---

## Carryover Status (from W1495 seventh pass)

| Finding | Status in W1588 |
|---------|-----------------|
| W1495-F1: `_active_session` lock bypass in `handle_get_recording_state` (MED) | OPEN — unchanged |
| W1495-F2: ETA formula doubly wrong in `handle_get_transcribe_progress` (LOW) | OPEN — unchanged |
| W1495-F3: phase_c STT crash propagates unhandled (HIGH, carryover W1177) | OPEN — W1177 NOT merged |
| W1495-F4: `_cancel_check` dict polling vs. Event (MED, carryover W1342) | OPEN — W1342 NOT merged |
| W1495-F5: phase_c makes 3 independent `cached_settings()` calls (MED, carryover W1385-F2) | OPEN — unchanged |
| W1495 async silent-drop (MED, carryover W1469-F4) | OPEN — unchanged |

---

## Test Coverage Gaps

| Gap | Severity |
|-----|----------|
| `_persist_lock` never acquired (F1): no test for concurrent `stop_recording` calls from two IPC threads | MED |
| `_transcribe_paths_core` dedup bypass (F2): no test that a re-imported file is deduplicated | MED |
| Timezone shift false negative (F3): no test with new_ts and cand_ts in different offsets | LOW |
| Exception swallow no Sentry (F4): no test where `check_duplicate` raises and verifies error bus is called | LOW |
| No Swift UI toggle (F5): no integration test verifying `auto_dedup_enabled` is reachable via settings panel | LOW |

No existing test in `test_auto_dedup_wiring_W1247.py` tests the exception-swallow path (no `side_effect=Exception()` on the mock). The `_persist_lock` declared-but-unused pattern is not tested anywhere.

---

## Finding Count

| Severity | Count | Findings |
|---|---|---|
| MED | 2 | F1 (`_persist_lock` declared but never acquired), F2 (transcribe_paths bypasses dedup) |
| LOW | 3 | F3 (local-time timestamps under TZ shift), F4 (exception swallow no Sentry), F5 (no Swift UI toggle) |
| **Total NEW** | **5** | All five are NEW — no carryovers among the five findings |
