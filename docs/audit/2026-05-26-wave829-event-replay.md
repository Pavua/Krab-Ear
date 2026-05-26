# Wave 829 — EventReplayManager Audit

**Date:** 2026-05-26  
**File audited:** `KrabEar/backend/event_replay.py`  
**Related tests:** `KrabEar/tests/test_event_replay.py`  
**Integration point:** `KrabEar/backend/service.py` lines 391–392, 1065–1067

---

## Summary

`EventReplayManager` is a thread-safe, in-memory ring-buffer event log with optional NDJSON
file persistence. It is instantiated once by `BackendService` with
`persist_path = data_dir / "event_replay.ndjson"` and exposes three IPC handlers
(`get_event_log`, `get_event_stats`, `replay_events`). The implementation is compact
(229 lines) and well-tested (13 test classes). Four structural issues were found: one
critical, two medium, one low.

---

## File Format

Every persisted line is a JSON object:

```json
{"type": "stt.final", "ts": "2026-05-26T12:00:00+00:00", "data": {...}, "seq": 42}
```

| Field | Type | Notes |
|-------|------|-------|
| `type` | `str` | Event type, free-form string |
| `ts` | `str` | ISO 8601 UTC, second precision |
| `data` | `dict` | Payload; coerced to `{}` if not a dict |
| `seq` | `int` | Monotonically increasing, intra-process only; resets to 0 on restart |

---

## Concurrency Model

A single `threading.Lock` (`self._lock`) is held for:
- `record_event` — buffer append + optional file write
- `get_events` — snapshot copy
- `replay_events` — snapshot copy
- `get_event_stats` — snapshot copy + `total` read
- `clear` — `deque.clear()`
- `close` — file handle close

The snapshot-copy pattern (release lock, iterate copy) is correct and prevents
holding the lock during potentially long CPU iterations. No deadlock path found.

---

## Findings

### CRITICAL-1 — Persist file grows without bound

**Location:** `event_replay.py:64`, `service.py:391–392`

The in-memory ring buffer is bounded at `maxlen=10_000` (hard-coded constant
`_MAX_BUFFER_SIZE`). However the NDJSON persist file at
`data_dir/event_replay.ndjson` is opened in **append mode** (`"a"`) and
**never truncated, rotated, or compacted**. On active systems, every
`record_event()` call writes a new line permanently. A typical Krab Ear session
recording every STT/translate/ping event can produce 100–300 events per minute.
At 200 events/min × 8 h/day × ~150 bytes each ≈ **14 MB/day, ~5 GB/year**
on a single data dir with no cleanup.

`clear()` explicitly documents it "does not delete the persist file" (line 185).
There is no compaction, no rotation trigger, and no `disk_monitor.py`-style guard
targeting this file.

**Risk:** Silent disk exhaustion on long-running deployments. The `DiskSpaceMonitor`
warns at <2 GB free, but by then the file may already be large.

**Recommendation:** Add size-based rotation (e.g., cap at 50 MB, keep last N MB on
rotation) OR honour the ring-buffer boundary by truncating the file on restart to
match the in-memory limit. Simplest fix: open in `"w"` instead of `"a"` on each
new `EventReplayManager` instantiation (one file per process lifetime), letting the
ring buffer eviction serve as the implicit retention policy.

---

### MEDIUM-1 — `close()` not called during `BackendService.close()`

**Location:** `service.py:725–737`

`BackendService.close()` stops `LLMHttpProbe` but does not call
`self._event_replay.close()`. If the process is killed or the service is shut down
normally, the file handle for `event_replay.ndjson` is left open. On POSIX (macOS)
this is mostly harmless — the OS will flush and close the file descriptor on process
exit — but:

1. In unit tests that mock `BackendService`, the file handle may leak across test
   cases (resource warnings).
2. If `GracefulShutdownHandler` flushes other stores on shutdown, the event log
   writes from that period may not be flushed to disk before the FD is closed by
   the OS, although `flush()` is called after each `write()` in `record_event()`,
   so in practice individual writes are safe.

**Recommendation:** Add `self._event_replay.close()` to `BackendService.close()`.

---

### MEDIUM-2 — `seq` counter is not persisted; restarts silently break ordering guarantee

