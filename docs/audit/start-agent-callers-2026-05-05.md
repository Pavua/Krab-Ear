# start_agent.command Caller Audit — 2026-05-05

## Direct callers (repo files that invoke start_agent.command)

| File | Line | How it calls | Notes |
|------|------|-------------|-------|
| `Start Krab Ear.command` | 9 | `exec "$ROOT_DIR/scripts/start_agent.command" "$@"` | Top-level one-click launcher; thin wrapper, delegates entirely |
| `scripts/open_control_panel.command` | 11 | `"$ROOT_DIR/scripts/start_agent.command" --show-history "$@"` | Opens history panel; spawns legacy runtime binary as side-effect |
| `scripts/run_smoke_release.command` | 88 | `"$ROOT_DIR/scripts/start_agent.command" --launched-by-launchd >... &` | Smoke-test harness; intentionally uses legacy path to validate backward compat |
| `scripts/install_agent.command` | 25 | Writes `com.krabear.agent.plist` pointing to `start_agent.command` | DEPRECATED script that would create legacy launchd plist |
| `native/KrabEarAgent/Sources/KrabEarAgent/LaunchAgentManager.swift` | 41, 77 | `buildPlistContent()` generates plist with `start_agent.command` as `ProgramArguments` | **Active Swift code path** — `install()` / `buildPlistContent()` write/return a plist invoking start_agent |
| `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` | 985 | `restartAgent()` shells out to start_agent.command `--show-history` and then terminates self | **Active Swift code path** — every self-restart goes through start_agent |

### Test/doc references (not runtime callers)

| File | Notes |
|------|-------|
| `KrabEar/tests/test_migration_scripts.py` | Validates start_agent.command exists and has DEPRECATED banner |
| `native/KrabEarAgent/Tests/.../LaunchAgentManagerTests.swift` | Asserts plist content contains start_agent.command |
| `docs/TROUBLESHOOTING_PERMISSIONS.md` | Mentions start_agent.command in troubleshooting advice |
| `docs/superpowers/specs/2026-05-05-phase-d-roadmap-design.md` | Documents this audit as open item |

## launchd plists

- `~/Library/LaunchAgents/com.krabear.agent.plist` — **NOT FOUND** (legacy plist not installed)
- `~/Library/LaunchAgents/ai.krab.ear.backend.plist` — EXISTS; launches Python backend directly (`python3 KrabEar/backend/service.py`); does NOT reference start_agent.command or KrabEarAgent binary
- `~/Library/LaunchAgents/ai.krab.ear-watcher.plist` — EXISTS; runs `krab_ear_watcher.py` from Краб project; monitors process health only, does NOT spawn KrabEarAgent
- `~/Library/LaunchAgents/ai.krab.ear.rest.plist` — EXISTS; not inspected (separate REST process)
- `~/Library/LaunchAgents/com.openclaw.krabear.plist.disabled_20260219_0113` — DISABLED; old scratch-path Python backend, irrelevant

**Key finding:** No currently-loaded launchd plist invokes `start_agent.command` or `native/runtime/KrabEarAgent`. The legacy `com.krabear.agent.plist` is absent.

## App bundle Info.plist

```
CFBundleIdentifier = com.antigravity.krab-ear
CFBundleExecutable = KrabEarAgent
LSUIElement        = true   (background app, no Dock icon by default)
LSMinimumSystemVersion = 13.0
```

No `LSBackgroundOnly`, `NSAppleScriptEnabled`, or process-spawn keys. The bundle itself does not auto-spawn anything outside its own binary.

## Login items

Current login items (via System Events):

```
Macs Fan Control, AlDente, Claude, OrbStack, Озвучивание системы, LM Studio
```

**Finding:** Krab Ear is NOT in the login items list. It is NOT auto-starting via Login Items mechanism. The `.app` bundle appears to be launched manually or via launchd (ai.krab.ear.backend.plist handles only the Python backend).

## Code references in Swift (active invocation paths)

### Path 1 — `LaunchAgentManager.install()` (launchd plist generator)

`native/KrabEarAgent/Sources/KrabEarAgent/LaunchAgentManager.swift` lines 41, 77

