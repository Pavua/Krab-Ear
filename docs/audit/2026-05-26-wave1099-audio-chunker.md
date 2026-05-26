# W1099 Audit: `core/audio_chunker.py` — AudioChunker

**Date:** 2026-05-26  
**Branch:** `fix/search-index-W1041`  
**Auditor:** W1099 sub-agent (read-only)

---

## Summary

`AudioChunker` splits long audio by silence for chunked transcription. The module
is 338 lines, well-tested (~80 tests across two files), and the W352–W391 cascade
bugs (GigaAM longform threshold + micro-advance infinite loop) were both resolved.
This audit surfaces 5 findings, 1 HIGH and 4 LOW/INFO.

---

## Findings

### F1 — HIGH · Silence skip condition uses `start_sec < cursor` (not `<=`)
**File:** `core/audio_chunker.py`, line 281

```python
if region.start_sec < cursor:
    continue
```

A silence region whose `start_sec == cursor` (i.e. it begins exactly at the
current cursor position) is **not** skipped. Its `mid = (start + end) / 2` will
therefore be inside the `(cursor, window_end]` window as long as the silence is
longer than `0.0 s`, and the cut will be placed at `region.start_sec +
_SPLIT_OFFSET_SEC` = `cursor + 0.05 s`. Because `0.05 < _MIN_ADVANCE_SEC`
(which is `max_chunk_sec / 2`), the condition on line 289 discards this cut
(`cut <= cursor + _MIN_ADVANCE_SEC`), so the boundary falls through to the
hard-cut path at `window_end`. The outcome is always **correct** (hard cut), but
the off-by-one on the skip guard is misleading and slightly wasteful: it evaluates
the full silence loop even though the only candidate will always be rejected.

**Fix (one-liner):** change `<` to `<=` on line 281, or add a comment documenting
why regions that start exactly at `cursor` are intentionally retained for inspection.

**Regression risk:** none — current behaviour is correct; this is a clarity/
performance nit that also documents intent.

---

### F2 — LOW · No `max_chunk_sec` cap enforced by constructor; caller sets 20 s for GigaAM

**File:** `core/engine.py`, line 2472 + `core/audio_chunker.py`, line 101

`AudioChunker` accepts any `max_chunk_sec > 0` at call time, with no constructor-
level cap. GigaAM's hard ~25 s limit is enforced only by the call site in
`engine.py` (`_GIGAAM_MAX_CHUNK_SEC = 20.0`). A future caller that passes the
default `30.0` (or a different value) to `AudioChunker.chunk()` for GigaAM will
silently produce 30 s chunks that exceed GigaAM's limit and fail at transcription
time. There is no docstring warning for this constraint.

**Recommendation:** add a note in `AudioChunker.chunk()` docstring that for GigaAM
callers must not exceed 20 s, or consider exporting a named constant
`GIGAAM_MAX_CHUNK_SEC = 20.0` from `audio_chunker.py` for call-site reuse.

---

### F3 — LOW · No min_chunk_sec guard; tail chunks can be < 400 samples

**File:** `core/audio_chunker.py`, `_build_chunks()` (line 306 ff.)

`AudioChunker` makes no guarantee about minimum chunk duration. A 1 ms tail chunk
(16 samples @ 16 kHz) is valid output. GigaAM Conformer requires a minimum of 400
samples; undersized chunks must be padded by the caller. `test_audio_chunker_edge_cases_wave373.py`
section 8 explicitly documents this as "caller responsibility", but there is no
runtime warning when a sub-400-sample chunk is emitted. On a 1-hour recording with
max_chunk_sec=20 s this produces 180 chunks, the very last of which may be
arbitrarily short.

**Recommendation:** emit a `logger.warning` from `_build_chunks()` when
`len(chunk_audio) < 400` so that the caller (engine.py) is alerted without
requiring a code change there.

---

### F4 — LOW · Memory pressure on very long audio (>1 hour)

**File:** `core/audio_chunker.py`, line 137–141

`detect_silence()` receives the full mono array and splits it into
`n_samples // 512` frames via `np.array_split()`. For a 3600 s recording @
16 kHz that is **57.6 M samples** (≈230 MB float32). `np.array_split` returns a
Python list of 112,500 views — **no copy** of the data, but the list object itself
is 112,500 × 8 bytes ≈ 900 KB of overhead, which is negligible. The frame RMS
array is 112,500 × 8 bytes ≈ 900 KB.

