# Call Automation — Design Spec

**Date:** 2026-04-18  
**Status:** DRAFT (approved pending brainstorm session — 7 open questions require user input before implementation)  
**Author:** Claude Haiku 4.5  
**Phase:** 3 of 4 (roadmap: Voice Assistant → Live Translation → **Call Automation** → STT Adapters)

---

## 1. Goals

Enable AI-powered outbound phone calling from Krab Ear, delegating call orchestration to Voice Gateway and Krab agent brain:

- **Outbound dialing via Twilio API** (MVP) or Telnyx (future alternative)
- **Real-time bidirectional conversation** — AI speaks first (self-disclosure mandatory), listens to human responses, adapts dynamically
- **Call goal delegation** — user specifies intent via Krab agent ("Позвони клинике, спроси про слот в среду") → system executes autonomously
- **TCPA compliance** — mandatory AI self-identification upfront ("Это AI-ассистент Павла Сергеева")
- **Opt-out handling** — respect verbal "СТОП" / "STOP" commands, immediate hangup
- **Persistent call logs** — transcript + audio recorded for legal compliance and future analysis
- **Cost estimation** — show Twilio API pricing before dialing (prevent surprise charges)
- **Multilingual support** — RU primary (Krab agent voice), EN/ES fallback
- **Voicemail detection & handling** — simple heuristic (silence >3s), leave templated message, terminate

---

## 2. Non-Goals (explicit out-of-scope)

- **Call center / bulk dialing** — no campaign mode, no list processing. Single human-targeted calls only (Phase 3 MVP).
- **Call recording without consent** — user explicitly enables recording per call; TCPA forbids silent recording in many jurisdictions.
- **Inbound call handling** — Phase 3 focuses on outbound only. Inbound routing deferred to Phase 3.5.
- **PBX/VoIP integration** (beyond Twilio) — Telnyx as future alternative; FreeSWITCH/LiveKit reserved for long-term DIY platform.
- **Multi-step conversation without user review** — goals limited to single intent per call (Q4 multi-step deferred).
- **End-to-end encryption** — Twilio/Telnyx provide TLS in transit; full E2E encryption out of scope.
- **Spam detection / reputation protection** — number spoofing, carrier reputation, SHAKEN/STIR compliance deferred to Phase 3.x.
- **Calendar integration for scheduling callbacks** — call scheduling deferred to Phase 3.x.

---

## 3. Architecture

### 3.1 Three-tier system (extended Phase 1+2 architecture)

```
┌──────────────────────────────────────────┐
│ TIER 1: Krab Ear .app (UI/UX)            │
│   - New "Позвонить" dialog in Settings   │
│   - Phone number input (paste/manual)    │
│   - Call goal text input ("спроси...")    │
│   - Cost estimate display (USD)          │
│   - Live call status (connected, speaking)│
│   - Transcript viewer during call        │
│   - Call history + transcript storage    │
└──────────────┬──────────────────────────┘
               │ IPC request: start_call(phone, goal)
               │ Polling: get_call_status(call_id)
               ▼
┌──────────────────────────────────────────┐
│ TIER 2: Voice Gateway (orchestration)    │
│   - New `/v1/calls/` endpoint module     │
│   - Twilio SDK integration               │
│   - Real-time bidirectional audio stream │
│   - SeamlessM4T / Moshi STT (incoming)   │
│   - Qwen3-30b LLM response generation    │
│   - TTS audio synthesis → Twilio         │
│   - Audio I/O loop (duplex, ~400ms)      │
│   - Call state machine (init→ringing→... │
│   - Voicemail detection heuristics       │
└──────────────┬──────────────────────────┘
               │ HTTP / module call / Twilio API
               │   twiml.py (TwiML bin XML generation)
               ▼
┌──────────────────────────────────────────┐
│ TIER 3: Krab agent (brain + tools)       │
│   - Existing memory_engine + LLM         │
│   - NEW: call_assistant_handler.py       │
│   - MCP tools: call_*_*                  │
│   - System prompt generation             │
│   - TCPA opt-out state tracking          │
│   - Call log persistence                 │
└──────────────────────────────────────────┘
```

