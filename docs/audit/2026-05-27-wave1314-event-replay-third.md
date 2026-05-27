# Audit W1314: event_replay.py — третий re-audit после W968/W969/W970

**Date:** 2026-05-27
**Branch:** `audit/event-replay-third-W1314`
**Auditor:** W1314 sub-agent (Sonnet 4.6)
**Files audited:**
- `KrabEar/backend/event_replay.py` (current `codex/krab-ear-v2`)
- `KrabEar/tests/test_event_replay.py` (current `codex/krab-ear-v2`)
- `KrabEar/backend/service.py` (instantiation site)
- Branches: `fix/event-replay-actually-W969`, `fix/event-replay-privacy-W970`

---

## Merge state of W968/W969/W970

| PR | Branch | State | Base |
|----|--------|-------|------|
| #889 | `docs/audit-event-replay-W968` | **OPEN** | `codex/krab-ear-v2` |
| #892 | `fix/event-replay-actually-W969` | **OPEN** | `codex/krab-ear-v2` |
| #891 | `fix/event-replay-privacy-W970` | **OPEN** | `codex/krab-ear-v2` |

**None of the three PRs are merged.** The production branch (`codex/krab-ear-v2`, tip `6c900317`) still contains the original `open("a")` bug identified as W829 CRIT-1 and re-confirmed in W968.

---

## Finding 1 — W970 merge-order regression: silently re-introduces `open("a")` (CRITICAL)

**Files:** `fix/event-replay-privacy-W970:KrabEar/backend/event_replay.py` line 64

W970 was branched directly from `codex/krab-ear-v2`, NOT from W969. The diff between the two fix branches confirms:

```diff
- # W969 has (correct fix):
  self._file_handle = self._persist_path.open("w", encoding="utf-8")

+ # W970 reverts back to:
  self._file_handle = self._persist_path.open("a", encoding="utf-8")
```

If W970 is merged before W969 (or W970 is rebased onto `codex/krab-ear-v2` after W969 merges), the `open("w")` fix introduced by W969 is silently overwritten by `open("a")` in W970. The W829 CRIT-1 unbounded-growth regression would return undetected — no test guards against this because the test for truncation (`test_session_log_truncates_on_init`) lives only in W969's test branch and is not present in W970.

**Correct merge order required:** W969 must merge first, then W970 must be rebased onto the W969-merged state of `codex/krab-ear-v2`. Alternatively, W970 should incorporate W969's `open("w")` change.

**Action:** Rebase `fix/event-replay-privacy-W970` on top of `fix/event-replay-actually-W969` before merging either. Verify the combined branch has `open("w")` AND `settings_provider` parameter.

---

## Finding 2 — `record_event()` never called in production: EventBus integration absent (HIGH)

**File:** `KrabEar/backend/event_replay.py` docstring lines 7–8; `KrabEar/backend/service.py` lines 390–391

The module docstring states:

> Интеграция: подписывается на EventBus, либо принимает события напрямую через record_event().

However, a full-codebase grep confirms `record_event()` is **never called** from any production code path:

```
grep -rn "record_event" KrabEar/backend/ KrabEar/core/
# → Only the method definition in event_replay.py (line 71)
```

`BackendService.__init__` (service.py:390) creates a `EventReplayManager` instance with `persist_path`, but nothing ever calls `self._event_replay.record_event(...)`. There is no `event_bus.subscribe(...)` call anywhere in service.py or event_replay.py.

**Consequence:** The ring buffer is always empty in production. All three IPC methods (`get_event_log`, `get_event_stats`, `replay_events`) return empty or zero results. The NDJSON file is created (because `open("a")` is called at init) but no events are ever written to it.

**Action:** Either wire EventBus → `record_event` subscription in `BackendService.__init__`, or add direct `self._event_replay.record_event(event_type, data)` calls at key STT/translation/error event emission sites. Until this is done the feature provides no value.

---

## Finding 3 — `test_append_to_existing_file` contradicts W969 design (MEDIUM)

**File:** `KrabEar/tests/test_event_replay.py` lines 334–351 (on `codex/krab-ear-v2`)

The existing test explicitly asserts that a second session **appends** to the existing file:

```python
def test_append_to_existing_file(self):
    """Если файл уже существует, события дописываются (append), а не перезаписываются."""
    ...
    lines = path.read_text().splitlines()
    self.assertEqual(len(lines), 2)  # "first" AND "second" both present
    ...
    self.assertIn("first", types)
    self.assertIn("second", types)
```

W969 changes `open("a")` to `open("w")` (truncate on init), which means this test WILL FAIL once W969 merges. W969's own test branch renames this test to `test_session_log_truncates_on_init` and inverts the assertion (expects only 1 line after second session). But the old test remains in `codex/krab-ear-v2` and will break the test suite post-W969 merge.

**Consequence:** CI failure on the first run after W969 merges, if the old test is not removed/replaced at the same time.

**Action:** The merge commit for W969 must remove `test_append_to_existing_file` from `test_event_replay.py` and ensure `test_session_log_truncates_on_init` (from W969's branch) replaces it.

---

## Finding 4 — `get_events(since=)` exclusive vs `replay_events(from_ts=)` inclusive asymmetry (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 117, 144

```python
# get_events: EXCLUSIVE — event at exactly "since" is NOT returned
if entry_dt <= since_dt:
    continue

# replay_events: INCLUSIVE — event at exactly "from_ts" IS returned
if from_dt <= entry_dt <= to_dt:
    results.append(entry)
```

A caller asking "give me events since T" via `get_events(since=T)` will miss an event timestamped exactly at `T`. A caller using `replay_events(from_ts=T, to_ts=T2)` will include events at exactly `T`. This asymmetry is undocumented. With 1-second timestamp resolution, events recorded within the same second will have identical `ts` and callers using `get_events(since=last_seen_ts)` for polling will systematically miss same-second events.

**Action:** Document the semantic difference in both docstrings. Consider making `get_events(since=)` inclusive (`<` instead of `<=`) to match `replay_events` semantics, but note this is a behavior change requiring a test update.

---

## Finding 5 — `clear()` does not truncate persist file: memory/disk divergence (LOW)

**File:** `KrabEar/backend/event_replay.py` lines 184–187

```python
def clear(self) -> None:
    """Очищает буфер событий (не удаляет файл персистенции)."""
    with self._lock:
        self._buffer.clear()
```

`clear()` empties the in-memory buffer but leaves the file handle open and positioned at the current EOF. After a `clear()`, subsequent `record_event()` calls append new events after the previously-cleared events in the file. The persisted NDJSON file will contain both pre-clear and post-clear events, while the in-memory buffer contains only post-clear events. This divergence means `get_event_log` (memory-only) returns different data than the file on disk.

The docstring acknowledges "не удаляет файл" (does not delete the file) but does not document the divergence between in-memory state and disk content after clear().

**Action:** Document that `clear()` does not sync the persist file. If disk-sync is needed, add `clear_persist=False` parameter that optionally truncates the file and resets the file handle position.

---

## Coverage gaps in current test suite (`codex/krab-ear-v2`)

The following scenarios lack test coverage on the main branch:

- **EventBus subscription wiring** — no test verifying that `BackendService` wires events to `record_event` (because the wiring doesn't exist yet)
- **W970 privacy redaction** — `TestPrivacyModeGuard` exists only in W970's branch, not in `codex/krab-ear-v2`
- **W969 truncation behavior** — `test_session_log_truncates_on_init` exists only in W969's branch
- **Shutdown handler integration** — `TestShutdownIntegration` exists only in W969's branch

---

## Summary table

| # | Severity | Issue | Status on main |
|---|----------|-------|---------------|
| F1 | CRITICAL | W970 merge-order re-introduces `open("a")` if not rebased on W969 | Risk pending merge |
| F2 | HIGH | `record_event()` never called in production; EventBus not wired | Active — feature inert |
| F3 | MEDIUM | `test_append_to_existing_file` will fail post-W969 merge | Active — will break CI |
| F4 | LOW | `get_events(since=)` exclusive vs `replay_events(from_ts=)` inclusive asymmetry | Active |
| F5 | LOW | `clear()` does not truncate persist file — memory/disk divergence undocumented | Active |

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
