# Wave 1008 Audit — LanguageDetector (`core/language_detector.py`)

**Date:** 2026-05-26  
**Auditor:** Sub-agent W1008  
**Branch:** `audit-language-detector-W1008`  
**File audited:** `KrabEar/core/language_detector.py` (165 lines)

---

## Summary

`LanguageDetector` is a dependency-free heuristic detector that classifies text as `ru`, `uk`, `es`, `en`, or `und` by counting Cyrillic vs Latin characters, then checking language-specific marker characters.  
It is used in three live call sites: `translation_service.py` (auto-detect source language for `translate_selection`), `metadata_enricher.py` (populate `language_detected` field on history items), and `stt_router.py` (injected via DI but marked deprecated in favour of `AudioLanguageID`).

**5 findings. 0 crashes. 1 high, 2 medium, 2 low.**

---

## Findings

### F1 — HIGH: False positive — French/Turkish/Portuguese classified as Spanish

**Mechanism:** `_ES_MARKERS = frozenset("ñáéíóúüÑÁÉÍÓÚÜ¿¡")` includes `é`, `ü`, `á`, `í`, `ó`, `ú`. These characters also appear in French (`café`, `très`, `élégant`), Turkish (`bugün`, `güzel`), Portuguese (`você`, `está`) and German (`für`, `über`). `_detect_latin` iterates the string and returns `"es"` at the first marker hit — no counter, no threshold.

**Observed behaviour:**

| Input | Expected | Detected |
|-------|----------|----------|
| `"café au lait"` | `en` or `und` | `es` |
| `"très élégant"` | `en` or `und` | `es` |
| `"nasılsın bugün güzel"` | `en` or `und` | `es` |
| `"Boa tarde, como vai você?"` | `en` or `und` | `es` |

**Impact:** `TranslationService.handle_translate_selection` uses the detector for source language auto-detection. French or Turkish user-selected text will be routed to the `es→ru` translation path instead of the `en→ru` fallback, producing garbage output silently.

**Fix direction:** Require a minimum marker density threshold (e.g. ≥2 markers OR markers / letter_count ≥ 0.02) before declaring Spanish, to avoid single-char false triggers. Alternatively, restrict ES markers to the subset that rarely appears in French/German/Portuguese (`ñ`, `¿`, `¡`).

---

### F2 — MEDIUM: Ukrainian detection depends on explicit marker characters — most plain Ukrainian text detects as Russian

**Mechanism:** `_detect_cyrillic` returns `"uk"` only when a character from `_UK_ONLY_CHARS = frozenset("іїєґІЇЄҐ")` is found. Ukrainian words without these markers (е.г. "Добрий вечір дорогий", "Сьогодні гарна погода") — which contain `і` — should trigger `uk` but only if `і` (U+0456) is present, not `и` (U+0438, Russian i).

**Observed behaviour:**

| Input | Expected | Detected |
|-------|----------|----------|
| `"Добрий вечір дорогий"` | `uk` | `uk` ✓ (`і` present in `вечір`) |
| `"Сьогодні гарна погода тут"` | `uk` | `uk` ✓ (`і` in `Сьогодні`) |
| `"Прывітанне як справы"` | `be` (Belarusian) | `uk` ✗ |
| `"Гарно. Так. Ні."` (pure overlap) | `uk` | `ru` ✗ |

**Real issue:** Belarusian text (which uses Cyrillic but has no `_UK_ONLY_CHARS` markers) is reported as `uk` when `і` appears coincidentally, or as `ru` otherwise. Belarusian is not a supported output language and no `und` fallback is offered for unsupported Cyrillic scripts. The project is RU/ES primary so this is low-operational impact, but metadata tags on Belarusian audio will be incorrect.

**Fix direction:** Document the known Belarusian / Macedonian / Bulgarian false-positive as a design limitation in the module docstring. No code change strictly needed given supported language scope.

---

### F3 — MEDIUM: Code-switching — Latin-dominant mixed text ignores Russian context

**Mechanism:** When Cyrillic and Latin both present, the dominant script determines the language branch entirely. There is no bonus for "the minority script's language being the session language."

**Observed behaviour:**

| Input | Cyrillic | Latin | Detected |
|-------|----------|-------|----------|
| `"я пишу code на python"` | 6 | 9 | `en` (conf=0.588) |
| `"estoy usando tensorflow para ml"` | 0 | 31 | `en` (conf=1.0) |
| `"я написал python код и он работает хорошо сегодня"` | 29 | 6 | `ru` (conf=0.854) ✓ |

