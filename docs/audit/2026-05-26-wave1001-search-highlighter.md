# Wave 1001 — SearchHighlighter Security & Quality Audit

**Date:** 2026-05-26  
**Auditor:** W1001 (sub-agent)  
**File:** `KrabEar/core/search_highlighter.py`  
**Wire:** `KrabEar/backend/history_service.py` → IPC method `search_with_highlights`

---

## Summary

`SearchHighlighter` is a compact, well-structured module. **No critical bugs found.** Five findings identified, all low-to-medium severity. The module is properly wired and has solid test coverage.

---

## Findings

### F1 — Word-boundary: no `\b` — substring matches cross word boundaries (LOW)

**Location:** `_build_pattern()` line 164

```python
alternation = "|".join(re.escape(w) for w in words)
return re.compile(alternation, re.IGNORECASE | re.UNICODE)
```

No word-boundary anchors (`\b`) are used. Searching for `"cat"` in `"catfish concatenate"` will highlight both substrings inside longer words. This is the **same boundary trap noted in W926/W991**.

For Cyrillic text, `\b` works correctly with `re.UNICODE` because Unicode word-character class includes Cyrillic letters, so the fix is safe cross-language.

**Risk:** Cosmetic false-positives in search results. Not a security issue.

**Fix candidate:**
```python
alternation = "|".join(r"\b" + re.escape(w) + r"\b" for w in words)
```

---

### F2 — `highlight_html`: per-word loop re-compiles regex, no caching (LOW-PERF)

**Location:** `highlight_html()` lines 86–93

```python
for word in words:
    escaped_word = re.escape(html.escape(word))
    word_pattern = re.compile(escaped_word, re.IGNORECASE | re.UNICODE)
    result = word_pattern.sub(...)
```

`_build_pattern()` already builds a single combined alternation pattern for `highlight()` and `extract_snippets()`, but `highlight_html()` loops and compiles a **separate regex per word** on every call. For a 10-word query against a long transcript, this is 10× the compilation cost with no caching.

Additionally, `_build_pattern()` result is discarded — `highlight_html()` never uses it.

**Risk:** CPU overhead on large history lists. No correctness issue.

**Fix candidate:** Unify with `_build_pattern()` logic; apply a single alternation pattern on the already-escaped text.

---

### F3 — Cyrillic case-insensitivity: `re.IGNORECASE | re.UNICODE` is correct but untested for uppercase Cyrillic (INFO)

**Location:** `_build_pattern()` line 165

`re.IGNORECASE | re.UNICODE` handles Cyrillic uppercase correctly in CPython (e.g., `"Привет"` matches `"привет"`). Tests cover a Cyrillic example (`test_unicode_query`) but only with a **lowercase query against lowercase text** — no test verifies `"МИР"` query matches `"мир"` text.

`str.upper()` is locale-independent in Python's `re` module (uses Unicode case-folding tables), so this is safe in practice. Flagged as a test gap only.

**Risk:** None in CPython. Test gap only.

---

### F4 — Snippet contains raw transcript text — no PII guard (LOW)

**Location:** `extract_snippets()` lines 135–146; `history_service.py` line 230

Snippets are slices of raw transcript text returned directly to the IPC caller:

```python
snippets.append(f"{prefix}{text[start:end]}{suffix}")
```

`history_service.handle_search_with_highlights()` passes the full `item["text"]` with no redaction. If privacy mode (`privacy_mode=True`) is active, raw transcripts should not be surfaced in search results. There is no check for the privacy setting before calling `extract_snippets()`.

Other IPC paths (e.g., `get_history`) respect `privacy_mode` by filtering or redacting text fields.

**Risk:** Privacy-mode bypass via `search_with_highlights` IPC call. Medium severity in a privacy-conscious product.

**Fix candidate:** In `history_service.handle_search_with_highlights()`, check `privacy_mode` setting before returning `snippets` (return `[]` or redacted placeholder when privacy mode is on).

---

### F5 — Overlapping multi-term matches in `highlight()`: double-tagging possible (LOW)

**Location:** `highlight()` — `_build_pattern()` builds one alternation, `.sub()` applies once

`highlight()` correctly uses a single pass with an alternation regex, so overlapping matches are handled by the regex engine (leftmost-longest wins). No double-tagging occurs here.

However, `highlight_html()` applies **sequential per-word substitutions** (F2 above). If two query words overlap in the escaped text (rare but possible with HTML entities like `&amp;`), the second substitution can wrap an already-tagged span, producing nested `<span>` tags.

**Example:** query `"amp"` on text `"a & b"` → escaped: `"a &amp; b"` → first pass wraps `amp` inside `&amp;` → second pass (if multi-word) would match inside the span tag text.

The existing test `test_html_escape_no_double_escape_in_highlight` covers the `"&"` single-char case but not multi-term overlap.

**Risk:** Malformed HTML in edge cases. No XSS risk (content is pre-escaped before substitution).

---

## Checklist Summary

| # | Check | Result |
|---|-------|--------|
| 1 | Regex injection (`re.escape`) | PASS — all user input escaped |
| 2 | Word boundary trap | FINDING F1 — no `\b`, substring matches |
| 3 | Case sensitivity (Cyrillic) | PASS — `re.UNICODE` correct; test gap noted F3 |
| 4 | Per-result regex compilation | FINDING F2 — `highlight_html` inefficient |
| 5 | HTML output escaping (XSS) | PASS — `html.escape()` before pattern |
| 6 | Snippet length bounded | PASS — `max_snippets` + `context_chars` params |
| 7 | Wire status | PASS — live at IPC `search_with_highlights` |
| 8 | Test coverage | PASS — 5 test classes, 40+ cases, concurrent test |
| 9 | PII / privacy guard | FINDING F4 — no `privacy_mode` check |
| 10 | Overlapping matches | PARTIAL — `highlight()` OK; `highlight_html` risk F5 |

---

## Priority

| Finding | Severity | Effort |
|---------|----------|--------|
| F4 — Privacy mode bypass in snippets | MEDIUM | S (1 guard in history_service) |
| F1 — No word boundary | LOW | S (add `\b` to pattern) |
| F5 — Nested spans in highlight_html | LOW | M (refactor to single-pass) |
| F2 — Per-word regex recompile | LOW-PERF | M (unify with _build_pattern) |
| F3 — Cyrillic uppercase test gap | INFO | S (one test case) |