In `_build_chunks()` each chunk slice `audio[start_sample:end_sample]` is a
**NumPy view** (no copy for mono; a copy for stereo because of `axis=0` layout).
For **stereo** 1-hour audio all 180 chunks stay live simultaneously (they are
collected in a list before returning), resulting in a full second copy of the
stereo array in memory: 2 channels × 57.6 M × 4 bytes ≈ **460 MB** peak.

This is an **INFO**-level finding (the engine immediately transcribes each chunk
sequentially and could free them, but the list is returned wholesale). For mono
audio (the actual GigaAM path), chunks are views and no extra memory is allocated.

**Recommendation:** for stereo callers consider yielding chunks lazily or slicing
mono audio only. Current GigaAM path is mono, so no immediate action required.

---

### F5 — INFO · Wire status — AudioChunker not exposed via RecordingCoreService

**File:** `backend/recording_core_service.py` (no references to AudioChunker)

`AudioChunker` is imported **lazily** inside `engine.py:_transcribe_gigaam()` only.
It is not referenced in `RecordingCoreService` or any IPC handler. This is the
correct design (chunking is an implementation detail of the STT engine, not an IPC
concern), but the CLAUDE.md architectural overview lists `AudioChunker` as a
standalone core module without noting it is internal to the GigaAM path. There is
no `chunk_audio` IPC method and none is needed.

**Status:** no action required; recording confirmed as expected.

---

## W352–W391 Cascade History

| Wave | Bug | Status |
|------|-----|--------|
| W352 | GigaAM longform threshold mismatch (30 s ≠ 24 s) | Fixed in engine.py |
| W359 | Micro-advance loop: cursor advanced only +0.01 s per step when all silences start before cursor | Fixed: `_MIN_ADVANCE_SEC = max_chunk_sec / 2.0`; hard-cut fallback when no valid cut found |
| W373 | Edge-case test suite added covering regression scenarios | 80 tests, all passing |
| W391 | AudioChunker silence skip condition revisited (current F1) | Not fixed; behaviour correct but guard is off-by-one |

---

## Test Coverage Assessment

| Area | Coverage |
|------|----------|
| Short audio (≤ max_chunk_sec) | Covered in `TestChunkShortAudio` |
| Hard split (no silence) | Covered in `TestChunkHardSplit`, `TestAllSpeechNoSilence` |
| Silence-based split | Covered in `TestChunkSmartSplit`, `TestSingleSilenceInMiddle`, `TestMultipleShortSilences` |
| All-silence audio | Covered in `TestAllSilentAudio`, `TestChunkAllSilenceAndContinuous` |
| Empty audio | Covered (`test_empty_audio_returns_one_chunk`) |
| Stereo audio | Covered (`test_stereo_audio_chunked`) |
| Boundary: MAX+1ms / 2×MAX | Covered in `TestBoundaryJustOverMaxChunk`, `TestBoundaryDoubleMaxChunk` |
| Wave 359 micro-advance regression | Covered in `TestWave359Regression` |
| GigaAM MIN_SAMPLES (400) tail | Documented in `TestMinSamplesPadding` |
| Thread safety | Covered in `TestChunkConcurrency`, `TestConcurrentChunkerCalls` |
| merge_results() | Covered in `TestMergeResults` |
| Missing: min_chunk warning | **Not tested** (F3) |
| Missing: F1 off-by-one boundary | **Not tested** |

**Gap:** no test exercises a silence region that begins exactly at `cursor`
(F1 boundary), and no test asserts that a sub-400-sample tail emits a warning (F3).

---

## Verdict

`AudioChunker` is **production-safe**. The W352–W391 cascade regressions are fully
resolved and well-covered by regression tests. Two actionable improvements (F1 and
F3) are low-risk one-liners. Memory behaviour on 1-hour+ mono audio is acceptable;
stereo handling has a documented copy overhead (F4) that does not affect the current
GigaAM pipeline.

**Recommended actions (priority order):**
1. F3: add `logger.warning` for sub-400-sample chunks in `_build_chunks()`.
2. F1: fix off-by-one skip guard (`<` → `<=`) or add clarifying comment.
3. F2: document `max_chunk_sec` GigaAM constraint in docstring or export constant.
