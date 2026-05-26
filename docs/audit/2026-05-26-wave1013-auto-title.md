# Audit W1013 — `core/auto_title.py` (AutoTitleGenerator)

**Date:** 2026-05-26  
**Auditor:** Sub-agent W1013 (read-only, static + dynamic)  
**File:** `KrabEar/core/auto_title.py` (263 lines)  
**Wire status:** Active — imported by `backend/service.py` (line 41), delegated via `TextScoringService` and `MetadataEnricher`; IPC method `generate_auto_title` live.

---

## Summary

`AutoTitleGenerator` is a heuristic title extractor used to produce short display
titles for history items. It is correctly wired and idempotent for typical inputs.
Six issues were found, ranging from a silent contract violation (LOW) to a
privacy concern (MEDIUM) that causes PII to appear in stored titles.

---

## Findings

### F1 — `max_length < 3` silently produces output longer than the bound (LOW)

**Location:** `_truncate_at_word_boundary` (line 222)

When `max_length` is 0, 1, or 2, the code computes `text[:max_length - 3]` which
wraps to `text[:-3]`, `text[:-2]`, or `text[:-1]`. The result is then appended
with `"..."` (3 chars), producing output that far exceeds the caller's bound.

```python
# max_length=2: truncated = text[:2-3] = text[:-1]  (whole string minus last char)
# → returns full-minus-one + "..."  (len >> 2)
generate_title("проект обсуждение", max_length=2)
# → 'Проект обсуждени...'  len=19
```

**Impact:** Any IPC caller passing `max_length` < 3 gets a silently wrong result.
The IPC handler in `service.py` accepts caller-supplied `max_length` without
clamping. A malicious or buggy client could not crash the backend, but titles
would overflow any fixed UI label.

**Fix:** Add `max_length = max(max_length, 4)` guard at the top of
`_truncate_at_word_boundary`, or document that the minimum useful value is 4.

---

### F2 — `generate_title_with_date` total length can exceed 63 chars regardless of `max_length` (LOW)

**Location:** `generate_title_with_date` (line 95)

The method hardcodes `max_length=50` for the text portion, then prepends the date
prefix `"YYYY-MM-DD — "` (13 chars), yielding a total up to **63 characters** with
no way for callers to control it. The outer `generate_title` signature accepts
`max_length` but `generate_title_with_date` ignores it.

```python
generate_title_with_date("обсуждение проекта команды...", "2026-05-26")
# → '2026-05-26 — Обсуждение проекта разработки новой...'  len=63
```

**Impact:** UI labels or storage fields expecting ≤ 50 chars will be silently
violated. The `MetadataEnricher` calls this without length control.

**Fix:** Accept and thread an optional `max_length` param; subtract
`len(date_prefix) + 4` from the text portion budget.

---

### F3 — PII (phone numbers, emails) leaks verbatim into generated titles (MEDIUM)

**Location:** `generate_title` → `_extract_first_sentence` (line 166)

The generator uses the first significant phrase of the transcript as the title.
When the transcript begins with contact information — a common occurrence for
meeting notes or call recordings — the PII is preserved in the title that is
stored in `history.ndjson` and returned to the UI.

```python
generate_title("+7 916 123 45 67 это мой телефон для связи команды")
# → '+7 916 123 45 67 это мой телефон для связи с...'

generate_title("test@example.com это мой email для рабочей переписки")
# → 'Test@example'   (truncated at word boundary, '@' counted as separator)
```

Phone numbers bypass the filler-word filter (they are not words). Emails survive
but truncate at `@`. Both appear in stored titles and, by extension, in Obsidian
sync exports and analytics dashboard data.

**Impact:** GDPR/privacy concern. Titles are logged, synced to Obsidian, and
passed to LLM summarizers. There is no privacy-mode bypass for title generation.

**Fix:** Strip or redact phone patterns (`\+?\d[\d\s\-]{7,}`) and email patterns
from the candidate before truncation, replacing with a placeholder like
`[телефон]` / `[email]`.

---

### F4 — Short texts (< 5 words) bypass filler-word stripping (LOW)

**Location:** `_extract_first_sentence` (lines 182–184)

The fast path for short text returns the raw content without calling
`_skip_filler_words`, so a transcript of 2–4 filler words ("ну привет как")
produces a title that is entirely composed of filler:

```python
generate_title("ну привет как")   # → 'Ну привет как'
generate_title("ну ладно хорошо") # → 'Ну ладно хорошо'
```

The comment says "use text as-is" for brevity, but the intent of filler stripping
is presumably universal.

**Impact:** Cosmetic quality degradation for short utterances (cough-detection
false positives, partial recordings). Not a correctness bug.

**Fix:** Apply `_skip_filler_words` even for short texts, then fall back to the
original text if the result is empty.

---

### F5 — Diarization brackets (`[...]`) are not stripped from candidate text (LOW)

**Location:** `_extract_from_diarized` → `_extract_first_sentence` (line 153)

`_LEADING_PUNCT_RE` strips `- — _ * • · : , ;` but does **not** include `[` or
`]`. When the first speaker's utterance is annotated with a non-verbal description
(e.g., `[Неразборчивое бормотание]`), that bracketed label becomes the title:

```python
text = "Speaker 0: [Тихое бормотание]\nSpeaker 1: Обсуждаем проект"
generate_title(text)  # → '[Тихое бормотание]'
```

This is not correct — bracketed annotations should be skipped in favour of actual
speech content from the next speaker.

**Impact:** Confusing titles for diarized recordings where the first segment is a
non-verbal annotation.

**Fix:** After splitting by diarization markers, skip segments whose stripped
content matches `^\[.*\]$` (pure bracket annotation) and continue to the next
part. Alternatively, extend `_LEADING_PUNCT_RE` to strip `[`.

---

### F6 — `_RE_WORD_PUNCT` pattern has redundant Cyrillic range (COSMETIC)

**Location:** `_RE_WORD_PUNCT` (line 49)

```python
_RE_WORD_PUNCT = re.compile(r"[^\wА-Яа-яёЁ]")
```

Python 3's `\w` in a compiled regex without `re.ASCII` flag already matches all
Unicode word characters including the full Cyrillic alphabet. The explicit
`А-Яа-яёЁ` range is therefore redundant. The pattern works correctly but the
redundancy could mislead maintainers into thinking `\w` alone would not match
Cyrillic, potentially encouraging copy-paste of the same incorrect assumption
elsewhere.

**Impact:** Zero runtime impact. Cosmetic/maintenance concern only.

**Fix:** Simplify to `re.compile(r"[^\w]")` or `re.compile(r"\W")`.

---

## Wire Status

| Consumer | Method called | Status |
|---|---|---|
| `backend/service.py` | `generate_title`, `generate_title_with_date`, `batch_generate` | Active (IPC `generate_auto_title`) |
| `backend/text_scoring_service.py` | `handle_generate_auto_title` | Active (delegation layer) |
| `backend/metadata_enricher.py` | `generate_title`, `generate_title_with_date` | Active (auto-enrichment on save) |

No dead callers found.

---

## Idempotency

`generate_title` is idempotent for all tested inputs: running it on its own output
produces the same string. The capitalization step (`text[0].upper() + text[1:]`)
is safe to apply to an already-capitalized string.

---

## Edge Cases Verified

| Input | Output | Correct? |
|---|---|---|
| `""` (empty) | `"Запись"` | Yes |
| `"   "` (whitespace) | `"Запись"` | Yes |
| `"Привет"` (single word) | `"Привет"` | Yes |
| `"42"` (single number) | `"42"` | Yes |
| `"1 2 3 4 5 6"` (all numeric) | `"1 2 3 4 5 6"` | Acceptable |
| All-filler text | Falls back to `text` (no crash) | Acceptable |
| 60-char word no spaces | Truncates to 47 chars + `"..."` | Yes |
| RTL Arabic text | Preserved, `capitalize` is no-op on Arabic | Yes |
| Pure emoji tokens | Skipped transparently, content preserved | Yes |
| Diarized text | First speaker extracted | Yes (but see F5) |

---

## Severity Summary

| ID | Severity | Area |
|----|----------|------|
| F3 | MEDIUM | Privacy / PII leak in stored titles |
| F1 | LOW | `max_length < 3` contract violation |
| F2 | LOW | `generate_title_with_date` uncontrolled total length |
| F4 | LOW | Short texts bypass filler stripping |
| F5 | LOW | Diarization brackets appear verbatim in title |
| F6 | COSMETIC | Redundant `А-Яа-яёЁ` in `_RE_WORD_PUNCT` |
