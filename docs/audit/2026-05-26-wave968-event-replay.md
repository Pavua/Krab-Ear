# Audit W968: event_replay.py residual issues

**Date:** 2026-05-26  
**Branch:** `docs/audit-event-replay-W968`  
**Files audited:**  
- `KrabEar/backend/event_replay.py`  
- `KrabEar/backend/shutdown_handler.py`  
- `KrabEar/tests/test_event_replay.py`

---

## Executive summary

W832 fix (`open("a") → open("w")`) is on a **PENDING PR branch** (`origin/feature/fix-event-replay-W832`, commit `eb8db610`) and has **NOT been merged into `codex/krab-ear-v2`**. The production branch still has `open("a")`, meaning the unbounded file-growth bug (W829 CRIT-1) is still active. Additionally, the companion `shutdown_handler.py` change (`_close_event_replay` step) from that same PR also has not landed.

5 residual findings follow.

---

## Finding 1 — W832 fix NOT merged: `open("a")` still in production (CRITICAL)

**File:** `KrabEar/backend/event_replay.py` line 64  
**Current code (codex/krab-ear-v2):**
```python
self._file_handle = self._persist_path.open("a", encoding="utf-8")
```

The W832 PR (`origin/feature/fix-event-replay-W832`, commit `eb8db610`) changed this to `open("w")` but that PR was never merged into `codex/krab-ear-v2`. The branch tip of `codex/krab-ear-v2` is `6c900317` (v2.0.5 release); W832 diverged from `7b541388` which predates that.

**Consequence:** `event_replay.ndjson` grows without bound across sessions (~14 MB/day per W829 estimate, ~5 GB/year). The ring buffer (`maxlen=10_000`) evicts old entries from memory but every call to `record_event()` writes to the file unconditionally — including evicted events. Confirmed by running: writing 7 events with `max_buffer=5` produces 7 lines on disk, not 5.

**Action:** Merge or cherry-pick `eb8db610` onto `codex/krab-ear-v2`. Alternatively, add a `RotatingFileHandler`-style byte-limit (e.g., 10 MB max, 1 backup).

**Note on fix correctness:** The `open("w")` approach chosen in W832 is sound for the stated intent (bound file to current session). However, it does erase the previous session's log on every restart — acceptable if the in-memory ring buffer (already transient) is the source of truth. If cross-session replay is ever needed the approach must change to a rotating file. For now the fix is correct.

---

## Finding 2 — Companion shutdown_handler.py change also not merged (HIGH)

**File:** `KrabEar/backend/shutdown_handler.py`  
**Issue:** The W832 PR also added `_close_event_replay()` as shutdown step 6 (with IPC socket becoming step 7), and called `close()` on `EventReplayManager` during graceful shutdown. This change is also absent from `codex/krab-ear-v2`.

**Consequence:** If the backend exits via `GracefulShutdownHandler.shutdown()`, the file handle for `event_replay.ndjson` is never explicitly flushed/closed. Python's runtime GC will eventually close it, but under abnormal termination (SIGKILL after timeout) the last-written events may be lost or the file left in a partially-written state. The `close()` method exists and is correct; it just isn't wired into the shutdown sequence yet.

**Action:** Same as Finding 1 — merge W832 PR.

---

## Finding 3 — Coarse 1-second timestamp resolution loses sub-second ordering (MEDIUM)

**File:** `KrabEar/backend/event_replay.py` lines 27, 75  
```python
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
```

All timestamps are truncated to 1-second granularity. Events recorded within the same second share identical `ts` values. `replay_events()` sorts by `seq` to break ties, which is correct for in-memory replay (seq is monotonically assigned). However, the 1-second resolution creates two problems:

1. **Range boundary ambiguity:** `from_ts <= entry_dt <= to_ts` compares at 1-second precision. An event at `12:00:00.999` recorded as `12:00:00` may appear inside or outside a range depending on whether the caller rounds their timestamp. This makes `replay_events()` "coarse" as noted in the audit checklist.

2. **Persisted file loses seq:** When the NDJSON file is read by an external tool (or a future reload), `seq` is available but only meaningful within one session — seq numbers restart at 0 on every boot. Cross-session ordering requires `ts` which is 1-second resolution only.

**Recommendation:** Change `timespec="seconds"` to `timespec="milliseconds"`. This is backward-compatible (ISO-8601 with milliseconds is parseable by `datetime.fromisoformat()` in Python 3.11+). Low effort, high payoff for debug utility.

---

## Finding 4 — No PII/privacy-mode guard on persisted event data (MEDIUM)

