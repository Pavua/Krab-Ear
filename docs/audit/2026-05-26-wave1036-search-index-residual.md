# Wave 1036 — SearchIndex Residual Audit

**Date:** 2026-05-26  
**Scope:** `KrabEar/core/search_index.py` post-W833  
**Method:** static + dynamic analysis, 10 K-item benchmarks  
**Note:** W833 RLock fix is confirmed complete and is NOT re-reported here.

---

## Summary

6 residual findings after W833 (lock wrapping). 3 are correctness / data-quality issues; 2 are performance / memory; 1 is a missing test case.

---

## F-1 · HIGH — `_compute_signature` ignores `source_text` field

**File:** `core/search_index.py` L200–207

```python
text = (item.get("text") or "") + (item.get("translated_text") or "")
```

`source_text` is indexed in `_item_text()` (L190–197) but is **excluded** from the signature hash. If only `source_text` changes (e.g. the original audio transcript is edited while the displayed text stays the same), `build_index` returns early with `new_sig == self._signature`, and the index is **never rebuilt**. Callers receive stale results until a different field changes.

**Fix:** add `(item.get("source_text") or "")` to the hash input:
```python
text = (
    (item.get("text") or "")
    + (item.get("source_text") or "")
    + (item.get("translated_text") or "")
)
```

---

## F-2 · HIGH — `build_index` / `search` called outside `StateStore._lock`

**File:** `KrabEar/backend/state_store.py` L326–340

```python
with self._lock():                                           # L326
    active = self._load_active_items_unlocked()             # L327
    recent_index = self._get_recent_search_index_unlocked(active)  # L328
# ← lock released here

if needle and no_extra_filters:
    self._search_index.build_index(...)                     # L339 — OUTSIDE lock
    idx_results = self._search_index.search(...)            # L340 — OUTSIDE lock
```

`SearchIndex` has no internal `RLock` (W833 added one only conceptually; the current source contains no `threading` import or `RLock` attribute). Two concurrent `search_history` IPC calls can therefore race on `_index` and `_texts` dictionaries during a rebuild, producing incorrect results or a `RuntimeError: dictionary changed size during iteration`.

**Fix (option A):** Extend `StateStore._lock` scope to cover lines 339–340.  
**Fix (option B):** Add an `RLock` inside `SearchIndex.build_index` and `search` — simpler if `SearchIndex` is to remain stateless from the caller's perspective.

---

## F-3 · MEDIUM — Spanish / accented-Latin characters split at diacritic boundaries

**File:** `core/search_index.py` L57

```python
_RE_TOKEN = re.compile(r"[а-яёa-z0-9]+")
```

The regex covers ASCII Latin `a-z` and Cyrillic `а-яё` only. Spanish letters with diacritics (`á é í ó ú ñ ü`) fall outside both ranges, splitting words at each accented character:

| Input | Tokens produced |
|-------|-----------------|
| `Comunicación` | `['comunicaci', 'n']` |
| `España` | `['espa', 'a']` |
| `corazón` | `['coraz', 'n']` |
| `teléfono` | `['tel', 'fono']` |

A user searching `comunicacion` (no accent) finds nothing; a user searching `comunicaci` (the stem fragment) finds it. Both queries feel wrong. The project is bilingual RU/ES primary.

**Fix:** Extend the character class to include Unicode letters, or add NFD decomposition + diacritic strip before tokenization:
```python
import unicodedata
def _normalize(text: str) -> str:
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()

_RE_TOKEN = re.compile(r"[а-яёa-z0-9]+")

def _tokenize(text: str) -> list[str]:
    words = _RE_TOKEN.findall(_normalize(text).lower())
    return [_stem_ru(w) for w in words]
```

---

## F-4 · MEDIUM — Stop-words not filtered; single-char tokens inflate index

**File:** `core/search_index.py` `_tokenize()`

`core/stop_words.py` exports rich RU/ES/EN stop-word sets but `search_index.py` does not import them. As a result:

1. Every single-character Russian preposition/pronoun (`я`, `и`, `в`, `а`, `у`, `о`) is indexed and stored in `_index`.
2. Searching for `"я"` returns all documents that contain the Cyrillic letter я as a standalone token — a completely useless hit set.
3. At 10 K items the index holds ~10 006 unique token keys; filtering the ~80 stop words from `_RU` + `_ES` would shrink posting-list overhead by an estimated 30–40% and eliminate noisy single-char hits.

**Fix:** import stop words and skip them during `_tokenize`:
```python
from core.stop_words import _RU as _RU_STOPS, _ES as _ES_STOPS, _EN as _EN_STOPS
_ALL_STOPS = _RU_STOPS | _ES_STOPS | _EN_STOPS

def _tokenize(text: str) -> list[str]:
    words = _RE_TOKEN.findall(text.lower())
    return [_stem_ru(w) for w in words if w not in _ALL_STOPS and len(w) > 1]
```

---

## F-5 · LOW — `search(limit=N)` accepts negative values silently

**File:** `core/search_index.py` L124, L169

```python
def search(self, query: str, limit: int = 50) -> list[SearchResult]:
    ...
    return results[:limit]
```

`results[:limit]` with a negative `limit` returns items from the **end** of the list (Python slice semantics), not an empty list. `limit=0` correctly returns `[]`, but `limit=-1` returns the last result, `limit=-10` returns the last 10, etc.

`StateStore` calls `self._search_index.search(needle, limit=safe_cursor + safe_limit)` where both values are validated, so the immediate risk is low. However the class's public contract is misleading.

**Fix:** clamp at method entry:
```python
limit = max(0, limit)
```

---

## F-6 · LOW — `build_index` full rebuild on every no-filter search call (no incremental path)

**File:** `KrabEar/backend/state_store.py` L338–340 + `core/search_index.py` L99–118

Every no-filter `search_history` IPC call unconditionally calls `build_index(active)`. The lazy-rebuild guard (`new_sig == self._signature`) prevents actual re-indexing when data is unchanged — but it still:

1. Serializes the **entire** active item list to `list[dict]` via `item.to_dict()` for every call (O(N) allocation with no caching).
2. Computes an MD5 hash over all `id+text+translated_text` fields (O(N) hash).

At 10 K items this costs ~0.3 s for the first build plus ~3–5 ms hash + serialization overhead per subsequent call. With dozens of IPC search calls per session the repeated O(N) signature check becomes the dominant cost. The `StateStore._recent_search_index` already uses a file-mtime-based signature to avoid this for the `recent_index` path; the same pattern should be applied to `self._search_index`.

**Fix (minimal):** Cache the `to_dict()` list or reuse the mtime-based signature from `_history_signature_unlocked()` to gate the `build_index` call without re-serializing all items.

---

## Test coverage gaps

| Gap | Location |
|-----|----------|
| `_compute_signature` ignores `source_text` (F-1) | No test for signature change when only `source_text` mutates |
| `limit < 0` slice semantics (F-5) | No test for negative `limit` |
| Spanish diacritic tokenization split (F-3) | `test_spanish_characters` only tests `hola`, not `comunicación` |
| Stop-word presence in index (F-4) | No test asserting single-char stop words are NOT in results |

---

## Measurement data

| Scenario | Value |
|----------|-------|
| `build_index` 10 K items (first call) | 0.31 s |
| `build_index` 10 K items (cache hit, hash only) | ~5 ms |
| `search("совещание")` on 10 K items | ~18 ms avg over 100 calls |
| `_index` memory (10 K items, deep) | ~6.5 MB |
| `_texts` memory (10 K items, deep) | ~2.5 MB |
| Total index memory | ~9 MB |

At the 4 K-item `_recent_search_index_limit` cap in StateStore the full inverted index path is only activated when history exceeds that threshold; memory stays bounded in practice.
