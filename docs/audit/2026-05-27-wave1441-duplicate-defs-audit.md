# W1441 Meta-Audit: Duplicate Function/Method Definitions

**Date:** 2026-05-27
**Wave:** W1441
**Type:** CRIT meta-audit — root-cause investigation
**Scope:** All `KrabEar/**/*.py` (232 files, excludes tests/, .venv*/, worktrees/, .claude/)
**Tooling:** `scripts/audit_duplicate_defs.py` (AST-based, @property pairs excluded)

---

## Background

Two shipped bugs revealed a systemic pattern:
- **W1425 F1 (translator.py):** `clear_cache` and `_check_privacy_mode_changed` had W1313-era fix
  definitions at lines ~115/134 silently shadowed by older definitions lower in the class body.
  The W1313 fixes (disk-cache clearing, `_translation_cache` integration) were entirely dead at
  runtime because Python uses the *last* definition in a scope.
- **W1438 F1+F2 (audio_lang_id.py):** `clear_model_cache` had W1405 fix (mx.clear_cache() for
  Metal GPU buffer release) shadowed by the original stub below it. Additionally, `_HAS_MLX` was
  set twice via two separate try/except import blocks.

This meta-audit systematically scans the full codebase for all such duplicate definitions.

---

## Scan Results Summary

| Category | Count |
|---|---|
| Files scanned | 232 |
| Total duplicate pairs found | 17 |
| **Genuine shadowing bugs** | **4** |
| @property getter/setter pairs (false positives) | 13 |
| Files with genuine bugs | 3 |

---

## Genuine Shadowing Bugs (4 findings)

### Finding 1 — `KrabEar/backend/translator.py` — `clear_cache` — CRIT

| Field | Value |
|---|---|
| File | `KrabEar/backend/translator.py` |
| Class | `Translator` |
| Method | `clear_cache` |
| Fix definition (line) | 115 |
| Shadowing definition (line) | 184 |
| Suspected fix wave | W1313 F2 |
| Severity | CRIT |

**Analysis:** The fix at line 115 clears *both* the in-memory LRU cache AND the persistent disk
cache (`_translation_cache`, injected at runtime per W1190). The shadowing version at line 184
uses `_cache_lock` but only clears the in-memory dict — entirely missing the disk layer.
All callers (privacy mode transition, explicit IPC clear) call the shadowing version, so
the disk cache is never cleared, defeating the purpose of W1313.

**Recommendation:** Remove lines 184–187 (the shadowing simple version). The W1313 version at
line 115 is the correct authoritative implementation.

---

### Finding 2 — `KrabEar/backend/translator.py` — `_check_privacy_mode_changed` — CRIT

| Field | Value |
|---|---|
| File | `KrabEar/backend/translator.py` |
| Class | `Translator` |
| Method | `_check_privacy_mode_changed` |
| Fix definition (line) | 134 |
| Shadowing definition (line) | 189 |
| Suspected fix wave | W1313 F2 |
| Severity | CRIT |

**Analysis:** The fix at line 134 takes `privacy_mode_enabled: bool` as argument, tracks
the previous state in `_last_privacy_mode`, and fires on ANY transition (True→False and
False→True). The shadowing version at line 189 takes no argument and requires `_error_bus`
+ `_settings_getter` to be injected — returns early silently if either is absent. The two
implementations have incompatible signatures: callers in `translate()` pass a bool argument,
which raises a `TypeError` against the no-arg shadowing version at runtime if `_error_bus`
is not set.

**Recommendation:** Remove lines 189–209 (the shadowing version). The W1313 version at
line 134 is the correct authoritative implementation.

---

### Finding 3 — `KrabEar/core/audio_lang_id.py` — `clear_model_cache` — HIGH

| Field | Value |
|---|---|
| File | `KrabEar/core/audio_lang_id.py` |
| Class | `AudioLanguageID` |
| Method | `clear_model_cache` |
| Fix definition (line) | 63 |
| Shadowing definition (line) | 91 |
| Suspected fix wave | W1405 F2 |
| Severity | HIGH |

**Analysis:** The fix at line 63 calls `mx.clear_cache()` after clearing `_model_cache`,
releasing Metal GPU buffers (~300–500 MB) — the core fix for the W1405 memory leak.
The shadowing version at line 91 only clears `_model_cache` under `_cache_lock` but
omits the `mx.clear_cache()` call entirely. All runtime invocations (settings hook,
W63 memory fix path) silently skip the Metal buffer release.

**Recommendation:** Remove lines 90–100 (the shadowing version). Keep the W1405 version
at line 63. The lock from the shadowing version should be incorporated: add
`with cls._cache_lock:` around the model cache clear in line 63's version.

---

### Finding 4 — `KrabEar/core/audio_quality.py` — `_safe_float` — HIGH

| Field | Value |
|---|---|
| File | `KrabEar/core/audio_quality.py` |
| Class | `<module>` |
| Function | `_safe_float` |
| Fix definition (line) | 20 |
| Shadowing definition (line) | 40 |
| Severity | HIGH |

