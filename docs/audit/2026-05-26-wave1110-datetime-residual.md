# Audit W1110 — DateTimeNormalizer Residual Re-audit (`core/datetime_normalizer.py`)

**Date:** 2026-05-26
**Branch:** audit-datetime-residual-W1110
**Auditor:** sub-agent W1110
**Prior audit:** W1083 (7 findings), W1072 (5 findings)
**Post-fix commits referenced:** W1089 (fix F1+F2+F3), W1094 (fix F4)

---

## Merge Status of W1089 and W1094

Both fix commits are **NOT yet merged** into `codex/krab-ear-v2`:

| Wave | Commit | Branch | Status |
|------|--------|--------|--------|
| W1089 | `d41ed2b7` | `origin/fix-datetime-normalizer-W1089` | NOT merged |
| W1094 | `ee04ffff` | `origin/fix-datetime-iso-W1094` | NOT merged |

Confirmed via `git merge-base --is-ancestor <sha> codex/krab-ear-v2` returning false for both.

All 7 original W1083 findings (F1–F7) remain active on `codex/krab-ear-v2`.

---

## Summary

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| F1 | HIGH | EN/ES word-hour false positives — bare numerals converted without context | UNFIXED (W1089 pending merge) |
| F2 | MED | RU month boundary — no `(?!\w)` guard after month alternation | UNFIXED (W1089 pending merge) |
| F3 | MED | `night` marker — hours 6–11 not converted to +12 | UNFIXED (W1089 pending merge) |
| F4 | MED | Output format is `DD.MM.YYYY`, not ISO-8601 as documented | UNFIXED (W1094 pending merge) |
| F5 | LOW | Timezone strings silently preserved verbatim, no documentation | UNFIXED |
| F6 | LOW | `DateTimeNormalizer()` re-instantiated per call + regex rebuilt per call | UNFIXED |
| F7 | LOW | Missing RU instrumental month forms (`январём`, `мартом`) | UNFIXED |
| **F8** | **MED** | **Separator space consumed by optional year group in combined date+time (RU)** | **NEW** |
| **F9** | **HIGH** | **`_normalize_date_es` missing `1 <= day <= 31` guard — F1 side-effect produces `03:00.11`** | **NEW** |

---

## W1094 Downstream Regression Risk Assessment

**Risk: LOW — no regression.**

W1094 changes the default output format from `DD.MM.YYYY` to `YYYY-MM-DD`. Callers that
parse timestamps use the `timestamp` metadata field (ISO-8601 RFC-3339) from history items —
not the normalized transcript text. Verified in:

- `KrabEar/core/auto_title.py:248–257` — parses `timestamp` field with ISO formats only
- `KrabEar/core/transcript_context.py:72–78` — parses `timestamp` field with ISO formats only
- `KrabEar/core/auto_glossary.py:158` — same pattern

No caller parses the `DD.MM.YYYY` string embedded in transcript text. The normalized text is
stored as human-readable display text, not a machine-parseable date field.

The `DD.MM.YYYY` → `YYYY-MM-DD` format change is safe to merge. It improves lexicographic
sort correctness for any consumer that sorts by the in-transcript date string.

---

## New Findings

### F8 — MED: Separator space consumed in combined date+time RU expressions

**File:** `KrabEar/core/datetime_normalizer.py`, `_normalize_date_ru` (line 364)

When a RU date and time expression appear in the same sentence, the year-words optional
group in `full_pat` greedily consumes the trailing space after the month word, causing
the date and time tokens to be concatenated without a separator in the output.

**Root cause:** The pattern ends with `rf"(?:\s+({year_words_pat}))?"`.
When no year follows the month, `year_words_pat` matches an empty string `""`, but the
outer `(?:\s+...)?` group — which requires `\s+` before the year alternation — still
matches the trailing space when `year_words_pat` is anchored to an empty match. Result:
the trailing space is consumed by the match but absent from the replacement string.

**Observed behaviour:**
```python
normalize("встреча третьего ноября в девять часов утра", "ru")
# → "встреча 03.1109:00"   # missing space between date and time
# expected: "встреча 03.11 09:00"
```

Processing trace:
1. `_normalize_time_ru` converts `"в девять часов утра"` → `"09:00"`, keeping leading space: `"встреча третьего ноября 09:00"`
2. `_normalize_date_ru` matches `"третьего ноября "` (including trailing space via year group) → `"03.11"` (no trailing space)
3. Result: `"встреча 03.1109:00"` — space lost

**Impact:** MED — any sentence containing both a date and a later time expression in Russian
produces fused output (`DD.MM HH:MM` → `DD.MMHH:MM`). Affects meeting-time transcripts which
are a primary use case.

**Fix direction:** Remove `\s+` from the year group's outer non-capturing group, or
add a trailing `(?:\s|$)` check, or ensure the replacement appends a space when a following
time expression is detected. Simplest fix: change `rf"(?:\s+({year_words_pat}))?"` to
`rf"(?:\s+({year_words_pat})\s*)?"` and trim in the replacement callback.

---

### F9 — HIGH: `_normalize_date_es` missing `1 <= day <= 31` range guard

