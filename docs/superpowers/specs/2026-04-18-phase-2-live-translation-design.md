# Live Translation Overlay — Design Spec

**Date:** 2026-04-18  
**Status:** Draft (approved pending brainstorm session — open questions require user input)  
**Author:** Claude Opus 4.7 (1M context) orchestrator  
**Phase:** 2 of 4 (roadmap: Voice Assistant → **Live Translation** → Call Automation → STT Adapters)

---

## 1. Goals

Add real-time bilingual translation UI overlay to Krab Ear, enabling two distinct use cases:

1. **Meeting Overlay (system audio transcription + live translation)**
   - Capture system audio from macOS (Zoom, Teams, Google Meet, etc.)
   - Live transcription in original language (RU, EN, ES primary)
   - Side-by-side translated text streamed in parallel
   - Target latency: p50 ≤ 1.2s (original | translation pair)

2. **Bilingual Dictation Mode**
   - User speaks mixed RU↔EN or RU↔ES mid-sentence (code-switching)
   - Transcript auto-detects language per phrase
   - Translation to user's "target language" (configurable)
   - Real-time bilingual vocabulary suggestions from glossary

3. **Shared engine with Phase 1**
   - Reuse `SeamlessStreamingEngine` (s2tt mode) from Voice Assistant Phase 1
   - New VG endpoint `/v1/translation/stream` for streaming mode
   - Same hardware budget constraints apply (M4 Max 36GB)
   - Fallback to local STT (mlx-whisper) if VG unavailable

---

## 2. Non-Goals

- **Floating ribbon overlay** (deferred to v1.1) — v1 uses extended "Live Translation" tab with side-by-side panel layout
- **Real-time subtitle burn-in to screen recording** — no direct video/screen mutation; translation text exported separately
- **Cross-app input injection** (e.g., "type translation into active app") — deferred to Phase 3 (Call Automation)
- **Document/PDF translation** — scope is real-time audio streaming only; document pipeline separate
- **Voice cloning of translated audio** — SeamlessStreaming provides synthetic speech, no custom voice training
- **Custom glossaries per-domain** — v1 uses global glossary only; per-meeting/per-domain deferred
- **Translation memory / TM databases** — translation cache exists (Phase 1), but no external TM integration

---

## 3. Architecture

### 3.1 Relationship to Phase 1 (Voice Assistant Mode)

Phase 2 **extends** Phase 1's architecture:

```
Phase 1 (Разговор с AI):
  Krab Ear .app
    ↓ WS uplink (user audio PCM)
  Voice Gateway
    ↓ SeamlessM4T / Moshi STT → text
  Krab agent
    ↓ LLM brain (qwen3-30b)
    ↑ Response text → TTS

Phase 2 (Live Translation):
  Krab Ear .app
    ↓ WS uplink (system audio PCM OR user audio)
  Voice Gateway
    ↓ SeamlessStreaming.s2tt (streaming STT + TTS in target lang)
    ↓ Original + Translation text (dual-stream)
  Krab Ear UI (new "Live Перевод" tab)
    Display: [Original text] | [Translated text]
    (later: floating ribbon v1.1)
```

**Key difference:** Phase 2 does NOT route through Krab agent LLM. Translation is pure SeamlessStreaming S2TT (speech-to-speech-with-text-output), deterministic and low-latency.

### 3.2 Three-tier system (adapted for translation)

```
┌──────────────────────────────────────────┐
│ TIER 1: Krab Ear .app (UI)               │
│   - New "Live Перевод" tab               │
│   - Audio capture (ScreenCaptureKit v2)  │
│   - or Microphone (Dictation mode)       │
│   - or BlackHole virtual audio (power)   │
│   - WS client to Voice Gateway           │
│   - Dual-pane transcript (orig | trans)  │
│   - Live glossary lookup (tooltip)       │
└──────────────┬──────────────────────────┘
               │ WS (binary Opus frames + JSON events)
               │ {type: "audio", lang_hint: "ru", ...}
               │ {type: "control", action: "start|end|pause"}
               ▼
┌──────────────────────────────────────────┐
│ TIER 2: Voice Gateway (streaming)        │
│   - /v1/translation/stream NEW endpoint  │
│   - SeamlessStreaming 2.5B engine (s2tt) │
│   - Language detect (input + auto pair)  │
│   - Dual-stream output (orig + trans)    │
│   - TranslationCache lookup/persist      │
│   - Audio I/O, low-latency loop          │
└──────────────┬──────────────────────────┘
               │ (no Krab agent dependency)
               │ (local processing only)
```

