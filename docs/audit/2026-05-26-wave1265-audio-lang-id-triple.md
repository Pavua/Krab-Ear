# W1265 Third-pass Re-audit: `core/audio_lang_id.py`

**Date:** 2026-05-26
**Auditor:** W1265 sub-agent (third-pass)
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)
**File:** `KrabEar/core/audio_lang_id.py`

---

## Previous Fix Merge State

All four PRs from prior audit rounds are **still OPEN** — none merged into `codex/krab-ear-v2`:

| Wave | PR   | Branch                            | Description                          | Status |
|------|------|-----------------------------------|--------------------------------------|--------|
| W1090 | #1004 | `fix-audio-lang-id-W1090`        | Zero-peak short-circuit + MIN_CONFIDENCE gate | **OPEN** |
| W1116 | #1031 | `fix-audio-lang-id-lock-W1116`   | `_model_cache` RLock (thread-safety) | **OPEN** |
| W1117 | #1035 | `fix-audio-lang-id-mx-clear-W1117` | `mx.clear_cache()` after inference  | **OPEN** |
| W1121 | #1033 | `fix-audio-lang-id-allowlist-W1121` | SUPPORTED_LANGUAGES allowlist      | **OPEN** |

Verification commands used:
```bash
grep "_ZERO_PEAK_THRESHOLD\|MIN_CONFIDENCE\|_model_cache_lock\|SUPPORTED_LANGUAGES" KrabEar/core/audio_lang_id.py
# → no output (all 4 fixes absent from current baseline)
```

The W1019 language_detector fix (PR: separate branch, FR/TR/PT exclusion from ES
classification) is also **not merged**, confirmed by `grep "_FR_MARKERS\|_TR_MARKERS"
KrabEar/core/language_detector.py` returning no output.

---

## NEW Findings (5)

The following findings are **distinct from W1090/W1116/W1117/W1121** and exist in the
current `codex/krab-ear-v2` baseline.

---

### F1 — MEDIUM: No `_model_cache` invalidation when `MODEL_BALANCED` changes at runtime

**Location:** `audio_lang_id.py:140-148`, `audio_lang_id.py:220-227`; `stt_router.py:458-467`

**Description:**

`AudioLanguageID._get_model_path()` reads `settings.MODEL_BALANCED` at call time. The
`_model_cache` dict (class-level singleton) keyed on model path will automatically evict
the old entry when the path changes (via the len>=1 clear at line 222). However, there
is no mechanism to propagate a `MODEL_BALANCED` change from `set_settings` IPC through
to the live `STTRouter._lang_id` singleton, which holds the already-constructed
`AudioLanguageID()` instance.

Sequence of events:
1. User calls `set_settings({"model_balanced": "mlx-community/whisper-small-mlx"})` →
   `settings.json` updated → `reload_settings_from_json()` hot-reloads the pydantic
   `Settings` singleton.
2. `STTRouter._lang_id` still holds the `AudioLanguageID(model_path=None)` instance
   created at first use. That instance calls `_get_model_path()` which reads the updated
   `settings.MODEL_BALANCED` on the next `detect()` call — so the path itself changes
   correctly.
3. The cache eviction works: on the next `detect()` call the new model path is not in
   `_model_cache`, the old entry is cleared, and the new model is loaded.

**However:** between step 1 and the next `detect()` call, if the model cache already
contains the old model, the next inference call will first need to load the new model,
incurring a cold-start latency of ~1–3 s. More importantly, if a `detect()` is in
progress when `set_settings` fires, there is a brief window where `_get_model_path()`
returns the new path but the cached model is for the old path — the path mismatch causes
immediate eviction and reload inside the same `_detect_with_mlx` call while `mlx_lock`
is held, blocking the STT thread for the full model-load duration.

Additionally, there is **no after_save_hook** in `service.py` that resets
`STTRouter._lang_id = None` when `model_balanced` changes, unlike the LLMRewriter
`api_key` hook at line 212–216. If a future bugfix tries to pre-warm the new model on
settings change, there is no hook point.

**Severity:** MEDIUM — primarily a latency spike on model switch, not correctness.

**Fix:** Register an `after_save_hook` in `BackendService.__init__` that checks if
`new["model_balanced"] != old.get("model_balanced")` and clears
`AudioLanguageID._model_cache` (or `stt_router._lang_id = None`). Also add a
`clear_model_cache()` classmethod to `AudioLanguageID`.

