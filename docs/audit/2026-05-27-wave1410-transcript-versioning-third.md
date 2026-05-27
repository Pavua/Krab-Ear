# Wave 1410 — Third-pass audit: TranscriptVersionManager

**Date:** 2026-05-27
**Auditor:** W1410 (third-pass)
**Scope:** `KrabEar/backend/transcript_versioning.py` + cascade sites
**Prior audits:** W1040 (first, 6 findings), W1254 (second, 5 findings)
**Prior fixes:** W1045 (cap + history cascade), W1259 (archive/merger/state_store cascade)

---

## Merge state (as of HEAD `6c900317` on `codex/krab-ear-v2`)

| Branch | Fix | Merged? |
|--------|-----|---------|
| `fix-transcript-versioning-W1045` | Per-item cap (50), `delete_versions_for`, `cleanup_for_ids`, HistoryService cascade | **NOT MERGED** |
| `fix-version-cascade-W1259` | `purge_versions_for_item`, ArchiveManager / RecordingMerger / StateStore cascade wiring | **NOT MERGED** |
| `fix-W1163-method-name-W1172` | `semantic_search.remove_item` alias + history_service call-site fix | **NOT MERGED** |
| `fix-defer-startup-compact-W1309` | Defer startup compact until versioner is wired | **NOT MERGED** |

All four fix branches are open PRs but have not landed on `codex/krab-ear-v2`. The production file is still the original 269-line version with zero cascade logic, zero cap, and zero semantic-search removal on version operations.

---

## New findings (NEW — not reported in W1040 or W1254)

### F1 HIGH — `save_version` accepts empty string; no MAX_TEXT_SIZE ceiling

**Location:** `transcript_versioning.py:95–99`

```python
if not isinstance(text, str):
    raise ValueError("text должен быть строкой")
```

The guard is a `isinstance(str)` type check only. An empty string `""` passes and is written to disk. A 10 MB wall-of-text is also written with no ceiling. Confirmed by runtime test:

```
save_version('id', '', 'manual')  -> version_num=1, text=''   # silently accepted
save_version('id', 'x'*10_000_000, 'manual')  -> accepted     # 10 MB written
```

W1254 F2 flagged this as MED but it was not addressed in W1045. An empty-string version pollutes history and is semantically meaningless; a multi-MB text can cause unbounded NDJSON growth even before the per-item cap logic lands. Needs `if not text.strip(): raise ValueError(...)` and a `MAX_TEXT_BYTES = 256 * 1024` guard.

---

### F2 HIGH — `reverted_from` field lost on disk; test gap masks the bug

**Location:** `transcript_versioning.py:168–175`

```python
new_version = self.save_version(item_id=item_id, text=target["text"], source="manual")
new_version["reverted_from"] = version_num   # set on in-memory dict only
return new_version
```

`save_version` calls `_append(record)` with a record that does **not** include `reverted_from`. The field is injected into the returned dict after `_append` returns. After process restart the field is gone from NDJSON.

Runtime confirmation:
```
revert returns: {'reverted_from': 1, ...}
NDJSON record: {'item_id': 'id1', 'version_num': 3, 'text': 'original text', 'source': 'manual', ...}
# No 'reverted_from' key in disk record
```

The existing test `TestTranscriptVersionManagerRevertPersistence.test_reverted_from_persists` (line 391) does **not** assert `reverted_from in record` — it only checks `text` and `version_num`, so the test passes even though the field is silently dropped. This is both a functional bug (audit trail missing) and a test gap. W1040 F4 and W1254 F3 identified this; it remains unpatched.

**Fix:** include `reverted_from` in the record dict before calling `_append`, or pass it as an optional kwarg to `save_version`. Update `test_reverted_from_persists` to assert `latest.get('reverted_from') == 1`.

---

### F3 MED — `diff_versions` not exposed via IPC; `handle_diff_transcript_versions` missing from dispatch table

**Location:** `backend/service.py:1076–1078`, `transcript_versioning.py:230+`

The `diff_versions()` method exists (line 177) but has no IPC handler. The service.py dispatch table registers only:

```python
"save_transcript_version": ...,
"get_transcript_versions": ...,
"revert_transcript_version": ...,
```

There is no `"diff_transcript_versions"` entry and no `handle_diff_transcript_versions` method on `TranscriptVersionManager`. Callers (Swift side or external tools) cannot request a diff without calling two `get_transcript_versions` calls and computing the diff client-side. W1040 F3 and W1254 F4 flagged this; it was not fixed by W1045.

**Fix:** add `handle_diff_transcript_versions(params)` to `TranscriptVersionManager` and register it in `service.py`.

---

### F4 MED — all four fix branches (W1045/W1259/W1172/W1309) unmerged; cascade gap is total

This is the overarching merge-state finding. In production `codex/krab-ear-v2`:

- `HistoryService.delete_history_item` — no version cascade (W1045 fix not merged)
- `HistoryService.cleanup_old_history` — no batch cascade (W1045 fix not merged)
- `ArchiveManager.archive_items` — calls `store.delete_history_item(clean_id)` at line 124 with no version purge (W1259 fix not merged)
- `RecordingMerger.merge_items` — calls `store.delete_history_item(item.id)` at line 69 with no version purge (W1259 fix not merged)
- `StateStore._compact_unlocked` — compaction purges tombstoned items from NDJSON with no version purge (W1259 + W1309 fixes not merged)
- Semantic search stale embeddings on history delete — `history_service.py` has no `_semantic_search.remove_item()` call (W1172 fix not merged)

Every delete path in production leaks versions into `transcript_versions.ndjson`. For a 90-day history store that sees regular `cleanup_old_history` calls, the version file grows unbounded indefinitely.

---

### F5 LOW — `_append` opens file in `"a"` mode without POSIX `flock`; multi-process corruption possible

**Location:** `transcript_versioning.py:63–67`

```python
def _append(self, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with self._versions_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
```

`self._lock` is a `threading.Lock` — effective within a single Python process. When the backend restarts under launchd and a second process briefly co-exists during the grace period (the two-binary drift window noted in CLAUDE.md), two processes can call `_append` concurrently. POSIX `flock` (as used by `StateStore`) is cross-process safe; `threading.Lock` is not.

W1040 F5 noted this as LOW; it remains unpatched and has become slightly more relevant now that the launchd KeepAlive supervisor (Phase A) can restart the backend mid-session. Impact: garbled NDJSON lines causing parse warnings, at worst lost writes. Fix: wrap the `open("a")` block with `fcntl.flock(fh.fileno(), fcntl.LOCK_EX)` / `LOCK_UN`.

---

## Summary table

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| F1 | HIGH | `save_version` accepts empty string and unbounded text size | NEW (W1254 F2 partial, no fix) |
| F2 | HIGH | `reverted_from` not persisted to NDJSON; test gap masks it | Carried from W1040 F4 / W1254 F3, confirmed still present |
| F3 | MED | `diff_versions` not exposed via IPC (`handle_diff_transcript_versions` missing) | Carried from W1040 F3 / W1254 F4, still unfixed |
| F4 | MED | W1045 / W1259 / W1172 / W1309 all unmerged; every delete path leaks versions | Overarching merge-state gap |
| F5 | LOW | `_append` uses `threading.Lock` only; POSIX flock missing for multi-process safety | Carried from W1040 F5, still unfixed |

**NEW findings for this pass:** F1 (empty-string + size guard), F2 confirmed live with runtime test and test-gap detail, F3 confirmed no IPC handler, F4 merge-state audit, F5 flock.

## Recommended merge order

1. Merge W1045 first (cap + HistoryService cascade baseline).
2. Merge W1259 second (depends on W1045's `purge_versions_for_item`).
3. Merge W1309 (depends on W1259's `_transcript_versioner` injection in StateStore).
4. Merge W1172 (independent, but logically completes the delete chain).
5. Fix F1 (empty text + MAX_TEXT_BYTES) inline with W1045 or as follow-up.
6. Fix F2 (`reverted_from` NDJSON persistence) — small targeted change.
7. Fix F3 (add `handle_diff_transcript_versions` + service.py registration).
