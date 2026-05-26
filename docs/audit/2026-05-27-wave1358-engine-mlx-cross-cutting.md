# W1358 Cross-Cutting Audit: engine.py MLX Lock Usage

**Date:** 2026-05-27  
**Branch audited:** `codex/krab-ear-v2` (HEAD `6c900317`)  
**Scope:** `KrabEar/core/engine.py`, `KrabEar/core/audio_lang_id.py`, `KrabEar/core/mlx_lock.py`, `KrabEar/core/mlx_inter_lock.py`, `KrabEar/core/pipeline/stt_parakeet.py`  
**Context:** Several wave fixes were written (W1117, W1219/W1223, W1225, W1235) to address MLX lock gaps. This audit verifies which fixes are present in `codex/krab-ear-v2` and identifies any remaining issues.

---

## Status of Prior Wave Fixes

| Wave fix | Commit | In `codex/krab-ear-v2`? |
|----------|--------|------------------------|
| W1117 — `audio_lang_id` `mx.clear_cache()` | `c0f9ef5d` | **NOT merged** |
| W1219/W1223 — Voxtral `mlx_lock` | `d03c8858` | **NOT merged** |
| W1225 — Parakeet inter-process lock | `d32aba9c` | **NOT merged** |
| W1235 — pyannote double-checked lazy lock | `a1d8c53f` | **NOT merged** |

All four fix commits exist in PR branches but have not been merged to the base branch. This audit documents the resulting open issues.

---

## Findings

### F1 — HIGH: Voxtral `_voxtral_generate()` not wrapped in `mlx_lock()`

**File:** `KrabEar/core/engine.py`, line 2641  
**Status:** Open (W1219/W1223 fix not merged)

`_transcribe_voxtral()` calls `_voxtral_generate()` which uses `mistral_inference` with the MLX-quantized model (`mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit`). The `_VOXTRAL_REPO_ALLOWLIST` default includes only `mlx-community/` prefixed repos, confirming this is an MLX workload. The call at line 2641 is not protected by `mlx_lock()`.

```python
# engine.py line 2641 — UNPROTECTED
out_tokens, _ = _voxtral_generate(
    input_ids,
    model,
    max_tokens=_VOXTRAL_MAX_TOKENS,
    ...
)
```

If Voxtral is enabled (`VOXTRAL_ENABLED=True`) and a concurrent Whisper transcription runs, both reach the Metal GPU simultaneously, risking the same `__hash_table<MTL::Resource*>` SIGSEGV documented in PR #71.

**Fix:** Wrap the `_voxtral_generate()` call in `with mlx_lock():` (exactly as done in commit `d03c8858` — merge that PR).

---

### F2 — HIGH: Lazy model loaders have no threading lock (race condition on concurrent load)

**File:** `KrabEar/core/engine.py`, methods `_load_diarization_pipeline` (line 2854), `_load_sensevoice_model` (line 1966), `_load_parakeet_model` (line 2080), `_load_whisperx_model` (line 2169)  
**Status:** Open (W1235 fix not merged)

All four lazy-load methods use the check-then-act pattern without a threading lock:

```python
# Pattern in all four methods — NOT thread-safe
def _load_diarization_pipeline(self) -> Pipeline:
    if self._diarization_pipeline is not None:   # check
        return self._diarization_pipeline
    # ... no lock here ...
    self._diarization_pipeline = Pipeline.from_pretrained(...)  # act
```

When IPC server threads and the REST server call these concurrently, two threads can both pass the `is not None` check and both attempt `from_pretrained()`. For `_load_diarization_pipeline` this loads pyannote (~3 GB) twice, and for models using MPS this can trigger Metal GPU assertion failures. The fix requires `threading.RLock` instance variables and double-checked locking (commit `a1d8c53f`).

Note: `_load_parakeet_model` (in `stt_parakeet.py` pipeline) does use `mlx_lock()` around inference, but the model *loading* itself is still unprotected against concurrent calls.

---

### F3 — MEDIUM: `set_quality_profile()` calls `mx.clear_cache()` without `mlx_lock()`

**File:** `KrabEar/core/engine.py`, lines 544–548  
**Status:** Open (no fix written yet)

```python
def set_quality_profile(self, profile: str) -> bool:
    ...
    self.current_model = new_model
    try:
        import mlx.core as _mx
        _mx.clear_cache()   # <-- NOT under mlx_lock()
    except (ImportError, AttributeError):
        pass
    return True
```

`mx.clear_cache()` frees Metal GPU buffers. If called while another thread holds `mlx_lock()` and is mid-inference in `mlx_whisper.transcribe()`, the cache flush can invalidate Metal heap allocations actively being used by the GPU kernel. The existing regression test `test_profile_switch_regression.py` deliberately documents that `set_quality_profile` does NOT acquire `mlx_lock()`, but it only tests that `set_quality_profile` is not *blocked* — it does not test whether `clear_cache()` is safe to call concurrently with active inference. The correct fix is `with mlx_lock(): _mx.clear_cache()`.

