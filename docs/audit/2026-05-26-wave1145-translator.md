# Audit W1145 — Translator (offline-first, RU↔ES/EN, cache, thread-safety)

**Date:** 2026-05-26  
**Scope:** `KrabEar/backend/translator.py` (769 lines) + `KrabEar/backend/translation_service.py`  
**Branch:** `fix/search-index-W1041` → auditing off `codex/krab-ear-v2`

---

## Summary

6 findings (2 HIGH, 2 MEDIUM, 2 LOW). The translator is functionally correct and well-structured; the issues are subtle concurrency, privacy, and language-detection gaps that are latent in production.

---

## Findings

### F1 — HIGH: `_cache` dict is not thread-safe (data race on concurrent IPC calls)

**File:** `KrabEar/backend/translator.py`, lines 103, 708–721  
**Severity:** HIGH

`Translator._cache` is a plain `collections.OrderedDict`. `_cache_get` (line 708) calls `self._cache.get(key)` followed by `self._cache.move_to_end(key)`, and `_cache_set` (line 716) modifies length and re-orders. Both are non-atomic.

`BackendService` (and therefore `TranslationService`) serves IPC requests on a single thread today, but `live_subs_service.py` calls `translator.translate()` from its own background thread. Additionally, `BackendService._handle_request_async` paths (if any future caller) would race. CPython's GIL does not protect multi-step read-modify-write sequences across two `dict` operations.

**Concrete race:** two threads simultaneously in `_cache_get` for the same key → both call `move_to_end` → one `OrderedDict` node points to stale neighbors → `ValueError: dict changed size during iteration` or silent corruption.

**Fix:**  
```python
import threading

def __init__(self) -> None:
    ...
    self._cache_lock = threading.RLock()

def _cache_get(self, key):
    with self._cache_lock:
        value = self._cache.get(key)
        if value is None:
            return None
        self._cache.move_to_end(key)
        return value

def _cache_set(self, key, value):
    with self._cache_lock:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_capacity:
            self._cache.popitem(last=False)
```

---

### F2 — HIGH: privacy_mode does not purge in-RAM translation cache

**File:** `KrabEar/backend/translation_service.py`, lines 96–105 and 201–210  
**Severity:** HIGH

When `privacy_mode_enabled=True`, `handle_translate_text` and `handle_translate_selection` correctly force `network_mode="offline_only"` and log to `privacy_audit`. However, the `_cache` in `Translator` (up to 500 entries) is never cleared on privacy-mode enable or on `purge_history` IPC. Translated text — including source phrases submitted before privacy mode was enabled — persists in RAM for the lifetime of the process.

This is a privacy-mode contract violation: a user who enables privacy mode to prevent further data retention still has old translations sitting in the in-process LRU dict. A privacy purge should atomically clear `translator._cache`.

**Fix:** add a `clear_cache()` public method to `Translator`, call it from the IPC handler that enables privacy mode and/or from a dedicated `purge_translator_cache` IPC method. Cross-ref: `history_service.py` has an analogous `cleanup_old_history` path.

---

### F3 — MEDIUM: Bilingual mode is sequential, not parallel — doubles latency

**File:** `KrabEar/backend/translator.py`, lines 260–311  
**Severity:** MEDIUM

`_translate_bilingual_ru_es` performs one translation call (e.g. `ru_to_es`) and formats the result. It does NOT perform a second call for the reverse direction. The bilingual output is `"RU: <original>\nES: <translated>"` — i.e. it only translates in one direction. This is by design for this specific pair, but the docstring says "два языка в одном сообщении," which is correct.

However, if the input is already in RU and the mode is `bilingual_ru_es`, the pipeline is:
1. `_detect_source_language(text)` → sequential heuristic detection
2. `_translate_with_model(...)` → sequential model call
3. String format

There is no parallelism. For future extension to true bidirectional output (RU→ES **and** ES→RU simultaneously), the sequential path would double latency. No `ThreadPoolExecutor` is used.

**Current state:** not broken, but the architecture does not scale to bidirectional without a refactor. Document this as a design limitation if bidirectional is desired.

---

### F4 — MEDIUM: `_detect_source_language` (inline heuristic) diverges from `LanguageDetector` used in `TranslationService`

**File:** `KrabEar/backend/translator.py`, lines 589–638 vs `KrabEar/core/language_detector.py`  
**Severity:** MEDIUM

`Translator` has its own inline `_detect_source_language` (static method, 50 lines) that handles RU/ES/EN/DE via marker-word scoring. `TranslationService.handle_translate_selection` uses a separate `LanguageDetector` instance (`self._lang_detector = LanguageDetector()` from `core/language_detector.py`), which is a char-based Unicode approach supporting RU/UK/ES/EN only (no DE).

Two divergences:

1. **German (DE):** `Translator._detect_source_language` supports DE via marker words and umlauts; `LanguageDetector._detect_latin` only returns `"es"` or `"en"` — German text is classified as `"en"` at the `LanguageDetector` layer. A `translate_selection` call with German text would map it to `"en"` → target `"ru"` → `en_to_ru` mode, instead of `"de"` → `de_to_en`.

