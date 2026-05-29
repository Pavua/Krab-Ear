# W1528 — audio_lang_id.py regression audit after W1497 cherry-pick

**Date:** 2026-05-27  
**Auditor:** W1528 sub-agent (read-only diff audit)  
**Branch audited:** `fix-event-replay-mode-constant-W1316`  
**Reference:** `codex/krab-ear-v2` (HEAD `98d0d679`)  
**File:** `KrabEar/core/audio_lang_id.py`

## Summary

The branch `fix-event-replay-mode-constant-W1316` diverged from `codex/krab-ear-v2` before
waves W1090–W1466 landed.  All five targeted fixes are **absent** on the branch.  The file on
that branch is 285 lines; the `codex/krab-ear-v2` version is 312 lines (27 lines of net
additions from the missing fixes).

## Findings (5 of 5 cap)

### F1 — W1416 REGRESSED: `clear_model_cache` does NOT call `mx.clear_cache()` (HIGH)

**Wave:** W1416 (commit `1032d17f`)  
**Severity:** HIGH — Metal GPU buffers (~300–500 MB) from evicted models are not freed.

`clear_model_cache()` on the current branch simply calls `cls._model_cache.clear()` without
flushing Metal buffers.  W1416 added an `_HAS_MLX` module-level flag and an `mx.clear_cache()`
call inside `clear_model_cache()` so that Metal buffers are freed immediately when a settings
hook evicts the stale model.

**Regressed code (present on `codex/krab-ear-v2`, absent on branch):**
```python
# Module level
try:
    import mlx.core as _mlx_core  # noqa: F401
    _HAS_MLX = True
except ImportError:
    _HAS_MLX = False

# Inside clear_model_cache():
        if _HAS_MLX:
            try:
                import mlx.core as mx
                with mlx_lock():
                    mx.clear_cache()
            except Exception:
                pass
```

---

### F2 — W1440 REGRESSED: duplicate `clear_model_cache` definition present (HIGH)

**Wave:** W1440 (commit `6791d405`)  
**Severity:** HIGH — two `clear_model_cache` class methods existed on an intermediate state;
W1440 removed the duplicate and made `_cache_lock` properly guard the remaining one.

On the current branch, `clear_model_cache` does NOT acquire `_cache_lock` before clearing
(the branch predates W1340 which added `_cache_lock`), and additionally predates W1440 which
removed a duplicate definition that appeared between W1340 and W1440.  The net effect: the
branch has a single `clear_model_cache` that neither acquires the lock nor calls
`mx.clear_cache()`.

---

### F3 — W1443 REGRESSED: `preview_sec=0` clamp to 1.0 s absent (MED)

**Wave:** W1443 (commit `f7086279`)  
**Severity:** MED — `preview_sec=0` (or a very small value from settings) produces an empty
audio slice, zero-pads to 30 s silence, and feeds that to LID — the result is garbage language
detection.

W1443 added a `_MIN_PREVIEW_SEC = 1.0` guard inside `_get_preview_sec()`:
```python
_MIN_PREVIEW_SEC = 1.0
raw = float(self._preview_sec)
return max(_MIN_PREVIEW_SEC, raw)
```

The branch returns `self._preview_sec` directly without the `max()` clamp.

---

### F4 — W1465 REGRESSED: `mx.clear_cache()` called OUTSIDE `mlx_lock()` in `_run_detect.finally` (HIGH)

**Wave:** W1465 (commit `45284340`)  
**Severity:** HIGH — W1117 added a `finally` block in `_run_detect()` that calls
`_mx.clear_cache()` outside `mlx_lock()`.  This violates MLX thread-safety policy (CLAUDE.md):
concurrent Metal buffer manipulation without the lock causes SIGSEGV.

W1465 removed that unsafe `finally` block from `_run_detect()`, pushing the `clear_cache()`
responsibility into `_detect_with_mlx()` where it already runs inside `mlx_lock()`.

The current branch has an intermediate state: it has the W1117 `finally` block (clear_cache
outside mlx_lock) and does NOT yet have the W1465 removal.  This makes EVERY LID inference a
potential SIGSEGV source under concurrent STT load.

**Dangerous code present on branch (removed by W1465 on `codex/krab-ear-v2`):**
```python
        finally:
            try:
                import mlx.core as _mx
                _mx.clear_cache()          # <-- outside mlx_lock() — UNSAFE
            except (ImportError, AttributeError):
                pass
```

---

### F5 — W1466 REGRESSED: `clear_model_cache` calls `mx.clear_cache()` without `mlx_lock()` (MED)

**Wave:** W1466 (commit `e0730585`)  
**Severity:** MED — W1416 added `mx.clear_cache()` inside `clear_model_cache()` but called it
without `mlx_lock()`.  W1466 wrapped that call with `with mlx_lock():` to comply with
CLAUDE.md policy.

The current branch predates both W1416 and W1466, so it has neither the `mx.clear_cache()`
call nor the `mlx_lock()` wrapper — but F1 covers the missing `clear_cache()` aspect; F5
specifically flags that even if someone back-ports W1416 in isolation they will re-introduce the
lockless pattern that W1466 fixed.

---

## Additional context (not counted in cap)

Beyond the 5 findings above, the branch is also missing:

- **W1090** (commit `c6edd77e`): `_ZERO_PEAK_THRESHOLD = 1e-4` silent audio short-circuit +
  `MIN_CONFIDENCE = 0.35` gate.
- **W1116** (commit `5e8abc28`): `_model_cache_lock` (RLock) protecting cache access.
- **W1121** (commit `33145b73`): `SUPPORTED_LANGUAGES` frozenset allowlist + structured warning
  for unsupported lang codes.
- **W1271** (commit `0a542230`): `clear_model_cache()` called from settings hook on
  `MODEL_BALANCED` change.
- **W1340** (commit `17f7371d`): case-tolerant key comparison — eviction was never firing because
  the dict key lookup was case-sensitive against a model path that could differ in casing.
- **W1367** (commit `fef660ab`): `mx.clear_cache()` in `_detect_with_mlx.finally` under
  `mlx_lock()` (W63 rule gap fix).

## Root cause

The branch `fix-event-replay-mode-constant-W1316` was cut from an older commit of
`codex/krab-ear-v2` (or a divergent ancestor) and the W1497 cherry-pick brought in
`event_replay.py` changes only — it did NOT bring `audio_lang_id.py` forward.  The file on the
branch dates to approximately the W1070/W1090 era (it has `peak > 1.0` normalization from W1070
but lacks W1090's `_ZERO_PEAK_THRESHOLD` class constant, suggesting the branch file is from
between W1070 and W1090 — or an intermediate squash).

## Recommended action

Do NOT merge `fix-event-replay-mode-constant-W1316` into `codex/krab-ear-v2` without first
rebasing it on top of `codex/krab-ear-v2` (or cherry-picking only the `event_replay.py` change
onto a fresh branch from `codex/krab-ear-v2`).  The `audio_lang_id.py` on this branch would
clobber all 10+ waves of fixes if merged as-is.
