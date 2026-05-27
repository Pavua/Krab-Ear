# Audit: abbreviation_expander.py — residual issues (W1111)

**Date:** 2026-05-26  
**Wave:** W1111  
**File:** `KrabEar/core/abbreviation_expander.py`  
**Auditor:** sub-agent W1111 (re-audit post-W1060, checking W1081 merge status)  
**Branch audited:** `codex/krab-ear-v2` @ `720bc1ab`

---

## W1081 Merge Status: CRITICAL DRIFT — NOT MERGED

Searching `origin/codex/krab-ear-v2` for `_BUILTIN_RU_UNAMBIGUOUS` and `expand_ambiguous`:

```
git log --oneline origin/codex/krab-ear-v2 | grep -i "W1081\|unambiguous\|expand_ambig"
# → (no output)
```

**W1081 fix (drop ambiguous defaults + opt-in flag) was never merged.** The ambiguous RU abbreviations (`пр.`, `гл.`, `св.`, `г.`, `д.`, `кв.`, `пл.`, `ред.`) remain active in the default builtin set and fire on every call to `expand(..., language="ru")` without any opt-in. See F1 below.

---

## Findings

### F1 — HIGH: W1081 unmerged — ambiguous RU builtins active in production

**Location:** `KrabEar/core/abbreviation_expander.py`, `_BUILTIN_RU` lines 27–58

Three abbreviations cause confirmed incorrect expansions in common speech patterns:

| Abbrev | Current expansion | Incorrect firing example |
|--------|-------------------|--------------------------|
| `пр.` | прочее | "Садовый пр." → "Садовый прочее" (should be проспект) |
| `гл.` | глава | "гл. редактор" → "глава редактор" (should be главный редактор) |
| `св.` | святой | "св. воздух" → "святой воздух" (contextually wrong) |

`г.` and `д.` are partially guarded by `no_after_digit` but still expand in text context where ambiguity remains (e.g., `г.` = год vs город). W1081 proposed splitting `_BUILTIN_RU` into `_BUILTIN_RU_UNAMBIGUOUS` (safe defaults) and `_BUILTIN_RU_AMBIGUOUS` (opt-in via `expand_ambiguous=True` flag). That split was never shipped.

**Impact:** Every RU transcript containing these abbreviations in address/title/adjective contexts receives incorrect expansions silently. No user-visible error; text quality degrades.

**Fix:** Implement the W1081 design: add `expand_ambiguous: bool = False` parameter to `expand()`, remove `пр.`, `гл.`, `св.`, `ред.` from default set (keep `г.`, `д.`, `кв.`, `пл.` with `no_after_digit` since the guard covers their main misfire pattern). PR must include regression tests for each removed abbreviation.

---

### F2 — MEDIUM: `add_abbreviation` IPC method missing from dispatch table

**Location:** `KrabEar/backend/ipc_dispatch.py` lines 239–241; `KrabEar/backend/text_processing_service.py`

`list_abbreviations` and `remove_abbreviation` are wired in `ipc_dispatch.py` (lines 240–241). `expand_abbreviations` is also wired (line 239). But `add_abbreviation` has **no IPC handler** — neither a `handle_add_abbreviation` method in `TextProcessingService` nor an entry in `ipc_dispatch.py`.

```python
# ipc_dispatch.py lines 239-241 — add_abbreviation missing:
"expand_abbreviations": svc._text_processing_svc.handle_expand_abbreviations,
"remove_abbreviation":  svc._text_processing_svc.handle_remove_abbreviation,
"list_abbreviations":   svc._text_processing_svc.handle_list_abbreviations,
# "add_abbreviation":  MISSING
```

`AbbreviationExpander.add_abbreviation()` method exists on the class (line ~200 in the module). The Swift UI or any IPC client cannot add custom abbreviations via the socket — they must edit `abbreviations.json` on disk directly.

**Impact:** Custom abbreviation management is incomplete via IPC. W1060 F1 flagged this; it remains unresolved.

**Fix:** Add `handle_add_abbreviation(params)` to `TextProcessingService` (pattern mirrors `handle_remove_abbreviation`) and register `"add_abbreviation": svc._text_processing_svc.handle_add_abbreviation` in `ipc_dispatch.py`.

---

### F3 — MEDIUM: No thread-safety lock on `_abbrevs`/`_compiled` shared state

**Location:** `KrabEar/core/abbreviation_expander.py`, `AbbreviationExpander.__init__`, `expand()`, `add_abbreviation()`, `_rebuild_compiled()`

`BackendService.__init__` creates one shared `AbbreviationExpander` instance (`self._abbreviation_expander`, line 451 of `service.py`). Multiple IPC threads can concurrently call:
- `expand()` — reads `self._compiled[lang]` (iterates the list)
- `add_abbreviation()` / `remove_abbreviation()` — writes `self._abbrevs` then calls `_rebuild_compiled()` which **replaces** `self._compiled[lang]` entirely

Python's GIL protects individual bytecode operations but not compound read-modify-write sequences across method calls. Concurrent `expand()` + `_rebuild_compiled()` can yield a partially-constructed compiled list or a `KeyError` if the dict replacement races with iteration.

W1036/W1041 fixed the identical pattern in `SearchIndex` with an `RLock`. The same fix is needed here.

**Impact:** Low probability in practice (add/remove are infrequent), but data races are undefined behavior. Under load testing (batch expand + concurrent add), this could produce corrupted expansions.

