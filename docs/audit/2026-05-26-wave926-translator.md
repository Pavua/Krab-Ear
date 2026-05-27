# Wave 926 — Translator Audit (translator.py)

**Date**: 2026-05-26  
**Scope**: `KrabEar/backend/translator.py` (770 lines) + `KrabEar/backend/translation_service.py` (465 lines)  
**Method**: static analysis + test inventory  
**Verdict**: 6 findings (2 HIGH, 2 MEDIUM, 2 LOW)

---

## F1 — HIGH: Glossary replacement uses plain `str.replace`, not word-boundary regex

**Location**: `translator.py:691` `_apply_glossary`

```python
for source, target in glossary.items():
    result = result.replace(source, target)
```

`str.replace` is substring-based. Glossary term `"el"` would corrupt `"elecciones"` → `"targetelecciones"`. For Cyrillic the same applies: term `"дом"` replaces inside `"домой"`. No word-boundary protection exists.

`translation_service.py:336` in `handle_get_glossary_suggestions` uses `\b` regex for matching, but the actual application at inference time uses plain `.replace()`.

**Impact**: silent text corruption on any glossary entry that is a prefix/suffix of another word. Affects ES (el/la/un) and RU (в/на/по) entries added by auto-suggestions.

**Fix**: wrap with `re.sub(r'\b' + re.escape(source) + r'\b', target, result)`, using `re.UNICODE` flag (covers Cyrillic word boundaries).

---

## F2 — HIGH: Model `_build_pipeline` is not thread-safe — double-init race

**Location**: `translator.py:344-370` `_translate_with_model`

```python
pipeline = self._pipelines.get(pipeline_key)
if pipeline is None:
    pipeline = self._build_pipeline(...)      # <── slow (1-10s)
    ...
    self._pipelines[pipeline_key] = pipeline  # <── no lock
```

`Translator` is a shared singleton (one instance per `BackendService`). If two IPC requests for the same language pair arrive concurrently before the pipeline is cached, both threads execute `_build_pipeline()` simultaneously — loading the Marian/NLLB model twice. Each Helsinki-NLP model is ~300 MB RAM; NLLB-200 distilled is ~2.4 GB. A double-load for NLLB = +2.4 GB RSS spike.

The `_unavailable` set and `_cache` OrderedDict share the same hazard (concurrent writes are not atomic in CPython dict for all operations).

**Fix**: add a `threading.Lock` per pipeline key (checked-locking pattern), or use a class-level `RLock` for the full `_pipelines` + `_unavailable` section.

---

## F3 — MEDIUM: Language detection silent failure on single-char / all-emoji / code-switched text

**Location**: `translator.py:589-638` `_detect_source_language`

| Input | Current result | Expected |
|---|---|---|
| `""` (empty) | `""` — OK (handled by `_translate_impl`) | OK |
| `"а"` (single Cyrillic char) | `"ru"` — correct | OK |
| `"🎉🔥💬"` (emoji-only) | `"en"` (falls through all-zero default) | ambiguous, but OK |
| `"Привет amigo"` (RU+ES code-switch) | `"ru"` (Cyrillic wins immediately, ES markers not counted) | should warn |
| `"k"` (single Latin char) | `"en"` (default fallback) | debatable |

The Cyrillic check (`re.search(r"[а-яА-ЯёЁ]", sample)`) short-circuits before any scoring, so code-switched RU+ES text always detects as `"ru"` and goes `ru_to_es` — the ES portion is untranslated and presented as-is in bilingual mode. This is an edge case but likely for real transcriptions (e.g., "ладно, vamos" which is a common code-switch pattern for the target audience).

No test covers code-switched input for language detection or bilingual mode.

**Fix**: low-severity — document the Cyrillic-priority behaviour. Add warning log when Cyrillic ratio < 30% AND Spanish markers score > 2 (mixed content signal).

---

## F4 — MEDIUM: Bilingual mode — tie-breaking when both RU and ES score equally

**Location**: `translator.py:262-311` `_translate_bilingual_ru_es`

