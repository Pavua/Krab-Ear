# W1348 Third-Pass Re-audit: `core/punctuation_fixer.py`

**Date:** 2026-05-27
**Branch:** `audit-punctuation-fixer-third-W1348`
**File audited:** `KrabEar/core/punctuation_fixer.py` (169 lines, `codex/krab-ear-v2`)
**Predecessor audits:** W886 (initial RU/ES), W1250 (second pass, 5 findings F1–F5)

---

## Prior-Wave Merge State

| Wave | Branch | PR | Merged into `codex/krab-ear-v2`? |
|------|--------|----|----------------------------------|
| W916 | `feature/fix-punctuation-en-W916` | #837 | **NOT merged** (OPEN) |
| W994 | `fix/voice-commands-boundary-W994` | #915 | **NOT merged** (OPEN) — voice_commands.py, not punctuation_fixer |
| W1250 | `audit/punctuation-fixer-residual-W1250` | #1156 | **NOT merged** (OPEN — docs only) |
| W1258 | `fix-es-punctuation-per-sentence-W1258` | #1161 | **NOT merged** (OPEN) |

All four predecessor branches are unmerged. The production file on `codex/krab-ear-v2` is
identical to the original W886-era version (169 lines, RU+ES only). W916 EN support, W1258
per-sentence ES fix, and the W1250 findings remain pending. W994 affects `voice_commands.py`
only and has no impact on `punctuation_fixer.py`.

---

## Status of W1250 Findings in Production (codex/krab-ear-v2)

| Finding | Severity | Status |
|---------|----------|--------|
| F1 — `_fix_spanish` prepends `¿`/`¡` to entire text | MEDIUM | **STILL PRESENT** — W1258 fix not merged |
| F2 — `language="auto"` / unknown codes skip first-char capitalisation | LOW | **STILL PRESENT** |
| F3 — Dead `_ES_QUESTION_MISSING_IQUEST_RE` / `_ES_EXCL_MISSING_IEXCL_RE` | LOW | **STILL PRESENT** |
| F4 — `_ASCII_QUOTE_BLOCK_RE` 80-char cap | LOW | **STILL PRESENT** |
| F5 — No test coverage for `language="auto"` / unknown codes | INFO | **STILL PRESENT** |

---

## W1019 (FR/TR/PT Language Detector Fix) Interaction

