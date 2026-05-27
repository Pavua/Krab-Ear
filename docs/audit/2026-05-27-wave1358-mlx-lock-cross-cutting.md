# MLX Lock Cross-Cutting Audit (W1358, 2026-05-27)

Re-audit of all MLX inference call sites for:
1. Correct `mlx_lock()` wrapping
2. `mx.clear_cache()` compliance (W63 rule)
3. `mlx_inter_process_lock` interaction
4. Test coverage of lock contention

Supersedes `mlx-call-sites-2026-05-04.md` (Phase C C.3). Adds Phase D.2 adapter paths
(`WhisperMLXAdapter`, `ParakeetSTTAdapter`) and deep-dives watchdog lock escape.

---

## Call Site Inventory

| File | Function | `mlx_lock()`? | `clear_cache()`? | Status |
|---|---|---|---|---|
| `core/engine.py:1892` | `_transcribe_model` direct path | ✅ yes | ✅ post-STT L921 | OK |
| `core/engine.py:1899` | `_transcribe_model` watchdog lambda | ⚠️ see F1 | ✅ post-STT L921 | See F1 |
| `core/engine.py:430` | `warmup()` | ✅ yes | — (warmup, not production inference) | OK |
| `core/engine.py:544` | `set_quality_profile()` cache flush | n/a | ✅ yes | OK |
| `core/audio_lang_id.py:203` | `_run_detect` → `_detect_with_mlx` | ✅ yes | ⚠️ see F2 | See F2 |
| `core/pipeline/stt_whisper_mlx_adapter.py:120` | `WhisperMLXAdapter.transcribe()` | ✅ yes | ⚠️ see F3 | See F3 |
| `core/pipeline/stt_parakeet.py:154` | `ParakeetSTTAdapter.transcribe()` | ✅ yes (correct) | ⚠️ see F4 | See F4 |
| `core/pipeline/stt_gigaam.py` | subprocess worker | n/a (PyTorch+MPS) | n/a | OK |
| `core/pipeline/stt_sensevoice.py` | `SenseVoiceSTTAdapter.transcribe()` | n/a (PyTorch+MPS) | n/a | OK |
| `scripts/debug_whisper.py:27,36` | `test_whisper_direct()` | ✅ yes (fixed 2026-05-04) | — (script) | OK |

---

## Findings

### F1 — MEDIUM: Watchdog daemon thread acquires no lock; lock released on timeout → concurrent GPU window

**File**: `core/engine.py:1892-1933`, `core/mlx_subprocess.py:95-165`

`_transcribe_model` acquires `mlx_lock()` (RLock) on the **calling thread**, then calls
`get_watchdog().run_with_timeout(fn=lambda: mlx_whisper.transcribe(...))`.
`run_with_timeout` spawns a **new daemon thread** that executes `fn()` (the actual
`mlx_whisper.transcribe` call). The daemon thread does NOT hold `mlx_lock` — RLock is
thread-owned, and the lock was acquired by the calling thread, not by the daemon thread.

**Normal path (no timeout)**: the calling thread holds `mlx_lock()` for the entire duration
of `thread.join(timeout_sec)`, so no second caller can enter the critical section while
the daemon thread is running. Effective serialization.

**Timeout path (GPU hang)**: when `thread.is_alive()` after `timeout_sec`, `run_with_timeout`
raises `MLXTimeoutError`. This propagates out of the `with mlx_lock()` block, releasing the
lock. But the daemon thread is still alive and still executing `mlx_whisper.transcribe` on
the Metal GPU. A subsequent call to `_transcribe_model` (fallback chain or next recording)
can now acquire `mlx_lock()` and start new MLX inference **concurrently** with the timed-out
daemon thread. This is exactly the race condition that caused the 2026-04-19 SIGSEGV.

**Risk assessment**: timeout triggers only in the GPU-hang scenario (rate: ~occasional, tracked
by W1316 `mlx.watchdog_hang` error code). When it does trigger, the concurrent GPU window lasts
until the daemon thread terminates or the process exits. The daemon thread is daemonized so it
won't block process exit, but the race window exists.

