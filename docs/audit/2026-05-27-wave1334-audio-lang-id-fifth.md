# W1334 Fifth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1334 sub-agent (fifth-pass)
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)
**File:** `KrabEar/core/audio_lang_id.py`

---

## Prior Wave Merge State

All prior-wave PRs/branches remain **unmerged** into `codex/krab-ear-v2`:

| Wave | Branch | Description | Status |
|------|--------|-------------|--------|
| W1090 | `fix-audio-lang-id-W1090` | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** |
| W1109 | `audit-audio-lang-id-residual-W1109` | Second-pass audit doc | **OPEN** |
| W1116 | `fix-audio-lang-id-lock-W1116` | `_model_cache` RLock (thread-safety) | **OPEN** |
| W1117 | `fix-audio-lang-id-mx-clear-W1117` | `mx.clear_cache()` after inference | **OPEN** |
| W1121 | `fix-audio-lang-id-allowlist-W1121` | `SUPPORTED_LANGUAGES` allowlist | **OPEN** |
| W1265 | `audit/audio-lang-id-triple-W1265` | Third-pass audit doc (5 findings) | **OPEN** |
| W1271 | `fix-audio-lang-id-cache-evict-W1271` | `clear_model_cache()` + `_after_save_hook` | **OPEN** |
| W1300 | `audit/audio-lang-id-fourth-W1300` | Fourth-pass audit doc (5 findings) | **OPEN** |
| W1308 | `fix-settings-hook-5-paths-W1308` | `_fire_after_save_hooks` on 5 save paths | **OPEN** |

Verification:
```bash
git merge-base --is-ancestor aab15f4f codex/krab-ear-v2 || echo "W1308 NOT merged"
git merge-base --is-ancestor 9bfec568 codex/krab-ear-v2 || echo "W1271 NOT merged"
# → both print "NOT merged"
```

---

## W1308 Propagation Verification

W1308 (`fix-settings-hook-5-paths-W1308`, commit `aab15f4f`) adds a
`_fire_after_save_hooks(old, new)` helper and calls it from all five
`SettingsService` save paths:

| Method | W1308 adds `_fire_after_save_hooks`? |
|--------|--------------------------------------|
| `handle_set_settings` | YES (already had manual loop; now uses helper) |
| `handle_apply_profile_preset` | YES |
| `handle_set_notification_preferences` | YES |
| `handle_import_settings` | YES |
| `handle_restore_settings_backup` | YES |

The hook propagation itself is structurally complete in W1308. However,
W1308 introduces a **new residual gap** documented as F1 below.

---

## W1271 Hook Logic Verification

W1271's `_on_settings_saved_lang_id` hook in `BackendService.__init__` compares:
```python
old_model = str(old.get("model_balanced", ""))
new_model = str(new.get("model_balanced", ""))
```

The settings dict passed to hooks comes from `SettingsService.cached_settings()`,
which calls `StateStore.load_settings()`. That function seeds the dict from
`DEFAULT_SETTINGS` (imported from `backend.models`). The key `"model_balanced"`
(lowercase) is **absent** from `DEFAULT_SETTINGS` — the actual model path
is configured via the pydantic `Settings.MODEL_BALANCED` field (uppercase),
overridable by `KRAB_EAR_MODEL_BALANCED` env var. See F2 below.

---

## NEW Findings (5)

---

### F1 — HIGH: W1308 fixes hook propagation but misses `reload_settings_from_json()` on 4 paths

**Location:** `KrabEar/backend/settings_service.py` lines 297-303 (`handle_set_settings`
hot-reload block); `handle_apply_profile_preset` (line 332 area), `handle_import_settings`
(line 456 area), `handle_set_notification_preferences` (line 367 area),
`handle_restore_settings_backup` (line 527 area).

**Description:**

W1308 ensures `_fire_after_save_hooks(old, new)` is called from all five save paths,
so the W1271 `_on_settings_saved_lang_id` hook fires regardless of which path changed
the settings. However, `reload_settings_from_json()` — which hot-reloads the pydantic
`settings` singleton in `core/config.py` — is **only called from `handle_set_settings`**:

```python
# handle_set_settings only:
try:
    from core.config import reload_settings_from_json
    updated = reload_settings_from_json()
    ...
```

