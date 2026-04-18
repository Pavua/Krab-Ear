# Mistral Voxtral: STT with Reasoning — Phase 4.4 Research

**Date**: 2026-04-17  
**Context**: Evaluate Voxtral as a potential Krab Ear adapter for STT + integrated reasoning.

---

## Model Availability

### Voxtral Variants (Open Source, Apache 2.0)

**Exists**: Yes. [Mistral released Voxtral publicly](https://mistral.ai/news/voxtral).

- **Voxtral Mini 4B Realtime 2602**: 4B params (3.4B LM + 970M audio encoder) — [on HuggingFace](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602)
- **Voxtral 24B**: Production-scale variant
- **Voxtral TTS**: 4B text-to-speech (separate model, not relevant for STT)

**License**: Apache 2.0 — commercial use OK.

**Model Size**: ~8.9 GB (BF16 weights). Minimum 16 GB GPU VRAM required.

---

## MLX Port Status

**Available**: Yes.  
[mlx-community has converted multiple Voxtral variants](https://huggingface.co/mlx-community):

- `mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit` — 4-bit quantization
- `mlx-community/Voxtral-Mini-4B-Realtime-2602-fp16` — FP16 version
- `mlx-community/Voxtral-Mini-3B-2507-bf16` — Smaller 3B variant

**M4 Max 36GB**: Comfortably fits any variant (4-bit quant ~2–3 GB, fp16 ~7–8 GB).

---

## Architecture Summary

**Key finding**: Voxtral is **NOT pure STT**; it combines transcription + semantic understanding.

### What Voxtral Does

1. **Speech-to-text** (primary): Converts audio to text
2. **Semantic Q&A & summarization**: Built-in reasoning directly over audio (no post-LLM needed)
3. **Function calling**: Can trigger workflows based on spoken intent
4. **Language model integration**: Retains "text understanding capabilities" of Mistral Small 3.1 backbone

### Architecture Pattern

Hybrid approach: **audio encoder + language model decoder**. Not a pure STT pipeline; more akin to multimodal (speech + text) LLM.

**Language support**: 13 languages including RU, ES, EN. Multilingual by design.

### Latency Profile

- **Configurable delay**: 80ms (high WER) to 2.4s (offline quality)
- **Recommended**: 480ms — matches offline STT accuracy
- **Throughput**: >12.5 tokens/sec
- **Real-time capable**: <500ms delay with configurable trade-offs

---

## Licensing & Commercial Use

- **License**: Apache 2.0 ✓
- **Weights**: Open-source, on HuggingFace
- **Commercial**: Allowed
- **No rate-limiting or auth required** for local inference

---

## Integration Recommendation for Krab Ear

### Option A: New Parallel STT Adapter (RECOMMENDED)

Add `VoxtralSTTAdapter` as a fourth STT candidate (alongside Whisper, Parakeet, SenseVoice):

- **Pros**:
  - Native reasoning (Q&A, summarization) without separate LLMRewriter stage
  - Multilingual, proven latency (480ms at quality parity with Whisper v3)
  - MLX port ready for M4 Max
  - Lighter than 24B variant (4B still ~8.9 GB, but quantized fits better)

- **Cons**:
  - Larger footprint than whisper-small (quantize to 4-bit to save ~2.5 GB)
  - Reasoning is deterministic; not configurable like LLMRewriter
  - Relatively new model (Feb 2026) — fewer long-term benchmarks

- **Implementation approach**:
  ```
  - Create core/voxtral_adapter.py
  - Integrate into AudioEngine.transcribe() fallback chain
  - Use vLLM or transformers library for inference
  - LLMRewriter becomes optional when Voxtral is selected
  ```

### Option B: Replace LLMRewriter Stage

Make Voxtral the default post-STT reasoning layer:

- **Pros**: Unified reasoning pipeline
- **Cons**: Voxtral's reasoning is fixed (no custom prompts); overkill for users who don't need semantic understanding

**Verdict**: Not recommended. Keep LLMRewriter as-is; Voxtral should be orthogonal (STT choice, not post-processing).

### Option C: Skip (Not Useful Yet)

- **Verdict**: **Reject**. Voxtral's maturity (Feb 2026) + MLX support + reasoning capability make it worth a Phase 4.4 adapter.

---

## Effort Estimate

**If integrate as Option A (new STT adapter)**:

| Task | Days |
|------|------|
| Create adapter + integrate into AudioEngine | 1–1.5 |
| Test fallback chain + latency benchmarks | 1 |
| GUI model selector (optional) | 0.5 |
| Documentation + release notes | 0.5 |
| **Total** | **3–3.5 days** |

(Assumes MLX community ports are stable; no custom quantization needed.)

---

## Alternatives Ranked

### 1. **Qwen3-Omni-30B-A3B-Thinking** (Alibaba, 2026)
- **Pros**: Native reasoning (chain-of-thought); multimodal (text + audio + image + video)
- **Cons**: 30B params — requires 60+ GB VRAM; too heavy for M4 Max inference
- **Verdict**: **Skip for Phase 4.4**; revisit if 7B distilled available

### 2. **NVIDIA Canary-Qwen-2.5B** (STT only)
- **Pros**: 2.5B params; lightweight; SALM architecture (ASR + LLM)
- **Cons**: English-only; smaller model = lower accuracy than Voxtral Mini 4B
- **Verdict**: **Lower priority** than Voxtral Mini 4B

### 3. **OpenAI GPT-4o-Audio** (Cloud only)
- **Pros**: State-of-the-art reasoning + speech
- **Cons**: Requires internet; not offline-first
- **Verdict**: **Incompatible with Krab Ear's vision** (local-first)

### 4. **Qwen2-Audio (Alibaba)**
- **Pros**: Lightweight; multilingual; LALM (audio-language model)
- **Cons**: Audio-to-text-only; no reasoning; smaller than Voxtral
- **Verdict**: **Covered by Parakeet/SenseVoice**; no advantage

---

## Conclusion

**Mistral Voxtral Mini 4B Realtime 2602** is the best candidate for Phase 4.4:

✓ Proven STT performance (8.72% WER multilingual @ 480ms)  
✓ Built-in reasoning (Q&A, summarization, function-calling)  
✓ MLX-ready for M4 Max  
✓ Apache 2.0 + commercial OK  
✓ Multilingual (RU, ES, EN)  

**Recommendation**: Implement as new STT adapter (Option A). ~3 days effort. Include in Phase 4.4 roadmap alongside existing fallback chain.

---

## References

- [Mistral Voxtral announcement](https://mistral.ai/news/voxtral)
- [HuggingFace Voxtral Mini 4B](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602)
- [mlx-community Voxtral ports](https://huggingface.co/mlx-community)
- [Qwen3-Omni GitHub](https://github.com/QwenLM/Qwen3-Omni)
- [NVIDIA Canary-Qwen HuggingFace](https://huggingface.co/nvidia/canary-qwen-2.5b)
- [Technical Report (arXiv)](https://arxiv.org/html/2602.11298v2)
