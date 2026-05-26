# Wave 835 — HistoryService audit

**Date:** 2026-05-26  
**File:** `KrabEar/backend/history_service.py` (2901 lines)  
**Scope:** CRUD correctness, SRT export edge cases, clipboard history limit enforcement, storage info accuracy.

---

## 1. CRUD correctness

### 1.1 `handle_add_history_item` — correct overall

- Empty-text guard fires correctly (line 59).
- All string fields are coerced via `str(...).strip()`, so `None` params become `""` safely.
- **Minor gap**: `paste_status` default is `"failed"` without validation against an enum.  
  A caller can write any arbitrary string (e.g. `"banana"`) and it will be persisted silently.  
  Downstream code that pattern-matches on `"ok"/"failed"/"skipped"` will treat unknown values as neither, which is safe but confusing.

### 1.2 `handle_delete_history_item` — correct

- Raises `ValueError` on missing `id` and on not-found item (lines 243–247).
- Breadcrumb emitted on success.

### 1.3 `handle_get_history_item` — O(N) full scan

- Lines 335–347: loads *all* active items under lock, then linear-scans for matching `id`.
- For large histories (> 5 000 items) this is a latency risk on every single-item fetch.
- `state_store.get_history_item_by_id()` already exists and is used in the tagging handlers (line 366) — `handle_get_history_item` should use it instead.

### 1.4 Tag handlers — TOCTOU race on add/remove

- `handle_add_tag` and `handle_remove_tag` read the item, compute a new tag list, then write it back — two separate `store` calls with no covering lock (lines 366–373, 390–395).
- Concurrent IPC calls could interleave, causing a tag to be lost or duplicated.  
  A single `store.update_history_item_tags_atomic()` (or a lock wrapping both operations) would eliminate this.

### 1.5 `handle_cleanup_old_history` — timestamp comparison is lexicographic, not timezone-aware

Lines 1282–1290:

```python
cutoff_iso = cutoff.isoformat()   # "2026-02-25T12:34:56.789012+00:00"
to_delete = [item for item in active if item.ts < cutoff_iso]
```

`cutoff` is timezone-aware (UTC). History items whose `ts` was stored without a UTC offset (naive ISO strings like `"2026-02-25T14:00:00"`) will *always* compare as less than the UTC-offset string because `"2026-02-25T14:00:00"` < `"2026-02-25T..."` only accidentally — and naive strings that happen to start later in the alphabet (e.g. `"2026-03-01T..."`) will sort correctly, but items with `"2026-02-25T14:..."` vs `"2026-02-25T12:...+00:00"` produce wrong results depending on wall-clock offset.

**Recommended fix:** parse both sides with `datetime.fromisoformat()` and compare `datetime` objects, handling tz-naive items as UTC.

### 1.6 `handle_restore_history` — no path-traversal check

Lines 2410–2411:

```python
backup_dir = Path(raw_path).expanduser().resolve()
```

Any caller-supplied path is accepted without checking it sits inside `data_dir/backups/`.  
An attacker with IPC access could supply `/etc/` or `~/Library/` and trigger a `shutil.copy2` overwrite of important files if those happen to contain a file named `history.ndjson`.  
`handle_import_history_ndjson` has an `allowed_roots` guard (lines 266–268); the same pattern should be applied here.

---

## 2. SRT export edge cases

### 2.1 `handle_export_history_srt` — sequential number reuse when turns have empty text

Lines 756–769: the loop increments `seq` for every turn, including those with empty `turn_text`.  
However, `continue` is called when `turn_text` is empty — but `seq` was already appended via `srt_lines.append(str(seq))` **before** the `continue` check. Wait — reading more carefully, `seq` is the `enumerate` index and `srt_lines.append(str(seq))` happens after the empty-text `continue` at line 759. This is actually correct.

**No bug here** — the empty-text guard fires before any line is appended.

### 2.2 `_build_srt_single` — zero-duration item produces timestamp `"00:00:01,000"` hardcoded

Line 956–957:

```python
end_ts = HistoryService._srt_timestamp(duration) if duration > 0 else "00:00:01,000"
```