**Recommended fix**: pass a reference to the lock to the daemon thread's lambda so it can
re-acquire it, OR use a subprocess instead of a daemon thread for watchdog isolation (subprocess
has its own GPU context). Alternatively, maintain a `_watchdog_thread_alive` flag and check
it before permitting the next `_transcribe_model` entry.

---

### F2 — LOW: `audio_lang_id.py._detect_with_mlx` missing `mx.clear_cache()` after LID inference

**File**: `core/audio_lang_id.py:209-285`

`_detect_with_mlx` calls `mlx_whisper.load_models.load_model`, `mlx_whisper.audio.log_mel_spectrogram`,
and `mlx_whisper.decoding.detect_language`. All three allocate Metal GPU buffers. None of these
calls are followed by `mx.clear_cache()`.

The module-level comment at line 218 notes: "buffers are retained even after `mx.clear_cache()` in
engine.py" (model cache leak), but this refers to the cached *model object*, not the per-call
intermediate tensors from `log_mel_spectrogram` and `detect_language`.

W63 rule: `mx.clear_cache()` must be called after every `mlx_whisper.transcribe()` call and
related MLX inference. LID inference is not `transcribe()`, but it does allocate Metal buffers.
Not calling `clear_cache()` after each LID call means 30-second mel spectrograms accumulate
in the Metal heap during long sessions that run LID on every recording.

**Recommended fix**: add `mx.clear_cache()` at the end of `_detect_with_mlx`, after
`detect_language` returns, inside the `mlx_lock()` context.

---

### F3 — LOW: `WhisperMLXAdapter.transcribe()` (Phase D.2) missing `mx.clear_cache()` after inference

**File**: `core/pipeline/stt_whisper_mlx_adapter.py:120-143`

`WhisperMLXAdapter.transcribe()` calls `mlx_whisper.transcribe()` under `mlx_lock()` (correct),
but does NOT call `mx.clear_cache()` after the inference. The W63 rule (`mx.clear_cache()` after
every `mlx_whisper.transcribe()`) was codified in the `engine.py` path (line 921) but was not
applied when the Phase D.2 `WhisperMLXAdapter` wrapper was introduced.

The Phase D.2 adapter is used by `STTRouter` when the pipeline path is active. On long sessions
with many recordings routed through `WhisperMLXAdapter`, Metal buffers accumulate — the same
leak pattern fixed by W63 in `engine.py`.

**Recommended fix**: after the `with mlx_lock()` block in `WhisperMLXAdapter.transcribe()`,
add the same `mx.clear_cache()` pattern used in `engine.py:919-923`.

---

### F4 — LOW: `ParakeetSTTAdapter.transcribe()` uses `mlx_lock()` but missing `mx.clear_cache()`

**File**: `core/pipeline/stt_parakeet.py:149-155`

`ParakeetSTTAdapter` correctly uses `mlx_lock()` (the Parakeet MLX adapter shares the Metal
GPU with `mlx_whisper` so serialization is correct). However, there is no `mx.clear_cache()`
call after the Parakeet inference completes.