### 3.2 Call lifecycle state machine

```
User triggers call via Krab Ear UI:
  ├─ Enter phone number + goal
  ├─ Show cost estimate (USD)
  ├─ Click "Позвонить"
  └─ IPC: start_call(phone, goal, cost_approved=True)
       │
       ▼
  [INIT] Voice Gateway allocates call_id, dials via Twilio
       │
       ├─ Twilio WebSocket streams incoming audio (RTP)
       ├─ VG generates system prompt: "Это AI-ассистент. {goal}. Что скажете?"
       ├─ Krab agent processes → qwen3-30b generates response
       └─ TTS → audio → Twilio TwiML uplink
            │
            ▼
       [RINGING] Recipient's phone rings (waiting for answer)
            │
            ├─ User declines (no answer >30s)
            │   └─ VG hangs up, returns {status: "no_answer"}
            │
            ├─ Voicemail picks up (silence heuristic >3s)
            │   ├─ AI leaves templated message
            │   ├─ Hangup
            │   └─ save {status: "voicemail", message_left: True}
            │
            └─ Recipient answers
                 │
                 ▼
            [CONNECTED] Bidirectional conversation loop
                 │
                 ├─ Recipient speaks
                 ├─ VG: SeamlessM4T/Moshi → text transcript
                 ├─ Krab agent: LLM processes intent + memory
                 ├─ Krab agent: generate response
                 ├─ VG: TTS → audio
                 ├─ Stream to recipient
                 ├─ Recipient responds (loop)
                 │
                 └─ [TERMINATION TRIGGERS]:
                    ├─ Recipient says "СТОП" / "STOP"
                    │  └─ VG: immediate hangup, {opt_out: True}
                    │
                    ├─ AI goal achieved (Krab agent sends signal)
                    │  └─ VG: "Спасибо, завершаю. До свидания."
                    │     → hangup, {status: "completed"}
                    │
                    ├─ Call duration >30 min
                    │  └─ VG: auto-hangup, {status: "timeout"}
                    │
                    └─ Silent >10s (no speech detected)
                       └─ [QUESTION #7] Auto-end or probe ("Здравствуйте?")?
                            │
                            └─ VG waits for decision from Krab agent
                                 │
                                 ▼
            [HANGUP] Twilio disconnects
                 │
                 ├─ Save transcript + full audio to {call_log_id}.wav
                 ├─ Save {status, duration, transcript, speaker_diarization, cost_actual}
                 ├─ Krab agent: append to memory_engine
                 ├─ Krab Ear: append to history NDJSON ({type: "call_automation", ...})
                 └─ UI shows summary: "Звонок завершен. 4m 32s. Спасено в историю."
```

### 3.3 Twilio integration

**MVP Provider:** Twilio (already integrated in Voice Gateway for Phase 2 fallback audio routing)

```python
# Voice Gateway: app/calls/twilio_client.py (NEW)

class TwilioCallManager:
    def __init__(self, account_sid, auth_token):
        self.client = Client(account_sid, auth_token)
    
    def dial_outbound(self, from_phone: str, to_phone: str, 
                      twiml_url: str) -> Call:
        """Initiate outbound call with TwiML instructions."""
        return self.client.calls.create(
            to=to_phone,
            from_=from_phone,
            url=twiml_url,
            record=True,  # TCPA: capture for compliance
            record_channels='mono'
        )
    
    def hangup(self, call_sid: str):
        """Terminate call."""
        return self.client.calls(call_sid).update(status='completed')
    
    def get_call_status(self, call_sid: str) -> str:
        """Poll call state: queued|ringing|in-progress|completed|failed."""
        call = self.client.calls(call_sid).fetch()
        return call.status
```

**Twilio credentials:** stored in `~/.krab_ear_data/secrets.json` (env var override: `KRAB_EAR_TWILIO_*`)

