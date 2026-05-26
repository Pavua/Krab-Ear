# Wave 823 Audit: `backend/semantic_search.py` + `core/search_index.py`

**Date:** 2026-05-26  
**Files:** `KrabEar/backend/semantic_search.py` (349 LOC), `KrabEar/core/search_index.py` (222 LOC)  
**Scope:** Performance at large history (>10k items), concurrency safety, rebuild trigger logic, correctness.

---

## Summary

Both files are functional and include threading primitives. Nine issues were found: two high-severity
bugs (data races), two medium-severity performance problems, three low-severity correctness gaps, and
two minor style/documentation notes.

---

## `backend/semantic_search.py`

### S1. TOCTOU race in `index_item` — correctness bug (HIGH)

**Location:** `index_item()` lines 96–105

**Code:**
```python
with self._index_lock:
    if item_id in self._index:
        row = self._index.index(item_id)
        self._embeddings[row] = embedding
    else:
        ...
```

**Problem:** The check `if item_id in self._index` and the subsequent `self._index.index(item_id)` are
performed inside `_index_lock`, which is correct. However, `self._encode(model, text)` on line 94
(which calls `model.encode(...)`) is called **outside** the lock. `sentence-transformers` models are
not documented as thread-safe at the Python layer; concurrent calls from `index_item` and a
simultaneous `search()` or `index_all()` can both call `model.encode` in parallel threads.

`search()` also calls `self._encode(model, query)` on line 198 **outside** `_index_lock` and uses
a separate `_model_lock` only during model load. There is no serialization between the `model.encode`
call paths in `search` and `index_item`. If the underlying transformer model is not internally
thread-safe (or if the `normalize_embeddings` NumPy post-processing is not), this is a latent race.

**Impact:** Potential silent corruption of embedding vectors or crashes in high-concurrency scenarios
(e.g., bulk IPC `index_all` running while a `search` call arrives).

**Recommendation:** Either add a dedicated `_encode_lock` (RLock) that serializes all `model.encode`
calls, or document/test that the specific `sentence-transformers` version used is thread-safe. Given
that `mlx_lock` is the established pattern for serializing ML inference in this codebase, the same
pattern should apply here.

---

### S2. O(N) linear scan inside `_index_lock` at every `index_item` call (HIGH)

**Location:** `index_item()` line 98, `index_all()` lines 158–159

**Code:**
```python
row = self._index.index(item_id)   # O(N) list scan
```

**Problem:** `self._index` is a plain Python `list[str]`. Both `index_item` and `index_all` call
`self._index.index(item_id)` — a linear scan — every time an existing item is updated. At 10k items
this is 10k comparisons per update, and at 100k items 100k comparisons. Because this runs inside
`_index_lock`, it blocks all concurrent reads for the duration.

The `in` check on line 97 is also O(N): `if item_id in self._index` performs a list membership test.
This is called for every item during a full `index_all` batch, making `index_all` O(N²) in the worst
case.

**Impact:** At 10k history items, a full reindex (`force=True`) performs 10k×2 = ~20k O(N) scans,
totalling O(N²) = ~100M comparisons. Benchmark estimate: ~5–15 s blocking time on M4 Max for 10k
items. Degrades the IPC thread holding `_index_lock` for that entire period.

**Recommendation:** Replace `self._index: list[str]` with a pair: `self._index: list[str]` (for
positional row lookup) and `self._id_to_row: dict[str, int]` (for O(1) membership and row lookup).
Keep the list for ordered indexing; update the dict on every insert/delete/update.

```python
# O(1) membership check and row lookup
if item_id in self._id_to_row:
    row = self._id_to_row[item_id]
    self._embeddings[row] = embedding
else:
    self._id_to_row[item_id] = len(self._index)
    self._index.append(item_id)
    ...
```

`remove_item()` also does an O(N) `self._index.index(item_id)` (line 225); this too would be fixed
by the dict approach.

---

### S3. Whole matrix copied into RAM on every `search` call (MEDIUM)

**Location:** `search()` line 195

**Code:**
```python
with self._index_lock:
    embeddings = self._embeddings.copy()
    index = list(self._index)
```

