# W1514 Fourth-pass audit: TextPostProcessor pipeline post W1101+W1241

**Date:** 2026-05-27  
**Branch:** `audit-text-postprocessor-fourth-W1514` (off `codex/krab-ear-v2` @ `a767d48d`)  
**Scope:** `core/text_postprocessor.py`, `core/punctuation_fixer.py`,
`core/abbreviation_expander.py`, plus pipeline interaction with recent fixes
(W1376/W1377/W1393/W1418/W1456 merge state verification)

---

## Prior wave merge status

| Wave | PR | Status | Description |
|------|----|--------|-------------|
| W1101 | #1014 | OPEN (docs only) | TextPostProcessor pipeline audit |
| W1241 | #1147 | OPEN (docs only) | Cross-cutting pipeline audit |
| W1376 | #1290 | **MERGED** commit `a9788070` — but **SILENTLY REVERTED** by W916 `cf71f7a4` | Colon symmetry |
| W1377 | #1292 | **MERGED** commit `4857b627` — but **SILENTLY REVERTED** by W916 `cf71f7a4` | Dotted abbreviation fix |
| W1393 | #1297 | **MERGED** commit `8c1ef0b3` (overridden by `ed9078e5`) | ES STT no-space fix |
| W1101 F1 | — | NOT FIXED | Shared singleton lazy-init TOCTOU |
| W1101 F3 | — | NOT FIXED | `steps_applied` ambiguous on failure |
| W1119 | (abbrev) | **MERGED** | AbbreviationExpander RLock |

---

## Key merge state discrepancy

