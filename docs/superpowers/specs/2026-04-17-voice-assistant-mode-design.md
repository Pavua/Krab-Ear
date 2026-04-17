# Voice Assistant Mode — Design Spec

**Date:** 2026-04-17
**Status:** Approved by user (Pavel) 2026-04-17
**Author:** Claude Opus 4.7 (1M context) (orchestrator) + Gemini 3.1 Pro (visual design later)
**Phase:** 1 of 4 (roadmap: Voice Assistant → Live Translation → Call Automation → STT Adapters)

## 1. Goals

Add a new "Разговор с AI" tab in Krab Ear `.app` that enables full-duplex voice conversation with AI:

- **160-400ms end-to-end latency** (vs current 3-4s STT→LLM→TTS chain)
- **Full-duplex**: user can interrupt AI mid-sentence, AI adapts in real-time
- **Multilingual** (RU primary, EN, ES supported): best available open-source models (SeamlessM4T v2 для 100+ языков + qwen3-30b-a3b-2507 multilingual brain)
- **Shared context with Krab Telegram userbot**: same memory, same tools, same LLM brain — voice is "just another channel"
- **Three triggers**: GUI button (explicit), hotkey (hands-free), wake word (sci-fi)
- **Lazy-loaded engines**: 0 GB RAM при idle; only one engine loaded при active conversation
- **High availability**: Voice Gateway primary; Krab Ear local fallback if VG down

## 2. Non-Goals (explicit out-of-scope)

- **iOS companion access** — deferred to phase 1.x post-MVP. Architecture supports it (VG hosts engines), but Swift iOS client work is separate.
- **Phone call integration via voice assistant** — that's Phase 3 (Call Automation). For now, voice assistant operates only as local Krab Ear UI.
- **Multi-user / shared conversations** — single-user Pavel only. No auth, no session sharing.
- **Custom Moshi fine-tuning on RU dataset** — long-term separate research project (3+ months). Phase 1 uses SeamlessM4T для RU, not fine-tuned Moshi.
- **TTS voice cloning of Pavel's own voice** — not in MVP. May add CSM-Sesame / XTTS-v2 later for emotional TTS polish.

## 3. Architecture

### 3.1 Three-tier system

```
┌─────────────────────────────────────────┐
│ TIER 1: Krab Ear .app (UI/UX)           │
│   - "Разговор с AI" NSViewController    │
│   - Audio capture (AVAudioEngine)       │
│   - WS client to Voice Gateway          │
│   - Live transcript display             │
│   - Hotkey + Silero wake-word listener  │
└──────────────┬──────────────────────────┘
               │ WebSocket
               │   uplink: Opus PCM 80ms frames
               │   downlink: Opus PCM frames + JSON events
               ▼
┌─────────────────────────────────────────┐
│ TIER 2: Voice Gateway (orchestration)   │
│   - /v1/sessions/{id}/conversation NEW  │
│   - Language detection at session start │
│   - Engine routing: EN→Moshi, RU→Seam.  │
│   - Lazy-load + LRU eviction (1 active) │
│   - Krab agent proxy for LLM brain      │
│   - Audio I/O loop, full-duplex         │
└──────────────┬──────────────────────────┘
               │ HTTP / direct module call
               ▼
┌─────────────────────────────────────────┐
│ TIER 3: Krab agent (brain + tools)      │
│   - OpenClaw Gateway (LLM router)       │
│   - memory_engine (shared с Telegram)   │
│   - search_engine (Brave + summarize)   │
│   - mcp_client (tools registry)         │
│   - voice_engine (existing TTS/STT)     │
│   NEW: voice_channel_handler            │
└─────────────────────────────────────────┘
```

### 3.2 Conversation flow (RU example)

1. User triggers (hotkey "Right Option double-tap" OR wake word "Краб" OR GUI button).
2. Krab Ear `.app` opens WS to `ws://127.0.0.1:8090/v1/sessions/{id}/conversation`.
3. Voice Gateway detects language from first 1.5s of audio (silero-lang or simple heuristic).
4. VG lazy-loads SeamlessM4T v2 Large (10 GB RAM) — first session takes ~15s warmup.
5. SeamlessM4T processes 200ms audio chunks bidirectionally.
6. SeamlessM4T text-token output streams to Krab agent voice_channel_handler.
7. Krab agent processes via OpenClaw → qwen3-30b-a3b-2507 (or qwen3-4b если 30b занят).
8. LLM may call MCP tools (search, memory, calendar, krab_ear, voice_gateway).
9. LLM response text streams back to SeamlessM4T → audio chunks → WS → Krab Ear → speaker.
10. End-of-conversation: VG auto-saves transcript + LLM-generated summary to Krab agent's memory + Krab Ear's history (NDJSON entry with `mode: "voice_assistant"`).