**Problem:** `self._embeddings.copy()` creates a full deep copy of the NumPy matrix to avoid holding
the lock during `model.encode + cosine similarity`. This is a reasonable concurrency trade-off, but
at 10k items × 768 dims (multilingual-e5-base), the matrix is 10k × 768 × 4 bytes = ~30 MB. At 100k
items it is ~300 MB. Each `search` call allocates this full copy, stressing the allocator and
increasing GC pressure.

**Impact:** Potential memory spikes visible as backend RSS growth during rapid repeated searches.
Particularly relevant during history panel "as-you-type" search scenarios.

**Recommendation:** Use `numpy.array(self._embeddings, copy=False)` with a `threading.RLock` that
allows concurrent readers (`concurrent.futures.thread.RLock` or a readers-writer lock), or cap the
index at a configurable size with an LRU eviction policy. Alternatively, a read-view slice
(`self._embeddings[:]`) is still a copy but avoids the method-call overhead.

A simpler near-term fix: only copy if the matrix is smaller than a threshold (e.g., 50k rows);
above that threshold, hold the lock during the dot-product (which is fast in NumPy and does not call
back into Python).

---

### S4. `_encode` uses `"query: "` prefix for both query and single-item indexing (LOW)

**Location:** `_encode()` line 273, `_encode_batch()` line 279

**Code:**
```python
# _encode (called for query AND single-item index_item):
prefix = "query: "

# _encode_batch (called for bulk index_all):
prefix = "passage: "
```

**Problem:** `multilingual-e5` requires asymmetric prefixes: `"query: "` for search queries and
`"passage: "` for documents. `_encode_batch` correctly uses `"passage: "`, but `_encode` (used for
both `index_item` single-document indexing and `search` query encoding) always uses `"query: "`.

This means single items indexed via `index_item` are encoded with `"query: "` while batch-indexed
items are encoded with `"passage: "`. Cosine similarity between a `"query: "`-prefixed query and a
`"query: "`-prefixed document (from `index_item`) is not symmetric in the way the model was trained
to produce; it degrades recall.

**Impact:** Medium precision/recall degradation for items indexed individually (e.g., new recordings
indexed in real-time via `index_item`) vs. items indexed in batch.

**Recommendation:** Add a `use_query_prefix: bool` parameter to `_encode`, defaulting to `False`
(passage prefix), and pass `use_query_prefix=True` only from `search()`.

---

### S5. `_save_locked` overwrites `.npy` non-atomically (LOW)

**Location:** `_save_locked()` lines 315–323

**Code:**
```python
np.save(str(self._embeddings_path), self._embeddings)
with open(self._index_path, "w", ...) as f:
    json.dump(self._index, f, ...)
```

**Problem:** `np.save` and `open(..., "w")` write directly to the target file paths. If the process
is killed between the two writes, the `.npy` and `.json` files become inconsistent (matrix updated,
index not, or vice versa). On next startup, `_load_from_disk` will load a matrix of size N but an
index of size M where N ≠ M, causing silent index corruption.

**Recommendation:** Write to `.npy.tmp` / `.json.tmp` first, then `os.replace()` both atomically
(or as close as the filesystem allows). At minimum, validate that `len(embeddings) == len(index)`
inside `_load_from_disk` and discard both if they mismatch.

---

### S6. `_load_from_disk` acquires `_index_lock` while `_model_lock` is held (LOW)

**Location:** `_get_model()` lines 252–254, `_load_from_disk()` lines 303–306

**Code:**
```python
# Inside _get_model() — _model_lock is already held:
self._model_loaded = True
self._load_from_disk()   # <-- calls with _model_lock held

# Inside _load_from_disk():
with self._index_lock:   # <-- acquires second lock
    self._embeddings = embeddings
    self._index = index
```

**Problem:** `_get_model` holds `_model_lock` and calls `_load_from_disk`, which acquires
`_index_lock`. Any other path that acquires `_index_lock` first and then calls `_get_model`
(or waits for it) would deadlock. Currently `search()` acquires `_index_lock` in the read section,
then exits, then calls `_get_model` — so the lock order is not reversed in the current code. However,
this is a fragile implicit contract; a future refactor that holds `_index_lock` during the model
check would introduce a classic AB/BA deadlock.