**File:** `KrabEar/backend/event_replay.py` lines 71-87  
**Issue:** `record_event(event_type, data)` persists the full `data` dict to disk without any redaction. The system supports `privacy_mode_enabled` (in `core/config.py:987` and checked in `translation_service.py:96`), but `EventReplayManager` has no awareness of it.

If a caller passes `{"text": "<full transcript>", "confidence": 0.97}` as `data`, the transcript is written verbatim to `event_replay.ndjson`. This is particularly relevant for `stt.final` events which are the most likely candidates for replay recording.

**Current state:** `record_event` is not called anywhere in the production backend except the module-level singleton that is never populated (the `BackendService` instance at line 391 creates its own `EventReplayManager` but never calls `record_event()` on it). So PII leakage is latent, not active. However, any future integration that records STT results will inadvertently persist transcript text.

**Recommendation:** Add a `privacy_mode: bool = False` constructor parameter. When `True`, skip `_file_handle` writes entirely (in-memory buffer still works for debug). Gate on `settings.get("privacy_mode_enabled")` in `BackendService.__init__` when constructing the manager.

---

## Finding 5 — File writes not bounded by ring buffer capacity (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 80-87  
**Issue:** The in-memory ring buffer is capped at `maxlen=max_buffer` (default 10,000). Eviction from the ring buffer is silent — the event leaves memory but was already written to disk. So the NDJSON file records every event ever written to the manager, not just the last 10,000.

This is the root cause of W829 CRIT-1, but it also means even after W832's `open("w")` fix is merged, a single very long session (days of uptime without restart) can still grow the file beyond the ring buffer limit. With 10,000 events at ~200 bytes each that's ~2 MB, which is acceptable — but the mismatch between "ring buffer = 10k" and "file = all events" is surprising and undocumented.

**Recommendation:** Document the invariant explicitly in the class docstring. Optionally add a `_rotate_if_needed()` check after writes when file exceeds a configurable byte threshold.

---

## W832 fix correctness verdict

The `open("a") → open("w")` change in W832 is **architecturally correct** for the intended "session-scoped log" design. It does NOT lose important data because:
- The ring buffer is the source of truth for in-process replay.
- The file is write-only (no read-back on startup).
- `open("w")` bounds the file to current-session events, consistent with the ring-buffer philosophy.

The fix is NOT a regression. It should be merged. The `test_append_to_existing_file` test in the test suite correctly validates the current `open("a")` behavior and will need updating once W832 lands.

---

## Shutdown ordering assessment

With W832 NOT merged: `_close_event_replay` is absent from `shutdown_handler.py`. The IPC socket closes (step 6) before the event replay file is explicitly closed. No events arrive after socket close, so no data is lost — but the file handle relies on GC, which is fragile.

With W832 merged: `_close_event_replay` becomes step 6, socket close step 7. This is the correct ordering since no new events can arrive once the file is flushed and before socket close.

---

## Concurrency assessment

Thread safety is sound. `record_event`, `get_events`, and `replay_events` all acquire `self._lock` before accessing `self._buffer`. The snapshot pattern (`list(self._buffer)`) used in readers prevents iterator invalidation. Concurrent write + replay tests pass without errors.

---

## Test coverage assessment

Coverage is comprehensive. `test_event_replay.py` covers:
- Record/retrieve, field presence, limit, type filter, `since` filter — yes
- `seq` monotonicity — yes
- Ring buffer eviction — yes
- Persistence (NDJSON file) — yes
- Thread safety (10 concurrent writers × 50 events) — yes
- IPC handlers — yes
- `replay_events` range + sort by seq — yes
- Corrupted timestamp entries skipped — yes

Gaps:
- No test for `record_event()` with `privacy_mode` (because the feature doesn't exist yet).
- `test_append_to_existing_file` will break once W832 lands (expected behavior flip: append → truncate on new instance).
- No test for file growth exceeding ring buffer capacity (Finding 5).

---

## Summary table

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | W832 fix (`open("w")`) NOT merged — unbounded file growth active | CRITICAL | Unresolved |
| 2 | `_close_event_replay` in shutdown NOT merged — file handle not explicitly closed | HIGH | Unresolved |
| 3 | 1-second timestamp resolution — coarse range replay + seq unusable cross-session | MEDIUM | Open |
| 4 | No PII/privacy_mode guard — full event data persisted unconditionally | MEDIUM | Latent |
| 5 | File writes not bounded by ring buffer — grows beyond 10k per session | LOW | Design gap |