**Twilio WebSocket (RTP):** Real-time media streaming via Twilio's WebSocket bridge:
- Incoming audio: PCM 16-bit 16kHz mono via WS binary frames
- Outgoing audio: TTS-generated Opus frames → Twilio → recipient
- Latency: ~200-400ms (Twilio API + network)

**Cost per call:** ~$0.013/minute (Twilio pay-as-you-go pricing as of 2026-Q2)

---

## 4. Component Specifications

### 4.1 Voice Gateway: new `app/calls/` module

```
app/calls/
  __init__.py
  base.py                      # CallState, CallSession ABC
  twilio_client.py             # TwilioCallManager wrapper
  twiml_generator.py           # TwiML XML generation for call flow
  call_orchestrator.py         # Main orchestration loop (state machine)
  voicemail_detector.py        # Silence heuristic + prompt detection
  call_router.py               # Route language/intent to LLM
  ws_handler.py                # WebSocket RTP media streaming
  call_state.py                # In-memory session state per call_id
  cost_estimator.py            # Twilio rate lookup + total estimation
```

**New VG endpoints:**

- `POST /v1/calls/start` — initiate call
  - Request: `{phone: "+1...", goal: "...", approved_cost_usd: 0.50}`
  - Response: `{call_id, status: "ringing", estimated_duration_sec: 120}`

- `GET /v1/calls/{id}/status` — poll call state
  - Response: `{status, transcript_so_far, speaker_segments, current_speaker, duration_sec}`

- `POST /v1/calls/{id}/end` — manual hangup
  - Response: `{status: "completed", transcript, audio_url, cost_actual_usd}`

- `GET /v1/calls/{id}/cost-estimate` — pre-call cost
  - Request: `{target_phone, estimated_duration_min}`
  - Response: `{currency: "USD", amount: 0.20}`

- `GET /v1/calls/history` — list past calls
  - Response: `[{call_id, date, target_phone, goal, duration_sec, outcome}, ...]`

### 4.2 Krab agent: new `call_assistant_handler.py`

New MCP tool category: `call_*`. Examples:

```python
@mcp_tool
def call_generate_system_prompt(goal: str, target_name: str = None) -> str:
    """
    Generate system prompt for call goal.
    Examples:
      - goal="спроси про слот в среду"
      - goal="подтверди доставку посылки"
      - goal="узнай про скидку на подписку"
    """
    prompt = f"""Это AI-ассистент Павла Сергеева.
Ты сейчас звонишь {target_name or 'в организацию'}.

Твоя задача: {goal}

Разговаривай естественно, кратко, вежливо.
Если услышишь "СТОП" или "STOP" — сразу заверши звонок.
Если окончательный ответ получен — скажи "Спасибо" и заверши.
Если вопрос не по теме — деликатно вернись к цели.

Начни с приветствия и представления."""
    return prompt

@mcp_tool
def call_check_opt_out_status(caller_phone: str) -> dict:
    """
    Check if phone number has opted out (said "СТОП").
    Returns {opted_out: bool, reason: str, timestamp: ISO}.
    """
    # Check memory_engine for prior STOP requests
    ...

@mcp_tool
def call_save_transcript(call_id: str, transcript: str, 
                        audio_url: str, cost_usd: float) -> dict:
    """
    Persist call log to memory_engine.
    Returns {memory_id, searchable: True}.
    """
    ...

@mcp_tool
def call_check_goal_achieved(goal: str, transcript: str) -> bool:
    """
    LLM heuristic: did conversation achieve the goal?
    """
    # Embedded LLM intent matching via qwen3-4b
    ...
```

Integrates with existing `memory_engine` for persistent call history searchable from Telegram.

### 4.3 Krab Ear .app: new `CallAssistantViewController`

**New files:**

