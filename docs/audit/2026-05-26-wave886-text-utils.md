# Audit: Text-Utils Layer — Wave 886

**Date:** 2026-05-26  
**Files audited:**
- `KrabEar/core/text_postprocessor.py` (292 lines)
- `KrabEar/core/text_anonymizer.py` (236 lines)
- `KrabEar/core/punctuation_fixer.py` (169 lines)
- `KrabEar/core/abbreviation_expander.py` (356 lines)

**Scope:** regex correctness · PII redaction completeness · RU/ES/EN coverage gaps

---

## Summary

**11 findings total:** 2 MEDIUM, 6 LOW, 3 INFO.  
No HIGH-severity issues. No security regressions. All four modules are structurally sound — lazy imports, no circular deps, no external dependencies in anonymizer, good overlap-handling.

---

## TextAnonymizer (`text_anonymizer.py`)

### Finding 1 — MEDIUM: `inn` pattern (12 digits) subsumes `credit_card` (16 digits) and `passport` (10 digits) for intermediate lengths

**Location:** `text_anonymizer.py:101-105`

```python
("inn", r"\b\d{12}\b", "[ИНН]"),
```

The `inn` rule matches any standalone 12-digit sequence unconditionally — no checksum guard unlike `credit_card`. In practice, ИНН has a checksum (two check digits), but the pattern does not validate it. This means any 12-digit number in text (e.g. a product barcode, an order number) is aggressively redacted as `[ИНН]`.

Additionally, rule ordering matters: `inn` (12 digits) and `passport` (10 digits) share no guard similar to Luhn. The `credit_card` rule wisely uses `_passes_luhn()`; the same discipline should apply to `inn` (контрольные цифры ИНН по алгоритму ФНС) and optionally to `snils`.

**Fix (suggested):** Add `_passes_inn_checksum()` helper (ФНС algorithm: weighted sum mod 11 for positions 1-10, then 11, with remainder ≥ 10 → 0).

---

### Finding 2 — MEDIUM: No Spanish/international phone coverage — EN/ES transcriptions pass through unredacted

**Location:** `text_anonymizer.py:59-73`

The `phone` rule covers only Russian formats (`+7 …` and `8 …`). International formats commonly appearing in RU/ES/EN transcriptions are unhandled:

| Format | Example | Redacted? |
|--------|---------|-----------|
| `+7 (999) 123-45-67` | Russian | ✅ |
| `+34 612 345 678` | Spanish mobile | ❌ |
| `+1 (555) 123-4567` | US/CA | ❌ |
| `+49 30 12345678` | German | ❌ |
| `0034 612 345 678` | Spanish with country prefix | ❌ |

For a bilingual (RU/ES primary) assistant this is a notable gap. When the `anonymize` step is invoked on Spanish call recordings, phone numbers are silently preserved.

**Fix (suggested):** Add an international catch-all rule before the existing `phone` rule:

```python
(
    "phone_intl",
    r"(?<!\d)\+(?:[1-9]\d{0,2})[\s\-]?(?:\(?\d{1,4}\)?[\s\-]?){2,5}\d{2,4}(?!\d)",
    "[ТЕЛЕФОН]",
),
```

Or add dedicated ES (`+34`) and EN (`+1`) variants with tighter anchoring.

---

### Finding 3 — LOW: `passport` pattern is ambiguous — also matches ИНН organisational (10 digits) and СНИЛС partial

**Location:** `text_anonymizer.py:89-93`

```python
r"\b(?:\d{4}[\s\-]\d{6}|\d{10})\b"
```

A bare 10-digit sequence matches both an RF passport number and an organisational ИНН (10 digits). These are different PII categories (paспорт vs ИНН юрлица). The redaction label `[ПАСПОРТ]` is therefore sometimes wrong. This is a labelling problem more than a data-leak problem, but can confuse audit trails.

**Fix (low priority):** Split into `passport` (require `\d{4}[\s\-]\d{6}` formatted form) and `inn_org` (10-digit bare number with checksum guard).

---

