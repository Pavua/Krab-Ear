# Audit W1012 — `core/auto_glossary.py`

**Date:** 2026-05-26  
**Auditor:** Wave 1012 sub-agent  
**File:** `KrabEar/core/auto_glossary.py` (380 lines)  
**Branch:** `audit/wave1012-auto-glossary`

---

## Summary

`AutoGlossaryBuilder` builds and caches a domain glossary from STT history to inject into Whisper `initial_prompt`. It is wired and active in production via `RecordingCoreService`. 6 findings identified (1 MEDIUM, 5 LOW/INFO). No critical issues; core logic is sound.

---

## Findings

### F1 — MEDIUM: Non-atomic disk write (torn cache on crash/kill)

**Location:** `auto_glossary.py:364–378` (`_save_cache_to_disk`)

`path.write_text(...)` writes directly to `auto_glossary.json` without an atomic tmp→rename pattern. If the process is killed mid-write (e.g., SIGTERM from `BackendSupervisor`), the file is left in a partially-written state. On next startup `_load_cache_from_disk` would catch the JSON parse error and silently fall back to an empty cache — behaviour is safe but the cache is lost, triggering an immediate expensive rebuild.

**Pattern used elsewhere:** `StateStore` uses `filelock` + append-only NDJSON; `SettingsBackup` uses a rolling backup before writes.

**Fix:** Write to `auto_glossary.json.tmp`, then `os.replace()` (POSIX-atomic on same filesystem).

```python
import os, tempfile
tmp = path.with_suffix(".tmp")
tmp.write_text(json.dumps(...), encoding="utf-8")
os.replace(tmp, path)
```

---

### F2 — LOW: `build()` cache-hit ignores the current call's `top_n`

**Location:** `auto_glossary.py:232–234`

```python
if not force and self._is_cache_valid():
    return list(self._cache[:top_n])
```

On a cache hit the result is sliced to `top_n`. However `_cache` was populated by the *previous* call's `top_n` (stored in `self._cache = terms` at line 243, where `terms` already has at most the old `top_n` elements). If `RecordingCoreService` later calls `build(top_n=10)` after a prior `build(top_n=30)`, it correctly gets ≤10 terms. But if it calls `build(top_n=50)` after a `build(top_n=30)` that filled the cache, it silently returns only 30 terms — the cache cannot satisfy the larger request. There is no check that the stored cache was built with at least the requested `top_n`.

**Practical impact:** Low — callers consistently pass `top_n=30` (config default); but a IPC `refresh_auto_glossary` with `top_n=50` followed by a normal transcription would read a stale-sized cache.

**Fix:** Store `_cache_top_n` alongside the cache and treat a hit as stale when `top_n > _cache_top_n`.

---

### F3 — LOW: No thread-safety on cache fields

**Location:** `auto_glossary.py:205–206, 243–244, 255–261`

`_cache` and `_cache_built_at` are plain Python lists/floats modified without a lock. `RecordingCoreService` calls `build()` from the transcription handler thread. If a background routine also calls `build(force=True)` (e.g., from a future cron refresh), there is a race on the assignment pair:

```python
self._cache = terms        # line 243
self._cache_built_at = ...  # line 244
```

These two writes are not atomic — a concurrent reader could see the new cache with the old `_cache_built_at`, causing an immediate spurious rebuild on the next call.

**Test coverage:** `test_concurrent_build` (Wave 133) tests for exception-safety but does not assert cache-value consistency under races.

**Fix:** Wrap `_cache`/`_cache_built_at` mutations in a `threading.Lock`.

---

### F4 — LOW: Privacy-mode bypass — history read ignores `privacy_mode_enabled`

**Location:** `recording_core_service.py:916–922` (caller), `auto_glossary.py:271–336` (`_build_from_history`)

`privacy_mode_enabled` is checked in `translation_service.py` and `observability.py`, but there is no such guard in either `AutoGlossaryBuilder._build_from_history` or its call site in `RecordingCoreService`. When privacy mode is enabled, the glossary builder still reads all history items and may persist extracted terms (capitalized proper nouns, names) to `auto_glossary.json` on disk.

This is a privacy consistency gap: enabling privacy mode suppresses translation history and Sentry, but the auto-glossary feedback loop continues to accumulate and persist identifying vocabulary.

**Fix:** In `RecordingCoreService`, skip the `auto_glossary.build()` call when `privacy_mode_enabled` is True. Optionally call `self._auto_glossary.invalidate()` on privacy-mode enable.

---

### F5 — LOW: `extract_terms()` called without locale hint — ES/EN terms may be under-extracted

**Location:** `auto_glossary.py:311`

```python
extracted = self._extractor.extract_terms(raw_text)
```

`TermExtractor.extract_terms` accepts a `language` parameter (default `"ru"`) that controls stop-word sets. The call site passes no language, so all history items are processed as Russian regardless of the actual transcript language. For ES or EN transcriptions this means ES/EN stop-words are not applied, allowing common Spanish/English words to leak into the glossary (e.g., "tiene", "están", "that", "with") if they happen to be capitalized mid-sentence (e.g., as first word of an embedded sentence or named entity).

**Fix:** Read `item.get("language", "ru")` from the history item dict and pass it to `extract_terms(raw_text, language=lang)`.

---

### F6 — INFO: `get_auto_glossary` / `refresh_auto_glossary` IPC handlers not registered in production

**Location:** `KrabEar/backend/service.py` (handler dispatch table)

The test file `test_auto_glossary.py` defines `_stub_get_auto_glossary` and `_stub_refresh_auto_glossary` as stubs that "replicate `_handle_get_auto_glossary` / `_handle_refresh_auto_glossary`" — but neither handler exists in `service.py`'s dispatch table (confirmed by `grep`). The auto-glossary builder is wired internally (invoked at transcription time inside `RecordingCoreService`), but there are no IPC methods to:

- Inspect the current cached glossary from the Swift UI.
- Manually trigger a cache refresh without restarting.
- Toggle or monitor the auto-glossary state from the history panel.

The test class `TestAutoGlossaryIpcHandlers` tests stub functions, not real handlers.

**Recommendation:** If the IPC API surface is intentionally internal-only, rename the test class and stubs to avoid misleading the "replicates handler" comment. If the handlers are desired (e.g., for a future diagnostics panel), register `_handle_get_auto_glossary` and `_handle_refresh_auto_glossary` in the dispatch table.

---

## Wire Status

| Aspect | Status |
|---|---|
| Instantiation in `BackendService.__init__` | Wired (`service.py:462`) |
| Delegation to `RecordingCoreService` | Wired (`service.py:498`) |
| Called at transcription time | Active (`recording_core_service.py:918`) |
| IPC handlers (`get_auto_glossary`, `refresh_auto_glossary`) | **Not registered** (F6) |
| `auto_glossary_enabled` toggle respected | Yes (at call site) |
| Privacy mode guard | **Missing** (F4) |

## Test Coverage

`KrabEar/tests/test_auto_glossary.py` — 40+ test cases covering: empty history, top-N, date filtering, cache TTL, force-rebuild, disk persistence, corrupt cache, concurrent build, hallucination filter, Unicode terms, and deduplication. Coverage is broad. Gaps: thread-safety consistency assertions (F3), locale injection (F5), privacy-mode bypass (F4).
