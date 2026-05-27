# Wave 1492 — Translator Third-Pass Audit

**Date:** 2026-05-27
**Branch:** `audit-translator-third-W1492` (off `codex/krab-ear-v2`)
**Auditor:** W1492 sub-agent (read-only)
**Scope:** `KrabEar/backend/translator.py` — third-pass after W1428, W1429, W1430, W1447, W1455.

---

## Prior Wave Merge State

| Wave | PR | Title | Merged to `codex/krab-ear-v2` |
|------|----|-------|-------------------------------|
| W1428 | #1327 | Remove duplicate `clear_cache` + `_check_privacy_mode_changed` | **YES** (commit `fcc0e6b3`) |
| W1429 | #1336 | Wire `TranslationCache` in `BackendService` + clear IPC | **YES** (commit `1c6efb64`) |
| W1430 | #1335 | `_apply_glossary` word boundaries + `IGNORECASE` | **YES** (commit `7cc21af1`) |
| W1447 | #1340 | Post-fix audit doc (5 new findings) | **YES** (commit `54b2e286`) |
| W1455 | #1347 | `_apply_glossary` lambda for target — no backslash interpretation | **YES** (commit `34f7679e`) |

All 5 prior waves are confirmed merged. The lambda fix from W1455 is visible at line 883:
`lambda _m, _t=target: _t`.

---

## New Residual Findings

### F1 — CRIT: Duplicate `clear_cache` — active definition does NOT clear `_unavailable`

**File:** `KrabEar/backend/translator.py`, lines 118–127 and 160–176
**Root cause:** W1428 was supposed to remove the duplicate `clear_cache` but the file now
contains **two** definitions of `clear_cache` in the same class:

- **Lines 118–127** (first definition): clears `_cache` AND `_unavailable` inside `_cache_lock`.
- **Lines 160–176** (second definition, W1319/W1429): clears `_cache` (in-lock) + calls
  `_translation_cache.clear()` (disk layer). Does **not** clear `_unavailable`.

Python silently uses the **last** definition in a class body. The active `clear_cache` (line 160)
therefore never clears `_unavailable`.

**Impact:**
1. When a model fails to load, its key is added to `_unavailable` (lines 542, 624).
2. When privacy mode is enabled, `clear_cache()` is called — but `_unavailable` is NOT cleared.
3. After the privacy-mode wipe, all previously-failed models remain permanently blocked
   for the lifetime of the process, even if the model files have since been downloaded.
4. A user who turns on privacy mode then tries to translate will silently receive
   `"model_unavailable_cached"` status even if the model is now available.

**Reproduction:**
```python
t = Translator()
# Simulate model unavailable
t._unavailable.add(("Helsinki-NLP/opus-mt-ru-es", False))
t.clear_cache()
# _unavailable is NOT empty — bug:
assert t._unavailable  # True — stale mark persists
```

**Fix:** Remove the first `clear_cache` definition (lines 118–127) and merge
`_unavailable.clear()` into the second definition (line 125):

```python
def clear_cache(self) -> None:
    with self._cache_lock:
        self._cache.clear()
        self._unavailable.clear()  # add this
    logger.debug("Translator: in-memory translation cache cleared")
    tc = self._translation_cache
    if tc is not None:
        try:
            tc.clear()
        except Exception as exc:
            logger.warning("Translator: disk translation_cache.clear() failed: %s", exc)
```

---

### F2 — HIGH: `_translate_impl` uses uninitialised `_privacy_was_on` → `AttributeError`

**File:** `KrabEar/backend/translator.py`, lines 290–294
**Root cause:** `_translate_impl` contains an undocumented, secondary privacy-mode guard
that was not removed when `_check_privacy_mode_changed` was introduced:

```python
# W1145 F2: detect privacy_mode true-transition and purge in-RAM cache.
privacy_now = bool(getattr(self, "_privacy_mode", False))
if privacy_now and not self._privacy_was_on:   # line 292
    self.clear_cache()
self._privacy_was_on = privacy_now             # line 294
```

Neither `_privacy_mode` nor `_privacy_was_on` is initialised in `__init__`. When
`privacy_mode` is set to `True` (via any external injection) before the **first** call to
`translate()`, line 292 evaluates `not self._privacy_was_on` which raises `AttributeError`
because `_privacy_was_on` does not exist yet.

Verified with Python 3.x:
```python
class MockT:
    _privacy_mode = True

t = MockT()
privacy_now = bool(getattr(t, "_privacy_mode", False))  # True
if privacy_now and not t._privacy_was_on:               # AttributeError
    pass
```

**Impact:** Any `translate()` call when `privacy_mode=True` from the start of the process
raises an unhandled `AttributeError` inside `_translate_impl`, which propagates out of
`translate()`, breaking the entire translation pipeline.

**Fix options:**
1. Remove lines 290–294 entirely — this guard duplicates `_check_privacy_mode_changed()`
   which is already called at line 228. (Preferred: eliminates dead-code conflict.)
2. Or initialise `self._privacy_was_on = False` in `__init__`.

---

### F3 — HIGH: `_settings_getter` never wired → privacy-mode detection dead in production

**File:** `KrabEar/backend/translator.py`, line 189;
`KrabEar/backend/service.py` (no injection site)

**Root cause:** `_check_privacy_mode_changed()` (called on every `translate()`) has two
code paths:

- **With argument** (tests / manual call): uses the passed `bool`.
- **Without argument** (production): reads `privacy_mode_enabled` via
  `getattr(self, "_settings_getter", None)` (line 189).

`BackendService.__init__` injects `_translation_cache` (line 212) but **never** injects
`_settings_getter` into the translator. A grep of `backend/service.py` confirms zero
occurrences of `_settings_getter`.

**Impact:** In production, `_check_privacy_mode_changed()` runs on every `translate()` call
but always takes the early-return path (`getter is None → return`) at line 190. Privacy-mode
transitions are **never detected** through the normal translation pipeline. The only way the
cache gets wiped is if `BackendService` calls `_check_privacy_mode_changed(bool)` explicitly
from the settings-update handler — which would need to be verified separately.

**Fix:** In `BackendService.__init__`, inject the settings getter into the translator:

```python
self.translator._settings_getter = self._get_runtime_setting
```

This is the same late-injection pattern used for `_error_bus` and `_translation_cache`.

---

### F4 — MED: Disk cache writes (`_tc.put`) have no privacy-mode guard

**File:** `KrabEar/backend/translator.py`, lines 368–379 and 390–401
**Root cause:** Both `_tc.put()` call sites in `_translate_impl` write to the persistent
translation cache unconditionally whenever the result is successful (`result.ok`). There is
no check for the current `privacy_mode_enabled` setting before persisting.

Since `_settings_getter` is not wired (F3), there is no efficient way to read
`privacy_mode_enabled` at the persistence point. The consequence is that even when
`privacy_mode_enabled=True`, completed translations are written to `translation_cache.json`
on disk.

The existing clear-cache mechanism only wipes disk entries on a privacy-mode
**transition** (False→True). But:
1. If privacy mode was already `True` when the backend started, the transition never fires
   (first-call guard at line 208 returns early).
2. Translations arriving during a stable `privacy_mode=True` session are persisted
   immediately after every successful translation.

**Impact:** Translations made during a privacy-mode session survive backend restart as
on-disk cache entries, violating the privacy guarantee. The user's translation history
is persisted even though they opted in to privacy mode.

**Fix:** Gate disk persistence on the absence of privacy mode. After wiring `_settings_getter`
(F3 fix), add the guard:

```python
privacy_active = bool(self._last_privacy_mode)  # already tracked
if result.ok and result.text and _tc is not None and not privacy_active:
    try:
        _tc.put(...)
    except Exception:
        pass
```

---

### F5 — LOW: `_apply_glossary` applied to bilingual text can corrupt language labels

**File:** `KrabEar/backend/translator.py`, lines 380 and 402
**Root cause:** In bilingual mode (`bilingual_ru_es`), `_translate_bilingual_ru_es` returns
a `TranslationResult` whose `.text` has the form:

```
RU: <original_text>
ES: <translated_text>
```

The caller (`_translate_impl` line 380) then applies the glossary to this **entire** string
via `_apply_glossary_to_result`. If the glossary contains an entry whose source term matches
`"RU"` or `"ES"` (case-insensitively), the language labels are mutated:

```python
glossary = {"RU": "Russian"}
# Result text: "Russian: Привет\nES: Hola"
```

The `\b` word-boundary ensures this only happens when `"RU"` / `"ES"` appear as isolated
words, but both labels appear exactly at a word boundary position (preceded by start-of-line
or newline, followed by `:`).

**Verification:**
```python
import re
text = "RU: Привет\nES: Hola"
result = re.sub(r"\bRU\b", lambda _m, _t="Russian": _t, text, flags=re.IGNORECASE)
# -> "Russian: Привет\nES: Hola"  # label corrupted
```

**Impact:** Low probability (requires a user glossary entry matching "RU", "ES", "EN", or
"DE"), but when triggered, the bilingual panel in the Swift UI displays
`"Russian: Привет\nES: Hola"` instead of `"RU: Привет\nES: Hola"`, confusing the display.

**Fix:** Apply the glossary **only to the translated portion** of the bilingual text, not the
label prefix. This requires splitting the bilingual text before glossary application, or
constructing the final bilingual string after applying the glossary to the translated segment.

---

## Test Coverage Post-W1455

| Area | Test files | Coverage gaps introduced by W1428–W1455 |
|------|-----------|------------------------------------------|
| `clear_cache` duplicate | `test_translator_clear_cache_W1319.py` | F1: `_unavailable` not tested after `clear_cache()` |
| `_translate_impl` privacy gate | `test_translator_cache_lock_W1161.py` | F2: `_privacy_was_on` AttributeError never tested |
| `_settings_getter` injection | `test_translator_clear_cache_W1319.py` | F3: getter path never tested in integration |
| Disk persistence + privacy | `test_translation_cache_wire_W1429.py` | F4: no test for `_tc.put` suppression when privacy active |
| Bilingual + glossary | `test_translator_glossary_boundary_W1430.py` | F5: bilingual mode + glossary combo not tested |

**Open from W1447 (still unresolved):**
- F2: overlapping glossary terms (shorter matches before longer compound) — no fix yet.
- F3: `\b` fails for terms ending in non-word chars (`C++`, `.NET`) — no fix yet.
- F4: `_check_privacy_mode_changed()` getter path coverage — partially addressed via F3 above.