**Recommendation:** Document the lock order explicitly: `_model_lock` always before `_index_lock`.
Add a comment in `_get_model` and `_load_from_disk` stating this invariant.

---

## `core/search_index.py`

### I1. Signature-based rebuild-skip misses `source_text` and `translated_text` changes (HIGH)

**Location:** `_compute_signature()` lines 200–207

**Code:**
```python
for item in items:
    item_id = item.get("id", "")
    text = (item.get("text") or "") + (item.get("translated_text") or "")
    h.update(f"{item_id}:{text}".encode("utf-8", errors="replace"))
```

**Problem:** `_item_text()` (lines 189–197) includes three fields when building the actual index:
`text`, `source_text`, and `translated_text`. But `_compute_signature()` only hashes `text` and
`translated_text` — it omits `source_text`. If a history item's `source_text` field changes (e.g.,
after a LLM rewrite that updates only the `source_text`), the signature remains unchanged and
`build_index` returns early without rebuilding, leaving stale tokens for `source_text` in the index.

**Impact:** Stale search results — items may be returned or missed based on the old `source_text`
after it is updated. Correctness bug that is hard to detect because the index appears to work
normally until an edge case triggers it.

**Recommendation:** Include `source_text` in the hash:
```python
text = ((item.get("text") or "") +
        (item.get("source_text") or "") +
        (item.get("translated_text") or ""))
```

---

### I2. AND-only search returns empty for any multi-word query term absent from index (MEDIUM)

**Location:** `search()` lines 140–143

**Code:**
```python
for i, token in enumerate(query_tokens):
    ids = self._index.get(token)
    if ids is None:
        return []   # early-exit on first missing token
```

**Problem:** The search implements strict AND semantics: if any single token is absent from the
index, the method returns immediately with an empty list. This is documented behaviour but creates
a poor user experience for long or multi-word Russian queries where a single inflected form is not
in the index. A user searching for "запись совещания" gets zero results if either stem is missing,
even if many items contain the other term.

This interacts poorly with the stemmer: `_stem_ru` is a simple suffix-stripper without a stemmer
dictionary, so uncommon inflections may produce stems not present in any indexed item.

**Impact:** False-zero search results for legitimate queries; users may think history is empty.

**Recommendation (medium priority):** Consider an OR fallback when AND yields zero results. At
minimum, expose a `mode: str = "and"` parameter to `search()` so callers can opt into OR semantics
for broader recall. The `keyword_fallback_search` function in `semantic_search.py` already implements
OR logic; consolidating or cross-linking these would reduce duplication.

---

### I3. `_compute_signature` is O(N × text_len) on every `build_index` call (MEDIUM)

**Location:** `_compute_signature()` lines 200–207, called from `build_index()` line 101

**Code:**
```python
def build_index(self, items: list[dict]) -> None:
    new_sig = self._compute_signature(items)
    if new_sig == self._signature:
        return
```

**Problem:** `_compute_signature` iterates over all `items` and hashes their full text on every call
to `build_index`. For 10k items with average 200-character transcripts, this is ~2 MB of text hashed
per call. `build_index` is likely called on every IPC `search_history` request (or at regular
intervals). The MD5 computation itself is fast, but the string concatenation
`f"{item_id}:{text}".encode(...)` inside the loop allocates a new string per item, totalling ~2 MB
of allocations per call.

**Impact:** Minor but measurable GC pressure on high-frequency search paths. At 100k items
(projected long-term usage), signature computation alone is ~20 MB of string allocation per search.

**Recommendation:** Cache the signature by tracking the last-seen history length and last-seen
item modification timestamp (if available in `HistoryItem`), avoiding the full MD5 scan unless the
count or timestamp changed. Alternatively, maintain an incremental hash that is updated only when
items are added or removed.

---

### I4. `score` field is constant for all AND results (LOW)

**Location:** `search()` lines 158–165

**Code:**
```python
results.append(
    SearchResult(
        item_id=item_id,
        score=len(query_tokens),   # same for every result
        ...
    )
)
results.sort(key=lambda r: (-r.score, r.item_id))
```

**Problem:** Every result that passes the AND filter gets `score = len(query_tokens)` — the same
value for all results in a given query. The final sort is therefore always by `item_id` (alphabetical
/ lexicographic), not by any relevance metric. The `score` field and the sort give a false impression
of relevance ranking to callers.

