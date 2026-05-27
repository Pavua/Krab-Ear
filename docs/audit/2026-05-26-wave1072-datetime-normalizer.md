# Wave 1072 — DateTimeNormalizer Audit

**Date:** 2026-05-26  
**Auditor:** Sub-agent W1072  
**Target:** `KrabEar/core/datetime_normalizer.py`  
**Wire point:** `KrabEar/core/engine.py:962` (lazy import, feature-flagged via `DATETIME_NORMALIZATION_ENABLED`)  
**Tests:** `KrabEar/tests/test_datetime_normalizer.py` — 49 tests, all pass

---

## Summary

`DateTimeNormalizer` is a heuristic regex/lookup normalizer (no heavy NLP deps) covering RU/ES/EN. It is lazily instantiated per transcription in `engine.py` when `datetime_normalization_enabled = True`. All 49 existing unit tests pass. Five actionable bugs found, two informational gaps noted.

---

## Findings

### F1 — MEDIUM: Night-marker ("ночи") bug — hours 6–11 returned as AM

**Location:** `core/datetime_normalizer.py:231–235` (`_apply_time_marker`)

```python
elif marker == "night":
    if hour < 6:
        return hour        # OK: 1–5 ночи = 01:00–05:00
    if hour < 12:
        return hour        # BUG: 6–11 ночи should be 18:00–23:00
    return hour
```

In Russian, "вечера/ночи" for hours 6–11 universally means PM (evening). "Восемь часов ночи" = 20:00, not 08:00. The `< 6` early-return is correct for "small hours" (1–5 am), but the `< 12` branch forgets to apply `+ 12`.

**Observed:** `d.normalize("восемь часов ночи", "ru")` → `"08:00"` (should be `"20:00"`)

**Fix:**
```python
elif marker == "night":
    if hour < 6:
        return hour          # 1–5 am stays
    if hour < 12:
        return hour + 12     # 6–11 "ночи" → PM
    return hour              # 12+ passthrough
```

The same logic applies to `_ES_TIME_MARKERS["noche"]` which is routed through the same `_apply_time_marker`. Spanish "ocho de la noche" (8 pm) exhibits the identical bug.

---

### F2 — MEDIUM: ES double-replacement produces malformed output ("03:00:00")

**Location:** `core/datetime_normalizer.py:530–531` (`_normalize_time_es`)

The ES time normalizer applies two regex substitutions sequentially:
1. `word_hour_pat` — matches bare hour words (e.g. "tres")
2. `alas_pat` — matches `a las N` digit patterns

When input contains "a las tres", pass 1 converts "tres" → "03:00", leaving "a las 03:00". Pass 2 then matches "a las 03" (digits only, no look-ahead for colon), producing "03:00:00".

**Observed:**
```
d.normalize("Llama a las tres", "es")        → "Llama 03:00:00"
d.normalize("a las tres de la tarde", "es")  → "15:00:00"
```

**Root cause:** `alas_pat` uses `(\d{1,2})` which greedily matches "03" from "03:00", then appends `:00` again.

**Fix options:** (a) add `(?!:\d)` negative lookahead after the digit group in `alas_pat`, or (b) apply `alas_pat` first (before word substitution). Option (b) is simpler:
```python
text = re.sub(alas_pat, _make_repl(True), text, flags=re.IGNORECASE)   # digit first
text = re.sub(word_hour_pat, _make_repl(False), text, flags=re.IGNORECASE)
```

---

### F3 — LOW: Missing trailing space after date substitution corrupts adjacent words

**Location:** `core/datetime_normalizer.py:344–410` (`_normalize_date_ru`, `_repl_date`)

The replacement string `f"{day:02d}.{month:02d}"` does not emit a trailing space. When the matched expression ends immediately before a non-space character (e.g., a newline-less word), the output is concatenated without a separator.

**Observed:**
```
d.normalize("первое мая праздник", "ru")  → "01.05праздник"
```

The regex `full_pat` does not consume trailing whitespace, so the space between "мая" and "праздник" is not part of the match and is not included in the replacement. The underlying cause is that `re.sub` replaces only what the pattern captures; the space after "мая" is consumed by group 2's match boundary, not re-emitted.

**Fix:** Capture and re-emit trailing space, or include `\b` / `(?=\s|$)` anchor: the simplest fix is to append a space to the replacement when the original match did not end at a word boundary that includes a space. Alternatively, ensure patterns always include `\s+` lookahead for the trailing token.

