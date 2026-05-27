# W1156 Audit — TranslationService

**Date:** 2026-05-26  
**File:** `KrabEar/backend/translation_service.py` (464 lines)  
**Scope:** translate endpoint param validation, glossary CRUD atomicity, vocab suggestions perf, privacy_mode interaction, W1019 language_detector FR/PT false positives, IPC handler completeness, test coverage.  

---

## Summary

5 findings. 1 HIGH (glossary TOCTOU), 2 MEDIUM, 2 LOW. No W1145 translator-layer findings re-reported.

---

## F1 — HIGH: Glossary CRUD has a TOCTOU lost-write race

**Location:** `translation_service.py:254–260` (`handle_set_translation_glossary_item`) and `:280–288` (`handle_remove_translation_glossary_item`)

**Description:**  
Both glossary mutation handlers follow the pattern:

```python
settings = self._cached_settings()          # reads TTL-cached copy
glossary = settings.get("translation_glossary", {})
glossary[source] = target                   # mutates the copy
settings["translation_glossary"] = glossary
saved = self.store.save_settings(settings)  # file-lock write
self._invalidate_settings_cache()
```

`_cached_settings()` returns a copy of the TTL-cached dict (5 s TTL, from `SettingsService`). The `save_settings` call is protected by `fcntl.flock`, but the read-modify step happens *outside* any lock. Two concurrent IPC requests (e.g., two simultaneous `set_translation_glossary_item` calls from a double-tap) will both read the same cached snapshot, each independently add/remove their entry, and the second `save_settings` will silently overwrite the first — losing the first caller's glossary entry.

This is a true data-loss race at the service layer. The file lock in `StateStore._lock()` serialises the *write* but not the full read-modify-write cycle.

**Fix:** Serialize with `self.store._lock()` around the full read-modify-write, or delegate the merge to `StateStore` as an atomic `update_settings_key(key, merge_fn)` helper (same pattern used in direct callers at `service.py:2302–2556`).

---

## F2 — MEDIUM: `handle_translate_text` — `translation_mode` and `translation_style` params are never validated

**Location:** `translation_service.py:87–138`

**Description:**  
`translation_mode` (line 90) and `translation_style` (line 91) are accepted from the IPC caller with no validation against an allowed-values list:

```python
mode = str(params.get("translation_mode", "off"))
translation_style = str(params.get("translation_style", "neutral"))
```

Any string value passes through to `translator.translate()`. Invalid mode values (`"gibberish"`, `"__import__('os').system(...)"`) are forwarded. The `Translator` class has internal fallback logic, but there is no IPC-layer rejection with a clear error. This creates: (a) silent misbehaviours when an iOS shortcut or test script passes a stale mode string after a rename; (b) no contract enforcement to callers.

`SettingsService` validates `translation_mode` enum at settings-save time (`settings_validator.py`), but the IPC endpoint itself skips this.

**Fix:** Add a guard: `_VALID_MODES = frozenset({"off", "auto", "ru_to_es", "es_to_ru", "en_to_ru", "bilingual"})` and raise `RuntimeError("invalid translation_mode")` for unknown values.

---

## F3 — MEDIUM: Language detector cannot distinguish FR/PT from ES — misroutes `translate_selection`

**Location:** `translation_service.py:173–195` + `core/language_detector.py:152–157`

**Description:**  
`handle_translate_selection` auto-detects source language via `LanguageDetector.detect()`. The detector's `_detect_latin` classifies any Latin text as either `es` (if `ñáéíóú…` markers present) or `en` (otherwise). French and Portuguese share the Latin-Extended-A/B block but lack `_ES_MARKERS`, so they are silently classified as `en`. 

Consequence: pasting a French sentence ("Bonjour le monde") detects `en`, maps to `target_lang=ru` (correct intent), then calls `mode="en_to_ru"` — the chain works accidentally. But a Portuguese sentence ("Muito obrigado") also detects `en` and routes to `en_to_ru`, which may produce poor output since the translator's EN→RU model is trained on English, not Portuguese.

More problematic: if the user sends French with accented Latin-Extended chars (e.g., "Données de recherché"), the unicode range `0x00C0–0x024F` is counted as `latin` and still returns `en` — no way to signal "this might not be English" to the caller.

