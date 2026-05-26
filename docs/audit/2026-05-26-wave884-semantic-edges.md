# Wave 884 Audit: `backend/semantic_search.py` — Edge Cases

**Date:** 2026-05-26
**File:** `KrabEar/backend/semantic_search.py` (349 LOC)
**Prior audit:** W823 (`2026-05-26-wave823-search-audit.md`) — focused on concurrency races and O(N) scans.
**Scope (this audit):** Cold-start behavior, model load failure modes, embedding cache eviction, hybrid scoring.

---

## Summary

4 dimensions audited, 8 findings total: 3 medium-severity gaps and 5 low-severity observations.
No previously-unreported HIGH-severity issues (W823 captured all HIGH findings).

---

## 1. Cold-start behavior (no embeddings yet)

### E1. Cold-start `search()` returns `[]` silently — no fallback triggered (MEDIUM)

**Location:** `service.py` `_handle_semantic_search()` lines 691–696.

**Code path:**

```python
# service.py
results = self._semantic_searcher.search(query, top_k=top_k)
if not results and use_fallback:
    items = [...]
    results = keyword_fallback_search(query, items, top_k=top_k)
    return {"results": results, "mode": "keyword", "reason": "model_unavailable"}

return {"results": results, "mode": "semantic"}
```

**Problem:** On a completely fresh installation (no `embeddings.npy`, no `embeddings_index.json`),
the model loads successfully, the index is empty, and `search()` returns `[]` — which is falsy. The
IPC handler then correctly triggers the keyword fallback. However, the fallback `reason` field is set
to `"model_unavailable"`, which is incorrect: the model **is** available, the index is simply empty.

The caller (GUI history panel) uses `reason` to decide what UI copy to display. A cold-start user
sees "model unavailable" messaging when the real state is "index is building" — misleading UX.

**Recommendation:** Distinguish between empty-because-disabled, empty-because-error, and
empty-because-cold-start in `search()` return value or a separate `is_cold_start()` helper:

```python
# in SemanticSearcher.search()
if self._embeddings is None or len(self._index) == 0:
    return []  # already correct — but add a status accessor:

def is_cold_start(self) -> bool:
    """True if model loaded but index is empty (no items indexed yet)."""
    with self._index_lock:
        return self._model_loaded and len(self._index) == 0
```

Then in `_handle_semantic_search`:
```python
if not results and use_fallback:
    reason = "cold_start" if self._semantic_searcher.is_cold_start() else "model_unavailable"
    ...
    return {"results": results, "mode": "keyword", "reason": reason}
```

**Coverage gap:** No existing test asserts that the `reason` field is `"cold_start"` vs
`"model_unavailable"` on an empty-but-healthy index.

---

### E2. Cold-start `_load_from_disk` called with only one file present — silent index corruption (MEDIUM)

**Location:** `_load_from_disk()` lines 296–312.

**Code:**
```python
if self._embeddings_path.exists() and self._index_path.exists():
    embeddings = np.load(str(self._embeddings_path))
    with open(self._index_path, encoding="utf-8") as f:
        index = json.load(f)
    with self._index_lock:
        self._embeddings = embeddings
        self._index = index
```

**Problem (already noted in W823 S5 for saves; this is the complementary read gap):** The guard
`if both files exist` correctly skips when neither file exists (clean cold-start). However, there is
a window between the two `_save_locked()` writes where only one file has been updated (e.g. after a
crash mid-save). On the **next** startup, `_load_from_disk` finds both files present but inconsistent:
`embeddings.npy` has N rows and `embeddings_index.json` has M ≠ N entries.

The function loads both unconditionally without a shape validation:
```python
# No check: len(index) == embeddings.shape[0]
```

After loading, `index[i]` maps to `self._embeddings[i]`, but with N ≠ M, any access to a row index
beyond `min(N,M)` will raise `IndexError` inside `_cosine_similarity_batch` (which uses slice
broadcasting on the full matrix) or silently return wrong scores (if N > M, extra rows have no id;
if N < M, `index` has dangling ids pointing to non-existent rows).

