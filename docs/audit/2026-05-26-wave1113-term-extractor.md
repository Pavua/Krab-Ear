# Audit: TermExtractor (W1113)

**File:** `KrabEar/core/term_extractor.py` (310 lines)  
**Date:** 2026-05-26  
**Auditor:** W1113  
**Scope:** Extraction algorithm quality, n-gram support, MIN_TERM_LEN bound, output size cap, privacy_mode interaction, wire status, test coverage, Unicode boundary handling, idempotency. Stop-words duplication excluded (W1105).

---

## Wire Status

Well-wired. Called from:
- `backend/text_scoring_service.py` → `handle_extract_terms` IPC method (via `ipc_dispatch.py`)
- `core/auto_glossary.py` → `extract_terms()` per history item
- `backend/smart_vocabulary.py` → `extract_from_history()` for vocabulary suggestions
- `core/context_memory.py` imports `_ALL_STOP_WORDS` (internal reuse)

---

## Findings

### F1 HIGH — AttributeError crash on every `extract_terms` IPC call

**Location:** `backend/text_scoring_service.py:99–108`

`handle_extract_terms` accesses `t.score`, `t.language`, `t.category` on each `ExtractedTerm` object, but `ExtractedTerm` only has `term`, `frequency`, `is_proper_noun`, `context`, `confidence`. These three attributes do not exist.

```python
# text_scoring_service.py:99–107
return {
    "terms": [
        {
            "term": t.term,
            "score": t.score,       # AttributeError — field does not exist
            "frequency": t.frequency,
            "language": t.language, # AttributeError — field does not exist
            "category": t.category, # AttributeError — field does not exist
        }
        for t in terms
    ]
}
```

The `extract_terms` IPC method has been broken since `TextScoringService` was extracted. Every call raises `AttributeError` and returns an error response. `auto_glossary.py` and `smart_vocabulary.py` call `extract_terms()` directly (not via IPC) and are unaffected.

**Fix:** Either add `score`, `language`, `category` fields to `ExtractedTerm` (mapping `confidence → score`, deriving `language` from script heuristic, `category` from `is_proper_noun`), or update `handle_extract_terms` to use the existing fields.

---

### F2 HIGH — `language` parameter is a no-op

**Location:** `core/term_extractor.py:136–241`

`extract_terms(text, language="ru")` documents that the `language` parameter "влияет на набор стоп-слов и порог уверенности" (affects stop-word set and confidence threshold). In practice, `language` is accepted but never read inside the method — `_ALL_STOP_WORDS` (union of all three language sets) is always used, and confidence thresholds are hardcoded without any language-dependent adjustment.

`auto_glossary.py:311` calls `extract_terms(raw_text)` without the `language` argument, silently accepting this no-op.

**Impact:** On ES-only or EN-only text, full RU + ES + EN stop-words are applied, which over-filters valid terms in ES (e.g. "coche", "tren") if they happen to collide with the combined stop list. The documented contract is broken.

**Fix:** Implement language-conditional stop-word selection (`_STOP_WORDS_RU`, `_STOP_WORDS_ES`, `_STOP_WORDS_EN` depending on `language` hint), or simplify the contract to document "always uses combined stop-word set".

---

### F3 MED — Ё/ё Unicode exclusion from all pattern classes

**Location:** `core/term_extractor.py:82–94`

Russian Ё (U+0401) and ё (U+0451) lie outside the Unicode ranges `А-Я` (U+0410–U+042F) and `а-я` (U+0430–U+044F). They are absent from:

- `_RE_CAPITALIZED` — `[А-ЯA-Z]` anchor: "Ёжик" after a sentence break is never detected as a proper noun.
- `_RE_ABBREV` — `[A-ZА-Я]{2,}`: abbreviations starting with Ё (e.g. "ЁЛЦ") are missed.
- `_RE_WORD` — `[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}`: "ёжик" matches only from the second letter ("жик"), corrupting bigrams that include the word.

`_RE_WORD_CLEAN` is unaffected because `\w` covers all Unicode letters including Ё/ё.

Verified in Python:
```python
>>> _RE_WORD.findall('ёжик')
['жик']       # should be ['ёжик']
>>> _RE_CAPITALIZED.search('Начало Ёжик конец')
None          # should match 'Ёжик'
```

**Fix:** Add Ё/ё to the character classes: `[А-ЯЁA-Z]`, `[А-Яа-яЁёA-Za-z…]`.

---

### F4 MED — No output size cap in either `extract_terms` or `handle_extract_terms`

