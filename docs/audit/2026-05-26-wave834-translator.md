# Audit: backend/translator.py — Wave 834

**Date**: 2026-05-26  
**File**: `KrabEar/backend/translator.py`  
**Lines**: 809  
**Auditor**: Claude Sonnet 4.6 (automated)

---

## Summary

Overall the module is well-structured: clean separation between `translate()` (public, telemetry wrapper) and `_translate_impl()` (business logic), correct LRU eviction, and a solid NLLB-200 fallback path. Seven findings were identified — two medium and five low severity. No critical or high issues.

**Findings: 7 (2 medium, 5 low)**

---

## Findings

### MEDIUM-1 — Cache key includes `network_mode`, causing unnecessary cache misses

**Severity**: MEDIUM  
**Location**: `_translate_impl()` lines 222–227

```python
cache_key = (
    normalized_mode,
    normalized_style,
    normalized_network_mode,   # <-- included
    clean_text,
)
```

The `network_mode` parameter controls which HuggingFace model download policy is used at pipeline-build time. Once a pipeline is loaded, the resulting translation is identical regardless of the original network mode. Including `network_mode` in the cache key means the same text+mode+style tuple generates two separate cache entries (one for `offline_default`, one for `online_opt_in`), doubling cache pressure and defeating the deduplication purpose for realtime/repeated requests.

**Fix**: Remove `normalized_network_mode` from `cache_key`. Pipeline availability is already handled by the `_unavailable` set; the translation result itself is network-mode-independent once produced.

---

### MEDIUM-2 — `_unavailable` set is never cleared; NLLB fallback permanently blocked after first failure

**Severity**: MEDIUM  
**Location**: `_translate_with_model()` lines 399–408, `_try_nllb_fallback()` lines 481–483

After a single model-unavailability failure the pipeline key is added to `self._unavailable`. There is no TTL, no manual reset, and no IPC-exposed reset method. Consequences:

1. If the user later copies a model to the cache directory (e.g. after an offline download on another machine), the translator silently returns `model_unavailable_cached` for the lifetime of the process — no recovery without restart.
2. The NLLB fallback path is equally blocked: once a `nllb_key` enters `_unavailable`, all subsequent calls for that language pair skip NLLB entirely and return empty string, even if the user switches to `online_opt_in` network mode.

The `allow_network` flag is part of both cache keys (`pipeline_key = (model_name, allow_network)` and `nllb_key = (..., allow_network, ...)`), so a mode switch does produce a different key — but a prior `offline_default` failure blocks that key permanently without the user knowing why.

**Fix**: Add a `reset_unavailable()` method (callable from IPC `restart` or settings change), or add a timestamp to each `_unavailable` entry and evict entries older than a configurable TTL (e.g. 5 minutes). Also expose the set size in diagnostics.

---

### LOW-1 — Error code mismatch: `translation.timeout` used for all pipeline errors, not just timeouts

**Severity**: LOW  
**Location**: `_translate_with_model()` lines 436–440

```python
self._push_error(
    "translation.timeout",
    f"{type(exc).__name__}: {exc} (mode={resolved_mode})",
    severity="warn",
)
```

This block fires on any exception raised during pipeline inference — including `RuntimeError`, `ValueError`, `MemoryError`, or a broken model file. Using `translation.timeout` for all of them makes Sentry grouping misleading and the user-facing message ("Перевод превысил лимит времени") incorrect for non-timeout failures.

**Fix**: Check `type(exc).__name__` or `isinstance(exc, TimeoutError)` and dispatch to a more appropriate error code, or introduce `translation.inference_error` as a general inference failure code.

---

### LOW-2 — `_apply_glossary` uses plain `str.replace` — order-dependent and no word-boundary guard

**Severity**: LOW  
**Location**: `_apply_glossary()` lines 727–731

```python
for source, target in glossary.items():
    result = result.replace(source, target)
```

Two problems:

1. **Order dependency**: if the glossary contains `{"run": "ejecutar", "running": "corriendo"}`, applying `run` first turns `"running"` into `"ejecutarring"`.
2. **Substring collision**: a glossary term `"an"` will match inside `"translation"` → `"treslación"`.

