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
current `voice_gateway_api_key` (may be empty). Swift reads this file when
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

Events consumed in wave 1 (payload shapes from `app/models.py:128+` and
publish sites):

| type | data (fields we use) | meaning |
|---|---|---|
| `stt.partial` / `stt.final` | `text, language, confidence` | **remote party** speech (no speaker field — attribution is BY EVENT TYPE; agent speech never passes STT) |
| `translation.partial` / `translation.final` | `text, source_text, src_lang, tgt_lang` | translation of remote speech (session `tgt_lang`, default ru) |
| `agent.response` | `text, text_ru, lang, utterance_ts, action` (`app/main.py:8338, 8899`) | **agent** reply: original + built-in RU translation |
| `agent.interrupted` | `utterance_ts` | agent reply was cut off mid-TTS; match strictly by `utterance_ts`, never "last reply" (VG's own hard rule) |
| `call.state` / `call.ringing` / `call.answered` / `call.ended` | `status, reason` | lifecycle for HUD status dot / auto-close |
| `cost.alert` | provider cost data | cost ticker in panel |

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
`{ok, session_id, call_sid, status, already_terminal?}`; 404
`session_not_found`; 502 on provider error. Idempotent on terminal sessions
(`already_terminal: true`).

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
     (`phone ≠ ""` ∨ `call_direction ≠ ""`) ∧ `updated_at` within 6 h (stale
     "running" rows are a known VG failure mode — they built a recency guard
     for the same reason, `app/main.py:3058`).
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
   - Close 1013 → show "лимит слушателей" hint in UI, do not retry-loop
     (retry only on explicit re-press). 1008/1000 → stop, reflect call end.
4. **`CallObserverHUD.swift`** — floating `NSPanel` (pattern:
   `LiveSubtitlesOverlay`): always-on-top, draggable, ~340 px wide. Shows
   status dot + direction + phone + elapsed timer, last 2 replicas (each:
   original + translation, dimmed partials), buttons 🔊 (toggle listen) and
   📞 (hangup), click anywhere else → expand to panel (HUD hides).
5. **`CallObserverPanelController.swift`** — `NSWindowController` (pattern:
   `MeetingLivePanelController` visuals, but data source is the WS client, not
   SSE): full scrolling transcript feed (both sides, translations under
   originals, `agent.interrupted` replicas struck-through/greyed with badge
   «прервано»), listen toggle, hangup button, cost line, connection badge
   («reconnecting…» on WS drop — panel stays open). Transcript feed capped at
   500 entries in memory.
6. **`main+CallObserver.swift`** — wiring: single owner
   (`callObserverCoordinator`) in `AgentAppDelegate`; starts watcher after
   backend-ready (reads settings via IPC off-main), status-menu item
   «Звонок агента…» (disabled when no live call) as manual entry to the panel.

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
`already_terminal` rely on `call.ended`/watcher to close UI; on 404/502 show
error toast, re-enable. Single-flight guard (bool) against double-click.

## 4. Behavior details

- **HUD lifecycle:** appears on `callAppeared` when `call_observer_hud_enabled`
  (default true) and the panel is not already open; disappears on `callGone` /
  `call.ended` after a 3 s linger (shows «Звонок завершён»). Manual close of
  HUD does not kill the watcher; the status-menu item remains as re-entry.
- **Audio autoplay:** `call_observer_autoplay_audio` (default false) → if true,
  CallAudioPlayer connects as soon as HUD/panel appears.
- **Partials:** `stt.partial`/`translation.partial` render dimmed and are
  replaced in place by the matching final (replace-last-partial-of-that-type;
  the stream is per-session single remote speaker, so no keying needed).
- **Ordering/attribution:** remote line = `stt.*` (+ its `translation.*`);
  agent line = `agent.response` (`text` + `text_ru`). Interleave by arrival
  order; `ts` shown on hover only.
- **Reconnect:** events WS and audio WS reconnect independently; watcher poll
  is the ground truth for call existence (heals missed `call.ended`).
- **VG restart mid-call:** WS drops → backoff reconnect; if session vanished,
  watcher closes UI within ≤ 3 s. No user action required.

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
  rule — test the wire, not a hand-made wrapper): `stt.final`,
  `translation.final`, `agent.response` (with `text_ru`, `utterance_ts`),
  `agent.interrupted`, `call.state`, `call.closed`.
- Watcher FSM: injected fetcher stub → appear/gone/stale-filter/backoff
  transitions; VG-unreachable produces no error-level logs (assert via
  injected logger).
- Interrupted matching: two agent replies, interrupt targets the FIRST
  `utterance_ts` → first is struck, last stays intact.
- Hangup flow: single-flight, `already_terminal`, 404/502 paths (stubbed).
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

Immediately after spec approval (parallel to implementation): cross-session
brief to VG — (a) wave-2 ask: extend assisted-mode to 2–3 suggestion
options; (b) confirm our live-call predicate matches their intent; (c)
heads-up: we become a second `monitor/audio` subscriber — consider raising
`KRAB_MONITOR_MAX_SUBSCRIBERS` to 3 in their prod env so iOS + macOS + spare
coexist.

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
- **`stt.partial` volume**: high-rate partials → coalesce UI updates
  (main-queue debounce ≥ 100 ms) so the panel never floods the main thread.

## Decision log (owner, 2026-08-21)

- Two waves: watch first, intervene second (this spec = wave 1).
- Form factor: BOTH — HUD auto-appears on call, click expands to full panel.
- Audio: off by default, button to listen; setting `call_observer_autoplay_audio`.
- Zone split with VG confirmed: they own telephony/audio path/commands; we are
  a second observer client of their contract (first is iOS).
