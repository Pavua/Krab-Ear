# W1506 Ninth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1506 sub-agent (ninth-pass)
**Branch audited:** `audit-audio-lang-id-ninth-W1506` (off `codex/krab-ear-v2`, HEAD `ecab3fff`)
**File:** `KrabEar/core/audio_lang_id.py`
**Focus:** Post-W1465 (double clear_cache removed from `_run_detect.finally`) and W1466
(`clear_model_cache` wraps `mx.clear_cache` in `mlx_lock`) verification; new residual issues;
test suite health after 8 prior audit cycles.

---

## Prior Wave Merge State Matrix

| Wave | PR | Description | Status |
|------|----|-------------|--------|
| W1090 | — | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** (never merged) |
| W1116 | — | `_model_cache` RLock (thread-safety) | **OPEN** (never merged) |
| W1117 | #1035 | `mx.clear_cache()` in `_run_detect.finally` (W63 compliance) | **MERGED** |
| W1121 | — | `SUPPORTED_LANGUAGES` allowlist + structured warning | **OPEN** (never merged) |
| W1340 | #1253 | Case-tolerant key comparison + `clear_model_cache()` + `_cache_lock` | **MERGED** |
| W1341 | #1248 | `_fire_after_save_hooks` always calls `reload_settings_from_json()` | **MERGED** |
| W1367 | #1277 | `mx.clear_cache()` in `_detect_with_mlx.finally` (inside `mlx_lock`) | **MERGED** |
| W1416 | #1313 | `clear_model_cache()` calls `mx.clear_cache()` (W1405 F2 MED) | **MERGED** |
| W1440 | #1332 | Remove duplicate `clear_model_cache` + `_HAS_MLX` (W1438 F1+F2 HIGH) | **MERGED** |
| W1443 | #1339 | `preview_sec=0` minimum 1s clamp (W1438 F4 MED) | **MERGED** |
| W1462 | — | Eighth-pass audit doc (5 findings: F1 double clear_cache, F2 mlx_lock gap) | **MERGED** (doc only) |
| W1465 | #1354 | Remove outer `_run_detect.finally` `mx.clear_cache()` (W1462 F1 HIGH) | **MERGED** |
| W1466 | #1352 | `clear_model_cache` wraps `mx.clear_cache` in `mlx_lock` (W1462 F2 MED) | **MERGED** |

Verification:
```bash
git log --oneline | grep -E "wave14(65|66|40|43)"
# 45284340 fix(wave1465): remove double mx.clear_cache ...
# e0730585 fix(wave1466): clear_model_cache wraps mx.clear_cache in mlx_lock ...
# f7086279 fix(wave1443): audio_lang_id preview_sec=0 minimum 1s clamp ...
# 6791d405 fix(wave1440): remove duplicate clear_model_cache + _HAS_MLX ...
```

---

## W1465 + W1466 Verification

