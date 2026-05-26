# Wave 991 — NumberNormalizer Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/number_normalizer.py`  
**Tests:** `KrabEar/tests/test_number_normalizer.py`, `KrabEar/tests/test_normalizers.py`  
**Auditor:** W991 sub-agent

---

## Summary

`NumberNormalizer` is a heuristic lookup-table + regex converter for spoken numerals (RU/ES/EN) to digit form. Overall design is sound but has 5 concrete findings: one **BUG** (compound-word corruption), one **GAP** (ES ordinals missing), one **GAP** (decimal phrases unsupported), one **PERF** concern (new instance per engine call), and one **AMBIGUITY** note (unit-word context).

---

## Findings

### F1 — BUG: Compound-word partial match corrupts tokens (HIGH)

**Pattern:** `двадцатилетний`, `двадцатиметровый`, `тридцатиградусный` — words where a decade prefix (`двадцати-`, `тридцати-`) appears at the start of a compound word.

**Root cause:** `_replace_cardinals_ru` uses `(?<!\w)` as a left-boundary assertion to prevent prefix matches, but `двадцать` appears at the start of `двадцатилетний` after a word boundary. The word-boundary lookbehind `(?<!\w)` succeeds at the string start or after a space — so `двадцат` (the regex alternation is built from dictionary keys longest-first) does NOT match, but `двадцать` (from `_RU_TENS`) matches the prefix `двадцат` of `двадцатилетний`... actually the right boundary `(?!\w)` is what fails to protect here: the regex matches `двадцать` but the remainder `-летний` lacks the leading space, so the match is: the pattern `(?<!\w)(?:двадцать)(?:\s+...)` matches only `двадцать` out of `двадцатилетний` because the lookbehind passes at `^` but the lookahead right-side check is NOT applied (the full_pat lacks a right-boundary guard).

**Observed output:**
```
двадцатилетний опыт  →  2дцатилетний опыт   # CORRUPT
двадцатиметровый     →  2дцатиметровый       # CORRUPT
тридцатиградусный    →  3дцатиградусный      # CORRUPT
```

Words NOT affected (no match): `двухлетний`, `трёхлетний`, `семидесятых` — because their stems don't appear literally in `_RU_TENS`/`_RU_ONES` keys.

**Fix:** Add a right-boundary word assertion `(?!\w)` at the end of the `num_seq_pat` group in `_replace_cardinals_ru`. Currently the pattern ends without one: `rf"(?<!\w)(?:(?:{word_pat})(?:\s+(?:{word_pat}))*)"` — the trailing token has no `(?!\w)` guard. Adding `(?!\w)` after the alternation group would reject `двадцатилетний` since `т` follows the match position.

---

### F2 — GAP: Spanish ordinals not implemented (MEDIUM)

`_ES_ORDINAL_SUFFIXES` is not defined anywhere in the module. The Spanish `_normalize_es` path calls only `_replace_cardinals_es`, skipping ordinals entirely.

**Affected words:** `primero`, `segundo`, `tercero`, `cuarto`, `quinto`, etc. — all pass through unchanged.

```python
n.normalize("el primero", "es")   # → "el primero"  (no conversion)
n.normalize("segundo", "es")      # → "segundo"
```

RU has a full `_RU_ORDINAL_SUFFIXES` dict with gender/case forms. EN has `_EN_ORDINAL_SUFFIXES`. ES has none.

**Recommendation:** Add `_ES_ORDINAL_SUFFIXES` with `primero/a/os/as` → `1°`, `segundo/a` → `2°`, etc., following the EN/RU pattern.

---

### F3 — GAP: Decimal/fractional phrases unsupported (MEDIUM)

Neither RU nor ES has support for decimal verbal expressions like `«три целых пять»` (3.5) or `«tres coma cinco»` (3,5).

**Observed:**
```python
n.normalize("три целых пять", "ru")     # → "3 целых 5"  (partial, garbled)
n.normalize("tres coma cinco", "es")    # → "3 coma 5"   (partial)
```

