# Moshi MLX — State of the Art Research (April 2026)

Target: Krab Ear Voice Assistant Mode, English speech-to-speech on M4 Max 36 GB.

---

## 1. Recommended Model

**`kyutai/moshiko-mlx-q4`** (male voice) or **`kyutai/moshika-mlx-q4`** (female voice).

| Field | Value |
|---|---|
| URL | https://huggingface.co/kyutai/moshiko-mlx-q4 |
| Quantization | 4-bit per weight (MLX native) |
| Architecture | 7B params, Mimi codec @ 12.5 Hz, 1.1 kbps |
| Last updated | December 24, 2025 |
| License (weights) | **CC-BY-4.0** (commercial + derivative OK with attribution) |
| License (code) | MIT (Python), Apache 2.0 (Rust) |
| Languages | **English only** |
| Downloads last month | ~1,075 (moshiko), ~258 (moshika) |

Alternatives: `moshiko-mlx-bf16` (full quality, ~14 GB RAM), `moshiko-mlx-q8` (intermediate).

---

## 2. Inference Path

**Library:** `moshi-mlx` 0.3.0 (PyPI, released 2025-08-04, MIT).

```bash
pip install moshi_mlx   # Python >=3.10 (3.12 recommended)
```

Entry points:
- `python -m moshi_mlx.local -q 4` — CLI (no echo cancellation, no frame-skip)
- `python -m moshi_mlx.local_web -q 4` — bundled web UI at `localhost:8998`

Direct API: undocumented but accessible by importing `moshi_mlx` submodules. Key dependencies: `mlx`, `rustymimi` (Rust codec binding), `sounddevice`, `sentencepiece`, `safetensors`.

---

## 3. WebSocket Server Availability

**Partial — `local_web` uses HTTP/WS for the UI but is not a clean programmatic WS server.** Undocumented framing, bundled with a web UI, no auth, no reconnection logic.

For a **STT-only** path (Kyutai's newer Delayed Streams Modeling), there *is* a WS endpoint at `ws://localhost:8080/api/asr-streaming` with MessagePack framing — but that's Moshi-STT, not full-duplex speech-to-speech.

**Verdict:** Krab Ear must write its own thin WS bridge over the `moshi_mlx` Python API. No turn-key full-duplex server exists.

---

## 4. License Decision

- **Weights (CC-BY-4.0):** commercial use OK, attribution required in UI/docs.
- **Code (MIT + Apache 2.0):** no issues bundling into Krab Voice Gateway.
- No patents, no copyleft, no distribution restrictions. **Ship-safe.**

---

## 5. Russian Quality

**None available.** No RU fine-tune exists on HuggingFace or GitHub (searched 2026-04-17).

Prior art: `akkikiki/j-moshi-mlx` (Japanese, CC-BY-NC-4.0, non-commercial) proves fine-tunes are feasible via `kyutai-labs/moshi-finetune` (LoRA). Pre-training corpus is 7M hours *mostly English*, so zero-shot RU = broken accent + heavy hallucination expected.

**Decision for Phase 1:** English-only Voice Assistant. Defer RU fine-tune (est. 200+ hrs stereo dialogue data, weeks of H100 time).

---

## 6. Resource Benchmarks (M-series)

| Metric | Value (M3/M4) | Source |
|---|---|---|
| Theoretical latency | 160 ms | Kyutai paper |
| Practical latency | 200 ms | Community reports |
| RAM (q4, idle) | ~4–6 GB (STT variant, 1B); 7B full-duplex estimated ~8–12 GB | jeanjerome/moshi-stt-apple-installer |
| RAM (bf16) | ~14 GB | Derived from 7B × 2 bytes |
| Per-step compute | ~94 ms (STT 1B on M4 Max) | Apple installer docs |
| Tested hardware | MacBook Pro M3 (official), M4 Max (community) | |

**M4 Max 36 GB:** comfortably fits q4 full-duplex Moshi with plenty of headroom for Krab Ear's other services (Whisper, pyannote, LLM rewriter).

---

## 7. Top 3 Known Risks

1. **5-minute conversation buffer cap** — MLX + Rust implementations stop generating after ~5 min due to fixed-size buffers (official FAQ). Need session restart logic or upstream patch.
2. **No attention sink / quality degradation** — PyTorch theoretically unbounded, but *quality drops* on long streams. Plan session chunking.
3. **MLX dependency fragility** — open issue #63 shows `moshi-mlx` pins `mlx<0.18,>=0.17.2` aggressively; conflicts with newer MLX ecosystem libs (mlx-whisper, mlx-lm). Krab Ear already uses mlx-whisper → lock-file collision risk. Also: MLX server processes have documented unbounded KV-cache growth → kernel panics on long sessions.

Bonus risks: no echo cancellation (user must use headphones), English-only.

---

## 8. Integration Effort Estimate

**~5–7 person-days** to production-ready Voice Gateway integration:

| Task | Days |
|---|---|
| Write thin WS bridge wrapping `moshi_mlx` Python API (frame in, frame out) | 2 |
| 5-min session recycler (auto-restart, state stitching) | 1 |
| Resolve mlx version conflict with existing mlx-whisper (likely: separate venv or pin bump) | 1 |
| Voice Gateway protocol integration + EVENT_CONTRACT_V1 events | 1.5 |
| E2E test on M4 Max + CC-BY-4.0 attribution in UI | 1 |

**Total: 5.5 person-days** (optimistic). Add +2 days if dependency conflict needs upstream PR.

---

## Final Recommendation

Proceed with `moshiko-mlx-q4` for Phase 1 (English). Use `moshi-mlx` 0.3.0 library. Budget 1 week integration. Accept 5-min session limit as MVP scope. Defer RU to a later phase requiring fine-tuning infrastructure.
