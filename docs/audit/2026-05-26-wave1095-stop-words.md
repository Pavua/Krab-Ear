# W1095 Audit: `core/stop_words.py` — Stop-Word Lists

**Date:** 2026-05-26  
**File audited:** `KrabEar/core/stop_words.py`  
**Status:** READ-ONLY audit, no code changes  
**Severity scale:** HIGH / MEDIUM / LOW

---

## Context

`stop_words.py` exports `StopWords` with four frozenset lists: RU (176 words), ES (157), EN (163), UK (138) — total 595 unique words in `_ALL`. Consumers: `keyword_cloud.py`, `history_service.py` (top-words), `translation_service.py` (glossary scoring). `TermExtractor` and `SmartVocabularyBuilder` maintain **separate, independent** stop-word lists in `term_extractor.py` and do not import from this module.

---

## Findings

### F1 — MEDIUM: Multi-word entry `"por qué"` is unreachable

**Location:** `_ES`, line 77.

`StopWords.filter_text()` operates on pre-tokenized word lists; `_TOKENIZE_RE` splits on `[^\W\d_]+` (letter runs only). The phrase `"por qué"` will never appear as a single token — the tokenizer produces `["por", "qué"]` individually. The multi-word entry sits in the set but can never match a filter call. `is_stop_word("por qué")` also returns `False` if the caller normalises input with the bundled regex. Additionally, the compiled `_TOKENIZE_RE` constant is defined at module level but **never called** anywhere in the file — it is dead code.

**Recommendation:** Split `"por qué"` into two separate entries `"por"` and `"qué"`. Remove or annotate `_TOKENIZE_RE`; if it is intended for external use, expose it or document the fact that `filter_text` expects callers to tokenize first.

---

### F2 — MEDIUM: RU oblique pronoun cases systematically absent (false negatives)

**Scope:** ~16 high-frequency forms missing from `_RU`.

The nominative pronouns are all present (`я`, `он`, `она`, `мы`, `вы`, `они`), but their most-common oblique cases are absent and will leak through keyword extraction:

| Base | Missing oblique forms |
|------|-----------------------|
| я | меня, мне, мной |
| ты | тебя, тебе, тобой |
| он/она | него, ней, неё, ему, ей, им |
| мы | нас, нам, нами |
| вы | вас, вам, вами |
| они | них, им, ими |
| который | который, которая, которое, которые, которого, которой, которым, которых |

Russian speech is inflection-heavy; a transcript like *"расскажи мне об этом"* would pass `мне` as a content term. This is the single largest gap relative to standard NLTK/spaCy Russian stop-word lists (e.g., `russtopwords`, Yandex Mystem).

**Recommendation:** Add the 16+ missing forms listed above. Consider a more systematic approach: generate all case forms programmatically or import a curated community list.

---

### F3 — LOW: EN auxiliary verbs `"need"` and `"dare"` are borderline false positives

**Location:** `_EN`, line 104.

Both `"need"` and `"dare"` are listed as modal-like auxiliaries (following a spaCy convention). However, in spoken transcripts they frequently carry topic content: *"we need a new approach"*, *"dare to innovate"*. Keyword extractors that use this list will silently drop these words even when they are semantically load-bearing verbs. The NLTK English stop-word corpus (179 words) does not include either word. `"own"` has a similar issue (adj vs. verb ambiguity).

**Recommendation:** Remove `"need"`, `"dare"`, `"own"` from `_EN` or document clearly that the list is intentionally broader than NLTK for spoken-word use.

---

### F4 — MEDIUM: Divergent parallel lists — `term_extractor.py` duplicates stop-words without re-using this module

**Locations:** `KrabEar/core/term_extractor.py` lines 16–66, `KrabEar/core/stop_words.py`.

`term_extractor.py` defines its own `_STOP_WORDS_RU`, `_STOP_WORDS_ES`, `_STOP_WORDS_EN` frozensets and never imports `StopWords`. The two lists have diverged:

- RU: 15 words in `term_extractor` but absent from `stop_words` (e.g. `которая`, `которого`, `давай`, `давайте`, `ваша`, `ваши`, `всем`, `всех`).
- ES: 15 words in `term_extractor` but absent from `stop_words` (e.g. `bueno`, `cada`, `entonces`, `hace`, `hacen`, `misma`, `mismo`, `otro`, `otras`).
- EN: no divergence.

