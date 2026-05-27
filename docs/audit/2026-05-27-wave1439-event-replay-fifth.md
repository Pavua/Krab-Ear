# Audit W1439: event_replay.py — пятый re-audit (after W832/W969/W970/W1316/W1317/W1362)

**Date:** 2026-05-27
**Branch:** `audit/event-replay-fifth-W1439`
**Auditor:** W1439 sub-agent (Sonnet 4.6)
**Files audited:**
- `KrabEar/backend/event_replay.py` (253 lines, `origin/codex/krab-ear-v2` tip `4eb8356f`)
- `KrabEar/backend/event_bus.py` (139 lines)
- `KrabEar/backend/service.py` (lines 448–455, EventReplayManager wiring)
- `KrabEar/tests/test_event_replay.py` (669 lines, 45 tests)
- `KrabEar/tests/test_event_bus_event_replay_wiring_W1317.py` (9 tests)

---

## Merge state of prior fixes

| Wave | PR | Branch | State | Notes |
|------|----|--------|-------|-------|
| W832 | #759 | `feature/fix-event-replay-W832` | **MERGED** | `open("w")` — unbounded growth fix |
| W969 | #892 | `fix/event-replay-actually-W969` | **MERGED** | W832 re-confirm + shutdown close |
| W970 | #891 | `fix/event-replay-privacy-W970` | **MERGED** | Privacy redaction guard + settings_provider |
| W1317 | #1222 | `wire-event-replay-W1317` | **MERGED** 2026-05-27 | EventBus → record_event wiring (9 tests) |
| W1316 | #1220 | `fix-event-replay-mode-constant-W1316` | **OPEN** | Open-mode `_REPLAY_LOG_OPEN_MODE` constant |
| W1362 | — | `audit/event-replay-fourth-W1362` | **NOT COMMITTED** | Branch created, no commits, no doc |

**4 of 6 prior fixes are merged.** W1317 merged today (2026-05-27T01:36Z). The fifth audit operates on the post-W1317 production state.

### W969/W970/W1317 merge verification

The three critical fixes are confirmed present on `origin/codex/krab-ear-v2`:

```
git log --oneline origin/codex/krab-ear-v2 | grep -E "W969|W970|W1317"
# ac1c0684 fix(wave970): event_replay privacy_mode guard ... (#891)
# 6d725efe fix(wave969): actually merge W832 event_replay open(w) ... (#892)
# b3160d8e fix(wave1317): wire EventBus.emit → event_replay.record_event ... (#1222)
```

`event_bus.py` now has `self._event_replay: Any | None = None` (line 42) and forwards all emits:
```python
if self._event_replay is not None:
    self._event_replay.record_event(event_type, payload)
```

`service.py` wires this at line 455:
```python
event_bus._event_replay = self._event_replay
```

`event_replay.py` opens with `"w"` (line 70) and has `_is_privacy_mode()` guard (line 77). All three are working.

---

## Finding 1 — `get_event_stats()` silently omits stale types from `rate_per_minute_by_type` (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 187–204

`rate_per_minute_by_type` is built only from `minute_counts` — events inside the last 60-second window. Types that have total `counts_by_type` > 0 but no recent events are **absent from `rate_per_minute_by_type`**, not present with value `0.0`.

```python
for t, cnt in minute_counts.items():
    rate_by_type[t] = round(cnt / 1.0, 2)  # only types with recent events appear
```

A consumer receiving `{"counts_by_type": {"stt.final": 5}, "rate_per_minute_by_type": {}}` cannot distinguish "zero recent rate" (event type existed but was idle) from "type never seen in this window". For a live dashboard this creates a visual gap where a previously active event type disappears from the rate panel.

**Severity:** LOW — cosmetic/UX; ring-buffer is functionally correct.

**Fix:** Populate `rate_by_type[t] = 0.0` for all types in `counts_by_type` that are absent from `minute_counts`:
```python
for t in counts_by_type:
    if t not in rate_by_type:
        rate_by_type[t] = 0.0
```

---

## Finding 2 — `handle_get_event_log()` exposes raw `ValueError` on non-integer `limit` (MED)

**File:** `KrabEar/backend/event_replay.py` line 232

```python
limit=int(params.get("limit", 100)),
```

If `limit` is a non-numeric string (e.g., `{"limit": "fast"}`) the bare `int()` raises `ValueError: invalid literal for int() with base 10: 'fast'`. The IPC dispatch catches generic exceptions and wraps them as IPC errors, but the error message leaks internal implementation detail (`int()` conversion) rather than a helpful user-facing message.

Verification:
```python
mgr.handle_get_event_log({"limit": "bad_value"})
# → ValueError: invalid literal for int() with base 10: 'bad_value'
```

**Severity:** MED — malformed IPC call causes unformatted exception instead of structured error. No security risk (local Unix socket only), but breaks the uniform IPC error contract.

**Fix:**
```python
try:
    limit = int(params.get("limit", 100))
except (TypeError, ValueError):
    limit = 100
```

Or use `InputSanitizer` which already exists at `KrabEar/backend/input_sanitizer.py`.