```
native/KrabEarAgent/Sources/KrabEarAgent/
  CallAssistantViewController.swift         NEW (main call dialog)
  CallAssistantViewController+Dialer.swift  NEW (phone entry, validation)
  CallAssistantViewController+Cost.swift    NEW (cost estimation display)
  CallAssistantViewController+Live.swift    NEW (transcript + status during call)
  PhoneNumberFormatter.swift                NEW (parse/validate RU/US/ES formats)
  CallHistoryView.swift                     NEW (list past calls + transcripts)
```

**UI layout — Call Initiator Dialog:**

```
┌─ Позвонить ─────────────────────────────┐
│                                          │
│ Номер телефона:                          │
│ ┌────────────────────────────────────┐   │
│ │ +7 (999) 123-45-67                 │   │
│ │ [Paste from clipboard ⟳]           │   │
│ └────────────────────────────────────┘   │
│                                          │
│ Цель (что спросить):                     │
│ ┌────────────────────────────────────┐   │
│ │ Спроси про свободный слот в среду  │   │
│ └────────────────────────────────────┘   │
│                                          │
│ Смета:                                   │
│  Продолжительность: ~2-3 минуты          │
│  Стоимость: ~$0.04-0.06 USD             │
│  [ℹ Как считается?]                     │
│                                          │
│           [Отмена]   [Позвонить]        │
│                                          │
└──────────────────────────────────────────┘
```

**UI layout — Live Call View (during conversation):**

```
┌─ Звонок со +7 (999) 123-45-67 ──── 2:34 ─┐
│                                           │
│ 🟢 Подключен                              │
│                                           │
│ ┌─────────────────────────────────────┐   │
│ │ AI:  "Здравствуйте, это ассистент   │   │
│ │      Павла. Я вам звоню в связи..." │   │
│ │                                     │   │
│ │ Человек:  "Привет, слушаю"         │   │
│ │                                     │   │
│ │ AI:  "Спасибо. Я звоню чтобы..."   │   │
│ │ (текущий фрагмент)                  │   │
│ └─────────────────────────────────────┘   │
│  ▲ (scroll up for earlier utterances)    │
│                                           │
│ [⏹ Завершить]      [Отключить звук]     │
│                                           │
└───────────────────────────────────────────┘
```

---

## 5. Telephony Provider Choice

### 5.1 MVP: Twilio

**Rationale:**
- Already integrated in Voice Gateway (Phase 2 fallback)
- Managed service (no infrastructure overhead)
- 1-2 PR effort (wrapper layer only)
- Pay-as-you-go pricing (~$0.013/min) acceptable for MVP
- Built-in recording compliance support

**Twilio credentials setup:**
```bash
export KRAB_EAR_TWILIO_ACCOUNT_SID="ACxxxxxxx"
export KRAB_EAR_TWILIO_AUTH_TOKEN="secret"
export KRAB_EAR_TWILIO_FROM_PHONE="+14155552671"  # Twilio-provided caller ID
```

**Rate limits:** 100 concurrent calls per account (default); sufficient for single-user MVP.

### 5.2 Future: Telnyx

**When to switch:** if Twilio rate increases or platform becomes bloated

**Telnyx advantages:**
- Similar API surface (easier migration than LiveKit)
- Better international rates (RU/ES focused)
- Bulk SMS/voice SLA better than Twilio

**Effort:** 4-5 PRs (new provider abstraction, API integration)

### 5.3 Long-term: LiveKit + FreeSWITCH (DIY platform)

**When to build:** if:
- Self-hosting becomes requirement (cost reduction at scale)
- Carrier-grade requirements (SLA, compliance, redundancy)

**Effort:** 8-12 weeks (separate voice infrastructure track)

---

## 6. Hardware + Service Requirements

### 6.1 Compute budget (M4 Max 36GB, RU call with LLM)

| Component | RAM | Notes |
|-----------|-----|-------|
| Krab Ear .app | 200 MB | UI only |
| Voice Gateway (VG) | 2-3 GB | Twilio WS, STT/TTS buffering, no model (inference remote) |
| Qwen3-30B (LM Studio) | 17.2 GB | Active during call (concurrent with Phase 1/2) |
| OS + browser | 5 GB | |
| **Total** | **~24-25 GB** | Safe margin to 36 GB |

