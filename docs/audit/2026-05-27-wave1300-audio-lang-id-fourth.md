# W1300 Fourth-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-27
**Auditor:** W1300 sub-agent (fourth-pass)
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)
**File:** `KrabEar/core/audio_lang_id.py`

---

## Prior Wave Merge State

All seven prior-wave PRs/branches remain **unmerged** into `codex/krab-ear-v2`:

| Wave | Branch                                   | Description                                    | Status |
|------|------------------------------------------|------------------------------------------------|--------|
| W1090 | `fix-audio-lang-id-W1090`              | Zero-peak short-circuit + MIN_CONFIDENCE gate  | **OPEN** |
| W1109 | `audit-audio-lang-id-residual-W1109`   | Second-pass audit doc                          | **OPEN** |
| W1116 | `fix-audio-lang-id-lock-W1116`         | `_model_cache` RLock (thread-safety)           | **OPEN** |
| W1117 | `fix-audio-lang-id-mx-clear-W1117`     | `mx.clear_cache()` after inference             | **OPEN** |
| W1121 | `fix-audio-lang-id-allowlist-W1121`    | `SUPPORTED_LANGUAGES` allowlist                | **OPEN** |
| W1265 | `audit/audio-lang-id-triple-W1265`     | Third-pass audit doc (5 findings)              | **OPEN** |
| W1271 | `fix-audio-lang-id-cache-evict-W1271`  | `clear_model_cache()` + `_after_save_hook`     | **OPEN** |

Verification:
```bash
git branch -a --merged codex/krab-ear-v2 | grep -E "W1090|W1109|W1116|W1117|W1121|W1265|W1271"
# → no output (all 7 unmerged)
```

The W1019 language_detector fix (`fix-language-detector-FR-TR-W1019`) is also
**not merged**, consistent with W1265 finding.

---

## W1265 Finding Carry-over

The following W1265 findings remain **unaddressed** in `codex/krab-ear-v2`:

- **W1265-F1** (MEDIUM): No `_model_cache` eviction hook when `MODEL_BALANCED` changes.
  W1271 introduces `clear_model_cache()` + `_after_save_hook`, but W1271 is itself
  unmerged. See new F1 below for a structural incompleteness in W1271 regardless.
- **W1265-F2** (MEDIUM): Confidence value from `detect_language` tuple branch silently
  discarded. W1090 adds `MIN_CONFIDENCE` gate, but W1090 is unmerged.
- **W1265-F3** (LOW): `STTRouter` fallback on LID-None is `"ru"` instead of `"und"`.
  No branch addresses this.
- **W1265-F4** (LOW): No arbitration between `AudioLanguageID` and
  `CodeSwitchingDetector` signals. No branch addresses this.
- **W1265-F5** (INFO): Missing tests for `_get_model_path()` fallback paths. Partially
  addressed by W1090/W1116/W1117 test additions (unmerged), but base branch still unset.

---

## NEW Findings (5)

The following findings are **distinct from all prior waves** and exist in the
current `codex/krab-ear-v2` baseline.

---

### F1 — HIGH: W1271 `clear_model_cache` hook blind to 4 of 5 save paths in `SettingsService`

**Location:** `KrabEar/backend/settings_service.py:305-309, 326, 381, 467, 531`;
`KrabEar/backend/service.py:212-216` (and W1271 line ~228-242)

**Description:**

W1271's proposed fix registers a `_on_settings_saved_lang_id` hook via
`SettingsService.register_after_save_hook()`. The hooks list is only iterated in
**`handle_set_settings`** (line 305 of `settings_service.py`). Four other save paths call
`store.save_settings()` directly without invoking `_after_save_hooks`:

| Method | Line | Saves settings? | Runs hooks? |
|--------|------|-----------------|-------------|
| `handle_set_settings` | 283 | yes | **YES** |
| `handle_apply_profile_preset` | 326 | yes | NO |
| `handle_set_notification_preferences` | 381 | yes | NO |
| `handle_import_settings` | 467 | yes | NO |
| `handle_restore_settings_backup` | 531 | yes | NO |

If a user changes `model_balanced` via any path other than `set_settings` — for example
by importing a settings file with `import_settings` or restoring a backup via
`restore_settings_backup` — the LID model cache will NOT be evicted even after W1271
lands. The stale model persists until the path self-evicts on the next `detect()` call
(which causes an in-lock cold-load stall, exactly the latency spike W1271 aims to
prevent).

Additionally, `handle_apply_profile_preset` also skips the
`reload_settings_from_json()` hot-reload (line 297-303 in `handle_set_settings`), so
the pydantic `Settings` singleton is not updated — `AudioLanguageID._get_model_path()`
reads the stale `settings.MODEL_BALANCED` until the next pydantic reload.

