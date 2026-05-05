# Phase D — Strategic Roadmap (post-B+C foundation)

**Status:** design proposal, awaiting user direction selection
**Date:** 2026-05-05
**Branch base:** `codex/krab-ear-v2` (post-Phase-C batch 4, PR #371)
**Author:** Pavel + Claude

---

## 1. What's done — Phases A / B / C recap

### Phase A — Auto-heal (2026-05-02, 11 commits)
Two-ring supervisor with exp backoff (0/2/5/15 s) + circuit breaker (5 fails / 60 s → 5 min cooldown). HealthMonitor actor with 3 s ping, SIGTERM→wait→SIGKILL respawn. BackendToast + StatusIndicatorView dot in menu bar. LaunchAgent `KeepAlive=true` (Variant B).

### Phase B — Loud Errors (2026-05-03/04, PRs #362-#365)
`ErrorBus` with 20 structured error codes across 6 severity levels. Active LLM HTTP probe (HEAD `/v1/models`, no-JIT). Sentry DSN wired Swift→IPC→backend. 7 codes wired end-to-end (rewriter failures, IPC disconnects, audio errors). `error_codes.py` canonical registry. Sentry breadcrumbs at IPC + probe sites. Diagnostics tab in UI.

### Phase C — Root Cause (2026-05-04/05, PRs #366-#371, ~300+ tests)
C.1 memory baseline infra + `tracemalloc` snapshots + GigaAM memory hypothesis. C.2 IPC exponential reconnect + SIGPIPE fix. C.3 MLX lock audit — all sites verified / wrapped. C.4 STT repetition loop detector + brand regex expansion. C.5 Sendable warnings → 0 internal. C.6 single-instance POSIX flock + orphan-kill + worktree shadow cleanup. C.7 observability completeness — all `NSApp.terminate` breadcrumbed, SIGSEGV/SIGABRT sigaction handler. C.8 doc verifier `verify_claude_md.py` + CI hook. MPS memory pool fix (batch 4). STT quality indicator in UI. `longform_audit.py` script.

**Foundation state:** the system can auto-restart, loudly signals failures, has root-cause tooling. Ready to build features on top.

---

## 2. Phase C remaining (complete BEFORE Phase D)

Three followups not yet closed. These are small — propose completing in one batch session:

| Item | Status | Effort |
|---|---|---|
| **C.1-fix** — actual leak fix after H1 measurement validates suspects | Pending measurement runs | 0.5–1 day |
| **C.2 E2E reconnect test** — kill backend mid-call, assert Swift auto-reconnects | Scaffolding done; test not written | 0.5 day |
| **C.5 external Sendable** — `sentry-cocoa` warning (external, not internal) | Suppress or vendor-patch | 0.25 day |

Complete these FIRST so Phase D starts on fully green CI.

---

## 3. Phase D strategic directions

Five directions proposed. User picks 1-3 to activate.

---

### D.1 — Voice Assistant Phase 2 (conversation depth)

**Goal:** extend the Phase 1 voice channel (Moshi/SeamlessStreaming + Krab brain) with real-time interruption handling, mid-conversation tool-call results, and persistent cross-session memory.

**Effort:** 4–6 days

**Key files to add/modify:**
- `backend/voice_session_store.py` — persist conversation turns + tool calls to NDJSON
- `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift` — add interruption UI (hold-to-interrupt affordance)
- `KrabEar/backend/call_assist_service.py` — wire tool-call result injection into voice stream
- `docs/superpowers/specs/2026-05-05-voice-assistant-phase2-design.md` (dedicated spec)

**Acceptance criteria:**
1. Mid-answer interruption (hold Right Option while AI is speaking) causes backend to truncate TTS and re-enter listen mode within 300 ms.
2. Tool call results (calendar lookup, Krab memory fetch) are injected into conversation context and reflected in the AI's next response — verified by E2E test with stubbed tool.
3. Conversation history persists across app restarts; `get_conversation_history` IPC returns last 20 turns with timestamps and tool_call metadata.

**Dependencies:** Phase 1 Voice Gateway WS endpoint must be merged + stable (already live per `project_va_phase1_progress.md`).

---

### D.2 — STT Engine Matrix Expansion

**Goal:** add Parakeet (NVIDIA-tuned EN), Voxtral (Mistral multilingual), and SenseVoice (emotion-aware) as selectable STT adapters alongside Whisper MLX + GigaAM, with automatic routing by language and audio type.

**Effort:** 3–5 days (per adapter ~1 day; routing logic ~1 day)

**Key files to add/modify:**
- `KrabEar/backend/transcriber.py` — adapter registry + routing logic (`_select_adapter`)
- `KrabEar/core/engine.py` — plug adapters into fallback chain
- `KrabEar/backend/parakeet_adapter.py` (new)
- `KrabEar/backend/voxtral_adapter.py` (new)
- `KrabEar/backend/sensevoice_adapter.py` (new — PyTorch+MPS, no MLX lock needed)
- `KrabEar/core/model_selector.py` — extend `SmartModelSelector` routing rules
- `KrabEar/tests/test_adapter_routing.py` (new)

**Acceptance criteria:**
1. `list_stt_adapters` IPC returns all installed adapters with status (`available` / `unavailable` / `loading`). Unavailable adapters are skipped in fallback chain, not error-logged.
2. Auto-routing: EN audio → Parakeet first; RU audio → GigaAM first; ES/multilingual → SenseVoice or Voxtral. Routing decision logged at `debug` level with reason.
3. Benchmark suite (`test_performance_benchmarks.py`) covers all adapters; p95 latency baseline established per adapter in `.perf_baselines/`.

**Dependencies:** D.2 is independent; GigaAM adapter already wired (PR #336). SenseVoice research done (per `project_research_backlog_2026-04.md`).

---

### D.3 — Translation Quality + Multilingual Matrix

**Goal:** upgrade offline translation from RU↔ES+EN→RU pair to full multilingual matrix (RU/ES/EN/DE/FR/ZH) via NLLB-200 or SeamlessM4T, replacing the current pair-only offline translator with a unified model.

**Effort:** 3–4 days

**Key files to add/modify:**
- `KrabEar/backend/translator.py` — swap pair-based logic for model-based routing; keep pair cache as fast-path
- `KrabEar/backend/translation_service.py` — expose `target_language` param on `translate` IPC
- `KrabEar/backend/live_subs_service.py` — pass `target_language` through to translator
- `KrabEar/core/language_detector.py` — extend beyond RU/ES/EN heuristics to DE/FR/ZH
- `KrabEar/tests/test_multilingual_translation.py` (new)
- Settings: `translation_target_language` (default `ru`), `translation_model` (`nllb-200` | `seamlessm4t` | `offline-pair`)

**Acceptance criteria:**
1. `translate` IPC accepts arbitrary `target_language` ISO 639-1 code; returns translated text or `{"error": "language_not_supported"}` for unlisted codes.
2. RU↔ES pair latency regression < 10% vs current baseline after model swap (measured via perf benchmark).
3. Live Subs pipeline uses `target_language` from settings — switching language in UI takes effect on the next flush without restart.

**Dependencies:** SeamlessM4T research done. Independent of D.1/D.2.

---

### D.4 — Productivity Integrations (dictate → create)

**Goal:** wire Krab Ear dictation output directly to Apple Notes, Reminders, and calendar events — "диктую → создаётся заметка" — without intermediate paste.

**Effort:** 2–3 days

**Key files to add/modify:**
- `KrabEar/backend/productivity_service.py` (new) — `create_note`, `create_reminder`, `create_calendar_event` via AppleScript/EventKit
- `KrabEar/backend/service.py` — register 3 new IPC methods
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Productivity.swift` (new extension) — "Send to Notes / Reminders / Calendar" buttons in history row context menu
- `KrabEar/tests/test_productivity_service.py` (new — stub AppleScript executor)

**Acceptance criteria:**
1. Right-click on a history item shows "Create Note", "Create Reminder", "Add to Calendar". Each fires the corresponding IPC method and shows a BackendToast confirmation.
2. `create_note` IPC produces a Note in the default Notes account containing the full transcript text and timestamp. Verified by AppleScript readback in integration test (stubbed in unit test).
3. `create_calendar_event` accepts optional `start_time` (ISO-8601) and `duration_minutes` params parsed from the transcript via heuristic extractor. Falls back to "today + user-selected time" if no time detected.

**Dependencies:** Independent. Uses existing AppleScript execution pattern from `PasteService`. Low risk.

---

### D.5 — Privacy Mode Hardening

**Goal:** implement a local-only mode that disables all telemetry, network calls, and cloud dependencies — one toggle for users who handle sensitive audio.

**Effort:** 2–3 days

**Key files to add/modify:**
- `KrabEar/core/config.py` — `privacy_mode: bool = False` setting
- `KrabEar/backend/service.py` — privacy gate: block `translate_selection` network, disable Sentry, disable webhook_manager, disable VGWSClient when `privacy_mode=True`
- `KrabEar/backend/observability.py` — `is_telemetry_allowed()` guard
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` — Privacy Mode toggle with explanatory footer
- `KrabEar/tests/test_privacy_mode.py` (new)

**Acceptance criteria:**
1. When `privacy_mode=True`, all outbound HTTP (Sentry, VGWSClient, webhook_manager, remote STT fallback) is suppressed; `get_diagnostics` returns `{"privacy_mode": true, "telemetry": "disabled"}`.
2. Toggling privacy mode via UI takes effect immediately (next IPC call), no restart required.
3. `privacy_mode=True` is persisted across restarts and survives `BackendSupervisor` respawn.

**Dependencies:** Independent. Builds on existing `observability.py` no-op pattern.

---

## 4. Recommended sequencing

### Signals from sessions

From B+C session signals (repeated user asks):
- **D.2 (STT matrix)** — GigaAM bench done (2026-04-26), benchmark matrix established (2026-04-18), explicit research index with Parakeet/Voxtral/SenseVoice. User invested real time in adapter infrastructure.
- **D.4 (Productivity)** — "диктовать → создать заметку" mentioned in context. Low effort, high daily-use value.
- **D.1 (Voice Phase 2)** — Phase 1 explicitly closed 2026-04-18 with note "Phase 2 separate". Natural next chapter.

### Proposed order

```
C remaining (C.1-fix + C.2 E2E + C.5 external)  →  ~1 day
D.2 STT matrix expansion                          →  3-5 days  (high leverage, infra ready)
D.4 Productivity integrations                     →  2-3 days  (quick wins, user-facing)
D.1 Voice Assistant Phase 2                       →  4-6 days  (deeper, needs D.2 stable)
D.3 Translation multilingual                      →  3-4 days  (parallel to D.1 or after)
D.5 Privacy mode                                  →  2-3 days  (any time, independent)
```

D.2 first because: adapter registry pattern unblocks routing work needed by D.1; benchmark coverage provides regression safety for D.3.

D.5 is independent — can be done by a parallel sub-agent at any point.

---

## 5. Out of scope for Phase D

- **Mobile iOS app (D-mobile)** — separate project boundary; requires iCloud sync infrastructure not present. Deferred until desktop is feature-complete.
- **Multi-device session sync** — requires backend to become a server (port-forwarding, auth, conflict resolution). Out of scope for local-first architecture.
- **Plugin/extension API** — `backend/plugin_system.py` stub exists but surface area is large; defer until 3+ real plugin use-cases identified.
- **Krab/Ear/Voice runtime merger** — explicitly a non-goal per PRD (separate projects, API boundaries).
- **App Store distribution** — requires notarization + sandboxing review; Accessibility API may not survive sandbox. Separate workstream.

---

## 6. Open questions

1. **Which directions does user want to activate?** Recommend picking 2-3. D.2 + D.4 is a natural low-risk pair. D.1 is the deepest bet.
2. **Effort budget per direction?** Each D.X planned as 1-2 batch sessions (3-5 parallel sub-agents per session, proven from B+C pattern).
3. **C.1 memory measurement** — have baseline measurement runs been executed yet? Results determine whether C.1-fix is 0.5 day or escalates to multi-day investigation.
4. **Live Subs crash** — C.7 observability shipped. Has the crash reproduced with Sentry DSN live? If yes, what do the breadcrumbs show? This may surface a quick D.0 fix before Phase D proper.
5. **GigaAM worker memory** — hypothesis (subprocess arena not freed between calls) not yet confirmed by measurement. Confirm via C.1 soak before wiring GigaAM as default for RU in D.2.
6. **`start_agent.command` audit** — Phase A followup, not yet closed. Does it correctly pick the bundle binary or the runtime binary? Relevant to two-binary drift (C.6 fix validation).

---

## Total estimated effort

| Phase | Directions | Effort |
|---|---|---|
| C remaining | C.1-fix, C.2 E2E, C.5 external | ~1 day |
| D.2 | STT matrix | 3–5 days |
| D.4 | Productivity integrations | 2–3 days |
| D.1 | Voice Phase 2 | 4–6 days |
| D.3 | Translation multilingual | 3–4 days |
| D.5 | Privacy mode | 2–3 days |
| **Total (all 5)** | | **~15–22 days** |

User selects which D.X to activate — each direction is independent enough to run in isolation.

---

## Next steps

1. **User selects** 2-3 directions from section 3.
2. **C remaining** batched into single session (small, keep CI green).
3. **Per-direction plan** via `superpowers:writing-plans` → execute via `superpowers:subagent-driven-development`.
4. **Dedicated spec** for D.1 (most complex) before implementation — see `docs/superpowers/specs/2026-05-05-voice-assistant-phase2-design.md` placeholder.

---

*End of Phase D roadmap design.*
