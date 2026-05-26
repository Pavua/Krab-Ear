# TextAnonymizer Fourth-Pass Audit — W1276

**Date:** 2026-05-26  
**Auditor:** W1276 (fourth-pass sub-agent)  
**File:** `KrabEar/core/text_anonymizer.py`  
**Base branch:** `codex/krab-ear-v2` (HEAD `6c900317`, v2.0.5)

---

## Prior Wave Merge State

All five prior fix waves are on **open branches** — none have been merged to `codex/krab-ear-v2`.

| Wave | Branch | Commit | Status |
|------|--------|--------|--------|
| W902 | `feature/fix-anonymizer-phones-W902` (remote only) | `eeb78634` | **NOT MERGED** |
| W1011 | `audit-text-anonymizer-W1011` (local+remote) | `00d4d373` | **NOT MERGED** (docs only) |
| W1021 | `fix-text-anonymizer-passport-dni-W1021` | `706afe6d` | **NOT MERGED** |
| W1022 | `fix-text-anonymizer-snils-ssn-iban-W1022` | `f0de9549` | **NOT MERGED** |
| W1122 | `audit/text-anonymizer-residual-W1122` | `c6b3b8fa` | **NOT MERGED** (docs only) |
| W1127 | `fix-text-anonymizer-eu-phones-W1127` | `b104d758` | **NOT MERGED** |
| W1128 | `fix-text-anonymizer-inn-yul-W1128` | `de17b6aa` | **NOT MERGED** |

**Main-branch state (baseline for this audit):** `text_anonymizer.py` contains 235 lines with 7 rules: `phone` (RU +7/8), `email`, `credit_card` (Luhn), `passport` (RU), `date_of_birth`, `inn` (12-digit FL), `snils` (formatted only).

---

## W1122 Findings Residual Status

| ID | Finding | Status on `codex/krab-ear-v2` |
|----|---------|-------------------------------|
| N1 | UK/DE/FR/IT phones (+44/+49/+33/+39) | **OPEN** — W1127 branch unmerged |
| N2 | ИНН ЮЛ 10-digit org TIN | **OPEN** — W1128 branch unmerged |
| N3 | Amex 15-digit cards | **OPEN** — no fix branch exists |
| N4 | RU/ES license plates | **OPEN** — no fix branch exists |
| N5 | IPv6 addresses | **OPEN** — no fix branch exists |
| N6 | MAC addresses | **OPEN** — no fix branch exists |
| N7 | SWIFT/BIC codes | **OPEN** — no fix branch exists |

---

## New Findings (W1276)

### F1 — HIGH: `[SSN]` and `[IBAN]` tokens are English; inconsistent with Russian token set

**Location:** `fix-text-anonymizer-snils-ssn-iban-W1022:KrabEar/core/text_anonymizer.py` (W1022 unmerged branch)

**Evidence:**

The W1022 branch adds two new rules with English replacement tokens:

```python
("us_ssn", r"...", "[SSN]"),
("iban",   r"...", "[IBAN]"),
```

All existing tokens are Russian: `[ТЕЛЕФОН]`, `[EMAIL]`, `[КАРТА]`, `[ПАСПОРТ]`, `[ДАТА_РОЖДЕНИЯ]`, `[ИНН]`, `[СНИЛС]`. The W1128 branch (ИНН ЮЛ) correctly uses `[ИНН_ЮЛ]`, preserving the Russian convention. Using `[SSN]` and `[IBAN]` breaks the locale-consistency contract that downstream consumers (paste formatters, transcript writers, Obsidian sync) rely on — they scan for Cyrillic bracketed tokens to strip or annotate PII in transcripts.

**Fix:** Change `[SSN]` → `[ССН]` and `[IBAN]` → `[ИБАН]` in W1022 before merge.

---

### F2 — MEDIUM: IBAN regex misses space-grouped format (the dominant human-dictated form)

**Location:** `fix-text-anonymizer-snils-ssn-iban-W1022:KrabEar/core/text_anonymizer.py`

**Evidence:**

```python
# W1022 pattern
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
```

Word-boundary anchors prevent matching across spaces. Testing confirms:

```python
iban_re.search("GB82 WEST 1234 5698 7654 32")  # → None (missed)
iban_re.search("GB82WEST12345698765432")         # → Match (compact form)
```

Real IBANs as spoken or printed always include spaces in groups of 4 (ISO 13616-1 §3.2). Transcribed speech ("Джи Би восемьдесят два...") arrives compact or space-separated, so both forms must be matched. The compact form is rare in natural speech.

**Fix:** Change regex to `r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{4}\s?){2,8}[A-Z0-9]{1,4}\b"` (allow optional spaces every 4 chars) and strip spaces before mod-97 validation (already done in W1022 via `re.sub(r"\s", "", ...)`).