**Severity:** HIGH — makes W1271's fix structurally incomplete regardless of merge
order. Even after all 7 PRs land, the 4 bypass paths remain.

**Fix:** Extract the after_save_hook notification into a shared `_fire_after_save_hooks`
helper called by ALL five save paths, or use a `preset.changed` EventBus subscriber in
`AudioLanguageID` that responds to the event already emitted by `handle_apply_profile_preset`.

---

### F2 — MEDIUM: `_get_preview_sec()` has no lower-bound clamp — zero/negative value causes masked `ValueError`

**Location:** `audio_lang_id.py:130-138` (`_get_preview_sec`),
`audio_lang_id.py:101-102` (preview slice), `audio_lang_id.py:239-256` (`_detect_with_mlx`)

**Description:**

`_get_preview_sec()` returns whatever float is stored in
`settings.STT_AUDIO_LANG_ID_PREVIEW_SEC` with no validation:

```python
return float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
```

If a user sets `stt_audio_lang_id_preview_sec = 0` via `set_settings`, then:

```python
preview_frames = int(sample_rate * 0)  # → 0
audio_preview = audio_mono[:0]         # → empty ndarray
```

`_detect_with_mlx` receives an empty array. At line 243:

```python
peak = float(np.max(np.abs(audio_norm)))  # ValueError: zero-size array
```

This `ValueError` is caught by the surrounding `except Exception` (line 254) and logged
as:

```
AudioLanguageID: log_mel_spectrogram failed: zero-size array to reduction operation maximum which has no identity
```

The error message incorrectly blames `log_mel_spectrogram` rather than the empty input,
making this extremely hard to diagnose. The failure silently returns `None`, causing
every STT call to fall through to the `"ru"` placeholder — a language routing regression
with no visible error.

Confirmed with numpy:
```
numpy.max(numpy.abs(numpy.array([], dtype=numpy.float32)))
# ValueError: zero-size array to reduction operation maximum which has no identity
```

**Severity:** MEDIUM — triggered by a valid user settings change (setting preview to 0
to effectively disable), causes silent language routing degradation with misleading logs.

**Fix:** Clamp in `_get_preview_sec()`:
```python
return max(1.0, float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0)))
```
And/or add an early-return guard in `detect()` after the preview slice:
```python
if len(audio_preview) == 0:
    logger.warning("AudioLanguageID: preview_sec=%.2f yields empty audio → skip", preview_sec)
    return None
```

---

### F3 — MEDIUM: `mx.clear_cache()` (W1117) does not free the loaded model held in `_model_cache`

**Location:** `audio_lang_id.py:220-236` (`_detect_with_mlx` model cache);
W1117 branch `_run_detect` finally block

**Description:**

W1117 adds `mx.clear_cache()` in a `finally` block after each inference to release MLX
Metal buffers. However, the loaded model object is held by
`AudioLanguageID._model_cache[model_path]` — a class-level Python dict. As long as this
reference exists, the MLX tensor objects inside the model (weights, buffers) are
**reachable** Python objects and will NOT be freed by `mx.clear_cache()`.

`mx.clear_cache()` only frees allocations that are not referenced by any live Python
object. The cached model weights — which are the dominant memory consumers (~300-500 MB
for `whisper-large-v3-turbo`) — remain live.

The practical consequence:
1. Wave 63's `mx.clear_cache()` fix (PR #405, already merged to codex) was designed for
   the STT inference path where the model is loaded fresh each call. The LID model is
   permanently cached in `_model_cache`, so the merged Wave 63 fix provides zero memory
   benefit for the LID path.
2. After W1117 lands, its `clear_cache()` call also provides zero memory benefit for the
   LID model memory — only the intermediate mel-spectrogram and logits buffers are freed,
   not the model weights.

This is independent of whether W1271 (which evicts `_model_cache` on settings change)
is merged: W1271 eviction does free the model reference, but only on profile switch.
During normal operation (no profile switch), the LID model occupies 300-500 MB
permanently.

**Severity:** MEDIUM — the existing `_model_cache` design was intentional for LID
performance, but the interaction with `mx.clear_cache()` is poorly documented and
creates false confidence that W1117 reduces LID memory footprint.

**Fix:** Document in `_detect_with_mlx` that `mx.clear_cache()` does not release
the cached model. If memory pressure is a concern, add a `TTL_SEC` for the model
entry (e.g., evict if unused for >5 minutes) so `clear_cache()` can actually free it.

---

### F4 — LOW: `STTRouter._lang_id` holds instance with explicit `model_path` immune to `clear_model_cache`

