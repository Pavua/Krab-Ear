# Audit: TranscriptVersionManager — Wave 1040

**File:** `KrabEar/backend/transcript_versioning.py`  
**Date:** 2026-05-26  
**Auditor:** Sub-agent W1040

---

## Summary

`TranscriptVersionManager` is a simple, well-scoped service that stores full-text transcript versions as append-only NDJSON. Overall quality is good — input validation, intra-process thread-safety, and test coverage are solid. Six findings were identified, ranging from high-impact (unbounded storage growth, privacy bypass) to low-impact (missing IPC handler, revert metadata loss).

---

## Findings

### F1 — Unbounded Storage Growth (HIGH)

**Location:** `save_version()` line 101–112; `_next_version_num()` line 69–72.

There is no cap on the number of versions stored per `item_id`. Every `save_version` call appends a full-text copy to `transcript_versions.ndjson` unconditionally. For an item that is repeatedly edited (e.g., via LLM rewrite loops or bulk re-processing), the file grows without limit.

`_next_version_num` does a linear scan of the entire file on every `save_version` call (`_read_all()` + filter). At N total versions across all items this is O(N) per write — a quadratic write pattern as the file grows.

**Risk:** A 10 000-word transcript saved 100 times = ~10 MB per item. `BulkReprocessor` or automated `llm_rewrite` pipelines can trigger hundreds of saves per session with no guard.

**Recommendation:** Add a `max_versions_per_item` cap (default 50) in the constructor; on cap exceeded, drop the oldest non-`stt_raw` version before appending. Also consider computing `_next_version_num` from an in-memory counter populated at init rather than scanning on every write.

---

### F2 — Privacy Mode Does Not Purge Version History (HIGH)

**Location:** `KrabEar/backend/transcript_versioning.py` (entire file); `KrabEar/backend/service.py` lines 921, 976.

When `delete_history_item` or `cleanup_old_history` removes a history entry from `history.ndjson`, the corresponding versions in `transcript_versions.ndjson` are **not deleted**. The two stores are decoupled with no cascading delete.

Similarly, there is no `delete_versions_for_item` IPC handler, and no hook called from `HistoryService.handle_delete_history_item`. A user who enables privacy mode and then deletes a sensitive recording will have the full text of every edit persisted indefinitely in `transcript_versions.ndjson`.

**Recommendation:** Add a `delete_versions_for_item(item_id)` method that rewrites the NDJSON file excluding the target `item_id`. Call it from `HistoryService.handle_delete_history_item` and from `cleanup_old_history` for items that cross the age threshold.

---

### F3 — `diff_versions` Not Exposed via IPC (MEDIUM)

**Location:** `transcript_versioning.py` line 177; `service.py` lines 1077–1079.

`diff_versions` is implemented and tested but has no corresponding IPC handler registered in `service.py`. The three registered handlers are `save_transcript_version`, `get_transcript_versions`, and `revert_transcript_version`. There is no `diff_transcript_versions` entry in the dispatch table.

The Swift `HistoryPanel` cannot call diff without a wired IPC method — the feature is effectively unavailable to the frontend.

**Recommendation:** Add `handle_diff_transcript_versions` IPC handler in `TranscriptVersionManager` and register it as `"diff_transcript_versions"` in `service.py`.

---

### F4 — `revert_to_version` Does Not Persist `reverted_from` Field (MEDIUM)

**Location:** `revert_to_version()` lines 168–175.

`revert_to_version` calls `save_version()` (which appends the record to NDJSON), then adds `reverted_from` to the **in-memory dict** returned to the caller. The `_append` call at line 111 happens inside `save_version` before `reverted_from` is set, so the `reverted_from` metadata is never written to disk.

A reload of the manager (or any new `TranscriptVersionManager` instance on the same data dir) will not see `reverted_from` on the reverted version, losing auditability of the rollback chain.

This is confirmed by the test `test_reverted_from_persists` in `TestTranscriptVersionManagerRevertPersistence`: it only checks that the version count and text are correct after reload — it does **not** check for `reverted_from` on the reloaded record.

**Recommendation:** Either pass `reverted_from` into `save_version` (adding an optional parameter) so it is included in the NDJSON record, or write a second append record that updates the reverted version's metadata.

---

### F5 — Non-Atomic Write Under Concurrent Producers (LOW)

**Location:** `_append()` line 63–67; `save_version()` lines 101–112.

The lock (`self._lock`) is a `threading.Lock` — it serializes writes within a single process. However, `_append` opens the file in `"a"` mode without an `fcntl.flock` or equivalent POSIX file lock. If two separate Python processes (e.g., the REST server and the IPC backend) both hold a `TranscriptVersionManager` pointing to the same `data_dir`, concurrent appends can interleave partial writes or corrupt the NDJSON file.

The sister pattern in `StateStore` (used for `history.ndjson`) uses `filelock.FileLock` for cross-process safety. `TranscriptVersionManager` does not follow this pattern.

**Risk:** Currently only one process instantiates `TranscriptVersionManager` (via `BackendService`), so this is a latent risk rather than an active bug. It becomes real if the REST server ever instantiates its own instance.

**Recommendation:** Wrap `_append` with `filelock.FileLock` on `transcript_versions.ndjson.lock`, matching the pattern used by `StateStore`.

---

### F6 — No Retention Policy / Compaction (LOW)

**Location:** `transcript_versioning.py` (entire file); no compaction method exists.

Unlike `StateStore`, which supports compaction of the NDJSON file (removing tombstoned entries), `TranscriptVersionManager` has no compaction or retention mechanism. The file grows monotonically. There is no `compact()` method, no scheduled cleanup, and no integration with `AutoBackupManager` or `DiskSpaceMonitor`.

Combined with F1 (unbounded versions per item), the file can accumulate stale versions from deleted items indefinitely.

**Recommendation:** Add a `compact(keep_item_ids: set[str])` method that rewrites the file retaining only versions whose `item_id` is in the live history set. Invoke it from `BackendService` startup diagnostics or `cleanup_old_history`.

---

## Wire Status

| IPC Method | Registered | Tested |
|---|---|---|
| `save_transcript_version` | Yes | Yes |
| `get_transcript_versions` | Yes | Yes |
| `revert_transcript_version` | Yes | Yes |
| `diff_transcript_versions` | **No** | Partial (unit test only) |
| `delete_versions_for_item` | **No** | No |

## Test Coverage

44 tests, all passing. Coverage is thorough for the happy path and parameter validation. Missing coverage:
- Privacy/cascading delete (no test for orphaned versions after `delete_history_item`)
- `reverted_from` field persistence after reload (existing test does not assert the field)
- Cross-process concurrent writes (only intra-process threading tested)
- Storage growth under bulk writes (no throughput/size regression test)
