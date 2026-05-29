# Closure: W1518 settings_service 31-test-failure report — W1564

**Date:** 2026-05-29
**Investigator:** W1564
**Triggered by:** W1518 audit doc (PR #1390 commit 3cf77796) claiming 31 failing tests
**Branch HEAD at investigation time:** `d39881a4` (latest `codex/krab-ear-v2`)

---

## Verdict: ALREADY FIXED — ghost audit

The 31 test failures documented in W1518 no longer exist on the current branch. All 72 settings_service
tests and all 298 settings-related tests pass clean:

```
Ran 298 tests in 0.510s
OK
```

---

## Root cause of the ghost audit

The W1518 audit doc (committed 2026-05-27 at 06:53:41 +0200) was written from a worktree snapshot
whose HEAD was `f6bb585e` — a stale checkout that predated 8 fix commits that had already landed on
the main branch.

### Timeline reconstruction

| Time (UTC+2) | Event |
|---|---|
| 2026-05-26 05:49 | `caa105ac` wave941 lands — overwrites ~200 lines of fixes (REAL regression) |
| 2026-05-26 22:46 | `2a7d30e7` wave1173 — SENSITIVE_FIELDS re-unified |
| 2026-05-26 22:48 | `d9b1c7ee` wave1178 — import+restore validator gate + hooks + pre-restore backup |
| 2026-05-27 00:38 | `b974f3cb` wave1341 — `_fire_after_save_hooks` + `reload_settings_from_json` |
| 2026-05-27 03:36 | `65402fb6` wave1308 — `_fire_after_save_hooks` on all 5 save paths |
| 2026-05-27 05:29 | `d557dc9b` wave1437 — RLock `_save_lock` around all 5 save paths |
| 2026-05-27 05:44 | `9f1d9ac5` wave1434 — `import_settings` raises ValueError on invalid data |
| 2026-05-27 05:44 | `00699e3b` wave1436 — `reload_settings_from_json` in all 5 save paths |
| 2026-05-27 05:43 | `979fa74e` wave1435 — `restore_settings_backup` migrate+validate before save |
| 2026-05-27 06:06 | `32a44ce7` wave1454 — `handle_restore_settings_backup` raises ValueError |
| 2026-05-27 06:07 | `522f13ec` wave1457 — `_maybe_migrate` invalidates cache after schema write-back |
| **2026-05-27 06:53** | **3cf77796 W1518 audit committed** — worktree was stale, documented failures that were already fixed |

The W1518 audit worktree checked out the branch at a point where `caa105ac` was the most recent
file-touching commit. By the time the audit doc was written, all 8 fix commits were already in the
main branch — but the worktree was not refreshed before running the test suite.

---

## Verification of current fix state

All methods documented as "REGRESSED" in W1518 are present and correct in the current HEAD:

| Method / feature | W1518 claim | Current HEAD |
|---|---|---|
| `threading.RLock` (`_save_lock`) | Absent | Present (line 78) |
| `_fire_after_save_hooks` | Absent | Present (line 116) |
| `_reload_and_fire_hooks` | Absent | Present (line 124) |
| `_maybe_migrate` | Absent | Present (line 143) |
| `import_settings` raises ValueError | Silent save on invalid | Raises ValueError (line ~567) |
| `restore_settings_backup` validate+migrate | None | Full validation gate (line ~615) |
| `reload_settings_from_json` in all 5 paths | Only 1/5 | All 5 paths |
| Hooks in all 5 paths | Only 1/5 | All 5 paths via `_reload_and_fire_hooks` |

---

## Action taken

No code changes required. Worktree created for investigation is being cleaned up.

This closure doc serves as the W1564 deliverable confirming the W1518-reported 31-failure regression
is resolved and the audit was based on a stale worktree state.

---

## Lesson: stale-worktree false-alarm pattern

W1518 is the second known instance of an audit agent working from a stale worktree checkout and
documenting failures that were already fixed on the main branch. Prevention:

1. Always run `git pull --rebase` or `git fetch && git reset --hard origin/codex/krab-ear-v2` at
   the start of any audit session.
2. Record the HEAD commit SHA in the audit doc and verify it matches `origin/codex/krab-ear-v2`
   before claiming failures are "live".
3. Cross-check fix-commit timestamps against audit doc commit time — if fix commits are older than
   the audit, the failures may already be resolved.