**Impact:** Graceful degradation (wrong translation quality), not a crash. But the service returns `"source_lang_detected": "en"` to Swift, which writes it back to the history item as `source_lang="en"`, permanently mislabeling the item.

**Fix (W1019 interaction):** The `LanguageDetector` is by design limited to `{ru, uk, es, en}`. `TranslationService` should check the detector's `confidence` field: if `confidence < 0.7` and `script == "latin"` and no ES markers found, emit `source_lang_detected = "und"` in the response rather than `"en"`. The translator already has an `auto` mode that handles unknown-language inputs better.

---

## F4 — LOW: `handle_get_glossary_suggestions` — O(H × W²) regex compilation on every call

**Location:** `translation_service.py:335–342`

**Description:**  
Inside the history loop, for each capitalized word `src_word` found in a history item, a new `re.compile()` pattern is created and immediately executed against `translated_text`:

```python
for src_word in set(cap_words):
    pattern = re.compile(r"\b" + re.escape(src_word) + r"\b", re.IGNORECASE)
    match = pattern.search(translated_text)
```

With `scan_limit=200` (default), up to 200 history items × N capitalized words each, this is `O(H × W)` regex compilations with no caching. Python's `re.compile` has a small internal LRU cache (size 512), so in practice most patterns are cached if the vocabulary is small, but for a user with 200+ history items and large text, worst-case behaviour is measurable.

Negligible for typical usage but `scan_limit` is user-configurable up to 1000 (`line 302`). At `scan_limit=1000` with 20 unique capitalized words per item, this is 20,000 `re.compile` calls per request.

**Fix:** Collect all unique `src_word` values across all items first, build the compiled patterns dict once, then do a second pass matching each pattern against each translated text. This reduces regex compilations from `O(H × W)` to `O(unique_words)`.

---

## F5 — LOW: No test coverage for privacy_mode interaction in `handle_translate_selection`

**Location:** `translation_service.py:200–215`, `tests/test_translation_service.py`

**Description:**  
`handle_translate_text` has a privacy_mode guard (lines 96–110) that forces `network_mode = "offline_only"` and logs a `privacy_audit` event. `handle_translate_selection` has the same guard (lines 200–215).

The existing 548-line test file (`test_translation_service.py`) has comprehensive breadcrumb tests, glossary CRUD tests, and edge-case tests, but **no test verifies that `privacy_mode_enabled=True` forces `network_mode="offline_only"` in `handle_translate_selection`**. The same gap exists for `handle_translate_text`.

The privacy guarantee is a compliance requirement (NDJSON audit trail). A regression in this codepath would be undetected until a production privacy audit.

**Existing coverage:** Tests 1–24 cover normal paths, stop-words, and Sentry breadcrumbs well. The gap is specifically privacy override enforcement.

**Fix:** Add two tests:
1. `test_privacy_mode_forces_offline_in_translate_text` — settings has `privacy_mode_enabled=True` + `network_mode="online"`, verify `translator.translate.call_args` has `network_mode="offline_only"`.
2. `test_privacy_mode_forces_offline_in_translate_selection` — same for `handle_translate_selection`.

---

## IPC Handler Completeness

All 5 methods extracted into `TranslationService` are correctly wired in `service.py:931–960`:

| Method | Handler | Wired |
|---|---|---|
| `translate_text` | `handle_translate_text` | Yes (line 931) |
| `translate_selection` | `handle_translate_selection` | Yes (line 932) |
| `set_translation_glossary_item` | `handle_set_translation_glossary_item` | Yes (line 934) |
| `remove_translation_glossary_item` | `handle_remove_translation_glossary_item` | Yes (line 936) |
| `get_glossary_suggestions` | `handle_get_glossary_suggestions` | Yes (line 937) |
| `get_vocabulary_suggestions` | `handle_get_vocabulary_suggestions` | Yes (line 960) |

No orphan or un-wired methods found.

---

## Test Coverage Assessment

- 24 unit tests across 6 test classes in `test_translation_service.py`
- Coverage: param validation (basic), glossary CRUD, vocab suggestions, breadcrumbs, privacy (missing)
- **Gap:** No concurrency tests, no privacy_mode enforcement tests (F5), no invalid `translation_mode` rejection tests (F2)
- Other translation test files (`test_translator.py`, `test_translator_glossary_deep.py`, etc.) cover the `Translator` layer, not this service layer
