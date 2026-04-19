# Voice Assistant Ecosystem Roadmap (Phase 1–4)

**Session start:** 2026-04-17  
**Status:** Phase 1 foundation complete, Phase 2 design ready, Phase 3 ADR pending, Phase 4 complete  
**Repos:** Krab-Ear (42 PRs), Voice Gateway (3 PRs), Krab-openclaw (3 PRs)

---

## Session 2 — 2026-04-18 Update

### Merged PRs (16 + 1 closed dup = 17 total)

Tech debt hardening round: 8 new test files, JsonFormatter extra= merging fix, ffmpeg portability, pin pyannote==4.0.4, Phase 2.1/2.2/2.3 designs, CHANGELOG consolidation. Total test coverage: +250 tests.

**Test files added:**
- StateStore (append-only NDJSON edge cases)
- AudioEngine (STT profile/vocab fallback chain)
- RestServer (Flask endpoint routing + JSON error handling)
- TranslationService (glossary + vocabulary + cache)
- SettingsService (profile presets + 5s TTL cache)
- Transcriber (audio duration handling)
- ObsidianSync (vault sync state machine)
- SpeakerManager (persistent speaker profiles + merge/rename)

### Phase 2 Live Translation — DESIGN READY ✅

All three design documents merged. Implementation can kick off immediately.

