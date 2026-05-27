# Wave 1374 — PunctuationFixer Fourth-Pass Audit

**Date:** 2026-05-27
**Auditor:** W1374 sub-agent (fourth pass)
**File:** `KrabEar/core/punctuation_fixer.py`
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)

---

## Merge State of Prior Waves

| Branch | Expected fix | Merged into `codex/krab-ear-v2`? |
|--------|-------------|----------------------------------|
| `fix-punctuation-fixer-order-W1354` | Rule ordering swap (W1348 R1) | **NOT MERGED** |
| `fix-punctuation-rule-order-W1354` | Rule ordering + idempotency + ya-language gate (W1348 R1+R2) | **NOT MERGED** |
| `feature/fix-punctuation-en-W916` | EN language support (W886) | **NOT MERGED** |
| `fix/voice-commands-boundary-W994` | Voice commands lookahead (W989) | **NOT MERGED** |
| `fix-es-punctuation-per-sentence-W1258` | ES per-sentence ¿/¡ prepend (W1250 F1) | **NOT MERGED** |
| `audit/punctuation-fixer-residual-W1250` | W1250 audit docs | **NOT MERGED** |

**All six branches are unmerged.** The production code (`codex/krab-ear-v2`) still carries the W1348 rule-ordering bug.

---

## Findings (5 new, relative to W1354 branch state)

### F1 — HIGH: Colon space asymmetry — space-before-colon removed, but space-after-colon never added

**File:** `KrabEar/core/punctuation_fixer.py`, module-level patterns + `fix()`

`_SPACE_BEFORE_PUNCT_RE` (line 17) includes the colon character (`:`) in its character class `[,.:;!?»]`, removing any space before a colon. However, `:` is **not present** in `_NO_SPACE_AFTER_PUNCT_RU_RE` (line 20, first capture group `[,;!?»]`). The two patterns are asymmetric:

```
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.:;!?»])")        # colon included ✓
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s...])")  # colon ABSENT ✗
```

**Observed behavior:**
```python
fixer.fix("план :первый", "ru")   # → "План:первый."   WRONG (should be "План: первый.")
fixer.fix("это:дело", "ru")       # → "Это:дело."      NOT FIXED
```

Removing the space before `:` via `_SPACE_BEFORE_PUNCT_RE` collapses `"план :первый"` to `"план:первый"`, but the resulting colon-without-space-after is never repaired because colon is absent from the add-space-after rule. This is a write-only remove with no complementary add.

**Fix:** Add `:` to the first capture group of `_NO_SPACE_AFTER_PUNCT_RU_RE`:
```python
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,:;!?»])([^\s\d»\"')\]])")
```

**Severity:** HIGH (production corruption; input with space-before-colon produces worse output than the input)

---

### F2 — MED: `_NO_SPACE_AFTER_PERIOD_RE` fires on dotted abbreviations and version strings

**File:** `KrabEar/core/punctuation_fixer.py`, line 21

```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")
```

The pattern matches any period followed by any uppercase RU or EN letter. This fires correctly on sentence boundaries (`.M` → `. M`) but also incorrectly on:

- Abbreviations where the next word begins with uppercase: `т.е.Правда` → `Т.е. Правда.`
- Mixed-script dotted names: `США.Войска` → `США. Войска.`
- Version strings like `v1.0.Beta` → `V1.0. Beta.` (splits the dotted version segment)
- Multi-part abbreviations: `i.e.Something` → `I.e. Something.`

**Observed behavior:**
```python
fixer.fix("т.е. это важно", "ru")  # → "Т.е. Это важно."  (Это incorrectly capitalized)
fixer.fix("США.Войска воевали", "ru")  # → "США. Войска воевали."
fixer.fix("v1.0.Beta выпуск", "ru")  # → "V1.0. Beta выпуск."
```