**No new GPU/hardware required** — call audio streams to LM Studio (existing) + Twilio (offsite).

### 6.2 Network bandwidth

- **Uplink:** ~32 kbps (Opus 16 kHz 16-bit mono)
- **Downlink:** ~64 kbps (TTS Opus + JSON metadata)
- **Peak:** ~100 kbps (both directions + signaling)
- **Requirement:** Stable 1+ Mbps home internet sufficient

### 6.3 Twilio API costs

| Duration | Cost (USD) | Notes |
|----------|-----------|-------|
| 5 min call | $0.065 | Typical "clinic inquiry" |
| 10 min call | $0.13 | Typical "support call" |
| 30 min call (max) | $0.39 | Edge case (auto-hangup safety) |

**Cost control:**
- Pre-estimate + user confirmation before dialing
- Auto-hangup at 30 min (configurable)
- Track cumulative cost per month in settings

---

## 7. Privacy & Compliance

### 7.1 TCPA Compliance (Telephone Consumer Protection Act)

**Mandatory requirements for US calls:**

1. **AI Self-disclosure (upfront, mandatory)**
   ```
   "Это AI-ассистент. Вас звонит компьютеризированная система..."
   (English: "This is an AI assistant. You're being called by a computerized system...")
   ```
   - Must play before any other content
   - User cannot skip/suppress
   - Logged in transcript

2. **Caller ID verification**
   - Must use Twilio-provided number (number spoofing prohibited)
   - Twilio handles SHAKEN/STIR signing (bundled)

3. **STOP / Do-Not-Call registry compliance**
   - Recipient says "СТОП" / "STOP" → immediate hangup
   - Store opt-out in Krab agent memory (check before future calls)
   - Do-Not-Call registry check (manual responsibility — not automated)

4. **Recording consent & disclosure**
   - "Этот звонок записывается" (upfront notification)
   - Twilio `record=True` captures for compliance
   - User responsible for local jurisdiction consent (some states require one-party consent, others two-party)

### 7.2 GDPR / International compliance

- **EU calls:** May require additional consent. Responsibility delegated to user (Krab agent warning: "Call to EU numbers may require explicit GDPR consent").
- **RU numbers:** No specific restrictions (as of 2026-Q2).
- **User owns compliance:** Krab Ear disclaims liability; system logs intent for audit trail.

### 7.3 Logging & audit trail

**Persistent call records:**

```json
{
  "call_id": "c_abc123xyz",
  "timestamp": "2026-04-20T14:32:00Z",
  "target_phone": "+7XXXXXXXXXX",  // hashed before storage
  "goal": "узнай про скидку",
  "ai_self_disclosure_played": true,
  "opt_out_requested": false,
  "duration_sec": 245,
  "transcript": "AI: Здравствуйте... HUMAN: Привет...",
  "speaker_diarization": [
    { "speaker": "AI", "start": 0, "end": 3.5 },
    { "speaker": "HUMAN", "start": 3.5, "end": 7.2 }
  ],
  "audio_url": "s3://krab-ear-backups/calls/c_abc123xyz.wav",
  "outcome": "completed",  // completed|no_answer|voicemail|opt_out
  "cost_usd": 0.065,
  "legal_note": "User consented to recording."
}
```

**Storage:** Encrypted at rest in `~/.krab_ear_data/calls/` (local) + optional S3 backup.

**Retention:** 7 years (TCPA audit requirement) by default; user-configurable retention policy.

---

## 8. Call Lifecycle Details

### 8.1 Initialization (user clicks "Позвонить")

1. Krab Ear validates phone number (format check, not availability)
2. VG cost estimator queries Twilio API: `GET /v1/calls/cost-estimate?duration=180`
3. UI shows cost in USD + confirmation dialog
4. User clicks "Позвонить" → sends IPC: `start_call(phone, goal, approved_cost_usd)`
5. VG creates call_id, allocates session state, prepares Twilio WebSocket
6. Krab agent generates system prompt via `call_generate_system_prompt(goal)`

