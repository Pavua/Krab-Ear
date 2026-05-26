# Wave 695 — OpenWakeWord Fallback Wire

**Date**: 2026-05-26  
**Branch**: wave695/openwakeword-fallback

## Gap identified

`WakeWordListener.swift` relied solely on Porcupine (Picovoice) SDK.
When `createPorcupineEngine()` returned `nil` (no AccessKey or .ppn file),
`start()` returned `false` and wake word was silently disabled — despite the
Python backend having a fully working `OpenWakeWordAdapter` (~30 MB, Apache 2.0,
no signup) with four IPC handlers already wired in `service.py` since initial impl.

## Changes

### `KrabEar/contracts/registry.py`
Added `WAKE_WORD_DETECTED = "wake_word.detected"` to `EventType` enum.

### `KrabEar/backend/openwakeword_adapter.py`
In `handle_wake_word_start._on_detected`: emit `wake_word.detected` via `EventBus`
when a wake word fires. Lazy import guards prevent breakage when EventBus is absent
(unit tests). Payload: `{"model": name, "score": float}` — no transcript text.

### `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordListener.swift`
- Added OWW fallback fields: `owwIPCClient`, `owwRestBaseURL`, `owwFallbackActive`,
  SSE delegate/session/task, `owwPendingEventType`.
- `start()`: when Porcupine returns `nil` AND `owwIPCClient != nil`, calls
  `startOpenWakeWordFallback(ipcClient:)`.
- `startOpenWakeWordFallback`: calls IPC `wake_word_start {model: hey_jarvis}`,
  then subscribes to SSE `/v1/events?filter=wake_word.detected` on port 5005.
  On `wake_word.detected` event: fires `onWakeWordDetected()` on main actor.
- `stop()`: calls `stopOpenWakeWordFallback()` → cancels SSE + IPC `wake_word_stop`.

### `native/KrabEarAgent/Sources/KrabEarAgent/main.swift`
In `setupWakeWordListenerIfEnabled()`: injects `listener.owwIPCClient = ipcClient`
before `listener.start()` so the fallback has a live IPC connection.

## Fallback chain

```
WakeWordListener.start()
  └─ createPorcupineEngine() → nil?
       └─ owwIPCClient != nil?
            └─ startOpenWakeWordFallback()
                 ├─ IPC: wake_word_start {model: hey_jarvis}
                 └─ SSE: /v1/events?filter=wake_word.detected
                      └─ onWakeWordDetected() → triggerConversationFromWakeWord()
```

## Notes

- Built-in model `hey_jarvis` used as default (available without download on fresh install).
- Custom "Краб" model path supported by adapter; change `model` param once .onnx trained.
- `openwakeword` Python lib optional — adapter stubs gracefully if not installed.
- SSE session reuses `SSESessionDelegate` pattern from `LiveSubtitlesOverlay`.