### Finding 4 — LOW: `date_of_birth` pattern fires on all dates, not just birth dates

**Location:** `text_anonymizer.py:95-99`

```python
r"\b(?:0?[1-9]|[12]\d|3[01])[.\-/](?:0?[1-9]|1[0-2])[.\-/](?:19|20)\d{2}\b"
```

This matches any date in `DD.MM.YYYY` format, including meeting dates, document dates, and deadlines. It restricts to 19xx/20xx years which helps, but in a meeting transcript "состоится 12.06.2026" gets redacted as `[ДАТА_РОЖДЕНИЯ]`, creating noise.

**Fix:** The rule name should be renamed to `date` with a neutral label `[ДАТА]`, or filtering should be context-aware (requires NER, out of scope for rule-based).

---

### Finding 5 — LOW: No ОГРН or email-domain-only patterns for ES context

**Location:** `text_anonymizer.py` — missing rules

ОГРН (13/15 digits for legal entity registration number) is common in Russian business transcriptions. It overlaps with `inn` (12 digits) only at length 13+, so it would not be caught. Not flagging as MEDIUM since ОГРН is less sensitive than ИНН/phone, but worth noting for compliance completeness.

---

### Finding 6 — INFO: `re.IGNORECASE` on all rules is unnecessary for digit-only patterns

**Location:** `text_anonymizer.py:116`

```python
return [(name, re.compile(pattern, re.IGNORECASE), repl) for name, pattern, repl in raw]
```

`re.IGNORECASE` is meaningful for `email` only; applying it to `phone`, `credit_card`, `passport`, `inn`, `snils`, `date_of_birth` wastes a small amount of CPU on each match. Not a correctness bug. Consider compiling per-rule with appropriate flags.

---

## PunctuationFixer (`punctuation_fixer.py`)

### Finding 7 — LOW: `_NO_SPACE_AFTER_PUNCT_RU_RE` applied globally — fires on Spanish too

**Location:** `punctuation_fixer.py:19`, `fix()` lines 72-73`

```python
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s\d»\"')\]])")
```

This pattern and `_NO_SPACE_AFTER_PERIOD_RE` are applied **before** the language branch, so they run on Spanish text too. The `»` guillemet character is Russian-specific; Spanish uses `»` occasionally but not as the primary quotation mark. The `_NO_SPACE_AFTER_PERIOD_RE` only capitalises after period when the next char is `[А-ЯA-ZЁ]` — which already excludes lowercase Spanish, but the `_NO_SPACE_AFTER_PUNCT_RU_RE` insertion of spaces is language-neutral and could mangle Spanish text with no issues (correctly, in this case), though it is misleadingly named `_RU`.

**Fix (cosmetic):** Rename to `_NO_SPACE_AFTER_PUNCT_RE` or move into `_fix_russian` / `_fix_spanish` with appropriate per-language variants.

---

### Finding 8 — LOW: English text receives no punctuation fixes (language="en" is a no-op branch)

**Location:** `punctuation_fixer.py:76-79`

```python
if language == "ru":
    result = self._fix_russian(result)
elif language == "es":
    result = self._fix_spanish(result)
```

There is no `_fix_english()` branch. When `TextPostProcessor` is instantiated with `FixPunctuation(language="en")` (not the default but a valid invocation), only the generic rules run (multi-space, space-before-punct, capitalise-after-sentence). No English-specific rules (e.g. straight-quote → curly-quote, double-space after period normalisation).

**Fix:** Add minimal `_fix_english()` — at minimum: capitalise first word (same as RU/ES), handle `'s` elision spacing.

---

### Finding 9 — LOW: `_STANDALONE_YA_RE` replaces «я» in all positions including word-medial via `(?<!\w)` lookahead, but `\w` is ASCII-only in Python by default for `re` without `re.UNICODE` flag — in practice `re.compile` in Python 3 is Unicode-aware by default, so this is a non-issue, but…

**Location:** `punctuation_fixer.py:32`

```python
_STANDALONE_YA_RE = re.compile(r"(?<!\w)(я)(?!\w)")
```