### 3.3 Engine selection

| Engine | Use | Memory | Latency | License |
|--------|-----|--------|---------|---------|
| **Kyutai Moshi 7B (`kyutai/moshiko-mlx-q4`)** | EN-only conversations | 8-12 GB | 160-200ms | Code MIT/Apache 2.0; weights CC-BY-4.0 |
| **SeamlessStreaming 2.5B (Meta)** | RU + ES + 100+ языков, streaming-capable EMMA decoder | 12-16 GB | **1-2s lag** (NOT 200ms — research-corrected) | CC-BY-NC 4.0 — personal OK, commercial blocked |
| **SeamlessM4T v2 Large** | Batch fallback (file imports, не real-time) | 10 GB | batch-only | CC-BY-NC 4.0 |
| **`lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit`** | LLM brain (RU primary, MoE 3.3B active) | 17.2 GB | **68-100 t/s** на M4 Max | Apache 2.0 |
| **qwen3-4b-instruct-2507-abliterated** | LLM brain fallback (existing) | 4 GB | ~80 t/s | Apache 2.0 |
| **Silero wakeword + VAD** | "Краб" wake detection | 50 MB | <50ms | MIT |
| **XTTS-v2 / CSM-Sesame** | TTS quality polish (optional, phase 1.7) | 2 GB | 200ms | MPL-2.0 / Apache 2.0 |

### 3.4 Lazy load + LRU eviction

`MoshiEngine` and `SeamlessM4TEngine` inherit from new `LazyConversationEngine` base class:
- `load()` — async, progress callback, returns when ready
- `unload()` — frees model weights from RAM, KV cache cleared
- `is_loaded` property
- `last_used_at` timestamp

Voice Gateway maintains `_active_engine` (singleton). On new session:
- If session language matches `_active_engine.language` → reuse
- Else → unload current, load new (5-15s warmup)

## 4. Component Specifications

### 4.1 Voice Gateway: new `app/conversation/` module

```
app/conversation/
  __init__.py
  base.py                # LazyConversationEngine ABC
  moshi_engine.py        # MoshiEngine (Kyutai mlx-moshiko wrapper)
  seamless_engine.py     # SeamlessM4TEngine (Meta wrapper)
  language_detect.py     # silero-lang or langid wrapper
  brain_proxy.py         # async HTTP client to Krab agent voice channel
  ws_handler.py          # WebSocket protocol implementation
  session_state.py       # in-memory state per active conversation
```

New endpoints in `app/main.py`:
- `WS /v1/sessions/{id}/conversation` — full-duplex audio + JSON events
- `POST /v1/conversation/start` — explicit start (returns session_id)
- `POST /v1/conversation/{id}/end` — explicit end (triggers auto-save)
- `GET /v1/conversation/engines` — list loaded/available engines
- `PATCH /v1/conversation/{id}/runtime` — reuse existing pattern

WS message protocol (binary frames for audio, text frames for events):
- **Uplink** (client → VG):
  - Binary: Opus-encoded PCM 16kHz mono, 80ms frames
  - Text JSON: `{"type": "control", "action": "interrupt|end|push_to_talk_off", ...}`
- **Downlink** (VG → client):
  - Binary: Opus-encoded PCM 24kHz mono, 80ms frames (engine output)
  - Text JSON events:
    - `{"type": "stt.partial", "text": "...", "lang": "ru"}` — для UI live transcript
    - `{"type": "engine.loaded", "name": "seamless_m4t_v2"}`
    - `{"type": "tool.invoked", "tool": "search", "args": {...}}`
    - `{"type": "summary.ready", "text": "..."}` — at end-of-conversation

### 4.2 Krab agent: new `voice_channel_handler.py`

Adds new MCP tool category: `voice_assistant_*`. Examples:
- `voice_assistant_get_recent_dictations(n)` — read Krab Ear history
- `voice_assistant_transcribe_file(path)` — trigger Krab Ear import
- `voice_assistant_send_telegram(chat_id, text)` — bridge to Telegram
- `voice_assistant_make_call(phone)` — hook to Phase 3 (call automation)

Integrates with existing `model_manager` for model routing.