### 8.2 Dialing phase

1. VG calls `TwilioCallManager.dial_outbound(from_phone=TWILIO_NUM, to_phone=user_input, twiml_url="...")`
2. Twilio initiates call, plays ringback tone to user (locally audible via speaker)
3. Recipient's phone rings (up to 30s wait)
4. Status polling via `GET /v1/calls/{id}/status` every 2s, UI shows "🔔 Рингует..."

### 8.3 Answer detection

1. Recipient picks up → Twilio WebSocket connects with incoming audio stream
2. VG detects "answer" condition (audio energy > silence threshold for >500ms)
3. AI plays self-disclosure: "Это AI-ассистент Павла Сергеева."
4. Wait 1s for recipient acknowledgment (or proceed)
5. AI: Krab agent generates opening: `call_generate_system_prompt(goal)` → qwen3-30b → TTS → Twilio
6. Conversation loop starts

### 8.4 Conversation loop (RealTime bidirectional)

1. **Recipient speaks:**
   - VG receives PCM 16kHz mono via Twilio WebSocket
   - VAD (voice activity detection) buffers until silence >400ms
   - SeamlessM4T/Moshi STT → text transcript

2. **AI processes:**
   - Krab agent receives transcript via internal call_handler
   - LLM (qwen3-30b) processes with context:
     - System prompt (goal)
     - Prior exchange (conversation history)
     - Available MCP tools (memory, search, etc.)
   - LLM generates next utterance

3. **AI responds:**
   - VG TTS: text → Opus audio 24 kHz mono
   - Stream audio via Twilio WebSocket
   - UI updates with AI transcript segment

4. **Loop continues** (or termination triggers — see Section 3.2 state machine)

### 8.5 Termination conditions

1. **Recipient says "СТОП" / "STOP":**
   - VG detects keyword → immediate hangup
   - Outcome: `{status: "opt_out", opted_out_phone: [hashed]}`

2. **AI goal achieved:**
   - Krab agent calls `call_check_goal_achieved(goal, transcript)` heuristic
   - LLM returns `True` → AI: "Спасибо. До свидания." → hangup

