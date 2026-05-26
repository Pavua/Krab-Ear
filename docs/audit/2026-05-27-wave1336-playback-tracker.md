# Wave 1336 Audit: playback_tracker.py (post-W879 residual)

**Date:** 2026-05-27  
**Branch:** `audit/playback-tracker-W1336`  
**Auditor:** W1336 sub-agent  
**Scope:** `KrabEar/backend/playback_tracker.py` — `PlaybackTracker` class  
**Prior audit:** W877 (`docs/audit/2026-05-26-wave877-bookmarks-playback-chain.md`)  
**Fix wave referenced:** W879 (PR #800, commit `c04b0199`)

---

## W879 merge state

**W879 is NOT merged into `codex/krab-ear-v2`.**

`git merge-base --is-ancestor c04b0199 codex/krab-ear-v2` returns false. The fix PR #800 exists on
several feature/audit worktree branches that were cut from it but has not been incorporated into the
main integration branch. The current `codex/krab-ear-v2` tip (`6c900317`, release v2.0.5) still
contains the pre-fix `_save()` using `Path.write_text()`.

The W877 BUG-3 (High) finding therefore remains **open** in the shipped codebase.

---

## Findings

### F1 — HIGH (regression): W879 atomic-write fix not merged

**File:** `KrabEar/backend/playback_tracker.py`, lines 62–72  
**Status:** BUG-3 from W877 audit, fix authored as PR #800, **not in `codex/krab-ear-v2`**

Current `_save()` uses `Path.write_text()`, which truncates before writing. A crash or SIGKILL
between truncation and completion produces a zero-byte or partial JSON file. On next startup
`_load()` silently discards the corrupt file and all playback statistics are lost.

```python
# Current (lines 67-70) — non-atomic:
self._path.write_text(
    json.dumps(self._stats, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
```

The fixed version (commit `c04b0199`) writes to `.tmp`, calls `f.flush()` + `os.fsync()`, then
replaces atomically via `tmp_path.replace(self._path)`. This pattern is already in use in
`RecordingChainManager._save()` and `ExportScheduler._save_schedule()`. The fix is a 12-line
change with no API impact.

**Action:** cherry-pick or re-merge PR #800 into `codex/krab-ear-v2`.

---

### F2 — MEDIUM: `get_never_played()` has no IPC handler — method is unreachable from Swift

**File:** `KrabEar/backend/playback_tracker.py`, lines 150–184; `KrabEar/backend/service.py`, lines 1109–1112

`get_never_played()` is a public method with a doc-comment describing it as a user-facing feature.
There is no `handle_get_never_played` handler in `PlaybackTracker`, and the method name does not
appear in the `service.py` handler dispatch table (`grep "never_played" service.py` returns zero
results). No Swift caller references it either.

The method requires `store: Any` as its first argument (the `StateStore` instance), which is
accessible in `BackendService` but not forwarded. This leaves the feature dead at the IPC level.

**Options:**
1. Add `handle_get_never_played(params)` to `PlaybackTracker` (taking `store` from `BackendService`
   via a bound partial or constructor injection) and register it in the dispatch table.
2. Remove the method if it is not planned for near-term implementation.

---

### F3 — MEDIUM: No `privacy_mode` guard on `record_playback` — tracking continues in privacy mode

**File:** `KrabEar/backend/playback_tracker.py`, lines 84–99; `KrabEar/backend/service.py`, lines 1109–1112

When `privacy_mode_enabled=true` the backend blocks certain data-export operations (timeline SVG,
JSON, iCal exports at lines 3656, 3703, 3748 of `service.py`) with an explicit error. However,
`record_playback` has no equivalent guard: calling `record_playback` while in privacy mode writes
`item_id`, `play_count`, `total_listened_sec`, and a UTC timestamp to `playback_stats.json`.

This leaks behavioural metadata (what the user listened to and for how long) even when they believe
privacy mode suppresses such tracking. `PrivacyAuditLogger` logs enable/disable events but not the
continued tracking. Comparable guards exist in the export handlers.

**Fix:** Check `self._get_runtime_setting("privacy_mode_enabled", False)` (or equivalent) at the
top of `handle_record_playback`; return early without writing if enabled.

---

### F4 — LOW: Orphan playback records accumulate on history deletion

**Files:** `KrabEar/backend/history_service.py:239`, `KrabEar/backend/playback_tracker.py`

`handle_delete_history_item` (history_service.py:244) calls `store.delete_history_item(item_id)`
and `handle_cleanup_old_history` bulk-deletes entries older than N days via tombstones. Neither
path calls `PlaybackTracker` to remove the corresponding stats entry. Over time, every deleted or
aged-out history item leaves a stale key in `playback_stats.json`. The file grows without bound
and `get_never_played()` — if ever wired — could incorrectly exclude items whose history records
are gone but whose playback keys still exist.

**Fix:** After each successful history deletion (or at compaction time), call
`playback_tracker.remove_stats(item_id)` — a method that does not yet exist but would be a
two-line addition: acquire lock, pop key, call `_save()`.

---

### F5 — LOW: No test coverage for the atomic-write path (even after W879 merges)

**File:** `KrabEar/tests/test_playback_tracker.py`

The test suite (742 lines, 10 test classes) covers persistence roundtrip, concurrency, IPC
handlers, and edge cases, but contains no test that verifies the atomic-write pattern. After W879
merges, there is no regression guard to prevent a future refactor from reintroducing `write_text`.

Comparable modules have dedicated tests: `test_export_scheduler_extras.py::TestAtomicWrite`
(lines 201+) asserts that `._save_schedule()` writes to a `.tmp` file then renames.

**Fix:** Add a test class `TestAtomicWrite` that:
1. Patches `os.replace` to raise mid-write, verifies no partial file is left.
2. Verifies the `.tmp` file is created then removed (not left as a stale artefact on success).
3. Verifies the final file is valid JSON after a normal write.

---

### F6 — INFO: `_save()` called inside `self._lock` on every `record_playback`

**File:** `KrabEar/backend/playback_tracker.py`, lines 91–99

Every call to `record_playback` holds `self._lock` for the duration of the file I/O (disk write +
optional fsync after W879). Because `get_playback_stats` and `get_most_replayed` also acquire the
same lock, a slow disk flush will block concurrent reads for the lock duration. This is a pre-existing
design tradeoff (W877 INFO-4) and not a correctness issue; it is included here for completeness since
the W879 fsync makes the critical section longer than before.

No action required; accept as-is unless profiling shows contention at high playback event rates.

---

## Summary table

| ID | Severity | Description |
|----|----------|-------------|
| F1 | **HIGH** | W879 atomic-write fix (PR #800) not merged into `codex/krab-ear-v2`; BUG-3 from W877 remains open |
| F2 | MEDIUM | `get_never_played()` has no IPC handler; unreachable from Swift |
| F3 | MEDIUM | `record_playback` lacks `privacy_mode_enabled` guard; tracks listening behaviour in privacy mode |
| F4 | LOW | History deletion does not remove corresponding playback stats; orphan keys accumulate indefinitely |
| F5 | LOW | No atomic-write test; no regression gate once W879 merges |
| F6 | INFO | `_save()` called inside lock; fsync (post-W879) lengthens critical section |

**Total: 2 High+Medium actionable bugs, 2 low-severity gaps, 1 info.**

---

## Recommended actions

1. **F1 (High, immediate):** Merge or cherry-pick PR #800 into `codex/krab-ear-v2`.
2. **F3 (Medium):** Add `privacy_mode_enabled` check in `handle_record_playback` before writing.
3. **F2 (Medium):** Add `handle_get_never_played` to `PlaybackTracker` with store injection, register in dispatch table, or remove the method.
4. **F4 (Low):** Add `remove_stats(item_id)` to `PlaybackTracker`; call it in `handle_delete_history_item` and `handle_cleanup_old_history`.
5. **F5 (Low):** Add `TestAtomicWrite` test class after F1 is resolved.
