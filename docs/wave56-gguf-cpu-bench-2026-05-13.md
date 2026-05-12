# Wave 56 — Qwen3.6-27B GGUF CPU bench (FAILED)

**Date:** 2026-05-13
**Test:** Replace baseline MLX (`gemma-4-26b-a4b-it-optiq`, 15.62 GB Metal) with
GGUF CPU (`qwen/qwen3.6-27b --gpu off`, 17.48 GB RAM) per Wave 55 A2 finding
that `--gpu off` flag frees Metal allocator for mlx-whisper.

## Result: DISQUALIFIED

| Metric | Baseline MLX | Qwen3.6-27B GGUF CPU |
|---|---|---|
| Cold load | ~30s (Metal) | ~60s (RAM) |
| First request | 1587ms (R19 isolated) | **120s+ timeout × 3 runs** |
| Memory pressure | 15.62 GB Metal (shares unified) | 17.48 GB RAM (full swap target) |
| Swap before | 12.45 GB used / 13.3 GB total | — |
| Swap during | — | **21.46 GB used / 22.5 GB total** (+9 GB) |
| Smoke output | "Привет! Чем я могу..." (6.9s warm) | empty body (timeout) |

## Why it failed

1. **Token generation too slow**: Qwen3.6-27B на CPU = ~1-2 t/s realistic
   (estimate from logs). 150-token rewrite = 75-150s. Not viable for live
   diктовка flow.
2. **Swap explosion**: 27B Q4_K_M = 17.48 GB RAM footprint. M4 Max 36 GB
   total → forced 9 GB additional swap during inference. kernel_task swap
   I/O spiked, system entered critical pressure.
3. **No Metal cost saved**: While Metal allocator was indeed free during
   GGUF run, that's irrelevant if LLM itself is too slow to be usable.

## Recovery

- Unloaded Qwen GGUF CPU
- Reloaded `gemma-4-26b-a4b-it-optiq` MLX baseline (15.62 GB Metal)
- Smoke test: 6964ms warm — production restored
- Swap recovered to 11.89 / 13.3 GB

## Conclusion: 27B too big for CPU

The `--gpu off` strategy is correct architectural insight, but the model
size constraint changes everything. On M4 Max 36 GB RAM:

- 27B model on CPU → too slow + swap explosion → DISQUALIFIED
- 12B model on CPU → maybe viable (~3-5 t/s estimate)
- 7-9B model on CPU → likely viable

## Wave 57+ next candidates

In order of preference:
1. `saiga_gemma3_12b.Q4_K_M.gguf` (6.8 GB on disk) — **Russian-tuned**, ideal
   for Krab Ear use case; 12B should fit comfortably in remaining RAM after
   baseline-style Metal usage
2. `mlabonne_qwen3-8b-abliterated` GGUF — already in inventory (per Wave 47 list);
   8B model very fast on CPU
3. `qwen2.5-14b-instruct-abliterated-v2` GGUF — middle ground at 14B

Bench procedure for Wave 57: same as this one, BUT with model ≤12B parameters.

## Production status post-test

✅ Restored — `gemma-4-26b-a4b-it-optiq` IDLE on Metal as before. No production
impact. User's диктовка flow uninterrupted.

⚠️ Architectural insight from Wave 55 A2 still valid — Metal contention IS
the root cause of 9500ms regression. The fix path is the right shape, just
wrong model size. Wave 57 with smaller GGUF should validate the approach.
