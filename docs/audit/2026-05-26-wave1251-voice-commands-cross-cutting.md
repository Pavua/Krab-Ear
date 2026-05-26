# W1251 — Voice Commands Cross-Cutting Audit

**Date:** 2026-05-26
**Branch:** audit/voice-commands-cross-W1251
**Scope:** `KrabEar/core/voice_commands.py` (post-W994 lookahead boundary fix)
**Auditor:** W1251 sub-agent (read-only)

---

## Summary

5 findings. Grammar completeness is adequate for EN/RU/ES. The primary issues are
high-severity false positives on common-noun homophones (RU `вопрос`, EN `period`,
ES `coma`), a duplicate ES table entry, and a pipeline ordering concern where
`TextPostProcessor` can re-process already-substituted punctuation.

---

## Findings

### F1 — HIGH: RU `вопрос` and `точка` are High-Frequency Nouns with No Escape Hatch

**File:** `KrabEar/core/voice_commands.py`, lines 51 and 47

`вопрос` ("question", a very common noun in Russian) fires as `?` on any exact-word
match. Similarly `точка` ("dot/point") fires as `.`.

Observed behaviour (confirmed by live test):

```
"это важный вопрос"          → "это важный?"
"у меня вопрос к вам"        → "у меня? к вам"
"поставь точку зрения"       → "поставь. зрения"
```

Unlike `запятая` ("comma") which is primarily punctuation vocabulary,
`вопрос` and `точка` appear constantly in normal dictation. There is no
escape-character syntax or confidence-score gate. When `voice_commands_enabled=True`
(the default) these words are destructively mangled in every transcription.

**Recommendation:** Remove `вопрос` and `точка` from the default command table
(or gate them behind a separate `voice_commands_strict_mode` flag). Replace
`вопрос` with a longer unambiguous phrase such as `вопросительный знак` (already
present as `вопросительный знак` at line 42) and remove the short alias.
`точка` should remain disabled in default mode since `точка с запятой` covers
the semicolon need and `восклицательный знак` / `вопросительный знак` cover the
terminal punctuation cases without ambiguity.

---

### F2 — HIGH: EN `period`, `colon`, `tab` and ES `coma`, `punto`, `dos puntos` Fire on Ordinary Words

**File:** `KrabEar/core/voice_commands.py`, lines 68–107

All three languages share the same design flaw as F1: command keywords that are
also frequent content words. Confirmed false positives:

**EN:**
```
"the period of time"    → "the. of time"
"a long period"         → "a long."
"colon cancer"          → ": cancer"
"switch to the tab"     → "switch to the\t"
```

**ES:**
```
"el paciente está en coma grave"   → "el paciente está en, grave"
"el punto de vista"                → "el. de vista"
"obtuve dos puntos en el examen"   → "obtuve: en el examen"
```

