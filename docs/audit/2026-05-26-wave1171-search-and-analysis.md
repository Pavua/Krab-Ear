# Wave 1171 Audit — Search & Analysis IPC Dispatch

**Date:** 2026-05-26  
**Scope:** IPC handlers for `fuzzy_search`, `semantic_search`, `search_index` usage, and `get_analytics_dashboard`  
**Files inspected:**
- `KrabEar/backend/history_service.py` (`handle_fuzzy_search`, `handle_search_history`, `handle_delete_history_item`)
- `KrabEar/backend/service.py` (`_handle_semantic_search`, `_handle_semantic_search_reindex`, `_handle_get_analytics_dashboard`)
- `KrabEar/core/fuzzy_search.py` (`FuzzySearcher`)
- `KrabEar/core/search_index.py` (`SearchIndex`)
- `KrabEar/backend/semantic_search.py` (`SemanticSearcher`, `keyword_fallback_search`)
- `KrabEar/backend/analytics_dashboard.py` (`AnalyticsDashboard`)

**Related open PRs / branches not yet merged into `codex/krab-ear-v2`:**
- `fix/fuzzy-search-privacy-W1007` — adds `_is_privacy_mode()` guard to `handle_fuzzy_search` (PR open)
- `fix/search-index-W1041` — adds `threading.RLock` to `SearchIndex` (PR open)
- `fix-search-index-es-diacritics-W1042` — adds Unicode tokenizer + `limit<0` guard (PR open)
- `fix-semantic-search-delete-W1163` — adds semantic remove on `delete_history_item` (PR open, contains bug — see F2)

---

## Findings

### F1 — HIGH | Privacy mode not enforced on `semantic_search` or `search_history`

**Location:** `KrabEar/backend/service.py:666-698` (`_handle_semantic_search`); `KrabEar/backend/history_service.py:99-124` (`handle_search_history`)

`handle_fuzzy_search` has a partial privacy guard being added via the unmerged branch `fix/fuzzy-search-privacy-W1007`. However, `_handle_semantic_search` and `handle_search_history` have **no privacy check on the current `codex/krab-ear-v2` branch**.

In `_handle_semantic_search`, the keyword fallback path (lines 684–688 and 692–696) loads `self.store._load_active_items_with_lock()` unconditionally and calls `keyword_fallback_search()` which scans full transcript text. This path fires even when `semantic_search_enabled=False` (the disabled fallback). When `privacy_mode_enabled=True`, callers still receive transcription text matches.

Consistent with W1003/W1007 which targeted `fuzzy_search`, the same pattern needs to cover semantic search and `search_history`.

**Fix:** Add `_is_privacy_mode()` check (or equivalent) to `_handle_semantic_search` before any history load, and to `handle_search_history` before calling `store.search_history()`. Return `{"results": [], "mode": "disabled", "reason": "privacy_mode_active"}` when active.

---

### F2 — HIGH | W1163 `delete_history_item` calls non-existent method `SemanticSearcher.remove()`

**Location:** `fix-semantic-search-delete-W1163` branch — `KrabEar/backend/history_service.py:254`

The W1163 fix PR (currently open, not yet merged) wires semantic index cleanup into `handle_delete_history_item`. However, it calls `self._semantic_searcher.remove(item_id)`, but the actual API on `SemanticSearcher` is `remove_item(item_id)` (defined at `semantic_search.py:211`).

If this PR is merged as-is, every delete that goes through `handle_delete_history_item` will raise `AttributeError: 'SemanticSearcher' object has no attribute 'remove'`. The error is wrapped in `try/except` so it silently logs a warning and the item remains stale in the semantic index — this is the exact W1148 F1 bug the PR intended to fix.

The W1148 F1 semantic delete wiring is therefore still broken on both the current main branch (no wiring at all) and the pending fix PR (wrong method name).

**Fix:** In `fix-semantic-search-delete-W1163`, change `self._semantic_searcher.remove(item_id)` → `self._semantic_searcher.remove_item(item_id)`.

---

### F3 — MED | `fuzzy_search` silently truncates history at 500 items (limit cap mismatch)

**Location:** `KrabEar/backend/history_service.py:147-155`; `KrabEar/backend/state_store.py:270`

`handle_fuzzy_search` calls `store.get_history_page_filtered(limit=10_000, ...)` intending to load the full history for exhaustive fuzzy matching. However, `StateStore.get_history_page_filtered` applies a hard cap of `safe_limit = max(1, min(limit, 500))` at line 270. Any history larger than 500 items is silently truncated, and fuzzy search returns incomplete results without any signal in the response dict.