**Location:** `core/stt_router.py:458-467` (`_get_lang_id`),
`audio_lang_id.py:46-51` (`__init__`), W1271 `clear_model_cache()`

**Description:**

`STTRouter._get_lang_id()` always constructs `AudioLanguageID()` with no arguments
(line 463: `self._lang_id = AudioLanguageID()`), so `_model_path=None` and
`_get_model_path()` dynamically reads `settings.MODEL_BALANCED`. This path is safe.

However, the public `AudioLanguageID` constructor accepts `model_path: Optional[str]`.
Any caller that passes an explicit `model_path`:

```python
lid = AudioLanguageID(model_path="mlx-community/whisper-small-mlx")
```

will have that `model_path` hard-coded in the instance. After W1271's
`clear_model_cache()` evicts `_model_cache`, the next `detect()` call on this instance
will still use the old explicit path — `_get_model_path()` returns `self._model_path`
immediately at line 142 without consulting `settings.MODEL_BALANCED`.

The `clear_model_cache()` docstring does not mention this limitation. Existing tests
(in unmerged W1271 test suite) use `model_path=None` instances exclusively and
therefore do not exercise this case.

In practice, the STTRouter always uses `model_path=None`, so this is not a production
bug today. But as `AudioLanguageID` is a public API, third-party callers or future
extracted services could pass explicit paths and be silently immune to the eviction
mechanism.

**Severity:** LOW — not a current production bug; limitation of public API not
documented.

**Fix:** Document in `clear_model_cache()` docstring: "Note: instances constructed
with an explicit `model_path` kwarg are unaffected — cache eviction only triggers
a fresh load for `model_path=None` instances." Consider adding an `instance_path`
warning log in `clear_model_cache()` if any cached model_path does not match
`settings.MODEL_BALANCED`.

---

### F5 — INFO: Zero-preview and preset-bypass paths have no test coverage

**Location:** `KrabEar/tests/test_audio_lang_id.py`,
`KrabEar/tests/test_audio_lang_id_cache_limit.py`

**Description:**

Two test gaps introduced or exposed by W1300 findings:

**Gap A** (F2): No test for `detect()` when `stt_audio_lang_id_preview_sec = 0` in
settings. The empty `audio_preview` + masked ValueError path is entirely untested.
Adding a test would assert `detect()` returns `None` without raising and logs a
diagnostic (once F2 fix is applied).

**Gap B** (F1): No integration test for `handle_apply_profile_preset` +
`clear_model_cache` wiring. The existing W1271 tests verify the hook fires from
`handle_set_settings` but do not test that `apply_profile_preset` triggers eviction.
Without this test, the F1 regression is invisible in CI.

The current test files (`test_audio_lang_id.py`, `test_audio_lang_id_cache_limit.py`)
contain 57 test methods (as of this audit pass) but cover none of these paths.

**Severity:** INFO — test gaps do not affect production behavior today but would
prevent F1/F2 regressions from being caught in CI.

**Fix:** Add to `test_audio_lang_id.py`:
```python
def test_zero_preview_sec_returns_none_gracefully(self):
    """F2: preview_sec=0 must not raise and must return None."""
    with patch.object(AudioLanguageID, "_get_preview_sec", return_value=0.0):
        lid = AudioLanguageID()
        result = lid.detect(np.ones(16000, dtype=np.float32), sample_rate=16000)
        self.assertIsNone(result)
```

---

## W1019 Interaction (text-side, unchanged)

W1019 (`fix-language-detector-FR-TR-W1019`) remains unmerged. W1265 analysis
concluded there is no direct joint-failure mode between `AudioLanguageID` (audio-side)
and `LanguageDetector` (text-side). This assessment holds for W1300 — no new
interaction identified.

---

## Summary Table

| ID | Severity | Description | Root file |
|----|----------|-------------|-----------|
| F1 | **HIGH** | W1271 hook wired only to `handle_set_settings`; 4 other save paths bypass hooks | `settings_service.py` |
| F2 | **MEDIUM** | `preview_sec=0` → empty audio_preview → masked `ValueError` with wrong log | `audio_lang_id.py:243` |
| F3 | **MEDIUM** | `mx.clear_cache()` (W1117) cannot free model held in `_model_cache` | `audio_lang_id.py:220-236` |
| F4 | LOW | `clear_model_cache()` does not affect instances with explicit `model_path` | `audio_lang_id.py:140-149` |
| F5 | INFO | No tests for zero-preview path or preset-bypass + eviction wiring | `tests/test_audio_lang_id.py` |

**Net finding count:** 5 new findings (2 HIGH+MEDIUM each, 1 LOW, 1 INFO).
All 7 prior-wave PRs remain unmerged as of 2026-05-27.
