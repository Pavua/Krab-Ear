# Wave 336 — Sentry dist:2.0.3 Tracking Verification

**Date**: 2026-05-21
**PR under test**: #588 (Wave 326 — Fix Sentry dist tracking)

---

## PR #588 Status

| Field | Value |
|-------|-------|
| State | **OPEN** (not merged) |
| mergedAt | `null` |
| mergeStateStatus | `CLEAN` (no conflicts) |

PR #588 is ready to merge but has not been merged yet. The dist fix code is not yet in `codex/krab-ear-v2`.

---

## Sentry Release Pages

| Project | Release | Created | Status |
|---------|---------|---------|--------|
| `krab-ear-agent` | `v2.0.3` | 2026-05-20T23:36:21Z | ✅ EXISTS |
| `krab-ear-backend` | `v2.0.3` | 2026-05-20T23:36:21Z | ✅ EXISTS |

Both release pages were created by Wave 326 (PR #588 pre-requisite step). Release infrastructure is in place.

---

## Events with dist:2.0.3

| Query | Results |
|-------|---------|
| `dist:2.0.3` (last 7d) | **0 events** |
| `release:v2.0.3` (last 7d) | **0 events** |

Zero events tagged with `dist:2.0.3` — expected: the binary must be rebuilt and the agent restarted before events bearing this dist tag can appear in Sentry.

---

## Root Cause Chain

```
PR #588 not merged
  → fix not in codex/krab-ear-v2
    → binary not rebuilt
      → running agent still reports old dist (or no dist)
        → dist:2.0.3 = 0 events in Sentry
```

---

## User Action Required

1. **Merge PR #588** (mergeStateStatus=CLEAN, no conflicts)
2. **Rebuild Swift agent**:
   ```bash
   cd native/KrabEarAgent && swift build -c release
   cp -f .build/release/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
   codesign -s - -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
   ```
3. **Restart agent** (quit from menu bar → reopen `Krab Ear.app`)
4. **Re-verify** in Sentry: search `dist:2.0.3` — should show events within minutes of first crash or error

---

## Verdict

🟡 **PENDING REBUILD**

- Release pages exist for both projects (✅)
- PR #588 fix is ready but unmerged (⚠️)
- Binary not rebuilt → 0 events with dist:2.0.3 (expected)
- No regression detected — just awaiting merge + rebuild cycle
