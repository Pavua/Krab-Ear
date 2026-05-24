# Wave 521 — macOS Sequoia 26 Deeper Integration Tests

## Overview

Wave 521 adds `Sequoia26IntegrationTests.swift` — 35 headless integration tests that go
beyond Wave 416's static source scans to verify runtime behaviour of the eight known
macOS Sequoia 26 issue categories.

**File:** `native/KrabEarAgent/Tests/KrabEarAgentTests/Sequoia26IntegrationTests.swift`

---

## Test Coverage Matrix

| # | Sequoia 26 Category | Test Class | Tests | Coverage |
|---|---------------------|------------|-------|----------|
| 1 | TCC — Microphone | `Sequoia26TCCMicrophoneTests` | 3 | `AVCaptureDevice.authorizationStatus` returns valid value; all switch branches covered; `requestAccess` API callable |
| 2 | TCC — Accessibility | `Sequoia26TCCAccessibilityTests` | 3 | `AXIsProcessTrusted()` callable without crash; `PasteService` instantiates; `SelectionTranslator` instantiates without AX grant |
| 3 | CoreText first render | `Sequoia26CoreTextPrewarmTests` | 4 | SF Symbol "circle.fill" available; `StatusIndicatorView.draw()` <1s; no Unicode ● in toolTip; palette colors work for all HealthState |
| 4 | AppHang — BackendToast | `Sequoia26BackendToastMainThreadTests` | 3 | `show()` after prewarm <16ms; prewarm caches CoreText metrics; Cyrillic+emoji batch <500ms |
| 5 | launchd plist format | `Sequoia26LaunchdPlistTests` | 5 | Template exists; required keys present; template is valid XML; `buildPlistContent()` valid XML; KeepAlive key present |
| 6 | Two-binary drift | `Sequoia26TwoBinaryDriftTests` | 6 | UUID parser extracts correctly; matching UUIDs = no drift; mismatched = drift detected; nil for empty/invalid; known production UUIDs parse |
| 7 | Sentry terminate breadcrumb | `Sequoia26SentryTerminateBreadcrumbTests` | 6 | no-op when inactive; empty callsite no crash; nil/empty DSN stays inactive; privacy mode skips Sentry; recordBreadcrumb no-op when inactive |
| 8 | HealthMonitor 3s ping | `Sequoia26HealthMonitorPingLoopTests` | 5 | ≥3 pings in 0.25s at 0.05s interval; production 3s interval creates without crash; stop cancels loop; hung after 2+ consecutive failures; recovers after single fail |

**Total: 35 tests, 8 test classes, 0 failures**

---

## Covered vs Gap Analysis

### Covered ✓

| Issue | What's Tested |
|-------|--------------|
| TCC Microphone (Sequoia 26 quirk: TCC doesn't commit changes via System Settings UI) | API contract, all switch branches, requestAccess callback |
| TCC Accessibility (paste path) | AXIsProcessTrusted API, PasteService + SelectionTranslator instantiation guards |
| CoreText "first render" hang (AGENT-J, Wave 67) | SF Symbol availability, draw() timing, no Unicode ● regression |
| BackendToast AppHang on Cyrillic (AGENT-M, Wave 266) | main-thread budget <16ms, prewarm cache, Cyrillic+emoji batch timing |
| launchd plist XML format | Both backend template and Swift-generated agent plist XML validity |
| Two-binary drift UUID matching | Parser correctness, match/mismatch detection, production UUID patterns |
| Sentry breadcrumb on terminate | No-op guards, privacy mode, DSN absence safety |
| HealthMonitor 3s ping cadence | Firing rate, stop cancellation, hung/recovery state machine |

### Gaps (not yet covered)

| Issue | Gap | Recommendation |
|-------|-----|----------------|
| TCC doesn't persist across System Settings UI on Sequoia | Cannot test TCC.db state machine without real user interaction | Add to manual QA checklist; instrument `tccutil` output in CI smoke test |
| macOS daily auto-update reboot (14:04 CEST) | System behaviour, not testable in unit scope | Monitor via Datadog uptime alert; document in runbook |
| PyTorch MPS concurrent inference (MLX inter-process lock) | Requires actual MLX inference + Metal GPU — prohibited by Wave 521 constraint | Add to integration-level soak test (`docs/SOAK_TESTING.md`) |
| `lsregister` worktree shadow drift | Shell-level command, not Swift unit-testable | `scripts/cleanup_worktree_shadows.command` covers this; add to CI post-build step |
| Real dwarfdump execution | Intentionally mocked — UUID parser logic is tested, not subprocess | Add to `make verify-binary-sync` Make target that runs real dwarfdump post-build |

---

## CI Integration Recommendations

### 1. Add to `swift test` filter group

```bash
# In .github/workflows/ci.yml, add to existing swift test step:
swift test --filter Sequoia26
```

### 2. Timing tests need macOS runner with display

Tests `Sequoia26BackendToastMainThreadTests` and `Sequoia26CoreTextPrewarmTests` guard
on `NSScreen.main != nil` — they skip gracefully in headless CI. For timing coverage,
run on a macOS GitHub Actions runner with display:

```yaml
runs-on: macos-latest  # has display; BackendToast timing tests will run
```

### 3. AVCaptureDevice requestAccess timeout

`test_TCC_microphone_requestAccess_API_is_callable` has a 3s timeout. In CI without
an Info.plist with `NSMicrophoneUsageDescription`, the callback arrives immediately
(denied). This is safe.

### 4. Separate soak target for PyTorch MPS

The MLX/MPS concurrent inference gap belongs in the existing soak framework:
```bash
make test-soak  # runs long-running integration tests including MLX load tests
```

---

## Running Locally

```bash
# Run all Wave 521 Sequoia tests
cd native/KrabEarAgent
swift test --filter Sequoia26

# Run a single test class
swift test --filter Sequoia26TwoBinaryDriftTests

# Run with verbose output
swift test --filter Sequoia26 --verbose
```

Expected output: `Executed 35 tests, with 0 failures`

---

## Related Files

- `native/KrabEarAgent/Tests/KrabEarAgentTests/Sequoia26IntegrationTests.swift` — this test file
- `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicatorView.swift` — Wave 67 SF Symbol fix
- `native/KrabEarAgent/Sources/KrabEarAgent/BackendToast.swift` — AGENT-M fix (Wave 266)
- `native/KrabEarAgent/Sources/KrabEarAgent/SentryConfig.swift` — terminate breadcrumb
- `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift` — 3s ping loop
- `KrabEar/launchagents/ai.krab.ear.backend.plist.template` — backend plist template
- `scripts/install_backend_launchagent.command` — launchd install script