### 4.3 Krab Ear .app: new `ConversationViewController`

```
native/KrabEarAgent/Sources/KrabEarAgent/
  ConversationViewController.swift     NEW
  ConversationViewController+UI.swift  NEW (waveform, transcript, controls)
  ConversationViewController+WS.swift  NEW (URLSessionWebSocketTask)
  WakeWordListener.swift               NEW (Silero wakeword via CoreML)
  HistoryPanelController+VoiceTab.swift  NEW (tab integration)
```

UI elements:
- Visualizer: live waveform (input + output overlay, 2 colors)
- Transcript area: scrollable, live updates with `stt.partial` events
- Status indicator: "🟢 Слушает" / "🟡 Думает" / "🔴 Говорит"
- Controls: "Прервать AI" button (hotkey: Esc), "Завершить" button
- Settings drawer: language hint override, engine selector (auto/moshi/seamless), brain selector (qwen3-30b/qwen3-4b/openclaw)

## 5. Multilingual Quality Strategy (RU primary, EN, ES, +другие)

### 5.1 Russian (primary use case)
1. **Engine**: SeamlessM4T v2 Large — lossless multilingual native speech-to-speech.
2. **Brain**: qwen3-30b-a3b-2507 Q4_K_M — best open-weights LLM for RU on M4 Max 36GB.
3. **System prompt + memory**: reuse existing Krab agent's RU-tuned prompts and persistent memory.
4. **TTS fallback**: XTTS-v2 RU voice или RHVoice if SeamlessM4T audio quality insufficient.
5. **Wake word**: Russian-trained Silero wakeword "Краб" / "Привет".
6. **Future**: fine-tune Moshi on RU dataset (separate project, 3+ months).

### 5.2 English
1. **Engine option A**: Kyutai Moshi 7B (mlx-moshiko) — fastest (160ms), full-duplex, voice preserving. Best для casual conversation.
2. **Engine option B**: SeamlessM4T v2 — same engine как RU, чуть выше latency но consistency cross-language.
3. **Auto-routing**: language detect → if 100% EN → Moshi (faster); if mixed/RU/ES → SeamlessM4T.
4. **Wake word**: optional secondary "Hey Krab".

### 5.3 Spanish (covered by same engine)
1. **Engine**: SeamlessM4T v2 Large — native ES speech-to-speech (Latin American + Iberian Spanish supported).
2. **Brain**: qwen3-30b-a3b-2507 (multilingual training includes solid ES capability).
3. **System prompt**: Krab agent's prompts будут automatically адаптироваться по language detect (existing pattern in `userbot_bridge.py`).
4. **Wake word**: optional ES trigger "Cangrejo" (краб по-испански) — pluggable phrase config.
5. **TTS fallback**: XTTS-v2 ES voice (latin) или Piper ES если нужно.

### 5.4 Other languages (passive support)
SeamlessM4T v2 supports 100+ languages out-of-box. Conversations в FR/DE/IT/PT/ZH/etc. will work automatically — quality зависит от training data per language. Ни Moshi engine ни custom routing required для них (just SeamlessM4T fallback).

### 5.5 Code-switching (mid-sentence language change)
SeamlessM4T handles code-switching between training languages. Krab agent LLM (qwen3-30b) тоже handles mixed input. Tested как acceptance criteria (Section 11).

## 6. Hardware Budget (M4 Max 36GB)

| State | Krab Ear .app | Voice Gateway | Krab agent + LLM | OS + browser | Total |
|-------|-----|-----|-----|------|-------|
| Idle (no active conv) | 200 MB | 100 MB | 100 MB | 5 GB | ~5.5 GB |
| Active EN (Moshi) | 200 MB | 7 GB | 4 GB (qwen3-4b) | 5 GB | ~16 GB |
| Active RU (Seamless+30b) | 200 MB | 10 GB | 17 GB (qwen3-30b) | 5 GB | ~32 GB |
| Active RU + Wake word + dictation simultaneous | 250 MB | 10 GB | 17 GB | 5 GB | ~33 GB |

**Tight но ok на 36GB**. Heavy Chrome tabs / large Xcode projects must be closed during active RU conversation. macOS swap will engage if exceeded — degraded UX но не crash.

**Auto-eviction policy**: if user starts conversation while LM Studio holds another model — VG sends explicit unload request. If both 30b and 4b loaded — unload 4b first (LRU).

## 7. Triggers — concrete details

