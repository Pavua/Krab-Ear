# Wave 1011 — TextAnonymizer Residual PII Gap Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/text_anonymizer.py`  
**Scope:** Residual gaps AFTER W902 (ES +34 / EN +1 phones, INN checksum). Does NOT re-report W902 items.

## Current coverage (baseline)

| Rule | Status |
|------|--------|
| phone (RU +7/8) | present |
| phone (ES +34) | W902 — pending merge |
| phone (US +1) | W902 — pending merge |
| email | present |
| credit_card (16-digit, Luhn) | present |
| passport RU (4+6 / 10 digits) | present |
| date_of_birth | present |
| inn (12 digits) | present — W902 added checksum |
| snils | present |

---

## Finding 1 — SNILS lacks checksum validation (HIGH)

**Category:** data quality / false-positive risk  
**Pattern:** `\b\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]?\d{2}\b`

СНИЛС uses a verhoeff-like mod-101 checksum (penultimate two digits verify the first nine). Without it, any `NNN-NNN-NNN NN` 11-digit sequence (e.g., phone area codes, random numeric strings) is redacted.  
INN already has a checksum guard added by W902; СНИЛС is the same class of problem.

**Fix:** Add `_passes_snils(digits: str) -> bool` analogous to `_passes_luhn`. Apply in `anonymize()` the same way credit_card is handled.

---

## Finding 2 — Spanish DNI / NIE missing (MEDIUM)

**Category:** ES PII — government ID  
**Locale:** ES (primary app language)

Spain national IDs are common in ES-locale transcriptions:
- DNI: `\b\d{8}[A-Z]\b` — 8 digits + 1 letter (letter is a checksum from a fixed 23-char table)
- NIE: `\b[XYZ]\d{7}[A-Z]\b` — foreigner ID, same letter checksum

No DNI/NIE rule exists. A Spanish speaker saying their ID number aloud produces text like "12345678Z" which passes through unredacted.

**Fix:** Add `dni_nie` rule with pattern `\b(?:\d{8}[A-HJ-NP-TV-Z]|[XYZ]\d{7}[A-HJ-NP-TV-Z])\b` (letter checksum optional but recommended — table is 23 chars, pure ASCII math).

---

## Finding 3 — US SSN missing (MEDIUM)

**Category:** EN PII — government ID  
**Locale:** EN secondary

US Social Security Numbers (`AAA-BB-CCCC`, 9 digits) are common in EN audio. Pattern `\b\d{3}[\s\-]\d{2}[\s\-]\d{4}\b` is unambiguous and low false-positive when bounded with `\b`.  
Note: SSN structural rules (area 000/666/900-999 invalid, group 00 invalid, serial 0000 invalid) can reduce false positives further.

**Fix:** Add `ssn` rule: `r"\b(?!000|666|9\d{2})\d{3}[\s\-](?!00)\d{2}[\s\-](?!0000)\d{4}\b"` with replacement `[SSN]`.

---

## Finding 4 — IBAN missing (MEDIUM)

**Category:** financial — bank account  
**Locale:** RU (RU IBANs are 33 chars), ES (ES IBANs: `ES\d{22}`), global

IBANs appear in transcriptions of banking calls, which is a primary Krab Ear use case (call translation). ES IBAN: `ES\d{2}\d{20}` (22 digits after country+check). RU IBAN: `RU\d{2}\d{29}`.  
General IBAN regex: `\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b` — a mod-97 checksum exists but is complex; country prefix alone is a strong discriminator.

**Fix:** Add `iban` rule: `r"\b(?:ES\d{22}|RU\d{31}|DE\d{20}|[A-Z]{2}\d{2}[A-Z0-9]{11,29})\b"` with replacement `[IBAN]`.

---

## Finding 5 — Passport `\b\d{10}\b` pattern is too broad (HIGH)

**Category:** false positive / correctness  
**Rule:** `passport`

The current pattern `\b(?:\d{4}[\s\-]\d{6}|\d{10})\b` will match ANY bare 10-digit number: phone numbers without country code, partial card numbers, INN (12 chars), СНИЛС (11 chars). The `\b\d{10}\b` branch overlaps with dates (YYYYMMDDNN) and other numeric identifiers.

RU passport has additional context markers in spoken text: preceded by "серия", "паспорт", "№", etc. The bare 10-digit pattern without context is purely noise.

**Fix:** Either (a) require explicit context keyword lookahead: `r"(?:паспорт\w*\s+|\bсерия\s+)\d{4}[\s\-]\d{6}\b"`, or (b) remove the bare `\d{10}` branch and keep only the `\d{4}[\s\-]\d{6}` spaced form, which is far more specific.

---

## Finding 6 — IPv4/IPv6 addresses not redacted (LOW)

**Category:** infrastructure PII — internal network  
**Locale:** all

In transcriptions of technical calls/meetings, IP addresses (internal server IPs, VPN addresses) are PII under GDPR and common sense. IPv4 `\b\d{1,3}(?:\.\d{1,3}){3}\b` is a low-FP addition. IPv6 (`[0-9a-f:]{7,}::?[0-9a-f:]+`) is more complex but also common in network config discussions.

Currently no IP rule exists. IPv4 is the priority.

**Fix:** Add `ipv4` rule: `r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"` with replacement `[IP]`. IPv6 can be a follow-up.

---

## Finding 7 — OAuth / Bearer tokens and API keys not redacted (LOW)

**Category:** credentials  
**Locale:** EN/technical

In transcriptions of developer calls or screen-reader sessions, API keys and Bearer tokens may appear as spoken sequences or copy-pasted content. Patterns:
- Bearer token (JWT): `eyJ[A-Za-z0-9+/\-_]{20,}\.[A-Za-z0-9+/\-_]{20,}\.[A-Za-z0-9+/\-_=]{20,}`
- Generic API key heuristic: high-entropy alphanumeric strings 32–64 chars with mixed case and digits (hard to pattern reliably)

JWT is the most unambiguous: always starts with `eyJ` (base64 of `{"`) and has exactly 3 dot-separated segments.

**Fix:** Add `jwt_token` rule: `r"\beyJ[A-Za-z0-9+/\-_]{15,}\.[A-Za-z0-9+/\-_]{15,}\.[A-Za-z0-9+/\-_=]{10,}\b"` with replacement `[TOKEN]`. Generic API key detection is out of scope (too many false positives without a key prefix).

---

## Priority order for fixes

| Priority | Finding | Effort | Impact |
|----------|---------|--------|--------|
| 1 | F5 — passport `\d{10}` too broad | XS (pattern tweak) | HIGH (eliminates FPs) |
| 2 | F1 — SNILS no checksum | S (add validator fn) | HIGH (parallel to INN fix) |
| 3 | F2 — DNI/NIE missing | S (add rule) | MEDIUM (ES locale) |
| 4 | F3 — US SSN missing | XS (add rule) | MEDIUM (EN locale) |
| 5 | F4 — IBAN missing | S (add rule) | MEDIUM (financial calls) |
| 6 | F6 — IPv4 missing | XS (add rule) | LOW (technical meetings) |
| 7 | F7 — JWT missing | XS (add rule) | LOW (dev calls) |

## Not in scope / confirmed absent by design

- General-purpose NER (names, locations) — requires ML, out of scope for regex-only module
- RU ОГРНip / КПП — niche, audit wave if needed
- ES NIF (empresa) — subset of DNI pattern, same fix covers it
- Credit card 15-digit (Amex) / 19-digit — separate follow-up; Luhn already in place for 16-digit
