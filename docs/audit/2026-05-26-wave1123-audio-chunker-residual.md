# W1123 Re-audit: `core/audio_chunker.py` — Residual Findings (post-W1099)

**Date:** 2026-05-26  
**Branch:** `audit/audio-chunker-residual-W1123`  
**Auditor:** W1123 sub-agent (read-only)  
**Based on:** W1099 audit (commit `9f0bc389`) — docs-only, **no code changes shipped**

---

## W1099 Finding Status

All five W1099 findings remain **unfixed** in `codex/krab-ear-v2` as of v2.0.5. The W1099
audit commit (`9f0bc389`) added only the audit document; no follow-up code PR exists.

| Finding | Description | Status |
|---------|-------------|--------|
| W1099-F1 | `start_sec < cursor` off-by-one skip guard (line 281) | **OPEN** — code unchanged |
| W1099-F2 | No `max_chunk_sec` cap / GigaAM constraint not documented | **OPEN** — code unchanged |
| W1099-F3 | No warning when tail chunk < 400 samples (GigaAM min) | **OPEN** — code unchanged |
| W1099-F4 | Stereo memory copy on long audio (INFO) | **OPEN** — INFO, no action required |
| W1099-F5 | AudioChunker not exposed via RecordingCoreService (by design) | **CONFIRMED OK** — intentional |

---

## New Findings

### NF1 — MED · SmartSilenceSkipper (W1102) applied before AudioChunker — timestamps in `merge_results()` become wrong

**Files:** `core/engine.py` (step 2.6), `core/audio_chunker.py` (`merge_results`)

W1102 wired `SmartSilenceSkipper` into `AudioEngine.transcribe()` at step 2.6, **before** the
main `_transcribe_with_fallback()` call. The GigaAM chunker path (`_transcribe_gigaam`) is a
separate code path that is invoked from within `_transcribe_with_fallback()` and operates on
`audio_data_np` which is the **raw** audio before step 2.6 processing.

However, there is a second scenario: if `SMART_SILENCE_SKIP_ENABLED=True` and the call goes
through the normal Whisper path (not GigaAM), `SmartSilenceSkipper` shrinks the array. Then
`AudioChunker` is **not** called for the Whisper path — it is only called inside
`_transcribe_gigaam`. So for the Whisper path the interaction is: denoiser → skipper → VAD →
Whisper (no chunking). This is internally consistent.

The actual interaction problem is in `merge_results()`: each `AudioChunk.start_sec` /
`end_sec` is computed from the **original** full audio timestamps (before any processing).
When chunks are later assembled into results, the caller (engine.py GigaAM path, lines 2492–2497)
sets `"start_sec": ch.start_sec` from the chunk metadata. This is always the position in the
**original** mono 16 kHz array. If `SmartSilenceSkipper` had been applied to `audio_data_np`
**before** the GigaAM call, the chunks' `start_sec`/`end_sec` would be positions in the
**shortened** array, not the original recording — yielding systematically wrong timestamps in
`merge_results()` output (e.g. Whisper segments and `start_sec`/`end_sec` of returned result).

**Current actual call order in engine.py:**
1. Step 2.6: `SmartSilenceSkipper` applied to `audio_data` (numpy, non-preview).
2. Step 3: `_transcribe_with_fallback(audio_data, ...)` called with the **shortened** audio.
3. Inside `_transcribe_with_fallback` → `_transcribe_gigaam(audio_data_np, ...)` receives
   the **shortened** audio.
4. `AudioChunker.chunk(audio_data_np, ...)` splits the shortened audio.
5. Chunks have `start_sec` relative to the shortened audio, not the original recording.
6. `merge_results()` produces `start_sec`/`end_sec` in shortened-audio time.

The W1102 CRITICAL WARNING comment in engine.py acknowledges that SmartSilenceSkipper
shifts Whisper timestamps but treats this as a caller concern. The GigaAM chunked path
within the same pipeline silently inherits shifted timestamps with no warning at the
merge step.

**Risk:** MED — `SMART_SILENCE_SKIP_ENABLED` defaults to `False` (per `config.py` line 826),
so this path is inactive in production. If a user enables it, GigaAM longform results will
have shifted timestamps, silently corrupting SRT exports and diarization alignment.

