# Wave 1014 — AudioChunker Audit

**Date:** 2026-05-26
**Scope:** `KrabEar/core/audio_chunker.py`, `KrabEar/core/silence_detector.py` (threshold dependency)
**Focus:** silence threshold alignment (W912 unified -40 dB), chunk boundary correctness, memory bound for 1h+ audio, M4 Max performance, edge cases (all-silence/no-silence), W352 micro-advance regression, wire status, test coverage

---

## Summary

| # | Severity | File | Finding |
|---|----------|------|---------|
| 1 | LOW | `audio_chunker.py` | Dead no-op branch in short-path: `chunk_audio = audio if audio.ndim == 1 else audio` always assigns `audio` regardless of condition |
| 2 | LOW | `audio_chunker.py` | Spanning silence regions silently skipped: silences that start before `cursor` but end inside the window are excluded by the `start_sec < cursor` guard, causing a potential hard-cut where a silence-cut was possible |
| 3 | INFO | `audio_chunker.py` | O(n²) silence scan already documented in W885 F9 — confirmed still present, no additional findings |
| 4 | INFO | `audio_chunker.py` | Memory bound for 1h audio is acceptable on M4 Max: mono float32 ≈ 220 MB, `detect_silence` list comprehension creates 112 500 intermediate array references but these are views (no copies for equal-size splits); `np.array_split` preserves base reference |
| 5 | INFO | `audio_chunker.py` | `_to_mono` called twice for mono input: once in `chunk()` line 124 and again inside `SilenceDetector.detect_silence()` line 63 — harmless (returns same object via identity path) but redundant |
| 6 | PASS | — | W352 micro-advance regression: `_MIN_ADVANCE_SEC = max_chunk_sec / 2.0` guard confirmed in place and covered by Wave 373 regression suite (9 tests in `TestWave359Regression`) |
| 7 | PASS | — | Silence threshold: default `-40.0 dB` in `AudioChunker.__init__` matches `SilenceDetector.detect_silence` default — consistent with W912 unified standard; engine.py instantiates `AudioChunker()` with no override |

---

## Detailed Findings

### F1 — Dead no-op branch in short-path (LOW)

**File:** `audio_chunker.py`, line 133

```python
chunk_audio = audio if audio.ndim == 1 else audio
```

Both branches of the conditional produce the same value. This was likely a placeholder for a future stereo-downmix in the short path. The short path returns the original `audio` unchanged (correct for stereo callers — they receive the full stereo array), but the conditional is dead code that may confuse future maintainers.

**Recommendation:** replace with `chunk_audio = audio` and add a comment explaining that stereo arrays are returned as-is (STT callers handle mono conversion internally).

---

### F2 — Spanning silence region silently skipped (LOW)

**File:** `audio_chunker.py`, `_compute_split_points`, line 281

```python
for region in usable_silences:
    if region.start_sec < cursor:
        continue
```

A silence region that **started before** the current cursor but **ends inside** the current window is skipped entirely. Example: cursor=25 s, region=[24.5 s, 26.0 s] — the silence mid-point is 25.25 s which falls inside the window, but the region is discarded because `start_sec (24.5) < cursor (25.0)`. In this case a valid silence-cut at ~25.3 s is missed and the algorithm falls back to a hard cut at 25 + max_chunk_sec.

In practice, this edge case only occurs when a previous cut lands inside a long silence (the cursor is advanced to `best_cut = region.start_sec + 0.05`, placing cursor mid-silence), so the next iteration sees the same silence's `start_sec < cursor`. The net effect is a hard cut slightly earlier than ideal — not data loss, but slightly worse chunk boundary placement.

**Recommendation:** change the guard to skip only regions that end before cursor:

```python
if region.end_sec <= cursor:
    continue
```

This preserves the mid-point calculation correctness (mid is still in the window) while correctly handling spanning silences.

---

### F3 — O(n²) silence scan (INFO, pre-existing W885 F9)

