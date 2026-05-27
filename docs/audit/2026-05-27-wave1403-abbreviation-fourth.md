# W1403 — AbbreviationExpander Fourth-Pass Audit

**Date:** 2026-05-27  
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)  
**Files audited:**  
- `KrabEar/core/abbreviation_expander.py`  
- `KrabEar/backend/text_processing_service.py`  
- `KrabEar/backend/service.py` (handler dispatch table)  
- `KrabEar/core/text_postprocessor.py`  
- `KrabEar/tests/test_abbreviation_expander.py`

---

## Merge State of Previous Waves

| Wave | Commit | Title | Merged into `codex/krab-ear-v2`? |
|------|--------|-------|----------------------------------|
| W1081 | `1b14fe94` | Drop ambiguous defaults + opt-in flag | **NOT merged** |
| W1118 | `82625bbb` | Wire `add_abbreviation` IPC handler | **NOT merged** |
| W1119 | `7095490c` | Add RLock for concurrent ops | **NOT merged** |

All three commits exist on remote branches (`fix-abbreviation-expander-W1081`, `wire-add-abbreviation-W1118`, `fix-abbreviation-lock-W1119`) but have not been merged into the main branch. The current `codex/krab-ear-v2` still carries all three pre-fix issues.

---

## New Findings (5 cap)

### F1 — CRIT: `add_abbreviation` IPC handler missing from dispatch table

**Severity:** HIGH  
**Status:** Regression — W1118 fix exists on a branch but is not merged.

`AbbreviationExpander.add_abbreviation()` exists and is tested, but has no corresponding IPC handler in `TextProcessingService` or the `service.py` dispatch table. The dispatch table (lines 1100–1102) wires only three of the four CRUD operations:

```python
"expand_abbreviations": self._text_processing_svc.handle_expand_abbreviations,
"remove_abbreviation":  self._text_processing_svc.handle_remove_abbreviation,
"list_abbreviations":   self._text_processing_svc.handle_list_abbreviations,
# "add_abbreviation" is ABSENT
```

A client calling `add_abbreviation` via IPC gets a `method not found` error. The method name is referenced in tests (`KrabEar/tests/test_abbreviation_expander.py` lines 178, 185, 191, etc.) but only at the unit level, not through IPC. This makes the feature incomplete: users can list and delete abbreviations but cannot add new ones via IPC.

**Fix:** Wire `handle_add_abbreviation` in `TextProcessingService` and add the dispatch entry between `expand_abbreviations` and `remove_abbreviation` (as done in unmerged W1118 commit `82625bbb`).

---

### F2 — HIGH: Ambiguous RU builtins still expand unconditionally (`св.`, `гл.`)

**Severity:** HIGH  
**Status:** Regression — W1081 fix exists on a branch but is not merged.

Two builtin RU abbreviations have multiple valid meanings but expand to a single hardcoded value:

- `св.` → `"святой"` (but also means "свежий", "связной", "свободный" in context)  
- `гл.` → `"глава"` (but also means "главный" — more frequent in professional contexts)

Verified live behaviour:
```
expand("Пост гл. редактора") → "Пост глава редактора"   # wrong: should be "главный"
expand("продукты св. рынка") → "продукты святой рынка"  # wrong: corrupt output
```

The W1081 fix splits builtins into `_BUILTIN_RU_UNAMBIGUOUS` and `_BUILTIN_RU_AMBIGUOUS`, adds an `expand_ambiguous: bool = False` constructor flag, and leaves ambiguous entries visible in `list_abbreviations` but skipped in `expand()` by default. None of that is present in the current branch.

**Fix:** Merge W1081 (`1b14fe94`) which already implements the opt-in split pattern.

---

### F3 — MED: No RLock — concurrent IPC calls can corrupt `_compiled` cache

**Severity:** MED  
**Status:** Regression — W1119 fix exists on a branch but is not merged.

`AbbreviationExpander._abbrevs` and `_compiled` are plain Python dicts with no lock. The backend IPC server is multi-threaded; simultaneous calls to `add_abbreviation` (writes to `_abbrevs`/`_compiled`) and `expand()` (reads from `_compiled`) produce a data race. Python's GIL prevents segfaults but does not prevent dict iteration errors (`RuntimeError: dictionary changed size during iteration`) or stale reads where a partial `_compiled` list is iterated mid-rebuild.

