# Wave 1228 — HistoryItem Schema & NDJSON Storage Audit

**Date:** 2026-05-26  
**Branch:** audit/history-schema-W1228  
**Files audited:**
- `KrabEar/backend/models.py`
- `KrabEar/backend/state_store.py`
- `KrabEar/backend/data_migrator.py`
- `KrabEar/contracts/` (for JSON Schema export coverage)
- `KrabEar/tests/test_models.py`, `test_state_store.py`, `test_data_migrator.py`, `test_history_contracts.py`

---

## Summary

5 findings. No blocking bugs found. The storage layer is solid for current usage but has two structural gaps that will surface as the schema evolves (forward-compat data loss on compaction, migration not auto-run at startup) and one silent API surface regression (`add_history_item` signature drift).

---

## Finding 1 — MEDIUM: `add_history_item` silently drops 8 newer `HistoryItem` fields

**Location:** `KrabEar/backend/state_store.py:166–224`

`HistoryItem.create()` accepts 28 keyword arguments, but `StateStore.add_history_item()` only proxies 20 of them. The following 8 fields added in later phases are not reachable through `add_history_item`:

| Field | Phase added |
|---|---|
| `tags` | Phase D.10a |
| `favorite` | Phase D.10a |
| `reasoning` | Phase 4.4 Voxtral |
| `audio_path` | re-transcription |
| `is_protected` | bulk-op guard |
| `action_items` | meeting extraction |
| `decisions` | meeting extraction |
| `questions` | meeting extraction |

**Impact:** Any code that calls `add_history_item` (the main entry point for new recordings) cannot persist these fields at creation time. Tags, favorite, audio_path, reasoning, and is_protected default to their zero-values in the initial NDJSON write. Tags and favorite have delta-journal workarounds (`update_history_item_tags`, `update_history_item_favorite`) requiring a second write, but `reasoning`, `audio_path`, and `is_protected` have no post-creation update path in `StateStore`. The existing test suite does not cover this gap.

**Fix:** Add the missing parameters to `add_history_item` and pass them through to `HistoryItem.create()`.

---

## Finding 2 — MEDIUM: Forward-compatibility data loss on compaction

**Location:** `KrabEar/backend/state_store.py:846–862` (`_compact_unlocked`)

`from_dict()` uses explicit `payload.get("known_field")` for every field — unknown keys in the JSON payload are silently discarded. When `_compact_unlocked()` runs, it deserializes every record via `_iter_history_items_unlocked()` (which calls `from_dict`) and then rewrites via `item.to_dict()`. Any field written by a **newer** binary that an **older** binary does not know about is permanently lost at the next compaction.

**Example scenario:**
1. User upgrades to v2.1 which adds `calibrated_confidence: float` to `HistoryItem`.
2. History accumulates 200 entries with `calibrated_confidence` populated.
3. User rolls back to v2.0.5 (or has two binaries with drift — a known production risk per CLAUDE.md).
4. Compaction triggers; v2.0.5 reads each record via `from_dict`, drops `calibrated_confidence`, writes back with `to_dict`. Data is gone.

The same mechanism affects `import_history_ndjson` (line 534) which also round-trips through `from_dict → to_dict`.

**Note:** This is a design-level trade-off inherent to the `dataclass + asdict` approach. The fix requires preserving a `_extra: dict` for unknown keys in `from_dict` and writing it back in `to_dict`.

---

## Finding 3 — LOW: DataMigrator is manual-only; no auto-run at startup

**Location:** `KrabEar/backend/service.py:419`, `KrabEar/backend/data_migrator.py`

`DataMigrator` is instantiated in `BackendService.__init__` and wired to two IPC handlers (`check_migration`, `run_migration`). Neither handler is called during startup. There is no automatic migration check when the backend starts. A user upgrading from v1.x with old-format history records (missing `tags`/`favorite`) will silently operate on un-migrated data until manually triggering `run_migration` via IPC.

