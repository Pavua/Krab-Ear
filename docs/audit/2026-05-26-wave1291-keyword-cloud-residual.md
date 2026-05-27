# Audit: KeywordCloudGenerator Residual (W1291)

**Date:** 2026-05-26
**File:** `KrabEar/backend/keyword_cloud.py`
**Auditor:** W1291 sub-agent (re-audit after W1093/W1084)
**Scope:** NEW residual issues only — W1084/W1093 findings not duplicated

---

## Merge-State Verification

| Wave | Branch | PR | Status |
|------|--------|----|--------|
| W1084 | `audit/keyword-cloud-W1084` | #998 | **OPEN — not merged** |
| W1093 | `fix-keyword-cloud-W1093` | #1005 | **OPEN — not merged** |
| W1095 | `audit/stop-words-W1095` | #1009 | **OPEN — not merged** |
| W1105 | `term-extractor-stopwords-unify-W1105` | #1022 | **OPEN — not merged** |
| W1106 | `stop-words-ru-pronouns-W1106` | #1015 | **OPEN — not merged** |

All five upstream fix/audit branches remain unmerged into `codex/krab-ear-v2`.  The current
production file at `KrabEar/backend/keyword_cloud.py` still carries the original code from
before any W1084/W1093 patches.

---

## Findings

### F1 — `_MERGE_PAIRS` is entirely dead code (MEDIUM)

**File:** `keyword_cloud.py:61–65`, `keyword_cloud.py:272–278`

```python
_MERGE_PAIRS: list[tuple[str, str]] = [
    ("ещё", "еще"),
    ("её", "ее"),
]
```

`_merge_similar` is called **after** stop-word filtering in `_collect_words` (line 264).
Both entries in `_MERGE_PAIRS` — `"ещё"/"еще"` and `"её"/"ее"` — are already present in
`stop_words.py`'s `_RU` frozenset and therefore never reach `_merge_similar`.  Verified:

```python
>>> StopWords.get_stop_words("ru")
{'ещё', 'еще', ...}  # both forms present
>>> 'её' in StopWords.get_stop_words("ru"), 'ее' in StopWords.get_stop_words("ru")
(True, True)
```

As a result the ё/е normalisation is silently a no-op for every production call.  More
importantly, real content words that have ё/е orthographic variants — e.g. `"жёсткий" /
"жесткий"`, `"чёрный" / "черный"` — are **not** in `_MERGE_PAIRS` and will be counted as
separate keywords, splitting their frequency and lowering both entries' weight in the cloud.

**Fix:** Move `_merge_similar` call to **before** stop-word filtering, or replace
`_MERGE_PAIRS` entries with common content-word ё/е alternation pairs that actually occur
after filtering.  At minimum remove the two dead entries and add a comment explaining the
pipeline order constraint.

---

### F2 — `max_words` has no upper bound — IPC-level DoS vector (MEDIUM)

**File:** `KrabEar/backend/service.py:2961`

```python
max_words = int(params.get("max_words", 100))
```

There is no upper cap.  A caller (or a bug in the Swift GUI) can send `max_words=1_000_000`.
With 10 000 history items × 300 words/item the corpus contains ~3 M tokens; after filtering
roughly 1 M distinct words may remain.  `Counter.most_common(1_000_000)` on a 1 M-entry
counter returns the entire counter (~2 M tuple items), which is then serialised into a JSON
response: ~150 bytes × 1 M entries ≈ **150 MB** per IPC response, causing either IPC socket
write timeout, client-side OOM, or main-thread stall while building the list.

The W1093 fix adds an early return for `max_words <= 0` but does **not** add an upper cap.

**Fix:** Clamp at the handler boundary before the `generate_cloud` call:

```python
max_words = min(max(0, int(params.get("max_words", 100))), 1000)
```

Cap of 1 000 words is well above any UI need and stops unbounded allocation.

---

### F3 — Fallback stop-word list is severely incomplete; silent bare `except` (LOW)

**File:** `keyword_cloud.py:25–49`

```python
try:
    from core.stop_words import StopWords
    _STOP_WORDS: frozenset = (
        StopWords.get_stop_words("ru") | ...
    )
except Exception:  # fallback если core.stop_words недоступен
    _STOP_WORDS: frozenset = frozenset({...})  # sparse inline list
```

Two separate problems:

1. **Bare `except Exception` swallows all import errors** — including `ImportError`,
   `SyntaxError`, broken `__init__.py`, permission errors.  The user gets degraded results
   with no log message because the `except` body has no `logger.warning`.

