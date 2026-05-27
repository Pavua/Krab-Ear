# W1148 Re-audit: `backend/semantic_search.py` — Residual Issues

**Date:** 2026-05-26
**File:** `KrabEar/backend/semantic_search.py` (358 LOC post-W901)
**Prior audits:** W823 (concurrency/O(N) scan), W884 (cold-start/model-error/cache/hybrid)
**Scope (this audit):** Verify W884 E2 + W911 E3 merge state; find NEW residual issues.

---

## Merge State Verification

### W884 E2 — Shape consistency check on disk load

**Status: MERGED** (commit `c2da81f0`, PR #817, in `codex/krab-ear-v2`).

`_load_from_disk()` now guards against corrupted on-disk state:
```python
if embeddings.shape[0] != len(index):
    logger.error(
        "semantic_search: несоответствие размеров при загрузке с диска "
        "(embeddings=%d, index=%d) — пропускаем загрузку, ...",
        embeddings.shape[0], len(index),
    )
    return
```
The guard is correct and sufficient for the crash-recovery case.

### W911 E3 — Transient model error reset (`reset_model_error`)

**Status: NOT MERGED into `codex/krab-ear-v2`.**

The commit `782e1bfe` (feat(wave911)) exists but lives only on
`origin/feature/fix-semantic-model-reset-W911` — it was never merged to the main branch.
`KrabEar/backend/semantic_search.py` on `codex/krab-ear-v2` has no `reset_model_error()` method
(confirmed by AST: only 16 methods present, `reset_model_error` absent).
`KrabEar/backend/search_and_analysis_service.py` and `KrabEar/backend/ipc_dispatch.py` likewise
have no `handle_semantic_search_reset` or dispatch entry.

The transient-error permanent-gate bug (W884 E3) remains open in production.

---

## New Findings (5 max)

### F1 — `delete_history_item` does NOT call `semantic_searcher.remove_item` (HIGH)

**Location:** `KrabEar/backend/history_service.py:239–253`

When a user deletes a history item (`delete_history_item` IPC), the NDJSON store receives a
tombstone and the item disappears from all history queries. However, `handle_delete_history_item`
only calls `self.store.delete_history_item(item_id)` — it never notifies `SemanticSearcher`.

`SemanticSearcher.remove_item()` exists and is correct (thread-safe, persists to disk), but it is
never called on deletion. The result:

1. The deleted item remains in `embeddings.npy` and `embeddings_index.json` permanently.
2. `search()` will continue returning the deleted item's `id` in results.
3. The GUI or caller must silently drop results for item IDs that no longer exist in history —
   a contract that is not documented and no test verifies.
4. Over time, the in-memory index and on-disk embeddings files grow monotonically even as history
   is cleaned up, diverging from the active history set.

`SemanticSearcher` also has no `clear_all()` method, so `cleanup_old_history` (batch delete older
than N days) has the same gap: it purges NDJSON entries but leaves all their embeddings intact.

**Fix:** In `HistoryService.handle_delete_history_item`, after `self.store.delete_history_item(item_id)`,
call `self._semantic_searcher.remove_item(item_id)` (needs `_semantic_searcher` injected into
`HistoryService`, currently absent — it is only passed to `RecordingCoreService` and
`SearchAndAnalysisService`). Similarly, `handle_cleanup_old_history` should batch-call
`remove_item` for each deleted id. Alternatively, expose a `clear_all()` method and call it before
`compact_with_stats()`.

**Coverage gap:** No test asserts that deleting a history item removes it from the semantic index.

---

### F2 — Non-atomic dual-file save creates crash window (MEDIUM)

**Location:** `_save_locked()` lines 322–331

`_save_locked` writes two files in sequence without atomicity:
```python
np.save(str(self._embeddings_path), self._embeddings)   # write 1
with open(self._index_path, "w", encoding="utf-8") as f:  # write 2
    json.dump(self._index, f, ...)
```

If the process is killed between write 1 and write 2 (or between write 2 beginning and closing),
the on-disk state is inconsistent. On next startup `_load_from_disk` finds both files present
(write 1 succeeded) but the second file is incomplete JSON (`json.load` raises) or has stale
content from a prior save — leading to either a crash or mismatched shape.

The W884 E2 shape-consistency guard (now merged) handles the size-mismatch case and returns
safely, but it does not handle the case where `embeddings_index.json` contains truncated/invalid
JSON (which raises `json.JSONDecodeError` inside `_load_from_disk`, caught by the outer `except
Exception` that logs a warning and leaves the in-memory state empty).

**Pattern already used elsewhere in the project:** `translation_cache.py` (line 118) and
`rest_auth.py` (line 92) both use `os.replace(tmp_path, self._path)` for atomic writes. `np.save`
writes to `embeddings.npy.npy` (numpy adds `.npy` extension if not present, or writes in-place).

**Fix:** Write `embeddings.npy` to a temp file first, then write `embeddings_index.json` to a
temp file, then `os.replace` both atomically:
```python
import os, tempfile
tmp_emb = self._embeddings_path.with_suffix(".tmp")
tmp_idx = self._index_path.with_suffix(".tmp")
np.save(str(tmp_emb), self._embeddings)
with open(tmp_idx, "w", encoding="utf-8") as f:
    json.dump(self._index, f, ensure_ascii=False)
os.replace(tmp_emb, self._embeddings_path)
os.replace(tmp_idx, self._index_path)
```
Note: `np.save` will append `.npy` to the `.tmp` name unless the path already ends in `.npy`.
Use `tmp_emb = Path(str(self._embeddings_path) + ".tmp")` to avoid the double-extension.

**Coverage gap:** No test simulates a mid-save kill and verifies safe recovery on next load.

---

### F3 — Embeddings indexed from raw transcript text, not anonymized text (MEDIUM)

**Location:** `recording_core_service.py:1225–1233` (auto-index on transcription)

When semantic indexing is triggered after a recording:
```python
_index_text = display_text or text
self._semantic_searcher.index_item(_index_id, _index_text)
```

`display_text` is the user-visible transcript but is **not** the anonymized/PII-redacted version.
The `TextAnonymizer` module (`core/text_anonymizer.py`) exists and is called in the text
post-processing pipeline, but the anonymized output is not what gets passed to `index_item`.

The `text_postprocessor.py` pipeline produces an `anonymized_text` field; `display_text` is the
version shown to the user (which may include raw PII depending on `anonymize_enabled` setting).

Concretely, if a user dictates "позвони Ивану Петровичу на +7 916 123-45-67", the phone number
and name are present in the raw embedding stored at rest in `embeddings.npy`. Since embeddings
encode semantic content, the phone number and full name are effectively searchable via cosine
similarity. This creates a privacy surface where embeddings.npy contains PII even when the user
has enabled privacy/anonymization mode.

Furthermore, when `purge_history` is called (via `cleanup_old_history` or a full wipe), embeddings
are not cleared (see F1), so PII survives the purge.

**Fix:** When `anonymize_enabled` is True in settings, pass the anonymized text to `index_item`
rather than `display_text`. The `phase_d` dict already contains `tp.get("anonymized_text", "")`
(from `TextPostProcessor`). Add a branch:
```python
_index_text = tp.get("anonymized_text") or display_text or text
```
Also: add `embeddings.npy` and `embeddings_index.json` to the list of files cleared by
`_handle_purge_history` (currently not present).

**Coverage gap:** No test verifies that PII-redacted text (not raw text) is passed to `index_item`
when anonymization is enabled.

---

### F4 — W911 `reset_model_error` branch not merged — permanent error gate remains open (MEDIUM)

**Location:** `_get_model()` lines 241–266 (no `reset_model_error` in codex/krab-ear-v2)

This is the W884 E3 finding that was fixed in `feature/fix-semantic-model-reset-W911` but
not yet merged to `codex/krab-ear-v2`. A transient load failure (HuggingFace network timeout,
temporary disk full, OOM during model download) sets `_model_error` permanently. All subsequent
calls to `_get_model()` fast-return `None` for the rest of the backend session. The user must
restart the entire backend process to retry.

The `reset_model_error()` fix is already written and tested in the feature branch — it just needs
to be merged.

**Immediate action:** Merge `feature/fix-semantic-model-reset-W911` into `codex/krab-ear-v2`.

---

### F5 — No throttle on `index_all` / reindex — can block for minutes on large histories (LOW)

**Location:** `search_and_analysis_service.py:134–148` (`handle_semantic_search_reindex`)

`handle_semantic_search_reindex` is a synchronous IPC handler that calls `index_all` for the
entire active history. For a user with 10,000 history items, `_encode_batch` encodes all items
in one shot via `SentenceTransformer.encode(batch)` — this runs on CPU for multilingual-e5-base
and takes approximately 1–5 seconds per 100 items. At 10k items that is 100–500 seconds, blocking
the IPC socket thread for the full duration.

There is no chunking, no progress reporting, and no cancellation mechanism. Other IPC calls that
arrive during reindex will time out on the Swift side (30s socket timeout per `IPCConstants`).

The `IPCThrottle` module exists (`backend/ipc_throttle.py`) but `semantic_search_reindex` is not
listed in the throttled methods in `ipc_dispatch.py`.

**Fix (short term):** Add `semantic_search_reindex` to `IPCThrottle` with a 60s minimum interval
to prevent rapid consecutive calls. For the blocking issue, move `index_all` off the IPC dispatch
thread via `threading.Thread(target=..., daemon=True)` and return `{"status": "started"}` with a
job ID, similar to `_handle_transcribe_audio_file` async pattern.

**Coverage gap:** No test asserts that concurrent reindex calls are rate-limited.

---

## Summary Table

| ID | Severity | Description | W884 ref |
|----|----------|-------------|----------|
| F1 | HIGH     | `delete_history_item` never removes from semantic index → ghost results + unbounded index | new |
| F2 | MEDIUM   | Non-atomic dual-file save (`np.save` then `json.dump`) — crash window remains after W884 E2 guard | new |
| F3 | MEDIUM   | Raw (non-anonymized) text embedded even when anonymize_enabled=True — PII in embeddings.npy | new |
| F4 | MEDIUM   | W911 `reset_model_error` branch not merged — W884 E3 permanent error gate still open | W884 E3 |
| F5 | LOW      | `handle_semantic_search_reindex` blocks IPC thread for 100–500s on 10k-item history | new |

**Total new findings: 5. W884 E2: merged. W911 (E3): NOT merged.**
