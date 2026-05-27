# W1438 Seventh-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1438 sub-agent (seventh-pass)
**Branch audited:** `origin/codex/krab-ear-v2` (post-W1340+W1367+W1416 merged)
**File:** `KrabEar/core/audio_lang_id.py`
**Focus:** Post-W1416 merge artifact verification, race between clear_model_cache and inference,
mx.clear_cache idempotency, preview_sec=0 edge case, test coverage.

---

## Prior Wave Merge State Matrix

| Wave | PR | Description | Status (in `origin/codex/krab-ear-v2`) |
|------|----|-------------|----------------------------------------|
| W1090 | #1004 | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** |
| W1109 | #1019 | Second-pass audit doc | **OPEN** (doc only) |
| W1116 | #1031 | `_model_cache` RLock (thread-safety) | **OPEN** |
| W1117 | #1035 | `mx.clear_cache()` after inference (superseded by W1367) | **OPEN** (superseded) |
| W1121 | #1033 | `SUPPORTED_LANGUAGES` allowlist + structured warning | **OPEN** |
| W1265 | #1171 | Third-pass audit doc | **OPEN** (doc only) |
| W1271 | #1177 | `clear_model_cache()` + `_after_save_hook` (superseded by W1340) | **OPEN** (superseded) |
| W1300 | #1205 | Fourth-pass audit doc | **OPEN** (doc only) |
| W1308 | #1210 | SettingsService fires `_after_save_hooks` on all 5 save paths | **MERGED** |
| W1334 | #1243 | Fifth-pass audit doc | **OPEN** (doc only) |
| W1340 | #1253 | Case-tolerant `_get_model_balanced()` + `clear_model_cache()` + `_cache_lock` | **MERGED** |
| W1341 | #1248 | `_fire_after_save_hooks` always reloads settings.json (pydantic stays fresh) | **OPEN** |
| W1367 | #1277 | `mx.clear_cache()` in `finally` after LID inference (W63 compliance) | **MERGED** |
| W1405 | #1311 | Sixth-pass audit doc | **MERGED** (doc only) |
| W1416 | #1313 | `clear_model_cache()` calls `mx.clear_cache()` (W1405 F2 MED) | **MERGED** |

Verification commands used:
```bash
git merge-base --is-ancestor 17f7371d origin/codex/krab-ear-v2   # W1340 MERGED
git merge-base --is-ancestor fef660ab origin/codex/krab-ear-v2   # W1367 MERGED
git merge-base --is-ancestor 65402fb6 origin/codex/krab-ear-v2   # W1308 MERGED
git merge-base --is-ancestor 1032d17f origin/codex/krab-ear-v2   # W1416 MERGED
git merge-base --is-ancestor 7b1485cb origin/codex/krab-ear-v2   # W1341 NOT in remote
git merge-base --is-ancestor f4bb574c origin/codex/krab-ear-v2   # W1116 NOT in remote
git merge-base --is-ancestor 30d38515 origin/codex/krab-ear-v2   # W1121 NOT in remote
```

---

## W1416 Verification

W1416 (commit `1032d17f`, PR #1313) added `_HAS_MLX` module-level guard and a `clear_model_cache()`
classmethod that calls both `cls._model_cache.clear()` and `mx.clear_cache()` to immediately
release Metal GPU buffers on model eviction.

**Problem:** W1416 was merged on top of W1340. W1340 had already added its own `clear_model_cache()`
classmethod with `_cache_lock` protection. The two patches were applied sequentially without merging
their implementations, resulting in TWO `clear_model_cache()` definitions in the same class body.

In Python, when a class body contains two methods with the same name, the **second definition
silently overwrites the first**. The second `clear_model_cache()` (from W1340, lines 91-100) does
NOT call `mx.clear_cache()`. W1416's version (lines 63-76) — which does call `mx.clear_cache()` —
is silently dead code.

**Result:** W1405 F2 (the entire goal of W1416) is still NOT fixed at runtime despite being merged.

---

## New Findings (post-W1416)

### F1 HIGH — Duplicate `clear_model_cache()`: W1416 fix silently overridden by W1340 definition

**File:** `KrabEar/core/audio_lang_id.py`, lines 62-100

```python
@classmethod
def clear_model_cache(cls) -> None:  # LINE 62 — W1416 version (with mx.clear_cache)
    """Сбрасывает Python-ссылку на модель и освобождает Metal GPU буферы.
    W1405 F2 MED: drop Python reference + flush Metal cache через mx.clear_cache().
    """
    cls._model_cache.clear()
    if _HAS_MLX:
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass

def __init__(self, ...):
    ...

# ------------------------------------------------------------------
# Публичный API
# ------------------------------------------------------------------

@classmethod
def clear_model_cache(cls) -> None:  # LINE 91 — W1340 version (WITHOUT mx.clear_cache)
    """Вытесняет кешированную модель LID.
    Вызывается из _on_settings_saved_lang_id hook...
    Безопасно вызывать конкурентно с detect() — защищено _cache_lock.
    """
    with cls._cache_lock:
        cls._model_cache.clear()
    logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
```

**Root cause:** W1416 was applied as a clean insert before `__init__`, without removing or merging
W1340's existing `clear_model_cache()` that appears later in the class body (after `__init__` in
the `# Публичный API` section). Python's class body evaluation means line 91 definition wins.