A 1-second fallback is reasonable, but not documented. If `audio_duration_sec` is `0` (not `None`) the same fallback fires, which may be surprising for truly instant recordings (e.g. hot-reload test items with `duration=0`).

### 2.3 `_build_bulk_srt` — SRT sequence numbers reset to 1 per file, which is correct, but the offset computation has a subtle flaw

Lines 2808–2810:

```python
start_sec = offset_sec + float(turn.get("start", 0.0) or 0.0)
end_sec = offset_sec + float(
    turn.get("end", start_sec + 1.0) or start_sec + 1.0
)
```

`start_sec + 1.0` here is *already offset-adjusted*, so the fallback `end` is correctly `start + 1 s`. However, `offset_sec += duration` is only reached if the item had diarisation with ≥ 2 speakers (line 2819 inside the `if`). If an item has `diarization.enabled=True` but only 1 speaker in `speaker_turns`, the code falls through to the plain-text path and `offset_sec += duration` is executed there (line 2833). This is correct.

**No bug here.**

### 2.4 SRT search loop: up to 20 000 page fetches for a single export

`handle_export_history_srt` loops up to 200 pages × 100 items = 20 000 items to find a single item by ID (lines 718–733). For large stores this performs 200 `get_history_page_filtered` calls. Using `store.get_history_item_by_id(item_id)` directly would be O(1) under lock instead of O(N/100) IPC-paginated.

---

## 3. Clipboard history limit enforcement

### 3.1 `handle_get_clipboard_history` — limit is enforced on read, not on write

Lines 1230–1239:

```python
limit = self._coerce_bounded(value=params.get("limit", 10), default=10, min_value=1, max_value=20)
return {"items": self._clipboard_history[-limit:], "count": len(self._clipboard_history)}
```

The `max_value=20` cap is documented ("last 20 paste items stored in memory" in CLAUDE.md), but enforcement only happens on the *read* side. The list grows unboundedly because nothing in `HistoryService` itself caps writes to `_clipboard_history`.

The trim logic lives in `BackendService` (the caller that mutates `_clipboard_history`), so the 20-item cap is enforced there — but `HistoryService` owns neither the write path nor a defensive check at initialisation. If a future refactor moves the write path into `HistoryService`, the cap would silently be lost.

**Recommendation:** add a `MAX_CLIPBOARD_HISTORY = 20` constant and a trim assertion in `handle_get_clipboard_history` (or expose a `_trim_clipboard_history()` helper that can be called from both sides).

### 3.2 `handle_repaste_item` — linear scan, no index

Lines 1255–1261: iterates the entire `_clipboard_history` list from the end. For the documented 20-item cap this is trivially fast, but the absence of a dict-keyed lookup means if the cap were ever relaxed, performance would degrade silently.

---

## 4. Storage info accuracy

### 4.1 `handle_get_storage_info` — `transcripts_count` only counts `*.md` files

Line 1319:

```python
md_files = list(transcripts_dir.glob("*.md")) if transcripts_dir.exists() else []
transcripts_count = len(md_files)
```

`transcripts_size_mb` is derived only from `.md` files, while `total_bytes` (line 1325) includes **all** files recursively (`rglob("*")`). This means `.srt`, `.json`, `.csv`, `.html` exports saved in `transcripts/` are counted in `total_data_mb` but **not** in `transcripts_size_mb`. The discrepancy makes `total_data_mb` appear larger than the sum of its documented sub-totals.

**Recommendation:** rename `transcripts_size_mb` to `transcripts_md_size_mb` and add a `transcripts_all_size_mb` covering all file types, or compute `transcripts_size_mb` over all files in the directory.

### 4.2 `handle_get_storage_info` — `reports_count` uses two disconnected globs, may double-count

Line 1323:

```python
reports_count = len(list(data_dir.glob("*.report")) + list(data_dir.glob("report_*")))
```

A file named `report_something.report` matches *both* globs and is counted twice.  
A `set` union would fix this:

```python
reports_count = len(
    set(data_dir.glob("*.report")) | set(data_dir.glob("report_*"))
)
```

### 4.3 `handle_get_storage_info` — no stat() error handling