**File:** `KrabEar/core/datetime_normalizer.py`, `_normalize_date_es` (line 534)

The Spanish date replacement callback (`_repl_date` in `_normalize_date_es`) converts any
matched integer day value without validating that `1 <= day <= 31`. The Russian and English
equivalents both have this guard; the Spanish version is missing it.

**Combined effect with F1 (ES word-hour false positive):**

1. F1 converts `"tres"` (Spanish word for "three") to `"03:00"` via `_normalize_time_es`.
2. The resulting string `"03:00 de noviembre"` then hits `_normalize_date_es`.
3. The date pattern `(\d{1,2})\s+de\s+(month)` matches `"00 de noviembre"` (the `:00` colon
   is not a digit, so `(?<!\d)` lookbehind passes; `"00"` is matched as a 2-digit day).
4. Without the `1 <= 0 <= 31` guard: `day=0`, `month=11` → `f"{0:02d}.{11:02d}"` = `"00.11"`
5. The `"03:"` prefix from step 1 + `"00.11"` = `"03:00.11"` — a permanently corrupt string.

**Observed behaviour:**
```python
normalize("tres de noviembre", "es")
# → "03:00.11"   # catastrophic double-corruption
# expected: "tres de noviembre" (unchanged) or "03.11" (if F1 were fixed)
```

With F1 fixed (W1089) the `"tres"` bare cardinal would no longer convert, making F9 a
separate code path. However, F9 is independently reachable: any Spanish date where a
2-digit number lands at position 0 (e.g. `"00 de diciembre"` from a malformed upstream
string) would produce `00.12` without a range guard.

**Severity:** HIGH — produces a permanently unrecoverable `03:00.11` string in history
for any ES transcript containing a day-of-month word followed by a month name.

**Fix direction:** Add `if not (1 <= day <= 31): return m.group(0)` after `day = int(day_str)`
in `_normalize_date_es._repl_date`, identical to the guard in `_normalize_date_ru._repl_digit_date`.

---

## W1089 Fix Verification (on fix branch, not on codex/krab-ear-v2)

The W1089 commit (`d41ed2b7`) correctly addresses F1, F2, and F3:

- **F1 (EN/ES anchors):** `word_hour_pat` now requires `o'clock`, am/pm, or a time marker as
  a mandatory anchor. Bare cardinals `"two people"`, `"una persona"` no longer convert.
- **F2 (RU month boundary):** `(?!\w)` appended after `({months_pat})` in both `full_pat`
  and `digit_day_pat`. Forms like `"майского"` no longer corrupt.
- **F3 (night marker):** `_apply_time_marker("night")` now adds 12 for hours 6–11:
  `"восемь часов ночи"` → `"20:00"` instead of `"08:00"`.

These fixes do not address F8 or F9 — those remain residual after W1089 merge.

---

## W1094 Fix Verification (on fix branch, not on codex/krab-ear-v2)

The W1094 commit (`ee04ffff`) correctly addresses F4:

- Adds module constant `DATETIME_OUTPUT_FORMAT: Literal["iso8601", "european"] = "iso8601"`.
- Adds `DateTimeNormalizer(output_format=...)` constructor parameter.
- Adds `_fmt_date(day, month, year)` helper that routes to ISO or European format.
- All three `_normalize_date_{ru,es,en}` methods route through `_fmt_date`.
- Default changed from European to ISO-8601: `"15.01.2026"` → `"2026-01-15"`.
- Legacy callers opt in via `output_format="european"`.

W1094 does not touch time formatting (`HH:MM` stays unchanged — no ISO `T09:00` prefix).
This is documented in the commit message; time format change is out of scope for W1094.

---

## Interaction Analysis: W1089 + W1094 Sequential Merge

If W1089 is merged first (fixes F1/F2/F3), then W1094 (fixes F4):

1. F9 is NOT fixed by either wave — it requires a separate fix.
2. F8 is NOT fixed by either wave — it requires a separate fix.
3. After W1089, F1 is fixed: `"tres de noviembre"` no longer converts `"tres"` to `"03:00"`.
   F9's observable double-corruption symptom disappears, but the missing guard is still
   a latent defect reachable by other code paths.
4. After W1094, all date outputs switch to ISO `YYYY-MM-DD`. The F8 separator loss
   (`"встреча 2026-11-03T09:00"` — date and time token fused) remains unfixed.

---

## Recommendations

Priority order for remaining unfixed items:

| Priority | Finding | Action |
|----------|---------|--------|
| CRITICAL | W1089 + W1094 merge | Merge both branches to `codex/krab-ear-v2` immediately — blocked by PR process only |
| HIGH | F9 — ES day range guard | Add `if not (1 <= day <= 31): return m.group(0)` in `_normalize_date_es._repl_date` |
| MED | F8 — RU separator loss | Fix year_words_pat optional group to not consume trailing space |
| LOW | F5 — timezone docs | Add docstring note + test asserting TZ strings preserved verbatim |
| LOW | F6 — instance caching | Cache `DateTimeNormalizer` instance in `engine.py`; pre-compile class-level regexes |
| LOW | F7 — instrumental forms | Add RU instrumental month forms to `_RU_MONTHS` dict |
