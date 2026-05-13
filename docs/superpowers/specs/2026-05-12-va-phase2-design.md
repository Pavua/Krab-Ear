# Voice Assistant Mode — Phase 2 Design Spec

**Date:** 2026-05-12
**Status:** Draft — pending product decisions on OQ-1, OQ-2, OQ-3
**Author:** Claude Sonnet 4.6 (orchestrator)
**Phase:** 2 of 4 — "Seeing & Remembering"
**Depends on:** `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md` (Phase 1)

---

## 1. Phase 1 Recap

Phase 1 shipped in late April 2026 (45+ PRs merged). The three-tier architecture is stable: Krab Ear `.app` (Swift UI) → Voice Gateway (Python orchestration) → Krab agent (LLM brain + tools). Core components live: `ConversationViewController`, `WakeWordListener` (Porcupine IPC bridge, hotkey primary), `SeamlessM4TEngine` (RU/ES 1–2s lag), `MoshiEngine` (EN, 160ms, 5-min auto-recycle), qwen3-30b-a3b-2507 MLX-4bit brain with shared memory/tools from Telegram channel.

The key limitation Phase 1 left open: the voice assistant is **text-only in context**. The user speaks → gets audio reply → can ask about documents, but cannot share a screen, image, or running application state in real-time. Additionally, each session starts "amnesia-fresh" relative to prior *voice* conversations (Krab agent memory has Telegram history but voice conversations are a thin NDJSON append — not semantically searchable from the VA side). Phase 1 latency targets were met for EN (p50 ~300ms) but RU first-audio remains ~1.5–2s which is acceptable for dialogues but noticeable on short replies.

---

## 2. Phase 2 Objectives

Five goals, in priority order:

| # | Objective | Value | Complexity |
|---|-----------|-------|------------|
| **2.1** | **Multimodal voice + screen** — user shares a screenshot mid-conversation, VA "sees" it and continues dialogue | High — "killer feature" turning VA into a pairing assistant | M |
| **2.2** | **Cross-session memory** — VA recalls prior voice conversations ("как мы обсуждали вчера...") via persistent, semantically-searchable store | High — makes VA feel continuous vs ephemeral | M |
| **2.3** | **Faster RU short replies** — reduce first-audio latency for ≤10-word responses from 1.5–2s → sub-1s | Medium — UX polish, not blocking | S |
| **2.4** | **Interrupt + barge-in reliability** — user can cleanly cut off a long AI reply by voice, AI stops within 200ms | Medium — Phase 1 spec'd this but full testing deferred | S |
| **2.5** | **Cross-lingual fluency** — RU↔ES↔EN mid-conversation switch without user declaring language or restarting session | Medium — RU+EN code-switching tested at 80% in Phase 1, ES not validated | S |

---

## 3. Tech Stack Changes

### 3.1 Multimodal brain: supergemma4-26b-abliterated-multimodal-mlx

R20 bench (2026-05-12) confirms `supergemma4-26b-abliterated-multimodal-mlx` (hereafter **supergemma-mm**):
- quality_avg = 1.00 on all text prompts (matches baseline)
- p50 = 4808ms text-only (vs baseline 1587ms) — **3.2× slower for text**
- Vision encoder is loaded at model init regardless of input type — this is the source of latency overhead
- Model is already downloaded and available in LM Studio

**Trade-off**: supergemma-mm provides vision capability at the cost of ~3s additional latency per turn *when used as text-only*. For Phase 2.1 this is acceptable when a screenshot is actually being analyzed; for pure-voice turns without images the 3× slowdown is a regression.

**Proposed approach (dual-model routing)**:
- Default brain: existing `gemma-4-26b-a4b-it-optiq` (baseline, 1587ms p50)
- When screenshot attached: switch to supergemma-mm for that turn only, then switch back
- Switch overhead: LM Studio model load = ~5–10s cold; solution is to keep both loaded simultaneously (~26GB each, 52GB total — exceeds 36GB limit on single machine)

**Alternative** (OQ-1 below): use supergemma-mm exclusively, accept 3× text latency as the new baseline for Phase 2. Simpler routing, no dual-load problem.

### 3.2 Screenshot capture: Cmd+Shift+4 space-click → VA pipe

Phase 1 has `SystemAudioCapture.swift` (ScreenCaptureKit). Phase 2.1 adds a parallel **screenshot injection path**:

```
User presses Cmd+Shift+4 (space, click window)
  └── macOS saves PNG to ~/Desktop/Screenshot ....png
        └── FSEvents watcher in ConversationViewController
              └── auto-detects new file in ~/Desktop matching pattern
                    └── encodes as base64, sends JSON IPC to backend
                          └── backend appends image to current conversation context
                                └── next LLM call includes base64 image in messages[]
```

