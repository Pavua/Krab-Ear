# W1526 Regression Audit — `history_service.py` post-W1497 cherry-pick train

**Date:** 2026-05-27  
**Auditor:** W1526 sub-agent  
**File:** `KrabEar/backend/history_service.py`  
**Trigger:** W1497 cherry-pick train used `--theirs` strategy; verify shipped fixes survived.

---

## Audit scope

| Wave | Fix description | Expected signature |
|------|----------------|-------------------|
| W844 | `handle_cleanup_old_history` — tz-aware POSIX timestamp comparison | `_item_ts()` helper, `cutoff_ts = cutoff.timestamp()` |
| W869 | `handle_import_history_ndjson` — path allowlist | `allowed_roots` check in import handler |
| W1163/W1172 | `semantic_search.remove_item()` called on delete | `semantic_searcher` kwarg in `__init__`, `_semantic_searcher` attribute |
| W1176/W1432 | Export path allowlist (instance + module-level) | `_resolve_export_dir()`, `_EXPORT_ALLOWED_ROOTS`, `_is_safe_export_dir()` |
| W1431 | Same as W1163/W1172 but definitive re-fix | Same `semantic_searcher` inject + `remove_item` call site |
| W1433 | SRT contiguous sequence numbers | Manual `idx` counter, no `enumerate(turns, start=1)` |

---

## Findings

### F1 — W844 REGRESSED (HIGH)
**Status:** REGRESSED  
**Location:** `handle_cleanup_old_history` (~line 1304)  
**Evidence:** Current code uses `cutoff_iso = cutoff.isoformat()` and `item.ts < cutoff_iso`
(lexicographic string comparison). W844 replaced this with a proper POSIX float comparison
via `_item_ts(ts_str)` helper returning `datetime.fromisoformat(...).timestamp()`.  
**Impact:** tz-naive ISO strings (e.g. `"2026-01-01T12:00:00"`) compare incorrectly against
tz-aware cutoff strings (e.g. `"2026-01-01T12:00:00+00:00"`), causing `cleanup_old_history`
to silently skip entries that should be deleted. Entries accumulate indefinitely.  
**Fix:** Re-apply W844 diff: introduce `_item_ts()` helper, replace string comparison with
`_item_ts(item.ts) < cutoff_ts`.

---

### F2 — W1163 / W1172 / W1431 REGRESSED (HIGH)
**Status:** REGRESSED — all three waves lost  
**Location:** `HistoryService.__init__` (~line 34), `handle_delete_history_item` (~line 245)  
**Evidence:**
- `__init__` signature has no `semantic_searcher` kwarg (only `store`, `clipboard_history`,
  `llm_rewriter`, `cached_settings`).
- No `self._semantic_searcher` attribute anywhere in the file.
- `handle_delete_history_item` does not call `.remove_item()` or `.remove()` after successful
  store delete.  
**Impact:** Deleting a history item does NOT remove its embedding from the semantic search
index. Stale item IDs are returned by semantic search indefinitely after deletion, causing
phantom search results.  
**Fix:** Re-apply W1431 (most complete version): add `semantic_searcher: Any | None = None`
kwarg, set `self._semantic_searcher = semantic_searcher`, call
`self._semantic_searcher.remove_item(item_id)` in `handle_delete_history_item` with
exception catch+warning.

---

### F3 — W1176 / W1432 REGRESSED (HIGH)
**Status:** REGRESSED — both the instance helper and module-level predicate are absent  
**Location:** Module top (~line 27), `HistoryService` class body  
**Evidence:**
- `grep '_EXPORT_ALLOWED_ROOTS\|_is_safe_export_dir'` → no results.
- `grep '_resolve_export_dir\|_ALLOWED_EXPORT_ROOTS'` → no results.
- `handle_export_obsidian` and `handle_batch_export` call `Path(output_dir_param).expanduser().resolve()` but apply no allowlist validation before writing.  
**Impact:** IPC callers can pass arbitrary `output_dir` values (e.g. `~/.ssh/`,
`~/Library/Keychains/`, `/etc/`) and the export handler will write files there without
restriction. Path-traversal write primitive via any authenticated IPC caller.  
**Fix:** Re-apply W1176 (`_resolve_export_dir()` instance method) and W1432
(`_EXPORT_ALLOWED_ROOTS` list + `_is_safe_export_dir()` module-level bool predicate).

---

### F4 — W1433 REGRESSED (LOW)
**Status:** REGRESSED  
**Location:** `handle_export_history_srt` SRT loop (~line 777)  
**Evidence:** Current code: `for seq, turn in enumerate(turns, start=1): ... if not turn_text: continue ... srt_lines.append(str(seq))`.
When empty turns are skipped via `continue`, the outer `seq` counter still advances, producing
non-contiguous SRT sequence numbers (e.g. 1, 3, 4 instead of 1, 2, 3).  
**Impact:** Non-contiguous SRT sequence numbers cause some SRT players to reject or misparse
the subtitle file (RFC 5646 / SRT format requires strictly ascending 1-based integers with no
gaps).  
**Fix:** Replace `enumerate(turns, start=1)` with a manual `idx = 0` counter incremented
only after the `if not turn_text: continue` guard (W1433 exact patch).

---

### F5 — W869 PRESENT (pass)
**Status:** NOT REGRESSED  
**Location:** `handle_import_history_ndjson` (~line 286)  
**Evidence:** `allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]` + `if not any(str(resolved).startswith(str(root)) for root in allowed_roots)` guard is intact.  
**Impact:** None — fix survived the cherry-pick train.

---

## Summary table

| Finding | Wave(s) | Severity | Status |
|---------|---------|----------|--------|
| F1 — tz-aware cleanup comparison | W844 | HIGH | REGRESSED |
| F2 — semantic_search delete cascade | W1163/W1172/W1431 | HIGH | REGRESSED |
| F3 — export path allowlist | W1176/W1432 | HIGH | REGRESSED |
| F4 — SRT contiguous sequence numbers | W1433 | LOW | REGRESSED |
| F5 — import path traversal guard | W869 | HIGH | PRESENT |

**Total regressed:** 4 of 5 checked (cap reached).  
**Root cause:** W1497 cherry-pick train applied `--theirs` on merge conflicts, taking a
base version of `history_service.py` that predates W844, W1163, W1172, W1176, W1431, W1432,
and W1433. Only W869 survived because it was applied to a different code region that did not
conflict.

---

## Recommended action

Re-apply all four regressed fixes as a single consolidating PR (W1527 or similar):
1. W844 tz-aware cleanup (HIGH — data loss risk)
2. W1431 semantic_searcher inject + delete cascade (HIGH — phantom search results)
3. W1176 + W1432 export allowlist (HIGH — path-traversal write)
4. W1433 SRT sequence counter (LOW — broken SRT output)