The other four paths (`apply_profile_preset`, `import_settings`,
`set_notification_preferences`, `restore_settings_backup`) save to `settings.json`
but do NOT call `reload_settings_from_json()`. As a result, after any of these
four paths execute, `AudioLanguageID._get_model_path()` still reads the stale
pydantic `settings.MODEL_BALANCED` value:

```python
def _get_model_path(self) -> str:
    ...
    return getattr(settings, "MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")
```

**Concrete failure scenario (W1271 + W1308 both merged):**

1. User changes model via `import_settings` with a JSON file containing a new
   `KRAB_EAR_MODEL_BALANCED`-equivalent key.
2. W1308 fires `_fire_after_save_hooks` → W1271's hook sees the change, calls
   `AudioLanguageID.clear_model_cache()`.
3. Cache is now empty. Next `detect()` call invokes `_get_model_path()`.
4. `_get_model_path()` returns `settings.MODEL_BALANCED` — pydantic singleton
   still holds the OLD value because `reload_settings_from_json()` was never called.
5. `_detect_with_mlx` loads the OLD model again into cache.
6. Cache eviction was pointless; stale model is in cache again.

Note: for `handle_apply_profile_preset`, the four built-in presets
(`default`, `meeting`, `translation`, `call_recording`) do not change
`MODEL_BALANCED`, so this is a latent issue only triggered by custom import
or restore operations that set model path fields.

**Severity:** HIGH — W1308 structurally solves hook coverage but leaves the
pydantic staleness gap that defeats the purpose of the cache eviction.

**Fix:** Call `reload_settings_from_json()` in all five save paths (after
`store.save_settings()` and before `_fire_after_save_hooks()`), or extract into a
shared `_save_and_notify(old, new, settings)` helper that calls both.

---

### F2 — HIGH: W1271 hook compares wrong key — `"model_balanced"` never appears in settings dict

**Location:** `KrabEar/backend/service.py` (W1271 branch `_on_settings_saved_lang_id`);
`KrabEar/backend/state_store.py:119-136` (`load_settings`);
`KrabEar/core/config.py` (`DEFAULT_SETTINGS` dict, `Settings.MODEL_BALANCED`)

**Description:**

W1271's cache-eviction hook compares:
```python
old_model = str(old.get("model_balanced", ""))
new_model = str(new.get("model_balanced", ""))
if new_model != old_model:
    AudioLanguageID.clear_model_cache()
```

The settings dict received by hooks is the output of `SettingsService.cached_settings()`,
which seeds from `DEFAULT_SETTINGS` in `backend/models.py`. Inspecting `DEFAULT_SETTINGS`
and `config.py`:

```bash
grep '"model_balanced"' KrabEar/core/config.py   # → no output
grep '"model_balanced"' KrabEar/backend/models.py # → no output (only MODEL_BALANCED
                                                  #   as pydantic field, uppercase)
```

The key `"model_balanced"` (lowercase) does NOT exist in `DEFAULT_SETTINGS`.
`StateStore.load_settings()` seeds from `DEFAULT_SETTINGS` and then overlays
`settings.json`. Unless a user explicitly sets `{"model_balanced": "..."}` via
IPC `set_settings`, this key is absent from the returned dict.

The actual model path is controlled by the pydantic `Settings.MODEL_BALANCED` field
(overridden via `KRAB_EAR_MODEL_BALANCED` env var). This field is NOT stored in
`settings.json` — it is a `pydantic-settings` env-var field only. Therefore:

- `old.get("model_balanced", "")` → always `""` (key absent)
- `new.get("model_balanced", "")` → always `""` (key absent)
- `"" != ""` → `False` → **cache eviction NEVER fires**

W1271's hook is completely inert in production. The stale-model bug it aims to fix
persists entirely.

**Severity:** HIGH — the central mechanism of W1271's fix is broken by key name mismatch.
Even with both W1271 and W1308 merged, LID model cache is never evicted on model change.

**Fix:** Either:
1. Add `"model_balanced"` to `DEFAULT_SETTINGS` in `config.py` defaulting to
   `settings.MODEL_BALANCED`, so it propagates through `StateStore.load_settings()`.