When the user enables autostart from within the app (`PermissionWizard` or settings), `LaunchAgentManager.install()` writes `~/Library/LaunchAgents/com.krabear.agent.plist` with:

```xml
<key>ProgramArguments</key>
<array>
    <string>/bin/zsh</string>
    <string>/path/to/scripts/start_agent.command</string>
    <string>--launched-by-launchd</string>
</array>
```

This is the **primary spawn source for the recurring orphan issue**. When autostart is enabled via UI → launchd creates `com.krabear.agent.plist` → launchd calls `start_agent.command` → which runs `native/runtime/KrabEarAgent` (legacy binary) instead of `Krab Ear.app`.

### Path 2 — `main.swift restartAgent()` (self-restart)

`native/KrabEarAgent/Sources/KrabEarAgent/main.swift` line 985

When the agent calls `restartAgent()` (e.g., after a crash, update, or explicit restart), it:
1. Shells out: `/bin/zsh -lc "start_agent.command --show-history &"`
2. Calls `NSApp.terminate(nil)` to exit current instance

This causes the child to be `native/runtime/KrabEarAgent` (the legacy binary that start_agent.command launches), not `Krab Ear.app`. Result: Dock shows orphan process.

### Path 3 — `Start Krab Ear.command` (one-click top-level)

User double-clicks `Start Krab Ear.command` at repo root → delegates to `scripts/start_agent.command` → runs `native/runtime/KrabEarAgent`. Same orphan outcome.

### Path 4 — `scripts/open_control_panel.command`

Shells directly to `start_agent.command --show-history`. Same orphan outcome.

## Hypothesis on spawn source

The recurring "KrabEarAgent in Dock" complaints are caused by **two active code paths both routing through start_agent.command**:

1. **Autostart via LaunchAgentManager** — if the user ever enabled autostart from within the app (via PermissionWizard or settings toggle), `com.krabear.agent.plist` was written and loaded. Even after manual removal, a reboot or `launchctl bootstrap` would re-create it the next time the setting is toggled. Currently the plist is absent, but toggling the setting will recreate it.

2. **restartAgent() in main.swift** — any programmatic restart (crash recovery, BackendSupervisor restart, user-triggered) spawns `native/runtime/KrabEarAgent` via start_agent.command rather than relaunching the `.app`. This orphan survives even if the launchd plist is clean.

**Root cause**: `LaunchAgentManager.swift` and `main.swift:restartAgent()` are still hardcoded to `scripts/start_agent.command`. They were never updated when start_agent.command was deprecated in PR #372. Phase C C.6.2 (`killOrphanRuntimeProcesses`) treats the symptom but not the cause.

## Recommended action

### Immediate (this PR scope — no removal yet)

- This audit documents the two active root-cause call sites.
- `start_agent.command` CANNOT be safely removed yet — it has two active Swift callers.

### Followup PR (Phase D / Phase E) — proposed changes

**Fix 1 — `LaunchAgentManager.swift`**: Change `install()` to generate a plist that calls `open -a "Krab Ear.app" --args --launched-by-launchd` (or equivalent `LaunchServices` invocation) instead of `start_agent.command`. Update `LaunchAgentManagerTests.swift` accordingly.

**Fix 2 — `main.swift restartAgent()`**: Replace the shell-out to `start_agent.command` with `NSWorkspace.shared.open(URL(fileURLWithPath: appBundlePath))` to relaunch via `.app` bundle.

**Fix 3 — `Start Krab Ear.command`**: After Fixes 1 & 2 land, redirect to `open "Krab Ear.app"` (one line). Keep as thin shim until all users have migrated.

**Fix 4 — `scripts/open_control_panel.command`**: Replace body with `open -a "Krab Ear" --args --show-history`.

**Fix 5 — Remove `start_agent.command`** (safe only after all fixes above + one release cycle for migration): Add explicit note in deprecated banner that it will be removed in next release.

### Safety gate

Before removing `start_agent.command`:
- `grep -rn "start_agent.command" . | grep -v "\.git\|test_migration_scripts\|audit\|DEPRECATED"` must return zero hits.
- CI `test_start_agent_audit.py::test_no_callers_for_runtime_path_in_repo` must pass.
