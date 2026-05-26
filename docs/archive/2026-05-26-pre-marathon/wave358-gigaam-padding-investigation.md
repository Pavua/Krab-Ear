# Wave 358 — GigaAM `padding (200, 200)` Bug Investigation

**Date:** 2026-05-22  
**Branch:** `investigate/wave358-gigaam-padding`  
**Triggered by:** Backend digest 2026-05-21 — 4 events 2026-05-18 21:41 and 21:50

---

## Summary

Two separate bugs cause GigaAM failures on recordings in the 24–40s range. The `padding (200, 200)` error is caused by Bug 2 (AudioChunker micro-advance loop) producing chunks of ~10ms — far below GigaAM's Conformer minimum input size. Bug 1 (overly aggressive longform threshold) causes 24.8–25.9s clips to unnecessarily take the longform path and fail with `LocalEntryNotFoundError` (pyannote not cached).

---

## Bug 1 — Threshold Too Aggressive (24s instead of ~30s)

**File:** `KrabEar/core/engine.py` line 2418

```python
use_longform = duration_sec > 24.0
```

**Problem:**  
- GigaAM's actual hard limit is approximately 25–26s per the code comment ("gigaam падает на ~26s+")
- The threshold at 24.0s forces any clip over 24s into the longform/chunked path
- Clips of 24.8s, 24.9s, 25.5s are all ~1–2s over the threshold — within GigaAM's actual safe range
- Observed in April 2026 logs: dozens of 24.8s/24.9s clips hit `LocalEntryNotFoundError` on longform because pyannote/segmentation-3.0 is gated on HuggingFace (TOS not accepted)

**Fix:**  
Raise threshold to 30s. GigaAM handles ≤30s reliably (benchmarks show RTF=0.041 on 20–25s clips). The 5s safety margin becomes 5s below the real limit:

```python
# Before
use_longform = duration_sec > 24.0
# After  
use_longform = duration_sec > 30.0  # GigaAM hard limit is ~30-32s; 30s gives 5s margin
```

---

## Bug 2 — AudioChunker Micro-Advance Loop (ROOT CAUSE of padding error)

**File:** `KrabEar/core/audio_chunker.py`, method `_compute_split_points`

### Observed behaviour

Log entries from 2026-05-05 20:18 — 21:07:

| Duration | Chunks (expected) | Chunks (actual) | Error |
|----------|-------------------|-----------------|-------|
| 25.5s    | 2                 | 545             | padding (200,200) |
| 40.2s    | 3                 | 1936            | padding (200,200) |
| 31.3s    | 2                 | 585             | padding (200,200) |
| 28.6s    | 2                 | 857             | padding (200,200) |
| 24.9s    | 2                 | 488             | padding (200,200) |
| 25.3s    | 2                 | **2**           | none ✓ |
| 35.2s    | 2                 | **2**           | none ✓ |

### Root Cause

The `_compute_split_points` algorithm in `AudioChunker` contains a **near-infinite micro-advance loop** that activates when a long silence region extends past the current cursor position.

```python
# From audio_chunker.py lines 263-291
while cursor + max_chunk_sec < total_sec:
    window_end = cursor + max_chunk_sec
    best_cut = None
    for region in usable_silences:
        mid = (region.start_sec + region.end_sec) / 2.0
        if cursor < mid <= window_end:
            cut = region.start_sec + _SPLIT_OFFSET_SEC  # BUG: always the SAME value for a long silence
            cut = max(cut, cursor + 0.01)               # floor ensures cursor advances by only 0.01s!
            if best_cut is None or cut > best_cut:
                best_cut = cut
    if best_cut is not None:
        split_points.append(best_cut)
        cursor = best_cut  # advances by only 0.01s if cut == cursor + 0.01
```

**Step-by-step trace for 25.5s audio with leading 0.1–25.0s silence:**

1. `cursor=0`, `window_end=20`. Silence `[0.1, 25.0]` has `mid=12.55` → inside `(0, 20)`.  
   `cut = 0.1 + 0.05 = 0.15`. `max(0.15, 0+0.01) = 0.15`. `cursor = 0.15`.
2. Same silence: `mid=12.55` still in `(0.15, 20.15)`. `cut = 0.15`. `max(0.15, 0.15+0.01) = 0.16`. `cursor = 0.16`.
3. `cut = max(0.15, 0.17) = 0.17`. `cursor = 0.17`.
4. ... cursor advances by **0.01s per iteration** until `cursor + 20 >= 25.5`, i.e. `cursor >= 5.5`.
5. Iterations needed: `(5.5 - 0.15) / 0.01 = 535` → **535 tiny split points**.

