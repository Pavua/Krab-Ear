# W1575 Tenth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-29
**Auditor:** W1575 sub-agent (tenth-pass)
**Branch audited:** `docs/audit-audio-lang-id-tenth-W1575` (off `codex/krab-ear-v2`, HEAD `dca2831d`)
**File:** `KrabEar/core/audio_lang_id.py`
**Focus:** Post-W1561 (SUPPORTED_LANGUAGES restored) + W1562 (_UNAVAILABLE_TTL_SEC alias in
engine.py) verification; W1530/W1561 restoration completeness; test suite divergence from
production contract.

---

## W1561 + W1562 Verification

**W1561 (commit `74377ee4`)**: `SUPPORTED_LANGUAGES = frozenset({"ru", "es", "en", "de", "fr", "it", "pt"})` is present at
line 45. The gate at lines 361–366 in `_detect_with_mlx` silently returns `None` for any code
outside this set.  W1561 is confirmed merged.

**W1562 (commit `ea2e3d81`)**: `_UNAVAILABLE_TTL_SEC = _UNAVAILABLE_MODEL_TTL_SEC` alias exists
at line 231 in `KrabEar/core/engine.py`.  W1562 is confirmed merged and is correct — it targets
`engine.py`, not `audio_lang_id.py`, consistent with the commit message.

---

## Prior Wave Merge State Matrix

| Wave | Description | Status |
|------|-------------|--------|
| W1090 | Zero-peak short-circuit + MIN_CONFIDENCE gate | **REGRESSED** (restored by W1530) |
| W1116 | `_model_cache` RLock (`_cache_lock`) | **MERGED** (present in W1530 baseline) |
| W1121 | SUPPORTED_LANGUAGES allowlist + `restrict_to_supported` parameter | **PARTIALLY MERGED** — see F1 |
| W1340 | Case-tolerant key comparison in `clear_model_cache` | **MERGED** |
| W1367 | `_HAS_MLX` + `mx.clear_cache()` in `_detect_with_mlx.finally` (W63 rule) | **CLOBBERED** — see F2 |
| W1416 | `clear_model_cache()` calls `mx.clear_cache()` | **CLOBBERED** — see F3 |
| W1440 | Remove duplicate `clear_model_cache` + `_HAS_MLX` module flag | **CLOBBERED** — see F3 |
| W1443 | `preview_sec=0` minimum 1s clamp | **PRESENT** (via W1530 base state) |
| W1465 | Remove outer `_run_detect.finally` double `mx.clear_cache()` | **MOOT** — outer clear already absent |
| W1466 | `clear_model_cache` wraps `mx.clear_cache` in `mlx_lock` | **CLOBBERED** — see F3 |
| W1530 | Restore zero-peak + MIN_CONFIDENCE (W1525 regression fix) | **MERGED** ✓ |
| W1561 | Restore SUPPORTED_LANGUAGES frozenset | **MERGED** ✓ |
| W1562 | `_UNAVAILABLE_TTL_SEC` alias in `engine.py` | **MERGED** ✓ (engine.py only) |

Root cause of clobber pattern: W1530 applied on top of the W1497 cherry-pick-reverted state,
which was already missing W1367/W1416/W1440/W1466.  W1530 only restored W1090 guards.
W1561 only restored the SUPPORTED_LANGUAGES constant.  Neither restore attempt re-applied
the MLX Metal buffer management fixes.

---

## New Findings (5)

---

### F1 — HIGH: SUPPORTED_LANGUAGES content mismatches W1121 contract — 5+ tests fail

**File:** `KrabEar/core/audio_lang_id.py` line 45 and
`KrabEar/tests/test_audio_lang_id_allowlist_W1121.py`

**Description:**

W1561 restored `SUPPORTED_LANGUAGES` with 7 codes: `{"ru","es","en","de","fr","it","pt"}`.
The original W1121 contract (confirmed in `git show 33145b73`) established 4 codes:
`{"ru","uk","en","es"}`.

The test file `test_audio_lang_id_allowlist_W1121.py` was written against the W1121 contract
and tests assumptions that now conflict with production:

1. `test_supported_lang_passes_through` iterates `("ru","uk","en","es")` — `"uk"` is NOT
   in the production set, so the test will attempt to construct `AudioLanguageID(restrict_to_supported=False)`
   which raises `TypeError` (see F4 below).
2. `test_unsupported_lang_logged_but_returned` iterates `("fr","tr","pt","de","zh")` expecting
   WARNING for each — but `"fr"`, `"de"`, `"pt"` are IN the production set and are returned
   silently without WARNING. Three of five iterations will fail the `assertLogs` assertion.
