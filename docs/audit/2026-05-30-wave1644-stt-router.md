# Wave 1644 — First-pass Audit: `core/stt_router.py` (STTRouter)

**Date:** 2026-05-30
**Scope:** `KrabEar/core/stt_router.py` (577 LOC) + callers in `core/engine.py`,
`backend/service.py`, `backend/stt_management_service.py`, and
`KrabEar/tests/test_stt_router.py` (596 LOC).
**Status:** 7 findings (2 HIGH, 3 MED, 2 LOW)

---

## Context

`core/stt_router.py` contains the *language-model-selection* router (`STTRouter`
with `select_model` + `score_adapter`/`select_adapter_scored`).  It is distinct
from `core/pipeline/stt_router.py` (see F1 below).  `engine.py` imports the
*top-level* `core.stt_router.STTRouter` at line 412; `backend/service.py` and
`backend/stt_management_service.py` import its free functions `score_adapters` +
`select_adapter_scored`.  Routing is *disabled by default*
(`STT_LANGUAGE_ROUTING_ENABLED=False`).

---

## Findings

### F1 — HIGH: Two incompatible `STTRouter` classes diverged from a single concept

**Files:** `KrabEar/core/stt_router.py` (577 LOC) and
`KrabEar/core/pipeline/stt_router.py` (58 LOC) + its factory
`KrabEar/core/pipeline/stt_router_factory.py`.

Two independent `STTRouter` classes exist under different namespaces:

| | `core.stt_router.STTRouter` | `core.pipeline.stt_router.STTRouter` |
|---|---|---|
| **Used by** | `engine.py`, `service.py`, `stt_management_service.py` | `stt_router_factory.build_router()`, `test_parakeet_mlx_adapter.py`, `test_stt_adapter_migration.py` |
| **API** | `select_model(audio, sr, hint)` → model-id string | `select_adapter(lang, prefer_speed)` → adapter object |
| **Scoring** | `score_adapter()` function, explicit weights | `candidates[0]` — first available, no weighted scoring |
| **GigaAM** | Has `get_gigaam_adapter()` / `warmup_gigaam()` | Not present |
| **`stt_routing` setting** | Documented (`"auto_scored"` / `"legacy"`) but **never read** | Not used |

The `core/pipeline/stt_router.py` version is a stub: its `select_adapter` method
comment says "placeholder — extend later" and returns `candidates[0]` with no
scoring.  The factory (`build_router`) uses the pipeline router but the engine
and all production paths use the top-level router.  The two code paths are never
unified and the more capable top-level router is **unused for adapter selection**
— it only supplies `get_gigaam_adapter()` to `engine.py`.

**Risk:** Future contributors adding a new adapter using `stt_router_factory`
will bypass all scoring logic silently.  Tests in `test_stt_adapter_migration.py`
exercise `build_router`, giving false coverage confidence.

---

### F2 — HIGH: `stt_routing` setting ("auto_scored" / "legacy") is never read — scored selection is always used when enabled

**File:** `KrabEar/core/stt_router.py` line 31–34 (module docstring) vs actual
`select_adapter_scored` / `score_adapters` call-sites.

The module docstring states routing is "Controlled by `stt_routing` setting:
`auto_scored` → scored selection (default), `legacy` → adapter order from
engine."  No code in `STTRouter.select_model`, `score_adapters`, or
`select_adapter_scored` reads `self._settings.stt_routing` or any equivalent.
The `get_stt_routing_decision` IPC handler and `STTManagementService` both call
`select_adapter_scored` unconditionally.

**Risk:** The documented "legacy" escape hatch does not exist.  Operators who set
`stt_routing=legacy` expecting to restore ordered-chain behaviour get no effect
and no warning.  If the setting was intended for gradual rollout, it has never
been implemented.

---

### F3 — MED: `_resolve_language` uses hardcoded "ru" fallback (W1265 F3 not yet fixed)

**File:** `KrabEar/core/stt_router.py` lines 502–522.

When audio is shorter than 1 s (`_AUDIO_LID_MIN_SEC = 1.0 s`), the router
returns the hardcoded placeholder `"ru"` (line 509).  The same placeholder is
returned when `STT_AUDIO_LANG_ID_ENABLED=False` (line 537) or when LID returns
`None` (line 522).  The docstring at the top of `stt_router.py` explicitly
documents this as a design decision: "graceful fallback placeholder 'ru' (primary
user language, 80%+ RU)".

**Issue:** For non-RU users (or EN/ES dictation shorter than 1 s), this
systematically routes to the RU specialist model.  The W1265 F3 recommendation
was to use `"und"` (undetermined) → generalist model instead, which is also the
behavior documented for `audio_data=None` (line 497).  The `"und"` path already
exists for the silence case (line 535) but not for the "audio too short" or "LID
disabled" cases.

**Fix:** Replace `return "ru"` at lines 509 and 537 with `return "und"`.  The
`_lang_to_model` method already handles `"und"` by returning `STT_OTHER_PRIMARY_MODEL`
(line 564, via `_LANG_TO_CONFIG_ATTR.get("und")` → None → generalist).

---

### F4 — MED: Parakeet EN-only gate missing from `engine.py` chain-building

**File:** `KrabEar/core/engine.py` lines 1736–1740.

The Parakeet adapter is inserted into the candidate chain whenever
`settings.PARAKEET_ENABLED=True`, regardless of detected language.  Parakeet-TDT
is EN-only; its `_transcribe_parakeet` docstring (line 2199) states "only 'en' is
supported; other languages produce lower quality."  `score_adapter` in
`core/stt_router.py` correctly gates Parakeet to `{"en"}` (line 156), but the
`engine.py` chain-building code at lines 1736–1740 has no language gate —
Parakeet is tried for RU and ES audio, wasting latency before the whisper
fallback.

