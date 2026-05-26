# Wave 857 — Transcriber audit

**File:** `KrabEar/backend/transcriber.py`  
**Date:** 2026-05-26  
**Auditor:** Claude Sonnet 4.6  
**Lines:** 147  

---

## Summary

`Transcriber` is a thin wrapper (147 LOC) over `AudioEngine`.
It adds: (1) profile switching before each transcription, (2) HF_TOKEN guard before diarization,
and (3) `error_bus` late-injection.

3 findings identified (1 medium, 1 low, 1 info).

---

## Findings

### F1 — MEDIUM: profile switch and transcribe are not atomic (TOCTOU race)

**Location:** `transcriber.py:82–94` (`transcribe`), `transcriber.py:99–100` (`transcribe_preview`)

`set_quality_profile()` mutates `engine.quality_profile` and `engine.current_model`
without any lock. Immediately after, `engine.transcribe()` is called using whatever
`quality_profile` is current at that point. If two IPC threads call `Transcriber.transcribe`
concurrently with *different* quality profiles, a race exists:

```
Thread A: set_quality_profile("max")        # engine.quality_profile = "max"
Thread B: set_quality_profile("balanced")   # engine.quality_profile = "balanced"
Thread A: engine.transcribe(...)            # runs with "balanced" — wrong profile
Thread B: engine.transcribe(...)            # also runs with "balanced"
```

`AudioEngine` itself holds no lock around the `set_quality_profile` + `transcribe` pair.
The MLX intra-call lock (`mlx_lock`) serialises only the raw `mlx_whisper.transcribe` GPU
call, not the profile mutation that precedes it.

**In production today:** IPC handlers run in per-client threads (`IPCServer` spawns one
thread per client connection). Concurrent `start_recording` + `transcribe_audio_file`
from two Swift windows is a realistic scenario.

**Impact:** wrong quality model is used silently; no error emitted. On a profile switch
that triggers `mx.clear_cache()` mid-inference the GPU could also briefly hold a stale
compute graph.

**Recommended fix:** wrap the switch+call pair in a `threading.Lock` on `Transcriber`:
```python
self._profile_lock = threading.Lock()

def transcribe(self, ...) -> dict:
    with self._profile_lock:
        self.engine.set_quality_profile(quality_profile)
        return self.engine.transcribe(...)
```
Or promote `set_quality_profile` into `engine.transcribe(quality_profile=...)` so the
engine can do the atomic switch itself.

---

### F2 — LOW: HF_TOKEN guard reads only env vars, ignores settings-stored token

**Location:** `transcriber.py:117–123` (`_push_diarization_no_token_if_needed`)

The guard checks only:
```python
os.environ.get("HF_TOKEN") or os.environ.get("KRAB_EAR_HF_TOKEN")
```

But `core/config.py` line 104 exposes `settings.HF_TOKEN` (a Pydantic-Settings field
overridable via `KRAB_EAR_HF_TOKEN` env var or settings.json). `engine.py:2871` reads
`os.environ.get("HF_TOKEN") or settings.HF_TOKEN` — the full lookup.

If a user sets their token via the IPC `set_settings` call (settings.json, not env), the
guard in `Transcriber` will not see it and will emit a spurious `diarization.no_token`
error, while `engine.py` will proceed successfully with the token from `settings.HF_TOKEN`.

**Impact:** false `diarization.no_token` error toasts for users who set their token via
the GUI settings panel rather than an env variable. Not a crash, but noisy.

**Recommended fix:**
```python
from core.config import settings as _settings

token = (
    os.environ.get("HF_TOKEN")
    or os.environ.get("KRAB_EAR_HF_TOKEN")
    or _settings.HF_TOKEN
    or ""
)
```

---

### F3 — INFO: no concurrency tests for Transcriber

**Location:** `KrabEar/tests/test_transcriber.py` (all four transcriber test files)

All four test files (`test_transcriber.py`, `test_transcriber_edge_cases.py`,
`test_transcriber_errors.py`, `test_transcriber_diarization.py`) test only
single-threaded, sequential calls via `FakeAudioEngine`. There are no tests that
exercise concurrent `transcribe()` calls or interleaved preview+full calls.

Given F1 above, a concurrency regression test would act as a guard.

**No code change required.** Adding a test with `threading.Thread` would catch regressions
in any future `Transcriber` refactor.

---

## MLX lock usage at this layer

`Transcriber` itself does **not** call MLX directly — correct. All MLX inference goes
through `AudioEngine._run_mlx_transcription`, which holds `mlx_lock()` for the
`mlx_whisper.transcribe` call. The transcriber layer appropriately delegates to the
engine without duplicating the lock.

The `set_quality_profile` path calls `mx.clear_cache()` (engine.py:544–548), which is
outside `mlx_lock`. `clear_cache()` is a cache flush, not an inference call, so this is
acceptable per the existing pattern — but it does mean the flush can race with an
in-progress inference in another thread. This is a pre-existing engine issue, not
introduced by `Transcriber`.

---

## Verdict

| # | Severity | Issue | Action |
|---|----------|-------|--------|
| F1 | Medium | profile switch + transcribe TOCTOU race | Add `_profile_lock` on `Transcriber` |
| F2 | Low | HF_TOKEN guard misses settings-stored token | Include `settings.HF_TOKEN` in lookup |
| F3 | Info | No concurrency tests | Add threaded smoke test |

No MLX lock violations at the transcriber layer. The layer correctly delegates to engine.