Commits `a9788070` (W1376) and `4857b627` (W1377) appear in `codex/krab-ear-v2` git log but their
changes were **silently overwritten** by a subsequent commit `cf71f7a4` (W916, "PunctuationFixer
English support", 2026-05-26 05:35). W916 was applied on top of the pre-W1376/W1377 base, clobbering
both the colon-symmetry fix and the dotted-abbreviation callback. The diff of `cf71f7a4` explicitly
shows:

```
-_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»]|:(?!/))([^\s\d»\"')\]])")
+_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s\d»\"')\]])")

-_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"\.([А-ЯA-ZЁ])")
-def _insert_period_space(m: re.Match) -> str: ...
+_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")
```

This makes findings F1 and F2 below new regressions introduced by W916.

---

## Findings (5 findings: 2 HIGH · 2 MEDIUM · 1 LOW)

### F1 HIGH — W916 silently reverted W1376 colon-symmetry fix

**File:** `core/punctuation_fixer.py`, `_NO_SPACE_AFTER_PUNCT_RU_RE` (line 19)

**Current state:**
```python
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s\d»\"')\]])")
```

W1376 (PR #1290, commit `a9788070`) added `:(?!/)` to this pattern so that `plan:details` →
`plan: details` without corrupting URLs. W916 (commit `cf71f7a4`, "PunctuationFixer English support")
applied its diff on top of the pre-W1376 base and overwrote the pattern back to the original form
without `:`.

**Reproduction:**
```python
fixer = PunctuationFixer()
fixer.fix("plan:details", "ru")   # returns "Plan:details." — colon NOT spaced
fixer.fix("итог:важно", "ru")     # returns "Итог:важно."  — colon NOT spaced
```

W1376 colon fix is in git history but NOT in the live file. Every RU/ES/EN transcript containing
`keyword:value` STT output (common in technical dictation) leaves the colon unspaced.

**Fix:** restore `:(?!/)` in `_NO_SPACE_AFTER_PUNCT_RU_RE` exactly as W1376 specified:
```python
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»]|:(?!/))([^\s\d»\"')\]])")
```

---

### F2 HIGH — W916 silently reverted W1377 dotted-abbreviation context-aware callback

**File:** `core/punctuation_fixer.py`, `_NO_SPACE_AFTER_PERIOD_RE` (line 20) and `fix()` line 86

**Current state:**
```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")
# ...
result = _NO_SPACE_AFTER_PERIOD_RE.sub(r"\1 \2", result)
```

W1377 (PR #1292, commit `4857b627`) replaced this with a callback `_insert_period_space` that
inspected context to NOT insert a space when the preceding character was preceded by another dot
(abbreviation pattern like `т.е.`) or was a digit (version string). W916 dropped the entire callback.

**Reproduction:**
```python
fixer = PunctuationFixer()
fixer.fix("Dr.Smith", "ru")         # returns "Dr. Smith." — WRONG (abbreviation split)
fixer.fix("v1.0Release", "ru")      # returns "V1.0Release." — ok (digit guard preserved)
fixer.fix("т.е.Важно", "ru")        # returns "Т.е. Важно." — WRONG (abbrev treated as sentence end)
```

Note: `v1.0Release` is incidentally safe because `_NO_SPACE_AFTER_PERIOD_RE` requires `[А-ЯA-ZЁ]`
and the old simple regex fires on `Р` in `Release` only if `_NO_SPACE_AFTER_PERIOD_RE` matches —
the digit before `.R` is not checked. Actually in this case `0R` → digit `0` is not in `[А-ЯA-ZЁ]`
so `0Release` is safe. But `Dr.Smith` → `D` is `[A-Z]` → space inserted incorrectly.

**Fix:** restore the `_insert_period_space` callback from W1377:
```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"\.([А-ЯA-ZЁ])")

def _insert_period_space(m: re.Match) -> str:
    start = m.start()
    text = m.string
    char_before = text[start - 1] if start > 0 else ""
    if char_before.isdigit():
        return m.group(0)
    if char_before and re.match(r"[а-яёА-ЯЁa-zA-Z]", char_before):
        pos_before_char = start - 2
        if pos_before_char >= 0 and text[pos_before_char] == ".":
            return m.group(0)
        if pos_before_char < 0 or text[pos_before_char] in (" ", "\t", "\n", "(", "["):
            return m.group(0)
    return ". " + m.group(1)

# in fix():
result = _NO_SPACE_AFTER_PERIOD_RE.sub(_insert_period_space, result)
```

---

### F3 MEDIUM — W1393 `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` corrupts URLs in ES mode

**File:** `core/punctuation_fixer.py`, `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` (line 26), `_fix_spanish()` (line 125)

W1393 (commit `8c1ef0b3` + `ed9078e5`) added:
```python
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(r"([.!?])([a-záéíóúüñ¿¡])", re.IGNORECASE)
```

This pattern matches ANY period followed by a lowercase ES/EN letter — including URL components:

**Reproduction:**
```python
fixer = PunctuationFixer()
fixer.fix("http://www.ejemplo.com", "es")   # "Http://www. ejemplo. com."  — BROKEN
fixer.fix("ver www.sitio.com para info", "es")  # "Ver www. sitio. com para info."  — BROKEN
fixer.fix("texto.es más fácil", "es")       # "Texto. es más fácil."  — BROKEN (.es TLD)
```

The commit message claims "Excludes abbreviation-style runs (e.g. "e.g.", "U.S.A") by requiring the
character BEFORE the period to be a word character (not already a digit)" — but the actual pattern
`([.!?])([a-záéíóúüñ¿¡])` has NO lookbehind for the character before the period at all. The commit
description is incorrect. URLs contain `://` which means `//` follows the colon, not the period, but
`http://www.ejemplo.com` → `.c` matches the pattern at position 19.

Additionally, the W1376 colon fix added `(?!/)` to protect URLs for the colon case, but W1393 did
not add analogous protection for the period case in ES mode.

**Fix:** add a negative lookbehind for digit/slash or a positive lookbehind requiring a word character
preceded by a space/start:
```python
# Require: char before period is NOT preceded by '/' or digit
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(
    r"(?<!\w\.\w)(?<!\d)([.!?])(?<![:/])([a-záéíóúüñ¿¡])", re.IGNORECASE
)
```
Or preferably, apply only when the preceding word is not part of a URL/domain pattern (check for
`https?://` or `www.` in context, similar to how `AbbreviationExpander` uses `_URL_RE` to skip
protected zones).

---

### F4 MEDIUM — `TextPostProcessor.__init__` shares singleton step instances (W1101 F1 still unresolved)

**File:** `core/text_postprocessor.py`, `__init__` (line 214), `_BUILTIN_STEPS` (line 179)

**Current state:**
```python
self._steps: dict[str, PostProcessorStep] = dict(_BUILTIN_STEPS)
```

`dict(_BUILTIN_STEPS)` copies the dict keys but shares the same object references. All
`TextPostProcessor` instances share the same `FixPunctuation`, `ExpandAbbreviations`, and `Anonymize`
objects. Each of these has an unprotected lazy-init:

```python
def _get_fixer(self):
    if self._fixer is None:           # ← TOCTOU window
        from core.punctuation_fixer import PunctuationFixer
        self._fixer = PunctuationFixer()
    return self._fixer
```

Empirically confirmed via `python3`:
```
p1._steps['fix_punctuation'] is p2._steps['fix_punctuation']  → True
p1._steps['expand_abbreviations'] is p2._steps['expand_abbreviations']  → True
p1._steps['anonymize'] is p2._steps['anonymize']  → True
```

W1101 F1 documented this in the first audit. As of this fourth-pass audit it is **still present** and
**still unresolved**. Since W1119 added `threading.RLock` to `AbbreviationExpander` itself, the
`expand()` call is now safe, but the TOCTOU window in `_get_expander()` / `_get_fixer()` /
`_get_anonymizer()` still exists (two threads can both pass the `is None` check and both construct
collaborators; the second write clobbles the first). The probability is low (lazy init is fast) but
the race is real.

**Fix (one-liner):** construct fresh step instances per `TextPostProcessor.__init__`:
```python
self._steps: dict[str, PostProcessorStep] = {k: type(v)() for k, v in _BUILTIN_STEPS.items()}
```
Note: `ExpandAbbreviations.__init__` takes `language` and `data_dir` params, so a plain `type(v)()`
call would lose those settings. The correct approach is a `clone()` or `copy()` method, or
constructing from spec:
```python
def _make_builtin_steps() -> dict[str, PostProcessorStep]:
    return {
        "strip_whitespace": StripWhitespace(),
        "fix_punctuation": FixPunctuation(language="ru"),
        "expand_abbreviations": ExpandAbbreviations(language="ru"),
        "anonymize": Anonymize(),
        "normalize_entities": NormalizeEntities(),
    }
# ...
self._steps = _make_builtin_steps()
```

---

### F5 LOW — `_CAPITALIZE_AFTER_SENT_RE` fires on RU abbreviation dots (pre-existing, newly confirmed)

**File:** `core/punctuation_fixer.py`, `_CAPITALIZE_AFTER_SENT_RE` (line 35), applied in `fix()` (line 87)

```python
_CAPITALIZE_AFTER_SENT_RE = re.compile(r"([.!?…]\s+)([а-яёa-z])")
```

This pattern fires on ANY period followed by whitespace and a lowercase letter, including abbreviation
dots. Common RU STT output with abbreviations:

**Reproduction:**
```python
fixer = PunctuationFixer()
fixer.fix("т.е. это версия", "ru")         # "Т.е. Это версия."   — WRONG
fixer.fix("т.д. остальные данные", "ru")   # "Т.д. Остальные данные."  — WRONG
```

`т.е.` → period followed by space → `_CAPITALIZE_AFTER_SENT_RE` treats it as a sentence end and
capitalizes `это` → `Это`. The word after a common abbreviation is silently wrongly capitalized in
every transcript containing `т.е.`, `т.д.`, `т.п.`, `напр.`, `др.`, etc.

This is technically pre-existing since v2.0.0, but it was **not listed as a finding** in W1101, W1241,
or any prior audit of `punctuation_fixer.py` through W1446. It is a NEW finding in this audit.
The W1377 revert (F2 above) means the only defense would be the `_insert_period_space` callback
detecting abbreviation context — but that callback was removed by W916. With W1377's callback in
place, `т.е.Важно` would have been left as `т.е.Важно` (no space inserted), so `_CAPITALIZE_AFTER_SENT_RE`
would not fire. With W916's revert, the period space is NOT inserted but the abbreviation dot still
triggers capitalize-after-sent via the existing space.

**Fix:** add an abbreviation-exclusion lookbehind to `_CAPITALIZE_AFTER_SENT_RE`. One approach:
require that the character before the period is NOT a single letter (abbreviation indicator):
```python
# Exclude: single-char abbreviation dot (т.е., т.д.) — "X. " where X is a single letter
_CAPITALIZE_AFTER_SENT_RE = re.compile(
    r"(?<!\b[а-яёa-z])([.!?…]\s+)([а-яёa-z])"
)
```
Or maintain a set of known abbreviation suffixes and skip capitalization when the match falls
inside one.

---

## Pipeline ordering — post W1376/W1377/W1393

The `fix()` method applies rules in this order:
1. `_MULTI_SPACE_RE` — collapse spaces
2. `_SPACE_BEFORE_PUNCT_RE` — strip space before punctuation (includes `:`)
3. `_NO_SPACE_AFTER_PUNCT_RU_RE` — add space after punctuation (currently MISSING `:`)
4. `_NO_SPACE_AFTER_PERIOD_RE` — add space after period+uppercase (currently MISSING callback)
5. `_CAPITALIZE_AFTER_SENT_RE` — capitalize after sentence end
6. Language-specific rules (`_fix_russian` / `_fix_spanish` / `_fix_english`)
7. `_MISSING_PERIOD_RE` — add terminal period

Due to F1 and F2 regressions, steps 3 and 4 are weaker than intended. Due to F3, step 6's ES branch
is more aggressive than intended. Due to F5, step 5 fires on abbreviation dots.

---

## Summary table

| # | Finding | Severity | Root cause | Status |
|---|---------|----------|-----------|--------|
| F1 | W916 reverted W1376 colon fix — `plan:details` not spaced | HIGH | W916 clobbered W1376 | NEW regression |
| F2 | W916 reverted W1377 dotted-abbrev callback — `Dr.Smith` split, `т.е.Важно` split | HIGH | W916 clobbered W1377 | NEW regression |
| F3 | W1393 ES `_NO_SPACE_AFTER_SENT_LOWER_ES_RE` corrupts URLs | MEDIUM | No URL exclusion | NEW (W1393 introduced) |
| F4 | `TextPostProcessor` shared singleton TOCTOU (W1101 F1 unresolved) | MEDIUM | `dict(_BUILTIN_STEPS)` shares objects | Pre-existing, unresolved |
| F5 | `_CAPITALIZE_AFTER_SENT_RE` fires on RU abbreviation dots (т.е., т.д.) | LOW | No abbrev-aware guard | Pre-existing, newly confirmed |

**W1376 and W1377 appear in git log but their code is NOT live** — both were clobbered by W916
(`cf71f7a4`). This is the most critical finding of this audit: two HIGH-priority merged fixes are
silently absent from production.
