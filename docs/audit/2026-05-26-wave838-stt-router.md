# Wave 838 Audit: `core/stt_router.py` — STT Router Deep Dive

**Date**: 2026-05-26  
**Wave**: 838  
**Auditor**: Claude (sub-agent)  
**Files audited**:
- `KrabEar/core/stt_router.py` (577 lines)
- `KrabEar/core/audio_lang_id.py` (286 lines)
- `KrabEar/core/pipeline/stt_router.py` (57 lines)
- `KrabEar/core/pipeline/stt_router_factory.py` (125 lines)
- `KrabEar/backend/stt_management_service.py` (partial — routing methods)
- `KrabEar/core/config.py` (routing-related settings)
- `KrabEar/core/engine.py` (router usage sites)

---

## Summary

`core/stt_router.py` is a well-structured, defensively coded module. The scoring formula is
correct for its described intent, MLX lock usage is properly absent from GigaAM/PyTorch paths,
and AudioLanguageID integration is correctly wrapped in `mlx_lock()`. However, **7 findings**
range from a dormant-by-default routing path that never activates in production to a naming
inconsistency that silently causes the IPC debug endpoint to misreport adapter availability.

---

## Architecture overview

There are **two separate STTRouter classes** in the codebase:

| Class | Location | Used in production? |
|---|---|---|
| `STTRouter` (language/model mapper) | `core/stt_router.py` | Yes — `engine.py` instantiates it, but only calls `get_gigaam_adapter()` and `warmup_gigaam()` |
| `STTRouter` (adapter-based pipeline) | `core/pipeline/stt_router.py` | No — only used by `build_router()` factory; not wired into engine.py |

The `select_model()` method and the entire scored-selection path (`select_adapter_scored()`) are
present in `core/stt_router.py` but **never called by `engine.py`** in the production transcription
path. Engine uses the router exclusively as a GigaAM adapter cache.

---

## Scoring formula (D.2.3)

Formula as implemented:

```
score = match_score + speed_bonus + quality_bonus + duration_penalty
```

| Component | Value |
|---|---|
| Exact language match | +100 |
| Multilingual (empty `supported_languages`) | +60 |
| Language not supported | 0 (early return — adapter excluded) |
| Speed: gigaam / parakeet | +20 |
| Speed: sensevoice | +10 |
| Speed: other | +0 |
| Quality: whisper (any name containing "whisper") | +15 |
| Quality: gigaam | +10 |
| Quality: parakeet | +10 |
| Duration penalty: gigaam AND audio > 30 s | -50 |

**Formula is internally consistent.** Example scenarios:

- RU audio, 20 s: GigaAM = 100+20+10+0 = **130**, Whisper = 60+0+15+0 = **75** → GigaAM wins. Correct.
- RU audio, 31 s: GigaAM = 100+20+10-50 = **80**, Whisper = **75** → GigaAM still wins (80 > 75).
  This is intentional — chunked longform is slower but GigaAM still preferred for RU accuracy.
- EN audio: Parakeet = 100+20+10 = **130**, Whisper = **75** → Parakeet wins. Correct.
- ZH audio: SenseVoice = 100+10+0 = **110**, Whisper = **75** → SenseVoice wins. Correct.
- "und" language: GigaAM score=0 (not in `supported_languages`); Whisper=**60** → Whisper. Correct.

**No mathematical correctness bug found.**

---

## Findings

### F1: `STT_LANGUAGE_ROUTING_ENABLED` defaults to `False` — `select_model()` is inert in production

**Severity**: MEDIUM (silent feature gap, not a crash)  
**Location**: `core/stt_router.py:331`, `core/config.py:509`

`STTRouter.select_model()` short-circuits immediately when `STT_LANGUAGE_ROUTING_ENABLED=False`
(the default) and returns `STT_OTHER_PRIMARY_MODEL` unconditionally. Since `engine.py` never calls
`select_model()` at all, the entire language→model routing path (including the `_lang_to_model`
mapping and AudioLanguageID integration) has **never been active in production**.

The comment in `config.py` acknowledges this ("Интеграция в engine.py — в follow-up PR"), but
the flag is also not documented in the user manual or IPC reference, leaving no clear activation
path for operators.

**Recommendation**: Either wire `select_model()` into `engine.py`'s transcription path or add a
`docs/` note explaining the intended activation workflow and the remaining steps.

---

### F2: `select_adapter_scored()` is only reachable via a debug IPC endpoint, not actual transcription

**Severity**: MEDIUM (architectural dead weight)  
**Location**: `core/stt_router.py:202-249`, `backend/stt_management_service.py:166-195`