### 3.3 Data flow (example: Zoom meeting, RU speaker → EN translation)

1. User opens Krab Ear "Live Перевод" tab → clicks "Начать перевод"
2. Selects audio source: "System Audio" (Zoom meeting via ScreenCaptureKit)
3. Selects language pair: RU (source, auto-detect) → EN (target, user choice)
4. Opens WS to `ws://127.0.0.1:8090/v1/translation/stream`
5. User sends control JSON: `{"type": "stream.config", "source_lang": "ru", "target_lang": "en"}`
6. Voice Gateway loads SeamlessStreaming 2.5B (if not already loaded from Phase 1 conversation)
7. VG processes 200ms audio chunks via SeamlessStreaming.s2tt pipeline:
   - Detects speech regions (VAD)
   - Transcribes to source language RU (streaming ASR)
   - Translates RU → EN in parallel
   - Encodes TTS audio to target language EN
8. VG streams back dual events (200ms cadence):
   - `{"type": "transcript.partial", "lang": "ru", "text": "Привет мир"}`
   - `{"type": "translation.partial", "lang": "en", "text": "Hello world"}`
9. Krab Ear UI updates left pane (original) + right pane (translation) in real-time
10. Optional: glossary tooltip on hover over unknown words
11. User clicks "Завершить" → WS closes, transcript auto-saved to history as `mode: "live_translation"` entry

---

## 4. Component Specifications

### 4.1 Voice Gateway: `/v1/translation/stream` endpoint

**New module:** `app/translation/`

```
app/translation/
  __init__.py
  base.py                   # StreamingTranslator ABC
  seamless_stream_engine.py # SeamlessStreamingEngine (s2tt mode)
  translator.py            # Dual-stream orchestration (orig + trans)
  language_pair_router.py   # RU↔EN, RU↔ES, auto-pair logic
  ws_handler.py            # WebSocket for streaming audio
  session_state.py         # Per-session config (lang pair, source, metrics)
```

**New VG endpoints:**
- `WS /v1/translation/stream` — streaming audio input + dual-output events (200ms frames)
- `POST /v1/translation/start` — explicit start (returns session_id, audio source selector)
- `POST /v1/translation/{id}/end` — explicit end
- `GET /v1/translation/language-pairs` — list supported pairs (RU↔EN, RU↔ES, EN↔ES, etc.)
- `GET /v1/translation/audio-sources` — enum ScreenCaptureKit, Microphone, BlackHole, Test

**WS message protocol:**

*Uplink (client → VG):*
- Binary: Opus-encoded PCM 16kHz mono, 200ms frames
- Text JSON control:
  - `{"type": "stream.config", "source_lang": "ru", "target_lang": "en", "audio_source": "system"}`
  - `{"type": "pause"}` / `{"type": "resume"}`
  - `{"type": "end"}`

*Downlink (VG → client):*
- Text JSON events (200ms cadence):
  - `{"type": "transcript.partial", "lang": "ru", "text": "...", "confidence": 0.95}`
  - `{"type": "translation.partial", "lang": "en", "text": "...", "timestamp_ms": 1200}`
  - `{"type": "transcript.final", "lang": "ru", "text": "...", "offset_ms": 400}`
  - `{"type": "translation.final", "lang": "en", "text": "..."}`
  - `{"type": "engine.loaded", "engine": "seamless_streaming_2.5b"}`
  - `{"type": "session.language_detected", "source_lang": "ru", "confidence": 0.98}`
  - `{"type": "error", "code": "audio_source_unavailable|inference_timeout", "message": "..."}`