**Impact:** Low — functional issue only. The index is correct but the sort order is arbitrary.
Callers that display results in order may confuse users by not surfacing the most-recent or
most-relevant items first.

**Recommendation:** Score by term frequency (TF) — count how many times each matched token appears
in the item's text — or at minimum sort by item recency (reverse chronological) when scores are tied.

---

## Cross-cutting: `SearchIndex` has no thread-safety at all (HIGH, shared with S2)

**Files:** `core/search_index.py` (entire class)

**Problem:** `SearchIndex` has no locks. `build_index` writes to `self._index`, `self._texts`, and
`self._signature` without any synchronization. `search` reads all three. If `build_index` is called
on a background thread (e.g., triggered by a `history_updated` event) while `search` is called from
the IPC handler thread, the following races are possible:

- `search` reads `self._index` dict mid-rebuild (when it has been partially cleared and not yet
  repopulated) → `get()` returns `None` for a valid token → premature `return []`.
- `search` reads `self._texts` for snippet generation after `build_index` cleared it but before
  repopulation → `get(item_id, "")` returns empty string → empty snippets for all results.

The Python GIL reduces (but does not eliminate) the risk: dict operations are individually atomic
under the GIL, but `build_index` performs a multi-step clear-and-rebuild sequence that is not
atomic as a whole.

**Recommendation:** Add a `threading.RLock` to `SearchIndex.__init__` and acquire it in both
`build_index` and `search`. The lock contention is low because `build_index` is infrequent and
`search` completes quickly (pure Python dict intersection + snippet generation).

---

## Summary Table

| ID  | File                  | Severity | Category       | Short description                                          |
|-----|-----------------------|----------|----------------|------------------------------------------------------------|
| S1  | semantic_search.py    | HIGH     | Concurrency    | `model.encode` called outside any lock — potential race    |
| S2  | semantic_search.py    | HIGH     | Performance    | O(N) list scan inside `_index_lock` → O(N²) reindex       |
| S3  | semantic_search.py    | MEDIUM   | Memory         | Full matrix copy on every `search` call (30–300 MB)       |
| S4  | semantic_search.py    | LOW      | Correctness    | `"query: "` prefix used for `index_item` documents        |
| S5  | semantic_search.py    | LOW      | Correctness    | Non-atomic `.npy` + `.json` save → inconsistency on crash |
| S6  | semantic_search.py    | LOW      | Concurrency    | Implicit lock-order assumption (`_model_lock` > `_index_lock`) |
| I1  | search_index.py       | HIGH     | Correctness    | `_compute_signature` omits `source_text` → stale index    |
| I2  | search_index.py       | MEDIUM   | UX             | Strict AND returns empty on any missing token             |
| I3  | search_index.py       | MEDIUM   | Performance    | Full text hash on every `build_index` call (O(N×len))     |
| I4  | search_index.py       | LOW      | Correctness    | All AND results get identical `score`; sort is by item_id |
| CX  | search_index.py       | HIGH     | Concurrency    | No locks at all; `build_index` vs `search` concurrent race |

**Total:** 11 findings — 3 HIGH, 3 MEDIUM, 4 LOW, 1 note.

---

## Recommended Fix Priority

1. **CX + I1 (HIGH, low effort):** Add `threading.RLock` to `SearchIndex`; fix `_compute_signature`
   to include `source_text`. Both are 5–10 line changes with no API surface impact.

2. **S2 (HIGH, medium effort):** Add `_id_to_row: dict[str, int]` alongside `_index: list[str]`
   in `SemanticSearcher`. Eliminates O(N²) reindex; no API change needed.

3. **S1 (HIGH, medium effort):** Add `_encode_lock` or document thread-safety contract of
   `sentence-transformers` for the version pinned in requirements. Low risk of false-positive
   concurrency issues in practice (only one IPC thread normally calls search), but worth hardening
   for future REST server parallelism.

4. **S4 (LOW, trivial):** Pass `is_query=True` from `search()`; use `"passage: "` in `_encode`
   otherwise. Improves recall for real-time indexed items.

5. **S5 (LOW, medium effort):** Write to temp files + `os.replace`; add consistency check in
   `_load_from_disk`.