3. **Silent >10s:**
   - **[QUESTION #7]** Hang up automatically or send probe "Здравствуйте?"?
   - Awaits user brainstorm decision

4. **Call duration >30 min:**
   - VG enforces max duration limit
   - Auto-hangup with message: "Звонок завершен. Спасибо."

5. **Voicemail detected (silence >3s from answer):**
   - VG heuristic: answer followed by no human speech
   - AI leaves templated message: "{goal}. Перезвоните мне, пожалуйста."
   - Hangup → outcome: `{status: "voicemail"}`

### 8.6 Post-call processing

1. VG receives `completed` status from Twilio
2. Save audio to `~/.krab_ear_data/calls/{call_id}.wav` (Twilio stream)
3. Persist call metadata to Krab agent memory via `call_save_transcript(...)`
4. Append to Krab Ear history NDJSON:
   ```json
   {
     "id": "hist_xyz",
     "type": "call_automation",
     "timestamp": "2026-04-20T14:32:00Z",
     "call_id": "c_abc123xyz",
     "target_phone": "[hashed]",
     "goal": "узнай про скидку",
     "outcome": "completed",
     "duration_sec": 245,
     "transcript_preview": "AI: Здравствуйте... HUMAN: Привет...",
     "cost_usd": 0.065,
     "mode": "call_automation"
   }
   ```
5. UI shows summary toast: "✅ Звонок завершен. 4m 5s. Стоимость: $0.07. Сохранено в историю."

---

## 9. Phasing — 4-5 PRs (3-4 weeks)

| PR | Title | Files | Effort | Owner | Dependencies |
|----|----|----|----|----|----|
| #1 | `feat(call): Twilio integration layer + cost estimator` | app/calls/twilio_client.py, cost_estimator.py | 3-4d | VG agent | None |
| #2 | `feat(call): VG /v1/calls/* endpoints + state machine` | app/calls/call_orchestrator.py, ws_handler.py, twiml_generator.py | 5-6d | VG agent | PR #1 |
| #3 | `feat(call): Krab agent call_assistant_handler + MCP tools` | call_assistant_handler.py, call_tools.py | 3d | Krab agent | PR #1, #2 |
| #4 | `feat(ui): Call Initiator dialog + live call view` | CallAssistantViewController*.swift | 4-5d | iOS agent | PR #1, #2, #3 |
| #5 | `test(e2e): Call automation integration tests` | tests/test_call_e2e.py | 2-3d | VG agent | PR #1-#4 |

**Timeline:** ~3-4 weeks (sequential PRs with overlapping code review/testing)

**Success criteria per PR:**

- **PR #1:** Unit tests for Twilio mock, cost estimator accuracy within ±$0.01
- **PR #2:** State machine tests for all 6 termination paths, latency <400ms per cycle
- **PR #3:** Krab agent can generate contextual system prompts, MCP tools callable
- **PR #4:** UI renders correctly on macOS 13+, hotkey bind optional (v1.1)
- **PR #5:** End-to-end test: dial test number, play voicemail, verify transcript saved, cost logged

---

## 10. Acceptance Criteria (measurable)

1. **Outbound call connects** within 5s of user click
2. **AI self-disclosure plays** before any conversational content
3. **Real-time transcript** visible during call (latency <2s)
4. **Recipient speech recognized** with ≥80% word accuracy (multilingual)
5. **AI response latency** <1.5s (user perceives natural conversation)
6. **Opt-out ("STOP") detected and honored** — hangup <500ms
7. **Call transcript persisted** to Krab agent memory (searchable from Telegram)
8. **Audio recording saved** to local storage + optional S3 backup
9. **Cost estimate accurate** within ±10% (Twilio actual vs. predicted)
10. **Call duration max 30 min** enforced (auto-hangup + warning)
11. **Voicemail detected** (silence >3s) and handled without crash
12. **Multi-language support:** RU → RU, EN → EN, ES → ES (basic quality check)
13. **0 data leaks:** phone number hashing, no logs in stdout except audit trail
14. **TCPA compliance checklist passed** (legal review before launch)

---

## 11. Open Questions (from brainstorm — user decision required)

These 7 items are **explicitly deferred** pending user input before implementation begins:

1. **Phone number input method:**
   - Clipboard paste only (current plan)?
   - OR manual text entry?
   - OR address book lookup?
   - **Decision:** User preference → affects PR #4 scope

2. **Human interruption timing:**
   - Should AI stop **immediately** when human starts speaking mid-sentence?
   - OR finish current word, then yield?
   - OR configurable via settings?
   - **Decision:** UX preference → affects Voice Gateway audio loop

3. **Outbound transcript storage:**
   - Store in **same** history as regular dictation (mixed)?
   - OR separate `/calls/` NDJSON file?
   - **Decision:** Affects history schema + UI search

4. **Multi-step goals (Phase 3.x deferred):**
   - Support chained intent ("call clinic, ask Wed, if no ask Thu")?
   - OR single-goal only (current plan)?
   - **Decision:** Affects system prompt generation + conversation flow

5. **Auto-end calls >30 min:**
   - Hard 30-min limit (current plan)?
   - OR configurable (10/20/30/unlimited)?
   - **Decision:** Cost control + user preference

6. **Cost estimate display:**
   - Show before dialing (required for compliance)?
   - AND show live cost ticker during call?
   - **Decision:** UI design

7. **Silence >10s handling:**
   - Auto-end call (fail-safe)?
   - OR send probe: "Здравствуйте?" (retry)?
   - OR wait indefinitely?
   - **Decision:** Affects voicemail detection + conversation UX

---

## 12. Risks

### 12.1 Legal & Compliance

| Risk | Severity | Mitigation |
|------|----------|-----------|
| TCPA violation (missing AI disclosure) | 🔴 Critical | Mandatory upfront disclosure in code; legal review gate (PR #5) |
| Phone number hashed incorrectly, PII exposed | 🔴 Critical | Encryption + audit logging; data retention policy user-controlled |
| Call recording without local consent | 🟡 High | User responsible for jurisdiction; Krab Ear logs consent intent |
| Caller ID spoofing (if Twilio account hijacked) | 🔴 Critical | Secure credential storage; Twilio account 2FA required |

### 12.2 Technical

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Twilio WS connection drops mid-call | 🟡 High | Auto-reconnect logic + graceful hangup; user notified immediately |
| LLM hallucination (AI says wrong info) | 🟡 High | System prompt scope limits (single goal); user verifies before dialing |
| Cost estimation inaccuracy (surprise charges) | 🟠 Medium | Estimate refreshed every 60s; hard cap at user-set monthly budget |
| Voicemail detection false positive (cuts off human) | 🟠 Medium | Configurable silence threshold; [Q7] requires brainstorm |
| Voice Gateway outage (no fallback) | 🟡 High | LM Studio on same machine — no remote fallback available |

### 12.3 User Experience

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Unexpected call cost (first-time user) | 🟠 Medium | Cost estimate + confirmation dialog mandatory |
| Recipient confused (sounds robotic) | 🟠 Medium | System prompt mentions "AI assistant" upfront |
| Call quality poor (lag, echo) | 🟡 High | Network quality check; fallback to text-only mode if needed |

---

## 13. Success Metrics

1. **Adoption:** ≥5 user calls in first week (internal testing)
2. **Reliability:** ≥95% calls complete (no crashes, premature hangups)
3. **Quality:** User satisfaction ≥4/5 (post-call survey)
4. **Cost accuracy:** Actual vs. estimated cost variance <10%
5. **Compliance:** 0 TCPA violations, legal audit passed
6. **Performance:** p95 AI response latency <2s
7. **Retention:** ≥30% calls re-attempted within 7 days (indicates usefulness)

---

## Appendix: Acceptance Test Plan (PR #5 implementation)

**Test 1: Outbound call → voicemail**
```
1. Start call to test number (configured in env)
2. Verify: ringback tone plays to user
3. Test number answers with >3s silence (voicemail simulation)
4. Verify: AI detects voicemail, leaves message, hangs up
5. Verify: transcript saved, outcome = "voicemail"
6. Verify: cost logged correctly
```

**Test 2: Bidirectional conversation**
```
1. Start call to interactive test bot
2. AI plays: "Здравствуйте, это AI-ассистент"
3. Test bot responds: "Привет, что нужно?"
4. AI processes and responds with goal-related statement
5. Verify: transcript shows alternating AI/HUMAN turns
6. Verify: latency <1.5s per turn
7. Verify: call completes with outcome = "completed"
```

**Test 3: STOP command**
```
1. During call, test bot says "СТОП"
2. VG must detect keyword and hangup within 1s
3. Verify: transcript shows "STOP" keyword
4. Verify: call ends immediately, outcome = "opt_out"
5. Verify: phone number stored in opt-out registry (memory_engine)
6. Verify: future call attempts show warning in UI
```

**Test 4: Cost estimation**
```
1. Pre-call: estimate for 2-min duration
2. User confirms
3. Actual call runs 2m 15s
4. Verify: actual cost within ±$0.01 of estimate
5. Verify: cost logged + UI summary shows correct amount
```

**Test 5: Multilingual conversation (RU)**
```
1. Start call with RU goal
2. Test bot speaks Russian
3. AI responds in Russian with relevant info
4. Transcript shows RU language tag
5. Verify: translation glossary lookup works (existing Krab Ear feature)
```

---

**End of Specification**

---

**NEXT STEP:** User reviews 7 open questions (Section 11) → confirms decisions → implementation plan drafted (separate doc)