Alternative trigger: dedicated hotkey (e.g. Cmd+Shift+I) opens NSOpenPanel for explicit file pick. Both can coexist.

The IPC method `conversation_inject_image` (new, Phase 2.1) accepts `{session_id, image_base64, mime_type}` and appends to the in-flight session's context window. Voice Gateway forwards the enriched context on the next LLM brain call.

### 3.3 Cross-session memory: RAG over voice history

Current state: voice conversations are saved to:
- Krab Ear NDJSON (`history.ndjson`, `mode: "voice_assistant"`, summary + full transcript)
- Krab agent `memory_engine` (ChromaDB-backed, also used by Telegram)

The gap: Voice Gateway's `brain_proxy.py` sends only *current session* context to the LLM. Prior voice sessions in ChromaDB are queryable from Telegram but not automatically recalled during a new voice session.

**Proposed**: add a retrieval step at voice session start and on semantic triggers:

```
Session start:
  1. brain_proxy.py calls memory_engine.query(recent_voice_sessions, k=5)
  2. Injects summaries as "prior context" system message fragment
  3. LLM sees: "Из прошлых разговоров: [summary1] ... [summary5]"

Mid-session trigger (optional, Phase 2B/2C):
  - When LLM output includes "как мы обсуждали" or "помнишь" patterns
    → re-query memory_engine with current topic
    → inject result into next turn context
```

**RAG vs full-history trade-off**:
- Full injection (all voice summaries) at session start: simple but grows unbounded; at 100+ sessions risks context-window overflow (qwen3-30b = 32K tokens)
- RAG (k=5 semantic search): controlled cost, but requires ChromaDB to be running (it is, as part of Krab agent)
- **Recommendation**: RAG with k=5, limited to voice sessions from last 30 days; full history queryable on demand via explicit "вспомни наш разговор про X" trigger

### 3.4 Faster RU short replies: streaming-first pipeline

Phase 1 latency profile for RU:
- SeamlessStreaming STT: ~300ms for first partial
- Brain TTFT (qwen3-30b): ~500ms
- SeamlessStreaming TTS: ~400ms for first audio frame
- **Total first audio: ~1.2–2s**

Options for sub-1s short replies:

**Option A — macOS `say` for ≤8-word responses** (low risk):
- Detect reply length before TTS routing: if LLM output ≤ 8 words → pipe to `say -v Milena` (system TTS, ~50ms)
- If > 8 words → SeamlessStreaming TTS (full quality)
- Trade-off: voice inconsistency (Milena vs SeamlessM4T voice)

**Option B — supergemma-mm as streaming brain** (medium risk):
- supergemma-mm p50=4808ms is *slower* than baseline for text — this option is counterproductive for RU latency unless text-only latency is reduced
- Not recommended for 2.3 unless R21 bench shows supergemma-mm text-only with vision encoder disabled

**Option C — Whisper STT + translate chain** (medium complexity):
- Replace SeamlessStreaming STT with mlx-whisper (RU, faster first partial ~150ms) + offline translation for TTS
- Risk: loses SeamlessStreaming's native speech-to-speech quality; introduces translation error layer
- Deferred to Phase 2C if A doesn't satisfy

**Recommendation**: implement Option A in Phase 2A as the quick win; revisit in Phase 2C.

### 3.5 VAD + barge-in: dedicated interrupt channel

Phase 1 spec'd full-duplex interrupt but testing was deferred (criterion 4 of Phase 1 acceptance). Current implementation relies on the WS uplink always being open; the Swift client sends a `{"type": "control", "action": "interrupt"}` JSON control frame when the user starts speaking while AI is outputting.

Gap discovered: `HotkeyDoubleTapDetector` and `WakeWordListener` both suppress VAD during recording. During VA conversation, there is no separate "listen-while-talking" path — the user must wait for the AI to finish.

**Phase 2.4 fix**: run a lightweight parallel VAD thread (Silero VAD, 50MB, already in `core/vad.py`) on the microphone input continuously during AI speech playback. On VAD trigger (>150ms speech detected), send interrupt immediately without waiting for a full frame. Voice Gateway cancels TTS stream, brain context records the interruption point.

### 3.6 Cross-lingual fluency: language re-detection per turn

Phase 1 detects language once at session start and routes to a fixed engine. Mid-conversation language switches break this.

