# TextAnonymizer Fifth-Pass Audit — W1470

**Date:** 2026-05-27
**Auditor:** W1470 (fifth-pass sub-agent)
**File:** `KrabEar/core/text_anonymizer.py`
**Base branch:** `codex/krab-ear-v2` (HEAD `f7086279`)

---

## Prior Wave Merge State

| Wave | Commit | Description | Status on `codex/krab-ear-v2` |
|------|--------|-------------|-------------------------------|
| W902 | `eeb78634` | Multi-lang phone + ИНН checksum | **MERGED** (confirmed `git merge-base`) |
| W1011 | `ff4aa321` | Audit docs (5 PII gap findings) | **MERGED** (docs only) |
| W1021 | `706afe6d` | Context-anchored RU passport + ES DNI/NIE | **NOT MERGED** |
| W1022 | `f0de9549` | СНИЛС + US SSN + IBAN with checksums | **NOT MERGED** |
| W1122 | `21b6a087` | Audit docs (7 new findings) | **MERGED** (docs only) |
| W1127 | `bf79c2b3` | EU phones +44/+49/+33/+39 | **MERGED** |
| W1128 | `de17b6aa` | ИНН ЮЛ 10-digit org TIN | **NOT MERGED** |
| W1276 | `e9c1192b` | Fourth-pass audit docs | **MERGED** (docs only) |
| W1280 | `b400b923` | Cyrillic SSN/IBAN tokens + IBAN spaces + СНИЛС unformatted + stale test fix | **NOT MERGED** |

**Current live rules in `codex/krab-ear-v2`:** `phone` (RU +7/8, +1), `phone_uk`, `phone_de`, `phone_fr`, `phone_it`, `email`, `credit_card` (Luhn), `passport` (RU), `date_of_birth`, `inn` (12-digit FL only), `snils` (formatted only).

**Accumulated backlog of unmerged fix commits:** W1021, W1022, W1128, W1280 — four concrete fix branches covering passport anchoring, DNI/NIE, IBAN, US SSN, СНИЛС unformatted, ИНН ЮЛ, and Cyrillic token naming.

---

## W1276 Findings Status on `codex/krab-ear-v2`

| ID | Finding | W1276 Status | Current Status |
|----|---------|--------------|----------------|
| F1 | `[SSN]`/`[IBAN]` tokens English (in W1022 branch) | OPEN | **OPEN** — W1022 not merged; W1280 (fixes F1+F2+F3) also not merged |
| F2 | IBAN spaced format not matched | OPEN | **OPEN** — W1280 not merged |
| F3 | СНИЛС unformatted 11-digit not matched | OPEN | **OPEN** — W1280 not merged |
| F4 | Russian tokens in ES/EN context (design) | OPEN | **OPEN** — no fix branch |
| F5 | Stale test `test_redact_credit_card_invalid_luhn_kept` failing in CI | OPEN | **FIXED** in W1280 (stale test deleted), but W1280 not merged |

---

## New Findings (W1470)

### F1 — HIGH: `passport` pattern `\d{10}` causes category mislabeling — 10-digit ИНН ЮЛ tagged as `[ПАСПОРТ]`

**Location:** `KrabEar/core/text_anonymizer.py`, line 163 (passport rule); W1128 unmerged

**Evidence:**

```python
# Current passport pattern (line 163):
r"\b(?:\d{4}[\s\-]\d{6}|\d{10})\b"
```

The bare `\d{10}` alternative has no checksum or context anchor. The ИНН ЮЛ (organisational TIN) is exactly 10 digits and passes its own checksum. Since W1128 (which adds an `inn_org` rule) is **not merged**, there is no `inn`-category rule for 10-digit numbers. The `passport` rule fires first, stamping real organisational TINs with the wrong category:

```python
>>> a.anonymize("ИНН организации 7707083893")  # Sberbank TIN
# Result: category='passport', text='ИНН организации [ПАСПОРТ]'
```

Downstream consumers that rely on `category == 'inn'` to handle TINs (e.g., audit logger, Obsidian sync frontmatter) will silently mis-classify. Users calling `anonymize(text, rules=["inn"])` receive 0 redactions for all corporate TINs — a **miss**, not a false positive.

Additionally, any 10-digit sequence (product codes, order numbers) triggers the passport rule:

```python
>>> a.anonymize("Заказ: 2024 123456")  # 4+6 digit order number
# Result: 1 redaction, category='passport'
```

**Fix:** Merge W1128 (adds `inn_org` rule for 10-digit with `_passes_inn_checksum`) and add context-anchor to passport rule per W1021. Until then, the ИНН ЮЛ false-category is a live data-quality defect.