The scored selection path (`select_adapter_scored`, `score_adapter`, `score_adapters`) is only
called from `handle_get_stt_routing_decision` — a debug-only IPC method. The production
transcription path in `engine.py` uses a hardcoded fallback chain (`balanced → max → remote`)
that completely bypasses the scoring logic.

The `STT_ROUTING` config setting (`"auto_scored"` | `"legacy"`, default `"auto_scored"`) is
defined in config but **read by nothing** — no code branches on its value. The docstring in
`stt_router.py` describes this setting as if it controls runtime behaviour, but that control
logic was never implemented.

**Recommendation**: Either implement the `STT_ROUTING` branch in the engine's fallback chain, or
mark the setting as `# reserved / not yet implemented` to avoid misleading future contributors.

---

### F3: Duplicate `STTRouter` class names across two modules

**Severity**: LOW (no runtime crash but import confusion risk)  
**Location**: `core/stt_router.py:274`, `core/pipeline/stt_router.py:12`

Two unrelated classes share the name `STTRouter`:
- `core.stt_router.STTRouter` — language-to-model mapper + GigaAM adapter cache
- `core.pipeline.stt_router.STTRouter` — adapter-dispatch router (takes `list[STTAdapterBase]`)

They have different constructors, different methods (`select_model` vs `select_adapter`), and
different purposes. A developer importing `from core.stt_router import STTRouter` vs
`from core.pipeline.stt_router import STTRouter` gets entirely different objects with no type
error. The `stt_router_factory.py` uses the pipeline variant; `engine.py` uses the core variant.

**Recommendation**: Rename one class — e.g. `core/pipeline/stt_router.py` → `AdapterDispatchRouter`
or `PipelineSTTRouter` — to eliminate ambiguity.

---

### F4: Config key mismatch — `PARAKEET_ENABLED` vs `STT_PARAKEET_ENABLED` causes silent routing error

**Severity**: MEDIUM (silent wrong availability report in IPC debug)  
**Location**: `backend/stt_management_service.py:221`, `core/config.py:268+362`

`config.py` defines **two separate settings** for Parakeet (and two for SenseVoice):
- `PARAKEET_ENABLED: bool = False` — used by `engine.py` fallback chain
- `STT_PARAKEET_ENABLED: bool = False` — intended for the router path (per config comment)

`stt_management_service._build_virtual_adapters_for_routing()` reads `PARAKEET_ENABLED` (line 221)
but the router path comment in `config.py:360` says to use `STT_PARAKEET_ENABLED`. The same
mismatch applies for `SENSEVOICE_ENABLED` vs `STT_SENSEVOICE_ENABLED`.

This means the `get_stt_routing_decision` IPC debug endpoint reflects engine-level availability
(`PARAKEET_ENABLED`) rather than the router-level flag (`STT_PARAKEET_ENABLED`). If a user enables
`STT_PARAKEET_ENABLED=True` expecting the router to activate Parakeet, the debug endpoint will
not reflect it. Both flags default to `False`, so the mismatch is currently masked.

**Recommendation**: Audit which setting is the canonical one for each adapter in the router path
and consolidate. Comment in config.py should indicate which setting gates which subsystem.

---

### F5: `warmup_gigaam()` has a redundant `import numpy as np` at module level and inside the method

**Severity**: LOW (style / minor redundancy)  
**Location**: `core/stt_router.py:453`

`numpy` is already imported at the top of the module (`import numpy as np`, line 48). The
`warmup_gigaam()` method re-imports it inline (`import numpy as np` on line 453). The inline
import shadows the module-level binding needlessly.

**Recommendation**: Remove the inline `import numpy as np` in `warmup_gigaam()`.

---

### F6: `_language_detector` constructor parameter is a documented dead field

**Severity**: LOW (API noise)  
**Location**: `core/stt_router.py:289-293`

`STTRouter.__init__` accepts a `language_detector` parameter (documented as "Устаревший параметр")
and stores it in `self._language_detector`, but `_language_detector` is never read anywhere in the
class. The docstring says it was superseded by `AudioLanguageID`. The parameter remains as an
accepted kwarg but does nothing.

**Recommendation**: Remove the parameter and its docstring note to avoid misleading callers who
might expect text-based language detection to be wired.

---

### F7: `_resolve_language` near-silence branch only runs when `STT_AUDIO_LANG_ID_ENABLED=False`

**Severity**: LOW (minor logic asymmetry)  
**Location**: `core/stt_router.py:524-542`

