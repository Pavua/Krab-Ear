# Research Documentation Index

**Location:** `/docs/research/`  
**Consolidated:** 2026-04-18  
**Source:** `/tmp/krab-ear-research/` (13 research files)  
**Purpose:** Permanent reference library for Voice Assistant Mode (Phase 1–4), audio engines, and infrastructure patterns.

---

## STT Models (Speech-to-Text)

### [voxtral_state.md](voxtral_state.md)
Mistral Voxtral: STT with integrated reasoning capability for Phase 4.4 research. Evaluates open-source Apache 2.0 STT variants for Krab Ear adapter integration.  
**Recommendation:** Phase 4 future consideration; STT-only adapters (Parakeet, SenseVoice) prioritized for Phase 1.

### [seamless_mlx_state.md](seamless_mlx_state.md)
Meta SeamlessM4T v2 for RU/ES/EN multilingual S2TT and S2ST. No native MLX port; use PyTorch+MPS via HuggingFace `transformers` (float16 on MPS). Streaming variant (2.5B EMMA) targets 1–2s TTFA, not real-time <200ms.  
**Recommendation:** Batch translation (Phase 2) preferred; streaming limited by TTFA guarantees.

---

## TTS Models (Text-to-Speech)

### [kokoro_tts_state.md](kokoro_tts_state.md)
Kokoro-82M: lightweight TTS (82M params, ~350–500 MB, Apache 2.0), 54 voices, 8 languages. Synthesizes at ~17x real-time (178 ms/sentence) on CPU. Russian not natively supported; fallback to macOS `say` or alternative.  
**Recommendation:** Fallback for Phase 1 when Moshi unavailable; not primary path.

---

## Voice Engine Models (Full-Duplex STT+TTS)

### [moshi_mlx_state.md](moshi_mlx_state.md)
Kyutai Moshi 7B: English-only speech-to-speech, MLX-native quantization (`kyutai/moshiko-mlx-q4` male, `moshika-mlx-q4` female). 1.1 kbps Mimi codec, ~12.5 Hz framerate. CC-BY-4.0 license. Uses `moshi-mlx` Python library; no turn-key WS server (must write custom bridge).  
**Recommendation:** Phase 1 confirmed for EN voice assistant responses; RU requires separate orchestration.

---

## LLM Brain Models

### [qwen3_30b_state.md](qwen3_30b_state.md)
Qwen3-30B-A3B-2507: MoE 30.5B total / 3.3B activated, MLX 4-bit quantization. Non-thinking Instruct variant (no `<think>` blocks → low TTFT). Recommended via lmstudio-community for <500 token voice-assistant replies.  
**Recommendation:** Confirmed Phase 1 brain via Krab agent OpenClaw gateway integration. Config: 8192 context, 512 max tokens, KV-cache q8_0.

### [qwen3_30b_benchmarks.md](qwen3_30b_benchmarks.md)
Detailed performance benchmarks: latency, memory, throughput for Qwen3-30B variants. Token/sec metrics by quantization, MoE expert routing overhead, KV cache configurations.  
**Recommendation:** Baseline for Phase 1 latency targets (sub-2s TTFT for voice responses).

---

## Wake Word Detection

### [wakeword_options.md](wakeword_options.md)
Ranked alternatives for Russian "Краб" trigger phrase: Porcupine (RECOMMENDED, <50ms, Metal GPU), Silero VAD (voice activity only, not wakeword), and KWS approaches. Porcupine requires macOS binding work (iOS-only official SDK).  
**Recommendation:** Porcupine via Python backend (`pvporcupine`, Picovoice SDK); official Swift binding is iOS-only.

### [picovoice_practical_guide.md](picovoice_practical_guide.md)
Picovoice ecosystem guide: Porcupine (wakeword), Leopard (STT), Gopher (intent), Orca (TTS), Cobra (voice activity). Python SDK available; commercial but accessible. Practical integration patterns for macOS voice apps.  
**Recommendation:** Porcupine integration path for Phase 1 wake-word fallback.