2. **W1019 FR/TR/PT false positives:** Both detectors have no coverage for French, Turkish, or Portuguese. Latin script text in these languages falls through to `"en"` (in `LanguageDetector`) or scores as `"en"` via marker-word scoring (in `_detect_source_language`). This means FR/TR/PT text submitted via `translate_selection` IPC is silently treated as EN and translated EN→RU. The `_AUTO_DIRECTION` map in `TranslationService` has no `"fr"`, `"pt"`, or `"tr"` entries. There is no Helsinki-NLP model for FR/PT/TR in `_MODEL_BY_MODE`.

**Fix (minimal):** add `_detect_latin` improvement in `LanguageDetector` for DE (umlaut check: `äöüß`), mirroring what `Translator._detect_source_language` already does. Longer term: unify both code paths to use the same detector.

---

### F5 — LOW: Cache key includes `network_mode` — offline and online hits for same text never share entries

**File:** `KrabEar/backend/translator.py`, lines 183–188  
**Severity:** LOW

Cache key is `(normalized_mode, normalized_style, normalized_network_mode, clean_text)`. This means a text translated offline will be re-translated if the user later calls with `online_opt_in` (and vice versa), even though the result from a local model is identical regardless of network policy (the policy only affects whether the model is *downloaded*, not the output).

This wastes cache slots (500 entries split across `offline_default`, `offline_strict`, `online_opt_in` variants of the same text) and causes redundant inference for the common workflow of toggling network mode mid-session.

**Fix:** remove `network_mode` from the cache key. It influences model loading only, not translation output. If the model was loaded offline and a subsequent online call arrives for the same text, the cached (offline) result is equally valid.

---

### F6 — LOW: No IPC method to inspect or reset the in-process translation cache

**File:** `KrabEar/backend/service.py` (handler table) + `KrabEar/backend/translator.py`  
**Severity:** LOW

There is no `clear_translator_cache`, `get_translator_cache_stats`, or `reset_unavailable_models` IPC method. The `_unavailable` set (which permanently marks Helsinki-NLP models as absent for the process lifetime) can only be reset by restarting the backend. If a model becomes available after initial load failure (e.g. user downloads it mid-session), there is no way to clear the unavailability flag without a restart.

Similarly, `_pipelines` dict grows unbounded — up to 8 Marian + up to 8×4=32 NLLB pipeline objects — with no eviction. For memory-constrained sessions, this is a hidden leak vector.

**Fix:** expose `reset_translator_state` IPC method that clears `_cache`, `_unavailable`, and optionally `_pipelines`. Low-cost to add, high operational value.

---

## IPC Handler Coverage

| Method | Registered | Notes |
|---|---|---|
| `translate_text` | yes (line 932) | VERIFIED Swift caller |
| `translate_selection` | yes (line 933) | Phase 2A workflow |
| `set_translation_glossary_item` | yes (line 935) | VERIFIED Swift caller |
| `remove_translation_glossary_item` | yes (line 937) | |
| `get_glossary_suggestions` | yes (line 961) | |
| `get_vocabulary_suggestions` | yes (line 961) | |
| `clear_translator_cache` | **missing** | See F2, F6 |
| `reset_unavailable_models` | **missing** | See F6 |

---

## Wire Status

- `Translator` is instantiated in `BackendService.__init__` (line 207) and passed to `TranslationService` (line 321).
- `ErrorBus` late-injection pattern (`_error_bus` attribute) matches `LLMRewriter` and `AudioEngine` — consistent, no drift found.
- Privacy audit log calls are correctly gated on `privacy_mode_enabled` in both `handle_translate_text` and `handle_translate_selection`.
- `_normalize_network_mode` accepts `"offline_only"` as an un-normalized value (maps to `"offline_default"`). The privacy override sets `network_mode = "offline_only"` but `_normalize_network_mode` will map it back to `"offline_default"`. **Net effect is the same** (both block network), but the cache key will use `"offline_default"` for privacy-forced calls — cache entries from privacy-forced calls will hit for non-privacy calls with the same text and mode. Mild leak: non-privacy callers may get back a result that was computed under privacy mode (offline-only). This is benign in terms of content correctness but violates the semantic separation.

---

## Glossary Integration (W935)

`_apply_glossary_to_result` is applied **after** cache retrieval (line 191) and after caching (lines 212, 221). The cache stores the **pre-glossary** result; the glossary is applied dynamically on every lookup. This is correct: the same cached translation can serve different glossaries without stale entries. W935 fix is confirmed in place.

---

## Recommendations (priority order)

1. **F1** — Add `threading.RLock` around `_cache` operations (live thread-safety issue).
2. **F2** — Clear `translator._cache` on `privacy_mode_enabled` toggle or `purge_history`.
3. **F4** — Align `LanguageDetector._detect_latin` to handle DE (umlaut check); document FR/PT/TR as unsupported.
4. **F5** — Remove `network_mode` from cache key.
5. **F3** — Document bilingual mode as sequential by design; no code change unless bidirectional is required.
6. **F6** — Add `reset_translator_state` IPC method.
