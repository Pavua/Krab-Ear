# W1302 Audit: state_store.py post-fix residual issues

**Date:** 2026-05-26  
**Branch:** `audit/state-store-post-fixes-W1302`  
**Scope:** `KrabEar/backend/state_store.py` + `KrabEar/backend/models.py`  
**Fixes audited:** W853, W1237, W1238, W1259  
**New findings:** 6

---

## Merge State

All four fixes are **NOT merged** into `codex/krab-ear-v2` as of this audit.

| Fix | Branch | Description | Status |
|-----|--------|-------------|--------|
| W853 | `origin/feature/audit-state-store-fixes-W853` | compact fsync + atomic journal truncation | NOT MERGED |
| W1237 | `origin/fix-statestore-field-forwarding-W1237` | `add_history_item` 8-field forwarding | NOT MERGED |
| W1238 | `origin/fix-history-forward-compat-W1238` | `HistoryItem._extra` forward-compat sidecar | NOT MERGED |
| W1259 | `origin/fix-version-cascade-W1259` | version cascade on delete/archive/compact | NOT MERGED |

The test branch `origin/feature/add-test-state-store-W862` (covering W853 atomicity) is also unmerged.

---

## Findings

### F1 — MEDIUM: W1259 startup compact silently skips version cascade

**File:** `KrabEar/backend/service.py` lines 3954–3960 and `BackendService.__init__`

`build_service()` calls `store.maybe_compact()` **before** `BackendService(store=store)`:

```python
def build_service(data_dir: Path) -> BackendService:
    store = StateStore(data_dir=data_dir)
    store.save_settings(...)
    store.maybe_compact()          # ← runs here, _transcript_versioner is None
    return BackendService(store=store)  # ← W1259 injects versioner here
```

W1259 late-injects `_transcript_versioner` inside `BackendService.__init__`:

```python
self._transcript_versioning = TranscriptVersionManager(...)
self.store._transcript_versioner = self._transcript_versioning  # W1254 F1
```

At startup the compact fires with `_transcript_versioner = None`, so `getattr(self, '_transcript_versioner', None)` returns `None` and the version cascade is silently skipped. Dangling version records accumulate for any items that were tombstoned before the previous restart.

**Fix:** In `build_service()`, construct `TranscriptVersionManager` first and inject it into `store` before calling `maybe_compact()`, or defer startup compact until after `BackendService` is fully constructed.

---

### F2 — MEDIUM: `compact_with_stats` holds global lock across 3 full NDJSON reads

**File:** `KrabEar/backend/state_store.py` method `compact_with_stats` (lines 559–602)

While holding `_lock` (which blocks all concurrent IPC handlers), `compact_with_stats` performs three complete NDJSON traversals:

1. `_history_stats_unlocked()` → `_load_active_items_unlocked()` (pre-compact stats)
2. `_compact_unlocked()` → `_load_active_items_unlocked()` (actual compact)
3. `_history_stats_unlocked()` → `_load_active_items_unlocked()` (post-compact stats)

Each `_load_active_items_unlocked()` reads `history.ndjson` plus all delta journals in full. For a 50k-item store (~25 MB NDJSON), this is ~75 MB of sequential I/O while all IPC requests queue behind the lock. The IPC server uses one thread-per-connection but all threads compete for the same `fcntl.LOCK_EX` file lock.

`compact_history` is called from Swift's HistoryPanel (UI-triggered), making a multi-second IPC stall observable as an unresponsive UI.

**Fix:** Cache the pre-compact stats from the already-traversed `active` list inside `_compact_unlocked` instead of re-reading. Alternatively, only call `_history_stats_unlocked` once before compact and derive after-stats from the delta.

---

### F3 — MEDIUM: W1259 + W853 merge produces incorrect version-cascade placement

**File:** `KrabEar/backend/state_store.py` method `_compact_unlocked`

W1259 inserts its version-cascade block **after** the `write_text("")` truncation lines. W853 replaces all those `write_text("")` calls with a `delta_journals` list + atomic tmp-file loop:

```python
# W853 replaces:
self.tombstones_path.write_text("", encoding="utf-8")
...
self.action_items_path.write_text("", encoding="utf-8")

# With:
delta_journals = [self.tombstones_path, ...]
for journal in delta_journals:
    tmp_journal = journal.with_suffix(".ndjson.tmp")
    with tmp_journal.open("w", ...) as fh:
        fh.flush(); os.fsync(fh.fileno())
    tmp_journal.replace(journal)
```

