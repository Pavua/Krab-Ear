# Audit W1243 — AutoDeduplicator (`backend/auto_deduplication.py`)

**Date:** 2026-05-26  
**Branch:** `audit/auto-deduplication-W1243`  
**Auditor:** W1243 (sub-agent, read-only)

---

## Scope

`KrabEar/backend/auto_deduplication.py` — `AutoDeduplicator`: automatically skip or merge near-duplicate history items above a configurable similarity threshold.

Companion file audited: `KrabEar/core/duplicate_detector.py` (`DuplicateDetector`).  
Service wiring: `KrabEar/backend/service.py` lines 441, 1123–1125, 3566–3598.  
Tests: `KrabEar/tests/test_auto_deduplication.py`.

---

## Findings (5)

---

### F-1 — MEDIUM: Similarity algorithm is `SequenceMatcher` (Ratcliff/Obershelp), not cosine/Jaccard — undocumented and asymmetric for short texts

**File:** `core/duplicate_detector.py:33`, `duplicate_detector.py:103`

`DuplicateDetector.is_duplicate` and `find_duplicates` both use `difflib.SequenceMatcher(None, text1, text2).ratio()`. This is the Ratcliff/Obershelp character-level sequence similarity, **not** cosine or Jaccard. The module docstring says "текстовое сходство via SequenceMatcher", which is correct, but the choice has subtle consequences:

1. **Character-level, not token-level.** "Привет" vs "привет" (case difference) yields ratio ≈ 0.91, passing a 0.9 threshold as a duplicate even though casing differences in STT are common and intentional.
2. **Length asymmetry.** A short transcript that is a prefix of a longer one (e.g., partial re-trigger) scores high: `SequenceMatcher(None, "hello", "hello world").ratio()` = `2*5/11 ≈ 0.91`. This can cause a 5-word re-trigger to suppress a 9-word follow-on as a "duplicate".
3. **Performance:** `SequenceMatcher.ratio()` is O(n·m) on character counts, so for long Russian transcripts (1000+ chars each) each pairwise comparison is expensive. The 60-second window in `check_duplicate` limits exposed pairs to ~50 recent items, so the hot path is safe. But `run_deduplication` has no such mitigation (see F-2).

**No existing test covers the prefix-suppression case or case-only differences.**

**Recommendation:** Document that the algorithm is Ratcliff/Obershelp, not semantic. Add a case-normalisation step (`text.casefold()`) before comparison. Add a min-length ratio guard: skip comparison if `len(shorter)/len(longer) < 0.5` to avoid short-prefix false positives.

---

### F-2 — HIGH: `run_deduplication` is O(n²) on full history with no time-window protection

**File:** `backend/auto_deduplication.py:203`, `core/duplicate_detector.py:77–117`

`run_deduplication` loads the entire history with unbounded pagination (limit=200 per page) and passes **all items** to `find_duplicates`. `find_duplicates` is an O(n²) nested loop: for each item `i` it iterates all items `j > i`. The 60-second timestamp window (`abs(ts_i - ts_j) > 60`) provides a skip-continue path, but **only when both timestamps are non-None**. Items without a `ts` field (legacy entries, imported audio) have `_get_timestamp()` return `None`, which causes the window check to be skipped (`if ts_i is not None and ts_j is not None: continue`). For a history of 10,000 items with missing timestamps, every pair is compared via `SequenceMatcher`, yielding up to 50 million character-level comparisons. On an M4 Max with typical 200-char Russian transcripts, this would block the IPC thread for tens of seconds.

Additionally, `handle_run_deduplication` runs synchronously in the IPC handler thread — there is no async offload, no progress reporting, and no time budget. The IPC client (Swift) has a default socket timeout; a full 10k-item scan will hit that timeout.

**Recommendation:** (1) Add a hard cap (e.g., `max_items=5000`) with a `truncated=True` flag in the response. (2) Always sort items by timestamp and use a sliding-window approach: advance a pointer while `ts[j] - ts[i] > 60` instead of checking every pair. (3) Offload to a background thread or return a job ID for large scans.

---

### F-3 — HIGH: `auto_dedup_enabled` flag is never consulted on the history-persist path — deduplication is entirely opt-in IPC-only

**File:** `backend/recording_core_service.py:1105`, `backend/recording_core_service.py:1340`, `backend/service.py:441`

`AUTO_DEDUP_ENABLED` defaults to `False` in `auto_deduplication.py:29` and `DEFAULT_SETTINGS["auto_dedup_enabled"]` is also `False` in `core/config.py:850`. So the feature is off by default — that is correct.

However, **even when enabled via settings**, `AutoDeduplicator.check_duplicate` is **never called automatically** during the normal recording completion flow. Tracing the persist path:

- `_stop_recording_phase_e` (`recording_core_service.py:1094`) → `self.store.add_history_item(...)` — no dedup call.
- `_handle_transcribe_paths` batch-import path (`recording_core_service.py:1340`) → `self.store.add_history_item(...)` — no dedup call.
- The `add_history_item` IPC handler (`service.py:926`) delegates directly to `HistoryService.handle_add_history_item` — no dedup call.

