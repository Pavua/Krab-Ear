# W1263 — core/pipeline/ audit

**Date:** 2026-05-26  
**Branch:** audit/core-pipeline-W1263  
**Scope:** `KrabEar/core/pipeline/` — Phase 4 deterministic pipeline (6 stages, executor, cache, bridge)

---

## Inventory

```
core/pipeline/
  base.py               PipelineStage Protocol (runtime_checkable)
  context.py            PipelineContext dataclass (all inter-stage state)
  executor.py           PipelineExecutor — sequential runner + cache logic
  factory.py            create_default_pipeline() — assembles 6-stage chain
  bridge.py             transcribe_v2() — legacy-dict adapter for BackendService
  stage_cache.py        StageCache — LRU + TTL, thread-safe (OrderedDict)
  stt_adapter.py        STT adapter base
  stt_gigaam.py         GigaAM subprocess adapter
  stt_gigaam_adapter.py GigaAM direct adapter
  stt_parakeet.py       Parakeet adapter
  stt_router.py         STT router (language-aware)
  stt_router_factory.py build_router() factory
  stt_sensevoice.py     SenseVoice adapter
  stt_whisper_mlx_adapter.py  MLX-Whisper adapter
  stages/
    audio_normalization.py    Stage 1
    stt.py                    Stage 2
    diarization.py            Stage 3
    text_cleanup.py           Stage 4
    llm_rewrite.py            Stage 5
    translation.py            Stage 6
```

---

## Stage ordering

Factory assembles: AudioNorm(1) → STT(2) → Diarization(3) → TextCleanup(4) → LLMRewrite(5) → Translation(6).

The ordering is logically correct. Diarization (3) operates on the normalized audio path
(`ctx.normalized_audio`), not on text, so it does not need `cleaned_text` to be available
first. TextCleanup (4) and LLMRewrite (5) are text-only and correctly run after STT produces
`raw_text`. Translation (6) correctly reads `ctx.final_text`, which is set by LLMRewrite(5)
or falls back to `cleaned_text`/`raw_text`. No ordering violations found.

---

## Findings (6 of 6)

### F1 — MEDIUM | Pipeline dead-code: `PIPELINE_V2` flag is never checked in production

**File:** `KrabEar/core/config.py:221`, `KrabEar/core/pipeline/bridge.py`

`settings.PIPELINE_V2 = False` exists as a feature flag. `bridge.transcribe_v2()` is built to
be a drop-in for `engine.transcribe()`, but **neither `backend/service.py` nor `main.py`
imports or checks this flag**. Zero production call-sites for `transcribe_v2()` were found
across the entire backend. The pipeline is fully functional and tested but is never invoked
in production — `AudioEngine.transcribe()` is used directly instead. The flag toggle tests in
`test_pipeline_e2e.py` only verify that the env-var flips the Pydantic setting value; they do
not verify that any code path branches on it.

**Impact:** The entire Phase 4 pipeline (all six stages, the cache, the bridge) is latent
infrastructure. Bugs introduced in production `engine.py` code are not caught by pipeline tests.

**Fix:** Wire `transcribe_v2()` in `BackendService._handle_transcribe()` behind the flag, e.g.:
```python
if settings.PIPELINE_V2:
    from core.pipeline.bridge import transcribe_v2
    return transcribe_v2(self.engine, audio, ...)
```

---

### F2 — MEDIUM | `_stage_had_error` prefix mismatch causes stale cache writes after stage errors

**File:** `KrabEar/core/pipeline/executor.py:172-175`

`_stage_had_error(stage_name, ctx)` checks whether `ctx.errors` contains any entry starting
with `f"{stage_name}:"`. However three stages use non-matching prefixes:

| Stage name   | `_stage_had_error` looks for | Actual prefix written       | Match? |
|--------------|------------------------------|-----------------------------|--------|
| `text_cleanup` | `"text_cleanup:"`          | `"text_cleanup_error:"`     | NO     |
| `llm_rewrite`  | `"llm_rewrite:"`           | `"llm_rewrite_unexpected:"` | NO     |
| `translation`  | `"translation:"`           | `"translation_unexpected:"` / `"translation_failed:"` | NO |

Consequence: when `text_cleanup`, `llm_rewrite`, or `translation` fail with an error, the
executor still writes the (bad) result to the `StageCache`, because `_stage_had_error` returns
`False` (no matching prefix found). Subsequent calls with the same audio hash will serve
corrupted/partial cached results.

**Fix option A** — normalise error prefixes in all stages to `"<stage_name>: ..."`:
- `text_cleanup.py:39`: `"text_cleanup_error:"` → `"text_cleanup:"`
- `llm_rewrite.py:54`: `"llm_rewrite_unexpected:"` → `"llm_rewrite:"`
- `translation.py:57,69`: `"translation_unexpected:"` / `"translation_failed:"` → `"translation:"`

**Fix option B** — use `startswith(stage_name)` (matches any suffix):
```python
return any(e.startswith(stage_name) for e in ctx.errors)
```

---

### F3 — LOW | `StageCache` is never instantiated by `bridge.transcribe_v2()`

**File:** `KrabEar/core/pipeline/bridge.py:69`, `KrabEar/core/pipeline/factory.py:67`