In practice, `_cosine_similarity_batch` never accesses individual rows by id — it computes
`normalized @ q` across all N rows — but the result `scores` has N elements while `index` has M.
The loop `for i in top_indices: results.append({"id": index[i], ...})` will raise `IndexError` when
`i >= M` (if N > M).

**Recommendation:** Add a consistency guard in `_load_from_disk`:
```python
if len(index) != embeddings.shape[0]:
    logger.warning(
        "semantic_search: inconsistent on-disk state (%d ids, %d embedding rows) — "
        "discarding and starting from scratch",
        len(index), embeddings.shape[0],
    )
    return  # stay with empty in-memory state; will rebuild on next index_all
with self._index_lock:
    self._embeddings = embeddings
    self._index = index
```

**Coverage gap:** No test simulates a half-written disk state and verifies safe recovery.

---

## 2. Model load failure modes

### E3. Permanent error after transient failure — no retry mechanism (MEDIUM)

**Location:** `_get_model()` lines 241–266.

**Code:**
```python
with self._model_lock:
    if self._model_loaded:
        return self._model
    if self._model_error:
        return None   # permanent gate
    try:
        ...
        self._model = SentenceTransformer(self._model_name)
        self._model_loaded = True
        ...
    except Exception as exc:
        self._model_error = str(exc)
        return None
```

**Problem:** Once `_model_error` is set, every subsequent call to `_get_model()` returns `None`
immediately without retrying. This is intentional as a fast-path guard, but it conflates two
distinct failure modes:

1. **Permanent failures** (wrong model name, model not found on HuggingFace, incompatible hardware) —
   correctly handled as permanent.
2. **Transient failures** (HuggingFace download interrupted, temporary disk full, network timeout on
   first download) — incorrectly treated as permanent. The user must restart the entire backend to
   clear the error, even if the condition has resolved.

The `sentence_transformers_not_installed` error is correctly permanent. But a generic `Exception`
from `SentenceTransformer(self._model_name)` could be `ConnectionError`, `TimeoutError`, or
`OSError: [Errno 28] No space left on device` — all potentially transient.

**Recommendation:** Distinguish permanent vs transient errors and provide a reset path:

```python
_PERMANENT_ERRORS = {"sentence_transformers_not_installed"}

def reset_model_error(self) -> bool:
    """Clear a transient model load error to allow retry. Returns True if reset happened."""
    with self._model_lock:
        if self._model_error and self._model_error not in _PERMANENT_ERRORS:
            self._model_error = None
            return True
    return False
```

Expose `semantic_search_reset` as an IPC method so the GUI can offer a "Retry" button without
requiring a full backend restart.

**Coverage gap:** No test simulates a transient load failure followed by a successful retry attempt.

---

### E4. Model load failure propagated as empty `index_all` result with no error code (LOW)

**Location:** `index_all()` line 123.

**Code:**
```python
if model is None:
    return {"indexed": 0, "skipped": len(items), "errors": 0, "reason": self._model_error or "model_unavailable"}
```

**Problem:** When the model failed to load, `errors` is `0` and `skipped` equals `len(items)`. From
a caller's perspective, all items were processed and just happened to be skipped — which is
indistinguishable from a normal run where all items were already indexed. The caller must inspect
`reason` to detect the failure.

The IPC handler `_handle_semantic_search_reindex` in `service.py` passes the result directly to the
caller without raising or transforming it:
```python
result = self._semantic_searcher.index_all(items, force=force)
return result
```

A Swift UI that expects `{"indexed": N, "skipped": M, "errors": 0}` as a success indicator will
silently report 0 items indexed with no visible error.

**Recommendation:** Set `errors = len(items)` (not `skipped`) when the model is unavailable, since
the items were not processed due to a backend error, not because they were already indexed. Or add
an explicit `"ok": false` flag:
```python
return {
    "indexed": 0, "skipped": 0, "errors": len(items),
    "reason": self._model_error or "model_unavailable",
    "ok": False,
}
```

**Coverage gap:** No test verifies that `index_all` with a failed model produces a result that is
distinguishable from a normal "all-skipped" run.

---

## 3. Embedding cache eviction

### E5. No cache eviction policy — unbounded memory growth at large history (LOW)

