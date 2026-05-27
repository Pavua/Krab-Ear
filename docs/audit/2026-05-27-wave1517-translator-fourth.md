# W1517 — translator.py fourth-pass audit

**Date:** 2026-05-27  
**Auditor:** Sub-agent W1517  
**File:** `KrabEar/backend/translator.py`  
**Branch audited:** `codex/krab-ear-v2` (HEAD `f6bb585e`)  
**Prior waves:** W1492 (third-pass, 5 findings), W1498 (F1 CRIT + F2 HIGH), W1500 (F3 HIGH / MILESTONE)

---

## W1498 + W1500 merge state verification

Both W1498 (`94f0222f`) and W1500 (`e5011c6b`) are present in `codex/krab-ear-v2` git history.
**However, their fixes were entirely reverted by a subsequent commit.**

Commit `60919b88` (`feat(W1190): wire TranslationCache into BackendService + Translator`) was applied
**after** W1500 and deleted -268 lines while adding +88, removing ALL of the following from
`translator.py`:

- `import threading`
- `self._cache_lock = threading.RLock()` (W1145 F1 HIGH)
- `self._last_privacy_mode: bool | None = None` (W1145 F2)
- `self._pipeline_locks` + `self._locks_mutex` (W926 F2 HIGH)
- `self._settings_getter: Any | None = None` (W1500 / W1492 F3)
- `def clear_cache(self)` — completely removed (W1319 / W1145 / W1492 F1)
- `def _check_privacy_mode_changed(self)` — completely removed (W1145 / W1492)
- `def _get_pipeline_lock(self)` — completely removed (W926 F2)
- `_apply_glossary` word-boundary `re.sub` + `re.IGNORECASE` + lambda-wrap (W1430 / W1455)

The current file at HEAD has **none** of these. W1498 and W1500 fixes are confirmed
**NOT present** in the running codebase despite appearing in the git log.

---

## Findings (5 new)

### F1 — CRIT: W1190 wiped ALL thread-safety — `_cache_lock` is gone, `_cache` is unprotected

**Severity:** CRIT  
**Lines:** `__init__` (lines 98-110), `_cache_get`/`_cache_set` (lines 759-772)

`OrderedDict` is not thread-safe for concurrent `get + move_to_end` operations. W1145 F1 HIGH
added `self._cache_lock = threading.RLock()` specifically to protect against concurrent access from
the `live_subs` background thread. W1190 removed both the `import threading` and all lock usage.

`_cache_get` at line 761 does `self._cache.get(key)` + `self._cache.move_to_end(key)` with no
locking; `_cache_set` at line 769 does `self._cache[key] = value` + `move_to_end` + `popitem`.
Under concurrent translate calls (live_subs + hotkey-triggered STT), these are TOCTOU — the
`OrderedDict` internal pointer can corrupt between the two calls, leading to
`RuntimeError: dictionary changed size during iteration` or silent data loss.

Additionally `self._pipeline_locks` (W926 F2) which prevented double-init of 2.4 GB NLLB models
under concurrent load is also gone.

**Fix:** Restore `import threading`, `self._cache_lock = threading.RLock()`,
`self._pipeline_locks: dict[tuple, threading.Lock]`, `self._locks_mutex: threading.Lock` in
`__init__`. Wrap `_cache_get`/`_cache_set` bodies in `with self._cache_lock:`. Restore
`_get_pipeline_lock()` helper and use double-checked locking in `_translate_with_model`.

---

### F2 — CRIT: `clear_cache()` method is completely absent — `clear_translation_cache` IPC crashes at runtime

**Severity:** CRIT  
**Lines:** `ipc_dispatch.py` line 77; `translator.py` (missing)

`ipc_dispatch.py` line 77 registers:
```python
"clear_translation_cache": svc._handle_clear_translation_cache,
```

But `_handle_clear_translation_cache` does not exist anywhere in the production backend
(grep confirms: zero results outside test files). Any client calling the `clear_translation_cache`
IPC method will receive an `AttributeError` crash on the `svc` object at dispatch time.

Additionally, `Translator.clear_cache()` itself is missing. This method was the core mechanism
by which privacy-mode transitions cleared cached translations. Without it, enabling privacy mode
no longer evicts the in-memory LRU cache or the disk-persistent `_translation_cache` — prior
sessions' translated text persists through privacy-mode transitions.

**Fix:** Restore `clear_cache()` method in `Translator` (clears `_cache`, `_unavailable`, and
calls `_translation_cache.clear()` under `_cache_lock`). Add `_handle_clear_translation_cache`
handler to `BackendService` or `TranslationService`.

---

### F3 — CRIT: `_check_privacy_mode_changed()` is gone — privacy-mode transitions never detected

**Severity:** CRIT  
**Lines:** `translator.py` (missing); `_translate_impl` (lines 164-272)

W1500 added `self._settings_getter` slot and wired BackendService injection so that
`_check_privacy_mode_changed()` could detect `privacy_mode: off→on` transitions and flush the
cache. W1190 deleted `_check_privacy_mode_changed()`, `self._settings_getter`,
`self._last_privacy_mode` — all of them.