---

### F2 — MEDIUM: Confidence value from `detect_language` tuple branch is never extracted or used

**Location:** `audio_lang_id.py:264-267`

**Description:**

When `mlx_whisper.decoding.detect_language()` returns a `(lang_code, probs_dict)` tuple,
the current code only extracts the language string:

```python
if isinstance(result, tuple):
    # (language_str, probs_dict)
    lang_code = result[0]
```

The `probs_dict` at `result[1]` contains per-language probabilities. The winning
language's confidence is `probs_dict[lang_code]` (a float in `[0, 1]`). This value is
silently discarded. Combined with the fact that W1090 (the MIN_CONFIDENCE gate) is
not yet merged, there is **no quality guard**: a language detection with 12% confidence
(e.g., short or noisy audio with ambiguous spectrogram) silently propagates to
`STTRouter._lang_to_model()` and selects a language-specific model, potentially
introducing worse transcription quality than the generalist fallback model would provide.

Even after W1090 is merged, the existing tuple-path code in the current baseline has
this latent issue: when W1090 adds `MIN_CONFIDENCE`, it will correctly extract
confidence — but only if the extraction logic is wired for all result types. The current
baseline has NO confidence extraction for ANY result type, meaning the W1090 PR's
confidence logic must add extraction from scratch.

**Severity:** MEDIUM — silent quality degradation for ambiguous audio, not a crash.

**Fix:** In `_detect_with_mlx`, when `result` is a tuple, extract `probs_dict =
result[1]` if it is a dict, then compute `confidence = probs_dict.get(lang_code, 0.0)`
before returning. Log confidence at DEBUG level. This work partially overlaps with
W1090 but must be done in coordination with W1090's confidence gate.

---

### F3 — LOW: `_resolve_language` in `STTRouter` hard-codes placeholder `"ru"` when LID returns None — always routes to RU model regardless of true audio language

**Location:** `stt_router.py:505-513`

**Description:**

When `AudioLanguageID.detect()` returns `None` (inference error, timeout, silent audio,
or mlx_whisper not installed), `_resolve_language` falls back to the hard-coded
placeholder `"ru"`:

```python
if detected is not None:
    logger.debug("STTRouter: audio LID detected → %s", detected)
    return detected
# LID вернул None → fallback на placeholder
logger.debug("STTRouter: audio LID returned None → placeholder 'ru'")
return "ru"
```

This means: any audio where LID fails (including a production environment where
mlx-whisper is temporarily unavailable due to OOM or model download failure) will
always be routed to `STT_RU_PRIMARY_MODEL`. For a user dictating in English or Spanish
with LID disabled (or failing), this silently selects the wrong model.

The correct fallback when LID intent is "language unknown" should be `"und"` →
`STT_OTHER_PRIMARY_MODEL` (the generalist multilingual model). The `"ru"` placeholder
was appropriate when LID didn't exist, but now that LID is the active detection path,
an LID failure should not be treated as a Russian-language session.

Note: the comment at `stt_router.py:480` says "Аудио слишком короткое (< 1с) →
placeholder 'ru'" which is a user-experience choice (short audio in this project is
usually a command in Russian), but the LID-None case at line 512 is different: it
represents a detection failure, not a user-intent signal.

**Severity:** LOW — affects users who dictate in EN/ES when LID fails or is unavailable.
Workaround: set `hint_language` or `STT_LANGUAGE_ROUTING_ENABLED=False`.