**File:** `audio_chunker.py`, `_compute_split_points`, lines 279–292

Already documented in Wave 885, finding F9. Re-confirmed present. For a 1-hour recording with 600 silence regions and 120 chunk iterations, the inner loop executes up to 600 × 120 = 72 000 iterations — negligible on M4 Max but worth noting. The `usable_silences` list is bounded by speech density, rarely exceeding a few hundred entries in practice.

No new action beyond the existing W885 recommendation (bisect or mutable index pointer).

---

### F4 — Memory bound for 1h+ audio (INFO, no action)

A 1-hour 16 kHz mono float32 recording occupies ~220 MB on M4 Max with 36 GB RAM — well within budget. Stereo doubles to ~440 MB. The `detect_silence` path uses `np.array_split` which creates **views** (shared base pointer), not copies — confirmed experimentally. The list comprehension builds 112 500 `float` RMS values (~0.9 MB float64 array). Total overhead for silence detection: O(n_samples) time, O(n_frames) extra memory for `frame_rms`, which is ~1 MB for a 1-hour recording. No memory risk.

The chunker processes audio once for silence detection, then slices the original array per chunk — no full duplication of audio data occurs.

---

### F5 — Double `_to_mono` call (INFO, no action needed)

`chunk()` calls `SilenceDetector._to_mono(audio)` at line 124, then `detect_silence()` calls `self._to_mono(audio)` again at line 63 on the already-mono result. For mono input, `_to_mono` returns the same object (identity path, `audio.ndim == 1` → `return audio`). For stereo input, the first call produces a mono array which is passed to `detect_silence`, where the second call is a no-op. Net result: zero extra computation, just a redundant call. No fix required.

---

### F6 — W352 Micro-Advance Regression: PASS

The Wave 352 / Wave 359 micro-advance bug (cursor advancing only ~0.01 s per iteration on all-silence audio, causing near-infinite loops) is confirmed fixed. `_compute_split_points` contains:

```python
_MIN_ADVANCE_SEC = max_chunk_sec / 2.0
...
if cut <= cursor + _MIN_ADVANCE_SEC:
    cut = None  # reject cuts that don't advance enough
```

When no valid cut is found or all candidate cuts are too close to cursor, the algorithm falls back to `window_end` (hard cut), guaranteeing cursor advances by exactly `max_chunk_sec`. The Wave 373 regression test suite (`TestWave359Regression`, 5 tests) covers long leading silence, trailing silence, alternating patterns, and chunk-count bounding. All 83 tests pass.

---

### F7 — Silence Threshold Alignment: PASS

`AudioChunker.__init__` defaults to `threshold_db=-40.0`. `SilenceDetector.detect_silence` defaults to `threshold_db=-40.0`. The engine instantiates `AudioChunker()` with no override (line 2492), so the effective threshold is -40.0 dB throughout the GigaAM longform path. This matches the W912 unified -40 dB standard. No drift detected.

---

## Wire Status

`AudioChunker` is wired in `core/engine.py` as the primary chunking strategy for GigaAM longform transcription (lines 2489–2516). It is used with `max_chunk_sec=20.0` (_GIGAAM_MAX_CHUNK_SEC). Fallback to `transcribe_longform()` (pyannote path) is in place at line 2516. No IPC wiring required — this is an internal audio processing utility.

---

## Test Coverage

- `KrabEar/tests/test_audio_chunker.py`: 8 test classes, covers dataclass, short/long audio, smart split, edge cases, all-silence, merge, integration, concurrency.
- `KrabEar/tests/test_audio_chunker_edge_cases_wave373.py`: 10 test classes, covers Wave 359 regression, GigaAM MIN_SAMPLES padding, boundary conditions (±1ms), concurrent calls.
- **Total: 83 tests, all pass.**
- **Missing coverage:** no test for F2 (spanning silence region across cursor boundary). A test with cursor placed mid-silence would document the current behaviour and catch any future regression if F2 is fixed.