A naïve cherry-pick of W1259 onto a W853-patched tree places the version-cascade code in the middle of the `delta_journals` loop (after `action_items_path.write_text(...)` which no longer exists), causing a `SyntaxError` or logic misplacement. The cascade must be placed **after** the entire `delta_journals` loop when both fixes are applied together.

**Fix:** When merging both, manually position W1259's cascade block after the closing brace of W853's `for journal in delta_journals:` loop.

---

### F4 — LOW: No checksum/integrity validation in read paths

**File:** `KrabEar/backend/state_store.py`

`_read_ndjson_unlocked` skips lines that fail `safe_json_loads` (truncated or corrupt JSON from a crash mid-write), but there is no content-level checksum. The existing `IntegrityChecker` (`backend/integrity_checker.py`) is only invoked via a manual IPC call (`check_data_integrity`), not automatically during `_load_active_items_unlocked` or after compaction.

Scenarios not covered:

- **Silent byte-flip corruption:** A valid JSON line with corrupted UTF-8 interior (e.g., storage media bit error) passes `safe_json_loads` but produces wrong field values.
- **Post-compact verification:** After `_compact_unlocked` completes, there is no check that the newly written `history.ndjson` is parseable and matches the expected item count, so a write-failure that produces a partial tmp file (e.g., out-of-disk during `fh.flush()`) results in history loss.

**Fix (low priority):** After `tmp_history.replace(self.history_path)` in `_compact_unlocked`, verify the new file's line count matches `len(active)`. Log `history.write_fail` error via `_push_error` on mismatch.

---

### F5 — LOW: Zero test coverage for post-fix behaviors in `codex/krab-ear-v2`

**Files:** `KrabEar/tests/test_state_store_*.py`

All seven existing state_store test files were audited. None covers:

- `os.fsync` atomicity guarantee from W853 (no mock-fsync crash-simulation test)
- `_extra` round-trip preservation through `compact()` from W1238
- Version cascade on `delete_history_item → compact` from W1259
- `add_history_item` forwarding of `reasoning`, `audio_path`, `is_protected`, `tags`, `favorite`, `action_items`, `decisions`, `questions` from W1237

The test branch `origin/feature/add-test-state-store-W862` contains compact tests but also does not cover fsync atomicity or the W1238/W1259 behaviors. It is unmerged.

**Fix:** Merge W862 test branch and add parameterized tests for W1237 field forwarding, W1238 `_extra` round-trip, and W1259 version cascade call verification using `unittest.mock.patch`.

---

### F6 — INFO: `_compact_unlocked` loads `deleted_ids` twice when W1259 is applied

**File:** `KrabEar/backend/state_store.py` method `_compact_unlocked` (W1259 diff)

W1259 adds `deleted_ids = self._load_deleted_ids_unlocked()` at the top of `_compact_unlocked`, then calls `self._load_active_items_unlocked()`. But `_load_active_items_unlocked` internally calls `_load_deleted_ids_unlocked()` again to filter tombstoned items. For large stores this doubles the tombstone file I/O within the same lock.

The redundancy is safe (both reads are inside the exclusive lock so the file cannot change between them), but adds unnecessary I/O cost for the common case where no items are deleted.

**Fix:** Pass `deleted_ids` as a parameter to `_load_active_items_unlocked`, or compute `active` first and derive `deleted_ids` from the set difference between raw history IDs and active IDs (no extra file read).

---

## Interaction Analysis

| Fix pair | Composes correctly? | Notes |
|----------|---------------------|-------|
| W853 + W1237 | Yes | Independent; W1237 changes the public API signature only |
| W853 + W1238 | Yes | W1238's `to_dict()` output is written by W853's compact; `_extra` is correctly merged |
| W853 + W1259 | **Requires manual merge** | Version cascade placement breaks if naïve cherry-pick; see F3 |
| W1237 + W1238 | Yes | W1237 adds call-site params; W1238 adds model round-trip; orthogonal |
| W1237 + W1259 | Yes | Independent |
| W1238 + W1259 | Yes | W1238 ensures future fields survive compact; W1259 purges versioning on delete; orthogonal |
| All four combined | **Requires care on W853+W1259 ordering + F1 fix** | See F1 and F3 |

---

## Corruption Recovery

`_read_ndjson_unlocked` correctly handles:
- Truncated last line (crash during `_append_ndjson`) — JSON parse fails, line skipped
- Empty lines — skipped via `if not raw`
- Non-dict JSON values — filtered by `if isinstance(payload, dict)`

Not handled:
- Binary null bytes within a line (returns corrupt dict with null-byte strings)
- Post-compact count mismatch (see F4)
