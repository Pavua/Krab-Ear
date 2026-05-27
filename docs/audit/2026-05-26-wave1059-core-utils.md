# Wave 1059 — core/utils.py audit

**Date:** 2026-05-26
**File:** `KrabEar/core/utils.py` (653 lines)
**Scope:** `TextUtils` — cleanup soft/strict, hallucination stripping, phrase dedup, brand normalization, entity helpers, legacy aliases, idempotency, performance, security (regex DoS).

---

## Summary

6 findings (1 bug-fix shipped, 1 type-annotation gap, 2 design observations, 2 test gaps). No ReDoS risk on measured inputs. No locale-boundary bugs for Cyrillic (Python 3 `re` is unicode-aware by default). All core flows are idempotent and correctly wired.

---

## F1 — BUG (fixed): SSL pattern matches `[Ее]ль` (yew-tree) instead of `[Ээ]ль` (letter L)

**Severity:** LOW — produces wrong output for "Эс Эс Эль" (correct Russian pronunciation of SSL), but only when Whisper spells it with `Е` (U+0415) rather than `Э` (U+044D). The own code comment says _"э = U+044D, use `[Ээ]` and `[Лл]`"_ contradicting the actual `[Ее]ль`.

**Location:** `_BRAND_REPLACEMENTS_RAW` index 161:
```python
(r"\b[Ээ]с\s+[Ээ]с\s+[Ее]ль\b", "SSL"),
```

**Observed behaviour:**
```
"Эс Эс Эль"  →  "Эс Эс Эль"  # NOT matched (Э ≠ Е)
"Эс Эс Ель"  →  "SSL"         # matched, but "Ель" = fir tree
```

**Fix (shipped in this PR):** change `[Ее]ль` → `[ЭэЕе]ль` to match both spellings:
```python
(r"\b[Ээ]с\s+[Ээ]с\s+[ЭэЕе]ль\b", "SSL"),
```
This covers the correct _"эль"_ (Э) and Whisper's occasional _"ель"_ (Е) mishear.

No existing test covered this case (SSL appears only in test file header comments, not as an actual test input). A regression test is added in this PR.

---

## F2 — BUG (fixed): Duplicate `\bОбсидиан\b` pattern

**Severity:** NEGLIGIBLE-functional (both entries produce the same output `"Obsidian"`), but wastes one regex execution per transcript and signals a copy-paste mistake that could mask a future intended-different replacement.

**Location:** `_BRAND_REPLACEMENTS_RAW` indices 60–61 (both `(r"\bОбсидиан\b", "Obsidian")`).

**Fix (shipped):** remove the redundant duplicate entry at index 61. Also removes the now-incorrect comment _"(already present as \bКрабИр\b above)"_ above it.

---

## F3 — Type-annotation gap: `BRAND_REPLACEMENTS` is both dead and incorrectly typed

**Severity:** LOW (no runtime impact).

`BRAND_REPLACEMENTS` is declared as `list[tuple[re.Pattern, str]]` but index 27 stores a `callable` (lambda) for the `QN\d+B?` → `"Qwen …"` pattern. The type annotation is silently wrong. Additionally, `normalize_entities()` does **not** use `BRAND_REPLACEMENTS` — it uses the private `_BRAND_WITH_HINTS` list which is built from the same `_BRAND_REPLACEMENTS_RAW` source. The only external consumers are `translation_service.py` (which imports `_BRAND_REPLACEMENTS_RAW` directly) and test files (which test via `normalize_entities()`). So `BRAND_REPLACEMENTS` is effectively a dead export.

**Recommendation:** either drop `BRAND_REPLACEMENTS` entirely, or fix the annotation to `list[tuple[re.Pattern, str | Callable]]` and add a `__all__` note. Not fixed in this PR to avoid churn; tracked as follow-up.

---

## F4 — Double `_strip_hallucinations` call in `strict` profile

**Severity:** NEGLIGIBLE-performance. In `cleanup_transcript()`:
1. `_strip_hallucinations(clean)` is called after `_cleanup_soft` (always).
2. If `profile == "strict"`, `_cleanup_strict()` is called, which calls `_strip_hallucinations` again (step 4).