### 7.1 GUI button
- Tab "Разговор с AI" в Krab Ear `.app`
- Big "🎙 Начать разговор" button — explicit start, modal session
- Visible status throughout conversation

### 7.2 Hotkey
- **Right Option double-tap within 300ms** — start/end conversation
- Distinct from existing dictation (Right Option single hold)
- Configurable via Settings → Hotkeys (default mapping)

### 7.3 Wake word
- Silero wakeword model loaded at app launch (50MB RAM, low CPU)
- Trigger phrases: **"Краб"** (default) — Russian. Optional secondary: "Hey Krab" (English)
- Toggle in Settings → Аудио-пайплайн (off by default for privacy)
- Conflict handling: if dictation is recording, wake word listener pauses

### 7.4 iOS push (deferred to post-MVP)
- Architecturally enabled (VG already has `/v1/mobile/devices/{id}` plumbing)
- Implementation в отдельный roadmap item

## 8. End-of-conversation behavior

User triggers end (button, hotkey, "до свидания" command, or 30s silence).

1. VG sends `{"type": "summary.requested"}` to Krab agent.
2. Krab agent generates summary via qwen3-30b: 1-2 sentences in user's language.
3. Summary + full transcript saved to:
   - Krab agent's `memory_engine` (persistent, searchable from Telegram + future voice)
   - Krab Ear's history NDJSON (`{type: "voice_assistant", summary, transcript, lang, duration_sec, ...}`)
4. VG sends `{"type": "summary.ready", "text": "..."}` to Krab Ear.
5. UI shows summary + "Сохранено в историю" toast.
6. WS connection closes, VG marks engine `last_used_at = now()`.
7. После 5 минут idle → engine `unload()` to free RAM.

## 9. Privacy & Logging

- **Default**: every conversation auto-saved (history + memory).
- **Override**: "Приватный режим" toggle in Settings → не сохраняет ни transcript, ни summary, ни в memory; только VG audit log "session N from M to T, no content".
- **Wake word audio**: rolling 5s buffer, never persisted unless wake detected.

## 10. Phasing — 8 PRs over ~3-4 weeks

| PR | Title | Effort | Dep |
|----|-------|--------|-----|
| 1.1 | Voice Gateway: `MoshiEngine` + `LazyConversationEngine` base class + WS handler | M | — |
| 1.2 | Voice Gateway: `SeamlessM4TEngine` + language detect + engine routing | M | 1.1 |
| 1.3 | Krab Ear: `ConversationViewController` + WS client + UI (waveform, transcript) | M | 1.1 |
| 1.4 | Krab agent: `voice_channel_handler` + brain proxy в VG | M | 1.1, 1.3 |
| 1.5 | Triggers: GUI button + Right Option double-tap hotkey + Silero wake word | S | 1.3 |
| 1.6 | qwen3-30b-a3b-2507 setup в LM Studio + routing + auto-eviction policy | XS | 1.4 |
| 1.7 | (Optional) XTTS-v2 voice clone fallback if SeamlessM4T audio insufficient | M | 1.2 |
| 1.8 | E2E acceptance: RU + EN scenarios, recording → playback, summary→history | M | all |

**Approx total**: 8 PRs × avg 2-3 days each = 3-4 weeks of focused work.

## 11. Acceptance Criteria

Phase 1 considered done when:

1. Open Krab Ear `.app` → click "Разговор с AI" tab → click "🎙 Начать разговор" → speak Russian → AI replies in Russian within 1s.
2. Right Option double-tap from anywhere → conversation starts; double-tap again → ends.
3. Wake word "Краб" detected → conversation starts.
4. Mid-AI-response, user starts speaking → AI stops mid-sentence within 200ms (full-duplex).
5. Conversation transcript + summary saved to history (visible in Krab Ear "История" tab) and to Krab agent memory (queryable from Telegram).
6. RU conversation accuracy ≥ 95% on test corpus (10 sentences read aloud).
7. EN conversation latency p50 ≤ **300ms** (Moshi + WS bridge overhead — research-revised).
8. RU conversation **first-audio latency p50 ≤ 2.5s** (SeamlessStreaming 1-2s + qwen3-30b TTFT 0.5s — research-realistic).
8a. ES conversation accuracy ≥ 90% on test corpus; latency p50 ≤ 2.5s.
8b. Code-switching test: 5-sentence mixed RU↔EN conversation completes без crash, accuracy ≥ 80%.
8c. **Moshi long-session recycler**: auto-restart conversation после 4 min (avoid 5-min buffer kernel panic). User видит seamless transition.
9. Hardware: total RAM during active RU conversation ≤ 33 GB.
10. Privacy mode toggle works — no transcript persisted.

