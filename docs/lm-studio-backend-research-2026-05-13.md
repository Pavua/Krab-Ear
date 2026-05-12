# LM Studio Backend Research — GPU Contention Fix
**Date:** 2026-05-13  
**Context:** Wave 48 Phase C MLX flock is intra-process only and cannot block LM Studio (separate OS process). This document investigates switching LM Studio to a non-Metal inference backend to eliminate the 1587→9500ms STT regression caused by concurrent Metal GPU allocator contention.

---

## 1. Root Problem

`mlx-whisper` (Krab Ear) and LM Studio MLX runtime both allocate Metal buffers via the shared `MTLDevice` allocator. When both run concurrently, the allocator's internal `__hash_table<MTL::Resource*>` contention causes:
- STT latency regression: 1587ms → 9500ms
- Occasional SIGSEGV / Metal GPU stuck requiring reboot

The intra-process `mlx_lock` (Wave 48) serializes only within the Python backend. LM Studio is a separate OS process — no shared mutex can reach it.

---

## 2. LM Studio Runtime Inventory

LM Studio has two runtimes installed:

| Runtime | Format | Version | Status |
|---------|--------|---------|--------|
| `llama.cpp-mac-arm64-apple-metal-advsimd` | GGUF | 2.14.0 | **SELECTED** |
| `mlx-llm-mac-arm64-apple-metal-advsimd` | MLX | 1.7.0 | **SELECTED** |

Key finding: **both selected runtimes are the latest**. llama.cpp is available for GGUF models and supports `--gpu off` flag.

### GPU offload flag (confirmed working)

```bash
lms load <model-key> --gpu off   # CPU-only, 55 MiB GPU vs 18+ GiB GPU
lms load <model-key> --gpu max   # Full Metal offload (default for MLX models)
```

When `--gpu off` is passed to llama.cpp (GGUF model), LM Studio reports:
- `Estimated GPU Memory: 55.01 MiB` (only small overhead, no compute)
- `Estimated Total Memory: 18.24 GiB` (all in RAM)

This confirms **GGUF + CPU-only is feasible** and would completely vacate Metal GPU for mlx-whisper.

---

## 3. Currently Loaded Model

```
IDENTIFIER                  MODEL                   STATUS    SIZE       DEVICE
gemma-4-26b-a4b-it-optiq   gemma-4-26b-a4b-it-optiq  IDLE    15.62 GB   Local
```

This is an MLX model — it uses the MLX runtime and holds Metal GPU allocations even while IDLE.

---

## 4. GGUF Model Inventory (on disk)

19 GGUF files found across `/Volumes/4TB SSD/LMStudio_models/`. Sorted by size and relevance:

| File | Size | Arch | Notes |
|------|------|------|-------|
| `lmstudio-community/Qwen3.6-27B-GGUF/Qwen3.6-27B-Q4_K_M.gguf` | **16 GB** | qwen35 | Best size match to baseline 26B |
| `models/DavidAU/Openai_gpt-oss-20b.../...MXFP4_MOE4.gguf` | **11 GB** | gpt-oss | 20B, MOE format |
| `models/bartowski/rwkv-6-world-7b-GGUF/rwkv-6-world-7b-f16.gguf` | **15 GB** | RWKV6 | Different arch (RNN), no use |
| `models/bartowski/microsoft_Fara-7B-GGUF/microsoft_Fara-7B-bf16.gguf` | **15 GB** | qwen2vl | 7B bf16, vision model |
| `mradermacher/Qwen2.5-14B-Instruct-abliterated-v2-GGUF/...Q4_K_M.gguf` | **8.4 GB** | Qwen2 | 14B Q4 |
| `richardyoung/Qwen3-14B-abliterated-GGUF/...Q4_K_M.gguf` | **8.4 GB** | qwen3 | 14B Q4 |
| `IlyaGusev/saiga_nemo_12b_gguf/saiga_nemo_12b.Q4_K_M.gguf` | **7.0 GB** | Llama | 12B Russian-tuned |
| `IlyaGusev/saiga_gemma3_12b_gguf/saiga_gemma3_12b.Q4_K_M.gguf` | **6.8 GB** | gemma3 | 12B Russian-tuned |
| `bartowski/mlabonne_Qwen3-8B-abliterated-GGUF/...Q4_K_M.gguf` | **4.7 GB** | qwen3 | 8B Q4 |
| `TheBloke/Unholy-v2-13B-GGUF/unholy-v2-13b.Q4_K_M.gguf` | **7.4 GB** | Llama | 13B Q4 |
| `bartowski/DarkIdol-Llama-3.1-8B.../...Q4_K_M.gguf` | **4.6 GB** | Llama | 8B Q4 |
| `Pixelber/.../Qwen3.5-9B.Q8_0.gguf` | **8.9 GB** | qwen35 | 9B Q8 |