The version detection heuristic in `DataMigrator._detect_version_from_items()` is field-presence based (checks for `tags` and `favorite`) rather than an explicit version marker stored in the history file. This works for the single current migration path (v1.0 → v2.0) but does not scale: if a v2.0 → v3.0 migration is needed, the heuristic would need to inspect a different field combination, and there is no persistent version stamp to rely on.

**Fix:** (a) Call `check_migration_needed` at startup and log a warning or auto-migrate to LATEST_VERSION. (b) Store a `__schema_version__` sentinel record as the first NDJSON line in `history.ndjson` during compaction.

---

## Finding 4 — LOW: Compaction missing `fsync` on temp file before `replace`

**Location:** `KrabEar/backend/state_store.py:851–856`

`_compact_unlocked` writes the new history into a `.ndjson.tmp` file with only `fh.flush()` before calling `tmp_history.replace(history_path)`. There is no `os.fsync(fh.fileno())` call before the rename. Compare with `_append_ndjson` (line 1115–1119) which does call `os.fsync`. On macOS (HFS+/APFS) the kernel's VM may not have flushed dirty pages to disk at the time of the rename. In a crash immediately after `replace()` but before the OS syncs the dirty inode, the new history file may be partially written or zero-length while the old file's data has been unlinked.

**Risk:** Unlikely to trigger in practice on APFS (which uses copy-on-write semantics and the rename is crash-safe), but technically violates the durability claim in the module docstring ("все операции записи защищены file-lock и атомарными replace"). The 6 delta-journal truncations following the replace (`tombstones_path.write_text("")`, etc.) also lack `fsync`.

**Fix:** Add `os.fsync(fh.fileno())` before `fh`'s context manager exits in `_compact_unlocked`, matching `_append_ndjson`.

---

## Finding 5 — LOW: `HistoryItem` has no JSON Schema in `contracts/`; PII fields unlabeled

**Location:** `KrabEar/backend/models.py`, `KrabEar/contracts/`

The `contracts/` directory exports JSON Schema for 9 SSE event types (STT, translation, live_subs, etc.) via `python -m contracts.export`. `HistoryItem` — the central data model exchanged in every IPC response — has no corresponding JSON Schema export. This means:

- No machine-readable contract for external integrations or for the Swift side to validate against.
- No OpenAPI-style documentation of which fields are optional vs required.

Additionally, `HistoryItem` contains several fields that hold user-identifiable or user-sensitive content but are not labeled as PII anywhere in the model or storage layer:

| Field | PII concern |
|---|---|
| `text` | verbatim transcription |
| `source_text` | original transcription before LLM |
| `translated_text` | translation output |
| `audio_path` | filesystem path, may reveal username |
| `chat_id` / `message_id` | Telegram account identifiers |
| `reasoning` | Voxtral LLM Q&A content |

The Sentry breadcrumb layer (`observability.py`) correctly omits transcript text from breadcrumbs. However, there is no PII annotation in the model itself (e.g. a `_PII_FIELDS` frozenset) that could be used by `error_reporter.py`, audit logs, or export sanitizers to automatically redact sensitive content.

**Fix:** (a) Add a `HistoryItem.json_schema()` classmethod or a `contracts/history_item.py` Pydantic model mirroring the dataclass. (b) Add a `_PII_FIELDS: frozenset[str]` constant to `models.py` listing the fields that contain user content.

---

## Coverage summary

| Area | Status |
|---|---|
| `HistoryItem` backward compat (old NDJSON loads with defaults) | Covered — `test_models.py` + `test_state_store.py` |
| `HistoryItem` round-trip (create → to_dict → from_dict) | Partially covered — no test for newer fields like `reasoning`, `word_timestamps` |
| Unknown/future field preservation in `from_dict` | Not tested |
| `add_history_item` signature vs `HistoryItem.create` parity | Not tested |
| DataMigrator v1.0 → v2.0 | Well covered — `test_data_migrator.py` |
| DataMigrator auto-run at startup | Not tested |
| Compaction fsync correctness | Not tested |
| Tombstone semantics | Covered — `test_state_store.py` |
| Atomic settings write | Covered |
| JSON Schema for `HistoryItem` | Missing entirely |