This means `keyword_cloud.py` (uses `stop_words.py`) and `TermExtractor`-backed paths (uses `term_extractor.py`) will produce inconsistent keyword sets from the same input.

**Recommendation:** Refactor `term_extractor.py` to import and extend `StopWords.get_stop_words()` rather than maintaining a parallel list. This is the highest-ROI structural fix.

---

### F5 — LOW: ES/EN cross-language token collisions in `_ALL`

**Scope:** 4 tokens: `"has"`, `"he"`, `"me"`, `"no"`.

When `is_stop_word()` or `filter_text()` is called without a `language` argument, the `_ALL` set is used. All four words are valid content words in one language:

- `"he"` — third-person pronoun EN, but means "he drank/made" in Spanish (verb `haber`/`hacer`).
- `"me"` — reflexive pronoun ES, also valid English pronoun.
- `"no"` — negation EN; `"no"` is also a valid Spanish word.
- `"has"` — English auxiliary; Spanish second-person present of `haber`.

Callers that omit `language=` (all three production call-sites do so) will silently suppress these tokens from keyword results.

**Impact:** Low in practice because these are short words and the default `min_length=2` filter in `filter_text` would already drop single-character tokens. The `"has"` / `"he"` collision could affect bilingual RU+ES+EN transcripts.

**Recommendation:** Document the known collisions in a module-level comment. Encourage callers to pass an explicit `language=` argument where the transcript language is known.

---

### F6 — LOW: RU spoken-language filler words absent

**Location:** `_RU`.

Conversational fillers extremely common in Russian speech are missing: `вообще`, `значит`, `короче`, `слушай`, `понимаешь`, `давай`, `давайте`. These would typically appear in Krab Ear transcripts of informal speech and, if not filtered, will inflate keyword clouds with meaningless filler terms.

`term_extractor.py` already includes `давай`/`давайте` in its parallel list (F4 above), confirming the gap was noticed previously but not propagated back to `stop_words.py`.

**Recommendation:** Add the ~6 common fillers to `_RU`. This is a low-effort, high-impact improvement for the primary use-case language.

---

## Summary Table

| # | Severity | Finding | Affected languages |
|---|----------|---------|-------------------|
| F1 | MEDIUM | `"por qué"` multi-word entry + dead `_TOKENIZE_RE` | ES |
| F2 | MEDIUM | 16+ missing RU oblique pronoun/pronoun-relative forms | RU |
| F3 | LOW | `"need"`, `"dare"`, `"own"` borderline false positives | EN |
| F4 | MEDIUM | `term_extractor.py` maintains divergent duplicate lists | RU, ES |
| F5 | LOW | 4 cross-language collisions in `_ALL` (no `language=` guard) | EN/ES |
| F6 | LOW | RU spoken-language fillers absent (`вообще`, `значит`, etc.) | RU |

---

## Wire Status

- `keyword_cloud.py` — **wired**, uses `StopWords.get_stop_words()` for all four languages (lines 27–31). Falls back to empty on import error.
- `history_service.py` — **wired**, uses `__import__` with union of all four languages (lines 2248–2253).
- `translation_service.py` — **not wired** to this module; uses its own inline `_STOP_WORDS_RU`/`_STOP_WORDS_ES` constants.
- `term_extractor.py` — **not wired**; parallel lists (see F4).
- `html_report.py` — **not wired**; hardcoded inline stop-word set (line 625).

## Test Coverage

`KrabEar/tests/test_stop_words.py` — comprehensive, 348 lines, 7 test classes covering: `get_stop_words`, `is_stop_word` (case-insensitive), `filter_text` (order, min_length, dedup), `supported_languages`, Unicode validity, and a smoke-test for `HistoryService._STOP_WORDS`. No tests exercise the multi-word `"por qué"` collision (F1) or language-collision behaviour without `language=` (F5).

## Performance

`_ALL` = 595 words (frozenset). Lookup is O(1). Module-level construction at import time is negligible. No performance concern at any realistic call rate.