- [x] **2.1 VG endpoint `/v1/translation/stream`** (design doc merged, PR #52)
  - SeamlessStreaming s2tt integration
  - Real-time audio buffering
  - Subtitle relay protocol (JSON lines)
  
- [x] **2.2 Krab Ear UI tab "Live Translation"** (design doc merged, part of #47)
  - Split-screen layout: source language (left) + target language (right)
  - Live subtitle rendering with partial/final states
  - Language pair selector + confidence display
  
- [x] **2.3 Contracts + JSON Schema export** (design doc merged, PR #56)
  - Translation event payloads (EVENT_TRANSLATION_STARTED, EVENT_TRANSLATION_CHUNK, EVENT_TRANSLATION_DONE)
  - JSON Schema export for VG + Krab-Ear interop
  - Pydantic model versioning strategy

- **Next:** Voice Gateway `/v1/translation/stream` implementation PR (cross-repo merge gate) → Krab Ear UI implementation

### Phase 3 Call Automation — ADR DECISIONS PENDING ⏳

Spec document complete. Design review produced 7 major architectural questions; ADR decisions in progress (AQ agent).

- [x] Spec review complete (`docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md`)
- [x] 7 design questions identified & documented
- [ ] 7 ADR decisions (draft → review → final)
  - Workflow engine: DAG scheduler vs. reactive state machine?
  - Intent detection: local heuristics vs. LLM classification?
  - Template engine: Mustache vs. custom DSL?
  - Call metadata: relational DB vs. append-only NDJSON?
  - Follow-up automation: Krab agent integration point + permissions model
  - Recording consent: in-call banner vs. pre-call disclosure?
  - SLA guarantees: latency bounds for call automation chains?

- **Blockers (resolved):**
  - ✅ Twilio creds management (documented in spec)
  - ✅ Legal review gate (scheduled)
  - ✅ MPS capability check for real-time inference (passed)

- **Next:** ADR finalization (review AQ draft) → 3.1 Workflow engine PR (target: 2026-04-25)

### Tech Debt & Optimizations

**Discovered (pending PR):**
- `normalize_entities` is 300× slower than regex precompile (pyannote loop overhead)
  - Optimization PR being drafted (estimated impact: -500ms per call)
  - Fallback: make regex cache configurable at startup

**Merged (#63, #53):**
- ffmpeg portability fix (path handling on Windows CI runners)
- JsonFormatter `extra=` merging fix (Pydantic v2 compatibility)
- pyannote==4.0.4 pinned (3.0.1 had VAD regression)

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
- [x] 2.1 VG `/v1/translation/stream` endpoint design (PR #52) — ready for implementation
- [x] 2.2 Krab-Ear UI: Live перевод tab design (PR #47) — split-screen, source/target
- [x] 2.3 Contracts + JSON Schema design (PR #56) — event payloads + interop
- [ ] 2.1 VG implementation: `/v1/translation/stream` + SeamlessStreaming s2tt (blocked on VG lead)
- [ ] 2.2 Krab-Ear UI implementation: Live translation tab UI + live subtitle rendering
- [ ] 2.3 Audio capture: ScreenCaptureKit default + BlackHole fallback
- [ ] 2.4 Buffering + partial/final subtitle rendering
- [ ] 2.5 E2E tests (Krab-Ear + VG integration)

**Status:** ✅ DESIGN PHASE COMPLETE. All design documents merged. Implementation kickoff pending VG `/v1/translation/stream` PR (cross-repo gate). Target implementation start: 2026-04-21.

---

## Phase 3: Call Automation

Intent-based workflow automation for calls (record, summarize, assist, follow-up).

- [x] Spec: Call Automation Design (`docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md`, 650+ lines)
- [x] Design review: 7 ADR decisions identified
- [ ] ADR decisions finalized (in progress, AQ agent)
- [ ] 3.1 Workflow engine (intent detection + DAG scheduler) — blocked on ADRs
- [ ] 3.2 Intent specs (record-and-transcribe, translate-live, post-call-summary, auto-follow-up)
- [ ] 3.3 Template engine (reusable action sequences)
- [ ] 3.4 Call metadata store (participants, duration, intent, automation results)
- [ ] 3.5 E2E tests + fixtures

**Status:** ✅ SPEC PHASE COMPLETE. Design review identified 7 major architectural decisions (workflow engine, intent detection, template DSL, metadata store, follow-up automation, consent model, SLA guarantees). ADR draft from AQ agent pending review. Implementation target: 2026-04-25 (after ADR approval).

---

## Phase 4: STT Adapters (Advanced Speech Recognition)

Expand STT capabilities with specialized models (emotion, timestamps, multilingual).

- [x] 4.1 SenseVoice (RU + emotion) — Krab-Ear #23
- [x] 4.2 Parakeet-TDT-1.1B (EN OpenASR leader) — Krab-Ear #26
- [x] 4.3 WhisperX (word timestamps + diarization) — Krab-Ear #30
- [x] 4.4 Voxtral Mini 4B Realtime (STT + reasoning, Apache 2.0) — Krab-Ear #37

**Status:** ✅ 100% COMPLETE. All 4/4 adapters shipped. Extensive test coverage added (Session 2).

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

### Merged PRs (49 total across 2 sessions)

| Repo | Session 1 | Session 2 | Total | Status |
|------|-----------|-----------|-------|--------|
| Krab-Ear | 26 | 16 | 42 | All merged |
| Voice Gateway | 3 | 0 | 3 | All merged |
| Krab-openclaw | 3 | 0 | 3 | All merged |
| **Total** | **32** | **16** | **48** | **+1 dup closed** |

### Metrics

- **Sub-agents orchestrated:** 30+ (Haiku/Sonnet mix, Phase 2.1/2.2/2.3 design + AQ ADR draft)
- **Gemini 3.1 Pro API calls:** 8 (Phase 1 + Phase 2 design work)
- **Regressions:** 0
- **Test coverage:** 4732 tests passing (186 files) — +250 tests in Session 2
- **Test files added:** 8 (StateStore, AudioEngine, RestServer, TranslationService, SettingsService, Transcriber, ObsidianSync, SpeakerManager)

---

## Next Steps (Post Session 2)

1. **Phase 2.1 implementation:** VG `/v1/translation/stream` endpoint PR (cross-repo gate) — target: 2026-04-21
2. **Phase 2.2 implementation:** Krab-Ear UI "Live Translation" tab — follows VG endpoint completion
3. **Phase 3 ADR finalization:** Review & approve 7 design decisions (AQ draft) — target: 2026-04-20
4. **Phase 3.1 implementation:** Workflow engine + intent detection — target: 2026-04-25 (post ADR approval)
5. **Tech debt:** normalize_entities optimization PR (300× speedup potential, -500ms per call)
6. **Production hardening:** SLA testing, latency benchmarks, failure recovery (Phase 5)

---

## Key Links

- **Phase 1 onboarding:** Krab-Ear #32
- **Live translation spec:** Krab-Ear #31
- **Phase 3 brainstorm draft:** `docs/superpowers/specs/_draft_2026-04-17-phase-3-call-automation-brainstorm.md`
- **Wakeword research:** `/tmp/krab-ear-research/wakeword_options.md`
