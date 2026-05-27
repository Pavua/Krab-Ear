# Wave 1060 — AbbreviationExpander Audit

**File:** `KrabEar/core/abbreviation_expander.py`  
**Date:** 2026-05-26  
**Auditor:** Sub-agent W1060

---

## Summary

`AbbreviationExpander` expands Russian/English/Spanish abbreviations in STT output. It is wired into production via `BackendService → TextProcessingService`. The implementation is generally sound: idempotent, thread-safe for `expand()`, and performs well (~12–28 ms for 20–65 k-char texts). Five findings were identified, ranging from a missing IPC endpoint to multi-sense ambiguity bugs that corrupt natural-language output.

---

## Findings

### F1 — `add_abbreviation` IPC method is absent (MEDIUM)

**Location:** `backend/service.py:1100–1102`, `backend/text_processing_service.py`

Three IPC methods are wired: `expand_abbreviations`, `remove_abbreviation`, `list_abbreviations`. The fourth method `add_abbreviation` — which exists in the Python class — has no IPC handler and no entry in the dispatch table. Users cannot add custom abbreviations from the Swift UI or any external caller without calling Python internals directly.

```python
# service.py dispatch table (lines 1100-1102) — add_abbreviation is absent
"expand_abbreviations": self._text_processing_svc.handle_expand_abbreviations,
"remove_abbreviation":  self._text_processing_svc.handle_remove_abbreviation,
"list_abbreviations":   self._text_processing_svc.handle_list_abbreviations,
# ← "add_abbreviation" missing
```

**Fix:** Add `handle_add_abbreviation` to `TextProcessingService` and wire it in `service.py`.

---

### F2 — Multi-sense abbreviations expand to wrong grammatical form (HIGH)

**Location:** `_BUILTIN_RU` table, `abbreviation_expander.py:48–61`

Several Russian abbreviations have multiple contextually-determined senses. The expander always uses one fixed expansion, producing incorrect output for common alternative meanings:

| Abbreviation | Fixed expansion | Alternative sense (unhandled) | Example failure |
|---|---|---|---|
| `гл.` | `глава` | `главный`, `главе`, `главы` | `По глава 5 закона` |
| `ред.` | `редактор` | `редакции` (law/document context) | `в редактор от 01.01.2023` |
| `д.` | `дом` | `дочь`, `действие` | `д. Петрова` → `дом Петрова` |
| `св.` | `святой` | `свежий`, `сведениям` | `по святой данным` |

These are grammatical corruption bugs — the expanded text is not just imprecise but actively wrong, which breaks LLM post-processing and readability scoring downstream.

**Verified with:**
```
expander.expand("По гл. 5 закона", "ru")   → "По глава 5 закона"
expander.expand("в ред. от 01.01.2023", "ru") → "в редактор от 01.01.2023"
expander.expand("по св. данным", "ru")      → "по святой данным"
```

**Fix options (in order of preference):**
1. Remove the most ambiguous entries (`гл.`, `св.`, `д.`, `ред.`) from builtins — they do more harm than good in STT context where abbreviated text is rare.
2. Add `no_after_digit` flag to `ред.` and rely on context patterns.
3. Long-term: context-window disambiguation (preceding noun triggers).

---

### F3 — `тыс.` / `обл.` lack `no_after_digit` guard (MEDIUM)

**Location:** `_BUILTIN_RU` table, `abbreviation_expander.py:56–61`

`тыс.` (тысяч), `млн.`, `млрд.`, `руб.`, `коп.`, and `обл.` never carry `no_after_digit`. In practice `тыс.` nearly always appears after a digit (`100 тыс.`) and *should* expand to `тысяч`, so this is harmless for those. But `обл.` after a region code number (`77 обл.`) expands incorrectly:

```
expander.expand("77 обл.", "ru") → "77 область"
```

Regional codes like `77 обл.` are standard in Russian government/document STT and should not be expanded. Adding `no_after_digit` to `обл.` is the correct fix.