---

### F3 — MEDIUM: СНИЛС unformatted 11-digit string not matched

**Location:** `KrabEar/core/text_anonymizer.py` (main branch, line ~93); same pattern in W1022

**Evidence:**

```python
# Current pattern (main + W1022)
r"\b\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]?\d{2}\b"
```

Testing:

```python
snils_re.search("12345678901")    # → None — unformatted 11 digits missed
snils_re.search("123-456-789 01") # → Match — only formatted caught
```

СНИЛС issued before 2002 and many digital forms omit separators. The IPC handler receives it as 11 continuous digits in OCR and voice transcription contexts. The W1022 branch adds `_SNILS_DETAIL_RE` for validation but the outer pattern still requires the dash/space separators, so the check never runs on compact input.

**Fix:** Extend pattern to `r"\b(?:\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]?\d{2}|\d{11})\b"` and feed both forms through `_SNILS_DETAIL_RE` after stripping separators.

---

### F4 — LOW: `[КАРТА]` (Russian) replacement token used for English/Spanish card context; misleads non-RU log consumers

**Location:** `KrabEar/core/text_anonymizer.py`, line ~100 (main branch)

**Evidence:**

The replacement token for `credit_card` is `[КАРТА]`. The anonymizer is used in ES/EN transcript contexts (Phase 2 live translation, call assist). Downstream log parsers outside the RU context (e.g., Sentry breadcrumbs in `backend/observability.py`) see `[КАРТА]` and cannot classify it as a card redaction without a Cyrillic decoder.

This is a **design inconsistency** rather than a correctness bug: the anonymizer DOES redact the PII; the token is just opaque to non-RU observers. The existing test `test_credit_card_no_spaces_16_digits` asserts `[КАРТА]` in a Spanish sentence, documenting the mismatch.

**Fix options (pick one):**
- Add a `locale` param to `anonymize()` defaulting to `"ru"` with `"en"` and `"es"` variants for replacement tokens.
- Or use locale-neutral tokens like `[PII:CARD]`, `[PII:PHONE]`, `[PII:SSN]` across all languages.
- Minimum fix for W1022/W1127: keep RU tokens but document the locale assumption in the module docstring.

---

### F5 — MEDIUM: `test_redact_credit_card_invalid_luhn_kept` is a **currently-failing** test in main branch

**Location:** `KrabEar/tests/test_text_anonymizer.py`, lines 432–443

**Evidence (verified by running the test suite):**

```
FAILED KrabEar/tests/test_text_anonymizer.py::TestTextAnonymizerCreditCardLuhn::test_redact_credit_card_invalid_luhn_kept
AssertionError: '[КАРТА]' not found in 'Число: 1234567890123456'
```

The test was written to document pre-Wave-214 behavior (no Luhn check, so all 16-digit strings were redacted). Wave 214 (`feat(wave214): TextAnonymizer Luhn validation`) was merged and changed the behavior — Luhn-invalid cards are no longer redacted — but the test class `TestTextAnonymizerCreditCardLuhn` was not updated. A correctly-behaving test already exists:

```python
# test_luhn_invalid_16_digits_kept (line 139) — correctly asserts the new behavior:
self.assertNotIn("[КАРТА]", result.anonymized_text)  # Luhn-invalid NOT redacted
```

The stale test directly contradicts Wave 214 and produces a CI failure. Upgrade severity to MEDIUM because it breaks the test suite on the current main branch.

**Fix:** Remove `test_redact_credit_card_invalid_luhn_kept` (lines 432–443) — its intent is already covered by `test_luhn_invalid_16_digits_kept`.

---

## Summary

| ID | Severity | Category | Affects |
|----|----------|----------|---------|
| F1 | HIGH | Token locale inconsistency (`[SSN]`/`[IBAN]` English) | W1022 (unmerged) |
| F2 | MEDIUM | IBAN spaced format not matched | W1022 (unmerged) |
| F3 | MEDIUM | СНИЛС unformatted 11-digit not matched | main + W1022 |
| F4 | LOW | Russian tokens in ES/EN transcript context | main (design) |
| F5 | MEDIUM | Stale test **currently failing** in CI post-Wave 214 | `test_text_anonymizer.py` |

**W1122 N3–N7** (Amex 15-digit, RU/ES plates, IPv6, MAC, SWIFT/BIC) remain open with no fix branches — not re-reported per audit cap.

---

## Merge Recommendation

1. **Block W1022 merge** until F1 (token names) and F2 (spaced IBAN) are fixed.
2. **Fix F3** in the same W1022 PR (СНИЛС unformatted 11-digit).
3. **W1127 and W1128** are clean to merge after rebase on `codex/krab-ear-v2`; no new issues found in those branches.
4. **F5** can be a standalone test-fix PR (low-risk, 3-line change).