2. Or compare pydantic fields directly in the hook:
   ```python
   from core.config import settings as _cfg
   old_model = _cfg.MODEL_BALANCED  # before reload
   # ... call reload_settings_from_json() ...
   new_model = _cfg.MODEL_BALANCED  # after reload
   if new_model != old_model:
       AudioLanguageID.clear_model_cache()
   ```

---

### F3 — MEDIUM: W1271 `_cache_lock` held during `load_model()` — 1-3 s lock contention on cold load

**Location:** `KrabEar/core/audio_lang_id.py` (W1271 branch `_detect_with_mlx`);
`clear_model_cache()` classmethod

**Description:**

W1271's `_detect_with_mlx` acquires `_cache_lock` for the entire model load path:

```python
with AudioLanguageID._cache_lock:
    if model_path not in AudioLanguageID._model_cache:
        ...
        model = mlx_whisper.load_models.load_model(model_path)  # 1–3s cold load
        AudioLanguageID._model_cache[model_path] = model
    model = AudioLanguageID._model_cache[model_path]
```

`load_model()` for `whisper-large-v3-turbo` takes 1–3 seconds on first call (cold load
from disk/HuggingFace cache). During this time `_cache_lock` is held, blocking any
concurrent call to `clear_model_cache()`:

```python
# clear_model_cache (called from _fire_after_save_hooks):
with cls._cache_lock:  # BLOCKS here for up to 3 seconds
    cls._model_cache.clear()
```

The consequence: when a user calls `set_settings` concurrent with the first LID
inference (e.g., during STT warmup), the IPC handler thread is blocked for 1-3 seconds
waiting for `_cache_lock`. The IPC socket has a read timeout; a 3-second block can
cause the Swift agent to receive a timeout error on the `set_settings` call.

Note: `_detect_with_mlx` already runs inside `mlx_lock()` (an RLock). The
`_cache_lock` is a non-reentrant `threading.Lock`. The lock ordering is:
`mlx_lock` → `_cache_lock` (inference path) vs `_cache_lock` only (eviction path).
This does not deadlock in current code, but is fragile if any future caller takes
`mlx_lock` while calling `clear_model_cache()`.

**Severity:** MEDIUM — not a deadlock but causes IPC latency spikes during concurrent
settings-save + first LID inference.

**Fix:** Only hold `_cache_lock` for dict mutation, not for `load_model()`:
```python
with AudioLanguageID._cache_lock:
    model = AudioLanguageID._model_cache.get(model_path)
    need_load = (model is None)
if need_load:
    model = mlx_whisper.load_models.load_model(model_path)  # outside lock
    with AudioLanguageID._cache_lock:
        AudioLanguageID._model_cache[model_path] = model
```

---

### F4 — MEDIUM: W1300 F2 (`preview_sec=0`) still unaddressed — open in base branch

**Location:** `KrabEar/core/audio_lang_id.py:130-138` (`_get_preview_sec`),
lines 101-102 (preview slice), `_detect_with_mlx` line 243 (`np.max` on empty array)

**Description:**

W1300 F2 identified that `_get_preview_sec()` returns unvalidated values, so
`stt_audio_lang_id_preview_sec = 0` (set via `set_settings`) produces an empty
`audio_preview` array, which causes a `ValueError` from `np.max(np.abs([]))` inside
`_detect_with_mlx`. This exception is caught by the surrounding `except Exception`
and misattributed to `log_mel_spectrogram`.

This finding is **still open** in `codex/krab-ear-v2` (W1300 branch unmerged):

```python
# _get_preview_sec() — no lower-bound clamp:
return float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
# preview_sec = 0.0 → audio_preview = audio_mono[:0] → empty
# → np.max(np.abs(empty)) → ValueError (masked by except, logged as mel failure)
```

Confirmed reproducible:
```python
import numpy as np
np.max(np.abs(np.array([], dtype=np.float32)))
# ValueError: zero-size array to reduction operation maximum which has no identity
```

W1300 F2 was rated MEDIUM. Carrying forward as F4 here because it is a distinct
finding from W1334's other four new findings.

**Severity:** MEDIUM — misleading error log + silent language routing fallback to `"ru"`
on a valid user settings operation (setting preview to 0 to disable LID).

**Fix (not yet applied in any merged branch):**
```python
def _get_preview_sec(self) -> float:
    if self._preview_sec is not None:
        return self._preview_sec
    try:
        from core.config import settings
        raw = float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
        return max(1.0, raw)  # lower-bound: at least 1 second
    except Exception:
        return 5.0
```

