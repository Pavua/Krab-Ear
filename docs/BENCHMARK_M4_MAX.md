# STT Benchmark — MacBook Pro M4 Max (36 GB)

**Date:** 2026-04-12  
**Hardware:** MacBook Pro M4 Max, 36 GB RAM  
**Python venv:** `.venv_krab_ear` (mlx-whisper backend)  
**Script:** `KrabEar/tests/benchmark_stt.py`

## Setup

| Parameter | Value |
|-----------|-------|
| Test audio | 3s sine wave @ 16 kHz (440 Hz + 880 Hz overtone), 93.8 KB WAV |
| Iterations | 3 (1 cold + 2 warm) |
| Cleanup profile | soft |
| Lang hint | ru |
| Domain | casual |

## Results

| Metric | Value |
|--------|-------|
| AudioEngine cold load | 3338 ms |
| Cold (1st transcription) | 3932 ms |
| Warm avg (2nd+) | **343 ms** |
| Avg (all iterations) | 1539 ms |
| p95 | 3573 ms |
| Max | 3932 ms |
| Real-Time Factor (warm) | **0.11x** |

## Interpretation

- **Warm RTF = 0.11x** — the engine processes 3s audio in ~343 ms, roughly **9x faster than real-time**. Well within the ≤1.0x real-time target.
- **Cold start total** (engine load + 1st inference): ~7270 ms (~3.3s load + ~3.9s first inference including model weight fetch). Subsequent calls are ~10x faster due to model caching.
- **Transcription of synthetic tone**: engine returned "Сохраняй смысл." for all iterations — consistent output on non-speech audio.

## Baseline Summary

```
AudioEngine load : 3338 ms
Cold latency     : 3932 ms
Warm latency avg :  343 ms   ← primary baseline
Warm RTF         : 0.11x     (real-time OK)
```

These numbers serve as the M4 Max baseline for regression tracking. Run `KrabEar/tests/benchmark_stt.py` to compare future changes.