**Phase 2.5 fix**: per-turn language detection (silero-lang, <10ms) on each STT partial. If detected language differs from session language for 2 consecutive turns → re-route that turn through appropriate path. Engine stays loaded (SeamlessM4T handles all RU/ES/EN natively). No engine swap needed for SeamlessM4T path; only relevant for Moshi (EN-only) sessions that switch to RU.

---

## 4. Implementation Plan

### Sub-phase 2A — Foundation (2 weeks, ~6 PRs)

| PR | Component | Description | Effort |
|----|-----------|-------------|--------|
| 2A.1 | VG `conversation/session_context.py` | Add `inject_image(session_id, base64)` to session state; propagate to next LLM call | S |
| 2A.2 | Krab Ear `ConversationViewController+Vision.swift` | FSEvents watcher for Desktop screenshots + `conversation_inject_image` IPC call; show "📎 Фото прикреплено" badge in UI | S |
| 2A.3 | VG `brain_proxy.py` | Route image-containing turns to supergemma-mm; text-only turns stay on baseline; model name configurable via setting `va_vision_model` | M |
| 2A.4 | Krab Ear `ConversationViewController+Barge.swift` | Parallel Silero VAD during AI playback; sends `interrupt` control frame on voice detection; Esc hotkey as manual fallback | S |
| 2A.5 | VG `language_detect.py` | Per-turn re-detection; on language change emit `{"type": "lang.changed", "from": "ru", "to": "en"}` event to client | S |
| 2A.6 | Tests | Unit: image injection + context serialization; integration: barge-in cancels TTS stream; E2E: RU→EN switch mid-session | M |

### Sub-phase 2B — Memory (1.5 weeks, ~4 PRs)

| PR | Component | Description | Effort |
|----|-----------|-------------|--------|
| 2B.1 | VG `brain_proxy.py` | At session start, call `memory_engine.query(voice_sessions, k=5)`, inject as system context fragment | M |
| 2B.2 | Krab agent `voice_channel_handler.py` | Expose `voice_memory_query(topic, k)` MCP tool so LLM brain can trigger mid-session memory lookups | S |
| 2B.3 | Krab Ear UI | "Из памяти: N сессий загружено" status line in ConversationViewController header | XS |
| 2B.4 | Tests | Memory injection E2E: prior session summary present in LLM context on second session | S |

### Sub-phase 2C — Latency + Polish (1 week, ~3 PRs)

| PR | Component | Description | Effort |
|----|-----------|-------------|--------|
| 2C.1 | VG `seamless_engine.py` | Short-reply routing: ≤8-word LLM output → `say -v Milena` for TTS; configurable threshold | S |
| 2C.2 | LM Studio config | Document `va_vision_model` setting; R21 bench of supergemma-mm text-only with vision encoder disabled (if LM Studio supports flag) | XS |
| 2C.3 | Docs + changelog | Update `docs/PHASE_1_VOICE_ASSISTANT_SETUP.md`, `CLAUDE.md`, memory | S |

---

## 5. Open Questions (product decisions required)

### OQ-1: Multimodal model strategy — dual-load vs single-model

**Question:** Accept 3× text latency (1587ms → 4808ms) by switching entirely to supergemma-mm as the sole brain, OR maintain dual-model routing (baseline for text, supergemma-mm for image turns)?

**Trade-offs:**
- Single supergemma-mm: simpler code, no routing logic, always vision-ready, but p50 ~5s even for "what time is it?" — noticeable regression
- Dual routing: baseline speed for voice-only, vision when needed, but requires LM Studio to swap models mid-conversation (~5s cold-load delay on first image)
- Partial alternative: keep supergemma-mm warm in LM Studio as a second slot (if 36GB allows; at 14GB quantized × 2 = 28GB, leaves 8GB for OS — very tight)

**Decision needed from product (Pavel):** which UX is acceptable?

### OQ-2: Memory scope — voice-only vs unified with Telegram

**Question:** Should VA cross-session memory pull from *all* Krab agent memory (Telegram + voice) or only from tagged `mode: "voice_assistant"` entries?

**Trade-offs:**
- Unified: "remember when I told you on Telegram about the API bug" works seamlessly; but ChromaDB has thousands of Telegram messages → retrieval may surface noisy, unrelated context; privacy blurring between channels
- Voice-only: clean separation, predictable context quality, "Telegram memory" only injected if user explicitly asks ("посмотри в Telegram историю")
- **Recommendation (draft):** voice-only by default + explicit `va_use_unified_memory: bool` setting (off by default)

### OQ-3: Screenshot capture trigger — automatic vs explicit