A user with 600+ transcriptions gets silently truncated search results. No `truncated: True` or `total_searched: N` field is returned.

**Fix (option A):** Use `store._load_active_items_with_lock()` directly (like `_handle_semantic_search` already does) for fuzzy search's full-scan use case.  
**Fix (option B):** Return a `total_searched`/`truncated` field so callers know the corpus was partial.

---

### F4 — MED | `SearchIndex` has no thread safety (W1041 fix unmerged)

**Location:** `KrabEar/core/search_index.py:84-221`; fix in `fix/search-index-W1041` (open PR)

The `SearchIndex` class has no `threading.Lock` or `RLock`. The `_index` dict and `_texts` dict are mutated during `build_index()` while `search()` reads them. In the backend, `build_index` is called from IPC handlers that may run from multiple concurrent connections (the IPC server spawns a thread per connection). A concurrent `build_index` + `search` is a data race: partial mutation of `_index` during a `search` iteration yields undefined results (KeyError or missing items silently).

The fix branch `fix/search-index-W1041` adds a `threading.RLock` wrapping all mutations and reads, but it has not been merged into `codex/krab-ear-v2`.

**Status:** Fix exists (PR open), not yet merged.

---

### F5 — LOW | `AnalyticsDashboard` accesses `StateStore` private API (`_lock()` + `_load_active_items_unlocked()`)

**Location:** `KrabEar/backend/analytics_dashboard.py:81-85`

`_build_dashboard` reaches into `store._lock()` (a `@contextmanager`-decorated internal file-lock) and `store._load_active_items_unlocked()` (an underscore-prefixed helper intended for internal use only). This coupling means any rename/refactor of those private APIs silently breaks the dashboard without a compile error.

There is a `try/except` guard that catches failure and falls back to `active = []`, so the dashboard does not crash — it returns all-zero metrics, which is misleading (looks like an empty history rather than a load error). The `except Exception: logger.exception(...)` swallows the error without surfacing it in the IPC response.

`StateStore` already has a public `_load_active_items_with_lock()` method (line 836) that encapsulates the same pattern. The dashboard should use this instead.

**Fix:** Replace `with store._lock(): active = store._load_active_items_unlocked()` with `active = store._load_active_items_with_lock()`. The private `_lock()` + `_load_active_items_unlocked()` pair is not needed externally.

---

## IPC Error Isolation Assessment

Each search/analytics handler runs inside the `try/except Exception` block at `service.py:1290-1295`. A failure in any one backend (e.g., `SemanticSearcher` model crash, `AnalyticsDashboard` store failure) returns an IPC error response for that specific call and does not propagate to other handlers. The IPC server is isolated at the per-request level.

Individual backends are also well-isolated from each other: `fuzzy_search` delegates entirely to `HistoryService`, `semantic_search` is handled directly in `BackendService`, and `get_analytics_dashboard` delegates to the `AnalyticsDashboard` singleton. A failure in any one does not affect the others.

---

## Test Coverage Gaps

| Gap | Severity |
|-----|----------|
| No test for `privacy_mode_enabled=True` on `fuzzy_search`, `semantic_search`, or `search_history` IPC paths | HIGH |
| No test verifying `fuzzy_search` corpus size cap (history > 500 items returns truncated results) | MED |
| No test for concurrent `SearchIndex.build_index()` + `search()` (race condition, W1041) | MED |
| `test_semantic_search_remove.py` tests `SemanticSearcher.remove_item()` in isolation but no test verifies the IPC wiring from `delete_history_item` through to `remove_item` | MED |

---

## Summary

| # | Severity | Title | Status |
|---|----------|-------|--------|
| F1 | HIGH | Privacy mode not enforced on `semantic_search` / `search_history` | On main; W1007 partial (fuzzy only, unmerged) |
| F2 | HIGH | W1163 delete fix calls `.remove()` not `.remove_item()` — AttributeError in production if merged as-is | In pending PR |
| F3 | MED | `fuzzy_search` silently truncated at 500 items (limit cap mismatch) | On main |
| F4 | MED | `SearchIndex` no thread safety (W1041 fix unmerged) | In pending PR |
| F5 | LOW | `AnalyticsDashboard` uses private `StateStore` API, misleading silent failure | On main |