---

### F2 — MEDIUM: `date_of_birth` regex matches **all** dates, not just dates of birth — 3 out of 4 transcription date contexts are false positives

**Location:** `KrabEar/core/text_anonymizer.py`, line 167 (date_of_birth rule)

**Evidence:**

```python
# Current pattern (line 167–169):
r"\b(?:0?[1-9]|[12]\d|3[01])[.\-/](?:0?[1-9]|1[0-2])[.\-/](?:19|20)\d{2}\b"
```

Pattern accepts any date from 01.01.1900 through 31.12.2099. Real transcription text routinely contains:

- Meeting dates: `Протокол совещания от 15.03.2024` → `[ДАТА_РОЖДЕНИЯ]`
- Document dates: `Договор от 01.01.2023` → `[ДАТА_РОЖДЕНИЯ]`
- Future scheduled dates: `Встреча 15.06.2025` → `[ДАТА_РОЖДЕНИЯ]`
- Historical dates: `Открытие 09.05.1945` → `[ДАТА_РОЖДЕНИЯ]`

In a 4-sentence business transcript with one actual DOB and three document dates, all 4 dates are tagged `[ДАТА_РОЖДЕНИЯ]`. This causes two interrelated problems:

1. **Privacy over-redaction:** non-PII content is removed from transcript.
2. **Misleading category label:** `[ДАТА_РОЖДЕНИЯ]` in position of document date confuses downstream diff/versioning (`transcript_versioning.py`) and LLM rewriter context.

The rule name `date_of_birth` implies DOB-specific detection, which the pattern does not enforce.

**Fix options:**
- Context-anchor the pattern: require preceding keyword context (`дата рождения`, `ДР`, `DOB`, `fecha de nacimiento`, `born`) within ±50 characters.
- Or rename the rule to `date` to accurately document scope (low-risk, just a label fix).
- A combined approach: `date_of_birth` rule with context anchor + separate `date` rule (no anchor) that is not applied by default.

---

### F3 — MEDIUM: `phone_fr` false negative for common informal `+33 0X` format — transcribed speech PII leak

**Location:** `KrabEar/core/text_anonymizer.py`, lines 129–135 (phone_fr rule)

**Evidence:**

```python
# Current phone_fr pattern (line 133):
r"\+33[\s\-]?(?:\(0\))?[\s\-]?[1-9](?:[\s\-]?\d{2}){4}"
```

The pattern accepts `(?:\(0\))?` — a bracketed zero literal — then requires `[1-9]` as the first subscriber digit. This correctly handles formal international notation `+33 6 12 34 56 78`. However, transcriptions frequently produce the informal form where users orally dictate the full French number including the domestic `0` prefix:

```
+33 06 12 34 56 78   # informal — very common in French audio transcripts
```

After `+33`, the next character is `0` which matches neither `\(0\)` nor `[1-9]`, so the pattern silently fails:

```python
>>> a.anonymize("+33 06 12 34 56 78", rules=["phone_fr"])
# Result: 0 redactions  ← PII leak
>>> a.anonymize("+33 6 12 34 56 78", rules=["phone_fr"])
# Result: 1 redaction   ← correct
```

The `+33(0)6` bracketed form matches (because `(0)` is consumed by `(?:\(0\))?`) but `+33 06` fails. This is a transcription-reality mismatch: Whisper/GigaAM often transcribes "plus trente-trois zéro six..." → `+33 06...`.

**Fix:** Replace `(?:\(0\))?[\s\-]?[1-9]` with `(?:(?:\(0\))|0)?[\s\-]?[1-9]` — allow bare `0` prefix too — then require the next digit to be 1-9 (mobile 6/7, landline 1-5/8/9):

```python
r"\+33[\s\-]?(?:(?:\(0\))|0)?[\s\-]?[1-9](?:[\s\-]?\d{2}){4}"
```

---

### F4 — LOW: `snils` rule has no checksum validation — arbitrary `XXX-XXX-XXX XX` numbers are false positives

**Location:** `KrabEar/core/text_anonymizer.py`, line 179 (snils rule)

**Evidence:**

```python
# Current SNILS pattern (line 181):
r"\b\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]?\d{2}\b"
```

Unlike `credit_card` (Luhn) and `inn` (`_passes_inn_checksum`), the `snils` rule has no checksum validation. СНИЛС has a well-defined control digit algorithm. Without it, any `DDD-DDD-DDD DD` sequence triggers redaction:

```python
>>> a.anonymize("Трек-код: 123 456 789 01", rules=["snils"])
# Result: 1 redaction — tracking code falsely redacted

>>> a.anonymize("Код 123-456-789 01 конец", rules=["snils"])
# Result: 1 redaction — generic code falsely redacted
```

The fix branches W1022 and W1280 both planned to add `_SNILS_DETAIL_RE` validation, but neither is merged, so the main branch has zero protection against false positives in the `snils` rule.

**СНИЛС checksum algorithm for reference:**

```python
def _passes_snils_checksum(digits: str) -> bool:
    """digits = 11-char string (separators stripped)."""
    if len(digits) != 11:
        return False
    n = sum(int(digits[i]) * (9 - i) for i in range(9))
    while n > 101:
        n %= 101
    if n in (100, 101):
        n = 0
    return n == int(digits[9:11])
```

**Fix:** Add `_passes_snils_checksum` and apply it in `anonymize()` alongside the existing `credit_card`/`inn` checksum gates (the same `if name == "snils": if not _passes_snils_checksum(...): continue` pattern).

---

### F5 — LOW: `list_rules()` allows duplicate rule names via `add_custom_rule()` — silent filter breakage

**Location:** `KrabEar/core/text_anonymizer.py`, lines 292–306 (`add_custom_rule`)

**Evidence:**

```python
def add_custom_rule(self, name: str, pattern: str, replacement: str) -> None:
    compiled = re.compile(pattern, re.IGNORECASE)
    self._custom_rules.append((name, compiled, replacement))
```

No deduplication check is performed. If a caller adds a custom rule with the same name as a builtin (e.g., `name="phone"`), `list_rules()` returns two entries named `phone`. More critically, `anonymize(text, rules=["phone"])` now applies **both** rules — the builtin and the custom one — because `rule_set = set(rules)` and `n in rule_set` matches both:

```python
>>> a.add_custom_rule("phone", r"\+7\d+", "[CUSTOM]")
>>> a.list_rules().count("phone")
# → 2  (duplicate name in list)
```

This silently doubles redaction work. If the custom rule is narrower than the builtin, it wins only when the builtin didn't match — but with the same name, users cannot remove just the custom rule via `rules=` filter. The `list_rules()` return also becomes non-unique, breaking any caller that uses it as a unique-rule index.

**Fix:** Add a dedup guard in `add_custom_rule`:

```python
def add_custom_rule(self, name: str, pattern: str, replacement: str) -> None:
    all_names = {n for n, _, _ in self._rules + self._custom_rules}
    if name in all_names:
        raise ValueError(f"Rule name {name!r} already exists. Use a unique name.")
    compiled = re.compile(pattern, re.IGNORECASE)
    self._custom_rules.append((name, compiled, replacement))
```

---

## Summary

| ID | Severity | Category | Root Cause |
|----|----------|----------|------------|
| F1 | HIGH | Category mislabeling: 10-digit ИНН ЮЛ tagged as `[ПАСПОРТ]` | W1128 not merged; passport `\d{10}` has no checksum |
| F2 | MEDIUM | `date_of_birth` redacts all dates, not just DOB | Pattern too broad; no context anchor |
| F3 | MEDIUM | `phone_fr` misses `+33 06...` informal format | Regex requires `[1-9]` after optional `(0)`, bare `0` fails |
| F4 | LOW | `snils` no checksum validation → false positives | W1022/W1280 not merged |
| F5 | LOW | `add_custom_rule` allows duplicate names → silent double-apply | No dedup guard in API |

---

## Merge Recommendations

Priority order:

1. **Merge W1127** (EU phones — +44/+49/+33/+39): already confirmed merged. ✓
2. **Merge W1128** (ИНН ЮЛ 10-digit): directly fixes F1 category mislabeling. Rebase needed.
3. **Merge W1280** (Cyrillic tokens + IBAN spaces + СНИЛС unformatted + stale test fix): fixes W1276 F1/F2/F3/F5 residuals. Rebase needed.
4. **Merge W1021** (passport context anchor + DNI/NIE): reduces passport false positives. Rebase needed.
5. **Fix W1470 F3** (phone_fr `+33 06`): one-line regex change, new wave.
6. **Fix W1470 F4** (СНИЛС checksum): add `_passes_snils_checksum` + gate in `anonymize()`.
7. **Fix W1470 F5** (`add_custom_rule` dedup): add ValueError guard.
8. **W1470 F2** (date_of_birth context): larger design decision — context-anchoring vs rename. Defer to separate wave.

**W1022** (IBAN + US SSN): hold until W1280 Cyrillic token fixes are merged (token naming consistency gate).