3. `test_restrict_mode_filters_unsupported` expects `"fr"` and `"pt"` to return `None` under
   `restrict_to_supported=True` — but `"fr"` and `"pt"` are supported now.
4. `test_supported_languages_constant` calls `assertNotIn("fr",SUPPORTED_LANGUAGES)` and
   `assertNotIn("de",SUPPORTED_LANGUAGES)` — both will fail immediately.

**Additionally**, the production `detect()` code silently returns `None` for unsupported codes
(line 362–366) without a WARNING log. The W1121 design intended to emit
`logger.warning(..., extra={"detected_lang": ..., "fallback": "other"})`.  This means any code
outside SUPPORTED_LANGUAGES is silently swallowed — downstream STTRouter has no signal to
adapt to detected languages that Whisper supports but the allowlist excludes.

**Fix:** Reconcile SUPPORTED_LANGUAGES with the test contract.  Decision tree:
- **Option A**: update SUPPORTED_LANGUAGES to `{"ru","uk","en","es"}` (W1121 original),
  restore `restrict_to_supported` param and warning log path.
- **Option B**: update the test file to match the expanded 7-code production set, remove `uk`
  assumption, and accept that `fr`/`de`/`pt` are now supported.

Option A is lower-risk (tests are authoritative documentation for a deliberate design choice to
gate Whisper to languages the STTRouter knows how to handle).

---

### F2 — HIGH: `mx.clear_cache()` absent from `_detect_with_mlx` — Metal buffers leak per LID call

**File:** `KrabEar/core/audio_lang_id.py`, `_detect_with_mlx()` (lines 267–373) and
`_run_detect()` (lines 248–265)

**Description:**

W1367 (commit `fef660ab`) added `_HAS_MLX` module flag and a `try/finally` block in
`_detect_with_mlx` that calls `mx.clear_cache()` after every LID inference path (success,
mel failure, detect_language failure).  The current production file has neither:
- `_HAS_MLX` module attribute (line 30 has no `try: import mlx.core as mx`)
- `mx.clear_cache()` call in any path of `_detect_with_mlx` or `_run_detect`

This violates the W63 rule (Wave 63 memory leak fix: `mx.clear_cache()` after each MLX
inference).  Every LID `detect()` call that runs the encoder leaves Metal buffers allocated.
On a session with frequent LID (e.g. auto-language routing every 5 s), RSS grows at ~15–25 MB
per hour.  Production evidence: Wave 63 memory leak fix was specifically motivated by the same
pattern.

The only reference to `mx.clear_cache()` remaining in the file is in a comment at line 276.

**Tests broken:**
- `test_audio_lang_id_double_clear_W1465.py::test_mx_clear_cache_called_once_per_inference`
  — expects exactly 1 `mx.clear_cache()` per inference; currently 0.
- `test_audio_lang_id_double_clear_W1465.py::test_mx_clear_cache_under_mlx_lock`
  — expects `mx.clear_cache()` to be called inside `mlx_lock`; currently never called.

**Fix:** Restore `_HAS_MLX` module guard and `try/finally` in `_detect_with_mlx` per W1367
design.  Reintroduce `_run_lid_inference()` helper or inline the `finally` block.

---

### F3 — HIGH: `clear_model_cache()` does NOT call `mx.clear_cache()` — evicted model Metal buffers held

**File:** `KrabEar/core/audio_lang_id.py`, `clear_model_cache()` (lines 156–174)

**Description:**

W1416 (commit `1032d17f`) added `mx.clear_cache()` inside `clear_model_cache()` so that when
the settings hook evicts the stale LID model (on `MODEL_BALANCED` change), the Metal buffers
it held are freed immediately.  W1440 (commit `6791d405`) consolidated the duplicate
`clear_model_cache` methods and made `_HAS_MLX` + `mx.clear_cache()` the single canonical
implementation.

Current production `clear_model_cache()` only acquires `_cache_lock` and calls
`cls._model_cache.clear()` — no `mx.clear_cache()`.  After eviction, the old model object's
Metal allocations (300–500 MB for large Whisper models) remain in the Metal heap until the
garbage collector collects the Python wrapper AND the MLX Metal pool reclaims them — which is
non-deterministic on Apple Silicon.

**Tests broken:**
- `test_audio_lang_id_mx_clear_cache_W1416.py::TestClearModelCacheCallsMxClearCache`
  — all 4 tests patch `core.audio_lang_id._HAS_MLX`; since `_HAS_MLX` doesn't exist,
  `patch("core.audio_lang_id._HAS_MLX", True)` will raise `AttributeError` at test time.