**Location:** `event_replay.py:58, 79, 147–148`

`_seq` starts at `0` on every instantiation. The `replay_events` sort key
`e.get("seq", 0)` is only meaningful within a single process lifetime. After a
restart, the new instance appends events with `seq=1, 2, 3…` to the existing file.
If a caller reads the full persisted log offline and tries to reconstruct ordering
by `seq`, events from different sessions will interleave incorrectly.

In practice this is low risk because the in-memory buffer does not reload from disk
(correctly documented in `TestPersistenceReload`), so `replay_events` only ever sees
events from the current session. The risk surfaces if someone builds a log reader
that trusts `seq` across restarts.

**Recommendation:** Document that `seq` is a per-session sequence number with no
cross-restart ordering guarantee, or add a `session_id` field (e.g., startup UTC
timestamp) to each persisted record.

---

### LOW-1 — `get_event_stats` rate formula is misleading

**Location:** `event_replay.py:174–175`

```python
rate_by_type[t] = round(cnt / 1.0, 2)  # events per minute window
```

`cnt / 1.0` is identical to `float(cnt)` — dividing by `1.0` does not convert
"count in last 60 s" to "events per minute". Because the window is exactly 60 s,
the count already equals the events-per-minute rate numerically, but the formula
is misleading and would silently break if the window were ever changed.

**Recommendation:** Replace with `round(cnt / 60.0 * 60, 2)` with a named
constant `_RATE_WINDOW_SEC = 60`, making the intent explicit.

---

## What Is Correct

- Ring buffer eviction (`deque(maxlen=…)`) correctly bounds memory.
- `_lock` is consistently held for all shared-state mutations; snapshot copy before
  iteration is the standard safe pattern.
- `flush()` after every file write prevents silent data loss on crash.
- `data` is coerced to `{}` on non-dict input, preventing malformed entries.
- Corrupted timestamps in entries are silently skipped in `replay_events` and
  `get_events(since=…)` — correct defensive behaviour.
- `replay_events` sorts by `seq`, not `ts`, handling same-second bursts correctly.
- `handle_replay_events` raises `ValueError` (not a generic exception) on missing
  params — consistent with the IPC error-handling convention.
- `close()` is idempotent (guards `if self._file_handle is not None`).
- Module-level `replay_manager` singleton is created without persistence — safe
  default that avoids creating files when the module is imported outside
  `BackendService` (e.g., tests).

---

## Test Coverage Assessment

`test_event_replay.py` has 13 test classes (43 test methods). Coverage is
comprehensive for in-memory behaviour. The following paths lack dedicated tests:

| Gap | Notes |
|-----|-------|
| File handle leak (MEDIUM-1) | No test verifies `close()` is called from `BackendService.close()` |
| Disk growth (CRITICAL-1) | No test verifies file size stays bounded across many writes |
| Cross-restart seq ordering (MEDIUM-2) | `test_append_to_existing_file` confirms append behaviour but does not check seq collisions |
| Rate formula correctness (LOW-1) | `test_stats_rate_includes_recent` checks presence but not numeric accuracy of rate |

---

## IPC Surface

| IPC method | Handler | Notes |
|---|---|---|
| `get_event_log` | `handle_get_event_log` | `limit` capped to `_MAX_BUFFER_SIZE` (10 000) |
| `get_event_stats` | `handle_get_event_stats` | Returns total, per-type counts, per-type rate |
| `replay_events` | `handle_replay_events` | Requires `from_ts` + `to_ts`; raises `ValueError` if absent |

No IPC method exists to clear the persist file or trigger rotation. `clear()` is
Python-internal only.

---

## Action Items (Priority Order)

| # | Severity | Action |
|---|----------|--------|
| 1 | CRITICAL | Add file size cap / rotation to `event_replay.ndjson`. Simplest: open `"w"` per session, not `"a"`. |
| 2 | MEDIUM | Add `self._event_replay.close()` to `BackendService.close()`. |
| 3 | MEDIUM | Document (or enforce via `session_id` field) that `seq` is per-session only. |
| 4 | LOW | Fix rate formula: `cnt / 1.0` → `cnt / _RATE_WINDOW_SEC * 60` with named constant. |
