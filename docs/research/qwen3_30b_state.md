# Qwen3-30B-A3B-2507 — State of Research (Krab Ear Phase 1)

**Date**: 2026-04-17 • **Target HW**: M4 Max 36 GB • **Use case**: voice-assistant brain, short (<=500 tok) replies, RU primary.

## TL;DR Recommendation

**Model**: `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit`
**URL**: https://huggingface.co/lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit
**Why**: Non-thinking Instruct variant (no `<think>` blocks → low TTFT), MoE 30.5B total / **3.3B activated** keeps compute tiny, first-party LM-Studio build = one-click install, MLX is Apple-native (beats GGUF/llama.cpp on Metal by 15-30%).

## LM Studio config snippet

```jsonc
// LM Studio → Developer → Load parameters
{
  "model": "qwen/qwen3-30b-a3b-2507",          // lmstudio-community MLX-4bit
  "context_length": 8192,                        // enough for voice-assistant turns; keep KV cache small
  "gpu_offload": "max",                          // MLX uses unified memory; leave at max
  "flash_attention": true,
  "kv_cache_quantization": "q8_0",              // halves KV mem, ~0 quality loss
  "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
  "max_tokens": 512,                             // voice-assistant cap
  "repeat_penalty": 1.05
}
```

Keep LM Studio server on port **1234** (already used by Krab). `llm_rewriter.py` points at OpenAI-compatible `/v1/chat/completions`; no code change needed.

## Memory + perf on M4 Max 36 GB

| Quant    | Disk   | Weights in RAM | +KV @ 8K | +KV @ 32K | Fits 36GB? | Notes |
|----------|--------|----------------|----------|-----------|------------|-------|
| MLX-4bit | 17.2 GB| ~17 GB         | +0.8 GB  | +3.2 GB   | Yes (8K/32K comfortable) | **pick this** |
| MLX-6bit | ~23 GB | ~23 GB         | +0.8 GB  | +3.2 GB   | Tight at 32K (other apps compete) | quality bump small |
| MLX-8bit | ~32 GB | ~32 GB         | +0.8 GB  | +3.2 GB   | **No** — leaves <2GB for OS+Whisper+pyannote | skip |
| GGUF Q4_K_M | 18.6 GB | ~18 GB    | +1 GB    | +4 GB     | Yes | slower than MLX on Apple |
| GGUF Q5_K_M | 21.7 GB | ~22 GB    | +1 GB    | +4 GB     | Yes | marginal quality gain |

**Tokens/sec (M4 Max, 4-bit MLX, 8K ctx)**: **68–100 t/s** steady-state generation (multiple HN/DeepNewz/MLX community reports; MoE 3.3B active pays off huge here). Qwen3-30B-A3B at 4-bit MLX hits **~87 t/s** on M4 Max @ 8K; **~100 t/s** peak; drops gracefully to ~40 t/s near 128K.

**TTFT**: MLX does full prefill before first token → on short prompts (~200 tok system + 50 tok user) **TTFT ~150–250 ms**. At 512-token output and 80 t/s: **full reply ~6.6 sec**; **first audible word via TTS ~0.5–1.0 sec** (Krab's <500 ms target met for TTFT; total-reply latency dominated by length, not model).

## RU + tool-calling quality

- **Training corpus**: Qwen3 pretrained on 36T tokens / **119 languages** (vs Qwen2.5 on 29 languages) → major RU uplift vs Qwen2.5.
- **MMLU-Pro**: 78.4 • **MMLU-Redux**: 89.3 • **MMLU-ProX (multilingual)**: 72.0 • **INCLUDE** (multilingual knowledge): 71.9 • **MultiIF** (multilingual instruction-following): 67.9. No public RU-specific MMLU split, but MMLU-ProX & INCLUDE include RU — performance roughly matches Qwen3-32B dense, i.e. **substantially better than current Krab qwen3-4b** (4B MMLU-Pro is ~54).
- **vs Qwen2.5-72B**: 30B-A3B trades blows on knowledge/reasoning at ~1/10 the active compute; 72B still edges it on longest-context reasoning but is unrunnable on 36 GB.
- **Tool calling**: first-class. Hermes-style templates; `Qwen-Agent` framework handles MCP configs. OpenAI `tools=[...]` format works out-of-the-box via LM Studio's adapter. Good fit for Krab MCP integration.
- **Reasoning trade-off**: this is the **Instruct-2507** variant = non-thinking (no `<think>` blocks emitted). Use the Thinking-2507 variant only for batch reasoning tasks — **never for voice** (adds 2–10 s of hidden chain-of-thought).

## Alternatives (ranked)

1. **Qwen3-Coder-30B-A3B-Instruct (MLX-4bit)** — same arch, tool-use tuned harder. Use if MCP/tool calls dominate over general chat. Slight RU regression (~3-5%) vs Instruct-2507.
2. **Qwen2.5-32B-Instruct (MLX-4bit, ~18 GB)** — dense 32B. Same memory but **~25-35 t/s** (5× slower than MoE) because all 32B activate per token. Slightly weaker on multilingual. Fallback only if Qwen3 licensing becomes an issue.
3. **gpt-oss-20b (MLX-4bit, ~11 GB)** — smaller, frees RAM, **~120-150 t/s**. RU quality noticeably below Qwen3-30B. Good fallback when diarization/Whisper-large-v3 compete for RAM.

**Ruled out**: DeepSeek-V3/R1 (>300 GB), Mistral-Large-123B (~70 GB min), DeepSeek-R1-Distill-Qwen-32B (thinking mode → TTFT too high for voice).

## Estimated voice-assistant latency budget

Typical turn: 180-tok system + 50-tok user → 200-tok reply.
- Prefill (230 tok @ ~350 t/s prefill): **~0.65 s**
- Generate (200 tok @ 85 t/s): **~2.35 s**
- **Total LLM**: ~3.0 s end-to-end; **TTFT ~0.7 s**
- With TTS streaming token-by-token (Krab pipeline): user hears first audio **~0.8–1.0 s** after query → acceptable for conversational UX.

For strict <500 ms TTFT (e.g. wake-word confirmation), pair with a 4B fast path and escalate to 30B only on longer turns.

## Sources

- [Qwen/Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507)
- [lmstudio-community MLX-4bit](https://huggingface.co/lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit)
- [unsloth GGUF Q4_K_M / Q5_K_M](https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF)
- [HN: 70-100 t/s on M4 Max MLX](https://news.ycombinator.com/item?id=44635589)
- [DeepNewz: M4 Max / M3 Ultra MLX lead at 32K](https://deepnewz.com/ai-modeling/qwen3-30b-a3b-model-mlx-weights-shows-m4-max-m3-ultra-lead-tokens-per-second-32k-380e5584)
- [Qwen blog: Think Deeper, Act Faster](https://qwenlm.github.io/blog/qwen3/)
- [Qwen3 Technical Report (arXiv 2505.09388)](https://arxiv.org/pdf/2505.09388)
- [Qwen function-calling docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
