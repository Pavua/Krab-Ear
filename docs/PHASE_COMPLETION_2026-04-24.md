# Phase Completion Report — 2026-04-24

**76 PRs merged** across Phase 1 / Phase 2 / Phase 3 + Phase 4 STT adapters.  
**6391+ tests passing**, 243+ test files.

---

## Summary

| Phase | Status | PRs | Key Deliverable |
|-------|--------|-----|-----------------|
| Phase 1 — Voice Assistant | ✅ SHIPPED | ~19 | ConversationViewController, Moshi+SeamlessStreaming, Qwen3-30B brain |
| Phase 2A — Selection Translate | ✅ SHIPPED | 2 | Cmd+Shift+T, AX API + clipboard fallback |
| Phase 2B — Live Subtitles | ✅ SHIPPED | 3 | ScreenCaptureKit → STT → HUD overlay |
| Phase 3 — Call Automation | ✅ SHIPPED | 5 | CallSession, Telnyx+Twilio adapters, cost ticker, auto-end |
| Phase 4 — STT Adapters | ✅ SHIPPED | 5 | Parakeet, SenseVoice, WhisperX, Voxtral, base Whisper |
| Observability (Sentry) | ✅ SHIPPED | 2 | SentryConfig.swift + observability.py, no-op без DSN |
| Glossary Auto-Learn | ✅ SHIPPED | 2 | Medical domain term extraction + UI suggestions panel |
| openWakeWord | ✅ SHIPPED | 1 | Free Apache-2.0 wake word (custom "Краб" model needs training) |
| Design Tokens Pipeline | ✅ SHIPPED | 3 | Figma↔Swift sync, REST API, console scripts |
| Settings Redesign | ✅ SHIPPED | 3 | CollapsibleSectionView + Claude Design A/B variant |

---

## Delivered Features Matrix

| Feature | Hotkey | IPC method | Status |
|---------|--------|------------|--------|
| Voice dictation → paste | Right Option (hold) | `start_recording` / `stop_recording` | ✅ |
| Voice Assistant conversation | Right Option ×2 | WS `/v1/sessions/{id}/conversation` | ✅ |
| Selection translate | Cmd+Shift+T | `translate_selection` | ✅ |
| Live system audio subtitles | Settings toggle | `live_subs_push_chunk` + SSE | ✅ |
| Outbound call automation | Call tab UI | `start_call` / `end_call` | ✅ |
| Glossary auto-learn | automatic | `get_glossary_auto_suggestions` | ✅ |
| Wake word "Краб" | passive | openWakeWord adapter | ✅ (basic) |
| Sentry crash reporting | automatic | `set_settings {sentry_dsn}` | ✅ |
| Translation history | automatic | `translate` → `history_service` | ✅ |
| Speaker diarization | automatic | pyannote.audio + MPS | ✅ |

---

## Architecture Diagram (as of 2026-04-24)

```
┌──────────────────────────────────────────────────────────────────────┐
│  Swift Agent (KrabEarAgent)                                          │
│                                                                      │
│  ┌─────────────┐  ┌──────────────────────┐  ┌───────────────────┐  │
│  │ HotkeyMgr   │  │ ConversationVC        │  │ CallAutomation    │  │
│  │ + DoubleTap │  │ (Voice Assistant)     │  │ Controller        │  │
│  └─────────────┘  └──────────────────────┘  └───────────────────┘  │
│                                                                      │
│  ┌─────────────┐  ┌──────────────────────┐  ┌───────────────────┐  │
│  │ Selection   │  │ SystemAudioCapture   │  │ LiveSubtitles     │  │
│  │ Translator  │  │ (ScreenCaptureKit)   │  │ Overlay (HUD)     │  │
│  └─────────────┘  └──────────────────────┘  └───────────────────┘  │
│                                                                      │
│  ┌─────────────┐  ┌──────────────────────┐  ┌───────────────────┐  │
│  │ SentryConfig│  │ SingleInstanceGuard  │  │ WakeWordListener  │  │
│  └─────────────┘  └──────────────────────┘  └───────────────────┘  │
│                                                                      │
│           Unix socket JSON-RPC (krabear.sock)                       │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────────┐
│  Python Backend (KrabEar/)                                           │
│                                                                      │
│  BackendService (IPCServer)                                          │
│   ├─ HistoryService        ├─ TranslationService                    │
│   ├─ SettingsService       ├─ CallAssistService                     │
│   └─ LiveSubsService ──────┼─ GlossaryAutoLearn                    │
│                             └─ observability.py (Sentry)            │
│                                                                      │
│  Phase 3 Call Services:                                              │
│   CallSession + CallSessionStore + CallCostEstimator                │
│   CallSilenceProbe + CallAutoEnd                                     │
│   TelnyxAdapter | TwilioAdapter (call_provider setting)             │
│                                                                      │
│  Core STT Pipeline:                                                  │
│   AudioEngine (mlx-whisper, mlx_lock) → TextUtils → LLMRewriter    │
│   Phase 4 Adapters: Parakeet | SenseVoice | WhisperX | Voxtral     │
│                                                                      │
│  EventBus (SSE) → LiveSubtitlesOverlay                              │
│                                                                      │
│  TelegramBridge ──► main Krab userbot (/api/notify)                 │
└──────────────────────────────────────────────────────────────────────┘
                              │
                    Voice Gateway (WS)
                    Qwen3-30B (LM Studio)
```

---

## Tests

| Scope | Count (approx) |
|-------|---------------|
| Python backend + core | ~5800 |
| Phase 3 call services | ~120 |
| Phase 2 live subs | ~80 |
| Swift XCTest suites | ~400 |
| **Total** | **6391+** |

---

## What Remains

| Item | Blocker | Priority |
|------|---------|----------|
| Picovoice wake word "Краб" | Need rongfa.biz corporate email for free tier signup | Medium |
| Custom openWakeWord "Краб" model | ~15 min Jupyter training once dataset collected | Low |
| Sentry project setup (user side) | User needs to create sentry.io or GlitchTip project | Low |
| Phase 4 adapter benchmarks | Update `docs/BENCHMARK_M4_MAX.md` with Parakeet/Voxtral numbers | Low |
| Twilio number porting | Optional — Telnyx sufficient for primary use case | Low |
| macOS 26 MPS regression watch | PyTorch issue #167679 — fallback to CPU in place | Monitoring |

---

## Key Architectural Decisions Made

1. **openWakeWord over Picovoice** — Apache-2.0, no signup; Picovoice waiting on corporate email (rongfa.biz)
2. **TelnyxAdapter as default call provider** — cleaner API, $5 free credit; Twilio as alternate via `call_provider` setting
3. **Sentry no-op pattern** — ship to users without requiring DSN; self-hosted GlitchTip supported
4. **ScreenCaptureKit for system audio** — requires Screen Recording permission, not Microphone
5. **SSE for live subs delivery** — low-latency, no polling, works over existing REST server (port 5005)
6. **AX API primary + clipboard fallback** for selection translate — handles apps that block AX

---

*Generated 2026-04-24. Next review: after Picovoice integration or Phase 4 adapter polish.*
