# Wave 798 — IPC Versioning & Handshake Compatibility Audit

**Date:** 2026-05-26  
**Auditor:** Claude Sonnet (wave798/ipc-versioning-W798)  
**Scope:** IPC handshake, API versioning modules, Swift-side compatibility checks

---

## 1. Current Versioning Surface

### 1.1 Application version

`KrabEar/__version__.py`:

```python
__version__ = "2.0.5"
```

This is the canonical version used by `api_versioning.py` (`get_api_info`) and by Sentry release tagging.

### 1.2 IPC handshake — Python side

Handler: `BackendService._handle_handshake` (`KrabEar/backend/service.py`, line 1875).

**Response payload (hardcoded):**

```json
{
  "ok": true,
  "backend_version": "1.0.0",
  "phase_b_capable": true,
  "phase_c_capable": true,
  "swift_version_ack": "<echoed swift_agent_version>"
}
```

**Problems identified:**

| Field | Issue |
|---|---|
| `backend_version` | Hardcoded `"1.0.0"` — does not match `__version__` (`2.0.5`). Always stale. |
| `phase_b_capable` | Boolean flag — always `True`. No mechanism to disable. |
| `phase_c_capable` | Boolean flag — always `True`. No mechanism to disable. |
| Capability list | Backend does not echo back a capability list for the full handler set (~296 handlers). Swift has no way to discover available methods. |
| Swift version | Echoed but never validated. No minimum-version enforcement. |

**Request params accepted from Swift:**

- `swift_agent_version` (str, default `"unknown"`)
- `capabilities` (list of str)

The backend logs these but takes no action on them — no minimum-version gate, no capability negotiation.

### 1.3 REST API versioning — `backend/api_versioning.py`

Separate from the IPC socket layer. Serves the Flask REST API on port 5005.

**Supported versions:** `v1`, `v2`  
**Default version:** `v1`  
**Deprecated versions:** _(none currently)_

Detection order per request:
1. URL path prefix (`/v1/...`, `/v2/...`)
2. `Accept: application/vnd.krabear.v1+json` header
3. `?api_version=v1` query parameter
4. Falls back to `v1`

Every response gets `X-API-Version: <resolved>` via `after_request` hook.

The `GET /info` endpoint returns the version metadata:

```json
{
  "app_version": "2.0.5",
  "current_version": "v1",
  "supported_versions": ["v1", "v2"],
  "deprecated_versions": []
}
```

**Note:** `v2` is listed as supported but has no actual routes differentiated from `v1` — it is a placeholder for future migration.

### 1.4 IPC socket constants (`backend/ipc_constants.py`)

No protocol version constant — only operational parameters:

```
IPC_SOCKET_BACKLOG     = 32
IPC_SOCKET_TIMEOUT_SEC = 0.8
IPC_MAX_MESSAGE_BYTES  = 1 048 576  (1 MB)
IPC_SOCKET_PERMISSIONS = 0o600
```

There is no framing version byte, no envelope version field, and no magic header on the Unix socket stream.

---

## 2. Swift-Side Compatibility Checks

### 2.1 `IPCClient.performHandshake` (`IPCClient.swift`, line 278)

Called once after the socket becomes reachable. Sends:

```swift
[
  "swift_agent_version": swiftAgentVersion,   // default "1.0.0" (hardcoded)
  "capabilities": capabilities,               // ["error_bus_consumer", "live_subs", "selection_translator"]
]
```

Reads back:
- `backend_version` — logged to NSLog only, not stored or acted on.
- `phase_b_capable` — if `false`, logs a WARNING but does not disable any Swift-side features.
- `phase_c_capable` — logged, not acted on.

**Handshake failure is explicitly non-fatal** (comment in source: "Gracefully degrades — never throws on unexpected backend_version or missing phase_b_capable"). This means an incompatible backend silently continues to serve requests.

### 2.2 `performHandshake` call site

`performHandshake` is defined in `IPCClient.swift` but as of this audit has **no call sites** in the source tree outside its own definition. It is not invoked from `BackendSupervisor.swift`, `main.swift`, or any extension file. The handshake is therefore **dead code on the Swift side** — it compiles and works when called, but the agent never calls it automatically on connect.

### 2.3 Swift hardcoded `swiftAgentVersion`

Default value is `"1.0.0"` (parameter default, line 279). The real bundle version (`CFBundleShortVersionString`) is never read here. The version sent to the backend is always `"1.0.0"` regardless of the actual installed binary.

### 2.4 `BackendSupervisor.swift`