---

### F4 — LOW: ES bare hour-word false positives in non-time contexts

**Location:** `core/datetime_normalizer.py:485–489` (`word_hour_pat`)

`word_hour_pat` matches any occurrence of ES hour words (una, dos, tres, cuatro…) without requiring any temporal context marker. Common Spanish words like "tres" (three) or "uno" (one) trigger false substitutions.

**Observed:**
```
d.normalize("los tres amigos", "es")   → "los 03:00 amigos"
d.normalize("son las dos", "es")       → "son 02:00:00"  (+ F2 compounding)
```

**Note:** The RU and EN normalizers also suffer from this — "три часа" will match "три" in "три кота" if "кота" is later in the sentence — but the RU pattern requires "часов|часа" immediately after the hour word, making false positives rare. The ES `word_hour_pat` has no such anchor requirement.

**Fix:** Require at least one of: (a) a preceding "las/la" article, (b) a following temporal marker, or (c) "y media/y cuarto" fractions. Without any temporal context, bare hour words should not be normalised.

---

### F5 — LOW: Regex patterns rebuilt on every `normalize()` call (no caching)

**Location:** `core/datetime_normalizer.py:279–338` (all `_normalize_*` methods)

Every call to `normalize()` → `_normalize_time_ru()` / `_normalize_date_ru()` etc. constructs `hour_words_pat`, `minute_words_pat`, `markers_pat` strings and calls `re.sub()` with uncached pattern objects. Python's `re` module caches the last 512 compiled patterns globally, so repeated identical patterns will hit that cache. However, the pattern strings are very long (join of 20–50 escaped words) and each new `DateTimeNormalizer()` instance in `engine.py` is constructed fresh per transcription call.

**Measured:** 1000 calls take ~57 ms (0.057 ms/call) — acceptable for current load but unnecessary overhead.

**Fix:** Compile patterns once at class or module level (or use `functools.lru_cache` on pattern-building helpers). `re.compile(pattern)` stored as class attributes would eliminate repeated string joins and compilation overhead.

---

## Informational Gaps (not bugs, but worth documenting)

### G1 — Relative date words not supported (undocumented)

"сегодня", "завтра", "вчера" (RU), "hoy", "mañana", "ayer" (ES), "today", "tomorrow", "yesterday" (EN) pass through unchanged. This is consistent with the module docstring's scope ("словесные даты") but is not explicitly listed as a non-goal. Any downstream consumer expecting ISO-8601 output should be aware that relative terms remain as prose.

### G2 — No timezone handling; timezone tokens silently appended to output

Input "в десять часов UTC" produces "10:00 UTC" — the timezone token is left in place after the time replacement. This is acceptable (timezone-aware parsing would require date context), but the extra token may confuse downstream consumers that pattern-match `HH:MM` at end-of-word.

---

## Test Coverage Assessment

| Area | Covered | Notes |
|---|---|---|
| RU inflected dates (all cases) | Yes | 9 tests |
| RU time with/without minutes | Yes | 7 tests |
| ES date ordinal + numeric | Yes | 6 tests |
| ES time word + half | Yes | 6 tests |
| EN date (ordinal + month-first) | Partial | 1 test, `_normalize_date_en` second pattern untested |
| EN time (word + digit+ampm) | Partial | Word form only; `digit_ampm_pat` has no dedicated test |
| Idempotency | Yes | 7 tests |
| Night marker correctness (F1) | No | Not tested; bug present |
| ES double-replacement (F2) | No | Not tested; bug present |
| ES false positives (F4) | No | Not tested |
| Regex caching | N/A | Performance characteristic, no test needed |

**Missing test cases to add:**
- `восемь часов ночи` → `20:00` (would catch F1)
- `a las tres de la tarde` → `15:00` (would catch F2)
- `9 pm` / `11:30 am` (EN digit+ampm pattern)
- `November 15th 2026` (EN month-first pattern)

---

## Recommended Action Priority

| Finding | Priority | Effort |
|---|---|---|
| F1 — Night marker wrong for hours 6–11 | MEDIUM | 1-line fix |
| F2 — ES double-replacement (malformed output) | MEDIUM | Swap sub order or add lookahead |
| F3 — Missing space after date | LOW | Regex boundary fix |
| F4 — ES bare hour-word false positives | LOW | Add context requirement |
| F5 — Regex not cached | LOW | Class-level compile |
