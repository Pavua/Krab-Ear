# Audit W1426: history_service.py residual — post W844/W869/W1163/W1166/W1172/W1259

**Date:** 2026-05-27  
**Branch:** audit-history-service-residual-W1426 (off codex/krab-ear-v2)  
**File:** `KrabEar/backend/history_service.py` (2900 lines)

---

## Prior Wave Merge State (codex/krab-ear-v2)

| Wave | Fix | Commit | Merged into codex/krab-ear-v2 |
|------|-----|--------|-------------------------------|
| W844 | `handle_cleanup_old_history` tz-aware comparison | 82b22518 | FIX PRESENT (content applied independently; commit not ancestor) |
| W869 | Path-prefix-collision bypass (`startswith` → `is_relative_to`) | 872b4ff2 | NOT MERGED — `str.startswith()` still at line 267 |
| W1163 | `semantic_search.remove` on `delete_history_item` | 54c0ea2c | NOT MERGED — no `_semantic_searcher` in `__init__` |
| W1172 | Fix broken W1163 call (`.remove()` → `.remove_item()`) | 6ca7ddfe | NOT MERGED — semantic_searcher param absent |
| W1166 | `export_obsidian` / `batch_export` output_dir allowlist | 63d5e7c6 | NOT MERGED — no `_resolve_export_dir` helper |
| W1259 | Version cascade in archive/merger/state_store delete paths | 0971f0a7 | NOT MERGED — does NOT touch history_service.py directly |
| W1268 | RecordingMerger TypeError fix | c3592195 | NOT MERGED — does NOT touch history_service.py directly |

**Summary:** 3 of 5 in-scope fixes are absent (W869, W1163/W1172, W1166). W1259 and W1268 target `archive_manager.py` and `recording_merger.py` only — no direct impact on history_service.py.

---

## New Findings (capped at 5)

### F1 — MEDIUM: Path-prefix-collision bypass in `handle_import_history_ndjson` (W869 NOT merged)

**Location:** `history_service.py:267`

```python
if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
```

`str.startswith()` is a string-level check, not a structural filesystem path check. An attacker can craft a path such as `/tmp/data-evil` that bypasses the `/tmp/data` root allowlist because `"/tmp/data-evil".startswith("/tmp/data")` returns `True`. The correct fix is `Path.is_relative_to(root)` (Python 3.9+).

The W869 fix commit (872b4ff2) applied this to both `history_service.py` and `recording_core_service.py` — that commit is NOT in `codex/krab-ear-v2`.

**Fix:** Replace line 267 with:
```python
if not any(resolved.is_relative_to(root) for root in allowed_roots):
```

---

### F2 — HIGH: Semantic index not purged on `delete_history_item` (W1163/W1172 NOT merged)

**Location:** `history_service.py:239-253` (`handle_delete_history_item`)

The method tombstones the NDJSON entry but has no reference to `SemanticSearcher`. The W1163 fix (54c0ea2c) added a call to `.remove()`, but W1172 (6ca7ddfe) found that `SemanticSearcher` only has `.remove_item()` (no `.remove` alias). Both fixes require:

1. A `semantic_searcher` parameter added to `HistoryService.__init__`
2. `_semantic_searcher.remove_item(item_id)` called after tombstoning (best-effort, swallowed exception)
3. `service.py` must pass the searcher instance and initialize it before `HistoryService`

Until merged, every `delete_history_item` call leaves a stale embedding in the semantic index. On large histories this causes search hits to return ghost items (deleted IDs), producing `RuntimeError: Запись {id} не найдена` in callers.

---

### F3 — HIGH: No path allowlist on `handle_export_obsidian` and `handle_batch_export` output_dir (W1166 NOT merged)

**Locations:** `history_service.py:2102-2106` (export_obsidian), `history_service.py:2612-2616` (batch_export)

Both handlers accept an arbitrary `output_dir` parameter from the IPC caller and write files there without validation:

