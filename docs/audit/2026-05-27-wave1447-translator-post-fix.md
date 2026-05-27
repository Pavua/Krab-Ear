# Wave 1447 — Translator Post-Fix Audit (W1428/W1429/W1430)

**Date:** 2026-05-27  
**Branch:** `codex/krab-ear-v2` (after merge of PR #1327 / PR #1336 / PR #1335)  
**Auditor:** W1447 sub-agent (read-only)

---

## Merge State Verification

| Wave | PR | Title | Merged to `codex/krab-ear-v2` |
|------|-----|-------|-------------------------------|
| W1428 | #1327 | remove duplicate `clear_cache` + `_check_privacy_mode_changed` | **YES** (commit `fcc0e6b3`) |
| W1429 | #1336 | wire `TranslationCache` in `BackendService` + clear IPC | **YES** (commit `1c6efb64`) |
| W1430 | #1335 | `_apply_glossary` word boundaries + `IGNORECASE` | **YES** (commit `7cc21af1`) |

All three fixes are present in `origin/codex/krab-ear-v2` HEAD as of 2026-05-27.

### W1428 — Duplicate method removal

Confirmed: `KrabEar/backend/translator.py` now has a **single** `clear_cache()` (line 148) that acquires `_cache_lock` and calls `_translation_cache.clear()` when injected. The old no-arg `_check_privacy_mode_changed()` that silently ignored `_translation_cache` is gone. The new unified implementation at line 166 correctly handles both the no-argument form (via `_settings_getter`) and the explicit bool form (for tests/BackendService).

### W1429 — TranslationCache wiring

Confirmed in `KrabEar/backend/service.py`:
- Line 69: `from backend.translation_cache import TranslationCache`
- Lines 211–212: `self._translation_cache = TranslationCache(data_dir=str(store.data_dir))` and `self.translator._translation_cache = self._translation_cache`
- Line 1726: `_handle_clear_translation_cache` IPC handler operational

In `translator.py`, `_translate_impl()` now checks `_tc = self._translation_cache` before every disk lookup/put (lines 306–384).

### W1430 — Word boundary + IGNORECASE

Confirmed: `_apply_glossary()` at line 850 uses `re.sub(r"\b" + re.escape(source) + r"\b", target, result, flags=re.IGNORECASE)`.

---

## New Residual Findings (Post-Fix)

### F1 — HIGH: `_apply_glossary` replacement target not escaped — `re.error` propagates uncaught

**File:** `KrabEar/backend/translator.py`, lines 858–865  
**Root cause:** `re.sub()` interprets backslash sequences in the replacement string. W1430 correctly escapes the *source* pattern via `re.escape()` but does NOT escape the *target* (replacement). A glossary value containing `\1`, `\U`, or other backslash sequences causes `re.error` that propagates uncaught out of `_apply_glossary()` → `_apply_glossary_to_result()` → `_translate_impl()` → `translate()`.

**Concrete reproduction:**

```python
glossary = {"world": r"C:\Users"}   # Windows path in glossary
Translator._apply_glossary("Hello world", glossary)
# -> re.error: bad escape \U at position 2
```

**Impact:** Any user with a Windows path, LaTeX snippet, or regex-like string in a glossary value gets a translation crash that silently returns no result (the caller may see an empty translation with no error message).

**Fix:** Use a lambda replacement to bypass `re.sub`'s backslash interpretation:
```python
result = re.sub(
    r"\b" + re.escape(source) + r"\b",
    lambda _m: target,   # target treated as literal string
    result,
    flags=re.IGNORECASE,
)
```

**Test coverage gap:** `test_translator_glossary_boundary_W1430.py` tests `re.escape` on the source but has no test for backslash content in the glossary *value* (target).

---

### F2 — MED: `_apply_glossary` silently breaks compound terms when shorter overlapping entry iterates first

**File:** `KrabEar/backend/translator.py`, lines 858–865  
**Root cause:** Glossary entries are applied sequentially in dict insertion order. If a shorter term (e.g., `"network"`) appears before a longer compound term (e.g., `"neural network"`), the shorter term is replaced first, leaving `"neural сеть"` that no longer matches the compound pattern.

**Concrete reproduction:**

```python
glossary = {"network": "сеть", "neural network": "НС"}  # shorter before longer
Translator._apply_glossary("neural network learning", glossary)
# -> "neural сеть learning"   (compound term never matches)
```

**Impact:** Multi-word glossary entries with shared sub-terms produce wrong output depending on insertion order. The user cannot control iteration order without knowing the internal implementation.

**Fix:** Sort glossary entries by descending length before applying, so longer (more specific) patterns are matched first.

**Note:** The *previous* `str.replace()` implementation had the same issue. W1430 did not introduce this bug, but it remains unaddressed.

---

### F3 — MED: `\b` boundary silently fails for terms ending or starting with non-word characters

**File:** `KrabEar/backend/translator.py`, line 861  
**Root cause:** Python's `re` module `\b` assertion requires a transition between a word character (`\w`) and a non-word character (`\W`). When a glossary term ends with a non-word character (e.g., `C++`, `.NET`, `etc.`, `co.`), the trailing `\b` looks for a word→non-word transition after a non-word character, which can never occur. The pattern therefore never matches.

**Concrete verification:**

| Term | Old `str.replace` matched? | New `\b...\b` matches? | Regression? |
|------|---------------------------|----------------------|-------------|
| `C++` | Yes | No | **YES** |
| `.NET` | Yes | No | **YES** |
| `etc.` | Yes | No | **YES** |
| `co.` | Yes | No | **YES** |

**Impact:** Any glossary entry for a common technical term or abbreviation ending with punctuation (`C++`, `.NET`, `co.`, etc.) is silently ignored after W1430. This is a behavioral regression: these terms were successfully replaced before W1430.

**Fix options:**
1. For terms that start and end with word chars: use `\b...\b` (current behavior, correct).
2. For terms that start or end with non-word chars: fall back to context-aware lookaround, e.g., `(?<!\w)TERM(?!\w)` using `re.escape(source)`.
3. Universal approach: use `(?<!\w)TERM(?!\w)` for all terms (equivalent for pure-word terms, correct for punctuation-bounded terms).

---

### F4 — LOW: `_check_privacy_mode_changed()` dual-mode implementation has untested getter path

**File:** `KrabEar/backend/translator.py`, lines 166–204  
**Root cause:** The method has two code paths: (a) called with no argument — reads privacy mode via `_settings_getter` (injected at runtime); (b) called with explicit `bool` — uses the passed value directly. Only path (b) is tested in `test_translator_clear_cache_W1319.py`. Path (a) requires a real or fake `_settings_getter` lambda to be injected.

The `test_translator_cache_lock_W1161.py` does inject a `_settings_getter` in a subclass for some tests (line 241), but those tests assert thread-safety rather than the privacy transition behaviour of path (a).

**Impact:** If the `_settings_getter`-based path has a subtle bug (e.g., the getter returns a non-bool falsy value other than `False`, or raises), the privacy-mode cache wipe is silently skipped without any test catching it.

**Recommendation:** Add a dedicated test that injects a real `_settings_getter` callable, triggers a `False→True` privacy transition via `translate()`, and asserts that `_translation_cache.clear()` is called.

---

### F5 — INFO: Disk cache key uses `normalized_mode`/`normalized_style` as `source`/`target` params

**File:** `KrabEar/backend/translator.py`, lines 308–313 and 352–357 and 374–379  
**Root cause:** `TranslationCache.get/put` signature documents `source`/`target` as language codes (e.g., `"ru"`, `"es"`). The W1429 wiring passes `source=normalized_mode` (e.g., `"ru_to_es"`) and `target=normalized_style` (e.g., `"neutral"`). This deviates from the documented API intent.

**Impact:** Functionally correct — the cache key is a SHA-256 hash of all parameters, so the `source`/`target` labels have no semantic meaning beyond participation in the key. However:
1. The `translation_cache.json` on disk will contain hashes computed with `source="ru_to_es"` rather than `"ru"`, which is confusing to inspect or debug.
2. Any future refactoring that calls `TranslationCache.get()` with actual language codes will produce a **cache miss** for all existing W1429-written entries (different hash).
3. No code comment in `_translate_impl` documents why mode/style are used for source/target.

**Recommendation:** Add an inline comment at the `_tc.get()` and `_tc.put()` call sites documenting the deliberate key-field mapping, or introduce named local variables to make the intent explicit.

---

## Test Coverage Post-Fix

| Fix | New test file | Tests added | Coverage gaps |
|-----|--------------|-------------|---------------|
| W1428 (duplicate removal) | `test_translator_clear_cache_W1319.py` | 7 | Getter-based path of `_check_privacy_mode_changed` (F4) |
| W1429 (disk cache wiring) | `test_translator_clear_cache_W1319.py` | 3 disk-layer tests | No integration test for `_translate_impl` disk hit/put via real `TranslationCache` |
| W1430 (word boundaries) | `test_translator_glossary_boundary_W1430.py` | 8 | No test for target backslash injection (F1); no test for overlapping terms (F2); no test for terms ending in non-word chars (F3 regression) |

**Total translator test files:** 9 (~2990 lines total). Core positive paths are well covered. The three residual gaps above are untested failure modes introduced or exposed by the W1428–W1430 changes.