**Fix:** Change the fallback in the LID-None branch from `"ru"` to `"und"`. Update the
short-audio fallback comment to clarify it is intentional (RU is default language for
this app's primary use case).

---

### F4 — LOW: `AudioLanguageID` does not interact with `CodeSwitchingDetector` (W1074) — conflicting signals possible

**Location:** `audio_lang_id.py` vs `transcript_context.py:172-182`

**Description:**

`AudioLanguageID` performs audio-level language identification (before STT). The
`CodeSwitchingDetector` in `transcript_context.py` analyzes the *previous* transcription
text for RU+EN mixing and injects a Whisper `initial_prompt` hint. These two systems
provide orthogonal signals:

- Audio-LID says: "this audio is probably EN" → `STTRouter` selects EN model.
- CodeSwitching says: "last transcript had 40% Latin script" → Whisper gets hint to
  expect code-mixed RU/EN text.

These signals can contradict each other: Audio-LID routes to EN model (which does not
inject language-specific hints), but the `initial_prompt` still suggests bilingual text,
potentially confusing the EN-primary model.

More concretely: in `audio_lang_id.py` the W1121 fix (not yet merged) will add
`SUPPORTED_LANGUAGES = {"ru", "uk", "en", "es"}`. When audio is detected as "en" but
the user is actually doing code-switched RU/EN speech, the code-switching hint in
`initial_prompt` may be the more accurate signal. There is no arbitration logic between
the two.

This is an **architectural gap** rather than a bug: neither system is wrong in
isolation, but they are developed independently with no documented resolution order. The
gap was pre-existing but becomes more visible as both systems mature.

**Severity:** LOW — quality/precision issue, not a crash or data loss.

**Recommendation:** Document the intended priority order (audio-LID overrides
code-switching hint vs. both applied in sequence) in a comment in `stt_router.py` and
`transcript_context.py`. Consider passing the audio-LID result as `hint_language` to
`build_initial_prompt` so the code-switching hint is skipped when LID is confident.

---

### F5 — INFO: No test coverage for `_get_model_path()` when `model_path=None` and `MODEL_BALANCED` absent from settings

**Location:** `audio_lang_id.py:140-149`, `KrabEar/tests/test_audio_lang_id.py`

**Description:**

`_get_model_path()` has a three-level fallback:

```python
def _get_model_path(self) -> str:
    if self._model_path is not None:
        return self._model_path
    try:
        from core.config import settings
        return getattr(settings, "MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")
    except Exception:
        return "mlx-community/whisper-large-v3-turbo"
```

The existing 33 test cases in `test_audio_lang_id.py` (29) and
`test_audio_lang_id_cache_limit.py` (4) do not test:

1. The `except Exception` path when `core.config` import fails.
2. The `getattr(settings, "MODEL_BALANCED", ...)` default when the attribute is absent
   from the settings object (e.g., a stripped-down mock settings with only IPC keys).
3. The case where `model_path=None` and the settings singleton returns a custom non-default
   model path (verifying the settings read actually happens).

The W1090/W1116/W1117/W1121 fix branches each add new test files
(`test_audio_lang_id_threadsafe_W1116.py`, `test_audio_lang_id_allowlist_W1121.py`)
without covering these fallback paths, so the gap persists post-merge.

**Severity:** INFO — test coverage gap, no production risk.

**Fix:** Add 3 test cases in `test_audio_lang_id.py`: (a) mock `core.config` import
failure, (b) mock settings without `MODEL_BALANCED` attribute, (c) verify custom
`model_path` kwarg propagates through `_get_model_path()`.

---

## Interaction Matrix: W1019 vs AudioLanguageID

W1019 (language_detector FR/TR/PT exclusion fix) is also **not merged** and operates on
the **text side** (post-STT). There is no direct interaction:

- `AudioLanguageID` operates on **audio** before STT.
- `LanguageDetector` (`core/language_detector.py`) operates on **transcribed text** for
  `translate_selection` IPC routing.
- The two systems use independent logic and independent caller chains.
- The only shared concern is that both produce ISO 639-1 language codes with possible
  "und" (undetermined) outputs that downstream callers must handle.

No new joint-failure mode identified between W1019 and the AudioLanguageID system.

---

## Summary Table

| ID | Severity | Description | PRs needed |
|----|----------|-------------|------------|
| F1 | MEDIUM | No `_model_cache` eviction hook when `MODEL_BALANCED` changes | New (after_save_hook) |
| F2 | MEDIUM | Confidence value from tuple branch silently discarded — no quality gate | Overlaps W1090 |
| F3 | LOW | STTRouter fallback on LID-None is `"ru"` (wrong model) instead of `"und"` (generalist) | New fix in stt_router.py |
| F4 | LOW | No arbitration between AudioLanguageID and CodeSwitchingDetector signals | Architecture doc + optional fix |
| F5 | INFO | Missing tests for `_get_model_path()` fallback paths | New tests |

**Net finding count:** 5 new findings (2 MEDIUM, 2 LOW, 1 INFO).
All 4 prior-wave PRs (#1004, #1031, #1033, #1035) remain unmerged as of 2026-05-26.