**Fix:**
```python
import threading

class AbbreviationExpander:
    def __init__(self, ...):
        self._lock = threading.RLock()
        ...

    def expand(self, text, language="ru"):
        with self._lock:
            ...  # existing body

    def add_abbreviation(self, ...):
        with self._lock:
            ...  # existing body

    def remove_abbreviation(self, ...):
        with self._lock:
            ...

    def _rebuild_compiled(self, language=None):
        # called only from within locked context, RLock is reentrant — safe
        ...
```

---

### F4 — LOW: `expand_abbreviations` not in `DEFAULT_CHAIN` in `text_postprocessor.py` (W1101 opt-in gap)

**Location:** `KrabEar/core/text_postprocessor.py` line 188

```python
DEFAULT_CHAIN: list[str] = ["strip_whitespace", "fix_punctuation", "normalize_entities"]
```

`expand_abbreviations` is **registered** as a named step (line 182: `"expand_abbreviations": ExpandAbbreviations(language="ru")`) but excluded from `DEFAULT_CHAIN`. The docstring (lines 198–209) documents it as opt-in by listing it in the examples. This is intentional per W1101 design.

However the `ExpandAbbreviations` step is hard-coded to `language="ru"` regardless of what language the transcript is in. If a caller adds `"expand_abbreviations"` to their custom `steps=[]` for an ES or EN transcript, it silently runs RU patterns against ES/EN text (no-ops mostly, but `"etc."` in EN text runs against RU dict which has no `etc.` → correct no-op). The step does not accept a language parameter from the caller's `steps` list.

**Impact:** Low — mostly a no-op since cross-language abbreviation collisions are rare. But the step API is misleading: callers cannot specify language when composing a custom chain.

**Fix:** Allow `ExpandAbbreviations` to accept `language` as a runtime parameter from `params` (passed alongside `text`), or document clearly that it only processes RU and callers should not include it in chains for other languages.

---

### F5 — LOW: ES coverage gaps — `c/` (calle) and honorifics `Lic.`, `Ing.` absent

**Location:** `KrabEar/core/abbreviation_expander.py`, `_BUILTIN_ES` lines 91–108

The ES builtin list (18 entries) omits several common Spanish abbreviations that appear frequently in speech dictation:

| Missing abbrev | Expansion | Notes |
|----------------|-----------|-------|
| `c/` | calle | Standard Spanish address notation |
| `Lic.` | licenciado/a | Common professional title (MX, AR, VE) |
| `Ing.` | ingeniero/a | Very common technical title |
| `Cía.` | compañía | Business name suffix |
| `Cte.` | corriente | Banking context |

`cap.` (capítulo) and `art.` (artículo) carry minor ambiguity risk (`cap.` = capitalización in finance; `art.` = artista informally) but are standard legal/academic abbreviations and the risk is low. No `no_after_digit` flag is needed for `cap.` and `art.` since numeric references like "art. 5" and "cap. 3" are correct expansions.

**Impact:** Coverage gap — dictated Spanish text with these abbreviations passes through unexpanded. Lower severity than F1–F3.

**Fix:** Add the 5 entries to `_BUILTIN_ES`. `Lic.` and `Ing.` should carry no flag (never follow digits in normal speech). `c/` needs special handling: the regex `(?<!\w)c/(?=\s|$|...)` may not match cleanly — test required.

---

## Verification Table

| Check item | Status |
|------------|--------|
| W1081 `_BUILTIN_RU_UNAMBIGUOUS` merged | **NOT MERGED — CRITICAL DRIFT** |
| W1081 `expand_ambiguous` flag merged | **NOT MERGED** |
| `add_abbreviation` IPC wired | **MISSING** (F2) |
| `expand_abbreviations` IPC wired | OK (ipc_dispatch line 239) |
| `remove_abbreviation` IPC wired | OK (ipc_dispatch line 240) |
| `list_abbreviations` IPC wired | OK (ipc_dispatch line 241) |
| Thread safety (`_abbrevs`/`_compiled`) | **MISSING RLock** (F3) |
| `expand_abbreviations` in `DEFAULT_CHAIN` | Intentionally excluded (W1101 opt-in) |
| ES coverage | Partial (F5) |
| Idempotency | OK — expansions don't re-match |
| Case preservation (`_match_case`) | OK — IGNORECASE + `_match_case` correct |
| `no_after_digit` guard | OK for RU `г.`, `д.`, `кв.`; EN `no.`; ES `núm.` |

---

## Summary

5 new findings (W1081 not merged = CRITICAL drift, counts as the merge-state flag, not a new finding per se):

| ID | Severity | Summary |
|----|----------|---------|
| F1 | HIGH | W1081 unmerged — 3+ RU abbreviations produce incorrect expansions in production |
| F2 | MEDIUM | `add_abbreviation` IPC handler absent from dispatch table and TextProcessingService |
| F3 | MEDIUM | No RLock on shared `_abbrevs`/`_compiled` — race on concurrent add+expand |
| F4 | LOW | `ExpandAbbreviations` step hard-coded to `language="ru"` — misleading for multilingual chains |
| F5 | LOW | ES missing 5 common abbreviations (`c/`, `Lic.`, `Ing.`, `Cía.`, `Cte.`) |

**W1081 merge status: NOT MERGED — CRITICAL DRIFT.** The main ambiguity fix remains in a branch that was never PR'd into `codex/krab-ear-v2`.