The abbreviation case `т.е. это` is doubly wrong: the period after `е` triggers `_CAPITALIZE_AFTER_SENT_RE` capitalizing `Это`, making a mid-sentence word a false sentence start.

**Fix:** Add a negative lookbehind that skips if the period is immediately preceded by a single letter (abbreviation dot), e.g.:
```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(?<!\b[А-ЯA-ZЁа-яёa-z])(\.)([А-ЯA-ZЁ])")
```
or restrict to periods preceded by a word character with ≥2 chars before.

**Severity:** MED (corrupts legitimate abbreviations; pre-existing bug not introduced by W1354)

---

### F3 — MED: `get_fixes_applied()` does not detect space-after-punctuation corrections

**File:** `KrabEar/core/punctuation_fixer.py`, `get_fixes_applied()` method (lines ~128–171)

`get_fixes_applied()` detects: double spaces, space-before-punctuation, missing period, first-letter capitalization, standalone `я`, ASCII quotes, capitalize-after-sentence, and `¿`/`¡` prepend.

It does **not** detect corrections made by `_NO_SPACE_AFTER_PUNCT_RU_RE` (space added after `,;!?»`) or `_NO_SPACE_AFTER_PERIOD_RE`.

**Observed behavior:**
```python
orig = "это,дело"
fixed = fixer.fix(orig, "ru")  # → "Это, дело."
fixer.get_fixes_applied(orig, fixed)
# → ['added missing period', 'capitalized first letter']
# NOTE: "added space after comma" is MISSING from the report
```

The diagnostic output is incomplete. When a caller uses `get_fixes_applied()` to audit changes (e.g., for logging breadcrumbs or audit trail), space-after corrections are silently omitted. This affects 5 of the fix types: `,`, `;`, `!`, `?`, `»`, and `.` followed by uppercase.

**Fix:** Add detection for `_NO_SPACE_AFTER_PUNCT_RU_RE` and `_NO_SPACE_AFTER_PERIOD_RE`:
```python
if _NO_SPACE_AFTER_PUNCT_RU_RE.search(original):
    fixes.append("added space after punctuation")
if _NO_SPACE_AFTER_PERIOD_RE.search(original):
    fixes.append("added space after period")
```

**Severity:** MED (silent omission in audit/diagnostic output; does not corrupt text)

---

### F4 — MED: ES `_fix_spanish()` applies `¿`/`¡` to whole text, not per-sentence (W1258 partially unresolved)

**File:** `KrabEar/core/punctuation_fixer.py`, `_fix_spanish()` (lines ~107–119)

W1258 was filed to fix per-sentence `¿`/`¡` prepending. The branch `fix-es-punctuation-per-sentence-W1258` is **not merged**. More importantly, even on the W1258 branch the `_fix_spanish()` method still checks the **last character of the whole `result` string** and prepends to the **start of the whole string**:

```python
if result.rstrip().endswith("?") and not result.lstrip().startswith("¿"):
    result = "¿" + result.lstrip()
```

For multi-sentence ES input, this prepends `¿` before the first sentence, not before the question sentence:

**Observed behavior:**
```python
fixer.fix("dime. como te llamas?", "es")
# → "¿Dime. Como te llamas?"   WRONG
# Expected: "Dime. ¿Como te llamas?"
```

Single-sentence case is correct (`¿Como te llamas?`). The problem only manifests when a non-question sentence precedes the question. This is an STT-realistic scenario (e.g., a greeting followed by a question).

**Fix:** Iterate per sentence in `_fix_spanish()` using a sentence splitter or at minimum check if the `?`-bearing segment starts after a `. `:
```python
sentences = re.split(r"(?<=[.!?])\s+", result)
# Apply ¿/¡ per sentence, then rejoin
```

**Severity:** MED (wrong output for multi-sentence ES — not a regression from W1354, but W1258 did not land)

---

### F5 — LOW: `_NO_SPACE_AFTER_PERIOD_RE` misses period + lowercase letter (W1354 partial fix gap)