The CLAUDE.md note clarifies: "PyTorch+MPS adapters (SenseVoice, Parakeet, WhisperX, Voxtral)
don't need this lock." This suggests `ParakeetSTTAdapter` may be using PyTorch+MPS rather than
MLX directly. The stt_parakeet.py file includes `mlx_lock` as a precaution ("prevent concurrent
Metal GPU access with Whisper MLX"). If Parakeet uses MLX internals, `clear_cache()` is needed;
if it uses PyTorch+MPS only, neither the lock nor `clear_cache()` are required but the lock is
harmless. Needs verification against actual Parakeet runtime path.

---

### F5 — LOW: `mlx_inter_process_lock` defaults to no-op; no call site uses it in production paths

**File**: `core/mlx_inter_lock.py`, `core/mlx_lock.py:39`

`mlx_inter_process_lock()` requires `KRAB_EAR_MLX_INTER_PROCESS_LOCK=1` to activate. It is
re-exported from `core.mlx_lock` but is used by **no production call site** (only in
`test_mlx_inter_lock.py` and `test_mlx_lock.py`). The CLAUDE.md recommends wrapping as:

```python
with mlx_inter_process_lock():   # outer: cross-process
    with mlx_lock():             # inner: intra-process
        result = mlx_whisper.transcribe(...)
```

This double-lock pattern is never used in practice. If a future subprocess Krab Ear process
(e.g., bulk reprocess worker spawned via `BulkReprocessor`) calls MLX directly and the flag
is not set, cross-process GPU access will be uncoordinated. Current production is safe because
MLX is always run in the single backend process — but this is an unguarded assumption.

---

### F6 — LOW: No test verifies `WhisperMLXAdapter.transcribe()` calls `mx.clear_cache()`

**File**: `KrabEar/tests/test_mlx_cache_clear.py`

`test_mlx_cache_clear.py` tests the `engine.py` cleanup block (lines 919-923) and the
`set_quality_profile()` flush. It does not cover `WhisperMLXAdapter.transcribe()` (F3) or
`ParakeetSTTAdapter.transcribe()` (F4). Since these adapters are the Phase D.2 pipeline
path, they are on a separate code path not exercised by engine-level cache-clear tests.

`test_mlx_concurrency.py` has a smoke-check for `engine.py` transcribe call sites being inside
`mlx_lock` blocks, but has no equivalent smoke-check for the adapter files.

---

### F7 — INFO: Lock hold time includes full transcription duration (~2-30s)

**File**: `core/engine.py:1892`, `core/audio_lang_id.py:203`

Both `_transcribe_model` and `_run_detect` hold `mlx_lock()` for the entire inference duration
(typically 1-30 seconds for Whisper, ~50ms for LID). RLock re-entry from the same thread is
cheap (O(1)), but any **other thread** attempting MLX inference will block for the full duration.

For the current single-backend-process architecture with a single recording thread this is not
a problem. It becomes relevant if the `BulkReprocessor` (which re-transcribes multiple items)
is ever made concurrent. Current architecture is safe, but the comment "Minimal critical section:
only the mlx_whisper.transcribe call" in `engine.py:1891` is accurate — the lock scope is correct
and not unnecessarily wide.

---

## Summary

| ID | Severity | File | Issue |
|---|---|---|---|
| F1 | MEDIUM | `engine.py:1892` + `mlx_subprocess.py:131` | Watchdog timeout releases lock while daemon thread still executing MLX |
| F2 | LOW | `audio_lang_id.py:262` | No `mx.clear_cache()` after LID inference |
| F3 | LOW | `stt_whisper_mlx_adapter.py:130` | No `mx.clear_cache()` after Phase D.2 adapter inference |
| F4 | LOW | `stt_parakeet.py:155` | No `mx.clear_cache()` (PyTorch+MPS clarification needed) |
| F5 | LOW | `mlx_inter_lock.py` | Inter-process lock always no-op; future subprocess paths unprotected |
| F6 | LOW | `test_mlx_cache_clear.py` | No test verifies adapter-path `clear_cache()` |
| F7 | INFO | `engine.py`, `audio_lang_id.py` | Lock held entire inference duration — fine for single-thread, note for future bulk concurrency |

**Call sites correctly wrapped (8/8 inference sites have lock)**: `engine.py._transcribe_model`,
`engine.py.warmup`, `audio_lang_id._run_detect`, `stt_whisper_mlx_adapter.WhisperMLXAdapter.transcribe`,
`stt_parakeet.ParakeetSTTAdapter.transcribe`, `debug_whisper.py` (both variants).
Non-MLX adapters (`stt_sensevoice`, `stt_gigaam`) correctly exempt.

**`mx.clear_cache()` gaps**: `engine.py` both call sites have it; Phase D.2 adapters
(`WhisperMLXAdapter`, `ParakeetSTTAdapter`, `audio_lang_id`) are missing it.

## References

- W63 memory leak fix: `PR #405` — `mx.clear_cache()` added to `engine.py`
- Phase C C.3 original audit: `docs/audit/mlx-call-sites-2026-05-04.md`
- MLX thread-safety SIGSEGV crash: `~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips`
- Fix PR: `#71` (2026-04-19)
- Phase D.2 adapters introduced: `e88e3e16`
- W1316 `stt.mlx_watchdog_hang` error code
