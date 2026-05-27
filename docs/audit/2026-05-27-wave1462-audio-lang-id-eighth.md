# W1462 Eighth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1462 sub-agent (eighth-pass)
**Branch audited:** `origin/codex/krab-ear-v2` (HEAD `f7086279`, post-W1440+W1443 merged)
**File:** `KrabEar/core/audio_lang_id.py`
**Focus:** Post-W1440 (duplicate clear_model_cache + _HAS_MLX removed) and W1443 (preview_sec=0
clamp) verification; new interaction bugs; double mx.clear_cache() path analysis; lock ordering.

---

## Prior Wave Merge State Matrix

| Wave | PR | Description | Status |
|------|----|-------------|--------|
| W1090 | — | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** (never merged) |
| W1109 | #1019 | Second-pass audit doc | **OPEN** (doc only) |
| W1116 | — | `_model_cache` RLock (thread-safety) | **OPEN** (never merged) |
| W1117 | #1035 | `mx.clear_cache()` in `_run_detect.finally` (W63 compliance) | **MERGED** |
| W1121 | — | `SUPPORTED_LANGUAGES` allowlist + structured warning | **OPEN** (never merged) |
| W1265 | — | Third-pass audit doc | **OPEN** (doc only) |
| W1271 | — | `clear_model_cache()` + `_after_save_hook` | **OPEN** (superseded by W1340) |
| W1300 | #1205 | Fourth-pass audit doc | **OPEN** (doc only) |
| W1308 | #1210 | SettingsService fires `_after_save_hooks` on all 5 save paths | **MERGED** |
| W1334 | #1243 | Fifth-pass audit doc | **OPEN** (doc only) |
| W1340 | #1253 | Case-tolerant key comparison + `clear_model_cache()` + `_cache_lock` | **MERGED** |
| W1341 | #1248 | `_fire_after_save_hooks` always calls `reload_settings_from_json()` | **MERGED** (verified in settings_service.py lines 392, 421, 488, 586, 717) |
| W1367 | #1277 | `mx.clear_cache()` in `_detect_with_mlx.finally` (inside `mlx_lock`) | **MERGED** |
| W1405 | #1311 | Sixth-pass audit doc | **MERGED** (doc only) |
| W1416 | #1313 | `clear_model_cache()` calls `mx.clear_cache()` (W1405 F2 MED) | **MERGED** |
| W1438 | — | Seventh-pass audit doc (5 findings: F1+F2 HIGH duplicate defs, F3-F5) | **MERGED** (doc only) |
| W1440 | #1332 | Remove duplicate `clear_model_cache` + `_HAS_MLX` (W1438 F1+F2 HIGH) | **MERGED** |
| W1443 | #1339 | `preview_sec=0` minimum 1s clamp (W1438 F4 MED) | **MERGED** |

Verification commands:
```bash
git merge-base --is-ancestor 6791d405 origin/codex/krab-ear-v2  # W1440 MERGED
git merge-base --is-ancestor f7086279 origin/codex/krab-ear-v2  # W1443 MERGED (=HEAD)
git merge-base --is-ancestor 1032d17f origin/codex/krab-ear-v2  # W1416 MERGED
git merge-base --is-ancestor fef660ab origin/codex/krab-ear-v2  # W1367 MERGED
git merge-base --is-ancestor 17f7371d origin/codex/krab-ear-v2  # W1340 MERGED
git merge-base --is-ancestor 65402fb6 origin/codex/krab-ear-v2  # W1308 MERGED
git merge-base --is-ancestor 055f84bd origin/codex/krab-ear-v2  # W1117 MERGED
```

---

## W1440 + W1443 Verification

**W1440 (commit `6791d405`)** merged the two `clear_model_cache()` definitions into one and
removed the second `_HAS_MLX` initialization block. Verified by AST analysis: `clear_model_cache`
appears exactly once in the class body. The surviving definition combines:
- W1340's `_cache_lock` protection (line 67)
- W1416's `mx.clear_cache()` call (lines 70-75)