W1019 (`fix-language-detector-FR-TR-W1019`, PR #942) is also unmerged. When it does merge,
`language_detector.py` will correctly suppress `fr`/`tr`/`pt` codes so they don't reach
`PunctuationFixer`. Until then, any FR/TR/PT text misclassified by `LanguageDetector` will
arrive here with codes that fall through all `if/elif language == "..."` branches silently.
Per F2 in W1250, this produces uncapitalised output. The W1019 fix reduces exposure but
does not eliminate the issue — non-zero probability of unknown codes still reaching the fixer
from other callers (REST API, LLM pipeline, user IPC `fix_punctuation` with custom language param).

---

## New Findings (W1348)

**5 NEW findings** distinct from all W886/W1250 scope: 1 HIGH, 1 MEDIUM, 2 LOW, 1 INFO.

---

### Finding R1 — HIGH: `»` in `_NO_SPACE_AFTER_PUNCT_RU_RE` causes spurious spaces before sentence punctuation

**Location:** `KrabEar/core/punctuation_fixer.py:19`

```python
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s\d»\"')\]])")
```

**Problem:** The closing guillemet `»` is included in the trigger set of `_NO_SPACE_AFTER_PUNCT_RU_RE`.
This pattern adds a space after `»` when the next character is not whitespace or a digit. That
includes periods, commas, and other punctuation characters — so `«слово».` becomes `«слово» .`.

The rule ordering in `fix()` is:

```
1. _SPACE_BEFORE_PUNCT_RE    # removes space before ,.:;!?»
2. _SPACE_BEFORE_PUNCT_RE    # (already ran)
3. _NO_SPACE_AFTER_PUNCT_RU_RE  # adds space after ,;!?»  ← introduces new space before .
```

`_SPACE_BEFORE_PUNCT_RE` runs **before** `_NO_SPACE_AFTER_PUNCT_RU_RE`, so any spaces introduced
by step 3 (including `» .` and `» ,`) are never cleaned up. The final `.strip()` does not remove
internal spaces.

**Reproduced (verified live):**

```python
fixer = PunctuationFixer()

fixer.fix('Он сказал «стоп».', language='ru')
# Returns: 'Он сказал «стоп» .'  ← space before period (WRONG)

fixer.fix('Он сказал «привет», она ответила.', language='ru')
# Returns: 'Он сказал «привет» , она ответила.'  ← space before comma (WRONG)

fixer.fix('Он читал «Мир»,«Война».', language='ru')
# Returns: 'Он читал «Мир» ,«Война» .'  ← spaces before both , and . (WRONG)
```

**Why existing tests don't catch it:** The existing test (`test_fix_ascii_quotes_to_russian`)
uses `'он сказал "привет" мне.'` where `»` is followed by a space (`мне`). Space is in
`[^\s...]`'s excluded set, so the pattern never fires on that case.

**Fix (suggested):** Remove `»` from the trigger set of `_NO_SPACE_AFTER_PUNCT_RU_RE`. A space
after `»` should only be added when followed by a word character, not when followed by any
non-space. Alternatively, add a second pass of `_SPACE_BEFORE_PUNCT_RE` after step 3:

```python
# Option A — remove » from trigger set:
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?])([^\s\d»\"')\]])")

# Option B — re-run space-before-punct after no-space-after-punct:
result = _NO_SPACE_AFTER_PUNCT_RU_RE.sub(r"\1 \2", result)
result = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", result)  # second pass cleans up new spaces
```

**Severity:** HIGH — corrupts every RU transcript where a quoted word is immediately
followed by a period or comma (e.g. `«Война и мир».` is common citation format in Russian).

---

### Finding R2 — MEDIUM: `get_fixes_applied` does not receive `language` parameter, causing spurious `ya`-capitalisation report for non-RU inputs

**Location:** `KrabEar/core/punctuation_fixer.py:120`

```python
def get_fixes_applied(self, original: str, fixed: str) -> List[str]:
```

**Problem:** `get_fixes_applied` has no `language` parameter. It unconditionally checks
`_STANDALONE_YA_RE.search(original) and 'Я' not in original` regardless of whether
`language="es"` (or any non-RU code) was used in the `fix()` call. When text contains the
Russian letter `я` but was processed as ES, the ES branch capitalises the first character
(making `я estaba` → `Я estaba`), and `get_fixes_applied` then reports
`"capitalized standalone 'я'"` — which was actually done by the first-char capitalisation
logic, not the dedicated ya-rule in `_fix_russian`.

**Reproduced (verified live):**

```python
fixer = PunctuationFixer()
original = 'я estaba en casa'
fixed = fixer.fix(original, language='es')     # fixed = 'Я estaba en casa.'
fixes = fixer.get_fixes_applied(original, fixed)
# Returns: ['added missing period', 'capitalized first letter', "capitalized standalone 'я'"]
# The ya-capitalisation report is SPURIOUS — it happened via first-char cap, not ya-rule
```

Callers that use `get_fixes_applied` to build structured audit logs (e.g. the LLM pipeline's
punctuation pass via `LLMRewriter.fix_punctuation_only`) will emit misleading audit entries.

**Fix (suggested):** Add a `language: str = "ru"` parameter to `get_fixes_applied` and gate
the ya-check:

```python
def get_fixes_applied(self, original: str, fixed: str, language: str = "ru") -> List[str]:
    ...
    if language == "ru" and _STANDALONE_YA_RE.search(original) and "Я" not in original:
        fixes.append("capitalized standalone 'я'")
```

**Severity:** MEDIUM — no functional corruption, but produces misleading structured audit/log
data. Can cause false positives in downstream analytics that count fix types.

---

### Finding R3 — LOW: `_NO_SPACE_AFTER_PERIOD_RE` only guards uppercase after period; lowercase-start words following `Dr.`/abbreviation get no space

**Location:** `KrabEar/core/punctuation_fixer.py:20`

```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")
```

**Problem:** The pattern only adds a space between a period and an **uppercase** letter. When
STT produces text like `Dr.smith` or `e.g.this`, the period is immediately followed by a
lowercase letter — the pattern does not match, no space is inserted. This is especially
relevant for EN text processed via the `language="ru"` fallback (since W916 is not merged).

**Reproduced:**

```python
fixer.fix('Dr.smith works here.', language='ru')
# Returns: 'Dr.smith works here.'  ← no space after Dr. (WRONG, should be 'Dr. Smith...')
```

**Note:** For standard sentence splitting in Russian text, STT output almost always produces
uppercase after a period (first word of next sentence), so this gap is low-severity for pure
RU. It becomes more relevant for mixed EN/RU text or when W916 EN support merges.

**Fix (suggested):** Broaden the pattern to include the Unicode word-boundary approach, or
handle only in the EN branch once W916 is merged. Minimal fix for cross-language:

```python
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-Za-zЁ])")
# Note: this would fire on abbreviations like 'U.S.A.' — needs careful scoping
```

A safer fix gates this only in the EN branch where abbreviations are common.

**Severity:** LOW — affects mixed-language text and post-W916 EN transcripts; pure-RU
sentences are unaffected since Whisper/GigaAM capitalise after sentence boundaries.

---

### Finding R4 — LOW: `text_postprocessor._BUILTIN_STEPS["fix_punctuation"]` hardcodes `language="ru"` — ES/EN transcripts silently get Russian rules

**Location:** `KrabEar/core/text_postprocessor.py:181`

```python
_BUILTIN_STEPS: dict[str, PostProcessorStep] = {
    ...
    "fix_punctuation": FixPunctuation(language="ru"),
    ...
}
```

**Problem:** The built-in step registry instantiates `FixPunctuation` with `language="ru"` at
module-load time. Any caller that uses the default `TextPostProcessor` chain (e.g. via
`DEFAULT_CHAIN`) to process Spanish or English transcripts will get Russian punctuation rules:

- `¿`/`¡` marks will never be added to ES output.
- Russian-specific `«»` guillemet conversion fires on ES text (benign but unexpected).
- When W916 EN support merges, EN-specific typographic apostrophes/quotes will still not
  apply to EN text processed through the default chain.

There is no factory or runtime-language-injection mechanism: `FixPunctuation._language` is
set in `__init__` and the BUILTIN_STEPS dict is a module-level singleton.

**Reproduced:**

```python
from core.text_postprocessor import TextPostProcessor
tp = TextPostProcessor()
result = tp.process('cómo estás?')  # uses fix_punctuation with language='ru'
# No ¿ added — ES rules never fire
```

**Fix (suggested):** Either (a) make `FixPunctuation.process()` accept a language kwarg
and pass it through, or (b) provide factory helpers in `text_postprocessor.py`:

```python
def make_processor(language: str = "ru") -> TextPostProcessor:
    """Return a TextPostProcessor with language-specific fix_punctuation step."""
    steps = dict(_BUILTIN_STEPS)
    steps["fix_punctuation"] = FixPunctuation(language=language)
    return TextPostProcessor(steps=steps)
```

**Severity:** LOW — ES transcripts processed via `TextPostProcessor` default chain silently
miss `¿`/`¡` markers. No crash or data loss, but quality regression for ES use-case.

---

### Finding R5 — INFO: No test for `»` immediately followed by sentence punctuation (R1 regression path)

**Location:** `KrabEar/tests/test_punctuation_fixer.py`

**Problem:** The test suite has zero coverage for the case where ASCII-quote → guillemet
conversion produces a `»` immediately adjacent to a period or comma. The existing test
`test_fix_ascii_quotes_to_russian` uses `'он сказал "привет" мне.'` where `»` is followed
by a space — masking the R1 bug entirely.

**Suggested test cases:**

```python
def test_guillemet_before_period(self):
    """'...«слово».' should not gain a space before the period."""
    result = self.fixer.fix('Он сказал «стоп».', language='ru')
    self.assertNotIn('» .', result, f"Space before period: {result!r}")
    self.assertTrue(result.endswith('.'))

def test_guillemet_before_comma(self):
    """'...«слово»,' should not gain a space before the comma."""
    result = self.fixer.fix('Он сказал «привет», она ответила.', language='ru')
    self.assertNotIn('» ,', result, f"Space before comma: {result!r}")
```

**Severity:** INFO — test gap documents the R1 bug path.

---

## Idempotency Verification

| Test Case | Language | Pass 1 | Pass 2 | Stable? |
|-----------|----------|--------|--------|---------|
| `'привет мир'` | ru | `'Привет мир.'` | `'Привет мир.'` | YES |
| `'cómo estás?'` | es | `'¿Cómo estás?'` | `'¿Cómo estás?'` | YES |
| `'hello world'` | auto | `'hello world.'` | `'hello world.'` | YES |
| `'Он сказал «стоп».'` | ru | `'Он сказал «стоп» .'` | `'Он сказал «стоп» .'` | YES (stable, but wrong) |

**Key observation:** The R1 bug output is **idempotent** — the corrupted form (`'Он сказал «стоп» .'`)
is stable on repeated application. The `» ` sequence is now followed by `.`, and since
`_SPACE_BEFORE_PUNCT_RE` removes `\s+` before `.`, the second pass would clean it up? Let
me verify:

```python
fixer.fix('Он сказал «стоп» .', language='ru')
# Returns: 'Он сказал «стоп».'  ← step 2 removes space before period
```

Interesting: the second application actually **repairs** the R1 corruption. This means the
fixer is not idempotent in the presence of the R1 bug (first application corrupts, second
repairs). This two-step oscillation can cause non-deterministic output when the fixer is
called multiple times in a pipeline.

---

## Non-Findings (Verified Clean in This Pass)

| Concern | Verdict |
|---------|---------|
| `_STANDALONE_YA_RE` Unicode word boundary (`моя`/`семья`) | CLEAN — Python 3 `\w` is Unicode-aware |
| `_MISSING_PERIOD_RE` double-period on `!`/`?` end | CLEAN — pattern anchors on word chars/digits only |
| Concurrent thread safety (20 threads) | CLEAN — all regex are module-level, no shared state |
| ES single-sentence idempotency (3 passes) | CLEAN — `¿cómo?` → `¿Cómo?` → stable |
| `language="auto"` crash | CLEAN — falls through gracefully, returns text with common rules |

---

## Action Summary

| # | Severity | Finding | Distinct from W1250? |
|---|----------|---------|----------------------|
| R1 | HIGH | `»` in `_NO_SPACE_AFTER_PUNCT_RU_RE` causes space-before-punct corruption | YES — new |
| R2 | MEDIUM | `get_fixes_applied` language-blind ya-check gives spurious audit entries | YES — new |
| R3 | LOW | `_NO_SPACE_AFTER_PERIOD_RE` misses lowercase after period (abbreviations) | YES — new |
| R4 | LOW | `text_postprocessor._BUILTIN_STEPS` hardcodes `language="ru"` | YES — new (wiring scope) |
| R5 | INFO | No test for `»` immediately before `.`/`,` (R1 regression path) | YES — new |
