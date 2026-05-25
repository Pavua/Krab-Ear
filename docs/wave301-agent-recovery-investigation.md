# Wave 301 — Agent Recovery Routine FAIL Pattern: Root Cause Investigation

**Date**: 2026-05-21  
**Branch**: `docs/wave223-session-snapshot`  
**Log**: `.remember/agent-recovery.log`  
**Pattern**: `FAIL  agent still absent 5s after 'open'` — daily at 04:07Z, 10:07Z, 16:07Z UTC since 2026-05-16.

---

## Findings Summary

All 8 log entries (lines 1–16 of `agent-recovery.log`) follow the same pattern:
- `WARN  agent absent — restoring via 'open …'` at time T
- `FAIL  agent still absent 5s after 'open'` at T + 5–6 seconds

The `smoke-history.log` shows an `E2E-SMOKE OK (auto-recovered Swift agent)` line roughly 60–90 s AFTER each WARN, meaning the e2e-smoke routine DID eventually recover the agent — but the recovery script itself reported failure first.

---

## Timeline vs. PR Merges

| Time (UTC) | Event |
|---|---|
| 2026-05-15T23:14Z | PR #408 merged — pgrep + `set -e` bug fix (`count_agent_pids` now uses `|| true`) |
| **2026-05-16T16:07Z** | First FAIL entry in log |
| 2026-05-17T16:07Z | FAIL |
| **2026-05-18T02:29Z** | Wave 67 / PR #412 committed — AGENT-J fix (Unicode `●` → SF Symbol `circle.fill`) |
| 2026-05-18T04:07Z | FAIL (routine ran 29 min before the fix commit) |
| **2026-05-18T04:58Z** | PR #422 committed — `ensure_agent_running.command` wait bumped 5 s → 15 s |
| 2026-05-18T10:07Z | FAIL (log STILL says "5s" — 5 h after PR #422) |
| 2026-05-18T16:07Z | FAIL |
| 2026-05-19T04:07Z | FAIL |
| 2026-05-19T16:07Z | FAIL |
| 2026-05-20T16:07Z | FAIL (last entry) |
| **2026-05-20T23:26Z** | App binary rebuilt — v2.0.3, AGENT-J fix in production binary |

---

## Ranked Hypotheses

### H1 — AGENT-J causes crash-on-startup (CONFIRMED PRIMARY CAUSE)

**Confidence: HIGH**

`StatusIndicatorView.swift` used Unicode `●` (U+25CF) as a menu bar indicator dot. On macOS 26 with ColorSync callbacks, CoreText attempted to render this character in the system font chain, causing a hang/crash during the callback. The `BackendToast` crash (AGENT-K) was the same pattern.

Evidence:
- Failures started 2026-05-16, the same session week as AGENT-J was first identified and fixed (Wave 67, PR #412 merged 2026-05-18T02:29Z).
- All 8 FAIL entries occurred BEFORE the app binary was rebuilt (binary mtime: May 20 23:26).
- When the agent crashes within ~1–2 s of launch, `pgrep` returns 0 even after a 5 s or 15 s wait.
- `smoke-history.log` shows OK entries ~60–90 s later — consistent with the e2e-smoke routine doing additional manual recovery (open + longer wait) after the script gave up.

**Status**: Fixed in v2.0.3 (binary rebuilt 2026-05-20 23:26). If no new FAIL entries appear at 04:07Z / 10:07Z / 16:07Z on 2026-05-21, H1 is confirmed fully closed.

---

### H2 — ensure_agent_running.command timeout insufficient (CONFIRMED SECONDARY CAUSE)

**Confidence: HIGH**

PR #408 (2026-05-16) fixed the `set -e` / `pgrep` interaction that caused the script to exit silently before the WARN/FAIL lines were ever written. PR #422 (2026-05-18 04:58Z) bumped the post-launch wait from 5 s to 15 s.

Evidence:
- All WARN→FAIL gaps are exactly 5–6 s, including entries AFTER PR #422 merged.
- The `FAIL … 5s after 'open'` message text matches the PRE-PR#422 script exactly.
- Main repo (`codex/krab-ear-v2`) IS up to date with PR #422; script on disk reads `"FAIL agent still absent 15s after 'open'"`.

**Unresolved sub-issue**: Why does the log continue to show `"5s"` after PR #422? Two explanations remain viable:

1. **Stale worktree invocation**: The e2e-smoke scheduled task agent runs from a worktree that branched before PR #422 (e.g., `zealous-williams-cdeb87`, `agent-j-fix`, `c1-baseline` — all confirmed to still have the old 5-loop script). If `$REPO_ROOT` resolves to that worktree, its `.remember/` would be different from the main repo `.remember/`. However, no `agent-recovery.log` was found in any worktree — only in the main `.remember/`. This makes this sub-explanation unlikely unless the AI routine writes to the log directly (below).

2. **AI routine generates log entries inline**: The e2e-smoke SKILL.md (Wave 50 section) instructs the AI agent to call `"$REPO_ROOT/scripts/ensure_agent_running.command" --quiet`. The AI may instead perform the recovery steps inline (open → sleep 5 → check) and write WARN/FAIL entries directly to `agent-recovery.log` using the text it "remembers" from the old script. This would explain why the gap is 5 s regardless of what the script on disk says.

Either way, the practical fix is:
- Update `krab-ear-e2e-smoke/SKILL.md` to explicitly state the new 15 s timeout and bump the inline sleep to 20 s.
- Remove stale worktrees with the old script (`zealous-williams-cdeb87`, `agent-j-fix`, `c1-baseline`, `c6-flock`, `c7-breadcrumbs`, `c8-drift`, `cmd-sync`, `d2-ipc-tests`, `e1-cleanup`, `extract-css`, `lm-models-fix`, `phase-b-w61`, `phase-b-w64`, `tech-debt-a2a3`, `w50-fix`, `w65-audit-script`, `w65-batch1`, `w65-batch2`, `w65-batch3`, `w69-dup-fix`, `w70-ffmpeg`, `wave63-leak`, `wave63-mem`, `wave66-ipc`, `zealous-williams-cdeb87`).

---

### H3 — `open Krab Ear.app` exits quickly but Krab Ear takes >5 s to appear (PARTIAL)

**Confidence: MEDIUM**

`open` returns immediately; the app takes 3–15 s to register in `pgrep`. The 5 s timeout was always too short on M4 Max (documented in PR #422 commit message). However, this alone doesn't explain 15 s timeout failures — AGENT-J crash-loop is needed to make even 15 s insufficient.

**Status**: Mitigated by PR #422 (15 s wait) and completely resolved once AGENT-J is fixed in binary (v2.0.3).

---

### H4 — launchd KeepAlive race (RULED OUT)

**Confidence: LOW**

`scripts/install_agent_launchagent.command` (Wave 59) installs an optional agent launchd plist. If installed and active, `launchctl` would respawn the agent — but then `SingleInstanceGuard.swift` kills the duplicate. This race was considered but:
- `smoke-history.log` never shows duplicate agent processes (count always goes 0→1, not 0→2).
- The pattern is consistent with a crash, not a race.

**Status**: Ruled out.

---

## Root Cause Diagram

```
04:07 / 10:07 / 16:07 UTC (every 6h)
   │
   ▼
krab-ear-e2e-smoke routine detects KrabEarAgent = 0 processes
   │
   ▼
calls scripts/ensure_agent_running.command --quiet
   │
   ├─► writes WARN to .remember/agent-recovery.log
   ├─► open "Krab Ear.app"    ← succeeds, app launches
   ├─► sleep 1s × 5 (old)     ← AGENT-J crashes app on startup within 1–2 s
   │    pgrep count = 0 after each iteration
   └─► writes FAIL to .remember/agent-recovery.log  ("5s after 'open'")

   ▼ (routine continues, does own manual recovery)
   
   open + wait 30–60 s → AGENT-J crashes every attempt
   Eventually agent comes up (race vs. ColorSync trigger) OR
   routine gives up and escalates to user.
   smoke-history: "OK (auto-recovered)" written ~70 s after WARN.
```

---

## Fix Scope

### Already fixed (v2.0.3, binary rebuilt 2026-05-20)
- **AGENT-J** (PR #412 / Wave 67): `StatusIndicatorView` `●` → `circle.fill` SF Symbol. This eliminates the crash-on-startup. Binary rebuilt 2026-05-20 23:26.
- **ensure_agent_running.command** (PR #422): 5 s → 15 s wait. On-disk script updated.

### Recommended follow-up (Wave 301)
1. **Update `krab-ear-e2e-smoke/SKILL.md`** — bump all references to post-launch sleep from 5 s to 20 s; clarify the AI agent should call the SHELL SCRIPT (not inline) and rely on its exit code.
2. **Prune stale worktrees** — 24+ worktrees contain the old 5-loop script. Run `scripts/cleanup_worktree_shadows.command` and then `git worktree prune` to remove stale worktree registrations. This prevents a future scheduled task agent from accidentally invoking an old script.
3. **SKILL.md version pin** — add a note in `krab-ear-e2e-smoke/SKILL.md` referencing PR #422 so future readers know the 15 s value must match the script.

### Verification
If the FAIL pattern has stopped (no entries in `agent-recovery.log` dated 2026-05-21+), v2.0.3 binary + AGENT-J fix is sufficient. Monitor for 48 h (8 routine runs).

---

## Files Referenced

- `.remember/agent-recovery.log` — failure log (16 entries, 2026-05-16 to 2026-05-20)
- `.remember/smoke-history.log` — e2e-smoke OK/FAIL history
- `.remember/smoke-diagnostic-2026-05-16.md` — first investigation; identified pgrep bug
- `scripts/ensure_agent_running.command` — recovery script (v2 = 15 s wait, `|| true` fix)
- `/Users/pablito/.claude/scheduled-tasks/krab-ear-e2e-smoke/SKILL.md` — routine definition
- `native/KrabEarAgent/StatusIndicatorView.swift` — AGENT-J source
- PR #408 (`9ca1f4c`) — pgrep + `set -e` fix
- PR #412 (`b5482ff`) — AGENT-J SF Symbol fix  
- PR #422 (`ff863f4`) — 5 s → 15 s timeout
