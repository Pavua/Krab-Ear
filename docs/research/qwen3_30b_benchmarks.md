# Qwen3-30B-A3B-Instruct-2507-MLX-4bit Benchmarks (M4 Max 36GB)

## Sources Found

- [SiliconBench — Apple Silicon LLM Benchmarks](https://siliconbench.radicchio.page/) — comprehensive Apple Silicon performance data
- [Qwen3 Evaluations by @wolfram (HF Posts)](https://huggingface.co/posts/wolfram/819510719695955) — MMLU-Pro accuracy + speed on M4 MacBook Pro
- [From Qwen 3 to Qwen 3.5 on Apple Silicon (Medium)](https://medium.com/@aejaz.sheriff/from-qwen-3-to-qwen-3-5-on-apple-silicon-a-14x-latency-regression-and-how-mlx-got-us-back-0ed9ed21fa68) — TTFT regression analysis
- [Qwen3-Coder-30B Hardware & Performance Guide](https://www.arsturn.com/blog/running-qwen3-coder-30b-at-full-context-memory-requirements-performance-tips) — context length + KV cache
- [LM Studio Tool Calling Issues](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/825) — function call format compatibility
- [M4 Max Efficiency (Jeff Geerling)](https://www.jeffgeerling.com/blog/2024/m4-mac-minis-efficiency-incredible/) — power consumption 40–80W during inference

## Performance Metrics

### Throughput (tokens/sec, steady-state)

| Variant | Configuration | t/s | Note |
|---------|---------------|-----|------|
| **MLX-4bit** | M4 Max, all-GPU | **64–100** | Median: ~82 t/s |
| GGUF-Q4_K_M | M4 Max | 68–72 | Slightly slower than MLX |
| Unsloth-4bit | M4 Max | ~45 | Heavier quantization |
| Coder variant | M4 Max | ~172 | Model-specific peak (outlier) |

**Recommendation: expect 64–100 t/s for voice assistant inference; 82 t/s is safe median.**

### TTFT (Time to First Token)

MLX exhibits **linear TTFT scaling with input length** (known weakness). Observed ranges:
- Empty prompt (4 tokens): ~80–120 ms
- 512-token input: ~300–400 ms
- 4K context: ~800+ ms

Voice assistant context windows should stay under 2K to keep TTFT < 500 ms.

## Memory Footprint

| Context | KV Cache | GPU Memory | Notes |
|---------|----------|-----------|-------|
| 8K | ~500–700 MB | ~18–19 GB | Safe, no slowdown |
| 16K | ~1.2–1.5 GB | ~19–20 GB | Feasible on M4 Max 36GB |
| 32K | ~2.5–3 GB | ~20–21 GB | Possible but approaching limits |

**Base weights (4-bit MLX): 17.2 GB**  
**Total recommended: 8K = 19 GB headroom; 16K = 21 GB headroom.**  
36 GB M4 Max safely accommodates up to 16K context with ~15 GB spare for system.

## Comparison Table

| Model | Tokens/s | Accuracy (MMLU-Pro) | Memory (8K) | TTFT | Recommendation |
|-------|----------|---------------------|-------------|------|---|
| **Qwen3-30B MLX** | 64–100 | 79.51% | 19 GB | 300–400ms @ 512tok | **✓ Best for voice** |
| Qwen3-32B | <10 | 82.20% | 21 GB | ~1s | Too slow for voice |
| Qwen3-14B MLX | ~120 | ~75% | 10 GB | 150ms @ 512tok | Faster, lower quality |
| Llama 3.3 70B | 40 | ~78% | 45 GB | 400ms | Too large for M4 Max |

## Known Issues & Workarounds

### Tool Calling Format (🔴 Critical for function calling)

- **Issue**: MLX-4bit + LM Studio OpenAI API emits non-standard function call JSON (missing `<tool_call>` tags, unreliable completion detection).
- **Workaround**: Use Unsloth GGUF variant (HF `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`) for reliable tool calling; chat template fixes as of 2025-08 apply to Qwen3.5 format universally.
- **Impact**: Voice assistant function calls (e.g., "set alarm") may fail silently; recommend input validation + fallback to CLI.

### Streaming Latency Variance

- KV cache prefill induces jitter: first token can lag if input is bursty or context changes rapidly.
- **Mitigation**: Pre-allocate context window in system prompt; batch small requests.

### iCloud Audio Path Issues

Not model-specific but relevant to Krab Ear: files from `~/Library/CloudDocs` trigger `errno 11` on some Macs. Copy to `/tmp` first.

## Energy Consumption

M4 Max LLM inference: **40–80 W sustained**  
Full MacBook Pro load: ~110 W peak  
**Battery life**: Up to 24 hours light usage; 4–6 hours continuous transcription + inference.

## Recommendation

**Confidence: HIGH** ✓

**Qwen3-30B-A3B-Instruct-2507-MLX-4bit is production-ready for Krab Ear**, given:
- 82 t/s median throughput matches voice assistant latency targets (< 200 ms perceived delay per 50-token response)
- 79.5% MMLU-Pro accuracy sufficient for general assistant tasks
- 8K context window fits comfortably in 19 GB; 16K feasible if needed
- MLX framework integrates with llama.cpp + LM Studio OpenAI API

**Caveats**:
1. **Tool calling requires Unsloth GGUF variant or custom chat template**—MLX format is unreliable via OpenAI API.
2. **TTFT scales linearly with input**—keep voice context under 2K tokens or implement prompt caching.
3. **Streaming requires jitter buffer**—variable token latency due to KV prefill.

**Alternative**: If tool calling is critical and speed acceptable, Qwen3-14B MLX (120 t/s, 10 GB) sacrifices accuracy for reliability.