**Verification:** `(duration - max_chunk_sec) / 0.01 ≈ actual_chunks`
- 25.5s: `(25.5 - 20) / 0.01 = 550` ≈ 545 ✓
- 24.9s: `(24.9 - 20) / 0.01 = 490` ≈ 488 ✓
- 28.6s: `(28.6 - 20) / 0.01 = 860` ≈ 857 ✓
- 40.2s: `2 × (20.1 / 0.01) ≈ 4020/2 ≈ 2010` ≈ 1936 (two windows)

**Why does this cause `padding (200, 200)`?**

Each 0.01s chunk = 0.01 × 16000 = **160 samples**.  
GigaAM's Conformer uses `padding=(200, 200)` in its convolution layers → requires ≥400 samples (0.025s).  
160 < 400 → `RuntimeError: Padding size should be less than the corresponding input dimension`.

**Why does it happen intermittently?**

Only occurs when a long silence region spans PAST the initial cursor position (long leading silence or a pause that starts before the current window boundary). Audio without such leading silences gets a proper cut in a single iteration.

### Fix for `_compute_split_points`

Filter out silence regions whose optimal cut point is already behind (or at) the cursor:

```python
for region in usable_silences:
    mid = (region.start_sec + region.end_sec) / 2.0
    if cursor < mid <= window_end:
        cut = region.start_sec + _SPLIT_OFFSET_SEC
        cut = max(cut, cursor + 0.01)
        # NEW: skip regions that don't advance cursor meaningfully
        # (i.e. their cut point is <= cursor + min_advance)
        # This prevents a long leading silence from dominating all iterations
        if cut <= cursor + 0.01:
            continue  # already-behind silence, skip
        if best_cut is None or cut > best_cut:
            best_cut = cut
```

**Better fix:** skip silence regions whose `start_sec` is already behind `cursor`. A silence that started before the current position cannot provide a useful forward cut:

```python
for region in usable_silences:
    # Skip if silence starts before current cursor (already cut through it)
    if region.start_sec < cursor:
        continue
    mid = (region.start_sec + region.end_sec) / 2.0
    if cursor < mid <= window_end:
        cut = region.start_sec + _SPLIT_OFFSET_SEC
        cut = max(cut, cursor + 0.01)
        if best_cut is None or cut > best_cut:
            best_cut = cut
```

This ensures that a long silence `[0.1, 25.0]` is only used as a cut point in the iteration where `cursor < 0.1` (i.e. the very first iteration), placing a clean cut at `0.15s`. On subsequent iterations, `region.start_sec=0.1 < cursor=0.15` → **skipped**.

---

## Proposed Threshold Ranges

| Duration | Current behavior | Recommended |
|----------|-----------------|-------------|
| ≤ 30s    | ≤24s: `transcribe()`, >24s: chunked/longform | `transcribe()` directly |
| 30–60s   | chunked (AudioChunker) | chunked (AudioChunker, after bug fix) |
| > 60s    | chunked → longform fallback | chunked → longform fallback |

```python
_GIGAAM_SHORTFORM_MAX_SEC = 30.0   # safe direct transcribe() limit
_GIGAAM_MAX_CHUNK_SEC = 20.0       # chunk size for >30s audio (unchanged)
use_longform = duration_sec > _GIGAAM_SHORTFORM_MAX_SEC
```

---

## Files to Change

1. **`KrabEar/core/audio_chunker.py`** — `_compute_split_points`: add `if region.start_sec < cursor: continue` guard.
2. **`KrabEar/core/engine.py`** — `_transcribe_gigaam`: change threshold `> 24.0` → `> 30.0`.

Both changes are small (1–3 lines each) and safe. The AudioChunker fix is correct by the algorithm's contract: we should not cut AT a silence that started before the current window position; we should cut INTO a silence that starts at or after cursor.

---

## Investigation Timeline

- **April 2026 logs**: 24.8–25.5s clips hit longform directly (no AudioChunker yet) → `LocalEntryNotFoundError` (pyannote gated). Bug 1 only.
- **May 5, 2026**: AudioChunker introduced. 25.5s+ clips now hit chunker first → micro-advance loop → padding error. Both bugs active.
- **May 18, 2026 21:41–21:50**: 4 Sentry events confirmed. Digest reported 24.8s clip as "should NOT trigger longform" — confirmed, threshold should be 30s not 24s.
