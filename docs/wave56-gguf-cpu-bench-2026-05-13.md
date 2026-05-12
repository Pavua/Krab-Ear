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

---

## Wave 57 — saiga_gemma3_12b GGUF CPU (also disqualified)

**Tested** smaller candidate per Wave 56 conclusion: 12B Russian-tuned, 7.30 GB
disk. Hypothesis: smaller model + Russian-native → maybe acceptable latency.

| Metric | Result |
|---|---|
| Cold load | 31.36s (6.80 GiB RAM) |
| Run 1 (dictation_note) | **90s timeout, empty body** |
| Run 2 (tech_short) | 71938ms, output OK ("Используем MLX Whisper для транскрипции, но есть проблема: когда два потока обращаются одновременно.") |
| Run 3 (meeting) | 73995ms, output mostly OK (small "мы будем обсуждать" hallucination) |
| Token gen rate | ~0.4 t/s (26 out tokens / 72s ≈ 0.36 t/s) |
| Quality on warm | Acceptable — Russian preserved, brand "MLX Whisper" correct |
| Memory pressure | Stable — swap went DOWN 11 → 10.6 GB |

**Why so slow on CPU**: LM Studio's llama.cpp runtime on macOS uses conservative
thread count by default. Real M4 Max has 14-16 CPU cores capable of ~10-15 t/s
with `-t 12 --batch-size` tuning, but `lms load` doesn't expose those flags.
Even tuned, GGUF на CPU vs MLX Metal: ~5-10× slower under best conditions.

**Combined Wave 56+57 verdict**: `--gpu off` strategy DOES free Metal allocator
(architectural insight valid), but **CPU LLM inference speed на macOS llama.cpp
is fundamentally insufficient** for 12B+ Russian rewrite models. 4× slower
than current production baseline под contention.

## Final recommendation: keep Metal architecture

The 9500ms regression under STT+LLM concurrent load is **inherent trade-off**
of unified memory architecture sharing Metal allocator. Cures:

### Within budget (no hardware change):

1. **Pre-warm LM Studio** before STT call — eliminate cold-load gap. Wave 43
   warmup_sync already does this. Verify production runs warmup.
2. **Smaller MLX model** for rewriter — supergemma-mm 4-bit is faster (R22
   showed 6.3s warm). MLX Metal contention persists but per-call cost lower.
3. **Accept current**: 9500ms under contention is real-world reality. UX
   mitigations (progress indicator, async paste, etc.) могут maskировать
   latency без architectural change.

### Out of budget (hardware/cloud change):

1. **Apple M4 Ultra (Mac Studio)** — 192 GB unified, much higher Metal
   bandwidth, contention less impactful.
2. **Discrete GPU** — would isolate STT (Metal) from LLM (CUDA), но Apple
   doesn't support eGPU on Apple Silicon Macs (M-series).
3. **Cloud LLM** — fast, но privacy-first Krab Ear design conflicts с this.

## Status post-test

✅ Restored — `gemma-4-26b-a4b-it-optiq` IDLE Metal, 3745ms warm smoke test
(excellent — better than 6964ms previous, system tension lower now).

⚠️ **Don't repeat** these tests on M4 Max — proven unviable. Document closes
LM Studio backend research thread for now. Re-open if hardware changes.
