# W1241 Cross-cutting audit: TextPostProcessor pipeline + dependent modules

**Date:** 2026-05-26  
**Branch:** `audit/text-postprocessor-cross-W1241` (off `codex/krab-ear-v2` @ `62df2ec9`)  
**Scope:** `core/text_postprocessor.py`, `core/abbreviation_expander.py`, `core/text_anonymizer.py`,
`core/number_normalizer.py`, `core/datetime_normalizer.py`, `core/punctuation_fixer.py`,
`core/language_detector.py`, `core/emotion_detector.py`

---

## Prior wave merge status

| Wave | PR | Status | Description |
|------|----|--------|-------------|
| W1001 | #918 | OPEN (docs only) | Audit search_highlighter |
| W1011 | #926 | OPEN (docs only) | Audit TextAnonymizer PII gaps |
| W1019 | #942 | **OPEN — fix NOT merged** | language_detector FR/TR/PT false positive |
| W1020 | #941 | **OPEN — fix NOT merged** | emotion_detector negation particles |
| W1081 | #1001 | **MERGED** (commit `1b14fe94`) | abbreviation_expander ambiguous opt-in |
| W1101 | #1014 | OPEN (docs only) | TextPostProcessor pipeline audit |
| W1115 | #1027 | **OPEN — fix NOT merged** | datetime ES date day=0 range guard |
| W1118 | #1032 | **OPEN — fix NOT merged** | add_abbreviation IPC handler |
| W1119 | #1030 | **OPEN — fix NOT merged** | AbbreviationExpander RLock |

---

## Findings (5 findings, 2 HIGH · 2 MEDIUM · 1 LOW)

### F1 HIGH — W1115 ES date day=0 guard absent from `_normalize_date_es`

**File:** `core/datetime_normalizer.py`, `_normalize_date_es._repl_date` (line 586–603)

The RU path (`_repl_digit_date`, line 401) and EN path (`_repl_date2`, line 754) both include:
```python
if not (1 <= day <= 31):
    return m.group(0)
```
The ES `_repl_date` at line 586 **has no such guard**. Combined with the existing ordering bug
(`_normalize_time_es` runs before `_normalize_date_es`): input `"tres de noviembre"` →
time step converts `"tres"` → `"03:00 de noviembre"` → date step matches `"00"` from `"03:00"`,
parses `day=0`, and writes `"00.11"` permanently to history.

PR #1027 (W1115) is open and **not merged** into `codex/krab-ear-v2`.

**Fix:** add `if not (1 <= day <= 31): return m.group(0)` immediately after the `day = int(day_str)` parse in `_normalize_date_es._repl_date`.

---

### F2 HIGH — W1019 language_detector FR/TR/PT false positives not merged

**File:** `core/language_detector.py`, `_detect_latin()` (line 151–157)

The current implementation returns `"es"` on the **first ES-marker character found**, with no
FR/TR/PT exclusion and no density threshold:
```python
def _detect_latin(text: str) -> str:
    for ch in text:
        if ch in _ES_MARKERS:
            return "es"
    return "en"
```
French (`é`, `à`, `ç`, `«»`), Turkish (`ü`, `ş`, `ğ`, `ı`), and Portuguese (`ã`, `õ`) texts all
misclassify as `"es"`, silently routing through the ES→RU translation pipeline.

PR #942 (W1019) is open and **not merged**. Fix adds `_FR_MARKERS`, `_TR_MARKERS`, `_PT_MARKERS`
exclusion sets + 2% density threshold.

---

### F3 MEDIUM — W1119 AbbreviationExpander RLock not merged — concurrent expand+add races

**File:** `core/abbreviation_expander.py`

`AbbreviationExpander._abbrevs` and `_compiled` are mutable shared state with no lock.
`add_abbreviation()` modifies `_abbrevs` and calls `_rebuild_compiled()` — no thread safety.
`expand()` iterates `self._compiled[lang]` — concurrent with `add_abbreviation()` this can produce
`KeyError`, `RuntimeError: dictionary changed size during iteration`, or partially rebuilt pattern list.