`_detect_source_language` returns `max(scores, key=...)`. For input `"no de"` (common in code-switching) both `en_score` and `es_score` may be equal (1 each). Python `max` with key returns the first maximum encountered from `{"en":1, "es":1, "de":0}` — dict ordering puts `"en"` first in Python 3.7+ insertion order, so `"en"` wins. Bilingual mode then returns `cannot_detect_language` (it only handles `"ru"` and `"es"`).

This is a silent degradation: the user sees empty bilingual output with no error surfaced to UI, only a `cannot_detect_language` status in the IPC response.

**Fix**: for bilingual mode specifically, if `detected == "en"` prefer `"es"` as a secondary fallback (caller knows context is RU/ES bilingual). Or surface `cannot_detect_language` as a user-visible notification via ErrorBus.

---

## F5 — LOW: In-memory cache unbounded on `_unavailable` set — no eviction

**Location**: `translator.py:101-103`

```python
self._unavailable: set[tuple] = set()
```

The `_cache` OrderedDict is bounded (capacity=500, LRU eviction). The `_pipelines` dict is bounded by the number of language pairs × network modes (~16 keys max). However `_unavailable` grows without bound. In practice it holds at most 16 entries (one per (model, allow_network) key), so this is not a real leak — just a documentation gap.

The bigger concern: once a model is in `_unavailable`, it stays there for the lifetime of the process. If the model becomes available mid-session (e.g., user downloads it while the backend is running), the backend must be restarted to reattempt.

**Fix**: add a TTL-based retry (`_unavailable_expires: dict[tuple, float]`) — after 10 minutes, remove from `_unavailable` and reattempt once.

---

## F6 — LOW: PII leakage risk — only partially guarded by `privacy_mode_enabled`

**Location**: `translation_service.py:96-110`, `translation_service.py:199-215`

Translation is strictly local (offline-first) by default. `network_mode=online_opt_in` is required to hit the network. Both `handle_translate_text` and `handle_translate_selection` correctly check `privacy_mode_enabled` and force `network_mode="offline_only"` when set.

However, `offline_only` is normalised to `offline_default` inside `_normalize_network_mode` (line 529-531):

```python
if clean not in {"offline_default", "offline_strict", "online_opt_in"}:
    return "offline_default"
```

`"offline_only"` is not in the accepted set, so privacy mode's `"offline_only"` silently falls through to `"offline_default"`, which then passes `allow_network=False` to the pipeline. End result is still correct (no network), but the privacy mode enforcement is fragile — it happens to work because `allow_network` only becomes `True` for `"online_opt_in"`. Any future engineer adding `"online_default"` to the network modes could silently break privacy enforcement.

**Fix**: add `"offline_only"` to the normalised set (mapping to `"offline_default"` explicitly), or raise on unrecognised `network_mode` in privacy context.

---

## Test Coverage Summary

| Area | Test files | Coverage verdict |
|---|---|---|
| Core translate() routing | `test_translator.py`, `test_translator_extended.py` | Good |
| LRU cache (bounded, eviction, key includes style) | `test_translation_edges.py` | Good |
| Bilingual RU/ES/EN edge cases | `test_translation_edges.py` | Partial (EN→cannot_detect covered, but tie-break not tested) |
| Glossary word-boundary correctness | `test_translator_glossary_deep.py` | **Missing** — no test for substring corruption |
| Thread-safety / concurrent double-init | None | **Not tested** |
| Code-switched language detection | None | **Not tested** |
| `_unavailable` TTL / retry after model available | None | Not tested |
| Privacy mode network guard | `test_translation_service.py` | Partial |
| PII — no network in offline mode | `test_translation_service.py` | Partial |

---

## Conclusion

The translator is solid for the common case (offline Marian pipelines, LRU cache, privacy guard, NLLB-200 fallback). Two issues warrant fixes before v2.1:

1. **F1 (glossary substring corruption)** — affects correctness of translated output for all users with glossary entries.
2. **F2 (double-init race)** — affects RAM stability on concurrent IPC under load; NLLB fallback makes this especially costly.

F3–F6 are documentation or low-risk operational issues.