### 4.2 Krab Ear .app: new `TranslationViewController`

**New files:**

```
native/KrabEarAgent/Sources/KrabEarAgent/
  TranslationViewController.swift         NEW (tab controller)
  TranslationViewController+UI.swift      NEW (dual-pane layout, glossary)
  TranslationViewController+WS.swift      NEW (WebSocket + Opus handling)
  AudioSourceSelector.swift               NEW (ScreenCaptureKit / Microphone / BlackHole picker)
  LanguagePairSelector.swift              NEW (RU→EN, RU→ES, EN↔ES, etc.)
  HistoryPanelController+TranslationTab.swift  NEW (tab integration)
```

**UI layout (v1 — side-by-side panel):**
```
┌─ Live Перевод ──────────────────────────────┐
│ [ScreenCapture ▼]  [RU → EN ▼]  [⏹ Завер]   │
├──────────────────┬──────────────────────────┤
│   ORIGINAL (RU)  │   TRANSLATION (EN)       │
│                  │                          │
│ Привет мир       │ Hello world              │
│ Как дела?        │ How are you?             │
│                  │ How are you? (TTS audio  │
│                  │ playing... 🔊)           │
│                  │                          │
│ Это очень        │ This is very             │
│ интересный       │ interesting              │
│ разговор         │ conversation             │
│                  │                          │
└──────────────────┴──────────────────────────┘
    (scroll both panes sync'd)
    (hover word in ORIGINAL → glossary tooltip in EN)
```

**Glossary tooltip interaction:**
- User hovers over "разговор" (RU original pane)
- Tooltip appears: "**разговор** [noun]\n— conversation (основное значение)\n— talk, discussion\n— speech"
- Dismisses on mouse-out or Esc

**Audio source picker (ScreenCaptureKit v2 preferred):**
- If available (macOS 14.1+) → default to system audio capture
- Fallback: manual BlackHole setup (instructions in Settings)
- Fallback: Microphone input (for Dictation mode)
- Test source: embedded sine wave (debug only)

### 4.3 Integration: Glossary + Vocabulary

Krab Ear's existing glossary (from Phase 1 translation) integrates seamlessly:
- `TranslationService.get_glossary_suggestions(word)` IPC call
- UI looks up on hover via IPC, caches locally (100-entry TTL cache)
- If no user glossary entry → fallback to inline Wiktionary/open dictionary API (if available)

---

## 5. Multilingual Strategy (RU↔EN, RU↔ES primary pairs)

### 5.1 Language Pair Routing

| Pair | Engine | Quality | Latency | Notes |
|------|--------|---------|---------|-------|
| **RU↔EN** | SeamlessStreaming 2.5B | High | 800-1200ms | Primary; bidirectional |
| **RU↔ES** | SeamlessStreaming 2.5B | High | 800-1200ms | Primary; bidirectional |
| **EN↔ES** | SeamlessStreaming 2.5B | Medium-High | 800-1200ms | Secondary support |
| **RU→PT** | SeamlessStreaming 2.5B | Medium | 1-1.5s | Passive (low priority) |
| **Code-switching RU↔EN** | SeamlessStreaming (auto) | Medium | +200ms overhead | Detected per phrase; may degrade |

### 5.2 Russian (primary source)

- **STT:** SeamlessStreaming auto-detected as RU → high confidence
- **Translation target:** EN (default) or ES (user choice)
- **TTS:** SeamlessStreaming native TTS RU audio quality acceptable (no XTTS fallback needed for v1)
- **Glossary:** user's existing RU↔EN glossary from Phase 1

### 5.3 English

- **STT:** auto-detected as EN
- **Translation target:** RU (reverse pair) or ES (if selected)
- **Latency:** p50 800ms (same as RU, SeamlessStreaming pan-lingual)

### 5.4 Spanish (bilingual support)

- **STT:** ES detected from phonetic patterns
- **Translation:** RU (if user is RU native) or EN
- **TTS:** SeamlessStreaming native ES (LatAm + Iberian variant selectable)

### 5.5 Code-switching handling