---

## Finding 3 — `clear()` diverges in-memory buffer from persist file (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 208–211

`clear()` empties the in-memory `deque` but does **not** truncate the persist file. After `clear()`, `get_events()` returns `[]` but the NDJSON file still contains the cleared events:

```python
mgr.record_event("before_clear", {})
mgr.clear()
mgr.record_event("after_clear", {})
mgr.close()
# File has 2 lines: "before_clear" + "after_clear"
# In-memory only ever had: "after_clear"
```

The docstring says "не удаляет файл персистенции" (does not delete the persist file), so this is intentional. However, the state divergence means that loading the NDJSON file for forensic analysis would show events that `get_events()` no longer reports. An operator calling `clear()` to "wipe the log" would find stale events still on disk.

**Severity:** LOW — docstring acknowledges the behavior, but the implication (disk contains more than buffer) is surprising for a "clear" operation. No data integrity violation.

**Fix (optional):** If disk-side clear is wanted, add a `truncate_file` param:
```python
def clear(self, truncate_file: bool = False) -> None:
    with self._lock:
        self._buffer.clear()
        if truncate_file and self._file_handle is not None:
            self._file_handle.seek(0)
            self._file_handle.truncate()
```

---

## Finding 4 — Module-level `replay_manager` singleton is dead code (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 250–252

```python
# Глобальный синглтон — создаётся без персистенции; BackendService может
# переопределить путь при инициализации.
replay_manager = EventReplayManager()
```

A full codebase grep confirms `replay_manager` is **never imported or used** outside `event_replay.py` itself:

```
grep -rn "replay_manager" KrabEar/ --include="*.py" | grep -v event_replay.py
# → (no output)
```

`BackendService` creates its own `EventReplayManager` instance at `service.py:448` and does not reference the singleton. The singleton is created without `persist_path` and without `settings_provider`, so if it were ever used inadvertently, privacy redaction would be absent and events would silently go nowhere.

**Severity:** LOW — no runtime impact, but: (a) misleading comment says "BackendService может переопределить путь" which it never does for this object; (b) the no-settings-provider singleton would silently skip privacy redaction; (c) adds cognitive overhead.

**Fix:** Remove the singleton and its comment from the module bottom.

---

## Finding 5 — W1316 open-mode constant (PR #1220) remains unmerged — protection is comment-only (LOW)

**File:** `KrabEar/backend/event_replay.py` line 70

The current production code uses a string literal:
```python
self._file_handle = self._persist_path.open("w", encoding="utf-8")
```

W1316 (PR #1220, OPEN) proposed extracting this to:
```python
_REPLAY_LOG_OPEN_MODE = "w"  # CRITICAL: must not be "a" — see W829/W969
```

Without the named constant, the only protection against reverting to `"a"` (which caused ~5 GB/year unbounded growth) is the inline comment. The comment is informational but not machine-verifiable — no CI test asserts `open("w")` is used, and a reviewer could miss it during conflict resolution.

The W1316 branch also added two regression tests:
- `test_replay_log_open_mode_is_w_not_a` — asserts `_REPLAY_LOG_OPEN_MODE == "w"` at import time
- `test_open_replay_log_uses_constant` — asserts the helper overwrites (not appends)

These tests remain absent from `origin/codex/krab-ear-v2`.

**Severity:** LOW — `open("w")` is already correct in production; the risk is only future regression. However the constant + tests provide a safety net that is currently absent.

**Action:** Merge PR #1220 or cherry-pick the constant + two regression tests independently.

---

## Test coverage summary

After W1317 merge, 45 + 9 = **54 tests** cover `event_replay.py` functionality:
- `test_event_replay.py`: 45 tests — basic CRUD, replay, stats, persistence, thread safety, privacy, shutdown integration
- `test_event_bus_event_replay_wiring_W1317.py`: 9 tests — EventBus → replay forwarding

**Not covered by any test:**
- `handle_get_event_log()` with non-integer `limit` (F2 — unguarded `int()` cast)
- `get_event_stats()` stale-type zero-rate gap (F1)
- `clear()` disk/memory divergence behavior (F3)
- Module-level `replay_manager` singleton (F4 — dead, but no test documents its unused state)

---

## Prior audit W1362 (fourth) status

Branch `audit/event-replay-fourth-W1362` exists locally but contains **zero commits** beyond the `codex/krab-ear-v2` base. No audit doc was written. The fourth audit round was skipped; this W1439 audit is the true fourth substantive audit.

---

## Summary table

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| F1 | LOW | `get_event_stats()` omits stale types from `rate_per_minute_by_type` | NEW |
| F2 | MED | `handle_get_event_log()` exposes raw `ValueError` on bad `limit` param | NEW |
| F3 | LOW | `clear()` doesn't truncate persist file — buffer/disk diverge | NEW |
| F4 | LOW | Module-level `replay_manager` singleton is dead code (no callers) | NEW |
| F5 | LOW | W1316 PR #1220 unmerged — `"w"` open-mode constant absent, protection comment-only | CARRY-OVER |