**Fix**: Apply glossary terms longest-first (sort by `len(source)` descending), and wrap each replacement in `\b` word-boundary regex if terms are whole-word tokens. For multi-word terms plain replace is fine; single-word terms need boundary checks.

---

### LOW-3 — `_detect_source_language` is not thread-safe under concurrent calls

**Severity**: LOW  
**Location**: `_detect_source_language()` — static method, lines 628–677

The method itself is stateless (static), but `translate()` is an instance method with no locking. The `_pipelines` dict and `_unavailable` set are shared mutable state on `self`. Multiple concurrent callers from IPC (which dispatches on a thread pool in `IPCServer`) can race:

- `_pipelines.get(pipeline_key)` + `_pipelines[pipeline_key] = pipeline` is not atomic.
- `_unavailable.add(pipeline_key)` during one thread's failure can silently suppress a concurrent thread's legitimate first attempt.

The parent `BackendService`/`TranslationService` may serialize access, but `Translator` itself has no guard.

**Fix**: Add a `threading.Lock` (or per-key `threading.Lock` for finer granularity) around `_pipelines` mutation and `_unavailable` mutation, consistent with the pattern used in `AudioRecorder` and `MetricsCollector`.

---

### LOW-4 — `auto_to_ru` with German source falls back to DE→EN, not DE→RU

**Severity**: LOW  
**Location**: `_resolve_mode()` lines 595–596

```python
if detected == "de":
    return "de_to_en"  # DE→EN как промежуточный шаг; RU нет прямой модели DE→RU
```

The comment acknowledges this is a two-step workaround, but the second step (EN→RU) is never performed. The result returned to the caller has `target_lang="en"`, not `"ru"`, which contradicts the semantics of `auto_to_ru`. The user expects Russian output but receives English.

A correct implementation would either: (a) chain `de_to_en` + `en_to_ru` and concatenate them transparently, or (b) return `status="unsupported_pair"` with a clear message rather than a misleading English translation.

**Fix**: Either chain the two hops explicitly inside `_translate_single_mode` when `mode == "auto_to_ru"` and `detected == "de"`, or return `status="unsupported_source_for_auto_to_ru"` so the caller can surface a meaningful error.

---

### LOW-5 — `_split_text_chunks` falls back to `[clean]` when chunking produces no output, bypassing the `max_chars` limit

**Severity**: LOW  
**Location**: `_split_text_chunks()` line 808

```python
return chunks or [clean]
```

If `clean` is non-empty but all sentence-split fragments are empty strings (e.g. text consisting entirely of whitespace or punctuation after `strip()`), the method falls back to returning the full `clean` string as a single chunk even if it exceeds `max_chars=450`. This can pass a >450-char string to Marian/NLLB pipeline, which may silently truncate it or raise an error depending on the model's max token limit.

In practice the guard `if not clean: return []` at line 766 prevents the all-whitespace case, but a string like `"... ... ..."` (dots with spaces) could produce all-empty sentences after split, triggering the fallback.

**Fix**: Add a hard-truncation fallback: `return [clean[:max_chars]]` instead of `[clean]`, or log a warning when the fallback fires so the edge case is observable.

---

## Positive Notes

- **LRU implementation is correct**: `OrderedDict` + `move_to_end` on get + `popitem(last=False)` on overflow is a textbook LRU. Capacity (500) is reasonable.
- **NLLB fallback strategy is sound**: dedicated key tuple with FLORES-200 codes avoids collision with Marian keys; pipeline is cached per language-pair.
- **Error handling perimeter is complete**: every code path returns a `TranslationResult` with a descriptive `status` rather than raising, keeping the STT pipeline safe.
- **Telemetry is non-intrusive**: all `add_breadcrumb` and `_push_error` calls are wrapped in `try/except`, so Sentry failures never propagate.
- **`_build_pipeline` `TypeError` compatibility shim** (lines 693–704) correctly handles older `transformers` versions that don't accept `task=` as keyword argument.
- **`_normalize_*` methods are pure and defensive**, normalizing all inputs before they reach business logic.