- SeamlessStreaming's S2TT pipeline processes mixed RU↔EN mid-sentence automatically
- Language per phrase detected and labeled in `transcript.partial` events: `{"text": "...", "lang_detected": "ru"}` or `"en"`
- Translation applied per-detected language segment
- **Acceptance criterion:** 5-sentence RU↔EN mixed conversation, ≥80% accuracy, no crashes

---

## 6. Hardware Budget (M4 Max 36GB)

| State | Krab Ear | VG | Translation Cache | OS + browser | Total |
|-------|----------|----|----|------|-------|
| Idle (no live translation) | 200 MB | 100 MB | — | 5 GB | ~5.5 GB |
| Active RU↔EN stream (SeamlessStream) | 300 MB | 8-10 GB | 50-100 MB | 5 GB | ~14 GB |
| Concurrent: Translation + Phase 1 Voice Assistant active | 400 MB | 15-16 GB (both engines loaded) | 100 MB | 5 GB | ~21-22 GB |

**Notes:**
- SeamlessStreaming 2.5B is smaller than Phase 1's SeamlessM4T Large (10GB) or Moshi (8-12GB)
- Translation cache (disk-backed) not counted against RAM
- At 22GB, safe margin to 36GB allows browser/Xcode work alongside
- **No auto-unload needed** between Phase 1 + Phase 2 (both use same engine base, compatible memory footprint)

---

## 7. Triggers & Controls

### 7.1 Start Translation

**Option A (Explicit UI):**
- Tab "Live Перевод" in Krab Ear .app
- Click "Начать перевод" button
- Select audio source + language pair
- Click "Запустить" → WS opens