**Analysis:** The fix at line 20 accepts `(v: float, default: float = 0.0)` — has a
configurable default value and also guards against non-numeric types (`isinstance` check).
The shadowing version at line 40 accepts only `(v: float)` with hardcoded zero default.

**Critical runtime impact:** Line 159 calls `_safe_float(silence_ratio, 1.0)` with an
explicit custom default (1.0 = treat missing silence_ratio as fully silent, which is
safety-conservative). The shadowing version raises `TypeError: _safe_float() takes 1
positional argument but 2 were given` whenever this path is hit. This crashes
`AudioQualityAnalyzer.analyze()` silently (the TypeError is likely caught upstream and
logged, masking it from users, but audio quality reports are wrong).

**Recommendation:** Remove lines 40–42 (the shadowing version). The version at line 20
is the correct authoritative implementation.

---

## False Positives — @property Getter/Setter Pairs (13 findings, all OK)

These are valid Python `@property` + `@<name>.setter` patterns. The AST scanner correctly
identifies them as such. Documented here for completeness.

| File | Class | Property name |
|---|---|---|
| `KrabEar/backend/keyword_cloud.py` | `KeywordCloudGenerator` | `_stop_words` |
| `KrabEar/backend/llm_rewriter.py` | `LLMRewriter` | `_timeout` |
| `KrabEar/backend/paste_app_memory.py` | `PasteAppMemory` | `enabled` |
| `KrabEar/backend/service.py` | `BackendService` | `_clipboard_history` |
| `KrabEar/backend/service.py` | `BackendService` | `_last_stt_engine` |
| `KrabEar/backend/service.py` | `BackendService` | `_list_audio_inputs` |
| `KrabEar/backend/service.py` | `BackendService` | `_preview_error_count` |
| `KrabEar/backend/service.py` | `BackendService` | `_preview_error_last_reset_ts` |
| `KrabEar/backend/service.py` | `BackendService` | `_preview_updated_at` |
| `KrabEar/backend/service.py` | `BackendService` | `_rt_partial` |
| `KrabEar/backend/service.py` | `BackendService` | `_rt_session_id` |
| `KrabEar/backend/service.py` | `BackendService` | `_transcription_counter` |
| `KrabEar/backend/service.py` | `BackendService` | `recorder` |

---

## Root Cause Analysis

All genuine duplicates share the same pattern: a *new, improved* definition was added at
the top of a method group but the *original* definition was not deleted. This happens when:

1. A wave adds a fix by inserting new code, but the PR diff review misses that the old
   version exists lower in the file.
2. The fix method has extra parameters or additional logic vs. the original — the test
   suite may call the new signature, passing, while production hits the shadowing version
   with the old signature.
3. Both versions share the same name and scope — Python silently uses only the last one.

**Secondary issue (audio_lang_id.py):** `_HAS_MLX` is set twice at module level (lines 28 and
40) via two separate try/except import blocks. This is a non-function duplicate (not caught
by the function-def scanner) and is harmless (both set the same boolean), but indicates the
same pattern of "add new code, forget to remove old". Tracked as a bonus finding.

---

## CI Guard

A new job `duplicate-defs-guard` has been added to `.github/workflows/ci.yml`:

```yaml
duplicate-defs-guard:
  name: Duplicate definitions guard (W1441)
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - name: Check for duplicate function/method definitions
      run: python3 scripts/audit_duplicate_defs.py --fail-on-found
```

The guard:
- Runs `scripts/audit_duplicate_defs.py --fail-on-found`
- Exits 0 if only @property pairs found (false positives correctly excluded)
- Exits 1 if any genuine shadowing duplicate found
- Requires no dependencies beyond stdlib `ast` — runs on `ubuntu-latest` without pip

---

## Recommended Fix Waves

| Priority | Wave | File | Action |
|---|---|---|---|
| CRIT | W1442 | `backend/translator.py` | Remove shadowing `clear_cache` (line 184) and `_check_privacy_mode_changed` (line 189) |
| HIGH | W1443 | `core/audio_lang_id.py` | Remove shadowing `clear_model_cache` (line 91); incorporate lock into W1405 version |
| HIGH | W1444 | `core/audio_quality.py` | Remove shadowing `_safe_float` (line 40) |

After fixes, the CI guard will pass with 0 genuine duplicates.

---

## Tooling

**Script:** `scripts/audit_duplicate_defs.py`

```
python3 scripts/audit_duplicate_defs.py              # Report mode
python3 scripts/audit_duplicate_defs.py --fail-on-found  # CI gate mode (exit 1 if bugs found)
```

The script uses Python's `ast` module to walk all `.py` files under `KrabEar/`, groups
function definitions by scope (module or class body), and detects any name appearing more
than once. It correctly distinguishes `@property` + `@name.setter` pairs (valid Python)
from genuine duplicate definitions (bugs).
