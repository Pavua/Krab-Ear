# Audit W1083 — DateTimeNormalizer (`core/datetime_normalizer.py`)

**Date:** 2026-05-26  
**Branch:** audit-datetime-normalizer-W1083  
**Auditor:** sub-agent W1083

---

## Scope

Read-only audit of `KrabEar/core/datetime_normalizer.py` (`DateTimeNormalizer`):
locale coverage, ambiguity handling, year defaults, timezone handling, idempotency,
regex DoS risk, wire status, test coverage, and ISO format validity.

---

## Summary

| # | Severity | Finding |
|---|----------|---------|
| F1 | HIGH | EN/ES word-hour false positives — bare numerals converted without context |
| F2 | MED | Partial month-name match corrupts suffixed words (e.g. «майского» → «05.05ского») |
| F3 | MED | «ночи» marker mismatch — 8–11 p.m. stays in 12h form instead of converting to 24h |
| F4 | MED | Output format is `DD.MM.YYYY` not true ISO-8601 (`YYYY-MM-DD`); docstring title misleads |
| F5 | LOW | No timezone awareness — timezone strings silently swallowed (e.g. «9 am UTC» → «09:00») |
| F6 | LOW | `DateTimeNormalizer()` re-instantiated on every transcription call in `engine.py` |
| F7 | LOW | Missing RU month instrumental/adjective forms (`январём`, `майский`, `декабрьских`) |

---

## Wire Status

**Wired in engine.py** (lines 959–970) behind `DATETIME_NORMALIZATION_ENABLED` flag
(default `True`). Called after `NumberNormalizer` in the post-STT normalization pass.

**Not wired in `text_postprocessor.py`** — only `engine.py` calls it directly.

The implementation is lazy-imported per call (`from core.datetime_normalizer import
DateTimeNormalizer`) and a **new instance is created on every transcription** (F6 below).

---

## Findings

### F1 — HIGH: EN and ES word-hour false positives

**File:** `core/datetime_normalizer.py`, `_normalize_time_en` (line 575), `_normalize_time_es` (line 474)

