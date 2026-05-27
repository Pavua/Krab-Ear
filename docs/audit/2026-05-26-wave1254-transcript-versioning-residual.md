# Wave 1254 — TranscriptVersionManager Residual Audit

**Date:** 2026-05-26
**File audited:** `KrabEar/backend/transcript_versioning.py`
**Auditor:** W1254 sub-agent (read-only re-audit)
**Parent audits:** W1040 (6 findings), W1045 fix (F1+F2 HIGH)

---

## Merge State of Prior Fixes

| Wave | PR | State | Notes |
|------|----|-------|-------|
| W1040 | #956 | OPEN — NOT merged | Audit doc only (read-only) |
| W1045 | #969 | OPEN — NOT merged | Fixes F1 (cap) + F2 (cascade) HIGH items |

**Both W1040 and W1045 are NOT merged into `codex/krab-ear-v2` as of 2026-05-26.**

Consequently, every HIGH finding from W1040 remains live in production:
- F1 HIGH: no per-item version cap (unbounded storage)
- F2 HIGH: no cascade delete on `delete_history_item` or `cleanup_old_history`
- F3 MED: `diff_versions` not wired to IPC
- F4 MED: `reverted_from` not persisted to NDJSON
- F5 LOW: no POSIX file lock
- F6 LOW: no retention-by-age policy

---

## New Residual Findings (W1254)

### F1 HIGH — Privacy bypass: three additional delete paths skip cascade delete

**Severity:** HIGH (privacy)
**File:** `KrabEar/backend/archive_manager.py:124`, `KrabEar/backend/recording_merger.py:69`, `KrabEar/backend/state_store.py:661`

Even after W1045 ships (adding cascade in `HistoryService.handle_delete_history_item` and `handle_cleanup_old_history`), three additional code paths call `store.delete_history_item()` directly, bypassing the transcript-version cascade entirely:

1. `archive_manager.py:124` — `ArchiveManager.archive_items()` moves items to archive by calling `_store.delete_history_item(clean_id)` directly. After archiving, the item's transcript versions remain in `transcript_versions.ndjson` indefinitely.
2. `recording_merger.py:69` — `RecordingMerger` deletes the source items after merging via `store.delete_history_item(item.id)`. Versions of merged-away items are never cleaned up.
3. `state_store.py:661` — `StateStore`'s own internal compaction/age-based cleanup loop (separate from `cleanup_old_history` IPC handler) calls `self.delete_history_item(item.id)` directly without triggering any version cascade.

W1045's fix only covers the two paths in `HistoryService`. The cascade fix is insufficient unless all delete call sites are addressed.

**Reproduction:** Archive a history item via `archive_items` IPC, then verify `transcript_versions.ndjson` still contains the item's versions.

**Fix:** Either move cascade delete logic into `StateStore.delete_history_item()` (covers all callers automatically), or inject `TranscriptVersionManager` into each service (`ArchiveManager`, `RecordingMerger`) and call `delete_versions_for()` after each direct store delete.

---

### F2 MED — `save_version` accepts empty-string text without error

**Severity:** MEDIUM
**File:** `KrabEar/backend/transcript_versioning.py:95-96`

The docstring at line 90 states "raises ValueError if text is empty", but the validation only checks `if not isinstance(text, str)`. An empty string passes the isinstance check and is silently stored as a version:

```python
if not isinstance(text, str):
    raise ValueError("text должен быть строкой")
```

A caller sending `{"item_id": "x", "text": ""}` via IPC will succeed, creating a version with an empty transcript. There is no `MAX_TEXT_SIZE` guard either — arbitrarily large texts are accepted.

**Impact:** Corrupt version entries accumulate; `diff_versions` returns meaningless diffs; storage can be exhausted with oversized text.

**Fix:**
```python
if not isinstance(text, str) or not text.strip():
    raise ValueError("text должен быть непустой строкой")
if len(text) > 1_000_000:  # 1 MB ceiling
    raise ValueError("text превышает допустимый размер (1 MB)")
```

---

### F3 MED — `reverted_from` field not persisted to NDJSON (W1040 F4, still present)

**Severity:** MEDIUM (data integrity)
**File:** `KrabEar/backend/transcript_versioning.py:168-175`

