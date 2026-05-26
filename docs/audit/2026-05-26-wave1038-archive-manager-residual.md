# Wave 1038 — ArchiveManager Residual Audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/archive_manager.py`  
**Scope:** Post-W896 residual issues (W896 write-first-then-delete is NOT re-reported here)  
**Status:** 6 findings, severity LOW–MEDIUM

---

## Finding 1 — MEDIUM: No archive file size cap or rotation

**Location:** `archive_manager.py` — `archive_items()`, `_append_ndjson()`

The archive file is an unbounded append-only NDJSON file. There is no size cap, entry count cap, or rotation policy. On a production system archiving items over months, `archive.ndjson` grows without bound. `get_archive_stats()` reports `size_mb` but nothing enforces a limit.

`list_archived(limit=500)` hard-caps the _response_ but the underlying file is never trimmed. After ~50 k archived items the `_read_archive()` call — which reads the entire file on every `list_archived`, `unarchive_items`, or `get_archive_stats` call — becomes a blocking O(N) disk read under the global `_lock`, stalling other IPC threads.

**Fix:** Add `max_archive_entries` setting (default 10 000). When exceeded on `archive_items`, drop the oldest N entries (rewrite via `_rewrite_archive`) or split into dated shards.

---

## Finding 2 — MEDIUM: `unarchive_items` loses rich metadata on restore

**Location:** `archive_manager.py:164-174` — `unarchive_items()`

```python
_store.add_history_item(
    text=restore_dict.get("text", ""),
    paste_status=restore_dict.get("paste_status", "failed"),
    source_text=restore_dict.get("source_text", ""),
    ...
)
```

`StateStore.add_history_item()` accepts 18 parameters (including `confidence`, `diarization`, `emotion`, `audio_duration_sec`, `llm_applied`, `llm_latency_ms`, `word_timestamps`, `speaker_turns`, `cleaned_text`, `chat_id`, `message_id`). `unarchive_items` only restores 8 of them. All rich metadata fields are silently discarded and get default values (`None`/`0`/`""`). A user who archives and restores a diarized recording loses speaker turns, confidence scores, and audio duration permanently.

Additionally, `add_history_item` generates a **new UUID** for the restored item, so the original ID is permanently gone. Any external reference (e.g. a bookmark, a collection entry, or a Swift UI selection) becomes a dangling pointer.

**Fix:** Use a lower-level store method that accepts the full dict (or exposes `add_history_item_dict(d: dict)`), and restore the original `id` field.

---

## Finding 3 — MEDIUM: `unarchive_items` restore failure leaves archive corrupted

**Location:** `archive_manager.py:163-178`

When `_store.add_history_item()` raises, the item is pushed back into `remaining`:

```python
except Exception as exc:
    logger.error(...)
    remaining.append(item)
```

Then `_rewrite_archive(remaining)` is called. This looks correct — the item stays in the archive if it failed to restore. However, the success path has a gap: the function first removes items from `remaining`, then calls `_rewrite_archive`. If `_rewrite_archive` raises (disk full, concurrent deletion of `.tmp`), the archive file is already being overwritten and the successfully-called `_store.add_history_item` items are already in the active store. The item is now in **both** the active store and the archive (as the partial rewrite rolled back), effectively duplicating the record.

W896 fixed archive → active; this is the complementary race for active → archive on restore failure.

**Fix:** Write the new archive before calling `_store.add_history_item`, or keep a transaction log that can roll back the store inserts on `_rewrite_archive` failure.

---

## Finding 4 — LOW: No privacy-mode purge of archive

**Location:** `archive_manager.py` — no `purge_all` method  
**Related:** `KrabEar/backend/privacy_audit.py`, `KrabEar/backend/service.py:2332`

When a user triggers privacy purge (erase all history), the active `history.ndjson` is wiped but `archive.ndjson` is a separate file in `{data_dir}/archive/` — it is **not** touched by any privacy purge path in `service.py`. An audit of all `privacy`-related IPC handlers confirms no handler calls into `ArchiveManager`.

A user who expects "delete all my data" to be total will unknowingly leave all archived transcripts on disk, along with full text content of their recordings.

**Fix:** Add `ArchiveManager.purge_all()` and call it from the privacy-purge IPC handler alongside the history store wipe. Log the event to `PrivacyAuditLogger`.

---

## Finding 5 — LOW: No search capability on archived items

**Location:** `archive_manager.py` — `list_archived(limit)` is the only retrieval path

`list_archived` returns up to 500 items sorted by `archived_at` descending. There is no text search, date-range filter, or tag filter on the archive. For a user with thousands of archived items, finding a specific recording requires fetching all 500 and searching client-side.

The IPC handler `handle_list_archived` only accepts `limit`; there is no `query`, `date_from`, or `date_to` parameter.

**Fix:** Add `search_archived(query: str, limit: int, date_from: str | None, date_to: str | None)` method and corresponding IPC handler `search_archived`. Wire via `_read_archive()` with in-memory filter (acceptable until file size cap is enforced).

---

## Finding 6 — LOW: `archive_items` iterates IDs holding `_lock` for entire batch

**Location:** `archive_manager.py:111-125`

```python
with self._lock:
    for item_id in item_ids:
        item = _store.get_history_item_by_id(clean_id)   # disk read under lock
        ...
        self._append_ndjson(self._archive_path, item_dict)  # disk write under lock
        _store.delete_history_item(clean_id)               # disk write under lock
```

Every `get_history_item_by_id` and `delete_history_item` call goes to the `StateStore` which acquires its own internal file lock. Each `_append_ndjson` is a disk write. For a batch of N items, this holds `ArchiveManager._lock` for O(N) store round-trips, blocking all concurrent `list_archived` / `get_archive_stats` / `unarchive_items` calls for the full duration.

This is a latency regression risk for bulk-archive operations (e.g. archiving 200 items at once from the GUI cleanup wizard).

**Fix:** Collect all items from the store first (outside or briefly inside the lock), write all to archive in a single `_rewrite_archive` call, then batch-delete from the store — reducing the lock hold time to a single file rewrite.

---

## Test coverage gaps

The existing test suite (`test_archive_manager.py`) covers basic happy paths well (archive, list, unarchive, persistence, Unicode, concurrency, corrupted file). Gaps:

- No test for the privacy-mode purge gap (Finding 4) — no `purge_all` method exists to test.
- No test that restoring an item preserves all 18 metadata fields (Finding 2).
- No test for the restore-then-rewrite-failure scenario (Finding 3).
- No test exercising `limit` > 500 clamping (`max(1, min(limit, 500))` — the 500 cap is untested).
- No test for `list_archived` returning items sorted by `archived_at` descending (sort order is untested).

---

## Summary table

| # | Severity | Area | Impact |
|---|----------|------|--------|
| 1 | MEDIUM | File size cap | Unbounded disk growth + O(N) reads stall IPC |
| 2 | MEDIUM | Restore fidelity | 10 metadata fields lost on unarchive; ID changes |
| 3 | MEDIUM | Restore race | Partial restore failure can duplicate records |
| 4 | LOW | Privacy mode | Archive not wiped on privacy purge |
| 5 | LOW | Search | No text/date search on archive |
| 6 | LOW | Lock contention | Batch archive holds lock for O(N) disk ops |
