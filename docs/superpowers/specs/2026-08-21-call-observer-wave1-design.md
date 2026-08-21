# Call Observer — Wave 1 «Watch» (macOS client for Voice Gateway calls)

Date: 2026-08-21 · Status: draft for review · Owner decision log at bottom.

## TL;DR (RU)

Наблюдение за звонком агента в реальном времени: HUD-плашка появляется при звонке,
клик разворачивает в полное окно; живой транскрипт обеих сторон с переводом; живое
аудио по кнопке (настройка); кнопка «положить трубку». Чисто Swift-клиент к
существующему WS/REST-контракту Voice Gateway — их бэкенд не меняем, наш Python
в тракте звонка не участвует (2 ключа настроек + файл-токен для auth). Волна 2 (takeover,
whisper, быстрые ответы) — отдельная спека.

## 1. Scope

**In (wave 1):** live audio listen, live two-sided transcript + translation,
hangup button, HUD → expandable panel, settings (HUD on/off, autoplay
audio; VG url/api-key settings already exist and are reused).

**Out:** takeover / whisper / quick suggestions (wave 2, separate spec);
seamless translator with agent voice (owner want #7 — Voice Gateway scope);
any persistence of call data on our side (VG's CallRecorder already records);
new global hotkeys.

## 2. Verified VG contract (facts, file:line in sibling repo `Krab Voice Gateway`)

All endpoints require auth (`_auth_required`, `app/main.py:2244`): Bearer
`KRAB_VOICE_API_KEY` in `Authorization` header (works on WS too — their iOS
client sets the header on the WS URLRequest, `GatewayStreamClient.swift:213`)
or `?token=` query. Empty key on VG side = auth-off dev mode (`dev_owner`).

🔴 **Swift token channel (verified constraint):** the owner's prod HAS a real
key (43 chars in live settings.json) and our existing setting
`voice_gateway_api_key` (`core/config.py:879`) is in `SENSITIVE_FIELDS`
(`settings_backup.py:30`), so IPC `get_settings` returns `REDACTED` — the
Swift client can NEVER obtain the key via IPC. Resolution: the backend
maintains `<data_dir>/vg_client_token` (0600, atomic tmp+replace write) —
written at startup and on every settings save that changes the key; content =
the EFFECTIVE runtime value of `voice_gateway_api_key` (i.e. after any
`KRAB_EAR_*` env override, not the raw settings.json field — otherwise the
file and the key VG actually expects diverge; may be empty). Swift reads this file when
connecting; empty/absent → no Authorization header (matches VG auth-off).
This mirrors the existing `event_bridge_token` pattern (same trust boundary:
same-user local processes; settings.json already stores the secret on the
same disk). Purge guard: `vg_client_token` goes into
`scripts/purge_coverage_allowlist.txt` with a reason comment — security key,
not user content (exact precedent: `event_bridge_token`, allowlist line 43).

### 2.1 Session discovery
`GET /v1/sessions?status=&source=&limit=` (`app/main.py:3051`) →
`{ok, count, items: [SessionState]}`. `SessionState` (`app/models.py:90`):
`id, status, phone, call_direction ("inbound"|"outbound"|""), created_at,
updated_at (ISO), src_lang, tgt_lang, source, call_brief, meta`.
`status ∈ {created, running, paused, stopped, failed}` (`app/models.py:18`).

### 2.2 Event stream
WS `/v1/sessions/{id}/stream` (`app/main.py:4160`). Envelope
`{"type": str, "ts": iso, "data": {...}}`. On connect server immediately sends
`call.state {session_id, status}`. A session deleted on VG side yields
`call.closed` + close 1000 — client MUST stop reconnecting on it.

🔴 Review round 1 (contract lens) corrections baked in below: `stt.partial`
DOES NOT EXIST server-side (0 publish sites; VG CLAUDE.md: fake PSTN partials
were deliberately removed) — the live transcript is FINALS ONLY, exactly like
their own iOS client lives today. `translation.partial` exists but is an
owner-side quick-phrase event with different fields — ignored.

Events consumed in wave 1 (payload shapes verified at publish sites):

| type | data | notes |
|---|---|---|
| `stt.final` | `text` (required); `language, confidence, engine` OPTIONAL — truncated variants exist: takeover path `{text, language}` (`app/main.py:9043`), realtime engine `{text}` (`app/engines/realtime.py:867`) | remote-party speech in agent calls. 🔴 Known limitation: the owner-mic path of translator-mode calls publishes owner speech as the SAME `stt.final` with no distinguishing field (`app/main.py:3296`) — misattributed until VG adds an `origin` field (asked, brief item e) |
| `translation.final` | `text, source_text, src_lang, tgt_lang, provider` (field is `provider`, not `engine`) | translation of the above |
| `agent.response` | `text` (required); `text_ru, lang, utterance_ts, action` OPTIONAL — FOUR publish sites with different field sets (`app/main.py:8338, 8899`, `app/routers/prompt_call.py:691`, `app/engines/realtime.py:942` — the last has no `text_ru`/`action`) | agent reply; render `text_ru` under `text` when present |
| `agent.suggestion.auto_spoken` | `text, text_ru, action, digits, goal_reached` (`app/main.py:8277`) | 🔴 Assisted-mode auto-timeout speaks WITHOUT emitting `agent.response` — without this event the panel silently loses agent speech; render as an agent line |
| `agent.interrupted` | `utterance_ts, spoken_fraction, spoken_text` (`app/main.py:7149`) | match strictly by `utterance_ts` (VG hard rule); replace the displayed reply with `spoken_text` + badge «прервано (N %)» — show what the caller actually HEARD, not just a strikethrough |
| `call.state` | `status`; `muted, held` OPTIONAL (mute/hold paths, `app/main.py:4624, 4656`; hold flips status to `paused`) | status dot + «mute»/«hold» badges |
| `call.ringing` / `call.answered` | `call_sid, twilio_status, provider` — NO `status` field (`app/main.py:5553-5567`) | lifecycle markers only |
| `call.ended` | `reason, provider` | into the §4.1 automaton |
| `call.closed` | `{session_id}` | terminal for EVERY call (not only deleted sessions; may arrive delayed after auto-summary) → permanent stop of reconnect |
| `diagnostic.error` | error info | badge «реплика не переведена» (their iOS shows an 8 s plaque — same treatment) |
| `screening.started` | screening info (`app/main.py:5135`) | badge «скрининг» — the flagship inbound scenario |
| `cost.alert` | `level, threshold_usd, current_usd, message` (`app/main.py:2176`) | threshold ALARM only (fire-once per session/day) — an alert badge, NOT a ticker |

Explicit ignore-list for wave 1 (decoder skips silently; unknown types too —
forward-compat): `translation.partial` (owner quick-phrase, different fields),
`agent.suggestion` (pending-suggestion UI is wave 2), `agent.whisper.ack`,
`agent.bridge_spoken` (empty payload), `tts.ready`, `dtmf.received/sent`,
`stt.numbers_uncertain`, `post_call.ready` (wave-2 candidate: free final
summary screen), `hold.*`, `engine.*`, `summary.ready`.

Live cost source (VG answer d, verified by them against code): poll
`GET /v1/sessions/{id}/diagnostics` (`app/main.py:3557`) while the panel is
open (3 s cadence) and read `costs.total_usd` — present from session creation
(default 0.0, `app/main.py:1392`), updated on every session event
(`app/main.py:1706`). No push event for cost growth exists.

### 2.3 Live audio monitor
WS `/v1/sessions/{id}/monitor/audio` (`app/main.py:4271`,
`app/live_monitor.py`): server-side mix of both tracks. First frame is JSON
`{"format": "mulaw_8k", "frame_ms": 100}`, then binary μ-law frames
(800 bytes = 100 ms @ 8 kHz mono). Close codes: 1008 = session missing or
terminal; 1013 = subscriber limit (`KRAB_MONITOR_MAX_SUBSCRIBERS`, default 2);
1000 = call ended (sentinel). Server backpressure: per-subscriber queue ≈ 1 s,
drops OLDEST frames — client never needs catch-up logic, just play what arrives.

### 2.4 Hangup
`POST /v1/telephony/calls/{session_id}/hangup` (`app/main.py:4513`) →
`{ok, session_id, call_sid, status, already_terminal?}` (success carries
`status: "completed"` and may carry `provider_id_pending`); 404
`session_not_found`; 409 `session_terminal` (race with natural end,
`app/main.py:4570`) — treat exactly like `already_terminal`; 502 on provider
error. Idempotent on terminal sessions (`already_terminal: true`).

## 3. Architecture

Swift-only client inside the existing agent (`native/KrabEarAgent`). Our Python
backend is NOT in the call path; its whole diff is 2 settings keys + the
`vg_client_token` file writer (§5). All VG
traffic is loopback (`voice_gateway_url` setting, default `http://127.0.0.1:8090`).

### Components (new files, flat in `Sources/KrabEarAgent/` per project convention)

1. **`VGSessionWatcher.swift`** — discovery + lifecycle owner.
   - Polls `GET /v1/sessions?limit=20` off-main. Cadence: 3 s while VG
     reachable and no live call; 2 s during a live call (cheap freshness for
     the HUD timer/status); backoff 15 s → 60 s while VG is unreachable.
   - 🔴 VG absent/down is an EXPECTED state (the gateway is not always
     running). Unreachable → silent (log at DEBUG once per state change, no
     ERROR spam — this is the exact "dead cloud STT fallback noise" class we
     just paid for).
   - Live-call predicate: `status ∈ {created, running, paused}` ∧
     (`phone ≠ ""` ∨ `call_direction ≠ ""`) ∧ `updated_at` within 6 h.
     Confirmed by the VG session against their code (2026-08-21): inbound
     calls fill `phone`/`call_direction` in the same webhook handling right
     after create (`app/main.py:4919-4940` — the race window is not observable
     at poll cadence); 6 h matches their own
     `stale_running_session_max_age_hours = 6.0` (`app/config.py:267`), the
     constant guarding the exact same stuck-"running" failure mode. No
     server-side `?source=` filter: Telegram-transport calls share
     `source="twilio_pstn_outbound"`, so the client predicate is the filter.
   - Telegram-transport agent calls pass the predicate too — INCLUDED by
     design: they are real calls on the same audio path/contract, and the
     owner wants to observe agent calls regardless of transport.
   - Outbound translator-mode calls fill `phone`/`call_direction` only AFTER
     the provider `create_call` returns (`app/main.py:4446`) — a subsecond
     window where the predicate misses a just-born session; costs at most one
     poll tick, accepted. Prompt-calls set `phone` at create
     (`app/routers/prompt_call.py:162`).
   - 🔴 Fail ≠ absent: a failed/timed-out poll is UNKNOWN, never `callGone`.
     `callGone` fires only from a SUCCESSFUL poll whose items lack the
     session, and only after 2 consecutive such polls (absent-streak — the
     project's "single observation instead of streak" lesson). While a live
     call is being observed, 3 consecutive FAILED polls (≈ ≥ 30 s
     unreachable) emit a one-shot `vgLost` into the call-end automaton
     (§4.1): every sticky "reconnecting…" state owns its timeout exit.
   - Emits `callAppeared(SessionState)` / `callGone(id)` to the coordinator.
     Multiple concurrent calls: track all, HUD shows the newest, panel has a
     session picker only if >1 (rare; simple segmented control).
2. **`VGCallStreamClient.swift`** — events WS. Port of VG's own
   `ios/KrabVoiceiOS/GatewayStreamClient.swift`: `URLSessionWebSocketTask`,
   Bearer header, exponential backoff 1 s → 30 s ±25 % jitter, ping every
   25 s, decode envelope → typed Swift enum. `call.closed` → permanent stop.
   Delivers parsed events on main queue to the UI models.
3. **`CallAudioPlayer.swift`** — audio WS + playback.
   - Own `URLSessionWebSocketTask` to `/monitor/audio`. First message JSON
     (validate `format == "mulaw_8k"`), then binary frames.
   - μ-law → PCM16 via 256-entry lookup table (standard G.711; no external
     deps). Feed `AVAudioPlayerNode` on an `AVAudioEngine` with an 8 kHz mono
     `AVAudioFormat`; the engine's mixer resamples to hardware rate.
   - Connect ONLY while listening is on (button / autoplay setting). This
     also conserves VG's 2-subscriber limit for the iOS client.
   - 🔴 Listen state has ONE owner (the coordinator/player); the HUD and the
     panel buttons are two renders of that one state (sibling-symmetry).
     Connect/disconnect are single-flight with a generation token: a new
     connect cancels the previous WS task AND invalidates its ping timer;
     toggling during an in-flight connect never yields two sockets (two
     sockets would exhaust VG's 2-subscriber limit by ourselves and kick the
     owner's iOS client to 1013).
   - Device change / sleep: subscribe to
     `AVAudioEngineConfigurationChange` and restart the engine; after wake,
     reconnect the audio WS. The button reflects the ACTUAL running player,
     never the requested state — no "looks on, plays nothing" lie.
   - Close 1013 → show "лимит слушателей" hint in UI, do not retry-loop
     (retry only on explicit re-press). 1008/1000 → stop, reflect call end.
4. **`CallObserverHUD.swift`** — floating `NSPanel` (pattern:
   `LiveSubtitlesOverlay`): always-on-top, draggable, ~340 px wide. Shows
   status dot + direction + phone + elapsed timer, last 2 replicas (each:
   original + translation, dimmed partials), listen-toggle and hangup
   buttons — SF Symbols `speaker.wave.2` / `phone.down.fill`, NOT emoji/
   Unicode glyphs (CoreText first-render hang class, AGENT-J/M precedent;
   StatusIndicatorView migrated to SF Symbols for the same reason). Click
   elsewhere → expand to panel (HUD hides); click = mouseUp without
   movement, so it does not fight `isMovableByWindowBackground` dragging.
5. **`CallObserverPanelController.swift`** — `NSWindowController` (pattern:
   `MeetingLivePanelController` visuals, but data source is the WS client, not
   SSE): full scrolling transcript feed (both sides, translations under
   originals, `agent.interrupted` replicas replaced by their `spoken_text`
   prefix + badge «прервано (N %)»), listen toggle, hangup button, cost line
   (polls `GET /v1/sessions/{id}/diagnostics` → `costs.total_usd` every 3 s
   while the call is live — §2.2; stops at terminal), connection badge
   («reconnecting…» on WS drop — panel stays open). Transcript feed capped at
   500 entries in memory. 🔴 At call end the panel is NOT auto-closed: it
   enters a terminal state (streams stopped, badge «завершён») and closes
   only manually — wave 1 persists nothing, so auto-close would destroy the
   only copy of the transcript the owner may still be reading. Only the HUD
   auto-hides (linger, §4.1).
6. **`main+CallObserver.swift`** — wiring: single owner
   (`callObserverCoordinator`) in `AgentAppDelegate`; starts the watcher at
   agent startup INDEPENDENTLY of backend health: settings bools come via IPC
   off-main, but IPC failure falls back to defaults (true/false) and the
   token is read from the file, never IPC — a dead Python backend must not
   blind us to VG calls. Status-menu item «Звонок агента…» (disabled when no
   live call) as manual entry to the panel.
   - Settings propagation: there is NO generic settings-change notification
     in the agent (verified) — the two checkboxes in the settings UI call
     the coordinator directly after `set_settings` (project pattern, e.g.
     `setPrivacyMode`), AND the coordinator re-reads the two bools +
     `privacy_mode_enabled` on each poll tick (cheap, self-healing).
   - 🔴 Privacy: when `privacy_mode_enabled` is on, auto-show of the HUD and
     audio autoplay are SUPPRESSED (every other live-transcript surface —
     meeting panel, live subs, wake word — respects privacy mode; this one
     must not be the asymmetric sibling that pops a live transcript over
     someone's screen-share). Manual open via the status menu stays allowed —
     an explicit owner action.

### Threading & UI rules (project invariants, apply to every component)
- All network (REST + WS) strictly off-main; UI mutations on main.
- IPC (settings read) off-main (AGENT-3 rule).
- No `runModal` — hangup confirmation via `presentAlertSheet` on the panel
  window; from the HUD, hangup opens the panel first, then the sheet (HUD has
  no window suitable for sheets → avoids a detached modal).
- Any new non-ASCII glyph passes the glyph gate (CoreText hang class).
- New files added to the Swift package target; bundle + runtime binary parity
  after deploy (LC_UUID check).

### Hangup flow
Button → confirm sheet («Положить трубку? Звонок агента будет завершён») →
POST off-main, button disabled while in-flight → on `{ok}` or
`already_terminal` rely on the §4.1 automaton; on 502 show error toast,
re-enable. 404 `session_not_found` AFTER the session went terminal is
silent (the call ended while the confirm-sheet was open — the automaton
closes the sheet, and a late "hangup failed" toast for an already-dead call
would be noise). Single-flight guard (bool) against double-click.

## 4. Behavior details

### 4.1 🔴 Call-end: ONE-SHOT terminal automaton (review round 1, 3×HIGH root)

Four independent paths can signal "this call is over": `call.ended` on the
events WS, `callGone` from the watcher, `already_terminal` in the hangup
response, and `vgLost` (watcher unreachable-timeout, §3.1). ALL of them
route through a per-session one-shot guard (`terminalDelivered`, precedent:
`deliverFinished` in `MeetingLivePanelController`); the first signal wins,
the rest are no-ops. Terminal actions run exactly once: stop events-WS +
audio player, close an open hangup confirm-sheet, panel → terminal state
(stays open, §3 component 5), HUD → 3 s linger «Звонок завершён» / for `vgLost` —
«связь с VG потеряна». The linger timer is BOUND to the session generation
(pattern: `sseGeneration` in `LiveSubtitlesOverlay`): a `callAppeared` for a
NEW session cancels a previous session's linger — call B appearing inside
call A's linger window must not have its HUD hidden by A's stale timer.

- **HUD lifecycle:** appears on `callAppeared` when `call_observer_hud_enabled`
  (default true) and the panel is not already open; hides via the terminal
  automaton's linger (§4.1). Manual close of
  HUD does not kill the watcher; the status-menu item remains as re-entry.
- **Audio autoplay:** `call_observer_autoplay_audio` (default false) → if true,
  CallAudioPlayer connects as soon as HUD/panel appears.
- **Per-session reset:** HUD/panel/WS clients are reused across calls, so a
  new `callAppeared` performs an explicit reset: transcript feed, cost line,
  listen-state, elapsed timer — and every (re)connect cancels the previous
  WS task and invalidates its ping timer (generation token) so ping timers
  never accumulate across reconnects.
- **No partials:** the server publishes NO remote-speech partials (§2.2) —
  the transcript renders finals as they arrive, same granularity their iOS
  client has. No dimmed-partial mechanics anywhere.
- **Ordering/attribution:** remote line = `stt.final` (+ its
  `translation.final`); agent line = `agent.response` AND
  `agent.suggestion.auto_spoken` (`text` + `text_ru` when present).
  Interleave by arrival order; `ts` shown on hover only. Translator-mode
  owner-mic misattribution — known limitation, §2.2.
- **Reconnect:** events WS and audio WS reconnect independently; watcher poll
  is the ground truth for call existence (heals a missed `call.ended` via
  `callGone` → the §4.1 automaton).
- **VG restart mid-call:** WS drops → backoff reconnect; a successful poll
  without the session ends the call UI via `callGone` (absent-streak 2 →
  ≈ 4–6 s); VG staying unreachable ends it via `vgLost` (≈ 30 s). No user
  action required, and neither state can hang forever.

## 5. Settings (the only Python diff)

Add to `DEFAULT_SETTINGS` (`core/config.py`) + docs — TWO new keys only:
- `call_observer_hud_enabled: true` (bool)
- `call_observer_autoplay_audio: false` (bool)

`voice_gateway_url` AND `voice_gateway_api_key` already exist
(`core/config.py:878-879`; the key is already sensitive/redacted — do not
re-add). New backend behavior: maintain `<data_dir>/vg_client_token` (§2 auth
note) — write on startup + on settings save; add to purge-coverage allowlist.

Settings UI checkboxes: small addition to the existing LiveSubs/VG settings
section (`HistoryPanelController+LiveSubsSettings.swift`) in the same wave.
Swift reads the two bools via IPC on coordinator start and on settings-change
notification (existing pattern); the token comes from the file, never IPC.

Privacy: wave 1 renders live data only, persists nothing, all traffic
loopback → no `privacy_mode` gate required (gates guard persisted/derived
transcript data). Revisit if a later wave saves call transcripts into history.

## 6. Testing

**Swift XCTest (unit, deterministic):**
- μ-law decode: golden vectors (canonical G.711 pairs incl. 0x7F/0xFF
  silence, extremes) + round-trip against VG's `pcm16_to_mulaw` reference
  values baked as fixtures.
- Event decoding: fixtures copied VERBATIM from VG publish sites (wire-format
  rule — test the wire, not a hand-made wrapper): `stt.final` in ALL THREE
  shapes (full / takeover `{text, language}` / realtime `{text}`),
  `translation.final` (with `provider`), `agent.response` in all four
  publish-site shapes (incl. the minimal realtime one WITHOUT `text_ru`),
  `agent.suggestion.auto_spoken`, `agent.interrupted` (with
  `spoken_fraction`/`spoken_text`), `call.state` (with/without
  `muted`/`held`), `call.ringing` (no `status` field), `call.closed`,
  `cost.alert`, plus an UNKNOWN type (silently ignored — forward-compat).
- Watcher FSM: injected fetcher stub → appear/gone/stale-filter/backoff
  transitions; VG-unreachable produces no error-level logs (assert via
  injected logger).
- Interrupted matching: two agent replies, interrupt targets the FIRST
  `utterance_ts` → first is struck, last stays intact.
- Hangup flow: single-flight, `already_terminal`, 404/409/502 paths
  (stubbed); 404/409-after-terminal are silent.
- Cost polling: reads `costs.total_usd` from a stubbed diagnostics payload;
  stops at terminal; missing field renders «—» (never crashes).
- §4.1 automaton: all four end-signals fire in every order → terminal
  actions run exactly once; linger is generation-bound (B's HUD survives
  A's stale linger); failed poll ≠ `callGone`; absent-streak=2; `vgLost`
  after 3 failed polls during a live call.
- Listen toggle: rapid double-toggle and HUD+panel simultaneous press yield
  exactly one socket (generation token); ping timer invalidated on
  reconnect.
- Source-contract: `runModal` allowlist guard (existing CI test) must stay
  green; glyph gate for new symbols.

**Python:** tests that the 2 new keys exist in `DEFAULT_SETTINGS` with correct
defaults; that `vg_client_token` is written 0600/atomic on startup and on
settings save, rewritten on key change, emptied when the key is cleared; and
that `audit_purge_coverage` stays green with the new allowlist entry.

**Live e2e (autonomous, no owner needed):**
- `scripts/fake_vg_server.py` — tiny stdlib/flask stub implementing
  `/v1/sessions`, `/stream` (scripted event sequence incl. partial→final,
  agent.response, interrupt, ended), `/monitor/audio` (JSON header + μ-law
  sine frames), hangup endpoint. Driven by
  `scripts/e2e_call_observer_smoke.command`: launch agent against the fake,
  assert HUD appears (AX), transcript lines render, hangup POST arrives,
  audio frames consumed. This is the merge gate.
- Final validation against REAL VG test call — coordinated with the VG
  session (cross-session), after merge, before closing the wave.

## 7. Work split & rollout

1. Claude: components 1–3 + 6, settings, tests, fake-VG e2e (contract-bearing).
2. Claude: minimal functional UI for HUD + panel (unstyled but complete).
3. agy / Gemini 3.1 Pro High: visual polish of HUD + panel via design brief
   with hard invariants (no IPC/WS key invention — key-by-key gate after,
   the C3b lesson); Claude reviews diff + rebuilds.
4. Fresh-context adversarial review of the whole branch diff (mandatory
   stage), live e2e, merge, deploy via `safe_backend_restart` ritual + binary
   parity, NOW.md card.

Cross-session brief to VG — SENT and ANSWERED (2026-08-21, verified by them
against their code): (a) multi-suggestion extension ACCEPTED into their
backlog (`pending_suggestions: [{id, text, text_ru}]`, `speak` with optional
`id`, back-compat; ~1 their session; start pending owner priority
confirmation); (b) live-call predicate CONFIRMED (details folded into §3.1);
(c) `KRAB_MONITOR_MAX_SUBSCRIBERS` default 2 confirmed (`app/config.py:253`);
raising to 3 = one-line `.env` + `launchctl kickstart -k ai.krab.voice-gateway`
on their side — they hold it until the owner's explicit "go" (live prod
config); (d) cost source = diagnostics polling (folded into §2.2/§3).
Follow-up ask (e), pending their answer: add an `origin: "remote"|"owner_mic"`
field to `stt.final`/`translation.final` payloads — trivial server-side (each
publish site knows its path), removes the translator-mode misattribution
limitation for good.

## 8. Risks / open items

- **Subscriber limit contention** (2 slots shared with iOS): mitigated by
  connect-only-while-listening + VG brief item (c). UI shows explicit hint on
  1013 instead of silent failure.
- **Event set drift**: VG contract is theirs; our decoder must ignore unknown
  event types silently (forward-compatible) — pinned by a fixture test with
  an unknown type.
- **URLSessionWebSocketTask ping**: manual ping timer required (no auto
  keepalive) — ported from their iOS client.
- **Stale sessions**: predicate + recency guard; ground truth is the poll.
- **Event bursts**: coalesce UI updates (main-queue debounce ≥ 100 ms) so
  the panel never floods the main thread — hygiene even without partials.
- **Contract drift over time**: the consumed-events table is pinned to VG
  publish sites as of 2026-08-21; the fixture tests copy those exact shapes,
  so a VG-side change turns into a red fixture, not a silent UI hole. The
  peer session's own catalog listed a phantom `stt.partial` — trust code,
  not catalogs.
- **Cost line**: resolved — diagnostics polling (§2.2); `cost.alert` is an
  alarm badge only.

## Decision log (owner, 2026-08-21)

- Two waves: watch first, intervene second (this spec = wave 1).
- Form factor: BOTH — HUD auto-appears on call, click expands to full panel.
- Audio: off by default, button to listen; setting `call_observer_autoplay_audio`.
- Zone split with VG confirmed: they own telephony/audio path/commands; we are
  a second observer client of their contract (first is iOS).