### [porcupine_swift_integration.md](porcupine_swift_integration.md)
Porcupine Swift integration research. CRITICAL: official iOS SDK is **iOS-only** (iOS 16+), no macOS target. Workarounds: Python backend (`pvporcupine`) or custom C binding. CocoaPods via custom podspec possible but unsupported.  
**Recommendation:** Implement in Python backend (`pvporcupine`), expose via IPC to Swift UI layer.

---

## Audio Engine Patterns

### [avaudioengine_patterns.md](avaudioengine_patterns.md)
AVAudioEngine best practices for real-time full-duplex audio: microphone capture (16 kHz, 80ms chunks) + speaker playback (24 kHz) simultaneously over WebSocket. Minimal 30–50 line Swift setup. Reduction Motion aware, Metal GPU acceleration on M4.  
**Recommendation:** Phase 1 audio loop foundation; 80ms chunk scheduling validated for <200ms latency.

---

## Platform & Infrastructure

### [seamless_install_guide.md](seamless_install_guide.md)
Step-by-step PyTorch+MPS setup for SeamlessM4T v2 on M4 Max macOS. HuggingFace `transformers` variant (not Meta `fairseq2` path). Float16 memory optimization, batch transcription examples, known issues (libsndfile on Apple Silicon).  
**Recommendation:** Batch translation fallback; streaming variant ≠ full-duplex (see SeamlessStreaming separate model).

### [permissions_diagnostic.md](permissions_diagnostic.md)
macOS TCC (Transparency, Consent, Credentials) diagnostics for Krab Ear. Current code signing: `com.antigravity.krab-ear`, ad-hoc signature. Path-based vs. bundle-ID caching. Diagnostic queries via `sqlite3 TCC.db`.  
**Recommendation:** Reference for user permission-grant troubleshooting; TCC reset workflow documented.

---

## Planning & Architecture

### [phase1_file_structure.md](phase1_file_structure.md)
Pre-plan reference: file structure mapping for Voice Assistant Mode Phase 1 across 3 repos (Krab Voice Gateway, Krab Ear, Krab agent). Directory layout, new modules, modified files. Cross-repo dependency graph.  
**Recommendation:** Architecture reference; superseded by actual PR implementations. Kept for session continuity.

---

## Usage Guidelines

- **Phase 1 (Foundation):** Reference `moshi_mlx_state.md` + `qwen3_30b_state.md` + `avaudioengine_patterns.md` + `wakeword_options.md`.
- **Phase 2 (Live Translation):** `seamless_mlx_state.md` + `seamless_install_guide.md`.
- **Phase 3 (Call Automation):** TBD (not yet researched in this library).
- **Phase 4 (STT Adapters):** `voxtral_state.md` + future SenseVoice / Parakeet research.
- **Troubleshooting:** `permissions_diagnostic.md` for macOS access issues; `picovoice_practical_guide.md` for Porcupine integration issues.

---

## Key Decisions Locked In (as of 2026-04-18)

1. **STT Engine:** mlx-whisper (Phase 1) → SeamlessM4T (Phase 2) → adapter ecosystem (Phase 4).
2. **Voice Synthesis:** Moshi EN-only (Kyutai) → fallback Kokoro / macOS `say` → SeamlessM4T S2ST (Phase 2).
3. **Brain LLM:** Qwen3-30B-A3B MLX 4-bit via LM Studio (Krab agent OpenClaw).
4. **Wake Word:** Porcupine "Краб" via Python backend.
5. **Audio Loop:** AVAudioEngine full-duplex at 80ms chunks, 16 kHz input, 24 kHz output.
6. **Orchestration:** Voice Gateway `/v1/sessions/{id}/conversation` WebSocket endpoint.

---

## File Statistics

- **Total files:** 13
- **Categories:** 6 (STT, TTS, Voice Engine, LLM, Wake Word, Infrastructure)
- **Est. read time (all):** 45–60 min
- **Last updated:** 2026-04-17 to 2026-04-18