W1040 identified this as F4 MED; W1045 did NOT fix it. Still present verbatim:

```python
def revert_to_version(self, item_id: str, version_num: int) -> dict[str, Any]:
    target = self.get_version(item_id, version_num)
    new_version = self.save_version(
        item_id=item_id,
        text=target["text"],
        source="manual",
    )
    new_version["reverted_from"] = version_num   # ← set only on in-memory dict
    return new_version
```

`save_version` writes the record to NDJSON before `reverted_from` is added to the dict. After a process restart, `get_versions("item_x")` will return the revert version without `reverted_from`, making the version history audit trail misleading.

The existing test `test_reverted_from_persists` (line 391) only checks that 3 versions exist after reload — it does not assert that `reverted_from` is present on the reloaded version (the field is absent from the NDJSON record).

**Fix:** Pass `reverted_from` into the NDJSON record before append, e.g. by adding a `metadata: dict | None = None` parameter to `save_version`, or by writing a dedicated `revert` record type.

---

### F4 MED — `diff_versions` IPC handler missing; W1040 F3 still open

**Severity:** MEDIUM (functionality gap)
**File:** `KrabEar/backend/service.py:1076-1078`, `KrabEar/backend/transcript_versioning.py:177`

Confirmed still present: `diff_versions()` method exists in `TranscriptVersionManager` but there is no `diff_transcript_versions` entry in the IPC dispatch table in `service.py`. The three wired methods are:

```python
"save_transcript_version": ...,
"get_transcript_versions": ...,
"revert_transcript_version": ...,
```

No `"diff_transcript_versions"` handler.

Additionally, `diff_versions` returns `text_v1` and `text_v2` (full transcript texts) in its response alongside `unified_diff`. If the IPC method were wired, the full text of both versions would be sent over the socket on every diff request. For long transcripts this is redundant (the diff already contains the changed lines); it also increases privacy risk since full texts are transmitted even when only the delta is needed.

**Fix:** Add IPC handler + dispatch entry; strip `text_v1`/`text_v2` from the default response (or make them opt-in via a `include_text=false` parameter).

---

### F5 LOW — Semantic search index not updated when transcript version is saved (W1148/W1172 context)

**Severity:** LOW (data consistency)
**File:** `KrabEar/backend/transcript_versioning.py:78-112`
**Related:** W1148 (PR #1059), W1172 (PR #1085, NOT merged)

When a new version is saved via `save_transcript_version` IPC, the updated text is written to `transcript_versions.ndjson` but the `SemanticSearcher` index is never updated. The semantic search index retains the text from the original `HistoryItem.text` field. If users edit and re-version transcripts frequently, semantic search results will diverge from the actual latest version text.

More critically, W1172 (PR #1085) — which fixes `SemanticSearcher.remove_item` not being called on history delete — is also NOT merged. This means deletes already leak stale embeddings into the semantic index. The transcript versioning system compounds this: `save_version` with an empty or corrected text (e.g. via `revert_to_version`) does not trigger re-indexing.

There is no `handle_diff_transcript_versions` IPC binding (see F4), and no post-version-save hook to call `semantic_searcher.index_item(item_id, new_text)`.

**Fix:** After W1148/W1172 land, add a post-save hook in `handle_save_transcript_version` to call `semantic_searcher.index_item(item_id, text)` if the semantic searcher is enabled and available. This requires injecting `SemanticSearcher` into `TranscriptVersionManager` or calling the update from `BackendService`.

---

## Summary Table

| # | Severity | Description | W1040 ref | W1045 fixes? |
|---|----------|-------------|-----------|--------------|
| F1 | HIGH | 3 additional delete paths bypass cascade (archive, merger, state_store internal) | F2 (partial) | Partial — only 2 paths covered |
| F2 | MED | Empty-string text accepted by `save_version`; no size cap | — | No |
| F3 | MED | `reverted_from` not persisted to NDJSON | F4 | No |
| F4 | MED | `diff_transcript_versions` IPC handler not wired | F3 | No |
| F5 | LOW | Semantic search index not updated on version save | — | No |

**Total new findings: 5.**
**W1045 (PR #969) partially addresses W1040 F1+F2 HIGH but is NOT merged and has its own residual gap (F1 above).**