**No GGUF variant of gemma-4-26b exists on disk.** The gemma-4 family is available only as MLX models.

---

## 5. Best GGUF Candidate: Qwen3.6-27B

`lmstudio-community/Qwen3.6-27B-GGUF/Qwen3.6-27B-Q4_K_M.gguf` is the strongest candidate:

- **Size:** 27B parameters, Q4_K_M quantization, 16 GB file
- **Estimated RAM (CPU mode):** 18.24 GiB — within M4 Max 36 GB headroom  
- **GPU when --gpu off:** 55 MiB (Metal completely free for mlx-whisper)
- **Arch:** Qwen3.5 — strong multilingual (RU/ES/EN), reasoning capable
- **Caveat:** ~2× slower than MLX on CPU for token generation (expected ~15-25 t/s vs ~60+ t/s MLX)

The llama.cpp runtime for this model is already installed and selected.

---

## 6. Alternative: Partial GPU Offload

llama.cpp also supports fractional offload: `--gpu 0.3` would put 30% of layers on Metal, keeping the majority on CPU. This could balance latency vs. contention:
- Reduce Metal usage from 15.62 GB → ~5 GB
- Still risks hash_table contention if mlx-whisper and partial-offload overlap
- Not recommended as primary fix — CPU-only is cleaner

---

## 7. CoreML Option

LM Studio does **not** currently expose a CoreML runtime option. The available runtimes are strictly GGUF (llama.cpp) and MLX. CoreML would require a separate inference stack outside LM Studio (e.g., swift-transformers or `mlx_lm` with CoreML export). Defer CoreML path — not actionable without significant work.

---

## 8. Pros / Cons Summary

| Option | Metal GPU freed | Latency impact | Implementation effort |
|--------|----------------|----------------|----------------------|
| **GGUF Qwen3.6-27B CPU** | 100% | ~2-3× slower token gen | Low — just reload with `--gpu off` |
| GGUF partial offload (30%) | ~70% | ~1.5× slower | Low — `--gpu 0.3` |
| CoreML (new stack) | 100% | Near-MLX speed | High — new runtime, no LM Studio support |
| MLX + mlx_lock (intra-proc) | 0% (done) | None | Done (Wave 48) |
| Keep status quo (MLX idle) | 0% | 1587→9500ms on overlap | Nothing |

---

## 9. Bench Plan for Wave 56

**Objective:** Quantify latency trade-off: GGUF CPU Qwen3.6-27B vs MLX gemma-4-26b under concurrent STT load.

**Preconditions:**
- User is NOT actively using LM Studio during bench run
- Unload `gemma-4-26b-a4b-it-optiq` first: `lms unload gemma-4-26b-a4b-it-optiq`
- Load GGUF model CPU: `lms load qwen/qwen3.6-27b --gpu off --identifier qwen3-6-27b-cpu`

**Test script:** extend `scripts/bench_r21.py` pattern:
1. Baseline: MLX gemma-4-26b (current) — 5 prompts × single-threaded
2. Treatment: GGUF Qwen3.6-27B CPU — 5 prompts × single-threaded
3. Concurrent: while GGUF CPU running, trigger `mlx-whisper` STT on test audio — measure STT latency
4. Compare: STT latency with GGUF CPU concurrent vs MLX concurrent

**Success metric:** STT p50 latency with GGUF CPU concurrent ≤ STT latency standalone (≤1600ms) — confirms Metal freed.

**Expected output:**
- GGUF CPU token generation: ~15-20 t/s (acceptable for post-processing rewrites)
- STT latency: should recover to ~1587ms baseline

---

## 10. Recommendation

**Run bench in Wave 56: YES**, with conditions:

1. **Primary candidate:** `qwen/qwen3.6-27b` GGUF with `--gpu off`
2. **Fallback candidate:** `saiga_gemma3_12b_gguf` (7 GB, gemma3 arch, Russian-tuned, CPU ~7 GiB RAM) — much lighter, faster CPU inference, RU quality possibly better for Krab Ear use case
3. **Decision gate:** if GGUF CPU STT latency recovers to ≤1800ms AND token generation ≥10 t/s → switch production default to GGUF CPU
4. **If bench shows unacceptable rewriter latency (>8s per call):** fall back to lighter 12B GGUF (saiga_gemma3_12b) — smaller models are faster on CPU even if quality lower

No new models need to be downloaded. All test candidates are already on disk.

---

## Files Referenced

- Current bench baseline: `docs/llm-bench-results-R22.md`  
- Bench script pattern: `scripts/bench_r21.py`  
- MLX lock (Wave 48): `KrabEar/core/mlx_inter_lock.py`  
- Crash report context: `~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips`
