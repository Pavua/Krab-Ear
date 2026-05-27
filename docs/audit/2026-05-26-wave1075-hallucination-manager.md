# Wave 1075 — HallucinationManager Audit

**Date:** 2026-05-26  
**File:** `KrabEar/core/hallucination_manager.py`  
**Tests run:** 42 passed (0 failed)

---

## Summary

`HallucinationManager` is a well-structured module with a clean lock model, good corruption resilience, and thorough test coverage (42 tests). Five findings were identified, ranging from a critical pipeline-integration gap to minor coverage and atomicity issues.

---

## Findings

### F-1 (HIGH) — Manager is instantiated but never used in the STT pipeline

`BackendService.__init__` instantiates `self._hallucination_manager` (line 380 of `service.py`) but **no code calls any method on it**. The actual hallucination stripping in production goes through `TextUtils._strip_hallucinations` (called from `core/utils.py` lines 488, 606 in `engine.py`), which reads from the separate `_HALLUCINATION_PATTERNS` list in `utils.py` — completely bypassing any user-added custom patterns.

Result: all custom patterns added via the manager are silently ignored by the STT pipeline. The manager is dead weight in production.

**Fix:** Wire `_hallucination_manager.strip_hallucinations()` into `engine.py`'s cleanup path, replacing or supplementing the direct call to `TextUtils._strip_hallucinations`.

---

### F-2 (HIGH) — No IPC handlers exposed — CRUD API is unreachable

There are zero IPC handlers for the manager's CRUD operations. The following methods exist in Python but have no corresponding JSON-RPC method wired in `service.py`:

- `add_pattern(pattern, category)`
- `remove_pattern(pattern)`
- `list_patterns()`
- `check_text(text)`

`grep` over `service.py` for `add_hallucination`, `remove_hallucination`, `list_hallucination`, and `hallucination_patterns` returns no results. The Swift side also has no callers. The entire user-facing API is unreachable from the app.

**Fix:** Add IPC handlers `add_hallucination_pattern`, `remove_hallucination_pattern`, `list_hallucination_patterns`, `check_hallucination` to `BackendService.handle_request`.

---

### F-3 (MEDIUM) — Duplicate builtin pattern list — drift risk

`hallucination_manager.py` maintains `_BUILTIN_PATTERNS_RAW` (15 entries) as a hardcoded copy of the patterns in `core/utils.py::_HALLUCINATION_PATTERNS`. The two lists are currently identical (confirmed), but there is no enforcement. Any future update to `utils.py` patterns will not automatically propagate to the manager.

**Fix:** Remove `_BUILTIN_PATTERNS_RAW` from `hallucination_manager.py`. Import the raw pattern strings from `utils.py` directly (or expose a `_HALLUCINATION_PATTERNS_RAW` constant in `utils.py` that both consumers reference).

---

### F-4 (MEDIUM) — Non-atomic persist (`write_text` without temp-file rename)

`_save_custom` writes directly to `hallucination_patterns.json` via `Path.write_text()` (line 102–105). A crash or power loss mid-write corrupts the file. The load path gracefully handles corruption by falling back to an empty list, so no data is permanently lost — but patterns added in the current session would be silently dropped.

Existing pattern (used by `StateStore`) is write-to-temp-then-rename for atomic persistence.

```python
# Current (non-atomic)
self._persist_path.write_text(json.dumps(...), encoding="utf-8")

# Recommended (atomic)
tmp = self._persist_path.with_suffix(".tmp")
tmp.write_text(json.dumps(...), encoding="utf-8")
tmp.replace(self._persist_path)
```

---

### F-5 (LOW) — Pattern coverage gaps: English and standalone "субтитры" variants missing

The 15 builtin patterns cover Russian YouTube phrases and `"to be continued"` (EN). Common Whisper hallucinations that are not covered:

| Missing pattern | Context |
|---|---|
| `thank you for watching[.!?…]*$` | EN YouTube, very frequent Whisper hallucination |
| `please subscribe[.!?…]*$` | EN YouTube |
| `субтитры[.!?…]*$` (standalone) | RU — current pattern only matches `"субтитры сделал <name>"` |
| `редактирование субтитров[.!?…]*$` | RU subtitle credit variant |
| `gracias por ver[.!?…]*$` | ES YouTube |

Given the project's RU/ES/EN trilingual focus, adding the ES pattern and the EN standalone phrases would materially improve recall for the primary audience.

---

## What is working well

- **Lock model:** `_lock` (threading.Lock) wraps all mutating operations. `check_text` and `strip_hallucinations` snapshot `self._compiled` inside the lock and iterate outside, avoiding lock-held regex execution. Correct.
- **Regex validation:** `add_pattern` compiles the regex before storing it and raises `ValueError` on `re.error`. Invalid patterns cannot enter storage.
- **Pattern injection risk (not an issue):** Users supply raw regex strings, which is the intended design. `re.escape` is not appropriate here. The `ValueError` on invalid regex is sufficient guard.
- **Corruption resilience:** `_load_custom` catches all exceptions and falls back to an empty custom list. Builtin patterns are never affected by corrupt storage (confirmed by test `test_corrupted_json_does_not_lose_builtins`).
- **Test coverage:** 42 unit tests across 9 test classes. Concurrency (20-thread stress), Unicode, persistence, corruption, and edge cases are all covered.
- **Case-insensitive matching:** Consistent `.lower()` applied to input before pattern search — correct for RU/ES/EN Cyrillic and Latin scripts.

---

## Verdict

The module is internally correct and well-tested. The critical issues are architectural: it is instantiated but disconnected from both the pipeline (F-1) and the IPC layer (F-2), making all user customization effectively a no-op in production. F-3 and F-4 are maintenance risks. F-5 is a quick coverage win.