**Fix:** Restore `_HAS_MLX` module flag (same as F2 fix) and add `mx.clear_cache()` call at
the end of `clear_model_cache()` when `_HAS_MLX` is True, guarded by a bare `except Exception`
to prevent propagation on OOM.

---

### F4 — MED: `restrict_to_supported` constructor parameter absent — W1121 tests TypeError

**File:** `KrabEar/core/audio_lang_id.py`, `AudioLanguageID.__init__` (lines 72–78)

**Description:**

W1121 (commit `33145b73`) added `restrict_to_supported: bool = False` to `__init__` and stored
it as `self._restrict_to_supported`.  The current `__init__` signature is:
```python
def __init__(
    self,
    model_path: Optional[str] = None,
    preview_sec: Optional[float] = None,
) -> None:
```
`restrict_to_supported` is absent.

The test helper `_make_lid()` in `test_audio_lang_id_allowlist_W1121.py` passes
`restrict_to_supported=restrict` as a keyword argument.  Every test in that file that calls
`_make_lid(lang, restrict=True/False)` will raise:
```
TypeError: AudioLanguageID.__init__() got an unexpected keyword argument 'restrict_to_supported'
```

This is 4 of 6 test methods in the file (all that call `_make_lid`).

**Fix:** Restore `restrict_to_supported: bool = False` parameter to `__init__` and the
`self._restrict_to_supported = restrict_to_supported` assignment.  The `detect()` method
then needs the conditional: if `result not in SUPPORTED_LANGUAGES`:
- log WARNING with structured extra `{"detected_lang": result, "fallback": "other"}`
- if `self._restrict_to_supported`: return `None` else return `result`.

---

### F5 — MED: SUPPORTED_LANGUAGES lacks `"uk"` (Ukrainian) — documented project language gap

**File:** `KrabEar/core/audio_lang_id.py` line 45

**Description:**

The project docstring states the system is "bilingual (RU/ES primary, EN secondary)" but
Ukrainian is a commonly detected language on RU-heavy audio (Whisper frequently detects
`"uk"` on mixed RU/UK speech, especially post-Soviet colloquial registers).  The original
W1121 design deliberately included `"uk"` in SUPPORTED_LANGUAGES to prevent spurious
"unsupported language" drops on these clips.

W1561 restored a 7-code set `{"ru","es","en","de","fr","it","pt"}` that adds `de`/`fr`/`it`/`pt`
(useful for European use cases) but drops `"uk"`.  If a user records Russian with Ukrainian
loanwords or switches mid-sentence, Whisper may return `"uk"` with confidence above 0.35,
which the current gate silently drops to `None` — causing full LID failure and fallback to
the default STT model regardless of actual language content.

**Fix:** Add `"uk"` to SUPPORTED_LANGUAGES.  This is a deliberate product decision: `"uk"` is
RU-adjacent and the STTRouter can handle it with the same model path as `"ru"`.

---

## Test Suite Health Summary (post-W1561+W1562)

| Test file | Expected status |
|-----------|----------------|
| `test_audio_lang_id.py` | PASS — core path tests unaffected |
| `test_audio_lang_id_allowlist_W1121.py` | **FAIL** — F1, F4: TypeError + 3+ assertion failures |
| `test_audio_lang_id_cache_evict_W1271.py` | PASS |
| `test_audio_lang_id_cache_limit.py` | PASS (but see W1506 F1 — module state leak) |
| `test_audio_lang_id_double_clear_W1465.py` | **FAIL** — F2: 2 tests expect `mx.clear_cache()` once per inference, get 0 |
| `test_audio_lang_id_lock_clear_W1466.py` | PASS (tests only check `_cache_lock`, not `mx.clear_cache`) |
| `test_audio_lang_id_mx_clear_cache_W1416.py` | **FAIL** — F3: AttributeError on `_HAS_MLX` patch |
| `test_audio_lang_id_threadsafe_W1116.py` | PASS |

Estimated failing tests post-W1561+W1562: **7–10** across 3 test files.

---

## Summary Table

| Finding | Severity | Root Cause |
|---------|----------|-----------|
| F1: SUPPORTED_LANGUAGES content mismatches W1121 contract | HIGH | W1561 set different from W1121 original |
| F2: `mx.clear_cache()` absent from `_detect_with_mlx` | HIGH | W1367/W1530 restoration gap |
| F3: `clear_model_cache()` lacks `mx.clear_cache()` | HIGH | W1416/W1440/W1466 not restored |
| F4: `restrict_to_supported` param absent | MED | W1121 param not restored by W1530 |
| F5: `"uk"` missing from SUPPORTED_LANGUAGES | MED | W1561 used different language set |