**Location:** `SemanticSearcher` class, no eviction code present.

**Observation:** `self._embeddings` is a NumPy matrix that grows monotonically: items are added via
`index_item` / `index_all` and removed via `remove_item`, but there is no maximum size cap. For a
user with 50k history items at 768 dims (multilingual-e5-base), the matrix consumes:

```
50,000 × 768 × 4 bytes = 153.6 MB in RAM
```

This is always resident as long as the backend is running, since there is no LRU eviction or
configurable `max_index_size`. It also means that `_load_from_disk` at startup unconditionally loads
the full matrix into RAM regardless of available memory.

**Context:** W823 S3 noted the copy-on-search issue (30–300 MB per search call). This finding is
complementary: the base RSS cost exists even before any search is issued.

**Recommendation:** Add a configurable `max_index_size: int = 0` (0 = unlimited) parameter to
`SemanticSearcher.__init__`. When set, use an LRU policy: the oldest-indexed items are evicted
when the matrix exceeds `max_index_size` rows. Alternatively, document the expected RAM ceiling at
the configured `max_history` setting (default 10,000 items = ~30 MB) so operators can make an
informed decision.

**No test** exists that validates memory footprint or eviction at large index sizes.

---

### E6. `force=True` in `index_all` drops in-memory index before verifying batch succeeds (LOW)

**Location:** `index_all()` lines 125–128.

**Code:**
```python
if force:
    with self._index_lock:
        self._embeddings = None
        self._index = []
```

**Problem:** The force-rebuild path clears the entire in-memory index **before** attempting to
re-encode the batch. If `_encode_batch` then raises (e.g., GPU OOM, model timeout, corrupted audio),
the except clause on lines 169–171 catches it but leaves `self._embeddings = None` and
`self._index = []` — the entire index is wiped. The next `search()` call returns `[]` for all
queries, falling back to keyword search silently.

The save is only called on success (`self._save_locked()` inside the success branch, line 167), so
the on-disk state from before the forced rebuild remains intact. On next startup, `_load_from_disk`
restores the old data — but the in-process state is empty until then.

**Recommendation:** Stage the new embeddings in a local variable; only replace the in-memory index
if encoding fully succeeds:

```python
if force:
    new_embeddings = None
    new_index = []
else:
    with self._index_lock:
        new_embeddings = self._embeddings
        new_index = list(self._index)

# ... encode batch into local variables ...

with self._index_lock:
    self._embeddings = new_embeddings
    self._index = new_index
    self._save_locked()
```

**Coverage gap:** No test simulates an exception during batch encoding with `force=True` and verifies
that the in-memory index remains intact.

---

## 4. Hybrid scoring

### E7. No hybrid scoring implemented — hard switchover between semantic and keyword (LOW)

**Observation:** The IPC handler `_handle_semantic_search` implements a binary fallback strategy:

```
if semantic results exist → return semantic results (mode="semantic")
else → return keyword results (mode="keyword")
```

There is no score blending. A user who has 5,000 items indexed and searches for a phrase that
appears verbatim in one item but is also semantically similar to several others will see:
- If the exact-match item is among the top-K semantic results: good result.
- If it is not (semantic score for exact text may be diluted by prefix normalization): no exact-match
  result, only semantic approximations.

The `keyword_fallback_search` function supports a simple overlap score. A hybrid ranker would:
1. Get top-K semantic results with scores.
2. Get top-K keyword results with scores.
3. Combine via `hybrid_score = alpha * semantic_score + (1 - alpha) * keyword_score` (RRF is also
   an option).
4. Re-rank and return top-K.

**Current state:** The architecture does not support this. `SemanticSearcher.search()` takes a query
string and returns ids+scores; `keyword_fallback_search` takes a query string + full items list and
returns ids+scores. Both exist but there is no merge step.

**Recommendation:** Add an optional `hybrid_alpha: float = 0.0` parameter to
`_handle_semantic_search`. When `hybrid_alpha > 0` and the semantic model is loaded:

```python
semantic_results = self._semantic_searcher.search(query, top_k=top_k * 2)
keyword_results = keyword_fallback_search(query, items, top_k=top_k * 2)
results = _merge_hybrid(semantic_results, keyword_results, alpha=hybrid_alpha)[:top_k]
return {"results": results, "mode": "hybrid"}
```