**W1443 (commit `f7086279`)** added `_MIN_PREVIEW_SEC = 1.0` local constant and
`return max(_MIN_PREVIEW_SEC, raw)` clamp in `_get_preview_sec()`. The constructor path and the
settings path both clamp. Verified at lines 169-178.

---

## New Findings (5)

The following findings are **distinct from all prior waves** in the post-W1440+W1443 baseline.

---

### F1 — HIGH: Double `mx.clear_cache()` per inference — W1117 outer call not removed after W1367

**File:** `KrabEar/core/audio_lang_id.py`, lines 251-258 (`_run_detect.finally`) and
lines 271-275 (`_detect_with_mlx.finally`)

**Description:**

W1117 (PR #1035) added `mx.clear_cache()` in the `finally` block of `_run_detect()` (lines
251-258). This call is **outside** `mlx_lock()` — it executes after `with mlx_lock():` exits.

W1367 (PR #1277) later added another `mx.clear_cache()` in the `finally` block of
`_detect_with_mlx()` (lines 271-275). This call is **inside** `mlx_lock()` — correct placement.

W1367's commit did NOT remove the W1117 outer `finally` block. The result is that every successful
inference now calls `mx.clear_cache()` TWICE:

```
_run_detect():
  try:
    with mlx_lock():              ← acquires mlx_lock
      _detect_with_mlx():
        try:
          _run_lid_inference()    ← actual inference
        finally:
          mx.clear_cache()        ← call #1 (inside mlx_lock) ← CORRECT
  finally:
    _mx.clear_cache()             ← call #2 (outside mlx_lock) ← WRONG
```

**The outer call (`_run_detect.finally`, line 256) violates the MLX thread-safety policy:**
`mx.clear_cache()` executes outside `mlx_lock()`, where it can race with concurrent `mlx_whisper.transcribe()` calls in `engine.py` (which hold `mlx_lock()` for the duration of STT inference). Per CLAUDE.md: "ALL MLX inference must be serialized through `core.mlx_lock.mlx_lock()`."

The `mlx.core` docs do not mark `clear_cache()` as thread-safe for concurrent calls with other
MLX operations. On M4 Max the race may be benign in practice (Metal runtime is internally
serialized per-device), but it is an unguarded violation of the project's MLX thread-safety
contract.

Additionally, the double call is wasteful: the first `clear_cache()` (inside `mlx_lock`)
fully releases intermediary Metal allocations from the LID pass. The second call is a no-op
(nothing new to free) but adds unnecessary overhead on every inference.

**Root cause:** W1367 was applied without checking whether W1117's `_run_detect.finally`
was now redundant and unsafe.

**Fix:** Remove the `_run_detect.finally` block (lines 251-258) entirely. The `_detect_with_mlx.finally`
(inside `mlx_lock()`) is the correct and sufficient location:

```python
def _run_detect(self, audio_16k: np.ndarray) -> Optional[str]:
    """..."""
    try:
        import mlx_whisper  # type: ignore[import]
    except ImportError:
        logger.debug("AudioLanguageID: mlx_whisper не установлен → skip")
        return None

    result = None
    try:
        with mlx_lock():
            result = self._detect_with_mlx(mlx_whisper, audio_16k)
    except Exception as exc:
        logger.warning("AudioLanguageID: inference failed: %s", exc)
    # No outer finally — mx.clear_cache() is already in _detect_with_mlx.finally (inside mlx_lock)
    return result
```

---

### F2 — MED: `clear_model_cache()` calls `mx.clear_cache()` outside `mlx_lock()` — MLX policy violation

**File:** `KrabEar/core/audio_lang_id.py`, lines 67-75 (`clear_model_cache`)

**Description:**

`clear_model_cache()` calls `mx.clear_cache()` at lines 70-75. This call is outside `mlx_lock()`.
The settings hook fires `clear_model_cache()` from the IPC dispatch thread (specifically from
`_on_settings_saved_lang_id` in `service.py`) whenever `MODEL_BALANCED` changes in settings.

If a recording is in progress at the moment of the settings change, `engine.py` holds `mlx_lock()`
while running `mlx_whisper.transcribe()`. The IPC thread's `clear_model_cache()` → `mx.clear_cache()`
then races with the ongoing STT inference inside `mlx_lock()`.

Current code:
```python
@classmethod
def clear_model_cache(cls) -> None:
    with cls._cache_lock:
        cls._model_cache.clear()          # ← dict cleared under _cache_lock (correct)
    logger.debug("...")
    if _HAS_MLX:
        try:
            import mlx.core as mx
            mx.clear_cache()              # ← OUTSIDE mlx_lock() ← WRONG
        except Exception:
            pass
```

Note: W1417 (the prior wave's recommended fix) suggested calling `mx.clear_cache()` here.
The fix was correct in intent but did not wrap the call under `mlx_lock()`.

**Severity:** MED — race requires concurrent recording + settings change. In practice:
1. User is recording (engine holds `mlx_lock()`)
2. Simultaneously, another IPC call changes `MODEL_BALANCED`
3. Hook fires → `clear_model_cache()` → `mx.clear_cache()` outside lock

**Fix:** Acquire `mlx_lock()` for the `mx.clear_cache()` call:
```python
@classmethod
def clear_model_cache(cls) -> None:
    with cls._cache_lock:
        cls._model_cache.clear()
    logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
    if _HAS_MLX:
        try:
            with mlx_lock():          # ← add mlx_lock() wrapper
                mx.clear_cache()
        except Exception as exc:
            logger.debug("clear_model_cache: mx.clear_cache failed: %s", exc)
```

---

### F3 — LOW: `_cache_lock` (non-reentrant) held during 1–3 s cold `load_model()` — carry-forward

**File:** `KrabEar/core/audio_lang_id.py`, lines 290-307 (`_run_lid_inference`)

**Description:**

This is a carry-forward of W1405 F4 / W1334 F3. `_run_lid_inference()` holds `_cache_lock`
for the entire duration of `mlx_whisper.load_models.load_model()`, which takes 1–3 s on cold
start (model weights loaded from disk into Metal GPU memory).

During this window, any concurrent `clear_model_cache()` call from the settings hook blocks
for up to 3 s. Since `clear_model_cache()` is called from the IPC dispatch thread on settings
save, this can add a 1–3 s hang to the settings IPC response on cold start.

Eight audits have documented this; it is actionable but not critical (cold start is rare,
subsequent calls hit cache with negligible lock time).

**Fix (double-checked locking):** Load model outside the lock, re-check before store:
```python
with AudioLanguageID._cache_lock:
    model = AudioLanguageID._model_cache.get(model_path)
if model is None:
    try:
        model = mlx_whisper.load_models.load_model(model_path)  # outside lock
    except Exception as exc:
        logger.warning("AudioLanguageID: не удалось загрузить модель %s: %s", model_path, exc)
        return None
    with AudioLanguageID._cache_lock:
        if model_path not in AudioLanguageID._model_cache:
            if len(AudioLanguageID._model_cache) >= 1:
                AudioLanguageID._model_cache.clear()
            AudioLanguageID._model_cache[model_path] = model
        model = AudioLanguageID._model_cache[model_path]
```

---

### F4 — LOW: `_MIN_PREVIEW_SEC` is a method-local constant — untestable and duplicated

**File:** `KrabEar/core/audio_lang_id.py`, lines 169, 122

**Description:**

W1443 added `_MIN_PREVIEW_SEC = 1.0` as a local variable inside `_get_preview_sec()`. The
same 1.0-second minimum also appears as a magic literal in `detect()` at line 122:
`min_frames = int(sample_rate * 1.0)  # хотя бы 1 секунда`.

Two problems:
1. **Duplication:** The minimum preview duration is expressed in two places (`_MIN_PREVIEW_SEC`
   in `_get_preview_sec` and the `1.0` literal in `detect`). If one is updated, the other
   may be forgotten.
2. **Untestability:** A method-local constant cannot be patched in tests via
   `patch("core.audio_lang_id.AudioLanguageID._MIN_PREVIEW_SEC", 0.5)`. If a test wants to
   verify behaviour at the exact minimum boundary with a custom threshold, it must supply a
   `preview_sec` constructor argument — but that only bypasses `_get_preview_sec()` entirely
   (line 170: `if self._preview_sec is not None: raw = float(self._preview_sec); return max(...)`)
   so the clamp still applies. However the class-level minimum is not easily observable.

**Fix:** Promote to a class-level constant:
```python
class AudioLanguageID:
    _MIN_PREVIEW_SEC: float = 1.0  # minimum audio preview for LID inference
    ...
    def _get_preview_sec(self) -> float:
        ...
        return max(self._MIN_PREVIEW_SEC, raw)
```
And replace the magic `1.0` literal in `detect()` line 122:
```python
min_frames = int(sample_rate * self._MIN_PREVIEW_SEC)
```

---

### F5 — LOW: No test for double-clear-cache scenario (F1 regression), no test for `mlx_lock()` wrapping in `clear_model_cache()` (F2)

**Files:** `KrabEar/tests/test_clear_cache_called_after_lid_inference.py`,
`KrabEar/tests/test_audio_lang_id_mx_clear_cache_W1416.py`

**Description:**

1. **F1 double-clear:** `test_clear_cache_called_after_lid_inference.py` checks that
   `mx.clear_cache()` is called, but test case 5 ("once per inference") does not assert that
   `clear_cache()` is called **at most once** per successful inference. A test asserting
   `call_count == 1` would catch the double-clear regression introduced by the W1117/W1367
   interaction (F1 above). Currently both the inner (`_detect_with_mlx.finally`) and outer
   (`_run_detect.finally`) calls are patched through the same mock, so `call_count` would be 2
   for a successful inference. The test's `assert_called_once()` was written only verifying "at
   least once", not "exactly once per inference path".

2. **F2 mlx_lock gap:** No test verifies that `clear_model_cache()`'s `mx.clear_cache()` call
   is made while holding `mlx_lock()`. Tests in `test_audio_lang_id_mx_clear_cache_W1416.py`
   only verify that `clear_cache()` is called, not that it is called under `mlx_lock()`.

---

## Summary

| # | Severity | Description | New in W1462? |
|---|----------|-------------|---------------|
| F1 | HIGH | Double `mx.clear_cache()` per inference: W1117 outer call not removed after W1367 | **NEW** |
| F2 | MED | `clear_model_cache()` calls `mx.clear_cache()` outside `mlx_lock()` | **NEW** |
| F3 | LOW | `_cache_lock` held during 1–3 s `load_model()` cold call | Carry-forward (W1405 F4) |
| F4 | LOW | `_MIN_PREVIEW_SEC` is method-local constant — duplicated and untestable | **NEW** (W1443 introduced) |
| F5 | LOW | No test for double-clear or `mlx_lock()` wrapping in `clear_model_cache()` | **NEW** |

**Root cause of F1:** W1367 added the correct `_detect_with_mlx.finally` location for
`mx.clear_cache()` but did not remove the W1117 `_run_detect.finally` that is now both
redundant and outside `mlx_lock()`.

**Root cause of F2:** W1416/W1440 correctly added `mx.clear_cache()` to `clear_model_cache()`
but did not wrap it in `mlx_lock()` as required by the MLX thread-safety policy.

**Still open from prior waves:** W1090 (MIN_CONFIDENCE gate), W1116 (RLock), W1121 (SUPPORTED_LANGUAGES allowlist).

---

## Recommended Actions

1. **Immediate (F1):** Remove `_run_detect.finally` block (lines 251-258). The
   `_detect_with_mlx.finally` (inside `mlx_lock`) is correct and sufficient.

2. **Short-term (F2):** Wrap `mx.clear_cache()` in `clear_model_cache()` under `mlx_lock()`.

3. **Short-term (F4):** Promote `_MIN_PREVIEW_SEC = 1.0` to class-level constant; use it in
   `detect()` line 122 to eliminate the duplicated magic literal.

4. **Low-priority (F3):** Implement double-checked locking for `_run_lid_inference()`.

5. **Test (F5):** Add test asserting `clear_cache()` called exactly once per successful
   inference. Add test verifying `clear_model_cache()` holds `mlx_lock()` when calling
   `mx.clear_cache()`.