Python 3 `re` treats `\w` as Unicode-aware by default (`re.UNICODE` is the default). So `(?<!\w)(я)(?!\w)` correctly anchors on Cyrillic word boundaries. This is fine. Noting as INFO.

---

## AbbreviationExpander (`abbreviation_expander.py`)

### Finding 10 — LOW: `_make_pattern` lookahead `(?=\s|$|[,;:!?»)])` misses `«` (opening guillemet) and `(` — abbreviation immediately before an opening parenthesis won't expand

**Location:** `abbreviation_expander.py:122`

```python
return re.compile(r"(?<!\w)" + escaped + r"(?=\s|$|[,;:!?»)])", re.IGNORECASE)
```

The lookahead after the abbreviation requires whitespace, end-of-string, or specific punctuation. Missing characters:
- `«` — opening guillemet (RU quotes): `напр.«пример»` won't match
- `(` — opening parenthesis: `т.е.(см. выше)` won't match
- `"` — opening ASCII quote
- `\n` — newline (separate from `$` which is end-of-string without `re.MULTILINE`)

**Fix:** Add `\n«("` to the lookahead character class:

```python
r"(?=\s|\n|$|[,;:!?»«\(\"'])"
```

---

### Finding 11 — INFO: W858 hallucination-patterns gap — `TextPostProcessor.DEFAULT_CHAIN` does not include `expand_abbreviations`

**Location:** `text_postprocessor.py:188`

```python
DEFAULT_CHAIN: list[str] = ["strip_whitespace", "fix_punctuation", "normalize_entities"]
```

`expand_abbreviations` is available as a built-in step but is **not** in the default chain. Callers must explicitly request it. This means abbreviation expansion is opt-in; the W858 audio-engine audit noted that hallucination stripping in `TextUtils` operates on Russian patterns only — the same selective application issue applies here. If a Spanish transcript contains `p.ej.` (por ejemplo) and goes through the default chain, it won't be expanded.

This is by design (abbreviation expansion changes meaning, so it should be opt-in), but it should be documented clearly as intentional.

---

## TextPostProcessor (`text_postprocessor.py`)

No regex correctness issues found. The pipeline design is clean: lazy imports, no circular deps, graceful degradation (exception in a step logs and continues with previous text). One style note:

- `DEFAULT_CHAIN` is a module-level mutable list. If a caller appends to it accidentally, the default changes globally. Consider `tuple` or a factory function.

---

## RU/ES/EN Coverage Matrix

| Capability | RU | ES | EN |
|---|---|---|---|
| Phone redaction | ✅ full | ❌ missing | ❌ missing |
| Email redaction | ✅ | ✅ | ✅ |
| Credit card (Luhn) | ✅ | ✅ | ✅ |
| ИНН/СНИЛС/Паспорт | ✅ (no checksum) | N/A | N/A |
| Date pattern | ✅ (over-fires) | ✅ (same) | ✅ (same) |
| Punctuation fixes | ✅ full | ✅ good | ❌ no branch |
| Abbreviation expand | ✅ 30 entries | ✅ 18 entries | ✅ 22 entries |
| Abbreviation boundary (lookahead) | ⚠️ misses `«(\n` | ⚠️ same | ⚠️ same |

---

## Recommended Actions (priority order)

| # | Priority | Action |
|---|----------|--------|
| 1 | MEDIUM | Add ИНН checksum validation (`_passes_inn_checksum`) |
| 2 | MEDIUM | Add international phone rule (`+34`, `+1`, generic `+X…`) |
| 3 | LOW | Expand `_make_pattern` lookahead to include `«(\n"` |
| 4 | LOW | Add `_fix_english()` branch to `PunctuationFixer` |
| 5 | LOW | Rename `date_of_birth` → `date` with neutral label `[ДАТА]` |
| 6 | LOW | Split 10-digit `passport` vs `inn_org` for correct labelling |
| 7 | INFO | Document DEFAULT_CHAIN opt-in intent for `expand_abbreviations` |
