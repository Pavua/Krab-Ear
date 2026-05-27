# W1165 Audit — CallAssistService (call_assist_service.py)

**Date:** 2026-05-26  
**Branch:** audit/call-assist-service-W1165  
**Auditor:** Sub-agent W1165  
**Files reviewed:**
- `KrabEar/backend/call_assist_service.py` (1148 lines)
- `KrabEar/backend/vg_ws_client.py` (106 lines)
- `KrabEar/backend/live_subs_service.py` (213 lines)
- `KrabEar/backend/call_auto_end.py` (242 lines)
- `KrabEar/backend/service.py` (wiring context, ~5024 lines)
- `KrabEar/tests/test_call_assist_service.py`, `*_deep.py`, `*_edges.py`

---

## Summary

5 findings. No crashes on VGW unreachable (HTTP errors return `{"ok": False, ...}` and the service degrades gracefully). IPC handler coverage is complete (18 handlers wired). The VGWebSocketClient reconnect logic is sound but is not used by CallAssistService itself — that module uses HTTP only.

---

## Findings

### F1 — MED: `_assist_loop` has no wall-clock session-duration cap

**File:** `call_assist_service.py:1017–1092`

`CallAutoEnd` (max 30 min, silence-based auto-end) exists in the codebase and is correctly wired into `CallSessionService`. However it is **not connected** to `_assist_loop` in `CallAssistService`. The background thread runs until either `_state["active"]` is set False (via `handle_stop`) or `recorder.is_recording` is False. If the user never calls `stop_call_assist` and keeps the recorder running, the loop and the open VGW HTTP session run indefinitely.

**Impact:** gateway session accumulates events, STT and network cost unbounded, no user-facing signal.

**Fix candidate:** read `started_at` from `_state` inside the loop and break when `time.monotonic() - started_at > max_duration_sec` (configurable via settings). Alternatively delegate auto-end check to `CallAutoEnd` every N iterations.

---

### F2 — MED: Double `_assist_loop` threads on re-`start` while active

**File:** `call_assist_service.py:245–350`, `1017–1092`

`handle_start` has no guard for an already-active session. When called twice:

1. `_state` is overwritten atomically with the new session's data (`active=True`, new `session_id`, new `gateway_session_id`).
2. A new `_assist_loop` thread is spawned for the new VGW session.
3. The **old** loop thread checks `self._state.get("active")` — it reads `True` (the new session's value) and continues running.
4. Both threads call `transcriber.transcribe_preview` and POST to different VGW sessions simultaneously.

The existing test `test_concurrent_start_blocked` documents this as "state overwrite" but does not verify that the old loop terminates. In practice the old loop sends stale STT to the superseded VGW session until the recorder stops.

**Fix candidate:** before spawning the new loop in `handle_start`, set a thread-local stop flag on the old loop (e.g., store `_assist_loop_thread` reference and a per-session cancellation token that the old loop checks).

---

### F3 — MED: No `privacy_mode_enabled` guard in `handle_start`

**File:** `call_assist_service.py:245–350`

When `privacy_mode_enabled=True`, `TranslationService` forces offline mode and `ObservabilityService` skips Sentry init. `CallAssistService.handle_start` has no equivalent check. It unconditionally:

- Starts the local mic recorder (acceptable).
- Calls `VoiceGatewayClient.start_session` — sends session metadata over HTTP to VGW (by default `http://127.0.0.1:8090`, but configurable to a remote host).
- Spawns `_assist_loop` which POSTs STT partial transcripts to VGW over HTTP on every transcription cycle.

If VGW is configured as a remote endpoint and `privacy_mode_enabled=True`, transcription content leaves the device without user knowledge.

**Fix candidate:** at the top of `handle_start`, check `settings.get("privacy_mode_enabled")`. If True and VGW URL is not localhost, either raise `RuntimeError("call_assist недоступен в режиме приватности")` or suppress the gateway start and log a `PrivacyAuditLogger` entry. Local-only mode (VGW = 127.0.0.1) can be allowed.

---

### F4 — LOW: Stale `_state["active"]=True` when recorder stops externally

**File:** `call_assist_service.py:1024–1031`

The `_assist_loop` exits without cleanup when `recorder.is_recording` becomes False:

```python
if not self.recorder.is_recording:
    break
```

After the break, `_state` still has `"active": True`, `"status": "running"`. This happens whenever the user presses the hotkey to stop a normal recording while call assist is active. The IPC state returned by `get_call_assist_state` and the `call_assist` field in `get_metrics` will report an active call that is no longer running.

Any subsequent call to `call_assist_diagnostics`, `call_assist_summary`, etc. will attempt to contact the VGW session ID that may have already timed out on the VGW side.

**Fix candidate:** after the `break` on `is_recording=False`, set `_state["active"] = False`, `_state["status"] = "stopped_by_recorder"` inside `_lock`.

---

### F5 — LOW: Concurrent MLX contention between `_assist_loop` and `live_subs_service`

**File:** `call_assist_service.py:1040–1042`, `live_subs_service.py:148–150`

Both services share the same `Transcriber` instance (confirmed in `service.py:303–319`):

- `CallAssistService._assist_loop` calls `transcriber.transcribe_preview` every ~1.5 s.
- `LiveSubsService._flush` calls `transcriber.transcribe` every ≥3 s (or on `is_final=True`).

Both serialize on `mlx_lock()` (global RLock in `core/mlx_lock.py`). When both are active simultaneously, one blocks the other for the full MLX inference duration (~1–3 s for balanced profile). `_assist_loop` has no timeout around `transcribe_preview`, so each iteration can block much longer than its intended 1.5 s period, increasing the STT→VGW delivery latency noticeably during live-subs sessions.

No test covers simultaneous call-assist + live-subs operation.

**Fix candidate:** no immediate code change needed (mlx_lock is correct). Document the contention behaviour. Optionally add a `timeout` wrapper around `transcribe_preview` in `_assist_loop` (e.g., run in a `ThreadPoolExecutor` with a 5 s timeout, skip that iteration on timeout).

---

## Out of scope / confirmed OK

- **VGW HTTP error handling:** all `VoiceGatewayClient` methods catch `HTTPError` and generic `Exception`, return `{"ok": False, "error": ...}`. No unhandled exceptions propagate to `service.py`.
- **VGWebSocketClient reconnect:** the WS client in `vg_ws_client.py` has proper exponential backoff (1s→10s cap). It is **not used by CallAssistService** (HTTP-only); it is used only for Voice Assistant (Phase 1) streaming.
- **IPC handler completeness:** all 18 `call_assist_*` handlers are wired in `service.py:901–914,1137–1141`.
- **PCM format:** `recorder.snapshot_audio` returns `np.float32` at the recorder's native rate; `transcribe_preview` delegates to `AudioEngine` which handles the format. No mismatch.
- **Test coverage:** `test_call_assist_service_deep.py` (859 lines, 40+ tests) and `*_edges.py` (393 lines) cover most happy-path and degraded-VGW scenarios well.