**Question:** Auto-detect new Desktop screenshots via FSEvents (zero friction, but fires whenever user takes *any* screenshot including unrelated ones) OR explicit in-app button/hotkey (Cmd+Shift+I)?

**Trade-offs:**
- Auto-detect: feels magical, "just works" for the "show me what's on screen" use case; but may accidentally attach screenshots from other workflows; user gets VA commentary on irrelevant screenshots
- Explicit: intentional, no false positives, but requires extra interaction mid-voice-conversation
- **Middle ground**: auto-detect + confirmation toast "📎 Krab видит новый скриншот — добавить к разговору? [Да/Нет]" with 5s auto-accept (configurable). This preserves magic while allowing rejection.

---

## 6. Hardware Budget (M4 Max 36GB)

| State | Krab Ear | VG (Seamless + engines) | LM Studio (brain) | OS + apps | Total |
|-------|----------|------------------------|-------------------|-----------|-------|
| Voice-only (text brain) | 250 MB | 12 GB | 17 GB (qwen3-30b) | 5 GB | ~34 GB |
| Vision turn (single swap) | 250 MB | 12 GB | 14 GB (supergemma-mm) | 5 GB | ~31 GB |
| **Dual-model preloaded** | 250 MB | 12 GB | 31 GB (both models) | 5 GB | **~48 GB — exceeds limit** |

**Constraint confirmed**: dual-model simultaneous load is not feasible on 36GB. Dual-routing requires cold-swap on first vision turn (~5–10s LM Studio load time). Subsequent vision turns within same session reuse loaded model if it hasn't been evicted.

**Mitigation**: LM Studio "warm keep-alive" setting for second model slot if memory allows; pre-load supergemma-mm at session start when `va_vision_enabled=true` setting is on (trade: 5s startup vs responsive first image turn).

---

## 7. Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| supergemma-mm text latency not acceptable as sole brain | High | Dual routing (OQ-1 decision) |
| FSEvents screenshot watcher fires on unrelated screenshots | High | Confirmation toast (OQ-3 middle ground) |
| ChromaDB query adds >200ms to session start | Medium | Async inject after session handshake; VA starts before memory is ready |
| Silero VAD false-positive barge-ins from background noise | Medium | Energy threshold guard (>30dB above noise floor required) + 150ms minimum duration |
| LM Studio model swap during active conversation feels broken | Medium | UI shows "🔄 Загружаю multimodal..." overlay; audio plays hold tone |
| Per-turn language re-detection causes engine thrash (EN→Moshi swap) | Low | Only re-route SeamlessM4T→Moshi if 3+ consecutive EN turns; threshold configurable |
| supergemma-mm vision encoder corrupts context for text-only prompts | Low | R19/R20 bench shows quality=1.00 on text prompts — encoder only active with image tokens present |

---

## 8. Non-Goals (Phase 2)

- **Video capture** — static screenshots only; live video feed is Phase 3+ or separate project
- **Document reading from file** — the existing `import_audio_file` IPC path handles audio files; Phase 2 adds image only
- **Multi-turn image history** — images are ephemeral per-turn context; VA does not "remember" prior images across sessions
- **iOS companion** — deferred per Phase 1 non-goals; architectural plumbing exists (VG endpoints) but no Swift iOS work
- **Voice cloning** — XTTS-v2 optional polish from Phase 1.7 is a separate line item; Phase 2 uses existing TTS stack
- **Concurrent multi-user sessions** — single-user (Pavel) only

---

## 9. Acceptance Criteria

Phase 2 considered done when all of the following pass:

1. **Vision (2.1)**: user takes macOS screenshot during active VA session → VA acknowledges image within 10s (including model load if cold) and answers a question about the screenshot content with ≥90% accuracy on 5 test images.
2. **Barge-in (2.4)**: user speaks for 200ms while VA is mid-reply → VA audio stops within 300ms and VA processes the interruption. Tested 10 consecutive times with ≤1 false negative.
3. **Memory recall (2.2)**: start a second VA session → ask "что мы обсуждали вчера?" → VA correctly references summary from prior session (manually seeded test). Latency from session start to first recalled context ≤ 3s.
4. **Short-reply latency (2.3)**: 5 RU single-word questions ("погода?", "который час?", ...) → first audio output within 1s p50 (macOS `say` path).
5. **Language switch (2.5)**: 10-turn RU conversation; at turn 6 switch to EN mid-sentence → next VA reply in EN within 1 turn; no crash, no engine restart. Reverse ES→RU switch also validated.
6. **No regressions**: existing dictation, live-subs, call automation flows unchanged. CI green. Test count grows by ≥40 new tests covering Phase 2 paths.
7. **Memory leak**: 30-minute continuous VA session → RAM delta ≤ 500MB vs session-start baseline (addresses MLX/SeamlessM4T accumulation risk from Phase 1 open items).

