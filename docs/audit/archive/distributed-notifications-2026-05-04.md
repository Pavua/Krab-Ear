# Distributed Notifications Audit

**Date:** 2026-05-04
**Scope:** `native/KrabEarAgent/Sources/`
**Purpose:** Catalog all `DistributedNotificationCenter` observers/posters and identify
quit-action propagation paths that can trigger `NSApp.terminate`.

---

## grep command used

```bash
grep -rn "DistributedNotificationCenter\|addObserver\|postNotificationName\|postNotification\|removeObserver\|controlNotificationName\|KrabEar\." \
  native/KrabEarAgent/Sources/KrabEarAgent/
```

---

## DistributedNotificationCenter — Observers

| File | Line | Notification name | Handler selector | Registered in |
|------|------|-------------------|-----------------|---------------|
| `main.swift` | 173–178 | `com.krabear.agent.control` (stored in `controlNotificationName`) | `handleControlNotification(_:)` | `applicationDidFinishLaunching` |

**Deregistration:**
- `main.swift:305` — `DistributedNotificationCenter.default().removeObserver(self)` in `applicationWillTerminate(_:)`.

---

## DistributedNotificationCenter — Posters

**None found.**

No Swift source file in this project posts to `DistributedNotificationCenter`.
The `com.krabear.agent.control` notification is intended to be posted by **external callers**
(e.g. CLI tools, other apps, shell scripts) as a control IPC mechanism.

---

## handleControlNotification — action dispatch

`main.swift:809–827`. Dispatches on `userInfo["action"]`:

| Action value | Effect |
|-------------|--------|
| `"show_history"` | Opens history panel |
| `"toggle_recording"` | Starts or stops recording |
| `"quit"` | Calls `NSApp.terminate(nil)` directly |
| *(anything else)* | Ignored (`default: break`) |

---

## Local (in-process) NotificationCenter — Posters

These use `NotificationCenter.default` (not distributed) and are scoped to the process.

| File | Line | Notification name | Trigger |
|------|------|-------------------|---------|
| `ErrorActionHandler.swift` | 194 | `KrabEar.focusHFTokenSetting` | Backend side_effect `swift_focus_hf_token` |
| `ErrorActionHandler.swift` | 197 | `KrabEar.focusHotkeyTab` | Backend side_effect `swift_focus_hotkey_tab` |

**Observers:** No `addObserver` calls for these notification names were found at audit time.
They are likely intended for future Settings panel observers.

---

## NSWorkspace.shared.notificationCenter — Observers

| File | Line | Notification | Handler |
|------|------|-------------|---------|
| `main.swift` | 179–184 | `NSWorkspace.didActivateApplicationNotification` | `handleWorkspaceActivatedApp(_:)` |

This is an OS-level notification for application focus changes (used for paste-target detection).
It does **not** lead to `NSApp.terminate`.

---

## Sources of `NSApp.terminate(nil)`

After Phase C C.7, all five sites now call `SentryConfig.recordTerminate(callsite:)` first:

| File | Line | Function | Callsite tag | Trigger |
|------|------|----------|-------------|---------|
| `main.swift` | ~798 | `onQuit()` | `onQuit` | User clicks "Quit" menu item |
| `main.swift` | ~823 | `handleControlNotification(_:)` | `handleControlNotification_quit` | External `DistributedNotification` with `action="quit"` |
| `main.swift` | ~952 | `stopAgent()` | `stopAgent` | Called by `onStopAgent()` (menu item) or supervisor |
| `main.swift` | ~962 | `restartAgent()` | `restartAgent` | Called by `onRestartAgent()` (menu item) or supervisor restart path |
| `main.swift` | ~971 | `showFatalAndTerminate(title:body:)` | `showFatalAndTerminate` | Fatal error after alert modal dismissed |

---

## Risk paths

### Risk 1 — External `quit` via DistributedNotificationCenter (MEDIUM)

Any process on the **same machine with the same UID** can post:

```swift
DistributedNotificationCenter.default().post(
    name: Notification.Name("com.krabear.agent.control"),
    object: nil,
    userInfo: ["action": "quit"]
)
```

This triggers `handleControlNotification` → `NSApp.terminate(nil)` at `main.swift:823`.
No authentication or signing check is performed.

**Impact:** Another app (or malicious script) can silently terminate Krab Ear Agent.

**Mitigation options:**
1. Add a `deliverImmediately: false` + sender check (distributed notifications carry no sender identity in macOS).
2. Move quit control to a Unix socket IPC call (already available for backend; could extend to agent).
3. Accept risk as low-severity (only local UID can post; no privilege escalation).

### Risk 2 — `showFatalAndTerminate` during `SystemAudioCapture` initialization (HIGH — Live Subs crash suspect)

`showFatalAndTerminate(title:body:)` runs modal alert then calls `NSApp.terminate`.
If it is called from a background `Task` or from the `BackendSupervisor` circuit-breaker
30–90 s after `SystemAudioCapture` starts (ScreenCaptureKit permission failure path),
it explains the silent crash pattern described in B.2 F4.

With Phase C C.7 breadcrumbs now in place, the callsite `showFatalAndTerminate` will appear
in Sentry before the next reproduction.

### Risk 3 — `restartAgent` double-terminate race (LOW)

`restartAgent()` launches a new process via `Process.run()` and then calls `NSApp.terminate`.
If `Process.run()` throws (script path invalid), the `try?` silences the error and
`NSApp.terminate` still runs. The new agent may not have started, leaving the system in
an unresponsive state until the user reopens the app.

---

## No-poster finding

No code in this repository posts to `DistributedNotificationCenter`. The control
channel is **receive-only** from the agent's perspective, relying entirely on external
senders. This is intentional (CLI control integration) but means the agent cannot
self-diagnose which external sender triggered a `quit` action — hence the importance
of the Sentry breadcrumbs added in Phase C C.7.
