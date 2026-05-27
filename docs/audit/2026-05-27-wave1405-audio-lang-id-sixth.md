# W1405 Sixth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1405 sub-agent (sixth-pass)
**Branch audited:** `origin/codex/krab-ear-v2` (HEAD `8ba7face`, post-W1340+W1367+W1308 merged)
**File:** `KrabEar/core/audio_lang_id.py`
**Focus:** Post-W1340 case-mismatch fix verification, W1341 reload status, new interaction bugs

---

## Prior Wave Merge State Matrix

| Wave | PR | Description | Status |
|------|----|-------------|--------|
| W1090 | #1004 | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** |
| W1109 | #1019 | Second-pass audit doc | **OPEN** |
| W1116 | #1031 | `_model_cache` RLock (thread-safety) | **OPEN** |
| W1117 | #1035 | `mx.clear_cache()` after inference | **OPEN** (superseded by W1367) |
| W1121 | #1033 | `SUPPORTED_LANGUAGES` allowlist | **OPEN** |
| W1265 | #1171 | Third-pass audit doc (5 findings) | **OPEN** |
| W1271 | #1177 | `clear_model_cache()` + `_after_save_hook` | **OPEN** (superseded by W1340) |
| W1300 | #1205 | Fourth-pass audit doc | **MERGED** (doc only) |
| W1308 | #1210 | SettingsService fires `_after_save_hooks` on all 5 save paths | **MERGED** |
| W1334 | #1243 | Fifth-pass audit doc | **OPEN** |
| W1340 | #1253 | Case-tolerant `_get_model_balanced()` + `clear_model_cache()` + `_cache_lock` | **MERGED** |
| W1341 | #1248 | `_reload_and_fire_hooks()` — pydantic reload on all 5 paths | **OPEN** |
| W1367 | #1277 | `mx.clear_cache()` in `finally` after LID inference (W63 rule) | **MERGED** |

Verification commands:
```bash
git log origin/codex/krab-ear-v2 --oneline | grep -E "wave1340|wave1308|wave1367"
# → 17f7371d fix(wave1340)... (#1253)
# → 65402fb6 fix(wave1308)... (#1210)
# → fef660ab fix(wave1367)... (#1277)

git log origin/codex/krab-ear-v2 --oneline | grep -E "wave1341|wave1090|wave1116|wave1117|wave1121|wave1271"
# → no output (all still OPEN)
```

---

## W1340 Verification

W1340 introduced:
1. `threading.Lock` class attribute `_cache_lock` protecting `_model_cache`
2. `clear_model_cache()` classmethod (acquires `_cache_lock`, clears dict)
3. `_get_model_balanced(d)` helper in `service.py` checking `"MODEL_BALANCED"`, `"model_balanced"`,
   `"stt_model_balanced"` in priority order
4. `_on_settings_saved_lang_id` hook registered via `register_after_save_hook`
5. `_run_lid_inference()` wraps model load/retrieval under `_cache_lock`

**Confirmed correct post-W1340:**
- `settings.json` stores keys lowercase (`model_balanced`). `_get_model_balanced()` checks
  both cases → correctly finds value regardless of key form.
- Hook fires on `handle_set_settings` with `model_balanced` key in both old/new dicts.
- `pydantic settings.MODEL_BALANCED` (uppercase attr) is hot-reloaded in `handle_set_settings`
  via `reload_settings_from_json()` (called before hooks) → `_get_model_path()` gets new
  value on the very next `detect()` call.
- Model ref is stored in local variable before `_cache_lock` release → concurrent eviction
  cannot invalidate in-flight inference.
- Lock ordering: `mlx_lock()` (outer RLock) → `_cache_lock` (inner Lock) → no reverse
  acquisition → no deadlock risk.

**W1334 F2 HIGH (case-mismatch) is FIXED.** The hook now correctly detects model changes.

---

## W1367 Verification

W1367 moved `mx.clear_cache()` into a `try/finally` block in `_detect_with_mlx()`:

```python
def _detect_with_mlx(self, mlx_whisper, audio_16k):
    try:
        return self._run_lid_inference(mlx_whisper, audio_16k)
    finally:
        if _HAS_MLX:
            mx.clear_cache()
```

This `finally` executes **inside `mlx_lock()`** (called from `_run_detect` → `with mlx_lock()`),
which is correct for MLX thread-safety. Covers all return paths including exceptions.

---

## W1341 Status (NOT MERGED — open gap)

W1341 would add `_reload_and_fire_hooks()` as a single point of truth: reload pydantic
`Settings` from disk, then fire registered hooks. Currently:
- `handle_set_settings`: calls `reload_settings_from_json()` THEN fires hooks → pydantic fresh.
- `handle_import_settings`: fires hooks (W1308) but does NOT call `reload_settings_from_json()`.
- `handle_restore_settings_backup`: same — hooks fire but pydantic stays stale.
- `handle_apply_profile_preset`: same — no pydantic reload (but built-in presets never set
  `model_balanced`, so eviction would not fire anyway).
