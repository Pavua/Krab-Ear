# Wave 807 — Scheduled Routine Reference Fixes

**Date:** 2026-05-26  
**Branch:** feature/fix-disk-hygiene-routine-W807  
**Audit source:** W799

## Summary

Two scheduled routines (`~/.claude/scheduled-tasks/`) referenced a non-existent file
`docs/llm-bench-results.md`. The file was part of a bench-rounds workflow that is now
gitignored (per security-audit-2026-05-12.md, item MED-3: `docs/llm-bench-results-*.md`
added to `.gitignore`). One routine also had a stale production rewriter model reference.

Both SKILL.md files were edited **directly** outside the repo (they live in
`~/.claude/scheduled-tasks/`, not in the git tree).

---

## Fixes Applied

### 1. `krab-ear-disk-hygiene` — broken doc reference

**File:** `~/.claude/scheduled-tasks/krab-ear-disk-hygiene/SKILL.md`

| Field | Before | After |
|-------|--------|-------|
| Doc reference | `docs/llm-bench-results.md` (does not exist) | `docs/BENCHMARK_M4_MAX.md` (exists, production stack reference) |
| Step 3 cross-ref | "🗑️ DELETE section of llm-bench-results.md" | "superseded/duplicate models vs production stack in BENCHMARK_M4_MAX.md" |
| Red alert text | "DELETE-tagged models" | "superseded/duplicate models" |

**Root cause:** `llm-bench-results.md` was never committed (gitignored per MED-3). Closest
surviving equivalent is `docs/BENCHMARK_M4_MAX.md` which documents the production model
stack. Historical per-round files (`llm-bench-results-R19.md`, `llm-bench-results-R22.md`)
are gitignored and not guaranteed to be present locally.

### 2. `krab-ear-fresh-mlx-models-watcher` — stale production model

**File:** `~/.claude/scheduled-tasks/krab-ear-fresh-mlx-models-watcher/SKILL.md`

| Field | Before | After |
|-------|--------|-------|
| Production rewriter | `qwen3.5-9b@6bit` (R19, retired) | `gemma-4-26b-a4b-it-optiq` (R22, current) |
| Doc reference (step 3) | `docs/llm-bench-results.md` | `docs/BENCHMARK_M4_MAX.md` (with note about gitignored R* files) |

**Root cause:** Model was upgraded in Wave 42 audit (2026-05-12) from qwen3-4b-abliterated →
gemma-4-26b-a4b-it-optiq, but the routine was never updated.

---

## Files That Cannot Be Committed

The actual SKILL.md files live at:
- `~/.claude/scheduled-tasks/krab-ear-disk-hygiene/SKILL.md`
- `~/.claude/scheduled-tasks/krab-ear-fresh-mlx-models-watcher/SKILL.md`

These are outside the git repository and were edited directly. This audit doc serves as a
record of what was changed and why.

---

## Verification

After applying fixes, the routines will:
- Reference `docs/BENCHMARK_M4_MAX.md` (committed, always present)
- Correctly identify `gemma-4-26b-a4b-it-optiq` as the production rewriter
- Gracefully handle absent `llm-bench-results-R*.md` files