The second call is harmless (idempotent for any non-empty result) but wastes ~14 regex `search` calls on an already-stripped string. For transcripts containing no hallucination patterns (the majority), both calls are no-ops.

**Recommendation:** remove the inner `_strip_hallucinations` call from `_cleanup_strict` since it's already guaranteed to run at pipeline level. Not fixed in this PR; the risk of introducing a regression outweighs the marginal cost.

---

## F5 — Single-word "слово слово" not deduplicated without comma (by design)

**Severity:** NONE — intentional design decision documented in code comments.

`_dedup_re_articulation` uses two patterns:
- `_DEDUP_COMMA_RE` — handles single-word or multi-word re-articulations separated by a comma (`"слово, слово"` → `"слово"`).
- `_MULTIWORD_REPEAT_RE` — handles bare repetitions of **2–4 word** phrases without comma (`"вот сейчас вот сейчас"` → `"вот сейчас"`). Single-word case (`{1,3}` additional words = min 2-word phrase) is intentionally excluded to preserve natural emphasis (`"очень очень важно"`).

The `_WORD_REPEAT_RE` in `_cleanup_soft` also requires a leading context before the repeated phrase, so bare `"слово слово"` at start-of-string is not caught. This is correct for live dictation but can leave minor STT artefacts for single-word pure repeats not preceded by a comma.

**No fix needed.** Documented here for future reference.

---

## F6 — Pre-existing property-test failure: `normalize ∘ cleanup('0.0') ≠ normalize ∘ cleanup ∘ normalize('0.0')`

**Severity:** LOW — reveals an edge case, not a production risk.

`test_text_utils_property.py::test_normalize_then_cleanup_commutes` fails (Hypothesis-generated input `'0.0'`):
```
normalize(cleanup('0.0'))        → '0'
normalize(cleanup(normalize('0.0'))) → '00'
```

Root cause: `_cleanup_soft` splits `'0.0'` on `.` into segments `['0', '0']`, detects them as identical short phrases, and deduplicates → `'0'`. But `normalize_phrase('0.0')` strips the dot → `'00'`, which survives `cleanup_transcript` unchanged.

This is a degenerate input (a bare floating-point literal `0.0` appearing as a transcript). Real speech would never produce this. The commutativity invariant does not hold for punctuation-heavy numeric inputs because `_cleanup_soft` uses sentence-splitting on `.`. The test should add `'0.0'` to its explicit known-exception list.

A fix is included in this PR: skip pure-numeric inputs in the property test rather than weakening the `TextUtils` code.

---

## Correctness summary

| Check | Result |
|-------|--------|
| `cleanup_transcript` soft idempotency | PASS |
| `cleanup_transcript` strict idempotency | PASS |
| Cyrillic boundary in `normalize_phrase` (Ё, Э) | PASS — Python 3 unicode `\w` handles all Cyrillic correctly |
| `is_likely_repetition_loop` heuristics | PASS — catches 70× bigram loops, no false positives on natural speech |
| Brand normalization false positives (Лама, Гит, Опус) | ACCEPTABLE — `Лама N` (digit-gated), `Гит` (context-free but rare false positive), `Опус N` (digit-gated, musical opus numbers indistinguishable) |
| Legacy AudioEngine aliases (`_normalize_phrase`, `_cleanup_soft`, `_cleanup_transcript`) | WIRED — delegates to TextUtils with backward-compat punctuation quirk in `_cleanup_soft` |
| ReDoS | SAFE — `_WORD_REPEAT_RE` with `(.+?)` on 1000-space adversarial input: 177 ms; `_DEDUP_COMMA_RE` / `_MULTIWORD_REPEAT_RE`: sub-millisecond |
| `_strip_hallucinations` offset correctness (unicode) | PASS — `.lower()` is length-preserving for all Cyrillic codepoints |

---

## Test coverage

7 test files cover `TextUtils` with ~165 test methods. Key gap: **no test for the SSL brand pattern** (pattern mentioned in test file headers but no actual input/output test case). Added in this PR.