Note: the `_build_virtual_adapters_for_routing` in `service.py` does correctly
mark parakeet as `{"en"}` for IPC scoring (line 2555), but the live production
path in `engine.py` does not.

**Fix:** Add `and (_effective_lang is None or _effective_lang == "en")` to the
Parakeet gate at line 1736.

---

### F5 — MED: `AudioLanguageID` created without `restrict_to_supported=True` — unsupported language codes can reach `_lang_to_model`

**File:** `KrabEar/core/stt_router.py` line 471.

```python
self._lang_id = AudioLanguageID()
```

`AudioLanguageID` has a `restrict_to_supported` flag (line 79 of
`core/audio_lang_id.py`).  When `False` (the default), codes outside
`SUPPORTED_LANGUAGES = {"ru", "uk", "en", "es"}` are returned as-is with a
WARNING.  The comment at line 46–47 states: "Codes outside this set emit a
WARNING and are returned as-is (STTRouter decides)".

`STTRouter._resolve_language` receives these unsupported codes and passes them to
`_lang_to_model`, which maps unknown codes to `STT_OTHER_PRIMARY_MODEL` (the
generalist) — that is correct fallback behaviour.  However:
- No warning is re-emitted at the router level, so the unsupported LID result is
  silently absorbed.
- If `restrict_to_supported=True` were set, `_try_audio_lid` would receive `None`
  instead, and the code would fall through to the "LID returned None → placeholder
  'ru'" path (line 522), which is wrong (F3).

The current behaviour is acceptable but the contract between `AudioLanguageID` and
`STTRouter` is implicit.  The router should be constructed with
`restrict_to_supported=False` (explicit) and should log the unsupported code at
DEBUG level so it is visible in diagnostics.

---

### F6 — LOW: `_gigaam_adapter` singleton has no thread lock — concurrent `get_gigaam_adapter()` calls can double-instantiate

**File:** `KrabEar/core/stt_router.py` lines 396–435.

The cached GigaAM adapter pattern (lines 397–435) checks
`self._gigaam_adapter is not None` and sets `self._gigaam_adapter = adapter` with
no lock.  `engine.py` launches GigaAM warmup in a background thread (line 436)
while the main thread may also call `get_gigaam_adapter()` on the first real
transcription (line 1724).  The race window is small but produces the exact
double-subprocess-spawn bug that the singleton was introduced to prevent.

`test_concurrent_route.py` (line 527) tests concurrent `select_model` calls but
not concurrent `get_gigaam_adapter()` calls.

**Fix:** Add a `threading.Lock` in `__init__` and guard the lazy-init block.

---

### F7 — LOW: `warmup_gigaam` calls `adapter.transcribe()` without `mlx_lock` guard

**File:** `KrabEar/core/stt_router.py` lines 452–460.

```python
dummy = np.zeros(16000, dtype=np.float32)
adapter.transcribe(dummy, sample_rate=16000)
```

GigaAM uses PyTorch+MPS (not MLX), so `mlx_lock` is not required here —
PyTorch+MPS adapters are exempt per CLAUDE.md.  However, if the GigaAM adapter
is ever swapped for an MLX-based model, this call-site would silently become
unsafe.  A comment explaining the exemption would prevent future regressions.

---

## Summary table

| # | Severity | File | Line(s) | Issue |
|---|----------|------|---------|-------|
| F1 | HIGH | `core/stt_router.py` + `core/pipeline/stt_router.py` | — | Two incompatible STTRouter classes; pipeline version is a dead stub |
| F2 | HIGH | `core/stt_router.py` | docstring L31–34 | `stt_routing` "legacy" setting documented but never implemented |
| F3 | MED | `core/stt_router.py` | 509, 537 | Hardcoded "ru" fallback for short/disabled LID; should be "und" |
| F4 | MED | `core/engine.py` | 1736 | Parakeet EN-only gate missing in chain-building |
| F5 | MED | `core/stt_router.py` | 471 | `AudioLanguageID` created without explicit `restrict_to_supported`; contract implicit |
| F6 | LOW | `core/stt_router.py` | 396–435 | `_gigaam_adapter` singleton has no lock — double-spawn race on concurrent `get_gigaam_adapter()` |
| F7 | LOW | `core/stt_router.py` | 452–460 | `warmup_gigaam` missing comment explaining PyTorch+MPS MLX-lock exemption |

---

## Routing mode status (production)

- `STT_LANGUAGE_ROUTING_ENABLED=False` (config default) → entire scoring path is
  bypassed; all calls return `STT_OTHER_PRIMARY_MODEL` (`whisper-large-v3-mlx`).
- `STT_GIGAAM_ENABLED=False`, `STT_PARAKEET_ENABLED=False` (defaults) → GigaAM
  and Parakeet chain entries never added.
- `select_adapter_scored` is exposed via `get_stt_routing_decision` IPC for
  diagnostics even when routing is disabled.

---

## Test coverage assessment

`test_stt_router.py` (596 LOC, 30+ cases) covers:
- disabled routing, hint language, UK→RU mapping, LID detection, fallback to "ru",
  silence audio, NaN/empty array, adapter_factory exception, concurrent calls (10
  threads on `select_model`), all-adapters-failed case.

Gaps:
- No test for `warmup_gigaam` background-thread race on `_gigaam_adapter`.
- No test for `_resolve_language` with < 1 s audio that exercises the "ru"
  placeholder path in isolation (only tested via `test_audio_lid_detection_*`
  which use long audio).
- `stt_routing` setting "legacy" path is untested (F2 — it doesn't exist).
- No integration test verifying that `core/pipeline/stt_router.py` factory is
  exercised by any real transcription path (F1 — it isn't).