- `handle_set_notification_preferences`: same.

---

## NEW Findings (5)

The following findings are **distinct from all prior waves** in the post-W1340 baseline.

---

### F1 — HIGH: W1341 NOT MERGED — pydantic staleness defeats eviction on import/restore paths

**Location:** `KrabEar/backend/settings_service.py:418-482` (`handle_import_settings`),
`KrabEar/backend/settings_service.py:517-535` (`handle_restore_settings_backup`);
`KrabEar/core/audio_lang_id.py:162-171` (`_get_model_path`)

**Description:**

After W1340 merged, the eviction hook fires correctly on **all 5 save paths** (W1308).
However, on four of those paths — `import_settings`, `restore_settings_backup`,
`apply_profile_preset`, `set_notification_preferences` — `reload_settings_from_json()` is
**not called**, so the pydantic `Settings` singleton retains stale values.

Failure sequence:
1. User calls `import_settings(file)` with JSON containing `"model_balanced": "new/model"`.
2. File is saved, cache invalidated, `_fire_after_save_hooks` called.
3. `_on_settings_saved_lang_id` sees model changed → calls `AudioLanguageID.clear_model_cache()`.
4. Eviction succeeds. Cache is empty.
5. Next `detect()` call → `_get_model_path()` → `getattr(settings, "MODEL_BALANCED", ...)`.
6. `settings.MODEL_BALANCED` is the **stale old value** (pydantic not reloaded).
7. `detect()` loads the **old model** from disk. Eviction was a no-op.

**Severity:** HIGH — cache eviction (W1340) works structurally but is semantically a no-op
for `import_settings` and `restore_settings_backup` paths because `_get_model_path()` reads
from the stale pydantic singleton, re-loading the same old model.

**Fix:** Merge W1341 (PR #1248) — adds `_reload_and_fire_hooks()` that calls
`reload_settings_from_json()` before firing hooks on all 5 paths.

---

### F2 — MED: `clear_model_cache()` does not call `mx.clear_cache()` — Metal buffers linger

**Location:** `KrabEar/core/audio_lang_id.py:67-77` (`clear_model_cache`)

**Description:**

W1340 correctly clears `_model_cache` when the model path changes, dropping Python's reference
to the model object. However, Python GC is non-deterministic — the actual deallocation of
MLX model weights from Metal GPU memory may be delayed until the next GC cycle.

Meanwhile, `mx.clear_cache()` (added by W1367) is called **after inference completes**, not
after eviction. So when a settings-triggered eviction occurs:

```
clear_model_cache() → dict cleared → Python refcount → 0
↓
GC non-deterministic: model object may linger in memory for seconds/minutes
↓
mx.clear_cache() not called → Metal buffers not freed until next inference's finally block
```

A balanced Whisper model (`whisper-large-v3-turbo`) holds ~300–500 MB in Metal memory.
After a profile switch, these buffers can linger until the user makes the next dictation,
at which point W1367's `finally: mx.clear_cache()` frees them (along with the new model's
intermediate buffers). On M4 Max with 36 GB this is tolerable but undesirable.

**Current code:**

```python
@classmethod
def clear_model_cache(cls) -> None:
    with cls._cache_lock:
        cls._model_cache.clear()
    logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
    # BUG: no mx.clear_cache() → Metal buffers not freed until next inference
```

**Fix:**

```python
@classmethod
def clear_model_cache(cls) -> None:
    with cls._cache_lock:
        cls._model_cache.clear()
    logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
    # Promptly release MLX Metal buffers freed by removing model reference
    if _HAS_MLX:
        try:
            mx.clear_cache()
        except Exception as exc:
            logger.debug("AudioLanguageID.clear_model_cache: mx.clear_cache failed: %s", exc)
```

**Severity:** MED — no correctness bug, but 300–500 MB Metal memory pressure between a
profile switch and the next dictation. Distinct from W1300 F3 (which was about normal-operation
weights being held in cache; this is about eviction not promptly releasing freed memory).

---

### F3 — MED: `preview_sec=0` produces all-zeros 30 s mel input — garbage LID result (W1300 F2 / W1334 F4 carry-forward)

**Location:** `KrabEar/core/audio_lang_id.py:111-124` (`detect`)

**Description:**

`_get_preview_sec()` returns raw `settings.STT_AUDIO_LANG_ID_PREVIEW_SEC` without clamping.
If a user sets `stt_audio_lang_id_preview_sec = 0` via `set_settings`:

```python
preview_sec = self._get_preview_sec()          # → 0.0
preview_frames = int(sample_rate * preview_sec) # → 0
audio_preview = audio_mono[:preview_frames]     # → empty array (shape: (0,))
# Then padded to 30s zeros:
audio_norm = np.pad(audio_preview, (0, n_samples - len(audio_preview)))
# → 480 000 samples of silence passed to detect_language → garbage result
```