The first example is a natural Russian sentence with two embedded English technical terms. The detector classifies it as English with 59% confidence. When this result feeds `translate_selection`, the `en→ru` path is chosen — translating the Russian words back through the translation engine is pointless and may produce errors.

**Fix direction:** For mixed texts with confidence < 0.70, consider incorporating session/history language hint (already available as `settings.default_language`) before defaulting.

---

### F4 — LOW: `stt_router.py` stores injected `LanguageDetector` but never calls it

In `STTRouter.__init__`, `self._language_detector = language_detector` is stored. The field is never accessed anywhere else in the file. The docstring says "устаревший параметр" (deprecated parameter), confirming the intent to remove it, but the field persists, creating dead-weight and a misleading API surface (callers might expect it to be active).

**Fix direction:** Remove `language_detector` parameter from `STTRouter.__init__` and the corresponding docstring line. The field is a no-op.

---

### F5 — LOW: No test coverage for code-switching false negatives and unsupported-language false positives

Existing tests in `test_language_detector.py` cover: pure RU/UK/ES/EN sentences, empty/whitespace/digits/emoji, 50/50 tie, mixed where cyrillic dominates, and the batch API. The following scenarios have zero test coverage:

- French text classified as Spanish (F1 above).
- Turkish text classified as Spanish (F1 above).
- Portuguese text classified as Spanish (F1 above).
- Latin-dominant Russian code-switching text classified as English (F3 above).
- Belarusian classified as Ukrainian (F2 above).
- Single-char boundary: `"і"` (U+0456 Ukrainian i) vs `"и"` (U+0438 Russian i) — both produce `uk`/`ru` respectively but no assertion verifies the Unicode point distinction.

The performance benchmark in `test_performance_unit_benchmarks.py` uses only clean RU/EN/ES sentences — it would not catch a quadratic worst-case (though the current O(n) algorithm has no such case).

---

## Wire Status

| Caller | Usage | Notes |
|--------|-------|-------|
| `backend/translation_service.py:175` | Auto-detect source language for `translate_selection` IPC | **Active, high impact** — feeds translation path selection |
| `backend/metadata_enricher.py:113` | Populate `language_detected` field on history items | **Active** — cosmetic metadata, lower impact |
| `core/stt_router.py:295` | Stored but never called | **Dead field** — see F4 |

---

## Performance

`detect()` is O(n) in text length (two passes: `_count_scripts` + one of `_detect_cyrillic`/`_detect_latin`). The benchmark gate (`test_performance_unit_benchmarks.py`) requires 100 calls in < 30 ms (budget 300 µs/call). On M4 Max the observed time is well under budget. No caching is needed at this scale.

---

## Edge Case Behaviour (verified)

| Input | language | confidence | script |
|-------|----------|------------|--------|
| `""` | `und` | 0.0 | `unknown` |
| `"   \t\n"` | `und` | 0.0 | `unknown` |
| `"😀🎉🔥"` | `und` | 0.0 | `unknown` |
| `"123 456"` | `und` | 0.0 | `unknown` |
| `"а"` (1 char) | `ru` | 0.4 | `cyrillic` |
| `"ok"` (2 chars) | `en` | 0.4 | `latin` |
| `"abc"` (3 chars, = MIN_LETTERS) | `en` | 1.0 | `latin` |
| `"ПРИВЕТ"` | `ru` | 1.0 | `cyrillic` |
| `"блять пиздец нахуй"` (мат) | `ru` | 1.0 | `cyrillic` |

Empty, emoji-only, digits-only, whitespace-only all correctly return `und`. Mat/slang Cyrillic correctly returns `ru`. All-caps correctly handled. Single-character inputs correctly produce low confidence (0.4).

---

## Action Items

| # | Priority | Action |
|---|----------|--------|
| 1 | HIGH | Add marker-density threshold in `_detect_latin` to reduce French/Turkish/Portuguese → es false positives |
| 2 | MEDIUM | Add tests for F1 (French/Turkish/Portuguese) and F3 (Latin-dominant code-switching) in `test_language_detector.py` |
| 3 | LOW | Remove dead `language_detector` parameter from `STTRouter.__init__` |
| 4 | LOW | Add module docstring note about known unsupported-Cyrillic (Belarusian, Bulgarian, Macedonian) limitation |