The W1119 fix (`7095490c`) adds `threading.RLock`, wraps all mutating operations (`add_abbreviation`, `remove_abbreviation`, `_rebuild_compiled`, `_save_custom`) and snapshots `_compiled[lang]` inside `expand()` before iteration.

**Fix:** Merge W1119 (`7095490c`).

---

### F4 — MED: `ExpandAbbreviations` step in `text_postprocessor.py` hardcodes `language="ru"`, bypasses caller's locale

**Severity:** MED  
**Status:** New finding (not covered by W1060/W1081/W1111 audits).

`text_postprocessor.py` line 182 instantiates the default `ExpandAbbreviations` step with a fixed `language="ru"`:

```python
_BUILTIN_STEPS = {
    ...
    "expand_abbreviations": ExpandAbbreviations(language="ru"),
    ...
}
```

The `TextPostProcessor.process()` method accepts arbitrary `steps` but the singleton step object always calls `AbbreviationExpander.expand(text, language="ru")`. When `post_process_text` is called by the IPC handler for a Spanish or English transcript (e.g., an ES-language session), abbreviations are looked up in the RU dictionary, silently failing to expand ES abbreviations (`p.ej.`, `Sr.`, etc.) or — if the transcript contains RU-shaped tokens — spuriously expanding them.

The `handle_post_process_text` IPC handler does not pass `language` to `process()`, and the step has no way to inherit it. The caller cannot override the step's locale without re-registering a custom `ExpandAbbreviations(language=lang)` step.

**Fix:** Add a `language` param to `TextPostProcessor.process()` that is threaded into `ExpandAbbreviations.process()` (or make `ExpandAbbreviations` stateless, receiving language per-call). The IPC handler should forward `params.get("language", "ru")` to the processor.

---

### F5 — LOW: `гл.` lacks `no_after_digit` guard — chapter context incorrectly expands before numerals

**Severity:** LOW  
**Status:** New finding.

In Russian, `гл. 5` means "chapter 5" and `гл. 5` should likely not be expanded (chapter references are typically kept abbreviated). The current `no_after_digit` flag only suppresses expansion when a digit appears **before** the abbreviation (e.g., `5 гл.`). There is no guard for when a digit follows, so `"читаем гл. 5"` becomes `"читаем глава 5"` — grammatically acceptable but semantically different from the intent.

```
expand("читаем гл. 5", "ru") → "читаем глава 5"   # chapter ref should stay compact
```

Additionally, `гл.` as "главный" (adjective, e.g., "гл. инженер") expands to "глава" (noun), producing grammatically wrong output: `"Глава инженер"` instead of `"Главный инженер"`. This is a symptom of F2 (ambiguous entry) but also an independent syntactic bug — even if "глава" were the only meaning, the expansion `"глава инженер"` is ungrammatical Russian.

**Fix (short-term):** Add `"no_after_digit"` flag to `гл.` so chapter references (e.g., `гл. 5`) are not expanded. **Fix (long-term):** Remove `гл.` from default builtins entirely and add it to the ambiguous set per F2/W1081.

---

## Coverage Gaps

- `test_abbreviation_expander.py` has no test for `handle_add_abbreviation` IPC roundtrip (because the handler does not exist yet — F1).
- `text_postprocessor.py` has no test for locale override in `expand_abbreviations` step (F4).
- Concurrent `add_abbreviation` + `expand()` stress test is absent (F3 — the lock is not present, so the test would be risky without the fix).

---

## Summary Table

| # | Severity | Description | Status |
|---|----------|-------------|--------|
| F1 | HIGH | `add_abbreviation` IPC handler missing | W1118 not merged |
| F2 | HIGH | Ambiguous RU builtins `св.`/`гл.` expand unconditionally | W1081 not merged |
| F3 | MED | No RLock — concurrent IPC race on `_compiled` | W1119 not merged |
| F4 | MED | `ExpandAbbreviations` hardcodes `language="ru"` in post-processor | New |
| F5 | LOW | `гл.` lacks `no_after_digit` guard; chapter refs expand incorrectly | New |

**Action:** Merge W1081 + W1118 + W1119 branches first (resolves F1–F3), then address F4–F5 in a follow-up wave.