`check_duplicate` is only reachable via explicit IPC calls (`check_duplicate`, `run_deduplication`). No caller automatically gates `add_history_item` on the dedup result. The `auto_dedup_enabled` runtime setting exists in `DEFAULT_SETTINGS` and `config.py`, but no code path reads it to activate automatic deduplication.

**The feature is effectively a no-op in production.** It only works if the Swift client explicitly calls `check_duplicate` before every `add_history_item`, which the current Swift source does not do (confirmed: no `check_duplicate` calls in `native/`).

**Recommendation:** Wire `check_duplicate` into `recording_core_service._stop_recording_phase_e` gated by `settings.get("auto_dedup_enabled")`. If `is_duplicate=True` and `action_taken="skipped"`, skip `store.add_history_item`. Log the skipped event at DEBUG level.

---

### F-4 — MEDIUM: `privacy_mode_enabled` not respected — `check_duplicate` reads history text even in privacy mode

**File:** `backend/auto_deduplication.py:96`, `backend/auto_deduplication.py:107–114`

When `privacy_mode_enabled=True`, the translation service (`translation_service.py:96`, `201`) skips external calls that would expose transcript text. There is no equivalent guard in `AutoDeduplicator.check_duplicate`. Under privacy mode, `check_duplicate` still:

1. Calls `store.get_history_page(limit=50)` — loading recent transcript texts.
2. Appends a `new_item` dict containing the raw `text` of the new transcription.
3. Passes all texts to `_detector.find_duplicates` where they are compared character-by-character via `SequenceMatcher`.

All processing is local (no network), so there is no external privacy leak. However, the design intent of privacy mode (minimize in-memory transcript text handling) is violated — the dedup path materialises up to 50 recent texts into a Python list even when the user has opted into privacy mode. If F-3 is fixed (dedup wired into the persist path), this becomes a real privacy-mode compliance issue.

**Recommendation:** In `check_duplicate`, read `settings.get("privacy_mode_enabled", False)` (inject settings or read via `_get_runtime_setting`) and return `DedupResult(is_duplicate=False, ..., action_taken="kept")` immediately when privacy mode is active.

---

### F-5 — MEDIUM: Concurrent-insert race — two simultaneous same-text transcriptions both pass `check_duplicate` and both get stored

**File:** `backend/auto_deduplication.py:91–161`

`check_duplicate` uses `self._lock` (RLock) only around counter increments (`_total_checked`, `_duplicates_found`, `_chars_saved`). The critical section — `store.get_history_page` → `find_duplicates` — is **outside the lock**. If two recording sessions complete simultaneously with the same text (e.g., double-tap hotkey, or concurrent file-import batch):

1. Thread A: `get_history_page` → sees no existing item for `text_X` → returns `is_duplicate=False`.
2. Thread B: `get_history_page` (before Thread A has called `add_history_item`) → also sees no item → returns `is_duplicate=False`.
3. Both threads proceed to `add_history_item` → two identical entries in the store.

The lock would need to cover the full read-check-write cycle to prevent this race, which would require also holding it during `add_history_item`. This is architecturally difficult since `store` is external to `AutoDeduplicator`.

Note: the race only matters if F-3 is ever fixed (dedup wired into the persist path). In the current state it is a latent issue.

**Recommendation:** The clean solution is a single `_dedup_lock` in `RecordingCoreService` or `BackendService` that serialises the `check_duplicate + add_history_item` sequence. Alternatively, accept a best-effort dedup semantic with a post-hoc `run_deduplication` sweep and document the race clearly.

---

## Summary

| # | Severity | Finding |
|---|----------|---------|
| F-1 | MEDIUM | SequenceMatcher is character-level Ratcliff/Obershelp; prefix texts and case-only differences can cause false-positive duplicate suppression |
| F-2 | HIGH | `run_deduplication` is O(n²) on full history; items with missing `ts` bypass the 60s window guard; no async offload; IPC timeout risk |
| F-3 | HIGH | `auto_dedup_enabled` flag never consulted on history-persist path; `AutoDeduplicator` is an IPC-only utility, not wired into recording completion |
| F-4 | MEDIUM | `check_duplicate` materialises recent transcript texts ignoring `privacy_mode_enabled` |
| F-5 | MEDIUM | Concurrent-insert race: two simultaneous same-text calls both pass `check_duplicate` before either writes to the store |

**Wire status:** Not wired into the automatic transcription flow. The `auto_dedup_enabled` setting has no runtime effect.  
**Test coverage:** Good for the IPC interface and unit logic. Missing: prefix-suppression case, case-only text, privacy-mode bypass, concurrent-insert race.  
**Semantic search interaction (W1148):** `SemanticSearcher.remove_item` is never called when a duplicate is "skipped". If F-3 is fixed, items that are skipped will never be indexed in the semantic search, but if an item is stored first and then detected as a duplicate by `run_deduplication`, the semantic index will have a stale entry that `remove_item` should clean up — currently no code does this.
