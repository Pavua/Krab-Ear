# Audit: PunctuationFixer Residual — Wave 1250

**Date:** 2026-05-26
**Branch:** `audit/punctuation-fixer-residual-W1250`
**File audited:** `KrabEar/core/punctuation_fixer.py` (168 lines, `codex/krab-ear-v2`)
**Predecessor audits:** W886 (initial RU/ES audit), W916 (EN support fix), W994 (voice_commands lookahead — not punctuation_fixer)

---

## Prior-Wave Merge State

| Wave | Commit | Branch | Merged into `codex/krab-ear-v2`? |
|------|--------|--------|----------------------------------|
| W886 | `5bcccdae` | `audit-abbreviation-residual-W1111` | NOT merged (docs only) |
| W916 | `009213ac` | `feature/fix-punctuation-en-W916` | NOT merged |
| W994 | `11ebc703` | `fix/voice-commands-boundary-W994` | NOT merged |

W886 was a text-utils audit doc; the EN support fix from W916 (`_fix_english()` method) is **not present** in `codex/krab-ear-v2`. All three predecessor waves are unmerged — the production file is the original W886-era version with RU+ES only.

---

## Summary

**5 NEW findings** (relative to W886/W916 scope): 1 MEDIUM, 3 LOW, 1 INFO.
No HIGH severity. No crash risk. No security issues.

All findings are distinct from W886/W916/W994 findings (W886 identified EN support gap, now tracked as W916 fix pending merge; W994 was voice_commands.py, not punctuation_fixer).

---

## Finding 1 — MEDIUM: `_fix_spanish` prepends `¿`/`¡` to entire multi-sentence text

**Location:** `KrabEar/core/punctuation_fixer.py:93–100`

```python
def _fix_spanish(self, text: str) -> str:
    # Добавить ¿ к вопросам
    if result.rstrip().endswith("?") and not result.lstrip().startswith("¿"):
        result = "¿" + result.lstrip()
    # Добавить ¡ к восклицаниям
    if result.rstrip().endswith("!") and not result.lstrip().startswith("¡"):
        result = "¡" + result.lstrip()
```

**Problem:** The check fires on the entire `text` string. When STT produces multi-sentence output where only the last sentence is a question, `¿` is prepended to the beginning of the entire paragraph.

**Reproduced:**
```python
f.fix("Hola. cómo estás?", language="es")
# Returns: '¿Hola. Cómo estás?'   ← ¿ before 'Hola', WRONG
# Expected: 'Hola. ¿Cómo estás?'
```

The same defect applies to `¡`: `f.fix("Buenas noches. qué tal!", language="es")` → `'¡Buenas noches. Qué tal!'`.

In practice, Whisper/GigaAM often returns multi-sentence output for a single recording. Spanish call recordings where a greeting is followed by a question are common.

**Fix (suggested):** Apply the `¿`/`¡` logic sentence-by-sentence instead of to the whole string. Minimal fix:

```python
# Split on sentence boundaries, fix each sentence individually
import re
_SENT_SPLIT_ES = re.compile(r'(?<=[.!?…])\s+')

def _fix_spanish(self, text: str) -> str:
    sentences = _SENT_SPLIT_ES.split(text.strip())
    fixed = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if s[0].islower():
            s = s[0].upper() + s[1:]
        if s.rstrip().endswith("?") and not s.lstrip().startswith("¿"):
            s = "¿" + s.lstrip()
        elif s.rstrip().endswith("!") and not s.lstrip().startswith("¡"):
            s = "¡" + s.lstrip()
        fixed.append(s)
    return " ".join(fixed)
```

**Severity:** MEDIUM — produces visibly wrong output for any multi-sentence Spanish recording.

---

## Finding 2 — LOW: `language="auto"` (and any unrecognised code) silently skips first-char capitalisation

**Location:** `KrabEar/core/punctuation_fixer.py:61–74`

```python
if language == "ru":
    result = self._fix_russian(result)
elif language == "es":
    result = self._fix_spanish(result)
# No else / no "auto" branch → no first-char capitalisation
```

**Problem:** All three language branches (`_fix_russian`, `_fix_spanish`, `_fix_english` in W916) capitalise the first character of the text. The mid-sentence capitalisation rule (`_CAPITALIZE_AFTER_SENT_RE`) is applied globally, but capitalising the very first character of the text is language-specific — so when `language` is `"auto"` or any unrecognised code (`"fr"`, `"pt"`, `"tr"`, `"de"`, `"zh"`), the first word stays lowercase.

**W1019 interaction:** W1019 identified that `audio_lang_id` can emit `"fr"`, `"tr"`, or `"pt"` when FR/TR/PT is misidentified as ES. When those codes reach `PunctuationFixer.fix()`, the entirely wrong language branch fires (or no branch at all for `"auto"`), silently producing uncapitalised output.

**Reproduced:**
```python
f.fix("я пошёл домой", language="auto")   # → 'я пошёл домой.'  (no capitalisation)
f.fix("bonjour monde", language="fr")     # → 'bonjour monde.'  (no capitalisation)
f.fix("olá mundo", language="pt")         # → 'olá mundo.'      (no capitalisation)
```

Compare to `language="ru"` which returns `'Я пошёл домой.'`.

**Fix (suggested):** Move first-char capitalisation to the shared prefix rules (before the language dispatch), keeping language-specific variants only for RU-specific `я` and ES-specific `¿`/`¡`:

```python
# In fix(), after _CAPITALIZE_AFTER_SENT_RE.sub(...)
if result and result[0].islower():
    result = result[0].upper() + result[1:]
```

**Severity:** LOW — silent quality degradation; not a crash or data-loss risk.

---

## Finding 3 — LOW: Dead module-level pattern variables `_ES_QUESTION_MISSING_IQUEST_RE` and `_ES_EXCL_MISSING_IEXCL_RE`

**Location:** `KrabEar/core/punctuation_fixer.py:37–42`

```python
# Испанский: вопросительное предложение без ¿
_ES_QUESTION_MISSING_IQUEST_RE = re.compile(r"^(?!¿)(.+\?)$")
# Испанский: восклицательное без ¡
_ES_EXCL_MISSING_IEXCL_RE = re.compile(r"^(?!¡)(.+!)$")
```

**Problem:** Both compiled patterns are defined at module level but are **never referenced** anywhere in the `PunctuationFixer` class or module. `_fix_spanish()` uses inline `str.endswith` / `str.startswith` checks instead of these patterns. The compiled objects are loaded on every import, consuming memory and adding confusion about the intended implementation path.

**Verified:** `grep` and `inspect.getsource` confirm zero usages inside the class or module outside their definition lines.

**Fix (suggested):** Remove both dead variables. If the regex approach is preferred over the inline string checks for the multi-sentence fix (F1), adapt the pattern and reference it in `_fix_spanish`.

**Severity:** LOW — dead code, no functional impact. Adds ~2 compiled regex objects per process startup (negligible cost but confusing).

---

## Finding 4 — LOW: `_ASCII_QUOTE_BLOCK_RE` silently skips quoted spans > 80 characters

**Location:** `KrabEar/core/punctuation_fixer.py:29`

```python
_ASCII_QUOTE_BLOCK_RE = re.compile(r'"([^"]{1,80})"')
```

**Problem:** The `{1,80}` quantifier hard-limits matched quoted content to 80 characters. For STT output containing a long quotation (e.g. a verbatim speech excerpt, a long name, a slogan), the pattern silently fails to convert the ASCII double-quotes to guillemets `«»`.

**Reproduced:**
```python
long_quote = '"' + 'A' * 81 + '"'
f.fix(f'он сказал {long_quote}', language='ru')
# Result still contains ASCII double-quotes; «» not substituted
```

The 80-char cap may have been intended to prevent catastrophic backtracking on pathological input, but `[^"]{1,80}` (negated character class, no backtracking risk) already has O(n) worst-case. The cap should be raised to at least 500 or removed entirely, since the negated class `[^"]` is already safe.

**Severity:** LOW — silent failure for long quotes in RU transcripts.

---

## Finding 5 — INFO: No test coverage for `language="auto"` or unknown language codes

**Location:** `KrabEar/tests/test_punctuation_fixer.py` — missing test cases

**Problem:** All existing tests use `language="ru"` or `language="es"`. There are zero tests for:
- `language="auto"` (documented as a valid input elsewhere in the pipeline)
- Unknown codes: `"fr"`, `"pt"`, `"tr"` (W1019 interaction path)
- `language="en"` (W916 fix, still unmerged)

The test `test_english_like_text_passes_through_without_error` (line 214) uses the default `language="ru"`, not `language="en"`, and only checks that no exception is raised — not that output is capitalised.

**Fix (suggested):** Add a `TestPunctuationFixerAutoAndUnknown` test class covering:
1. `fix("я пошёл", language="auto")` → result is a string, no exception
2. `fix("bonjour", language="fr")` → result is a string, no exception
3. Capitalisation is **not** expected for unknown language (documents current behaviour, prevents silent regression if F2 is fixed later)

**Severity:** INFO — test gap only; no functional impact.

---

## Non-Findings (Verified Clean)

| Concern | Verdict |
|---------|---------|
| Idempotency (run twice breaks) | CLEAN — verified for RU, ES; all tested cases stable after 3 passes |
| `_STANDALONE_YA_RE` Cyrillic word boundary | CLEAN — Python 3 `re` uses Unicode `\w` by default; `моя`/`семья`/`нельзя` correctly excluded |
| Unicode curly/prime quote corruption | CLEAN — `_ASCII_QUOTE_BLOCK_RE` matches only U+0022 straight quotes; curly/typographic quotes (U+201C/D, U+2032/33) pass through untouched |
| Performance on long text (8 500 chars) | CLEAN — 0.35 ms/call; no catastrophic backtracking |
| `_MISSING_PERIOD_RE` double-period on `!`/`?` ending | CLEAN — `_MISSING_PERIOD_RE` anchors on `[А-Яа-яA-Za-zЁё0-9)]` so does not fire when text ends with `!` or `?` |

---

## Action Summary

| # | Severity | Finding | Suggested fix wave |
|---|----------|---------|-------------------|
| F1 | MEDIUM | `_fix_spanish` prepends `¿`/`¡` to full multi-sentence text | Next fix wave |
| F2 | LOW | `language="auto"` / unknown codes skip first-char capitalisation | Next fix wave (bundle with F1) |
| F3 | LOW | Dead `_ES_QUESTION_MISSING_IQUEST_RE` and `_ES_EXCL_MISSING_IEXCL_RE` variables | Easy cleanup |
| F4 | LOW | `_ASCII_QUOTE_BLOCK_RE` hard-cap 80 chars silently skips long quotes | Easy fix (raise to 500) |
| F5 | INFO | No test coverage for `language="auto"` or unknown codes | Add test class |