The `word_hour_pat` for English and Spanish matches standalone hour-word tokens
(`one`, `two`, …, `twelve` / `una`, `dos`, …, `doce`) without requiring any time-context
anchor (e.g. «o'clock», «hours», «de la mañana»). The marker group is fully optional.

Observed behaviour:
```
normalize("one more time", "en")      → "01:00 more time"
normalize("two people came", "en")    → "02:00 people came"
normalize("three dogs", "en")         → "03:00 dogs"
normalize("una persona", "es")        → "01:00 persona"
normalize("dos cosas", "es")          → "02:00 cosas"
normalize("tres amigos", "es")        → "03:00 amigos"
```

The RU normalizer is less prone because it requires the word «часов/часа/час» as a mandatory
separator — EN and ES lack this guard.

**Impact:** Every sentence mentioning a small number in English or Spanish transcripts
gets corrupted. The `DATETIME_NORMALIZATION_ENABLED` flag is `True` by default.

**Fix direction:** For EN, require an explicit time marker OR the word «o'clock»/«oclock».
For ES, require «de la»/«y media»/«a las» prefix. Without a disambiguating context
word, bare cardinal numbers must not be converted.

---

### F2 — MED: Partial month-name match corrupts suffixed words

**File:** `core/datetime_normalizer.py`, `_normalize_date_ru` (line 344)

Neither `full_pat` nor `digit_day_pat` adds a word-boundary assertion (`\b` or `(?!\w)`)
after the month-name alternation group. Russian months like «май», «март», «июнь» are
3–4 character prefixes shared with morphologically derived adjectives/genitives.

Observed behaviour:
```
normalize("пятое майского", "ru")    → "05.05ского"   # «май» matched inside «майского»
normalize("5 майского", "ru")        → "05.05ского"
```

The corrupted output `05.05ского` is then idempotent (second pass leaves it unchanged),
so the damage is permanent once written to history.

**Impact:** Low-frequency but produces visually broken transcripts that survive re-runs.

**Fix direction:** Append `(?!\w)` (or `\b`) after `({months_pat})` in both patterns.

---

### F3 — MED: «ночи» marker — late-evening hours stay in 12-hour form

**File:** `core/datetime_normalizer.py`, `_apply_time_marker` (line 220)

The `_RU_TIME_MARKERS` table maps «ночи»/«ночью» → `"night"`.
The `_apply_time_marker` function for `night` keeps the hour unchanged regardless of
its value (lines 232–235: both branches return `hour`). This is correct for
«два часа ночи» (2 AM → 02:00) but wrong for «восемь часов ночи» or «десять часов
ночи», which in Russian idiom mean late evening (colloquially 20:00–22:00):

```
normalize("восемь часов ночи", "ru")   → "08:00"   # should likely be 20:00
normalize("десять часов ночи", "ru")   → "10:00"   # should likely be 22:00
```

Standard Russian usage: «ночи» used with hours 1–5 means AM; used with hours 8–11
it is colloquial for «вечера» (PM). The current implementation treats all «ночи»
as a no-op on the hour value.

**Impact:** MED — affects late-night meeting times in Russian; produces wrong 24h output.

**Fix direction:** In `_apply_time_marker` for `"night"`, add: if `6 <= hour < 12`, convert
to `hour + 12` (i.e. treat 8 ночи as 20:00). Hours 0–5 stay unchanged (true early AM).

---

### F4 — MED: Output format is DD.MM.YYYY, not ISO-8601

**File:** `core/datetime_normalizer.py`, module docstring (line 1) and class docstring (line 248)

The module docstring states «конвертирует словесные даты и время в цифровую форму»
and the task description calls this «ISO-8601 in transcripts». ISO-8601 date format is
`YYYY-MM-DD`; the actual output is `DD.MM.YYYY` (European/Russian convention) and
`HH:MM` (no seconds, no UTC offset, no `T` separator).

```
normalize("15 января 2026 года", "ru")   → "15.01.2026"   # not "2026-01-15"
normalize("девять часов утра", "ru")     → "09:00"        # not "T09:00" or "09:00:00"
```

The format is internally consistent and the tests pass, but callers expecting true
ISO-8601 for downstream parsing (e.g. `CalendarLinker`, export pipelines) will break
silently. The module-level docstring and task spec are misleading.

**Impact:** Documentation mismatch. If any consumer assumes RFC-3339 or ISO-8601 sort
order, results will be wrong (e.g. lexicographic sort of «03.11» ≠ «11.03»).

**Fix direction:** Either (a) rename the output style to «European numeric» in all docs,
or (b) add an `output_format` parameter (`"eu"` | `"iso"`) and document the default.

---

### F5 — LOW: Timezone strings are silently swallowed / ignored

**File:** `core/datetime_normalizer.py`, all `_normalize_time_*` methods

All time normalizations produce naive (no-timezone) `HH:MM` strings. When a timezone
abbreviation follows the time expression, the matched group stops before the timezone
string and it survives in the output:

```
normalize("nine am UTC", "en")           → "09:00 UTC"
normalize("nine am Moscow time", "en")   → "09:00 Moscow time"
```

This is a reasonable fallback (no silent data loss), but there is no documentation
of this limitation, no test for timezone-containing input, and no warning emitted.
Callers that subsequently try to parse `"09:00 UTC"` as a `datetime` or time object
may encounter unexpected parsing failures.

**Impact:** LOW — timezone info is preserved verbatim, not corrupted. But downstream
consumers are unaware that the timezone annotation was not normalized.

**Fix direction:** Add a note in the docstring and a test asserting that timezone strings
are preserved verbatim. Optionally strip well-known TZ abbreviations before/after
conversion and attach them to the output or discard them consistently.

---

### F6 — LOW: `DateTimeNormalizer()` re-instantiated per transcription call

**File:** `KrabEar/core/engine.py`, line 962

```python
from core.datetime_normalizer import DateTimeNormalizer  # lazy
_dt_result = DateTimeNormalizer().normalize(text, language=_norm_lang)
```

`DateTimeNormalizer.__init__` does no expensive setup (no regex compilation in `__init__`;
all patterns are built inline inside each `_normalize_*` method call). However,
the inner methods rebuild `re.escape`-joined pattern strings on **every invocation**:
`months_pat`, `day_ordinals_pat`, `hour_words_pat`, etc. are string-joined and recompiled
each call. The performance benchmark passes (100 RU phrases in <50 ms) but this is
avoidable overhead on high-frequency paths.

**Impact:** LOW — benchmark passes comfortably; this is latency hygiene.

**Fix direction:** Pre-compile all regex patterns as class-level or module-level constants,
or cache compiled regexes in `__init__`. Also cache the instance in `engine.py` (same
pattern as `NumberNormalizer` — check if it is already cached there).

---

### F7 — LOW: Missing Russian month inflection forms (instrumental, adjective)

**File:** `core/datetime_normalizer.py`, `_RU_MONTHS` (line 28)

The `_RU_MONTHS` table covers nominative, genitive, prepositional, and dative.
Missing:
- Instrumental: «январём», «февралём», «мартом», «апрелем», etc.
- Adjectival/compound forms: «майский», «июльского», «декабрьский», etc.

These are infrequent in spoken time expressions but can appear in transcribed speech:
«майскими праздниками», «мартовским утром».

**Impact:** LOW — instrumental forms are rare in date expressions; missing forms are
silently ignored (no bad output, just missed normalization).

**Fix direction:** Add instrumental forms to `_RU_MONTHS`. The adjective forms are
intentionally out of scope (they do not appear in ordinal date contexts).

---

## Test Coverage Assessment

| Area | Tests | Coverage |
|------|-------|----------|
| RU inflected ordinal dates | 9 cases | Good |
| RU time with markers (утра/вечера/дня) | 7 cases | Adequate |
| ES date (digit + ordinal) | 6 cases | Adequate |
| ES time (word hours + markers) | 6 cases | Adequate |
| EN date | 3 cases | Minimal |
| EN time | Present in `test_normalizers.py` | Minimal |
| Idempotency (double-normalize) | 2 cases | Partial |
| False-positive EN/ES word hours | **0 cases** | **Gap (F1)** |
| Partial month-name boundary | **0 cases** | **Gap (F2)** |
| «ночи» with hours 6–11 | **0 cases** | **Gap (F3)** |
| Timezone-containing input | **0 cases** | Gap (F5) |
| Regex DoS on long input | Performance benchmark only | Adequate |

Total: 49 tests pass in `test_datetime_normalizer.py` (Wave 118). No regressions.
All 49 pass; gaps are in areas that were never covered, not regressions.

---

## Idempotency

Idempotency is **correctly implemented** for the main success paths:
- `normalize("15.01.2026", "ru")` → unchanged (numeric guard `(?<!\d)` prevents re-match).
- `normalize("09:00", "ru")` → unchanged (no «часов» keyword to trigger re-match).
- `normalize("третье ноября", "ru")` applied twice → stable.

The `(?<!\d)` lookbehind on the date patterns prevents re-conversion. The one confirmed
idempotency break is the partial-month-suffix bug (F2): `"05.05ского"` is idempotent
(second pass changes nothing) but the output is already corrupt.

---

## Regex DoS Assessment

The year-words pattern contains:
```
r'двух?\s+тысяч(?:и|ного)?\s+(?:\w+\s+)*?\w+ого\s+года?'
```
The `(?:\w+\s+)*?` is a lazy quantifier followed by `\w+ого` which can in theory
cause catastrophic backtracking. Empirically tested at 50 chars → 3 ms, 50 k chars
→ 40 ms — within Python's linear-ish backtracking behaviour for this input shape.
No DoS risk under normal transcript lengths (≤ 8 k chars typical).

---