```python
# export_obsidian line 2102:
if output_dir_param:
    out_dir = Path(output_dir_param).expanduser().resolve()
# No allowlist check — mkdir + write_text follow unconditionally
```

A malicious or buggy IPC caller can write Obsidian `.md` files to `~/.ssh/`, `~/Library/Keychains/`, or other sensitive locations.

The W1166 fix (63d5e7c6) adds a `_resolve_export_dir()` helper with an explicit allowlist (`data_dir`, `~/Documents`, `~/Downloads`, `~/Desktop`, `/tmp`) using `Path.relative_to()`.

---

### F4 — LOW: SRT sequence numbers skip when turns have empty text

**Location:** `history_service.py:756-770` (`handle_export_history_srt`)

```python
for seq, turn in enumerate(turns, start=1):   # seq assigned before empty check
    turn_text = str(turn.get("text", "")).strip()
    if not turn_text:
        continue                               # seq consumed but no line emitted
    srt_lines.append(str(seq))                # next turn gets seq N+1, not N
```

When one or more turns have empty text (e.g., diarization segments without transcription), the SRT sequence numbers are non-contiguous (e.g., `1, 3, 4` instead of `1, 2, 3`). The SubRip format requires strictly sequential integers starting at 1. Non-sequential SRT files are rejected by many video players and subtitle editors.

**Fix:** Use a separate counter that only increments when a segment is written:
```python
seq = 1
for turn in turns:
    turn_text = str(turn.get("text", "")).strip()
    if not turn_text:
        continue
    srt_lines.append(str(seq))
    ...
    seq += 1
```

The same pattern (correct: separate counter) is used in `_build_bulk_srt()` at line 2791. No test currently covers this gap.

---

### F5 — LOW: `handle_restore_history` accepts arbitrary `backup_path` without allowlist

**Location:** `history_service.py:2406-2456`

The `handle_restore_history` handler resolves `backup_path` and reads from it, but only validates that the directory exists and contains `history.ndjson` or `backup_meta.json`:

```python
backup_dir = Path(raw_path).expanduser().resolve()
if not backup_dir.exists() or not backup_dir.is_dir():
    raise RuntimeError(...)
# No allowlist check — proceeds to shutil.copy2 any file from backup_dir
```

If an attacker controls the IPC socket, they can point `backup_path` at any directory on the filesystem. If that directory happens to contain a file named `history.ndjson` (e.g., the output of a previous export), it will be silently copied over the active history. Contrast with `handle_import_history_ndjson` which has an allowlist (albeit with the W869 string-prefix bug). The same allowlist used for imports (`data_dir`, `~`, `/tmp`) should be applied here.

**Note:** This is lower severity than F3 because (a) the operation is destructive to the app's own data only, not to OS files, and (b) `restore_settings=True` is opt-in.

---

## Test Coverage Gap

- `test_srt_export_with_speaker_turns` (edges) does not assert that sequence numbers start at 1 and are contiguous.
- No test for `handle_restore_history` with `backup_path` outside the data dir.
- No test for `handle_export_obsidian` with `output_dir` pointing to a sensitive location (pending W1166 merge).
- No test for `handle_import_history_ndjson` using a prefix-collision path (pending W869 merge).

---

## Not a Finding

- **W844 cleanup tz-aware fix:** `datetime.now(timezone.utc)` is present at line 1282 — content independently applied, no action needed.
- **W1259/W1268:** These waves modify `archive_manager.py` and `recording_merger.py` only; history_service.py is unaffected.
- **Clipboard history TTL:** In-memory only (process-lifetime), max 20 entries. No persistence means no cross-session leak; no TTL bug exists.
- **storage_info archive accuracy:** `total_bytes` uses `data_dir.rglob("*")` which recursively includes `archive/archive.ndjson`. The explicit `history_bytes` field tracks only the active NDJSON, which is correct and expected by callers.
