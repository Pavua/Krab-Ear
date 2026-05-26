# Wave 1122 — TextAnonymizer Residual PII Gap Re-audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/text_anonymizer.py`  
**Scope:** NEW gaps only — re-audit AFTER W902/W1011/W1021/W1022. Does NOT re-report items already covered by those waves.

---

## Merge State of Prior Fixes

| Wave | Branch | Status on `codex/krab-ear-v2` |
|------|--------|-------------------------------|
| W902 | `origin/feature/fix-anonymizer-phones-W902` | **NOT MERGED** — ES +34, US +1 phones + ИНН checksum are absent from main |
| W1011 | `origin/audit-text-anonymizer-W1011` (docs only) | **NOT MERGED** — 7-finding audit doc not on main |
| W1021 | `origin/fix-text-anonymizer-passport-dni-W1021` | **NOT MERGED** — context-anchored passport + ES DNI/NIE absent from main |
| W1022 | `origin/fix-text-anonymizer-snils-ssn-iban-W1022` | **NOT MERGED** — SNILS checksum, US SSN, IBAN absent from main |

All four fix branches exist on remote and contain correct implementations. They require merge into `codex/krab-ear-v2` before any W1122 fixes are prioritized.

Additionally, W1011 findings F6 (IPv4) and F7 (JWT tokens) have **no fix branch** — still fully open.

**Effective baseline for W1122:** `codex/krab-ear-v2` HEAD = v2.0.5, which has only Luhn credit-card validation (W214) and no other anonymizer improvements since the wave-110 test coverage PR.

---

## New Findings

### Finding N1 — International phone prefixes +44/+49/+33/+39 absent (HIGH)

**Category:** phone — international PII  
**Locale:** EN (UK), DE, FR, IT — all supported transcription languages

W902 added ES `+34` and US/CA `+1` formats to the phone rule. However, the following major ITU country codes remain uncovered:

| Prefix | Country | Typical format |
|--------|---------|----------------|
| `+44` | United Kingdom | `+44 7700 900123` (mobile), `+44 20 7946 0958` (landline) |
| `+49` | Germany | `+49 30 12345678` (Berlin landline), `+49 151 12345678` (mobile) |
| `+33` | France | `+33 6 12 34 56 78` (mobile), `+33 1 23 45 67 89` (Paris landline) |
| `+39` | Italy | `+39 06 12345678`, `+39 333 1234567` |
| `+61` | Australia | `+61 2 9876 5432`, `+61 412 345 678` |

None of these match the current phone rule on `codex/krab-ear-v2`. Since W902 is unmerged, even `+34` and `+1` are absent.

**Suggested fix:**

Add a generic international phone branch covering the unhandled prefixes:

```python
# International: +[2-9]X[X] followed by 6-14 digits (covers +44, +49, +33, +39, +61 etc.)
r"\+(?:44|49|33|39|61|81|82|86|91|55)[\s\-]?(?:\(?\d+\)?[\s\-]?){2,5}\d{2,4}"
```

This should be added alongside the W902 ES/US branches, not instead of them.

---

### Finding N2 — ИНН ЮЛ (10-digit organization TIN) not covered (HIGH)

**Category:** RU government identifier — business PII  
**Locale:** RU (primary)

The current `inn` rule is `\b\d{12}\b`, which matches only **individual** ИНН (12 digits). Russian legal entities use a **10-digit** ИНН (e.g., Sberbank: `7736207543`, Yandex: `7736033476`). These are the most common TINs in business call transcriptions (invoicing, contract discussions, banking calls).

The 10-digit org ИНН also has a checksum (W902 added the `_passes_inn_checksum` helper that handles both 10- and 12-digit variants, but W902 is unmerged). On the current codebase, `\b\d{10}\b` would conflict with the passport rule's bare 10-digit branch (itself a known false-positive source fixed in W1021).

**False positive risk:** High. Standalone 10-digit sequences (order IDs, zip+4 codes, tracking numbers) would match. The fix requires both the W902 checksum helper AND the W1021 passport context-anchor fix to be merged first, so the `inn` rule can safely add a `\b\d{10}\b` branch validated by checksum.

**Suggested fix:**

After W902 and W1021 are merged:

```python
# ИНН: 12-digit (физлицо) or 10-digit (ЮЛ) — both with checksum validation
(
    "inn",
    r"\b(?:\d{10}|\d{12})\b",
    "[ИНН]",
),
```

With checksum validation in `anonymize()`:

```python
elif name == "inn":
    digits = m.group(0).strip()
    if not _passes_inn_checksum(digits):
        continue
```

---

### Finding N3 — American Express 15-digit card not covered (MEDIUM)

**Category:** financial — credit card PII  
**Locale:** all

The `credit_card` rule matches only:
1. `\b(?:\d{4}[\s\-]){3}\d{4}\b` — 4-4-4-4 grouped (16 digits)
2. `\b\d{16}\b` — raw 16 digits

American Express cards use **15 digits** in a **4-6-5** grouping: `3714 496353 98431`. Union Pay cards can be **19 digits**.

The Luhn validation helper `_passes_luhn()` already exists and works correctly for any length — the gap is purely in the regex pattern.

**Suggested fix:**

Extend the credit_card pattern to include the Amex format:

```python
(
    "credit_card",
    r"\b(?:\d{4}[\s\-]){3}\d{4}\b"        # Visa/MC 4-4-4-4
    r"|\b\d{4}[\s\-]\d{6}[\s\-]\d{5}\b"   # Amex 4-6-5
    r"|\b\d{16}\b"                          # raw 16-digit
    r"|\b\d{15}\b",                         # raw 15-digit (Amex)
    "[КАРТА]",
),
```