Note: the post-inference `clear_cache()` at line 921 (inside `transcribe()`) is already called after the `mlx_lock()` context has been released, which is also a subtle ordering issue — the cache flush should ideally occur while the lock is still held to prevent a new inference from starting before the Metal heap is fully released. However, for a single-user desktop application, the practical risk is low.

---

### F4 — MEDIUM: `mlx_inter_process_lock()` is re-exported but never called at any production call site

**File:** `KrabEar/core/mlx_lock.py` (re-export), `KrabEar/core/mlx_inter_lock.py` (implementation)  
**Status:** Documentation gap

`mlx_inter_lock.py` is complete and correct. `mlx_lock.py` re-exports `mlx_inter_process_lock` with the Wave 49 pattern documented in comments:

```python
# Documented usage pattern (mlx_lock.py lines 22-25):
with mlx_inter_process_lock():  # outer: cross-process flock
    with mlx_lock():            # inner: intra-process RLock
        mlx_whisper.transcribe(...)
```

However, `grep -rn "mlx_inter_process_lock" KrabEar/core/engine.py` returns zero results. No production call site in `engine.py`, `audio_lang_id.py`, or any pipeline adapter actually uses the outer `mlx_inter_process_lock()` wrapper. The feature flag `KRAB_EAR_MLX_INTER_PROCESS_LOCK=1` enables the lock object but it is never acquired.

The practical impact is low because: (a) the feature flag is OFF by default, (b) LM Studio (the only other MLX-using process) cannot be coordinated via `flock` regardless. However, the W1225 fix for Parakeet (`d32aba9c`) was supposed to add this to the Parakeet adapter — that fix is also not merged, leaving the documented pattern as dead code.

---

### F5 — LOW: `audio_lang_id.py` has no `mx.clear_cache()` after LID inference

**File:** `KrabEar/core/audio_lang_id.py`  
**Status:** Open (W1117 fix not merged, commit `c0f9ef5d`)

`AudioLanguageID._run_detect()` correctly wraps inference in `mlx_lock()`, but does not call `mx.clear_cache()` after the LID inference completes. Per the W63 policy (`engine.py` line 920), `clear_cache()` should be called after every MLX inference to release Metal heap buffers and prevent RSS growth on long sessions. The comment at `audio_lang_id.py` line 218 explicitly acknowledges that the LID model cache "holds MLX Metal buffers even after `mx.clear_cache()` in `engine.py`" — but the fix (calling `clear_cache()` after LID inference itself) is in the W1117 PR, not yet merged.

---

## Non-issues (confirmed correct)

- **All `mlx_whisper.transcribe()` callsites** in `_transcribe_model()` (line 1892) and `warmup()` (line 429) are correctly wrapped in `with mlx_lock()`.
- **GigaAM** (`_transcribe_gigaam()`): uses PyTorch MPS, not MLX — `mlx_lock` correctly not required. Comment at line 1646 is accurate.
- **SenseVoice** (`_transcribe_sensevoice()`): PyTorch MPS — `mlx_lock` not required. Correct.
- **Parakeet** in pipeline (`stt_parakeet.py` line 154): correctly uses `with mlx_lock()` around inference. The in-engine `_transcribe_parakeet()` delegates to `_load_parakeet_model()` + NeMo transcribe — NeMo is PyTorch-based, no MLX lock needed there.
- **WhisperX** (`_transcribe_whisperx()`): PyTorch backend, no MLX lock needed. Comment is absent but correct.
- **`mlx_lock()` is an RLock** (reentrant) — the fallback chain calling `_transcribe_model()` from inside a `ThreadPoolExecutor` does not deadlock because each `ThreadPoolExecutor` thread is a *new* thread (not the same thread that holds the lock). RLock re-entry is within the same thread only. No deadlock risk from this pattern.
- **`mlx_lock.py` re-export** of `mlx_inter_process_lock` is technically a no-op (feature flag off by default) — import side effects are zero.

---

## Summary Table

| # | Severity | Issue | Fix exists? |
|---|----------|-------|-------------|
| F1 | HIGH | Voxtral `_voxtral_generate()` unprotected by `mlx_lock()` | Yes (W1223, not merged) |
| F2 | HIGH | Lazy model loaders race on concurrent calls (4 methods) | Yes (W1235, not merged) |
| F3 | MEDIUM | `set_quality_profile()` calls `mx.clear_cache()` outside `mlx_lock()` | No fix yet |
| F4 | MEDIUM | `mlx_inter_process_lock()` re-exported but never used at call sites | Partial (W1225, not merged) |
| F5 | LOW | `audio_lang_id.py` missing `mx.clear_cache()` after LID inference | Yes (W1117, not merged) |

**Root cause pattern:** Four separate PR branches (W1117, W1223, W1225, W1235) contain valid fixes for MLX lock issues but none have been merged to `codex/krab-ear-v2`. The highest-risk item is F1 (Voxtral + Whisper concurrent SIGSEGV) because Voxtral is the only MLX adapter in the chain that lacks `mlx_lock()` protection on the current base branch.