PR #1030 (W1119) is open and **not merged**. Fix adds `threading.RLock` with a snapshot pattern in
`expand()` (acquire lock → copy compiled list → release → iterate copy).

---

### F4 MEDIUM — W1118 add_abbreviation IPC handler not wired

**File:** `backend/text_processing_service.py`

`TextProcessingService` exposes `handle_expand_abbreviations`, `handle_remove_abbreviation`,
`handle_list_abbreviations` — but **no `handle_add_abbreviation`**. Users cannot add custom
abbreviations via IPC. `AbbreviationExpander.add_abbreviation()` exists since W1081 but is
unreachable from the IPC layer.

PR #1032 (W1118) is open and **not merged**.

---

### F5 LOW — W1020 emotion_detector negation/affirmative particles not removed

**File:** `core/emotion_detector.py`, `_NEGATIVE_WORDS` and `_POSITIVE_WORDS` dicts (lines 16, 23, 28, 36, 42, 47)

Negation particles (`«не»`, `«нет»`, `"no"`, `"never"`, `"not"`) remain in `_NEGATIVE_WORDS` and
affirmative particles (`«да»`, `"sí"`, `"yes"`) remain in `_POSITIVE_WORDS`. Neutral phrases
like `«не знаю точно»` or `"no problem"` are scored as `negative`; `«да, обсудим»` as `positive`.

This does not affect the text pipeline directly, but `SentimentTrendAnalyzer` aggregates daily
sentiment from these scores and `AnalyticsDashboard` surfaces the result — false negative trends
may mislead users. PR #941 (W1020) is open and **not merged**.

---

## Pipeline ordering analysis

**Current `DEFAULT_CHAIN`:** `strip_whitespace → fix_punctuation → normalize_entities`

The documented canonical order in the module docstring (`пробелы → пунктуация → сущности → 
аббревиатуры → анонимизация`) is aspirational, not enforced. The actual chain when a caller
requests `expand_abbreviations` + `anonymize` is caller-defined.

**Ordering correctness (`expand_abbreviations` vs `anonymize`):**
- Safe in either order. `AbbreviationExpander` patterns are word-boundary anchored (`(?<!\w)`);
  PII tokens (phone numbers, emails, card numbers) do not match any built-in abbreviation.
  `TextAnonymizer` placeholders (`[ТЕЛЕФОН]`, `[EMAIL]`) are never expanded by `AbbreviationExpander`
  because they are uppercase bracket strings not matching any abbreviation pattern.

**`NumberNormalizer` and `DateTimeNormalizer` are not pipeline steps.**
These two normalizers are used directly in `engine.py` / `AudioEngine` outside the
`TextPostProcessor` framework. They have no `PostProcessorStep` wrapper and are not in `_BUILTIN_STEPS`.
This is consistent with W1002 finding (pre-existing — not new to this audit).

**`PunctuationFixer` language parameter:**
`FixPunctuation` defaults to `language="ru"` in `_BUILTIN_STEPS`. When a caller specifies
`steps=["fix_punctuation"]` for ES text, the shared singleton applies RU rules (adds `.`, converts
`"я"` → `"Я"`, adds `«»` quotes). There is no auto-language-detect. This is pre-existing (W1101 F4).

---

## Summary table

| # | Finding | Severity | Wave | PR | Status |
|---|---------|----------|------|----|--------|
| F1 | ES date day=0 guard missing in `_normalize_date_es` | HIGH | W1115 | #1027 | OPEN |
| F2 | language_detector FR/TR/PT false positives | HIGH | W1019 | #942 | OPEN |
| F3 | AbbreviationExpander no RLock — concurrent race | MEDIUM | W1119 | #1030 | OPEN |
| F4 | add_abbreviation IPC not wired | MEDIUM | W1118 | #1032 | OPEN |
| F5 | emotion_detector negation/affirmative particles | LOW | W1020 | #941 | OPEN |

**Merged since W1101:** W1081 (ambiguous opt-in, #1001), W1089 (datetime EN/ES anchor, #1000). All 5 findings above require separate fix PRs.