When `STT_AUDIO_LANG_ID_ENABLED=True` and AudioLID returns `None` (e.g. on silence), the code
falls back to placeholder `"ru"` (line 522) without the silence check. The near-silence RMS guard
(`rms < 1e-6 → "und"`) only runs in the `STT_AUDIO_LANG_ID_ENABLED=False` branch. So with LID
enabled, silent audio → `"ru"` (rather than `"und"` → generalist fallback). The silence guard
effectively disappears for the common configuration.

This is a minor asymmetry — GigaAM routing for RU on silent audio would result in GigaAM being
called unnecessarily, though it will quickly return an empty transcript.

**Recommendation**: Move the silence check before the `lang_id_enabled` branch so it applies
regardless of the LID setting, and return `"und"` for near-silent audio in all cases.

---

## MLX lock usage — CORRECT

`AudioLanguageID._run_detect()` correctly wraps the mlx-whisper inference in `with mlx_lock():`
(line 203 of `audio_lang_id.py`). `STTRouter._try_audio_lid()` delegates entirely to
`AudioLanguageID.detect()` which carries the lock internally.

GigaAM uses PyTorch+MPS (subprocess or in-process), not MLX — confirmed in
`core/pipeline/stt_gigaam.py`: "PyTorch + MPS, не MLX → mlx_lock НЕ нужен". `warmup_gigaam()`
calls `adapter.transcribe()` without `mlx_lock` — correct.

The `core/stt_router.py` module itself imports `mlx_lock` indirectly through `AudioLanguageID`
and does not call it directly — appropriate given none of the router's own code calls MLX.

---

## Fallback chain correctness

`select_adapter_scored` initialises `best_score = 0` and only updates when `s > best_score`.
This means:
- Score-0 adapters (unsupported language or unavailable) are never selected.
- If all adapters score 0, `None` is returned (correct — caller must handle).
- Negative scores (GigaAM long-audio penalty: 80 → still positive for RU) cannot currently
  cause incorrect selection, but if a score ever went below zero it would also be correctly
  excluded (since `s > 0` required to beat `best_score=0`).

Tie-breaking favours the **first adapter in the list** (stable — docstring acknowledges this as
"backward-compat" behaviour). This is correct for the documented priority order.

---

## Test coverage assessment

Four test files cover routing:

| File | Scope |
|---|---|
| `test_stt_routing_scored.py` | Scored scoring + selection + IPC method |
| `test_stt_router.py` | `STTRouter.select_model()` + GigaAM adapter + `_resolve_language` |
| `test_stage_pipeline_stt_router.py` | Pipeline `STTRouter` (adapter-dispatch) |
| `test_stage_pipeline_stt_router_factory.py` | `build_router()` factory |

Coverage is thorough for the scoring formula (31 test methods in `test_stt_routing_scored.py`)
but does not include:
- The silence-check asymmetry (F7) — no test for near-silent audio with LID enabled returning `"und"`
- The `uk` language in `select_model()` resolving to `STT_RU_PRIMARY_MODEL` (only tested in scored tests via adapter `supported_languages`, not in `_lang_to_model`)

---

## Summary table

| # | Finding | Severity | Affected code |
|---|---|---|---|
| F1 | `STT_LANGUAGE_ROUTING_ENABLED=False` default — `select_model()` inert | MEDIUM | `core/stt_router.py:331`, `config.py:509` |
| F2 | `STT_ROUTING` setting read by nothing — scored selection only reachable via debug IPC | MEDIUM | `stt_router.py`, `config.py:513` |
| F3 | Two unrelated classes both named `STTRouter` | LOW | `core/stt_router.py:274`, `core/pipeline/stt_router.py:12` |
| F4 | `PARAKEET_ENABLED` vs `STT_PARAKEET_ENABLED` mismatch in routing debug endpoint | MEDIUM | `stt_management_service.py:221` |
| F5 | Redundant `import numpy as np` inside `warmup_gigaam()` | LOW | `stt_router.py:453` |
| F6 | `language_detector` constructor param stored but never read | LOW | `stt_router.py:289-293` |
| F7 | Near-silence `"und"` guard absent when `STT_AUDIO_LANG_ID_ENABLED=True` | LOW | `stt_router.py:514-522` |

**MLX lock usage**: CORRECT — no issues found.  
**Fallback chain logic**: CORRECT — score=0 adapters excluded; negative scores handled safely.  
**GigaAM adapter cache**: CORRECT — singleton reuse prevents subprocess spawn per call (Wave 525 fix).