`create_default_pipeline()` accepts an optional `cache: StageCache` parameter but the bridge
never passes one. `PipelineExecutor.__init__` stores `None` for `self._cache`, so the
`use_cache` gate is always `False`. The entire `StageCache` / LRU infrastructure (300 lines)
is a no-op in the only production entry point. The `cacheable = True` attribute on `STTStage`
has no runtime effect.

**Impact:** Low (since F1 means pipeline isn't in production), but if F1 is fixed the cache
benefit will still be absent without this fix.

**Fix:** Maintain a process-level `_shared_cache: StageCache` singleton and pass it through
`create_default_pipeline(cache=_shared_cache)` in the bridge.

---

### F4 — LOW | `AudioNormalizationStage` leaks the normalised WAV temp file when iCloud copy also creates a temp file

**File:** `KrabEar/core/pipeline/stages/audio_normalization.py:118-119`

`_normalize_file()` creates two temp files for iCloud-sourced audio:
1. An iCloud-copy temp (stored in `temp_copy`, cleaned in the `finally` block — OK).
2. A normalised output WAV (`out_tmp.name`).

The output WAV path is assigned to `ctx._temp_path` **only when `ctx._temp_path is None`**
(line 118). If a second `AudioNormalizationStage` call were to run on the same context (not
possible today but would become possible if stages are reused), the second temp file would
silently leak. More importantly, `PipelineExecutor._cleanup()` only deletes `ctx._temp_path`
(one path). If `normalized_audio` is reassigned (e.g. by a hypothetical retry), the previous
temp file is not tracked.

The code is safe in the current single-run model, but the `_temp_path: Optional[str]` field
design (scalar, not list) is fragile and undocumented as a single-slot constraint.

**Fix:** Change `ctx._temp_path` to `ctx._temp_paths: list[str]` and accumulate all temp
files; executor cleans the entire list.

---

### F5 — LOW | `LLMRewriteStage.process()` sets `ctx.final_text` prematurely, bypassing executor resolution

**File:** `KrabEar/core/pipeline/stages/llm_rewrite.py:64`

When LLM rewrite succeeds, the stage does:
```python
ctx.rewritten_text = result.text or text_in
ctx.final_text = ctx.rewritten_text   # line 64
```

The executor's final-text resolution (line 98) is:
```python
ctx.final_text = ctx.rewritten_text or ctx.cleaned_text or ctx.raw_text
```

Setting `ctx.final_text` inside a stage breaks the single-responsibility invariant: stages
should write only their own output fields; `final_text` is the executor's responsibility.
The duplication is harmless today but creates confusion if a later stage (e.g. Translation)
also wants to update `final_text`, and it means the stage metric system cannot observe whether
`final_text` was set by the stage or by the executor.

**Fix:** Remove line 64 (`ctx.final_text = ctx.rewritten_text`) from `LLMRewriteStage.process()`.
The executor already does this correctly on line 98.

---

### F6 — INFO | Test suite does not cover the `_stage_had_error` prefix logic or cache poisoning scenario

**Files:** `KrabEar/tests/test_pipeline_core.py`, `test_pipeline_stages.py`, `test_pipeline_e2e.py`

2234 lines of pipeline tests cover context defaults, stage skipping, executor flow, cache
hit/miss counters, bridge legacy-dict shape, and the PIPELINE_V2 env toggle. However no test
exercises the scenario where a stage fails **and** the cache was enabled — specifically, no
test verifies that a failed stage result is NOT written to cache (F2's root condition). The
bug in F2 would be undetectable by the current test suite even if cache were wired.

Additionally, the bridge's `StageCache`-not-passed path is not tested: all tests that exercise
caching instantiate `PipelineExecutor(stages, cache=StageCache())` directly, not via
`transcribe_v2()`.

**Fix:** Add integration tests:
1. `test_cache_not_written_on_stage_error`: inject a failing stage with cache enabled, assert
   `cache.get_stats()["hits"] == 0` on second call.
2. `test_bridge_no_cache_used`: call `transcribe_v2()` twice with same audio, assert
   `duration_ms` of second call is not 0 (would be 0 if cache hit occurred).

---

## Summary

| ID | Severity | Area             | Description                                          |
|----|----------|------------------|------------------------------------------------------|
| F1 | MEDIUM   | Wire status      | Pipeline never invoked in production (PIPELINE_V2 not checked) |
| F2 | MEDIUM   | Cache semantics  | Error-prefix mismatch → stale results cached after stage fail |
| F3 | LOW      | Cache semantics  | StageCache not instantiated by bridge → cache is no-op |
| F4 | LOW      | Resource mgmt    | Single-slot `_temp_path` is fragile; second temp file would leak |
| F5 | LOW      | Idempotency      | LLMRewriteStage sets `ctx.final_text` inside a stage (executor's job) |
| F6 | INFO     | Test coverage    | No test verifies cache-poisoning-on-error or bridge cache absence |

Error isolation is good: all stages catch exceptions and soft-fail via `ctx.errors.append()`;
no single stage failure can break subsequent stages. Stage ordering is correct. Idempotency
holds within a single run. The primary risk is F1 (dead code) combined with F2 (cache bug
that would only manifest if F1 were fixed without also fixing F2).
