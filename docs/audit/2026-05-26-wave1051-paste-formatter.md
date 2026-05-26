# Wave 1051 — PasteFormatter Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/paste_formatter.py`  
**Wire status:** Fully wired — `format_for_paste` and `list_paste_formatters` registered in `service.py:1082–1085` via `self._paste_formatter`.  
**Test coverage:** `KrabEar/tests/test_paste_formatter.py` — 5 test classes, ~60 test methods covering builtins, `_apply_rules`, persistence, IPC handlers, and edge cases. Good breadth.

---

## Findings (5)

### F1 — Telegram 4096-char limit not enforced [HIGH]

**Location:** `paste_formatter.py:30–40` (`_fmt_telegram`)  
**Issue:** Telegram's documented message limit is 4096 characters. The formatter splits long text into newline-separated sentences at >120 chars, but never truncates or raises an error when the result exceeds 4096 chars. A 5000-char input passes through at full length. The Swift paste layer will pass this directly to the clipboard, and any Telegram API call (e.g. via `telegram_bridge.py`) will receive a `400 Bad Request – MESSAGE_TOO_LONG` error.  
**Reproduction:**
```python
from core.paste_formatter import _fmt_telegram
result = _fmt_telegram("A" * 5000)
assert len(result) == 5000  # no truncation
```
**Fix:** Add a hard cap of 4096 chars at the end of `_fmt_telegram`, appending `…` when truncated. Alternatively expose a `max_length` rule option that downstream callers can set.

---

### F2 — No Telegram markdown escaping (injection risk) [MEDIUM]

**Location:** `paste_formatter.py:30–40` (`_fmt_telegram`)  
**Issue:** Telegram Bot API MarkdownV2 reserves the characters `_ * [ ] ( ) ~ ` > # + - = | { } . !`. If a transcript contains any of these (common in code snippets, URLs, or foreign-language text with dashes), sending the formatted text via Bot API with `parse_mode=MarkdownV2` will either fail or render unintended bold/italic/code. The formatter does not escape these characters.  
**Example:** transcript `"_bold_ *italic* [link](http://evil.com)"` passes through unchanged and would render as formatted Telegram markdown.  
**Note:** When pasting to the Telegram Desktop GUI (clipboard path), this is harmless — the GUI pastes literal text. The risk applies only if the text is sent via Bot API (`telegram_bridge.py`). Current usage is clipboard-only, so severity is MEDIUM.  
**Fix:** Either document that the formatter is clipboard-only (not Bot API), or add a `telegram_escape_markdown` option to the rules engine that applies MarkdownV2 escaping when enabled.

---

### F3 — `_apply_rules` max_length applied before prepend/append [MEDIUM]

**Location:** `paste_formatter.py:152–163` (`_apply_rules`)  
**Issue:** The `max_length` truncation is applied to the body text before `prepend` and `append` are added. This means the final output can exceed `max_length` by `len(prepend) + len(append) + 2` characters. For custom formatters with large prepend blocks, the limit provides false assurance.  
**Reproduction:**
```python
from core.paste_formatter import _apply_rules
result = _apply_rules("hello world!", {"max_length": 5, "prepend": "X" * 50, "append": "Y" * 50})
assert len(result) == 108  # far exceeds max_length=5
```
**Fix:** Move the `max_length` check to the end of `_apply_rules` (after prepend/append) to enforce a true output cap.

---

### F4 — `max_length=0` silently skips truncation (falsy guard) [LOW]

**Location:** `paste_formatter.py:152` (`_apply_rules`)  
**Code:** `if max_len and isinstance(max_len, int) and len(text) > max_len:`  
**Issue:** The condition uses `if max_len` which is falsy for `0`. A custom formatter rule of `{"max_length": 0}` is silently ignored instead of either raising a `ValueError` or enforcing zero-length (which would truncate all body text). This creates a confusing silent no-op for any user who sets `max_length: 0` expecting it to suppress output.  
**Fix:** Change the guard to `if max_len is not None and isinstance(max_len, int) and max_len > 0 and len(text) > max_len:` and add a validation check in `add_custom_formatter` to reject `max_length <= 0`.

---

### F5 — No PII scrubbing on any formatter path [LOW / DESIGN NOTE]

**Location:** All formatter functions; `paste_formatter.py` overall  
**Issue:** Transcripts containing PII (phone numbers, emails, card numbers) pass through all formatters — including `email`, which wraps the text in a formal email template and adds a signature. There is no integration with `core/text_anonymizer.py` (`TextAnonymizer`), which already implements rule-based PII redaction. When a user pastes into an email client, sensitive data from the transcript is included without any warning or opt-in redaction.  
**Note:** `text_anonymizer.py` exists but is not referenced anywhere in `paste_formatter.py`. There is no `anonymize` rule key in `_apply_rules`.  
**Fix (design suggestion):** Add an `anonymize` boolean rule key that calls `TextAnonymizer.anonymize(text)` when `True`. Wire it as default-`True` for the `email` formatter, off for others. This is opt-in and preserves current behavior by default.

---

## Non-issues / confirmed correct

- **Wire status**: Both IPC handlers properly registered and accessible.
- **Thread safety**: `_lock` protects all `self._custom` reads/writes.
- **Custom formatter validation**: `add_custom_formatter` correctly raises `ValueError` on empty name or non-dict rules.
- **Partial name matching**: `"Telegram Desktop"` correctly routes to the `telegram` formatter.
- **Persistence**: `paste_formatters.json` is written atomically per add/remove; load is guarded against JSON errors.
- **Non-string input**: `format_for_app` coerces non-string `text` via `str(text)` before processing.
- **Test coverage quality**: All 5 formatters tested; `_apply_rules` individually tested; IPC handlers tested; persistence round-trip tested. No major gaps.

---

## Summary table

| # | Finding | Severity | File:Line |
|---|---------|----------|-----------|
| F1 | Telegram 4096-char limit not enforced | HIGH | `paste_formatter.py:30–40` |
| F2 | No Telegram MarkdownV2 escaping | MEDIUM | `paste_formatter.py:30–40` |
| F3 | `max_length` applied before prepend/append | MEDIUM | `paste_formatter.py:152–163` |
| F4 | `max_length=0` silently skipped (falsy guard) | LOW | `paste_formatter.py:152` |
| F5 | No PII scrubbing on email formatter path | LOW | design gap |