## 12. Open Questions / Future Work

| Topic | Decision pending | Defer to |
|-------|------------------|----------|
| iOS companion access | TBD — architecturally enabled, needs Swift iOS client work | Post-Phase 1 |
| TTS quality polish (XTTS-v2 vs CSM) | Wait until SeamlessM4T quality tested | PR 1.7 (optional) |
| Moshi RU fine-tuning | Long-term research, separate roadmap | Phase ε (research) |
| Cross-app actions (voice → trigger Krab Ear import / Voice Gateway call) | Implement minimal in Phase 1, expand in Phase 3 | Phase 3 |
| Multi-language single conversation (code-switching RU↔EN mid-sentence) | SeamlessM4T should handle, не verified | Phase 1.8 acceptance |

## 13. Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| SeamlessM4T RU audio quality below expectation | Medium | Fallback to XTTS-v2 RU voice (PR 1.7) |
| qwen3-30b too slow on M4 Max (>2s response) | Low-Medium | Auto-fallback to qwen3-4b for short queries |
| Memory pressure при concurrent dictation + voice assistant | Medium | Mutex: voice assistant pauses dictation listener and vice versa |
| Moshi MLX port stability (community fork) | Medium | Pin specific commit; have fallback to CPU mode |
| Voice Gateway becomes single-point-of-failure | Low (dev usage) | Krab Ear local fallback (Phase C from architecture) |
| **MLX version conflict** (`moshi-mlx` pins `mlx<0.18`, `mlx-whisper` newer) | High | (a) Pin both к compatible range, OR (b) split env: VG runs in moshi-mlx env, Krab Ear backend в mlx-whisper env, OR (c) Moshi via subprocess isolation |
| **Moshi 5-min buffer cap** kernel panics на long sessions | High | Auto-recycler: VG closes session at 4 min, transparently re-opens (criterion 8c) |
| **PyTorch MPS regression macOS 26** (open issue pytorch#167679) | Medium | Test on current macOS version; CPU mode fallback for SeamlessStreaming if MPS broken |
| **No SeamlessM4T MLX port** = PyTorch+MPS path | Medium | Performance impact tolerable (research: fp16 fits 36GB); rebenchmark in PR 1.2 acceptance |
| **CC-BY-NC license SeamlessStreaming** | Low (personal) | Future commercial offering — switch к Whisper-Large + Translation engine chain instead |

## 14. Success Metrics

- **Daily use**: Pavel uses voice assistant ≥ 3 times/week within 2 weeks of MVP.
- **Latency p50**: meets criteria (200ms EN / 500ms RU).
- **No regressions**: existing dictation + history + import flows unchanged.
- **CI**: new tests pass; total test count grows by 30+ new tests covering conversation engine, brain proxy, WS protocol.

---

## Cross-references

- **Phase 2** (Live Translation Overlay) will extend `SeamlessM4TEngine` from Phase 1 with language-pair routing.
- **Phase 3** (Call Automation) will use Voice Gateway's existing Twilio integration + Krab agent's voice_channel_handler.
- **Phase 4** (STT Engine Adapters) is independent backend work, parallel ongoing.
- Memory entry: `project_research_backlog_2026-04.md` — original tech survey.

## Research backing (all 2026-04-17)

- `/tmp/krab-ear-research/moshi_mlx_state.md` — `kyutai/moshiko-mlx-q4` confirmed; MIT/Apache code, CC-BY-4.0 weights; **5-min buffer cap**; **no WS server** (write own bridge); MLX version pin issue.
- `/tmp/krab-ear-research/seamless_mlx_state.md` — **no MLX port** (PyTorch+MPS only); batch-only M4T → use `seamless-streaming` 2.5B for real-time; **1-2s lag realistic**.
- `/tmp/krab-ear-research/qwen3_30b_state.md` — `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` 17.2GB; **68-100 t/s** на M4 Max; 200-token reply ~3s end-to-end; 119 languages pretrained.
- Krab agent integration map (sub-agent afbec26d) — voice_engine.py = Azure Edge TTS (cloud, irrelevant for VA — Moshi/Seamless имеют own TTS); MCP/OpenClaw/ChromaDB Memory abstractions ready; ~300-500 LOC handler estimated 4-6 days.