2. **The fallback list is severely under-complete.**  Comparing with `stop_words.py`:
   - EN: fallback has **35** words vs `stop_words.py`'s **163** (128 missing, e.g. `about`,
     `across`, `after`, `already`, `although`, `always`, `among`, `any`, `around`, `as`,
     `because`, `been`, `before`, …).
   - RU: fallback has ~50 words vs `stop_words.py`'s **~170** (120+ missing, including
     `меня`, `мне`, `тебя`, `нас`, `вам`, `него`, `ему`, `им` from W1095/W1106).

   When `core.stop_words` is unavailable — e.g., in a broken venv upgrade or during testing
   — keyword clouds will be flooded with common grammatical words that should have been
   filtered.

**Fix:**
- Add `logger.warning("core.stop_words import failed, using sparse fallback: %s", e)` inside
  the `except` block.
- Expand the fallback EN list to at least match the 50 most-common English stop words not
  already present; same for RU.  Or replace the inline fallback with a minimal but
  representative set defined at the top of the file.

---

### F4 — W1106 unmerged: 16 RU oblique pronoun forms produce keyword-cloud false positives (LOW)

**Upstream fix:** `stop-words-ru-pronouns-W1106` (PR #1015, OPEN)

W1095 finding F2 identified 16+ high-frequency Russian pronoun oblique forms absent from
`stop_words.py`'s `_RU`.  W1106 added them but was not merged.  On the current main branch
the forms `меня`, `мне`, `мной`, `тебя`, `тебе`, `тобой`, `нас`, `нам`, `нами`, `вас`,
`вам`, `вами`, `него`, `ему`, `им`, `ним` all pass through `keyword_cloud.py`'s stop-word
filter and appear as keywords in the cloud.

Verified on `codex/krab-ear-v2`:

```python
>>> 'им' in StopWords.get_stop_words("ru")
False
>>> 'ними' in StopWords.get_stop_words("ru")
False
```

In a typical Russian-language transcript these forms are among the most frequent tokens, so
their presence degrades cloud quality significantly (they rank near the top of the cloud,
displacing actual content words).

**Fix:** Merge PR #1015 (`stop-words-ru-pronouns-W1106`).

---

### F5 — `_merge_similar` rebuilds `merge_map` dict on every call (LOW)

**File:** `keyword_cloud.py:272–278`

```python
@staticmethod
def _merge_similar(words: list[str]) -> list[str]:
    merge_map: dict[str, str] = {}
    for canonical, variant in _MERGE_PAIRS:
        merge_map[variant] = canonical
    return [merge_map.get(w, w) for w in words]
```

`merge_map` is reconstructed from `_MERGE_PAIRS` on every call.  For large corpora (e.g.
10 000 items) `_collect_words` can produce lists of millions of tokens, and `_merge_similar`
iterates all of them while rebuilding the same 2-entry dict each time.  While the overhead
is small for the current 2-entry `_MERGE_PAIRS`, the pattern is fragile: as `_MERGE_PAIRS`
grows the per-call dict build will scale O(pairs × words) instead of the intended
O(pairs + words).

This is also linked to F1: because both current entries are dead, the bug is currently
harmless but will become relevant if the dead entries are replaced with real content-word
pairs.

**Fix:** Hoist `merge_map` to a module-level constant:

```python
_MERGE_MAP: dict[str, str] = {variant: canonical for canonical, variant in _MERGE_PAIRS}

@staticmethod
def _merge_similar(words: list[str]) -> list[str]:
    return [_MERGE_MAP.get(w, w) for w in words]
```

---

## Coverage Status

The existing `KrabEar/tests/test_keyword_cloud.py` has **50 tests** on the original
(unpatched) code.  The W1093 branch adds 4 new tests for F1/F3, but because W1093 is
unmerged, the current test file does **not** include tests for:

- `max_words <= 0` → empty list (W1093 F1, PR #1005 unmerged)
- Privacy-mode guard (W1093 F3, PR #1005 unmerged)
- `max_words` upper-bound cap (F2 this audit — no test anywhere)
- `_MERGE_PAIRS` dead-code scenario (F1 this audit — no test)
- Fallback stop-word warning logging (F3 this audit — no test)

---

## Finding Summary

| ID | Severity | Description | Interaction |
|----|----------|-------------|-------------|
| F1 | MEDIUM | `_MERGE_PAIRS` entirely dead — both entries are stop words filtered before `_merge_similar` | New |
| F2 | MEDIUM | `max_words` no upper bound at IPC boundary — DoS/OOM via huge value | New (W1093 partial fix misses this) |
| F3 | LOW | Bare `except Exception` swallows import error silently; fallback stop-word list 128+ words short of `stop_words.py` | New |
| F4 | LOW | W1106 (oblique RU pronouns, PR #1015) unmerged — 16 forms still leak into cloud | W1095/W1106 carry-over |
| F5 | LOW | `_merge_similar` rebuilds `merge_map` dict on every call (O(pairs) per-call overhead) | New |
