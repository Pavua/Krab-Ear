# Worktree Cleanup — Usage Guide

Wave 311 introduced `scripts/cleanup_merged_worktrees.command` to remove stale
worktrees whose branches are already merged into `codex/krab-ear-v2`.

## Background

Mega-marathon sessions (Waves 86–300+) create hundreds of worktrees in
`.claude/worktrees/` and `/private/tmp/`. After a merge train, these become
orphans: they take up disk space and confuse git/routine tooling that iterates
over `git worktree list`. Wave 301 audit counted **376 total worktrees**.

## When to run

Run after any merge train (typically after a session snapshot commit):

```bash
./scripts/cleanup_merged_worktrees.command           # dry-run first
./scripts/cleanup_merged_worktrees.command --execute # delete confirmed candidates
```

A good trigger is the same moment you run the release checklist:

```
RELEASE_CHECKLIST.md → "Post-merge cleanup" step
```

## Dry-run first (always)

The script is **safe by default** — without `--execute` it only prints what
would be removed and exits without making changes.

```
=== Worktree cleanup audit ===
Total worktrees: 376

  KEEP (main): /Users/pablito/.../Krab Ear  [codex/krab-ear-v2]
  KEEP (locked): .../agent-a00db5f5  [polish/call-ui-quality-fixes]
  ...
  MERGED → candidate: .../affectionate-lovelace-038f6b  [feat/rest-api-auth-tokens]
  PRUNABLE (stale path): /private/tmp/action-items-wt  [feat/action-items-extraction]

=== Summary ===
  Total worktrees scanned : 376
  Candidates for removal  : 312
  Preserved               : 64

DRY RUN — no changes made.
Pass --execute to actually delete candidates.
```

Review the `MERGED →` and `PRUNABLE` lines. If anything looks unexpected,
do **not** run `--execute` and investigate manually.

## What the script skips

| Category | Reason skipped |
|----------|---------------|
| Main repo root | Always preserved |
| `locked` worktrees | Active agent session in progress — `git worktree list` marks these |
| Unmerged branches | Branch tip not reachable from `origin/codex/krab-ear-v2` |
| Detached HEAD / no branch | Cannot determine merge status safely |

## Recovery if a branch was deleted by mistake

Git keeps reflog entries for 90 days by default.

```bash
# Find the deleted branch tip
git reflog show --all | grep 'feat/my-deleted-branch'

# Re-create the branch from the last known SHA
git checkout -b feat/my-deleted-branch <sha>

# Re-add a worktree if needed
git worktree add /tmp/my-wt feat/my-deleted-branch
```

For prunable worktrees (path already gone), only the branch recovery step is
needed — the directory was already absent.

## Locked worktrees

`locked` worktrees belong to active Claude Code agent sessions. The script
skips all of them automatically. After the session ends, the lock is released
and the worktree becomes eligible for cleanup on the next run.

To unlock manually (only if the session is truly dead):

```bash
git worktree unlock .claude/worktrees/<name>
git worktree remove --force .claude/worktrees/<name>
```

## See also

- `scripts/cleanup_worktree_shadows.command` — removes lsregister shadow `.app`
  bundles that appear as duplicates in Spotlight (separate concern).
- `scripts/cleanup_worktree_builds.command` — removes stale `.build/` artifacts
  inside worktrees to reclaim disk space.