---

### F5 — INFO: No test coverage for the W1308 pydantic-reload gap (F1)

**Location:** `KrabEar/tests/test_settings_service_hooks_W1308.py` (W1308 branch,
not merged); `KrabEar/tests/test_audio_lang_id.py`

**Description:**

W1308 ships 23 tests in `test_settings_service_hooks_W1308.py`. Inspection of the
test file (via `git show fix-settings-hook-5-paths-W1308`) confirms these tests verify:

- `_fire_after_save_hooks` is called from all 5 paths
- Hook receives correct `(old, new)` dicts
- Hook exceptions are swallowed

However, no test verifies that `reload_settings_from_json()` is called before
`_fire_after_save_hooks()` in the `handle_set_settings` path, and no test at all
checks that pydantic `settings.MODEL_BALANCED` reflects the new value after any of
the five save paths. The F1 gap (pydantic staleness after non-`set_settings` paths)
is therefore invisible in CI even after W1308 merges.

Additionally, `test_audio_lang_id.py` (29 test methods, 577 lines) still has no test
for `_get_preview_sec()` returning 0 (W1300 F2 / F4 above) and no test for
`clear_model_cache()` + `_get_model_path()` interaction after pydantic reload.

**Severity:** INFO — test gap does not cause production failures but means F1 and F2
regressions will not be caught by CI.

**Fix:** Add to `test_settings_service_hooks_W1308.py`:
```python
def test_set_settings_calls_reload_settings_from_json(self):
    """reload_settings_from_json() must be called by handle_set_settings."""
    with patch("backend.settings_service.reload_settings_from_json") as mock_reload:
        self.svc.handle_set_settings({"quality_profile": "balanced"})
        mock_reload.assert_called_once()

def test_apply_profile_preset_does_not_reload_pydantic(self):
    """Documents F1: apply_profile_preset saves but does NOT reload pydantic settings."""
    with patch("backend.settings_service.reload_settings_from_json") as mock_reload:
        self.svc.handle_apply_profile_preset({"profile": "meeting"})
        mock_reload.assert_not_called()  # F1: this should fail once F1 is fixed
```

---

## Summary Table

| ID | Severity | Description | Root file |
|----|----------|-------------|-----------|
| F1 | **HIGH** | W1308 adds hook calls but skips `reload_settings_from_json()` on 4 paths → pydantic `settings.MODEL_BALANCED` stale → W1271 eviction re-loads old model | `settings_service.py` |
| F2 | **HIGH** | W1271 hook compares `"model_balanced"` (lowercase) — key absent from `DEFAULT_SETTINGS`/settings.json → hook always compares `""==""` → cache eviction **never fires** | `service.py` (W1271 branch) |
| F3 | **MEDIUM** | W1271 holds `_cache_lock` during `load_model()` (1-3s) — blocks `clear_model_cache()` and IPC response thread | `audio_lang_id.py` (W1271 branch) |
| F4 | **MEDIUM** | W1300 F2 still open: `preview_sec=0` → empty array → masked `ValueError` attributed to mel spectrogram | `audio_lang_id.py:243` |
| F5 | INFO | W1308 tests do not cover pydantic reload gap; no tests for F2 key-mismatch or F4 preview clamp | `tests/` |

**Net finding count:** 5 new findings (2 HIGH, 2 MEDIUM, 1 INFO).
All 9 prior-wave PRs remain unmerged as of 2026-05-27.

---

## W1271 + W1308 Combined State Assessment

When both W1271 and W1308 are merged into `codex/krab-ear-v2`:

- W1308 **does** successfully fire hooks on all 5 save paths (structural goal met).
- W1271's hook **does not** evict the LID cache because it compares the wrong key name
  (F2 — the hook is completely inert).
- Even if F2 were fixed, the pydantic `settings.MODEL_BALANCED` read by
  `_get_model_path()` would remain stale on 4 of 5 paths because W1308 does not
  propagate `reload_settings_from_json()` (F1).
- W1271's `_cache_lock`-during-`load_model()` creates IPC latency contention (F3).

The core W1265 F1 finding ("stale model after profile switch") remains unresolved
after all three fix branches (W1271, W1308, W1300) even if merged in sequence.