The `min_frames` guard (line 112–120) checks `audio_mono` length, not `audio_preview` length,
so a 3-second recording passes the guard and then gets clipped to zero. Without the
`MIN_CONFIDENCE` gate (W1090, not merged), the garbage language code is returned to the
STT router.

**Severity:** MED — `stt_audio_lang_id_preview_sec=0` is an edge case but available to UI.
Logged as "log_mel_spectrogram failed" which is misleading; actual root cause is silent input.

**Fix:** Clamp in `_get_preview_sec()`:

```python
val = float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
return max(val, 1.0)  # minimum 1 second of preview
```

---

### F4 — LOW: `_cache_lock` (non-reentrant Lock) held for 1–3 s during cold model load

**Location:** `KrabEar/core/audio_lang_id.py:260-277` (`_run_lid_inference`)

**Description:**

W1340 wraps model load inside `_cache_lock` to prevent races. However, `_cache_lock` is
a `threading.Lock` (non-reentrant), and `mlx_whisper.load_models.load_model()` takes 1–3 s on
a cold first call (model is not in RAM cache). During this window:

- Any other thread calling `detect()` blocks for 1–3 s waiting for `_cache_lock`.
- `clear_model_cache()` (from settings hook) also blocks.

This converts what would be cache-hit parallelism into a serial queue on cold start.
The lock scope is larger than necessary — the dict check and insert are the only operations
requiring mutual exclusion; the model load itself does not.

**Recommended fix:** Load model outside the lock, then re-check inside:

```python
# Check if load needed (fast path)
with AudioLanguageID._cache_lock:
    cached = AudioLanguageID._model_cache.get(model_path)
if cached is not None:
    model = cached
else:
    # Load outside lock (slow, 1-3s)
    loaded = mlx_whisper.load_models.load_model(model_path)
    # Insert with eviction inside lock
    with AudioLanguageID._cache_lock:
        if model_path not in AudioLanguageID._model_cache:
            if len(AudioLanguageID._model_cache) >= 1:
                AudioLanguageID._model_cache.clear()
            AudioLanguageID._model_cache[model_path] = loaded
        model = AudioLanguageID._model_cache[model_path]
```

**Severity:** LOW — affects only the cold-start scenario (model not yet loaded). Subsequent
calls hit the cache with negligible lock contention. The current design is safe; this is a
latency optimization.

---

### F5 — LOW: No tests for `clear_model_cache()` + `mx.clear_cache()` interaction; no tests for import/restore eviction round-trip

**Location:** `KrabEar/tests/` (missing test file)

**Description:**

Post-W1340+W1367+W1308, the following test scenarios are absent:

1. `clear_model_cache()` does NOT call `mx.clear_cache()` — not asserted anywhere (F2).
2. `handle_import_settings` with `model_balanced` change → hook fires → pydantic stale →
   next `_get_model_path()` returns old value (F1 regression test).
3. `handle_restore_settings_backup` → same.
4. Cold-load with concurrent `clear_model_cache()` call during lock hold (F4 scenario).

W1340's 15 tests cover the hook firing correctly on `handle_set_settings` path and
`clear_model_cache()` API, but do not test the Metal memory release or import/restore paths.

**Severity:** LOW — test coverage gap; the underlying bugs (F1, F2) exist regardless.

---

## Summary

| # | Severity | Description | Root file | New in W1405? |
|---|----------|-------------|-----------|---------------|
| F1 | HIGH | W1341 not merged: pydantic stale on import/restore paths defeats eviction | `settings_service.py` | Carry-forward (W1334 F1), still unresolved |
| F2 | MED | `clear_model_cache()` lacks `mx.clear_cache()` — Metal buffers linger post-eviction | `audio_lang_id.py` | **NEW** (post-W1340 interaction) |
| F3 | MED | `preview_sec=0` → empty mel → garbage LID result | `audio_lang_id.py` | Carry-forward (W1300 F2) |
| F4 | LOW | Non-reentrant `Lock` held during 1–3 s cold load | `audio_lang_id.py` | Carry-forward (W1334 F3) |
| F5 | LOW | Missing tests for eviction+Metal+import/restore paths | `tests/` | **NEW** (post-W1340 test gap) |

**Most urgent:** Merge W1341 (PR #1248, fixes F1). Then fix F2 (4-line change to
`clear_model_cache()`). F3 requires lower-bound clamp in `_get_preview_sec()`.

**Still blocked by unmerged PRs:**
- W1090 (MIN_CONFIDENCE gate + zero-peak short-circuit): PR #1004
- W1116 (RLock for `_model_cache`): PR #1031
- W1121 (SUPPORTED_LANGUAGES allowlist): PR #1033