**Location:** `core/term_extractor.py:241`, `backend/text_scoring_service.py:97–108`

`extract_terms` returns an unbounded list. On a transcript with 1 000 capitalized words in one sentence (no punctuation), up to 1 000 entries can accumulate. `extract_from_history` compounds this: it calls `extract_terms` per history item and aggregates, then returns all terms above `min_frequency` — on large history corpora this is unbounded.

`handle_extract_terms` at the IPC layer also applies no cap before serializing the result over the Unix socket, potentially emitting megabyte-sized JSON responses.

**Fix:** Add `max_results: int = 100` to `extract_terms` (and `extract_from_history`) with a truncation on the sorted list before return. Apply the same cap in `handle_extract_terms` or accept an optional `limit` param from the caller.

---

### F5 MED — `privacy_mode` not checked in `handle_extract_terms` IPC

**Location:** `backend/text_scoring_service.py:83–109`

The `extract_terms` IPC method accepts raw transcript text in the `text` parameter and returns extracted terms (proper nouns, CamelCase tokens, abbreviations) that are derived from that text. When `privacy_mode` is enabled, transcript text should not leave the recording pipeline. No `privacy_mode` guard exists in `handle_extract_terms` — a caller can submit arbitrary transcript text and receive structured vocabulary extraction even in privacy mode.

Comparable IPC methods (e.g. `translate`, `rewrite_text`) do check privacy settings. This is a pattern inconsistency.

**Fix:** Add a `privacy_mode` check at the top of `handle_extract_terms`:
```python
if self._get_privacy_setting():
    return {"terms": [], "privacy_blocked": True}
```

---

### F6 LOW — No test coverage for `TermExtractor`

No test file targeting `TermExtractor` was found in `KrabEar/tests/`:
```
$ grep -rl "term_extractor\|TermExtractor\|extract_terms" KrabEar/tests/
(no output)
```

The module has zero direct unit tests despite multiple extraction paths (proper nouns, CamelCase, abbreviations, tech-with-digits, bigrams) and documented edge cases (multilingual input, empty text). F1 (AttributeError in IPC) and F3 (Ё exclusion) would have been caught by a basic test suite.

**Fix:** Add `KrabEar/tests/test_term_extractor.py` covering: empty input, single-word input, proper noun detection, CamelCase, abbreviation, tech-with-digits, bigram frequency threshold, `extract_from_history` aggregation, and at least one Ё-containing Russian word.

---

### F7 LOW — Trigram extraction silently absent despite `_extract_repeated_ngrams` supporting `n=3`

**Location:** `core/term_extractor.py:224`, `core/term_extractor.py:294–309`

`_extract_repeated_ngrams` accepts arbitrary `n`, but `extract_terms` only calls it with `n=2` (bigrams). The comment block in the class docstring lists "повторяющиеся биграммы/триграммы (≥3 раза)" as a supported strategy, but trigram extraction (`n=3`) is never invoked. As a result, three-word domain terms common in Russian transcriptions (e.g. "машинное глубокое обучение") are never surfaced.

**Fix:** Either call `_extract_repeated_ngrams` with `n=3` and a slightly higher `min_freq` (e.g. 3), or update the docstring to remove the trigram claim.

---

## Summary

| # | Severity | Issue |
|---|----------|-------|
| F1 | **HIGH** | `handle_extract_terms` IPC crashes on every call — accesses nonexistent `score`/`language`/`category` fields |
| F2 | **HIGH** | `language` param is completely ignored — documented contract broken |
| F3 | **MED** | Ё/ё (U+0401/U+0451) excluded from all regex patterns — proper nouns and bigrams with ё silently dropped |
| F4 | **MED** | No output size cap — unbounded IPC responses on large transcripts |
| F5 | **MED** | `privacy_mode` not checked — terms extracted from transcript text even in privacy mode |
| F6 | **LOW** | Zero test coverage for `TermExtractor` |
| F7 | **LOW** | Trigrams documented but never invoked |

**Idempotency:** Pass — algorithm is fully deterministic (no random state, no side effects).  
**Performance:** Acceptable — bigram Counter on 10k words completes in ~4 ms; the unbounded output (F4) is a greater concern than algorithmic complexity.  
**Algorithm quality:** Heuristic-only (no TF-IDF); frequency bonus applies a small linear boost. Adequate for vocabulary/glossary suggestions but F2 (language no-op) and F3 (Ё) reduce precision on real Russian transcripts.