`_RU_FRACTIONS` covers only simple fractions (`половина` → `1/2`, `треть` → `1/3`). There is no `целых`/`coma`/`punto` handling.

This matters for financial/scientific transcripts: `«выручка три целых пять миллиона»` → `«3 целых 5 миллиона»` is semantically broken.

**Recommendation:** Detect `<number> целых <number>` → `<number>.<number>` in a pre-pass regex; similarly `<number> coma <number>` for ES.

---

### F4 — PERF: New instance created per engine call (LOW)

In `KrabEar/core/engine.py` line 951:
```python
_nn_result = NumberNormalizer().normalize(text, language=_norm_lang)
```

A fresh `NumberNormalizer()` instance is constructed on every transcription. The class defines a class-level `_compiled: Dict[str, re.Pattern] = {}` cache dict but **never populates it** — `_replace_cardinals_ru/es/en` rebuild the `word_pat` alternation and call `re.sub` (which compiles internally) on each invocation.

On a 100-iteration benchmark: 8.1 ms for 100 calls, ~0.08 ms per call — acceptable but unnecessary overhead on long transcriptions. The `_compiled` class attribute is dead code.

**Recommendation:** Either (a) pre-compile patterns once at class-level construction and cache them, or (b) keep engine.py using a singleton/cached instance rather than `NumberNormalizer()` per call.

---

### F5 — AMBIGUITY: Unit-word context not distinguished (INFO)

`«пять часов»` maps to `«5 ч»` (5 hours unit abbreviation) regardless of context. There is no disambiguation between:
- `«прошло пять часов»` — duration, unit correct → `«5 ч»` ✓  
- `«пять часов вечера»` — time of day → `«5 ч вечера»` ✓ (acceptable)
- `«пять часов пик»` — idiomatic phrase → `«5 ч пик»` (slightly odd but acceptable)

This is intentional/documented heuristic behavior. The doc comment in the module header makes no claim about time disambiguation. No behavioral fix needed — noted for awareness.

---

## Positive Observations

1. **Locale coverage is solid:** RU has full cardinal system (0–999,999,999,999), all hundred/thousand/million/milliard forms, 10 ordinal stems with gender/case variants. ES covers 0–999 atomically (veinte-series inline), thousands, millones. EN has 0–19 + decades + hundred/thousand/million/billion.

2. **Compound numbers work correctly:** `«сто двадцать три»` → `«123»`, `«ciento veintitres»` → `«123»`, `«one hundred twenty three»` → `«123»`. The `_parse_number_*` functions handle multiplier accumulation correctly.

3. **Idempotency confirmed:** Existing digit strings (`«25»`, `«123 штуки»`, `«42 apples»`) pass through unchanged. The `(?<!\w)` left-boundary guards prevent re-matching digits.

4. **Capitalization handled:** `«Сто двадцать три»` (sentence-initial cap) normalizes correctly to `«123»` via `re.IGNORECASE`.

5. **Wire status:** Called from `engine.py` post-STT pipeline (line 951), gated by `NUMBER_NORMALIZATION_ENABLED` setting (default `True`). Not wired via `text_postprocessor.py` — normalization happens in the engine directly before LLM rewrite.

6. **Test coverage:** `test_number_normalizer.py` has 5 test classes (simple RU, compound RU, simple ES, compound ES, ordinals, unaffected text, concurrency) with 30+ test methods. Thread-safety confirmed via 10-thread concurrent test. Performance benchmark in `test_performance_unit_benchmarks.py` (100 calls < 50 ms budget).

---

## Findings Summary

| # | Severity | Description |
|---|----------|-------------|
| F1 | HIGH/BUG | Compound-word prefix match corrupts `двадцатилетний` → `2дцатилетний` |
| F2 | MEDIUM/GAP | Spanish ordinals (`primero`, `segundo`) not implemented |
| F3 | MEDIUM/GAP | Decimal phrases `«три целых пять»` not normalized |
| F4 | LOW/PERF | New `NumberNormalizer()` instance per engine call; `_compiled` cache unused |
| F5 | INFO | Unit-word `часов` ambiguity (time vs duration) — by design |
