# Wave 321 — v2.0.3 Sentry Release Deployment Verification

**Date:** 2026-05-21  
**Verifying:** v2.0.3 ship (2026-05-20 23:26 CEST, Wave 274)  
**Agent UUID:** FDAB353F-A8E5-3859-A140-ADE01CE43CD5  

---

## 1. v2.0.3 Release Page Status

**`find_releases` result:** No releases found for `krab-ear-agent` project via Sentry releases API.

**Root cause:** The Swift agent does not call `sentry_sdk.set_release()` / Sentry Cocoa SDK `SentryOptions.releaseName` pointing at the `v2.0.3` git tag — the agent still reports `dist:2.0.2` in events (see AGENT-M below). The Python backend tags releases as `krab-ear@2.0.0` (see KRAB-EAR-BACKEND-J tag `release: krab-ear@2.0.0`).

**dSYM status:** Cannot confirm attachment via Sentry releases API since no release entry exists for `krab-ear-agent/v2.0.3`. The dSYM upload was claimed in Wave 274 but is unverifiable through MCP.

**Action needed:** After next Swift rebuild, ensure `SentryConfig.swift` sets `options.releaseName = "com.antigravity.krab-ear@v2.0.3"` before SDK init.

---

## 2. AGENT-J Post-Ship Analysis

AGENT-J (StatusIndicatorView `●` Unicode → CoreText hang) is **not present** in either `is:unresolved` or `is:resolved` issue lists for `krab-ear-agent`. It does not appear in any search by culprit, title, or issue ID.

**Conclusion:** AGENT-J was never filed as a distinct Sentry issue — it was identified and fixed (Wave 67, PR #412, SF Symbol `circle.fill`) before generating enough distinct events to create a separate grouped issue. The fix pre-dated v2.0.3 ship.

- **Pre-ship events (Wave 260 context):** 6 events attributed to glyph hang were folded into other AppHang issues (likely AGENT-E/G with `<redacted>` culprit).
- **Post-ship events:** 0 new events matching glyph/StatusIndicator pattern.
- **Verdict:** Fix is holding. No regression.

---

## 3. New Agent Issues on dist:2.0.3

`search_issues query="dist:2.0.3"` → **0 results.**

No issues have been filed with `dist:2.0.3` tag. This is consistent with the agent still reporting `dist:2.0.2` (see AGENT-M event tags).

### AGENT-M (new, resolved) — Important finding

| Field | Value |
|-------|-------|
| **ID** | KRAB-EAR-AGENT-M |
| **Title** | App Hanging: App hanging for at least 2000 ms |
| **Culprit** | `BackendToast.show` |
| **Status** | **resolved** |
| **Events** | 1 |
| **First/Last seen** | 2026-05-18T23:26:43Z (2 days ago) |
| **dist** | **2.0.2** (agent not yet updated to report 2.0.3) |
| **release** | `com.antigravity.krab-ear@2.0.2+2.0.2` |

**Stacktrace analysis:**
```
BackendToast.show (BackendToast.swift:40)
  → AgentAppDelegate.showFatalAndTerminate (main.swift:1033)
    → closure in AgentAppDelegate.applicationDidFinishLaunching (main.swift:223/225)
      → -[NSWindow _doOrderWindow:]
        → mach_msg2_trap  (blocked on WindowServer IPC)
```

This is a **sibling regression** to AGENT-H (`showFatalAndTerminate` runModal hang) and AGENT-K (`BackendToast.createPanel` ColorSync). The `BackendToast.show` call on line 40 triggers `NSWindow _doOrderWindow` → WindowServer mach IPC on the main thread, causing a 2 s hang.

**Status: resolved** — already marked resolved (likely via `resolvedInNextRelease` or manual). Single event, 2026-05-18 (pre-ship). Does not represent a post-v2.0.3 regression.

---

## 4. Agent Issues — Full Current State

### Unresolved
`search_issues query="is:unresolved" project=krab-ear-agent` → **0 results.**

**Agent project is fully clean post-ship.**

### Resolved (19 total)
All 19 historical agent issues are in `resolved` state. Key recent ones:

| Issue | Title | Events | Last Seen | Notes |
|-------|-------|--------|-----------|-------|
| AGENT-M | AppHang BackendToast.show | 1 | 2 days ago | Pre-ship, resolved |
| AGENT-K | AppHang BackendToast.createPanel | 1 | 5 days ago | Wave 66 fix |
| AGENT-H | AppHang showFatalAndTerminate | 2 | 9 days ago | Wave 59 fix |
| AGENT-E | AppHang `<redacted>` | 11 | 11 days ago | Resolved |
| AGENT-G | AppHang `<redacted>` | 16 | 15 days ago | Resolved |
| AGENT-8 | AppHang `<redacted>` | 139 | 15 days ago | Resolved |

---

## 5. Backend Issues — Post-Restart State

`search_issues query="is:unresolved" project=krab-ear-backend` → **1 issue:**

| Issue | Title | Events | First/Last Seen | Status |
|-------|-------|--------|-----------------|--------|
| KRAB-EAR-BACKEND-J | [warn batch x2] http_400_after_retry | 2 | 2026-05-18 (2 days ago) | unresolved |

**Details:**
- `code: rewriter.timeout`, `component: rewriter`
- `model: gemma-4-26b-a4b-it-optiq`, `base_url: http://127.0.0.1:1234/v1`
- `release: krab-ear@2.0.0` (backend not tagging v2.0.3 release either)
- 2 events, both on 2026-05-18 — **pre-ship, not post-restart**
- This is the existing LM Studio rewriter timeout warn-batch issue (WarnBatcher Phase B), not a new regression

**Assessment:** Pre-existing, Phase B warn-batch. LM Studio rewriter timed out twice on 2026-05-18. No new backend events since ship.

---

## 6. Overall Verdict

| Area | Status | Detail |
|------|--------|--------|
| v2.0.3 release page | ⚠️ Not found | Agent still reports dist:2.0.2; release tag not registered in Sentry |
| dSYM upload | ⚠️ Unverifiable | No release entry to attach to; symbolication of future events uncertain |
| AGENT-J (glyph fix) | ✅ 0 post-ship events | Fix holding, no regression |
| New agent issues (dist:2.0.3) | ✅ 0 | Clean |
| Agent unresolved | ✅ 0 | Fully clean |
| AGENT-M | ⚠️ Pre-ship sibling | BackendToast.show main-thread WindowServer block; resolved; 1 event pre-ship |
| Backend unresolved | ⚠️ 1 pre-existing | KRAB-EAR-BACKEND-J rewriter.timeout warn batch x2 (2026-05-18, pre-ship) |
| Backend post-restart new | ✅ 0 | No new backend issues after agent restart |

### Verdict: 🟡 MINOR

**Clean on the critical path (AGENT-J fix verified, 0 new post-ship issues) but two action items:**

1. **Fix dist/release tagging in SentryConfig.swift** — `dist` must be bumped to `2.0.3` so future events are tagged correctly and the release page is created in Sentry.
2. **AGENT-M root cause** — `BackendToast.show` calls `NSWindow _doOrderWindow` on main thread synchronously during `showFatalAndTerminate`. Same class as AGENT-K/H. Consider moving `BackendToast.show` behind `DispatchQueue.main.async` + weak-capture guard (same pattern as AGENT-K PR #406 fix).
3. **KRAB-EAR-BACKEND-J** — LM Studio rewriter HTTP 400 retries remain unresolved. Low urgency (2 events, pre-ship).