**W1465 (PR #1354)** removed the outer `_run_detect.finally` `mx.clear_cache()` call that was
added by W1117. Verified by AST analysis: `_run_detect` has no `finally` block containing
`clear_cache`. The correct inner call (in `_detect_with_mlx.finally`, inside `mlx_lock`) remains.

**W1466 (PR #1352)** added `with mlx_lock(): mx.clear_cache()` inside `clear_model_cache()`.
Verified: `clear_model_cache()` now acquires `_cache_lock`, clears the dict, releases it, then
acquires `mlx_lock()`, calls `mx.clear_cache()`, releases it. The two locks are used
SEQUENTIALLY, never nested in `clear_model_cache()`.

**Test verification:**
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_audio_lang_id_double_clear_W1465.py \
  KrabEar/tests/test_audio_lang_id_lock_clear_W1466.py -v
# 7/7 pass when run in isolation
# 5/7 pass when run after test_audio_lang_id_cache_limit.py (see N1 below)
```

---

## New Findings (4)

The following findings are distinct from all prior waves in the post-W1465+W1466 baseline.

---

### F1 — HIGH: `test_audio_lang_id_cache_limit.py` leaks module state — 2 W1466 tests fail in full suite

**File:** `KrabEar/tests/test_audio_lang_id_cache_limit.py` (line 36) and
`KrabEar/tests/test_audio_lang_id_lock_clear_W1466.py` (class `TestClearModelCacheAcquiresMlxLock`)

**Description:**

`test_audio_lang_id_cache_limit.py::setUp()` deletes `sys.modules["core.audio_lang_id"]` and
re-imports the module via `importlib.import_module("core.audio_lang_id")`. This creates a **new
module object** for `core.audio_lang_id` in `sys.modules`. However, `tearDown()` does **not**
restore the original module object.

`test_audio_lang_id_lock_clear_W1466.py` imports `AudioLanguageID` at module level:
```python
from core.audio_lang_id import AudioLanguageID  # at file top
```
This captures the **original** module object. When the W1466 test then patches:
```python
with patch("core.audio_lang_id._HAS_MLX", True), \
     patch("core.audio_lang_id.mx", fake_mx), \
     patch("core.audio_lang_id.mlx_lock", return_value=...):
```
`patch("core.audio_lang_id._HAS_MLX", ...)` targets `sys.modules["core.audio_lang_id"]._HAS_MLX`
— the **replacement** module object from cache_limit's setUp. But `AudioLanguageID.clear_model_cache()`
executes in the **original** module's namespace and reads the **original** `_HAS_MLX` variable.
The patch has no effect; `_HAS_MLX` is `True` (MLX is installed), and `mlx_lock` is not the
fake — so `fake_mx.clear_cache` is never called.

**Observed failure (full suite run):**
```
FAILED TestClearModelCacheAcquiresMlxLock::test_clear_model_cache_acquires_mlx_lock
  AssertionError: Expected 'clear_cache' to have been called once. Called 0 times.
FAILED TestClearModelCacheAcquiresMlxLock::test_clear_model_cache_lock_called_before_mx_clear
  AssertionError: 'lock_enter' not found in []
```

The tests pass when `test_audio_lang_id_lock_clear_W1466.py` is run in isolation.

**Repro:**
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_audio_lang_id_cache_limit.py \
  KrabEar/tests/test_audio_lang_id_lock_clear_W1466.py -v
# 2 FAIL due to module object mismatch
```

**Fix:** Add `tearDown` to `TestAudioLanguageIDCacheLimit` that restores `sys.modules`:
```python
def tearDown(self):
    # Restore module so subsequent test files that imported AudioLanguageID
    # at module-level continue to patch the correct module object.
    sys.modules["core.audio_lang_id"] = self.mod
    self.mod.AudioLanguageID._model_cache.clear()
```
Alternatively, the W1466 test class should use a `_fresh_aid_module()` helper (already used
in `test_audio_lang_id_double_clear_W1465.py`) to always patch through the live module object.

---

### F2 — LOW (carry-forward F3/W1462): `_cache_lock` held during `load_model()` — 9 audits, never fixed

**File:** `KrabEar/core/audio_lang_id.py`, lines 284–299 (`_run_lid_inference`)

**Description:**

This is a carry-forward from W1462 F3, W1405 F4, W1334 F3, and five earlier passes. The `with
AudioLanguageID._cache_lock:` block in `_run_lid_inference()` covers the entire call to
`mlx_whisper.load_models.load_model(model_path)`, which takes 1–3 s on cold start.

During this window any concurrent `clear_model_cache()` call (triggered by the settings hook
when `MODEL_BALANCED` changes) is blocked for up to 3 s, stalling the IPC dispatch thread and
delaying the settings save response to the Swift agent.

Eight prior audits documented this. Confidence: fix is safe (double-checked locking pattern).

**Fix:**
```python
# Load model outside _cache_lock, then re-check before storing.
with AudioLanguageID._cache_lock:
    model = AudioLanguageID._model_cache.get(model_path)

if model is None:
    try:
        model = mlx_whisper.load_models.load_model(model_path)
    except Exception as exc:
        logger.warning("AudioLanguageID: не удалось загрузить модель %s: %s", model_path, exc)
        return None
    with AudioLanguageID._cache_lock:
        if model_path not in AudioLanguageID._model_cache:
            if len(AudioLanguageID._model_cache) >= 1:
                AudioLanguageID._model_cache.clear()
            AudioLanguageID._model_cache[model_path] = model
```

---

### F3 — LOW (carry-forward F4/W1462): `_MIN_PREVIEW_SEC` is method-local — duplicated with literal in `detect()`

**File:** `KrabEar/core/audio_lang_id.py`, lines 169 and 122

**Description:**

W1443 added `_MIN_PREVIEW_SEC = 1.0` as a local variable inside `_get_preview_sec()` (line 169),
correctly clamping any zero/negative `preview_sec` values. However the same 1.0-second minimum
appears as a magic literal in `detect()` at line 122:
```python
min_frames = int(sample_rate * 1.0)  # хотя бы 1 секунда
```

Two problems:
1. If `_MIN_PREVIEW_SEC` is changed to a different value in `_get_preview_sec`, the hard-coded
   `1.0` in `detect()` would be out of sync — a latent inconsistency.
2. The constant cannot be mocked by test code patching a class attribute.

This was W1462 F4. Still unfixed.

**Fix:** Promote to a class-level constant:
```python
class AudioLanguageID:
    _MIN_PREVIEW_SEC: float = 1.0
    ...
    def detect(self, audio, ...):
        min_frames = int(sample_rate * self._MIN_PREVIEW_SEC)
    def _get_preview_sec(self):
        return max(self._MIN_PREVIEW_SEC, raw)
```

---

### F4 — LOW (NEW): `_detect_with_mlx.finally` calls `mx.clear_cache()` without `try/except` — can mask inference exceptions

**File:** `KrabEar/core/audio_lang_id.py`, lines 263–269 (`_detect_with_mlx`)

**Description:**

The `finally` block in `_detect_with_mlx()` calls `mx.clear_cache()` without exception
protection:
```python
finally:
    if _HAS_MLX:
        mx.clear_cache()   # ← no try/except
```

By contrast, `clear_model_cache()` (added by W1466) wraps the same call defensively:
```python
try:
    with mlx_lock():
        mx.clear_cache()
except Exception:
    pass  # MLX не установлен или старая версия без clear_cache
```

If `mx.clear_cache()` raises (e.g. due to a Metal device reset or MLX version incompatibility),
Python's exception handling replaces any active exception with the `clear_cache()` exception:

- **Success path:** `_run_lid_inference()` returns the detected language code. The `finally`
  raises from `clear_cache()`. `_run_detect` catches it and logs "inference failed". Returns
  `None` instead of the language code — a silent correctness failure (language detection silently
  falls through to fallback on every call while the Metal device is unhealthy).

- **Failure path:** `_run_lid_inference()` raises (e.g. OOM). The `finally` raises from
  `clear_cache()` too. The original OOM exception is replaced by the `clear_cache()` exception,
  making the real root cause invisible in logs.

This inconsistency was not present in prior waves (W63/W1367 only focused on adding the call,
not on exception safety).

**Fix:**
```python
finally:
    if _HAS_MLX:
        try:
            mx.clear_cache()
        except Exception:
            pass  # Metal device reset or old MLX version — not critical
```

---

## Prior Open Findings Still Unresolved

| Finding | Source Wave | Severity | Status |
|---------|-------------|----------|--------|
| Zero-peak short-circuit + confidence gate | W1090 | MED | Open (never merged, ~9 waves) |
| `_model_cache` as plain `dict` (should be `RLock`-protected dict) | W1116 | LOW | Open (never merged) |
| `SUPPORTED_LANGUAGES` allowlist + structured log | W1121 | LOW | Open (never merged) |
| `_cache_lock` held during `load_model()` (F3/W1462) | W1462 F3 | LOW | Open (9 audits) |
| `_MIN_PREVIEW_SEC` method-local (F4/W1462) | W1462 F4 | LOW | Open (2 audits) |

---

## Test Suite Status

| File | Tests | Status |
|------|-------|--------|
| `test_audio_lang_id.py` | 45 | PASS |
| `test_audio_lang_id_cache_limit.py` | 4 | PASS (isolated) |
| `test_audio_lang_id_double_clear_W1465.py` | 4 | PASS |
| `test_audio_lang_id_lock_clear_W1466.py` | 3 | PASS (isolated) / **2 FAIL (after cache_limit)** |
| `test_audio_lang_id_mx_clear_cache_W1416.py` | 4 | PASS |
| `test_lang_id_hook_case_W1340.py` | 15 | PASS |
| **Total** | **75** | **73 PASS / 2 FAIL (order-dependent)** |

The 2 failures in `TestClearModelCacheAcquiresMlxLock` are order-dependent (see F1 above).

---

## Summary

W1465 and W1466 are correctly merged and verified. The post-fix baseline has:
- No double `mx.clear_cache()` per inference
- `clear_model_cache()` properly wraps `mx.clear_cache()` in `mlx_lock()`

**4 new/carry-forward findings:**
| # | Severity | New? | Description |
|---|----------|------|-------------|
| F1 | HIGH | NEW | `test_audio_lang_id_cache_limit.py` module leak causes 2 W1466 tests to fail in full suite |
| F2 | LOW | carry-forward | `_cache_lock` held during `load_model()` — 9 audits |
| F3 | LOW | carry-forward | `_MIN_PREVIEW_SEC` method-local — 2 audits |
| F4 | LOW | NEW | `_detect_with_mlx.finally` missing `try/except` around `mx.clear_cache()` |