No version checks. Supervisor cares only about process liveness (exit code, health-ping timeout). Version mismatches do not affect supervisor behaviour.

---

## 3. Gap Summary

| # | Gap | Severity |
|---|---|---|
| G1 | `backend_version` in handshake response is hardcoded `"1.0.0"`, not read from `__version__` (`2.0.5`). | Medium |
| G2 | `performHandshake()` is defined but never called — handshake is a no-op in production. | High |
| G3 | Swift sends `swiftAgentVersion: "1.0.0"` (hardcoded), not the real bundle version. | Low |
| G4 | No minimum-version enforcement on either side. A stale backend and a new Swift agent (or vice-versa) will silently mismatch. | Medium |
| G5 | Backend does not return its full capability list (handler names or feature flags). Swift cannot do capability discovery at runtime. | Low |
| G6 | REST API lists `v2` as supported but has no v2-differentiated routes. Clients that negotiate v2 get v1 semantics with a v2 header. | Low |
| G7 | No IPC protocol framing version (envelope byte/field). Breaking wire changes (e.g. binary frames, compression) would be undetectable. | Low (future risk) |

---

## 4. Recommendations

### 4.1 Fix `backend_version` (G1) — quick win

```python
from KrabEar.__version__ import __version__ as APP_VERSION

def _handle_handshake(self, params):
    ...
    return {
        "ok": True,
        "backend_version": APP_VERSION,   # was: "1.0.0"
        "phase_b_capable": True,
        "phase_c_capable": True,
        "swift_version_ack": swift_version,
    }
```

### 4.2 Wire `performHandshake()` call (G2) — medium effort

In `BackendSupervisor.swift` (or `main.swift`), after the first successful IPC connect:

```swift
await ipcClient.performHandshake(
    swiftAgentVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
)
```

This makes the handshake actually execute and provides real version telemetry in logs.

### 4.3 Read real bundle version in Swift (G3)

Remove the `"1.0.0"` default and read from `Bundle.main`:

```swift
func performHandshake() async {
    let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown"
    ...
}
```

### 4.4 Add soft version compatibility gate (G4)

The backend can compare `swift_version` against a known minimum and log a structured warning (not a hard error — graceful degradation is correct). Suggested approach:

```python
MINIMUM_SWIFT_AGENT_VERSION = (2, 0, 0)

def _parse_semver(s):
    try:
        return tuple(int(x) for x in s.split(".")[:3])
    except Exception:
        return (0, 0, 0)

swift_tuple = _parse_semver(swift_version)
if swift_tuple < MINIMUM_SWIFT_AGENT_VERSION:
    logger.warning("swift_agent_version %s is below minimum %s", swift_version, MINIMUM_SWIFT_AGENT_VERSION)
```

### 4.5 Emit capability list in handshake response (G5)

Return a stable set of feature flags (not the full 296 handler names) to let Swift make runtime decisions:

```python
return {
    ...
    "capabilities": [
        "error_bus",
        "live_subs",
        "call_automation",
        "obsidian_sync",
        "stt_management",
        "apple_integration",
    ],
}
```

### 4.6 Retire REST `v2` stub or implement it (G6)

Either add a `DEPRECATED_VERSIONS` entry for `v2` with a sunset date, or implement at least one v2 route (e.g. a richer `/v2/transcribe` response shape). As-is, advertising `v2` is misleading.

### 4.7 Version bump policy for IPC breaking changes (G7)

No formal policy exists. Proposed convention:

- **Additive** (new handler, new response field): no version bump required. Both sides must be lenient about unknown fields.
- **Behaviour change** (field semantics change, handler removed): bump `backend_version` patch/minor. Add log warning in handshake if Swift is below a minimum.
- **Wire format change** (framing, encoding): bump `backend_version` major. Hard-fail handshake on incompatible client.

---

## 5. Scope of Effort

| Recommendation | Effort | Risk |
|---|---|---|
| 4.1 Fix hardcoded `backend_version` | 2-line change, Python | Negligible |
| 4.2 Wire `performHandshake` call site | ~5 lines, Swift | Negligible (non-fatal by design) |
| 4.3 Real bundle version in Swift | 2-line change, Swift | Negligible |
| 4.4 Soft version gate | ~15 lines, Python | Low |
| 4.5 Capability list in response | ~10 lines, Python | Low |
| 4.6 Retire/implement REST v2 | Architecture decision | None until decided |
| 4.7 Version bump policy | Doc only | None |

All items are independent; they can be shipped in any order. G2 (dead handshake call) is the highest-impact issue: the entire handshake mechanism provides zero production value until it is actually invoked.
