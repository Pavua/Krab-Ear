# Audit W1167 — TranslationService

**Date:** 2026-05-26
**Branch:** audit-translation-service-W1167
**Files audited:**
- `KrabEar/backend/translation_service.py` (464 lines)
- `KrabEar/backend/translator.py` (769 lines, relevant sections)
- `KrabEar/backend/translation_cache.py` (121 lines)
- `KrabEar/tests/test_translation_service.py`, `test_privacy_mode.py`, `test_privacy_audit.py`

---

## Summary

5 findings. 1 HIGH (privacy bypass), 2 MEDIUM, 2 LOW.

---

## Finding 1 — HIGH: `privacy_mode` network isolation silently bypassed

**File:** `KrabEar/backend/translation_service.py` lines 97–110, 202–215
**Also:** `KrabEar/backend/translator.py` lines 527–532

`handle_translate_text` and `handle_translate_selection` both enforce privacy mode by setting `network_mode = "offline_only"` before calling `self.translator.translate()`. However `Translator._normalize_network_mode()` only accepts the values `{"offline_default", "offline_strict", "online_opt_in"}`:

```python
# translator.py:527-532
if clean not in {"offline_default", "offline_strict", "online_opt_in"}:
    return "offline_default"
```

`"offline_only"` is **not** in this set, so it normalises silently to `"offline_default"`. `offline_default` allows `allow_network = False` (no online_opt_in) but still permits loading models via HuggingFace Hub when models are absent locally, and does not enforce `offline_strict` behaviour. The intent (comment in `DEFAULT_SETTINGS` line 984: *"translation forced to offline_only"*) is therefore **never achieved at the Translator level**.

The unit tests in `test_privacy_mode.py` pass because they mock `translator.translate` entirely and only assert that the string `"offline_only"` is forwarded to the mock — they do not exercise the actual Translator normalisation.

**Fix:** Either add `"offline_only"` to the accepted set in `Translator._normalize_network_mode()` (mapping it to `offline_strict` behaviour), or change the service to use `"offline_strict"` when privacy mode is active. The latter is preferred to keep naming consistent with config.

---

## Finding 2 — MEDIUM: `TranslationCache` is a dead module — never wired

**File:** `KrabEar/backend/translation_cache.py`

`TranslationCache` provides a persistent LRU disk cache (5 000 entries, SHA-256 keyed) with `get()`, `put()`, `clear()`, and `get_stats()`. It was presumably targeted by W925/W938 fixes for lock correctness. However, a full codebase search confirms it is **imported nowhere except its own test file** (`test_translation_cache.py`, `test_resource_leaks.py`). Neither `BackendService` nor `TranslationService` nor `Translator` instantiate or reference it.

`Translator` uses its own private in-memory `OrderedDict` cache (`_cache`, 500-entry, non-persistent). The disk-persistent `TranslationCache` is unused dead code; its W925/W938 fixes have never had any effect in production.

**Recommended action:** Wire `TranslationCache` into `TranslationService` or `Translator` (inject via constructor), or document it as intentionally deferred and track separately. Current state means cache hits across restarts are impossible.

---

## Finding 3 — MEDIUM: Glossary entries have no length bounds

**File:** `KrabEar/backend/translation_service.py` lines 248–273

`handle_set_translation_glossary_item` accepts `source` and `target` strings with no length cap. The only validation is non-empty check. Glossary entries are stored inside `settings.json` as raw dict values. A malicious or buggy client could insert:

- Arbitrarily long strings (multi-MB), bloating `settings.json` on every save.
- Strings containing characters that interfere with downstream `str.replace()` in `Translator._apply_glossary()`, e.g. replacement strings with `\n` or full translated paragraphs.
- Embedded HTML/script tags — `_apply_glossary` uses plain `str.replace`, not HTML-escaped output. Since IPC is local Unix socket (not HTTP), there is no direct XSS vector in the current architecture. However glossary entries are stored persistently and could reach a future web UI or HTML report without escaping.

No `InputSanitizer` is called for glossary inputs. Comparable handlers elsewhere (e.g. `recording_comparison.py`) enforce `MAX_ITEMS` limits.

**Fix:** Add `MAX_GLOSSARY_SOURCE_LEN = 200` / `MAX_GLOSSARY_TARGET_LEN = 500` guards and a `MAX_GLOSSARY_ENTRIES = 1000` cap before saving. Strip or reject values containing null bytes.

---

## Finding 4 — LOW: Vocabulary suggestion word regex admits numeric tokens and hyphens

**File:** `KrabEar/backend/translation_service.py` lines 432–436

The word-extraction regex in `handle_get_vocabulary_suggestions` is:
```python
words = re.findall(r"[A-Za-zА-Яа-яÁÉÍÓÚáéíóúÑñÜü0-9_-]{2,}", raw)
```

This includes `0-9`, `_`, and `-`, which means pure-numeric tokens (`"42"`, `"2024"`), underscore-identifiers (`"_id"`), and hyphenated fragments (`"co-"` from truncated words) become vocabulary candidates. Stop-word filtering does not catch these. In practice, frequently transcribed year numbers or phone-number fragments will appear in the suggestions list and pollute STT vocabulary.

**Fix:** Change the regex to require at least one alphabetic character:
```python
words = re.findall(r"(?=[^0-9_-])[A-Za-zА-Яа-яÁÉÍÓÚáéíóúÑñÜü0-9_-]{3,}", raw)
```
Or, simpler: use `r"[A-Za-zА-Яа-яÁÉÍÓÚáéíóúÑñÜü]{3,}"` and separately allow hyphenated compound words.

---

## Finding 5 — LOW: Test coverage gap — `vocabulary_store` injection path untested

**File:** `KrabEar/tests/test_translation_service.py`

`TranslationService.__init__` accepts an optional `vocabulary_store` (a `VocabularyStore` instance). When injected, `handle_get_vocabulary_suggestions` calls `self._vocabulary_store.load()` instead of `self.store.load_vocabulary()`. The `_make_service()` helper in `test_translation_service.py` never passes a non-None `vocabulary_store`, so the injected-store path is completely untested. The `_make_service` factory also ignores the `vocabulary` parameter it accepts — it sets `store.load_vocabulary.return_value = vocabulary or []` but never passes the list to a real `VocabularyStore`, meaning test cases 21 and 23 only exercise the `store.load_vocabulary()` fallback, not the `VocabularyStore.load()` path.

**Fix:** Add a test case that instantiates `TranslationService` with a real (or mock) `VocabularyStore` and verifies that `vocabulary_store.load()` is called and its result filters suggestions correctly.

---

## Interaction with W1161 (translator cache lock + privacy clear)

W1161 addressed lock correctness in `TranslationCache._persist()`. Finding 2 above confirms that `TranslationCache` is never instantiated in production code, so W1161's fixes currently have no operational impact. If `TranslationCache` is wired (as recommended in Finding 2), the `_persist()` lock pattern should be reviewed: `_persist()` is called after releasing `self._lock`, then re-acquires it to take a snapshot — this is safe with `threading.Lock` (non-reentrant) as long as callers always release before calling. The current `put()` and `clear()` callers do so correctly.

Privacy `clear()` interaction: when `privacy_mode` is enabled, `TranslationCache.clear()` is never called by any code path (the cache is not wired at all). Once wired, a `clear()` call should be added to the privacy-mode activation handler.