`f.stat().st_size` inside `rglob` (line 1326) raises `FileNotFoundError` if a file is deleted between the `rglob` walk and the `stat()` call (TOCTOU). This would crash the handler entirely. A `try/except OSError: continue` guard should wrap each `stat()` call.

### 4.4 `handle_get_storage_info` — `tombstones_path` and `settings.json` excluded from reported sizes

`history_bytes` covers only `history.ndjson`. The tombstones file (`history_tombstones.ndjson`) and `settings.json` are not individually reported, even though they are included in `total_bytes`. Consumers comparing `history_bytes + transcripts_size_mb ≈ total_data_mb` will see an unexplained gap when tombstones are large (heavy-delete workloads).

---

## 5. Additional findings

### 5.1 `handle_export_history_csv` — `save_path` truthy-check conflates `True` with a path string

Lines 1199–1205:

```python
if save_path or save_path is True:
    ...
    file_path = transcripts_dir / fname
    file_path.write_text(...)
    file_path = str(file_path)
```

If a caller passes `"save_to_file": true` (boolean), `params.get("save_to_file")` returns `True`, which then satisfies `save_path is True`. But if the caller passes `"save_to_file": "/custom/path"`, the custom path is ignored — the file is always written to `transcripts/`. Unlike `handle_export_history_json` (which uses `self._coerce_bool`), the CSV handler treats the parameter as both a boolean and a path string. The intent should be clarified and unified with the other export handlers.

### 5.2 `handle_export_obsidian` — `output_dir` param has no path-traversal guard

Line 2103:

```python
out_dir = Path(output_dir_param).expanduser().resolve()
```

No `allowed_roots` check. A caller can write the Obsidian export to any writable path on the filesystem. Low severity (IPC is local-only), but inconsistent with `handle_import_history_ndjson`.

### 5.3 `_STOP_WORDS` class-level `__import__` at module parse time

Lines 2249–2254: the `_STOP_WORDS` frozenset is evaluated when the class body is parsed (import time). If `core.stop_words` fails to import (missing venv, import error), the entire `history_service` module fails to load, blocking all history IPC handlers. A lazy property or `try/except` fallback would isolate this optional dependency.

---

## Summary

| # | Severity | Area | Description |
|---|----------|------|-------------|
| 1 | LOW | CRUD | `paste_status` not validated against enum |
| 2 | MEDIUM | CRUD | `handle_get_history_item` O(N) scan; `get_history_item_by_id` exists |
| 3 | MEDIUM | CRUD | Tag add/remove TOCTOU (two-step read-modify-write without lock) |
| 4 | HIGH | CRUD | `handle_cleanup_old_history` lexicographic ts comparison fails for tz-naive items |
| 5 | MEDIUM | CRUD | `handle_restore_history` no path-traversal guard (cf. import handler which does have one) |
| 6 | LOW | SRT | `handle_export_history_srt` O(N/100) page-scan to find one item; `get_history_item_by_id` preferred |
| 7 | LOW | SRT | `_build_srt_single` undocumented 1-second fallback for zero-duration items |
| 8 | LOW | Clipboard | 20-item cap only enforced at read; write-side trim lives in BackendService |
| 9 | MEDIUM | Storage | `transcripts_size_mb` undercounts — only `.md` files; `.srt`/`.json`/`.csv`/`.html` excluded |
| 10 | LOW | Storage | `reports_count` double-counts files matching both globs |
| 11 | LOW | Storage | `rglob` `stat()` calls unguarded against TOCTOU `FileNotFoundError` |
| 12 | LOW | Storage | `tombstones_path` size not individually reported despite being included in `total_bytes` |
| 13 | LOW | Export | CSV `save_to_file` conflates bool and path-string; inconsistent with JSON handler |
| 14 | LOW | Export | `handle_export_obsidian` `output_dir` no path-traversal guard |
| 15 | LOW | Module | `_STOP_WORDS` class-level import fails hard at module load time |

**Total findings: 15 (1 HIGH, 4 MEDIUM, 10 LOW)**

The highest-priority item is finding #4 (tz-naive cleanup), which can silently skip deletions or delete wrong items in mixed-timezone stores.
