# Scoping — Backlog #13 conversation-mode VG-bridge polish (2026-06-20)

## 🔑 KEY FINDING (de-risks the whole item)
**Voice Gateway's conversation WS speaks raw PCM16 LE mono — NOT Opus.**
- `Krab Voice Gateway/app/routers/conversation.py:7`: «Бинарные фреймы: PCM16 LE mono (частота зависит от движка) от клиента → движку».
- `Krab Voice Gateway/app/audio_utils.py`: only mu-law table, `pcm16_to_wav`, `resample_pcm16` — **no Opus encode/decode anywhere**.

→ The Krab Ear Swift stubs in `ConversationViewController+Audio.swift` ("PCM 16kHz → Opus (stub) → sendAudioFrame", "Opus → PCM 24kHz (stub)") encode a **stale Phase-1.3 design assumption**. VG implemented PCM, not Opus. **No Opus library / dependency decision is needed.** #13 is therefore CONTAINED, not a multi-session big bet.

## Reduced scope (likely ONE Sonnet task)
1. **Uplink:** delete the Opus-encode stub (`ConversationViewController+Audio.swift` ~line 132). The mic tap already produces PCM16 16kHz mono — send those bytes directly as binary WS frames via `sendAudioFrame(...)`. Confirm `+WebSocket.swift` sends binary (not text) frames.
2. **Downlink:** delete the Opus-decode stub (~line 146-154). VG sends back PCM16 binary frames at the engine's `sample_rate` (handshake sends `engine` + `sample_rate`, see conversation.py:151). Wire: received `Data` → `AVAudioPCMBuffer` (Int16→Float32) → `AVAudioPlayerNode.scheduleBuffer` → playback. The capture/playback `AVAudioEngine` is already set up per the file header.
3. **Sample-rate handling:** uplink fixed 16kHz; downlink uses the `sample_rate` VG announces in the config frame (don't hardcode 24kHz — the stub comment assumed it). `resample_pcm16` exists VG-side; Swift just plays what it's told.

## Verify before implementing
- VG downlink: confirm binary PCM frame shape + that `sample_rate` is in the config/first frame (grep `conversation.py` send path; `_detect_lang_from_first_frames` is a PR-1.2 stub still returning 'en' — language detect is a SEPARATE PR-1.4 item, not blocking audio).
- `ConversationViewController+WebSocket.swift`: current send/recv (text vs binary, frame framing).
- Needs **Voice Gateway running** to E2E-test (sibling project). Brain = Krab agent OpenClaw (qwen3-30b) per CLAUDE.md Voice Assistant section.

## Execution path
brief (this) → Sonnet worktree (PCM wiring, no codec) → Opus gate (build + greps + glyph-gate) → two-binary → push → CI. E2E voice test requires VG up (owner/manual). Pure-PCM means the Swift change is testable for build/compile without VG; live audio needs VG.

## Status
Discovered + scoped 2026-06-20 (during «2-3-4» turn after v2.1.6). NOT implemented — deferred to a focused session/worker. The big-bet framing in [[project_feature_backlog_2026-06-18]] #13 is OBSOLETE: no Opus lib, contained.