Luhn validation already gates all matches, so the additional `\b\d{15}\b` branch has low false-positive risk.

---

### Finding N4 — License plate formats absent (MEDIUM)

**Category:** vehicle PII — location/identity inference  
**Locale:** RU, ES (primary)

Vehicle registration plates appear in transcriptions of traffic incidents, insurance calls, parking disputes, and law enforcement interactions. Neither RU nor ES plate formats are covered:

- **RU plate:** Cyrillic letter + 3 digits + 2 Cyrillic letters + 2–3-digit region code.  
  Pattern: `\b[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}\b`  
  Example: `А123ВС77`, `М001ОС97`

- **ES plate (post-2000):** 4 digits + 3 consonants (no vowels, no Ñ/Q/CH/LL).  
  Pattern: `\b\d{4}\s?[BCDFGHJKLMNPRSTUVWXYZ]{3}\b`  
  Example: `1234 BCD`, `5678FGH`

The RU pattern uses Cyrillic letters that visually resemble Latin counterparts (А, В, Е, К, М, Н, О, Р, С, Т, У, Х) — transcription engines may emit either depending on ASR model language mode.

**Suggested fix:**

Add two rules, `license_plate_ru` and `license_plate_es`, with Unicode flag enabled for the Cyrillic pattern. Mark as MEDIUM priority — lower than phone/INN but relevant for the primary RU locale.

---

### Finding N5 — IPv6 address absent (LOW)

**Category:** infrastructure PII  
**Locale:** technical, all

W1011 F6 identified IPv4 as a gap (LOW priority, no fix branch created yet). IPv6 is also absent. Full `[0-9a-fA-F]{1,4}` colon-notation addresses appear in transcriptions of DevOps calls and network-configuration discussions.

IPv6 detection is harder than IPv4 due to the many valid abbreviation forms (`::`, `::1`, `2001:db8::/32`). A practical regex for full 8-group addresses:

```python
r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
```

This covers the most common spoken form while avoiding the complexity of all RFC 5952 compressed forms. IPv4-mapped IPv6 (`::ffff:192.0.2.1`) is handled separately.

**Dependency:** IPv4 (W1011 F6) should be implemented first; IPv6 can follow as a companion rule.

---

### Finding N6 — MAC address absent (LOW)

**Category:** infrastructure PII — hardware identifier  
**Locale:** technical, all

MAC addresses (`00:1B:44:11:3A:B7` or `00-1B-44-11-3A-B7`) uniquely identify network interfaces and appear in transcriptions of IT support calls, router configuration sessions, and security incident reports.

Pattern: `\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b`

False positive risk is low — the colon/dash-separated 6-group structure is highly distinctive and unlikely to appear in natural speech transcription except when genuinely dictating a MAC address.

---

### Finding N7 — SWIFT/BIC code absent (LOW)

**Category:** financial — bank routing  
**Locale:** financial calls, all

SWIFT/BIC codes (e.g., `DEUTDEDB`, `SABRRUMM`) uniquely identify financial institutions and appear in transcriptions of international wire transfer instructions. Format: 4-letter bank code + 2-letter country + 2-char location + optional 3-char branch.

Pattern: `\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b`

**False positive risk:** MEDIUM. The 8–11 uppercase-letter pattern can match words, acronyms, or version strings. Recommend keyword context anchoring (`r"(?:SWIFT|BIC|код\s+банка)[:\s]+[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?"`) to reduce false positives.

---

## Items Explicitly Assessed and Not Reported

| Category | Assessment |
|----------|------------|
| BR CPF / CNPJ | Brazil is not a primary locale. CPF `\b\d{3}\.\d{3}\.\d{3}-\d{2}\b` is structurally safe but out of project scope. Recommend as custom rule documentation instead. |
| CN ID (18-digit) | China is not a primary locale. Pattern `\b\d{17}[\dXx]\b` has HIGH collision with other 18-digit numeric identifiers. Out of scope. |
| URL redaction | Internal URLs with credentials in path/query (`https://server/api?token=...`) are not speech-transcription PII in the Krab Ear use case; the URL text itself is user-visible content, not PII. Out of scope. |
| Email edge cases | RFC-5321 quoted local parts and IDN domains are not generated by any current STT engine output; existing pattern has no actionable gaps for the transcription use case. |

---

## Priority Matrix

| # | Finding | Effort | Impact | Merge prerequisite |
|---|---------|--------|--------|--------------------|
| N1 | Phone +44/+49/+33/+39 | S | HIGH | W902 should merge first |
| N2 | ИНН ЮЛ 10-digit | S | HIGH | W902 + W1021 must merge first |
| N3 | Amex 15-digit CC | XS | MEDIUM | none (Luhn already present) |
| N4 | License plates RU/ES | S | MEDIUM | none |
| N5 | IPv6 | XS | LOW | W1011 F6 (IPv4) first |
| N6 | MAC address | XS | LOW | none |
| N7 | SWIFT/BIC | XS | LOW | none (add context anchor) |

**Recommended merge sequence:**
1. Merge W902 → W1021 → W1022 into `codex/krab-ear-v2` (unblocks N1, N2 prerequisites)
2. Fix W1011 F6 (IPv4) + F7 (JWT) — still have no fix branches
3. N3 (Amex) — trivial, no dependencies
4. N1 (more phone prefixes) — after W902 merged
5. N2 (INN org 10-digit) — after W902 + W1021 merged
6. N4 (plates), N5 (IPv6), N6 (MAC), N7 (SWIFT) — batch as a single small PR
