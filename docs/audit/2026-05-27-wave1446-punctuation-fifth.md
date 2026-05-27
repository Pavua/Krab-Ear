# Wave 1446 — PunctuationFixer Fifth-Pass Audit

**Date:** 2026-05-27
**Auditor:** W1446 sub-agent (fifth pass)
**File:** `KrabEar/core/punctuation_fixer.py`
**Branch audited:** `codex/krab-ear-v2` (HEAD `4eb8356f`)

---

## Merge State of Prior Waves

| Wave | Branch | Fix description | Merged into `codex/krab-ear-v2`? |
|------|--------|-----------------|----------------------------------|
| W916 | `feature/fix-punctuation-en-W916` | EN language support | **NOT MERGED** |
| W1258 | `fix-es-punctuation-per-sentence-W1258` | ES per-sentence ¿/¡ prepend | **MERGED** (#1161) |
| W1354 | `fix-punctuation-rule-order-W1354` | Rule ordering: add-space before remove-space | **MERGED** (#1264) |
| W1376 | `fix-punctuation-colon-W1376` | Colon space asymmetry (W1374 F1 HIGH) | **NOT MERGED** |
| W1377 | `fix-punctuation-dotted-abbrev-W1377` | Dotted abbreviation + version exemption (W1374 F2 MED) | **NOT MERGED** |
| W1393 | `fix-punctuation-es-per-sentence-W1393` | ES STT no-space: period before lowercase (W1374 F4 MED) | **MERGED** (#1297) |

**Summary:** W1258, W1354, W1393 are merged. W1376 (colon) and W1377 (dotted abbrev) remain unmerged.

---

## Findings (5 new, relative to W1393 merged state)

### F1 — MED: W1393 ES no-space fix creates capitalization gap for post-period sentence starts

**File:** `KrabEar/core/punctuation_fixer.py`, `fix()` pipeline order + `_fix_spanish()`

W1393 added `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` inside `_fix_spanish()` (line 119) which inserts a
space between a period and a lowercase ES/EN word: `"dime.como"` → `"dime. como"`. However,
`_CAPITALIZE_AFTER_SENT_RE` runs in the **common section** of `fix()` (line 83), **before**
`_fix_spanish()` is called (line 87). By the time W1393's space insertion runs, the common
capitalize rule has already finished and will not run again.

As a result, the newly-introduced sentence boundary (`". como"`) is never capitalized:

```python
fixer.fix('dime.como te llamas?', 'es')
# → 'Dime. ¿como te llamas.'    WRONG — 'como' lowercase after sentence boundary
#
# Compare with pre-spaced input (cap rule fires in common section):
fixer.fix('dime. como te llamas?', 'es')
# → 'Dime. ¿Como te llamas.'    CORRECT — '. c' caught by _CAPITALIZE_AFTER_SENT_RE
```

**Root cause:** `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` runs *after* `_CAPITALIZE_AFTER_SENT_RE`
because it is in `_fix_spanish()`, not in the common section. The common caps rule only sees
`.c` (no space), which does not match its pattern `([.!?…]\s+)([а-яёa-z])`.

**Fix:** Apply `_CAPITALIZE_AFTER_SENT_RE` a second time inside `_fix_spanish()` after the
no-space substitution, or move the capitalization into `_apply_inverted_markers_per_sentence`
so it occurs after spacing is normalized:

```python
def _fix_spanish(self, text: str) -> str:
    result = _NO_SPACE_AFTER_SENT_LOWER_ES_RE.sub(r'\1 \2', result)
    # Re-apply sentence-start caps now that spaces are inserted
    result = _CAPITALIZE_AFTER_SENT_RE.sub(lambda m: m.group(1) + m.group(2).upper(), result)
    ...
```

**Severity:** MED — silently produces lowercase sentence starts in realistic Whisper output
(`"dime.como te llamas?"` is a canonical STT artifact per the W1393 commit message).

---

### F2 — HIGH: Colon space asymmetry still unmerged (W1376 NOT in `codex/krab-ear-v2`)

**File:** `KrabEar/core/punctuation_fixer.py`, `_NO_SPACE_AFTER_PUNCT_RU_RE`

W1376 added `:` to the space-after-punctuation rule (with a URL exclusion `(?!/)`), but the
branch remains unmerged. In production, colon is stripped of its preceding space by
`_SPACE_BEFORE_PUNCT_RE` (colon IS in `[,.:;!?»]`) but never given a following space (colon
is NOT in `_NO_SPACE_AFTER_PUNCT_RU_RE`'s capture group `[,;!?»]`):

```python
fixer.fix('план :первый шаг', 'ru')
# → 'План:первый шаг.'    WRONG — space removed, no space added after colon
# Expected: 'План: первый шаг.'
```

This is production-corrupting: input with `space-colon` is made worse, not better.

**Status:** W1374 F1 (HIGH). Unblocked — fix is on branch `fix-punctuation-colon-W1376`.

**Note on W1376 URL exclusion scope:** W1376's fix uses `(?!/)` (colon not followed by `/`)
to exclude `http://` and `https://`. This correctly handles double-slash protocols but would
still insert space in `mailto:user` or `data:image` patterns. These are uncommon in STT output
but should be documented as an accepted limitation when the branch is merged.

**Severity:** HIGH (unmerged since W1374, production regression for colon-prefixed text).

---

### F3 — MED: Dotted abbreviation corruption still unmerged (W1377 NOT in `codex/krab-ear-v2`)

**File:** `KrabEar/core/punctuation_fixer.py`, `_NO_SPACE_AFTER_PERIOD_RE`

W1377 added a callback `_insert_period_space` to exempt dotted abbreviations and version
strings from period-space insertion, but the branch remains unmerged. In production, the
simple `re.compile(r"(\.)([А-ЯA-ZЁ])")` pattern still fires on mid-word abbreviation dots:

```python
fixer.fix('т.е. это важно', 'ru')
# → 'Т.е. Это важно.'    WRONG — 'Это' spuriously capitalized (false sentence start)
# Expected: 'Т.е. это важно.'

fixer.fix('v1.0.Beta выпуск', 'ru')
# → 'V1.0. Beta выпуск.'    WRONG — version segment split
# Expected: 'V1.0.Beta выпуск.' or 'V1.0. Beta выпуск.' (acceptable either way)
```

The abbreviation case (`т.е. Это`) is particularly damaging: the spurious capitalize triggers
`_CAPITALIZE_AFTER_SENT_RE`, making `Это` look like a sentence start to downstream consumers.

**Status:** W1374 F2 (MED). Unblocked — fix is on branch `fix-punctuation-dotted-abbrev-W1377`.

**Severity:** MED (corrupts legitimate abbreviations; pre-existing in production since before all prior audit waves).

---

### F4 — LOW: `get_fixes_applied()` does not detect space-after-punctuation corrections

**File:** `KrabEar/core/punctuation_fixer.py`, `get_fixes_applied()` (lines 171–219)

`get_fixes_applied()` tracks corrections for: double spaces, space-before-punctuation, missing
period, first-letter capitalization, standalone `я`, ASCII→«» quotes, post-sentence capitals,
and `¿`/`¡` prepend. It does **not** detect corrections made by `_NO_SPACE_AFTER_PUNCT_RU_RE`
(space inserted after `,;!?»`) or `_NO_SPACE_AFTER_PERIOD_RE` (space after period+uppercase).

```python
orig = 'это,дело'
fixed = fixer.fix(orig, 'ru')       # 'Это, дело.'
fixer.get_fixes_applied(orig, fixed)
# → ['added missing period', 'capitalized first letter']
# MISSING: 'added space after punctuation'
```

This affects audit logging, breadcrumb generation, and any caller that uses `get_fixes_applied()`
for diagnostic output. The space-after correction is the most commonly triggered fix in real
STT output yet is silently omitted.

**Fix:**
```python
if _NO_SPACE_AFTER_PUNCT_RU_RE.search(original):
    fixes.append("added space after punctuation")
if _NO_SPACE_AFTER_PERIOD_RE.search(original):
    fixes.append("added space after period")
```

**Status:** W1374 F3 (MED), unaddressed in all prior waves.

**Severity:** LOW (diagnostic omission only; no text corruption).

---

### F5 — LOW: `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` includes `¿¡` in the letter character class

**File:** `KrabEar/core/punctuation_fixer.py`, line 26

```python
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(r"([.!?])([a-záéíóúüñ¿¡])", re.IGNORECASE)
```

The pattern includes `¿` and `¡` in the second capture group alongside lowercase letters.
The module comment on lines 22–25 describes this pattern as matching "a lowercase ES/EN
letter" (`dime.como`), but `¿` and `¡` are inverted punctuation marks, not letters.

The undocumented side effect: the pattern also fires on adjacent markers like `?¿` and `!¡`
(e.g., `hola?¿bien?`), inserting a space between them. This happens to produce correct output
(`hola? ¿bien?`) but is an accidental behaviour not covered by the pattern comment or tests:

```python
# 'hola?¿bien?' — adjacent question markers, no space
fixer.fix('hola?¿bien?', 'es')
# → '¿Hola? ¿bien?'   (correct output, but ¿ insertion via undocumented ¿ in class)
```

If a future maintainer removes `¿¡` from the class to "fix" the comment mismatch, the
adjacent-marker spacing would silently regress. There are no tests for this case.

**Fix:** Either update the comment to document the `¿¡` intent explicitly, or extract
adjacent-marker handling into a separate rule with its own test:

```python
# Match period/!? before a lowercase ES/EN letter OR before an inverted Spanish marker
# (handles Whisper no-space: "dime.como" and adjacent markers: "hola?¿bien?")
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(
    r"([.!?])([a-záéíóúüñ]|(?=[¿¡]))", re.IGNORECASE
)
```

Or simply update the comment:
```python
# STT-no-space: period/!? followed by lowercase ES/EN letter OR inverted Spanish marker.
```

**Severity:** LOW (no current bug; maintainability / test coverage gap).

---

## Idempotency Status

All tested inputs on the current `codex/krab-ear-v2` HEAD are idempotent (pass1 == pass2):

| Input | Language | Pass1 | Pass2 | Idempotent |
|-------|----------|-------|-------|------------|
| `dime.como te llamas?` | es | `Dime. ¿como te llamas.` | same | YES |
| `¿Como te llamas?` | es | `¿Como te llamas?` | same | YES |
| `т.е. это важно` | ru | `Т.е. Это важно.` | same | YES |
| `план :первый` | ru | `План:первый.` | same | YES |
| `hola?¿bien?` | es | `¿Hola? ¿bien?` | same | YES |

No idempotency regressions introduced by W1258, W1354, or W1393.

---

## Interaction Analysis (W1376 + W1377 + W1393 combined)

When W1376 and W1377 eventually merge with the current W1393 code:

- **W1376 × W1393:** Independent rules (colon in RU vs. period+lowercase in ES). No conflict.
- **W1377 × W1393:** Independent rules (`_NO_SPACE_AFTER_PERIOD_RE` for RU uppercase vs. `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` for ES lowercase). No conflict.
- **W1376 × W1377:** Both modify period/colon handling in the common section. W1377 changes `_NO_SPACE_AFTER_PERIOD_RE` to use a callback; W1376 changes `_NO_SPACE_AFTER_PUNCT_RU_RE` to include colon. These are different regex objects. No conflict.
- **W1376 URL note:** W1376's `(?!/)` exclusion misses `mailto:`, `data:`, `ws:` patterns. Acceptable limitation for STT context (these URI schemes are very rare in transcribed speech).

---

## Action Items

| Finding | Severity | Fix location | Effort | Priority |
|---------|----------|-------------|--------|----------|
| F1: ES caps gap after W1393 space insertion | MED | Re-apply `_CAPITALIZE_AFTER_SENT_RE` in `_fix_spanish()` after no-space sub | 3 lines | Medium |
| F2: Colon asymmetry (W1376 unmerged) | HIGH | Merge `fix-punctuation-colon-W1376` | 1 PR merge | High |
| F3: Dotted abbreviation (W1377 unmerged) | MED | Merge `fix-punctuation-dotted-abbrev-W1377` | 1 PR merge | Medium |
| F4: `get_fixes_applied()` missing space-after | LOW | Add 2 detection checks in `get_fixes_applied()` | 4 lines | Low |
| F5: `¿¡` in ES pattern char class undocumented | LOW | Update comment or extract rule | 1-3 lines | Low |

**Priority order:** Merge W1376 first (F2, HIGH), then fix F1 (ES caps gap, introduced by W1393), then merge W1377 (F3), then F4+F5 together (LOW cosmetic).