---

## 10. Cross-References

- Phase 1 spec: `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md`
- Phase 1 progress memory: `project_va_phase1_progress.md`
- R19 bench results: `docs/llm-bench-results-R19.md` (supergemma text quality baseline)
- R20 bench results: `docs/llm-bench-results-R20.md` (supergemma-mm vision latency, 4808ms p50)
- Phase 3 (Call Automation): `docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md`
- IPC API reference: `docs/IPC_API_REFERENCE.md` (new method `conversation_inject_image` to be added in 2A.1)
- Live translation Phase 2 design: `docs/superpowers/specs/2026-04-18-phase-2-live-translation-design.md` (separate Phase 2 track — live subtitles, not voice assistant)

---

## 11. Open Research Tasks (before implementation starts)

1. **R21 bench**: test supergemma-mm with image input on 5 representative screenshots (code, UI, document, chart, photo). Measure per-turn latency and output quality to establish realistic vision-mode latency expectations.
2. **LM Studio dual-slot feasibility test**: load both `gemma-4-26b-a4b-it-optiq` (baseline) and `supergemma4-26b-abliterated-multimodal-mlx` simultaneously in LM Studio on 36GB hardware. Measure actual RAM usage and whether swap causes Metal GPU pressure.
3. **Silero VAD barge-in prototype**: standalone Python script: play audio via sounddevice while simultaneously running `core/vad.py` on microphone input. Measure false-positive rate in quiet + ambient noise environments. Determines feasibility of Phase 2.4 before committing PR.

---

## 12. Phase 2A Status — 2026-05-13

**Skeleton created. Not yet wired to IPC or Swift UI.**

### Files created

| File | Lines | Purpose |
|------|-------|---------|
| `KrabEar/backend/va_multimodal.py` | ~230 | `MultimodalVAClient` + `VAMultimodalResult` skeleton |
| `KrabEar/tests/test_va_multimodal.py` | ~220 | 13 test stubs, all `@unittest.skip` until Wave 56+ integration |

### Class surface area (`MultimodalVAClient`)

```python
class MultimodalVAClient:
    def __init__(self, base_url, api_key="", vision_model=_DEFAULT_VISION_MODEL, timeout_sec=60.0)
    def send_with_image(self, text, image_path, conversation_history=None, system_prompt=None) -> VAMultimodalResult
    @staticmethod _encode_image(image_path) -> tuple[str, str]        # base64, mime_type
    @staticmethod _build_messages(text, image_b64, mime_type, ...) -> list[dict]
```

### Test suites (all skipped)

- `TestVAMultimodalResultContract` — 3 tests: `text_or_fallback` contract
- `TestImageEncoding` — 4 tests: PNG encode, JPEG mime, size guard, missing file
- `TestBuildMessages` — 4 tests: vision content shape, data URI format, history ordering, custom system prompt
- `TestSendWithImageHTTPLayer` — 5 tests: success path, connection error, missing file, timeout, model name in payload

### What is NOT done (Wave 56+ scope)

1. **IPC dispatch**: `va_send_with_image` / `conversation_inject_image` methods not added to `service.py` handler table — kept out intentionally to avoid premature API exposure before OQ-1 product decision.
2. **Swift ConversationViewController+Vision.swift**: FSEvents watcher + "📎 Фото прикреплено" badge — separate PR (spec §4 PR 2A.2).
3. **OQ-1 decision required**: dual-model routing vs. single supergemma-mm (see §5). Skeleton assumes dual routing (vision_model param is separate from baseline).
4. **Config registration**: `va_vision_enabled` / `va_vision_model` settings not yet in `DEFAULT_SETTINGS` — add when wiring IPC.
5. **Session context store** (`conversation/session_context.py` in Voice Gateway): image injection state per session — VG-side work, separate repo.

### Next steps for Wave 56+

1. Get OQ-1 product decision (single vs dual model strategy).
2. Add `va_vision_enabled: bool = False` and `va_vision_model: str` to `DEFAULT_SETTINGS` in `core/config.py`.
3. Wire `va_send_with_image` IPC method in `service.py` → delegates to `MultimodalVAClient`.
4. Implement `ConversationViewController+Vision.swift` (PR 2A.2).
5. Run R21 bench (listed above in §11) to validate actual vision-turn latency on hardware.
6. Remove `@unittest.skip` decorators and run full test suite.