`tab` is particularly destructive: the word appears in browser, music, and
software-related dictation constantly. `coma` in Spanish is the medical term for
a coma (quite common in medical dictation, one of Krab Ear's primary use cases).

**Recommendation:**
- EN: remove `tab` from default table (use `tab key` or `press tab` instead).
  Gate `period` / `colon` behind `voice_commands_strict_mode` similarly to F1.
- ES: add `insertar coma` / `poner coma` as alternative to bare `coma`.
  Remove `dos puntos` from default (too common as a count phrase).

---

### F3 — LOW: Duplicate Entry `nueva línea` in `_ES_COMMANDS`

**File:** `KrabEar/core/voice_commands.py`, lines 71–72

```python
(r"nueva línea", "insert", "\n"),
(r"nueva línea", "insert", "\n"),   # exact duplicate
```

The duplicate is harmless at runtime (first match wins, second is never reached)
but increases the compiled-pattern list by one slot, wastes one regex compilation,
and is confusing to maintainers who assume the table is canonical.

The original intent was likely to add `nueva línea` → `\n` **and** `nueva línea`
→ `\n\n` (paragraph break), but `punto y aparte` already covers the paragraph case
(line 68). No test covers the duplicate, so it has gone undetected.

**Recommendation:** Remove the second `nueva línea` entry (line 72).

---

### F4 — MEDIUM: Pipeline Order — `TextPostProcessor` Runs After Voice-Command Substitution via IPC but Not via Engine

**Files:**
- `KrabEar/core/engine.py` line 930 (voice_commands step)
- `KrabEar/backend/text_processing_service.py` line 350 (`post_process_text` IPC)

In `engine.py` the pipeline is:
1. `TextUtils.cleanup_transcript` (line 927)
2. `VoiceCommandProcessor.process()` (line 935) — inserts raw punctuation chars
3. `NumberNormalizer` / `DateTimeNormalizer` (lines 947–970)
4. LLM punctuation pass (line 973+)

`TextPostProcessor` (which includes `FixPunctuation` via `PunctuationFixer`) is
**not wired into the engine pipeline at all** — it is only available via the
`post_process_text` IPC method (used by the UI for on-demand text clean-up).

This means:
- When a user calls `post_process_text` on a transcription that already went
  through voice commands, `PunctuationFixer` may re-interpret the already-inserted
  literal punctuation (`, .  — `) and alter it again.
- Conversely, if someone builds a pipeline that runs `TextPostProcessor` BEFORE
  calling transcribe (unlikely but possible via IPC chaining), voice commands
  would not have run yet.

There is also no integration test that exercises the engine path end-to-end with
voice commands enabled alongside a `post_process_text` call on the result.

**Recommendation:** Add a note in `text_processing_service.py::handle_post_process_text`
warning callers that voice-command substitution has already occurred if the text
came from a transcription. No code change required in the engine — the current
separation is correct — but the contract should be documented.

---

### F5 — MEDIUM: `capitalize_next` Silently Consumed When Last Token Is a Command

**File:** `KrabEar/core/voice_commands.py`, lines 312–326

When `большая буква` (or EN `capitalize next`) is the **last token** in the text
with no following word, `capitalize_next` is set to `True` but the processing loop
ends immediately. The trailing space appended by lines 325–326 is also consumed:

```python
# append " " only if pos < length — but pos == length here
if pos < length:
    output.append(" ")
```

Result: the entire `большая буква` command is consumed and the output is the
empty string (or just the preceding content with trailing space stripped).

```
proc.process("большая буква", language="ru") → ""
```

A user dictating `"Привет большая буква"` at the very end of a recording gets
`"Привет"` with the capitalize modifier silently dropped rather than any indication
that a word was expected. There is no warning log at this position.

The same applies to `верхний регистр` / `uppercase` / `caps lock` / `mayúscula`.

**Recommendation:** When `capitalize_next` or `uppercase_next_sentence` is still
`True` at loop exit, append a `logger.debug` warning so the condition is
observable. Optionally, preserve the command token as-is when there is no
following word to modify (less destructive). At minimum add a test case for
"command at end of text with capitalize modifier" to the test suite.

---

## Grammar Completeness Check

| Feature                  | RU        | ES                 | EN                        |
|--------------------------|-----------|--------------------|---------------------------|
| Comma                    | `запятая` | `coma`             | `comma`                   |
| Period / full stop       | `точка`   | `punto`            | `period`, `full stop`     |
| Semicolon                | `точка с запятой` | `punto y coma` | `semicolon`          |
| Colon                    | `двоеточие` | `dos puntos`     | `colon`                   |
| Exclamation              | `восклицательный знак`, `восклицание` | `signo de exclamación` | `exclamation mark`, `exclamation point` |
| Question mark            | `вопросительный знак`, `вопрос` | `signo de interrogación` | `question mark` |
| Em dash                  | `тире`    | `guión largo`      | `em dash`, `dash`         |
| New line                 | `новая строка` | `nueva línea` (×2 — see F3) | `new line` |
| New paragraph            | `новый абзац` | `punto y aparte` | `new paragraph`          |
| Tab                      | `табуляция` | `tabulación`     | `tab`                     |
| Space                    | `пробел`  | `espacio`          | (not present)             |
| Capitalize next          | `большая буква` | `mayúscula`, `letra mayúscula` | `capitalize next` |
| Uppercase sentence       | `капс`, `верхний регистр` | `todo mayúsculas` | `caps lock`, `upper case`, `uppercase` |
| Delete last word         | `удалить последнее слово` | `borrar última palabra` | `delete last word` |
| Delete last sentence     | `удалить последнее предложение` | `borrar última oración` | `delete last sentence` |
| Delete last paragraph    | `удалить последний абзац` | `borrar último párrafo` | `delete last paragraph` |
| Delete fallback          | `удалить последнее` | `borrar último`  | `delete last`             |

Grammar is symmetric and complete. ES is missing a `space` command (`espacio`
is present) and EN is missing explicit `space` insertion — minor, not a gap.

---

## W994 Lookahead Boundary Fix — Confirmed Working

The `_build_pattern()` approach using `\b` + `re.compile(..., re.IGNORECASE)` +
`pattern.match(text, pos)` correctly handles:
- Inflected Cyrillic forms: `запятой` does not match `запятая` (verified).
- Compound words: `незапятаянный` does not trigger (verified).
- Composite commands: `точка с запятой` wins over `точка` because the composite
  pattern is listed first in `_RU_COMMANDS` (index 7 vs 13).

No regression from W994 found.

---

## Test Coverage Assessment

`KrabEar/tests/test_voice_commands.py` covers:
- All three languages with basic insert commands
- Composite RU commands (`точка с запятой`, `восклицательный знак`)
- Delete-last word/sentence variants
- Capitalize / uppercase
- Disabled flag
- Language isolation (RU command not applied for EN)
- Region code normalisation (`ru-RU` → `ru`)
- Word-boundary preservation (substring non-match)
- Concurrent access (thread safety)

**Missing coverage:**
- F1/F2 false-positive cases (`вопрос`, `точка` as nouns, EN `tab` as noun,
  ES `coma` as noun) — no test documents the intentional (or unintentional)
  behaviour.
- F5 capitalize/uppercase command at end-of-text with no following word.
- ES `borrar última oración` (delete-last-sentence in ES) — not tested.
- `auto` / `und` / unrecognised language codes passed to `process()`.
- `voice_commands_languages` as a comma-separated string (line 223 branch).

---

## Interaction with W1116/W1117 AudioLanguageID Routing

`engine.py` line 934: `_vc_lang = resolved_lang or settings.TRANSCRIBE_LANGUAGE`

`resolved_lang` is the Whisper-returned language (or `None` if Whisper ran in
auto-detect mode). `AudioLanguageID` (W1116/W1117) feeds into `STTRouter`, which
resolves the routing language **before** transcription; the result is stored back
in `result.get("language", resolved_lang)` (line 1020) but this post-transcription
language is NOT passed to `VoiceCommandProcessor` — the pre-transcription
`resolved_lang` is used instead.

Consequence: if a user speaks in ES but `resolved_lang` is `"ru"` (fallback when
LID is disabled or audio is too short), ES commands (`coma`, `punto`) will not
fire. This is actually **protective** against F2 false positives in the common
bilingual session — the LID miss silently prevents the false positive. Documenting
this so future changes to LID routing consider the voice-commands side effect.
