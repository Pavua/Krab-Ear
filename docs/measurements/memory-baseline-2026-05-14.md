# Memory Baseline 2026-05-14

- duration: 300s, interval: 10s, samples: 31
- python backend PID: 1210 (launchd Variant B, ai.krab.ear.backend)
- KrabEarAgent PID: 699 (bundle binary)
- gigaam_worker PIDs: 3474, 3600

## RSS Summary

| Process | Min MB | Mean MB | Max MB |
|---------|--------|---------|--------|
| KrabEarAgent (Swift) | 13.6 | 23.3 | 29.4 |
| backend/service.py | 8.5 | 169.1 | 392.8 |
| gigaam_worker (total) | 2.9 | 108.0 | 2610.2 |

## Notable Observations

- **Backend RSS drift**: first-5-sample mean = 124.1 MB → last-5-sample mean = 391.6 MB (+267.5 MB over 300s).
  This coincides with LLM rewriter activity (2 inference bursts visible in trace).
- **Worker spike**: gigaam_worker peaked at 2610 MB at T+51s — GigaAM model warm-up / longform audio load.
  Post-spike it returns to 3 MB idle (subprocess releases model after job).
- **Backend spike T+40s**: 45 MB → 380 MB → 393 MB during apparent LLM rewrite burst.
  Memory does NOT fully return to baseline (stays ~391 MB), indicating a hold by the LLM rewriter or model cache.
- **Agent RSS** stable, minor GC oscillation 14–29 MB — no leak signal.
- **LLM circuit**: `closed` throughout all 31 samples — rewriter healthy.
- **history_total_items**: 3265 (constant) — no background writes during window.

## Leak Indication

**Backend: SUSPECTED LEAK / HOLD** — RSS grew +267 MB over 5 min and did not return to start level.
Root cause candidates: LLM rewriter response cache, mlx-whisper model weight retention, or uncollected
Python objects after inference. Needs Phase C follow-up profiling (tracemalloc / memray).

**Worker: no leak** — spikes are transient GigaAM model loads; returns to ~3 MB idle.

## Script Fix Applied

`scripts/memory_baseline.py` cmd_short matching bug fixed in this branch:
`cmd_short` (last path component = `Python`) was used for `service.py` matching → always 0 MB.
Fix: store full `cmdline` in process dict and match on that.