---

### F4 — `_make_pattern` lookahead misses closing parenthesis `)` as trailing context (LOW)

**Location:** `_make_pattern()`, `abbreviation_expander.py:122`

The lookahead allows `)` after the abbreviation but the comment says "пробел/конец строки/пунктуация после". However `(т.е.)` correctly matches because `)` *is* in the character class `[,;:!?»)]`. This is actually correct behavior, but the pattern does NOT match abbreviations followed immediately by a period that ends a sentence (`т.е..` — double period), which is correct. The real gap is that `«` (opening guillemet) is not handled as a valid following character, whereas `»` is. This is a minor asymmetry; in STT output `«` would rarely follow an abbreviation.

More importantly: the lookahead character class contains a literal `$` (`|\$`), which in a non-multiline regex matches only the literal dollar sign, not end-of-string. End-of-string is already covered by `$` outside the alternation group (`(?=\s|$|[,;:!?»)])`). This is redundant but harmless.

**Note:** Word boundary behavior (W991 compound corruption class) was tested: `(?<!\w)` correctly prevents matching inside compound words like `программ.` or `педр.`. No compound corruption found.

---

### F5 — Performance is O(N×M) with no early-exit; adequate but not scalable (LOW)

**Location:** `expand()`, `abbreviation_expander.py:186–218`

The current approach calls `finditer` once per pattern (N=29 RU, 24 EN, 18 ES) across the full text. For a 60-minute meeting transcript (~64 k chars), this takes ~24 ms. For dense abbreviation input it reaches ~28 ms. Both are within acceptable budget for a post-processing step.

However, collected matches are sorted and deduplicated with `O(K log K)` (K = total match count), and the entire match list is built before any replacement. On pathological input with thousands of matches (e.g., repeated abbreviation text), peak memory and time could grow quadratically.

**Not blocking** for current use case (STT chunks are typically under 5 k chars). No action needed unless transcript sizes increase significantly.

---

## Wire Status

| IPC Method | Wired | Handler |
|---|---|---|
| `expand_abbreviations` | Yes | `TextProcessingService.handle_expand_abbreviations` |
| `remove_abbreviation` | Yes | `TextProcessingService.handle_remove_abbreviation` |
| `list_abbreviations` | Yes | `TextProcessingService.handle_list_abbreviations` |
| `add_abbreviation` | **No** | Missing — see F1 |

`AbbreviationExpander` is also used inline by `TextPostProcessor` (lazy-import in `core/text_postprocessor.py:127`), which is the pipeline path for STT output post-processing.

---

## Test Coverage

**File:** `KrabEar/tests/test_abbreviation_expander.py` — 470 lines, 8 test classes, ~42 test methods.

Coverage is good for: builtins (RU/EN/ES), custom CRUD, persistence, idempotency, concurrent `expand()`, Unicode, code-span and URL protection, longest-match-first ordering.

**Gaps:**
- No test for `ред.` in legal/document context (F2).
- No test for `обл.` after digit (F3).
- No test confirming `add_abbreviation` is reachable via IPC (F1 — only in-memory tested).
- No test for `гл.` before number (grammatical form, F2).

---

## Idempotency

Confirmed idempotent: running `expand()` twice on the same text produces identical output. Expanded words do not match any abbreviation patterns, so no re-expansion occurs.

---

## Recommendations (Priority Order)

1. **(HIGH, quick fix)** Remove `гл.`, `св.`, `д.`, `ред.` from `_BUILTIN_RU` or mark them as disabled pending context-aware handling — they cause grammatical corruption in common RU STT patterns.
2. **(MEDIUM)** Add `handle_add_abbreviation` IPC method to `TextProcessingService` + wire in `service.py`.
3. **(MEDIUM)** Add `no_after_digit` flag to `обл.` in `_BUILTIN_RU`.
4. **(LOW)** Add targeted tests for the F2/F3 scenarios to prevent regression.
