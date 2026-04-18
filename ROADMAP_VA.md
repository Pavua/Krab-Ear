# Voice Assistant Ecosystem Roadmap (Phase 1–4)

**Session start:** 2026-04-17  
**Status:** Phase 1 foundation complete, Phase 4 4/5 adapters shipped  
**Repos:** Krab-Ear (26 PRs), Voice Gateway (3 PRs), Krab-openclaw (3 PRs)

---

## Phase 1: Voice Assistant Mode (Foundation)

Interactive conversational agent using local LLM + low-latency speech I/O.

- [x] 1.1 Moshi engine (Voice Gateway #9)
- [x] 1.2 SeamlessStreaming engine (Voice Gateway #11)
- [x] 1.3 ConversationViewController UI (Krab-Ear #24)
- [x] 1.4 voice_channel_handler (openclaw #18)
- [x] 1.5 Triggers: double-tap hotkey + Porcupine wakeword (Krab-Ear #29 + #34 follow-up)
- [x] 1.6 qwen3-30b LM Studio routing (openclaw #21)
- [ ] 1.7 XTTS-v2 voice clone (optional, deferred)
- [x] 1.8 E2E tests + fixtures (Krab-Ear #27 + #28)

**Status:** Fully shipped. Pending: Porcupine setup (see User blockers).

---

## Phase 2: Live Translation Overlay

Real-time speech-to-speech translation for multilingual conversations.

- [x] Spec: Live Subtitle Relay (Krab-Ear #31, 577 lines)
- [ ] 2.1 VG `/v1/translation/stream` endpoint + SeamlessStreaming s2tt
- [ ] 2.2 Krab-Ear UI: Live перевод tab (split-screen, source/target)
- [ ] 2.3 Audio capture: ScreenCaptureKit default + BlackHole fallback
- [ ] 2.4 Buffering + partial/final subtitle rendering
- [ ] 2.5 E2E tests (Krab-Ear + VG integration)

**Status:** Design complete, implementation pending. Blocker: VG `/v1/translation/stream` not yet implemented.

---

## Phase 3: Call Automation

Intent-based workflow automation for calls (record, summarize, assist, follow-up).

- [ ] 3.1 Workflow engine (intent detection + DAG scheduler)
- [ ] 3.2 Intent specs (record-and-transcribe, translate-live, post-call-summary, auto-follow-up)
- [ ] 3.3 Template engine (reusable action sequences)
- [ ] 3.4 Call metadata store (participants, duration, intent, automation results)
- [ ] 3.5 E2E tests + fixtures

**Status:** Design document ready (`docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md`). Open design questions: 7 major decisions needed. Brainstorm session TBD.

---

## Phase 4: STT Adapters (Advanced Speech Recognition)

Expand STT capabilities with specialized models (emotion, timestamps, multilingual).

- [x] 4.1 SenseVoice (RU + emotion) — Krab-Ear #23
- [x] 4.2 Parakeet-TDT-1.1B (EN OpenASR leader) — Krab-Ear #26
- [x] 4.3 WhisperX (word timestamps + diarization) — Krab-Ear #30
- [x] 4.4 Voxtral Mini 4B Realtime (STT + reasoning, Apache 2.0) — Krab-Ear #37

**Status:** COMPLETE. All 4/4 adapters shipped.

---

## Infrastructure & Supporting Services

### TTS & Audio

- [x] TTS: Silero (RU) + Kokoro (EN) dual-engine (Krab-Ear #33)
- [x] Audio pipelines: mlx-optimized engines for real-time performance
- [x] Kokoro TTS integration + voice selection UI (Krab-Ear #33)

### Documentation & Onboarding

- [x] Phase 1 onboarding guide (Krab-Ear #32, 476 lines)
  - Covers: Moshi setup, hotkey binding, first conversation, troubleshooting
- [ ] Production deployment docs (pending)
- [ ] XTTS-v2 voice clone guide (optional, deferred)

---

## User Blockers (Manual Actions Required)

These must be completed by the user before Phase 1 is fully functional:

- [ ] **TCC GUI cleanup:** Grant Accessibility + Microphone permissions via System Settings → Privacy
- [ ] **Install moshi-mlx 0.3.0** in Voice Gateway venv:
  ```bash
  pip install moshi-mlx==0.3.0
  ```
- [ ] **Install torch + transformers** for SeamlessStreaming in VG:
  ```bash
  pip install torch transformers
  ```
- [ ] **Download qwen3-30b-a3b-instruct-2507** in LM Studio (or equivalent qwen3-30b variant)
- [ ] **Picovoice Console setup:**
  1. Create free AccessKey at https://picovoice.ai/console/
  2. Train custom `.ppn` model for keyword "Краб"
  3. Add `ppn` path to Krab-Ear settings
- [ ] **Uncomment Porcupine dependency** in `native/KrabEarAgent/Package.swift`:
  ```swift
  .package(url: "https://github.com/Picovoice/porcupine-swift.git", from: "3.0.0")
  ```

---

## Research & Reference Docs

Comprehensive research summaries available in `/tmp/krab-ear-research/`:

| Document | Purpose |
|----------|---------|
| `moshi_mlx_state.md` | Moshi v0.1 architecture, inference patterns, expected latency |
| `seamless_mlx_state.md` | SeamlessStreaming setup, quantization options, install guide |
| `qwen3_30b_state.md` | qwen3-30b model selection criteria, hardware requirements |
| `wakeword_options.md` | Silero (VAD only) vs. Porcupine (true wakeword, recommended) |
| `avaudioengine_patterns.md` | macOS AVAudioEngine best practices for low-latency streaming |
| `kokoro_tts_state.md` | Kokoro v0.19 EN performance, Silero RU fallback setup |
| `voxtral_state.md` | Voxtral Mini 4B: Apache 2.0, 13 langs, streaming capable, 4B params |

---

## Delivery Summary

### Merged PRs (32 total)

| Repo | Count | Status |
|------|-------|--------|
| Krab-Ear | 26 | All merged |
| Voice Gateway | 3 | All merged |
| Krab-openclaw | 3 | All merged |

### Metrics

- **Sub-agents orchestrated:** 20+ (Haiku/Sonnet mix)
- **Gemini 3.1 Pro API calls:** 5 (design work)
- **Regressions:** 0
- **Test coverage:** 4482 tests passing (178 files)

---

## Next Steps (Post Phase 1)

1. **Phase 2 kickoff:** Implement VG `/v1/translation/stream` endpoint (target: 2026-04-21)
2. **Phase 3 brainstorm:** Schedule design session for call automation intents
3. **Phase 4.4:** Voxtral integration (pending demand signal)
4. **Production hardening:** SLA testing, latency benchmarks, failure recovery

---

## Key Links

- **Phase 1 onboarding:** Krab-Ear #32
- **Live translation spec:** Krab-Ear #31
- **Phase 3 brainstorm draft:** `docs/superpowers/specs/_draft_2026-04-17-phase-3-call-automation-brainstorm.md`
- **Wakeword research:** `/tmp/krab-ear-research/wakeword_options.md`
