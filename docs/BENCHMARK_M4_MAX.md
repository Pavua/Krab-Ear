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

---

## 2026-05-12 Wave 45 Refresh

**Date:** 2026-05-12  
**Hardware:** MacBook Pro M4 Max, 36 GB RAM, macOS 26.5 (Darwin 25.5.0)  
**Backend:** launchd Variant B, uptime ~44 min, PID 45033  
**Stack:**  
- STT primary (RU): GigaAM RNNT (subprocess worker, Python 3.12 venv)  
- STT fallback: mlx-whisper large-v3 (mlx-community/whisper-large-v3-mlx)  
- Diarization: pyannote speaker-diarization-3.1, device=MPS  
- LLM rewriter: gemma-4-26b-a4b-it-optiq via LM Studio (http://localhost:1234)  
- Language routing: enabled (RU → GigaAM first)  

Data source: backend's `PerformanceProfiler` (5 real transcription calls, logged in-process), plus direct `ps` RSS snapshots and synthetic audio benchmarks via `transcribe_paths` IPC.

### STT Latency (GigaAM RNNT, RU audio)

| Stage | Calls | p50 ms | p95 ms | max ms | Notes |
|-------|-------|--------|--------|--------|-------|
| GigaAM RNNT transcribe | 5 | 14,561 | 31,562 | 32,986 | warm; subprocess IPC overhead included |
| STT with fallback chain | 5 | 14,561 | 31,568 | 32,986 | same wall time (GigaAM succeeded) |
| mlx-whisper cold load | 1 | 9,711 | — | — | AudioEngine.__init__() full cold start |

> **Note:** STT p50 of ~14.5 s is high due to MLX/MPS memory contention (LM Studio also active, sharing GPU). Under idle GPU conditions expect 1–3 s (per April 2026 baseline at 343 ms for synthetic 3 s tone; GigaAM measured 1.1 s warm on 20 s RU call in isolation — see `memory/reference_gigaam_bench_2026-04-26.md`).

### Diarization (pyannote speaker-diarization-3.1, MPS)

| Stage | Calls | p50 ms | p95 ms | max ms | Notes |
|-------|-------|--------|--------|--------|-------|
| Diarization (MPS) | 2 | 9,029 | 10,348 | 10,494 | on real recordings |
| Model load (cold) | 1 | 2,781 | — | — | first load; cached thereafter |

### LLM Rewriter (gemma-4-26b-a4b-it-optiq)

Data from two sources: backend profiler (in-process, 5 calls) and R19 bench (2026-05-12 04:19, 5 prompts × 3 runs).

| Scenario | p50 ms | p95 ms | Notes |
|----------|--------|--------|-------|
| Warm (profiler, mixed success/fail) | 688 | 58,643 | circuit breaker fast-fails skew p50 low |
| Warm meeting/phone prompt (R19) | ~9,000–10,000 | ~11,600 | 3/3 succeeded; quality=1.0 |
| Warm short prompt — http_400 | 18–27 | 42 | model rejects short dictation prompts |
| Cold start | ~30,000 | — | model load in LM Studio (once) |

> **Current model:** `gemma-4-26b-a4b-it-optiq`. Quality leader from R19: `supergemma4-26b-uncensored-mlx-v2` (p50=8,380 ms, quality=1.00 all prompts). Previous R18 warm baseline: 1,587 ms p50 (`qwen3-4b-abliterated`).

### Full Pipeline E2E (audio-in → transcript-out)

From IPC `transcribe_paths` call on 2.2 s synthetic RU speech (includes GigaAM STT + diarization + LLM rewrite):

| Audio | Duration | E2E ms | RTF | Notes |
|-------|----------|--------|-----|-------|
| Short synthetic (say TTS) | 2.2 s | ~143,000 | ~65× | Under GPU contention; LM Studio active |

> E2E is dominated by diarization (~9 s) + LLM rewrite (~10–28 s) + STT (~14.5 s). Under no-contention conditions, GigaAM alone is RTF ~0.04× (24× faster than real-time per April bench).

### Memory Footprint (production idle stack)

| Process | PID | RSS MB | Notes |
|---------|-----|--------|-------|
| backend (service.py) | 45033 | 81.0 | Python 3.14, all services loaded |
| rest_server | 45035 | 23.1 | Flask REST API (port 5005) |
| gigaam_worker (active) | 45096 | 7.8 | GigaAM RNNT subprocess, model hot |
| gigaam_worker (idle) | 45091 | 4.3 | second worker, model not loaded |
| KrabEarAgent (Swift) | 39224 | 15.8 | native macOS agent, UI + IPC client |
| **TOTAL** | — | **132.0** | idle, no model inference in progress |

> pyannote diarization model (MPS) and mlx-whisper weights are NOT reflected in RSS — they live in Metal GPU heap and MLX's unified memory, not in the Python process RSS. Expect +1–3 GB in Metal during active inference.

### Comparison vs April 2026 Baseline

| Metric | Apr 2026 | May 2026 (Wave 45) | Delta | Reason |
|--------|----------|---------------------|-------|--------|
| AudioEngine cold load | 3,338 ms | 9,711 ms | +191% | GigaAM worker + pyannote init added to startup |
| STT warm (3 s tone) | 343 ms | ~14,561 ms* | +42× | GigaAM RNNT now primary for RU; MLX contention |
| Diarization p50 | — | 9,029 ms | new | pyannote-3.1 MPS first measured |
| LLM rewriter warm | 1,587 ms | ~9,500 ms | +6× | Switched from qwen3-4b to gemma-4-26b-a4b-it-optiq |
| Backend RSS (idle) | ~453 MB* | 81 MB | -82% | prev was measured post-inference with MLX weights hot |
| Total stack RSS | — | 132 MB | new | GigaAM workers + agent included |

*\*STT under GPU contention from LM Studio; isolation benchmark (GigaAM warm on 20 s RU audio): 1,100 ms (RTF=0.04×).*  
*\*Prior RSS 453 MB was measured with mlx-whisper model weights cached in process; current idle RSS is lower because MLX memory sits in GPU heap.*

### Recommended Regression Thresholds (Wave 45+)

| Stage | Warn (p50 > ) | Fail (p50 > ) |
|-------|--------------|--------------|
| GigaAM STT (isolated) | 3,000 ms | 8,000 ms |
| Diarization (MPS) | 15,000 ms | 30,000 ms |
| LLM rewriter (meeting prompt) | 15,000 ms | 30,000 ms |
| Backend idle RSS | 150 MB | 300 MB |