**Option B (Hotkey — future v1.1):**
- Single Right-Option tap (distinct from Phase 1's double-tap)
- Auto-selects last used language pair
- Auto-detects audio source (system audio if available)

**Option C (Scheduled — future, deferred):**
- Set recurring translation windows (e.g., "translate Zoom 9-10 AM daily")
- Would require scheduled task integration (out of v1 scope)

### 7.2 End Translation

- "Завершить" button (explicit)
- Esc hotkey (if focus on transcript pane)
- Session timeout (30 min inactivity)
- App quit (clean shutdown)

### 7.3 Pause/Resume (v1.1 future)

- "Пауза" button pauses audio capture + translation, keeps WS alive
- Translation cache remains warm
- Resume within 5 min without re-initializing

---

## 8. Engine Reuse from Phase 1

### 8.1 SeamlessStreaming S2TT mode

Phase 1's `SeamlessM4TEngine` for conversation mode does NOT directly translate → ASR → TTS sequentially. Phase 2 requires **streaming speech-to-speech-with-text (S2TT)** output:
- Input: source language speech (RU)
- Output: target language TTS audio (EN) + intermediate text labels

**Solution:** Extend `SeamlessStreamingEngine` in VG's `app/translation/seamless_stream_engine.py`:
```python
class SeamlessStreamingEngine:
    def s2tt_stream(
        self,
        audio_chunks: AsyncIterator[bytes],  # PCM 16kHz mono
        source_lang: str,                     # "ru", "en", "es"
        target_lang: str,                     # translation target
    ) -> AsyncIterator[TranslationEvent]:
        # Yields: TranslationEvent(type="transcript.partial"|"translation.partial", text=str, confidence=float)
        ...
```

**Reuse from Phase 1:**
- Same `LazyConversationEngine` base class (load/unload logic)
- Same language detection heuristics
- Same audio I/O plumbing (200ms chunking, Opus encoding)
- Different inference pipeline (S2TT vs. conversation)

### 8.2 Model compatibility

- SeamlessStreaming shares weights family with SeamlessM4T (Meta Meta Research)
- No version conflicts (Phase 1 uses large; Phase 2 uses 2.5B, distinct RAM footprint)
- Both available in MLX format (consistent with Phase 1 setup)

---

## 9. Privacy & Logging

### 9.1 Transcript & Translation Storage

**Default (v1):**
- Every session auto-saved to Krab Ear history NDJSON: `{type: "live_translation", original_lang: "ru", target_lang: "en", transcript_original: "...", transcript_translation: "...", timestamp, duration_sec}`
- Searchable via Krab Ear history UI
- **NOT** auto-saved to Krab agent memory (unlike Phase 1 voice assistant)

**Opt-in to memory:**
- User clicks "Сохранить в память" → IPC call to Krab agent, creates searchable memory entry
- Useful for meeting notes, important discussions

### 9.2 Audio Source Safety

**System audio (ScreenCaptureKit):**
- Captures microphone + system speaker (Zoom audio)
- Sensitive if co-speaker exists (e.g., two people on Zoom)
- **Recommendation:** Privacy notice in UI: "Перевод будет содержать слова всех участников. Убедитесь в согласии."

**Microphone only:**
- Only user's voice captured
- Safest for solo dictation

### 9.3 Glossary & History Privacy

- Glossary entries are user data (stored locally, encrypted at rest if Krab agent memory used)
- History visible in Krab Ear UI by default
- "Private mode" toggle (v1.1, future) → don't save to history, no memory

### 9.4 Logging

- VG logs per-session language pair, duration, byte counts to structured audit log
- No transcript content in logs (privacy-first)
- TranslationCache lookups logged (anonymized hit/miss ratio for performance tuning)

---

## 10. Phasing — 4-5 PRs over 4-6 weeks

| PR | Title | Effort | Dependencies |
|----|-------|--------|--------------|
| 2.1 | VG: `SeamlessStreamingEngine` s2tt mode + `/v1/translation/stream` WS endpoint | M | Phase 1 complete |
| 2.2 | VG: Language pair router + translation cache integration | S | 2.1 |
| 2.3 | Krab Ear: `TranslationViewController` + dual-pane UI (original \| translation) | M | 2.1 |
| 2.4 | Krab Ear: ScreenCaptureKit audio source + glossary tooltips | M | 2.3 |
| 2.5 | E2E acceptance: RU↔EN streaming, history save, glossary lookup | S | 2.4 |

**Optional follow-up (v1.1):**
- 2.6: Hotkey trigger (Right Option single tap, distinct from Phase 1)
- 2.7: Floating ribbon overlay (non-tabbed, free-floating window)
- 2.8: Per-meeting glossaries (future research)

**Approx timeline:**
- PR 2.1-2.2: 5-7 days (VG backend)
- PR 2.3-2.4: 5-7 days (UI + audio capture)
- PR 2.5: 2-3 days (testing + fixes)
- **Total: 4-6 weeks** (assuming 50% effort allocation alongside other work)

---

## 11. Acceptance Criteria

Phase 2 Live Translation considered MVP-complete when:

1. **ScreenCaptureKit audio capture:**
   - User selects "System Audio" in source picker
   - Zoom/Teams/Meet audio fed into translation pipeline
   - Dual transcripts appear in real-time (original RU + translated EN)

2. **Latency (streaming mode):**
   - p50 ≤ 1.2s (original phrase appears, then translation within 1.2s)
   - p95 ≤ 2.5s (acceptable for real-time but slower)
   - No buffering stalls for 30+ min continuous meeting audio

3. **Language pair accuracy (RU↔EN primary):**
   - Test corpus: 20 short sentences (2-10 words each) in Russian
   - STT accuracy (original transcription): ≥ 95%
   - Translation accuracy (RU → EN): ≥ 90% semantic correspondence (human judgment)

4. **Glossary integration:**
   - Hover over word in original pane → tooltip appears within 200ms
   - Shows translations + part-of-speech from user's glossary
   - Fallback to generic definition if not in glossary

5. **History persistence:**
   - Session transcript + translation saved to NDJSON
   - Visible in Krab Ear history (new column: "Type: Live Translation")
   - Searchable by original or translated text

6. **Code-switching (v1 scope):**
   - 3-sentence RU↔EN mixed input (e.g., "Привет, how are you? Мне хорошо.")
   - Completes without crash
   - Both languages transcribed + translated correctly (≥80% accuracy tolerance)

7. **Audio sources:**
   - ScreenCaptureKit: system audio only, or system + mic (user choice)
   - Microphone (Dictation mode): working alternative if ScreenCaptureKit unavailable
   - BlackHole fallback documented (power-user setup)

8. **Hardware:**
   - Active RU↔EN stream consumes ≤ 15 GB RAM (safe margin on 36GB)
   - No OOM crashes; graceful degrade to CPU mode if GPU pressure high

9. **UI/UX:**
   - Tab "Live Перевод" appears in Krab Ear main window
   - Side-by-side layout responsive (grows/shrinks with window resize)
   - Session controls (start, end, pause) visible and functional
   - Status indicator shows "🟢 Переводит", "🟡 Загружает", "🔴 Ошибка"

10. **E2E scenario (happy path):**
    - Open Krab Ear → "Live Перевод" tab
    - Select ScreenCaptureKit audio (Zoom window)
    - Choose RU → EN pair
    - Click "Начать перевод"
    - Speak/play Russian audio
    - Both original (RU) and translation (EN) appear side-by-side within 1.2s
    - Click "Завершить"
    - Transcript saved and visible in history

---

## 12. Open Questions / Future Work

| Topic | Decision Pending | Target Phase |
|-------|-----------------|--------------|
| **Meeting overlay priority vs. Dictation priority** | Should v1 focus on meeting transcription or bilingual dictation first? (Different UX, same backend) | User decision before 2.1 |
| **Floating ribbon v1 vs. v1.1** | Tab-based (v1) or free-floating window (v1.1)? Ribbon better for meetings (always visible), tab requires context switch. | Deferred to 2.6+ (v1.1) |
| **Per-meeting glossaries** | Custom domain glossaries (e.g., "medical terminology" for doctor, "tech jargon" for engineering meeting). Research scope. | Phase 2.ε (future) |
| **Auto-pause dictation on translation start** | If user starts live translation, should Phase 1 dictation auto-pause to free audio resources? Or concurrent OK? | Spec 2.2-2.3 |
| **ScreenCaptureKit fallback on older macOS** | For users on macOS 13.x (Phase 1 requirement), what audio source? BlackHole mandatory? | Spec 2.1 audio source spec |
| **Translation model fine-tuning** | SeamlessStreaming pre-trained → domain-specific fine-tuning (medical, legal, finance). Feasibility? | Research phase 2.ε |
| **Real-time TTS audio quality** | SeamlessStreaming's synthesized audio acceptable, or XTTS-v2 fallback needed for naturalness? | Test in PR 2.5 |
| **Code-switching mid-translation** | If user switches language mid-sentence, does target language pair auto-update, or require manual selection? | Design decision 2.2 |
| **Concurrent Phase 1 + Phase 2 active** | Can user run voice assistant conversation AND live translation simultaneously? Memory allows ~21GB, but single-user focus OK? | Architecture decision |

---

## 13. Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| **SeamlessStreaming s2tt latency > 1.5s** | Medium | Set acceptance to p50 ≤ 1.5s (less aggressive); fallback to Phase 1's batch mode for non-real-time translation |
| **ScreenCaptureKit unavailable on macOS 13.x** | High (Phase 1 supports 13.x; ScreenCaptureKit requires 14.1+) | Mandate BlackHole setup for pre-14.1 users; or gate feature behind OS version check |
| **Concurrent VG load (Phase 1 engine + Phase 2 engine)** | Medium | Auto-evict Phase 1 if translation starts; user sees 5s pause to unload/reload engines; document in UX |
| **Glossary lookup lag (IPC → Krab Ear history)** | Low-Medium | Implement local 100-entry cache with TTL; fallback to offline dictionary (if available); show "..." while loading |
| **Memory pressure (15 GB SeamlessStreaming + browser + Xcode)** | Medium | Monitor swap usage; auto-reduce quality if swap engaged; warn user if available RAM < 10GB |
| **Audio source enumeration (ScreenCaptureKit framework changes)** | Low | Pin Apple framework version; have fallback to manual device listing via sounddevice |
| **Translation cache bloat (long-running sessions)** | Low-Medium | Implement TTL-based eviction (oldest 10% dropped after 1 hour); limit to 10,000 entries max |
| **Microphone + ScreenCaptureKit conflict** | Low | Mutex at VG level: only one audio input stream per session; clarify in Settings |
| **Privacy leak: co-speaker transcripts auto-saved** | Medium | Add explicit "Include all participants?" dialog before starting system audio translation |

---

## 14. Success Metrics

- **MVP adoption:** Pavel uses live translation ≥ 2 times/week within 3 weeks of launch
- **Latency:** p50 ≤ 1.2s (original + translation pair ready)
- **Accuracy:** RU→EN test corpus ≥ 95% STT, ≥ 90% translation semantic match
- **Hardware:** active session ≤ 15 GB RAM (safe margin on 36GB)
- **No regressions:** Phase 1 (voice assistant), Phase 1 dictation, history, import flows unchanged
- **Test coverage:** new 25+ unit tests covering s2tt engine, language routing, glossary integration, dual-stream events
- **Documentation:** this spec + implementation guide for future extensions (floating ribbon, per-meeting glossaries)

---

## 15. Implementation Notes

### 15.1 SeamlessStreaming S2TT Integration

The core of Phase 2 is adding a new inference mode to VG's translation engine: **streaming speech-to-speech-with-text** (s2tt). This differs from Phase 1's conversational mode:

- **Phase 1 (voice assistant):** audio → STT → LLM reasoning → TTS → audio (with optional context memory)
- **Phase 2 (live translation):** audio → STT (source lang) → MT → TTS (target lang) → audio + text labels

Implementation approach:
1. Extend `SeamlessStreamingEngine` class in `app/translation/seamless_stream_engine.py`
2. Add method `async def s2tt_stream(...)` that yields dual-stream events
3. Reuse audio I/O, chunking, and model loading from Phase 1
4. New `TranslationEvent` pydantic model for dual-stream output
5. Register new endpoint in `app/main.py` → WS handler routes to `s2tt_stream()`

### 15.2 Audio Source Abstraction

Krab Ear needs to support multiple audio sources; design as strategy pattern:

```python
# In Krab Ear backend (or VG)
class AudioSource(ABC):
    async def capture_chunk(self) -> bytes: ...
    async def start(self): ...
    async def stop(self): ...

class ScreenCaptureKitSource(AudioSource): ...  # macOS 14.1+
class MicrophoneSource(AudioSource): ...
class BlackHoleSource(AudioSource): ...
class TestSource(AudioSource): ...  # debug sine wave
```

VG's `/v1/translation/audio-sources` endpoint lists available sources; client picks one before WS opens.

### 15.3 Glossary Lookup Caching

To avoid IPC latency on every tooltip hover:
- Krab Ear maintains local 100-entry LRU cache of (word, language) → glossary entry
- On cache miss, IPC to backend; result cached with 30-min TTL
- Display "..." spinner while IPC in-flight
- Graceful degrade if IPC times out (show generic definition or "⚠️ offline")

---

## Cross-references

- **Phase 1** (Voice Assistant Mode): `/docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md`
- **Phase 3** (Call Automation): TBD (will reuse both Phase 1 + Phase 2 engines for call recording translation)
- **Phase 4** (STT Adapters): Parallel work; decoupled from Phases 1-3
- **Memory entry:** `project_phase5_progress.md` — Phase 5 MVP (Ordinary Call Translator, interim spec)
- **Krab Ear public repo:** https://github.com/Antigravity-AGENTS/Krab-Ear

---

## Research backing (2026-04-18)

- SeamlessStreaming 2.5B: Meta Research, streaming S2TT capability verified; CC-BY-NC 4.0
- SeamlessM4T v2 Large (Phase 1): batch-only fallback; 10GB RAM footprint
- ScreenCaptureKit v2: macOS 14.1+; gated on OS version check (fallback BlackHole for 13.x)
- MLX compatibility: SeamlessStreaming (2.5B) + SeamlessM4T (Large) coexist in Phase 2 memory budget