**Recommendation:** Either (a) guard the GigaAM chunker path to assert
`not _smart_silence_active` before chunking and raise/log a warning, or (b) document the
expected timestamp shift explicitly in `merge_results()` docstring with a note that
`start_sec`/`end_sec` values are relative to the audio array passed to `chunk()`, not the
original recording.

---

### NF2 — MED · AudioDenoiser (W1080) applied per full audio, not per chunk — boundary artifacts on silence splits

**Files:** `core/engine.py` (step 2.5), `core/audio_denoiser.py`, `core/audio_chunker.py`

`AudioDenoiser._denoise_spectral_gating()` estimates the noise floor from the **first 200 ms**
(`_NOISE_FLOOR_SAMPLES = 3200` samples at 16 kHz) of the full audio array. This noise profile
is then applied uniformly to the entire signal.

`AudioChunker` splits the audio **after** denoising (step 2.5 precedes step 3 which calls
`_transcribe_gigaam`). Each chunk therefore receives pre-denoised audio with no noise-profile
discontinuities at chunk boundaries — this is **correct** and **better** than per-chunk
denoising (which would re-estimate noise floor from the first 200 ms of each chunk, potentially
sampling speech rather than silence).

However, if in a future refactor chunks are denoised independently (e.g. if AudioChunker is
exposed as a pre-processing step before the full pipeline), per-chunk denoising would create
spectral artifacts at the boundaries: the STFT overlap-add reconstruction at boundary 0 of
each chunk would mismatch the ISTFT reconstruction at the end of the preceding chunk, causing
a small spectral discontinuity. The 512-sample FFT window spans 32 ms; at a 30 s chunk
boundary this is inaudible but may slightly affect Whisper's confidence on the first word of
each chunk.

**Current state:** the current code denoises once before chunking — no artifact. The finding
is a **latent risk** if the calling order is ever changed.

**Recommendation:** Add a comment at step 2.5 in `engine.py` explicitly noting that
`_maybe_denoise` must be applied to the full array before chunking, not per-chunk, to avoid
noise-profile discontinuities at boundaries.

---

### NF3 — LOW · No `max_chunk_sec` cap in constructor — configurability gap still open (W1099-F2 not closed)

**File:** `core/audio_chunker.py` (constructor, line 76–91)

W1099-F2 noted the missing GigaAM cap. The re-audit confirms it remains open. A related gap:
the `threshold_db` and `min_silence_sec` constructor parameters have no bounds validation.
A caller that passes `min_silence_sec=0.0` or `threshold_db=0.0` will silently produce
degenerate silence detection (every frame is "silence" at 0 dB, or no frame qualifies at
`min_silence_sec=0.0`), leading to either all-silence splits or no splits at all with no
error or warning.

In the `threshold_db=0.0` case, `SilenceDetector` will treat the entire audio as silence
(RMS of any signal < 0 dB re: 1.0 is false only for clipped audio), meaning `usable_silences`
will contain the entire recording as one region. The chunker will still terminate correctly
(the `_MIN_ADVANCE_SEC` guard prevents micro-advance), but the output may be single large
chunks with misleading split behaviour.

**Recommendation:** Add `if threshold_db >= 0.0: raise ValueError(...)` and
`if min_silence_sec < 0.0: raise ValueError(...)` guards to `__init__`, mirroring the
existing guards in `chunk()`.

---

### NF4 — LOW · Stereo slicing in `_build_chunks` uses `audio.shape[0]` but falls back to `len(audio)` for 1D — inconsistency

**File:** `core/audio_chunker.py`, `_build_chunks()` (lines 322–327)

```python
max_samples = len(audio) if audio.ndim == 1 else audio.shape[0]
```

For a 1D mono array `len(audio) == audio.shape[0]`, so the logic is correct. However:

