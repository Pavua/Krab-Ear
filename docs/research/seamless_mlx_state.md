# SeamlessM4T v2 — State for Krab Ear Voice Assistant (Phase 1)

**Target hardware:** MacBook Pro M4 Max, 36 GB unified memory. **Date:** 2026-04-17.

## 1. Recommended deployment

**No native MLX port exists** as of April 2026. `mlx-community` on HuggingFace has `mlx-audio` (TTS/STT/STS library by Blaizzy) but **no `seamless-m4t-v2-large-mlx`** weights. No community quantization under `mlx-community/*` matches the model. `mlx-audio` does not ship a SeamlessM4T adapter.

**Recommended path:** **PyTorch + MPS via HuggingFace `transformers` `SeamlessM4Tv2Model`** (not the Meta `fairseq2` path). Rationale:
- `transformers` implementation is self-contained (no `fairseq2` Apple-Silicon pain, which has sporadic build issues with `libsndfile` and pre-built wheels).
- MPS backend on PyTorch 2.5+ supports the ops SeamlessM4T needs (conv1d, transformer attention, HiFi-GAN vocoder). `istft` support landed and is stable.
- Use `torch_dtype=torch.float16` on MPS to halve memory; run with `model.to("mps")`.

**Fallback:** CPU inference (float32) is viable on M4 Max for batch offline jobs but not for streaming.

## 2. Streaming feasibility

**SeamlessM4T v2 Large is batch-only.** For real-time conversation, use the **separate `facebook/seamless-streaming` model** (2.5B params, distinct checkpoint) built on EMMA (Efficient Monotonic Multihead Attention). It supports **simultaneous S2TT/S2ST on 101 source / 96 target / 36 speech-output languages** with monotonic decoding.

Chunk duration **not officially documented** — the streaming CLI (`streaming_evaluate`) exposes `--sample-rate 16000` but chunk-size constants live in the EMMA agent code. Community reports indicate effective policy steps of ~320 ms with wait-k scheduling; sub-200 ms TTFA is **not claimed** (Qwen3.5-Omni hits 200 ms, SeamlessStreaming targets ~1–2 s lag). **200 ms windows are not realistic** without retraining.

## 3. RU + ES quality estimates

- Both RU (`rus`) and ES (`spa`) are first-class: speech+text source and target.
- Paper: SeamlessM4T-v2 improves +5.2 ASR-BLEU over v1 (20.9 → 26.1 avg across FLEURS), +3.9 on X→eng S2ST, and beats Whisper-Large-v2+YourTTS cascade by 9.6 ASR-BLEU on CVSS.
- **No RU-specific WER numbers** published; RU is in the mid-resource tier of FLEURS and typically lands near the mean. Expect rough parity with Whisper-Large-v3 for RU ASR.
- **No ES-specific BLEU** published either; ES is high-resource and historically strong in Seamless. Iberian vs. Latin-American dialect split is not benchmarked separately — assume both covered but neutral/peninsular bias.
- Code-switching: **officially supported**, single-pass encoder handles mid-sentence language shifts (Meta highlighted this as a headline feature). Practical quality unverified for RU↔ES mid-sentence.

## 4. License

**CC-BY-NC 4.0** for both `seamless-m4t-v2-large` and `seamless-streaming`. Personal / research use OK. **Commercial use is blocked** — if Krab Ear ever ships paid tiers or B2B, replace or relicense.

## 5. Memory / performance

- Disk: ~9.3 GB (F32 safetensors, 2.3 B params).
- float16 inference: ~5.8 GB weights + activations; HF automated report cites ~7 GB minimum, **~10+ GB peak in practice**.
- int4: ~1.45 GB weights (no canonical HF quant yet for v2 Large).
- SeamlessStreaming: 2.5 B params; open GH issue #347 reports **OOM on 20 GB L4** during `streaming_evaluate` fp32. fp16 likely ~12–16 GB peak.
- On M4 Max 36 GB unified: both models fit comfortably with LM Studio + other apps loaded. No Metal-specific OOM reports in the Seamless repo.

Inference speed on Apple Silicon is **not publicly benchmarked**. Extrapolating from Whisper-Large-v3 on MPS (~1.5–3× RT on M3 Max), expect Seamless v2 Large at ~0.3–0.7× RT on M4 Max for offline S2TT, degraded by HiFi-GAN vocoder when doing S2ST.

## 6. Streaming server

**No ready WS/HTTP wrapper ships with `seamless_communication`.** The CLI is evaluation-only (`streaming_evaluate`). Meta's public demo (`seamless.metademolab.com`) is closed-source. **We write our own FastAPI/WebSocket wrapper** — standard pattern, ~1–2 days.

## 7. Top 3 risks

1. **License wall** — CC-BY-NC blocks monetization; any future paid tier forces swap to Whisper+NLLB+XTTS cascade or commercial license negotiation with Meta.
2. **Streaming latency** — SeamlessStreaming's real end-to-end lag on M4 Max MPS is unmeasured. If >1.5 s, full-duplex conversation UX degrades; mitigation is hybrid (Whisper chunked for ASR + batch Seamless for translation).
3. **MPS op coverage regression** — PyTorch MPS on macOS 26 Tahoe has open issues (pytorch/pytorch#167679). A torch upgrade could silently break one op in the UnitY2 stack. Pin torch version and add CI smoke test.

## 8. Wrap effort estimate

- PyTorch+MPS integration + warmup + fallback to CPU: **1 day**
- FastAPI WS wrapper for streaming (SeamlessStreaming agent, audio chunking, backpressure): **2 days**
- RU/ES quality smoke tests + glossary/code-switch eval harness: **1 day**
- Integration into Krab Ear backend service (new `VoiceAssistantService`, IPC methods): **2 days**

**Total: ~6 engineering days** for a working MVP, excluding UX tuning.

## Sources

- [facebook/seamless-m4t-v2-large (HuggingFace)](https://huggingface.co/facebook/seamless-m4t-v2-large)
- [facebook/seamless-streaming (HuggingFace)](https://huggingface.co/facebook/seamless-streaming)
- [facebookresearch/seamless_communication (GitHub)](https://github.com/facebookresearch/seamless_communication)
- [Streaming evaluate CLI README](https://github.com/facebookresearch/seamless_communication/blob/main/src/seamless_communication/cli/streaming/README.md)
- [OOM issue #347 on 20 GB L4](https://github.com/facebookresearch/seamless_communication/issues/347)
- [Seamless paper (arxiv 2312.05187)](https://pierrefdz.github.io/assets/publis/seamless/seamless.pdf)
- [Memory requirements discussion](https://huggingface.co/facebook/seamless-m4t-v2-large/discussions/41)
- [SeamlessM4T-v2 transformers docs](https://huggingface.co/docs/transformers/model_doc/seamless_m4t_v2)
- [Meta blog on code-switching](https://ai.meta.com/blog/seamless-m4t/)
- [CC-BY-NC 4.0 deed](https://creativecommons.org/licenses/by-nc/4.0/)
- [mlx-community on HuggingFace](https://huggingface.co/mlx-community)
- [mlx-audio (Blaizzy)](https://github.com/Blaizzy/mlx-audio)
- [PyTorch MPS Tahoe regression #167679](https://github.com/pytorch/pytorch/issues/167679)