**File:** `KrabEar/core/punctuation_fixer.py`, line 21 + `fix()` method

The W1354 fix correctly swaps rule order so that "add-space-after" runs before "remove-space-before". However, `_NO_SPACE_AFTER_PERIOD_RE` only matches period followed by **uppercase** letters (`[А-ЯA-ZЁ]`). When a period has a spurious space before it and the following word starts **lowercase**, removing the space-before leaves a period directly adjacent to a lowercase word — and `_NO_SPACE_AFTER_PERIOD_RE` never fires to add the missing space:

```
Input:  "текст .продолжение"
  step A: _NO_SPACE_AFTER_PERIOD_RE: pattern needs (\.)([А-ЯA-ZЁ])
             "текст .продолжение" — 'п' is lowercase → NO MATCH
  step B: _SPACE_BEFORE_PUNCT_RE removes the space: "текст.продолжение"
  result: "Текст.продолжение."   WRONG — period missing space after
```

This is specifically a gap in the W1354 fix: the fix is effective for `. UpperCase` cases (because A fires before B), but not for `. lowercase` cases (because A never fires regardless of order).

**Observed behavior:**
```python
# On codex/krab-ear-v2 (W1354 NOT merged, buggy order):
fixer.fix("текст .продолжение", "ru")  # → "Текст.продолжение."

# With W1354 order applied (still wrong for lowercase):
# After step A (add-space): no change (lowercase 'п' not matched)
# After step B (remove-space): "текст.продолжение"  → final: "Текст.продолжение."
```

**Fix:** Extend `_NO_SPACE_AFTER_PERIOD_RE` to also match lowercase letters, then filter out decimal numbers and abbreviations:
```python
# Current (too narrow):
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")

# Proposed (catch lowercase too, exclude digits for decimals):
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁа-яёa-z])")
# Then add guard: skip if period preceded by single letter (abbreviation)
```
(Requires coordination with F2 fix to avoid over-matching.)

**Severity:** LOW (niche edge case: period + space + lowercase; W1354 fix is correct for most cases)

---

## Idempotency Status (post-W1354 branch)

All tested inputs on the `fix-punctuation-rule-order-W1354` branch are idempotent (pass1 == pass2 == pass3) including: double-space collapse, comma/semicolon space, `¿`/`¡` adding, ASCII quote conversion, standalone `я`, and missing period. No new idempotency regressions introduced by W1354.

## Performance

No changes to precompile patterns. All patterns remain module-level constants. No performance concerns.

## Test Coverage Gaps

The existing `test_punctuation_fixer.py` (284 lines, 5 test classes, ~25 cases) does not cover:

- Colon asymmetry (`план :первый` → `План:первый.`) — **F1**
- Dotted abbreviations (`т.е. это`, `США.Войска`) — **F2**
- `get_fixes_applied()` missing space-after fix type — **F3**
- Multi-sentence ES `¿` placement — **F4**
- Period + lowercase space gap — **F5**

---

## Action Items

| Finding | Severity | Fix location | Effort |
|---------|----------|-------------|--------|
| F1: Colon in space-after rule | HIGH | `_NO_SPACE_AFTER_PUNCT_RU_RE` pattern | 1 line |
| F2: Dotted abbreviation corruption | MED | `_NO_SPACE_AFTER_PERIOD_RE` pattern | 1-2 lines |
| F3: get_fixes_applied missing space-after | MED | `get_fixes_applied()` body | 4 lines |
| F4: ES multi-sentence ¿/¡ (W1258 not merged) | MED | `_fix_spanish()` | 8-12 lines |
| F5: Period + lowercase gap in W1354 | LOW | `_NO_SPACE_AFTER_PERIOD_RE` pattern | coordinate with F2 |

**Priority:** Merge W1354 first (addresses W1348 ordering), then address F1 (colon), then F2+F5 together (period pattern), then F3 (diagnostic), then F4 (ES multi-sentence).