**Impact:** Every call to `clear_model_cache()` (from the settings hook on `MODEL_BALANCED` change)
clears the Python dict but does NOT call `mx.clear_cache()`. Metal GPU buffers from the evicted
model (~300-500 MB per W1405 analysis) linger until the next inference `finally` block. On systems
where LID is infrequent, this can mean minutes of Metal buffer retention after a profile switch.

**Fix:** Merge the two implementations into a single method — W1340's `_cache_lock` + W1416's
`mx.clear_cache()`:
```python
@classmethod
def clear_model_cache(cls) -> None:
    with cls._cache_lock:
        cls._model_cache.clear()
    if _HAS_MLX:
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass
    logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
```

---

### F2 HIGH — Duplicate `_HAS_MLX` / `mx` initialization blocks: `mx = None` assignment orphaned

**File:** `KrabEar/core/audio_lang_id.py`, lines 26-42

```python
# Block 1 (W1367 / pre-existing)
try:
    import mlx.core as mx  # type: ignore[import]
    _HAS_MLX = True
except ImportError:
    _HAS_MLX = False
    mx = None  # type: ignore[assignment]  ← assigns mx = None fallback

# ... logger definition ...

# Block 2 (W1416)
# Флаг наличия MLX (проверяется один раз при загрузке модуля)
try:
    import mlx.core as _mlx_core  # noqa: F401
    _HAS_MLX = True
except ImportError:
    _HAS_MLX = False
    # ← NO mx = None here — orphans block 1's assignment if both fail
```

**Impact (scenario A — MLX installed):** Both blocks succeed; `_HAS_MLX = True` and `mx` is
the real `mlx.core` module. No problem.

**Impact (scenario B — MLX not installed):** Block 1 sets `_HAS_MLX = False` and `mx = None`.
Block 2 sets `_HAS_MLX = False` again (fine). However, `_detect_with_mlx` (line 279) uses
`if _HAS_MLX: mx.clear_cache()` — guarded correctly. The `mx = None` assignment in block 1 is
now redundant but harmless.

**Impact (scenario C — import race / partial install):** Both blocks import from `mlx.core`
separately. If MLX is installed, `mx` (block 1 import) is used in `_detect_with_mlx` while
`_mlx_core` (block 2) is unused. The duplication creates confusion and risks divergence: if
block 1's import succeeds but block 2's fails (theoretically impossible for the same module,
but triggers linter warnings), `mx` would be the real module while `_HAS_MLX = False`, causing
the Metal dealloc to be silently skipped while the `mx` reference exists.

**Fix:** Remove block 2 (W1416 addition, lines 37-42). Block 1 already defines both `mx` and
`_HAS_MLX` correctly. Update `clear_model_cache()` to use the module-level `mx` directly
(consistent with `_detect_with_mlx`).

---

### F3 MED — `_run_lid_inference()` holds `_cache_lock` during 1-3s `load_model()` cold call

**File:** `KrabEar/core/audio_lang_id.py`, lines 295-312

```python
with AudioLanguageID._cache_lock:
    if model_path not in AudioLanguageID._model_cache:
        ...
        try:
            model = mlx_whisper.load_models.load_model(model_path)  # ← 1-3s cold load
            AudioLanguageID._model_cache[model_path] = model
        except Exception as exc:
            ...
            return None
    model = AudioLanguageID._model_cache[model_path]
```

`_cache_lock` is held for the entire duration of `load_model()`, which takes 1-3 seconds on
cold start (model weights loaded from disk into GPU). During this window:
- Any concurrent `clear_model_cache()` call blocks for up to 3s (lock contention)
- Any concurrent `detect()` call blocks for up to 3s even if the model is already in cache

This is a carry-forward of W1405 F4 (W1334 F3). W1340 added `_cache_lock` to protect the dict
but did not implement the double-checked locking pattern to load outside the lock.