1. The condition `audio if audio.ndim == 1 else audio` at line 133 in `chunk()` is a **no-op**:
   `chunk_audio = audio if audio.ndim == 1 else audio` — both branches assign `audio` unchanged.
   This is a copy of dead code (the original intent may have been `audio.mean(axis=1)` for
   stereo-to-mono conversion before returning the single chunk). Currently stereo audio in the
   single-chunk fast path is returned as-is (stereo), which is correct, but the conditional
   is misleading.

2. For stereo audio `audio[start_sample:end_sample]` slices along axis 0 (rows = time samples),
   which is correct for `(n_samples, n_channels)` layout. This is the standard soundfile layout.
   However, if audio is passed as `(n_channels, n_samples)` (channel-first, e.g. from some
   torch audio loaders), the slice would return the wrong shape. There is no dtype/shape
   assertion to validate the layout.

**Recommendation:** Remove the dead `audio if ndim == 1 else audio` conditional (line 133),
replace with just `chunk_audio = audio`. Optionally add an assertion
`assert audio.ndim in (1, 2)` and `assert audio.ndim == 1 or audio.shape[1] <= audio.shape[0]`
to catch channel-first layout before producing silently wrong chunks.

---

### NF5 — LOW · Idempotency: calling `chunk()` twice on the same audio produces identical results — confirmed

**File:** `core/audio_chunker.py`

`AudioChunker` holds no mutable state between calls: `self._detector` (a `SilenceDetector`)
contains no per-call state (it runs `np.array_split` and comparisons on each call), and
`_compute_split_points` / `_build_chunks` are purely functional. Thread-safety tests in
`TestConcurrentChunkerCalls` confirm no state corruption under concurrency.

Idempotency verified by code inspection: two calls to `chunker.chunk(same_audio, sr,
max_chunk_sec)` return structurally identical results (same `start_sec`/`end_sec` boundaries,
same sample slices). There is no caching or lazy-initialisation that could differ on second call.

**Status:** CONFIRMED OK — no issue. The test suite already covers this implicitly via
`TestChunkConcurrency` (multiple threads sharing the same chunker instance).

---

## Test Coverage Gap — Stereo Path

W1099 noted `test_stereo_audio_chunked` exists. Re-audit confirms the existing stereo test
only checks that chunking completes without error and that chunks are 2D arrays. Missing tests:

1. **Stereo sample count preservation** — no test asserts `sum(chunk.audio.shape[0] for chunk in chunks) == audio.shape[0]`.
2. **Stereo channel count preservation** — no test asserts `chunk.audio.shape[1] == 2` for all chunks.
3. **Dead conditional at line 133** — no test catches the no-op branch since the assertion
   `chunk.audio.ndim == 2` is already satisfied regardless.

---

## Summary Table

| ID | Severity | Description | W1099 linkage |
|----|----------|-------------|---------------|
| NF1 | MED | SmartSilenceSkipper timestamp shift inherited by GigaAM chunker path | New (W1102 interaction) |
| NF2 | MED | Denoiser applied to full audio before chunking — latent per-chunk boundary artifact risk | New (W1080 interaction) |
| NF3 | LOW | No bounds validation on `threshold_db` / `min_silence_sec` constructor params | Extends W1099-F2 |
| NF4 | LOW | Dead no-op conditional line 133; no channel-layout assertion for stereo | Extends W1099-F4 |
| NF5 | INFO | Idempotency confirmed — no issue | New verification |

**W1099 findings all still open; no code changes shipped post-audit.**

---

## Verdict

`AudioChunker` remains production-safe. The two MED findings (NF1, NF2) affect non-default
feature flags (`SMART_SILENCE_SKIP_ENABLED=False` by default) and are latent risks rather
than active bugs. NF3 and NF4 are one-liner hardening improvements. NF5 is a clean
confirmation.

**Priority order for follow-up fixes:**
1. NF1: document timestamp shift contract in `merge_results()` docstring + add guard in GigaAM path.
2. W1099-F3: add `logger.warning` for sub-400-sample tail chunks in `_build_chunks()`.
3. NF3: add `ValueError` guards for `threshold_db` / `min_silence_sec` in constructor.
4. NF4: remove dead no-op conditional at line 133.
5. W1099-F1: fix off-by-one skip guard (`<` → `<=`) or add clarifying comment.