`_translate_impl` (lines 164-272) never calls any privacy check. When the user enables
privacy_mode during a session, previously-translated content remains in the in-memory LRU
cache (capacity 500 entries) and in the on-disk `_translation_cache`. Any subsequent translate
call for a previously-seen text will return the cached result without making a network call —
which is correct — but the cached result itself may expose translations performed before the
user explicitly requested privacy. This is a privacy data leak.

**Fix:** Restore `self._last_privacy_mode: bool | None = None`, `self._settings_getter: Any | None = None`,
and `_check_privacy_mode_changed()`. Call it at the start of `_translate_impl`. Re-inject
`_settings_getter = self._get_runtime_setting` from BackendService after construction.

---

### F4 — HIGH: `_apply_glossary` lost word-boundary regex — substring substitution regression (W1430/W1455 reverted)

**Severity:** HIGH  
**Lines:** `_apply_glossary` (lines 738-743)

Current implementation:
```python
def _apply_glossary(text: str, glossary: dict[str, str]) -> str:
    result = text
    for source, target in glossary.items():
        result = result.replace(source, target)
    return result
```

W1430 replaced `str.replace` with `re.sub(r"\b" + re.escape(source) + r"\b", ..., flags=re.IGNORECASE)`
and W1455 added the lambda wrapper (`lambda _m, _t=target: _t`) to prevent backslash interpretation
in the replacement string. Both fixes were in the codebase at W1500 but W1190 replaced the entire
`_apply_glossary` body with the naive `str.replace` version.

Consequences:
1. Glossary term "AI" will incorrectly replace the "AI" substring in "PAIN", "AIRLOCK", etc.
2. Glossary term "el" will corrupt Spanish words like "elecciones" → "Educationecciones".
3. Glossary values containing backslash sequences (e.g. `C:\Users`) raise `re.error` — actually
   this is now silently masked by the plain `replace` which interprets no backslashes, but the
   word-boundary protection is gone entirely.
4. Case-insensitive matching (W1430's `re.IGNORECASE`) is gone — glossary "Краб" won't match "краб".

**Fix:** Restore word-boundary `re.sub` with `re.escape`, `re.IGNORECASE`, and lambda replacement.

---

### F5 — HIGH: W1190 "TranslationCache wiring" commit left `_handle_clear_translation_cache` unimplemented in service.py — IPC handler references non-existent method

**Severity:** HIGH  
**Lines:** `ipc_dispatch.py` line 77; `service.py` (missing); `translation_service.py` (missing)

This is a narrower restatement of F2 but from the service-wiring angle. The W1190 commit message
claims it added `clear_translation_cache` IPC handler to `service.py`, but the actual diff only
touched `translator.py`. The `ipc_dispatch.py` entry at line 77 was added by an earlier wave
(W1429) and still references `svc._handle_clear_translation_cache`, but no such method exists
anywhere in the production backend files.

Additionally W1190's stated intent was to instantiate `TranslationCache` in `BackendService.__init__`
and inject it into `self.translator._translation_cache`. But `grep` of the current `service.py`
finds zero references to `TranslationCache` — the instantiation never landed. The
`translator.py` slot `self._translation_cache` (line 109) will always remain `None` at runtime,
meaning all the persistent-cache code paths at lines 201-270 are permanently dead.

**Fix:** In `BackendService.__init__`, add `self._translation_cache = TranslationCache(data_dir=...)` and
`self.translator._translation_cache = self._translation_cache`. Add
`def _handle_clear_translation_cache(self, params)` that calls `self.translator.clear_cache()`.

---

## Root-cause analysis

All five findings trace to a single commit: `60919b88` (`feat(W1190): wire TranslationCache`)
that was applied AFTER W1498 + W1500. The commit deleted 268 lines and added 88, but the
feature it intended to add (TranslationCache instantiation in BackendService) was never
completed — only the destructive removal happened. The result is a worse state than before
any of the translator fix waves: all thread-safety, privacy detection, and glossary safety
gone, plus a new broken IPC handler reference.

**Recommended action:** A single consolidating fix commit should:
1. Restore `threading` import and all lock infrastructure in `__init__`
2. Restore `clear_cache()` with three-layer clearing (W1492 F1 semantics)
3. Restore `_check_privacy_mode_changed()` + `_settings_getter` injection (W1500 semantics)
4. Restore `_apply_glossary` word-boundary regex + lambda (W1430/W1455 semantics)
5. Wire `TranslationCache` instantiation + `_handle_clear_translation_cache` in `service.py`

---

## Test files to check

- `KrabEar/tests/test_translator_dup_clear_cache_W1498.py` — 10 tests for F1/F2 of W1492 (will fail)
- `KrabEar/tests/test_translator_settings_getter_W1500.py` — 10 tests for W1492 F3 (will fail)
- `KrabEar/tests/test_translation_cache_wiring_W1190.py` — 17 tests for W1190 wiring (will fail)
- `KrabEar/tests/test_translation_cache_wire_W1429.py` — clear_cache handler tests (will fail)