**Fix:** Double-checked locking — check cache under lock, load outside lock, re-check before store:
```python
with AudioLanguageID._cache_lock:
    model = AudioLanguageID._model_cache.get(model_path)
if model is None:
    try:
        model = mlx_whisper.load_models.load_model(model_path)  # outside lock
    except Exception as exc:
        logger.warning(...)
        return None
    with AudioLanguageID._cache_lock:
        if model_path not in AudioLanguageID._model_cache:  # re-check
            if len(AudioLanguageID._model_cache) >= 1:
                AudioLanguageID._model_cache.clear()
            AudioLanguageID._model_cache[model_path] = model
        model = AudioLanguageID._model_cache[model_path]
```

---

### F4 MED — `preview_sec=0` produces garbage LID output (W1300 F2 / W1334 F4 / W1405 F3 carry-forward)

**File:** `KrabEar/core/audio_lang_id.py`, lines 145-147

```python
preview_frames = int(sample_rate * preview_sec)  # = 0 when preview_sec == 0
audio_preview = audio_mono[:preview_frames]        # → empty array []
```

The empty `audio_preview` passes through `_resample` (no-op on empty array), then in
`_run_lid_inference` is padded to 30s zeros (line 325-326). `detect_language()` is called on
30 seconds of pure silence — this returns a garbage language code (typically "en" or "ja" for
silence) with high confidence. The caller has no way to distinguish this from real detection.

`_get_preview_sec()` has no guard: `float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))`
returns 0.0 if the setting is explicitly set to 0.

**Fix:** Clamp preview_sec to minimum 1.0s at point of use (or in `_get_preview_sec()`):
```python
preview_sec = max(1.0, self._get_preview_sec())
```

This is consistent with the existing 1s minimum frames guard on the input audio.

---

### F5 LOW — `clear_model_cache()` duplication and F1 not covered by existing tests

**Files:** `KrabEar/tests/test_audio_lang_id_mx_clear_cache_W1416.py`,
`KrabEar/tests/test_audio_lang_id.py`

The W1416 test file (`test_audio_lang_id_mx_clear_cache_W1416.py`) tests that `clear_model_cache()`
calls `mx.clear_cache()`. However, because the second `clear_model_cache()` definition (W1340's,
without `mx.clear_cache()`) silently overrides W1416's, the test passes only if it patches
`core.audio_lang_id._HAS_MLX` and mocks `mlx.core` at the module level — but the active method
does NOT call `mx.clear_cache()`.

Specifically, the test in W1416 checks:
```python
AudioLanguageID.clear_model_cache()
mock_clear.assert_called_once()
```
Since the active method is the W1340 version (no `mx.clear_cache()` call), this assertion
**will fail** unless the test itself has a bug (e.g., the mock intercepts before the method body
is reached). This means the W1416 regression test for F1 is either already failing in CI or has
a logical flaw.

No test covers the `preview_sec=0` edge case (F4 carry-forward, unaddressed across 7 audits).

---

## Summary

| Finding | Severity | Status | Carry-forward? |
|---------|----------|--------|----------------|
| F1: Duplicate `clear_model_cache()` — W1416 fix silently dead | HIGH | NEW | No |
| F2: Duplicate `_HAS_MLX` blocks — confusion, `mx = None` orphaned | HIGH | NEW | No |
| F3: `_cache_lock` held during `load_model()` cold call (1-3s) | MED | CARRY-FORWARD | W1405 F4 |
| F4: `preview_sec=0` → 30s silence → garbage LID | MED | CARRY-FORWARD | W1300 F2, W1334 F4, W1405 F3 |
| F5: W1416 regression test likely fails due to F1 | LOW | NEW | No |

**Root cause of F1+F2:** W1416 was applied as a naive insert on top of W1340 without reconciling
the existing `clear_model_cache()` definition and `_HAS_MLX` initialization.

**Still open from prior waves:** W1341 (pydantic reload, PR #1248), W1116 (RLock, PR #1031),
W1121 (SUPPORTED_LANGUAGES allowlist, PR #1033).

---

## Recommended Actions

1. **Immediate (F1):** Merge the two `clear_model_cache()` into one with both `_cache_lock` AND
   `mx.clear_cache()`. Remove the duplicate at lines 62-76.

2. **Immediate (F2):** Remove the second `_HAS_MLX` block (lines 37-42). Keep block 1 which
   also initializes `mx = None` as the fallback.

3. **Short-term (F4):** `preview_sec = max(1.0, self._get_preview_sec())` — one-line fix, been
   open since W1300.

4. **Medium-term (F3):** Implement double-checked locking for `load_model()` to avoid holding
   `_cache_lock` during the slow cold load.

5. **Test (F5):** Verify W1416 test suite passes after F1 fix. Add test for `preview_sec=0`.