Default `hybrid_alpha=0.0` preserves current behavior. This is a new feature (not a bug), so
priority is lower than the correctness issues above.

**Coverage gap:** No test exists for hybrid scoring because the feature is not yet implemented.

---

### E8. `keyword_fallback_search` uses substring matching, not word-boundary matching (LOW)

**Location:** `keyword_fallback_search()` line 343.

**Code:**
```python
matched = sum(1 for w in query_words if w in text)
```

**Problem:** `w in text` is substring matching, not word-boundary matching. A query word `"кот"`
would match `"скотч"`, `"коточке"`, `"ткоте"`, etc., because the string contains the substring
`"кот"`. For Russian and Spanish (both morphologically rich languages with prefixes and infixes),
this produces false positives.

Example:
- Query: `"кот"` (cat)
- Item text: `"Скотланд ярд — лондонская полиция"` (Scotland Yard)
- Matched: 1 / 1 = score 1.0 (false positive)

When hybrid scoring is added, this false positive will pollute the merged results with a high
keyword score for irrelevant items.

**Recommendation:** Use `re.search(r'\b' + re.escape(w) + r'\b', text)` or split text into words
and use set intersection:

```python
text_words = set(re.split(r'\s+', text))
matched = sum(1 for w in query_words if w in text_words)
```

The set-split approach is O(N) where N is word count, fast enough for the fallback path.

**Coverage gap:** No test verifies word-boundary behavior for Russian words with substrings.

---

## Summary Table

| ID  | Category        | Severity | Short description                                                          |
|-----|-----------------|----------|----------------------------------------------------------------------------|
| E1  | Cold-start UX   | MEDIUM   | Empty-index search returns `reason="model_unavailable"` instead of `"cold_start"` |
| E2  | Cold-start      | MEDIUM   | `_load_from_disk` missing size-consistency check → `IndexError` after partial crash save |
| E3  | Model load      | MEDIUM   | Transient load errors permanently block model use; no retry/reset path     |
| E4  | Model load      | LOW      | `index_all` model-unavailable result indistinguishable from normal skip    |
| E5  | Cache eviction  | LOW      | No max_index_size cap — unbounded RSS growth for large histories            |
| E6  | Cache eviction  | LOW      | `force=True` clears in-memory index before batch success — wipes on error  |
| E7  | Hybrid scoring  | LOW      | No hybrid scoring; binary semantic-or-keyword switchover only              |
| E8  | Hybrid scoring  | LOW      | `keyword_fallback_search` uses substring match, not word-boundary          |

**Total:** 8 findings — 0 HIGH (covered by W823), 3 MEDIUM, 5 LOW.

---

## Recommended Fix Priority

1. **E2 (MEDIUM, 5 lines):** Add `len(index) != embeddings.shape[0]` guard in `_load_from_disk`.
   Prevents silent `IndexError` crash on next startup after a mid-save process kill. No API change.

2. **E3 (MEDIUM, ~15 lines):** Add `reset_model_error()` method + `semantic_search_reset` IPC
   handler. Allows recovery from transient load errors without backend restart.

3. **E1 (MEDIUM, ~10 lines):** Add `is_cold_start()` accessor; update `_handle_semantic_search`
   to set `reason="cold_start"` when model is loaded but index is empty.

4. **E6 (LOW, ~20 lines):** Stage new embeddings locally in `force=True` path before committing
   to in-memory state.

5. **E8 (LOW, 2 lines):** Replace `w in text` with word-set intersection in
   `keyword_fallback_search`. No API change.

6. **E4 (LOW, 2 lines):** Set `errors = len(items)` (not `skipped`) when model is unavailable in
   `index_all`.

7. **E7 (LOW, new feature):** Hybrid scoring via `hybrid_alpha` param — implement after E1–E6 are
   resolved.

8. **E5 (LOW, new feature):** Add `max_index_size` cap with LRU eviction — lower priority since
   default history limit (10k items) keeps RSS at ~30 MB, within acceptable range.
