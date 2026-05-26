# Wave 799 — Scheduled Routines Audit

**Date:** 2026-05-26
**Auditor:** wave799/stale-routines-audit
**Scope:** All `~/.claude/scheduled-tasks/krab-ear-*` directories

---

## Summary

| Metric | Count |
|--------|-------|
| Total routines | 19 |
| Stale (>30 days, topic outdated) | 5 |
| Duplicate pairs | 1 pair (2 routines) |
| Missing cron field | 16 |
| Broken file dependencies | 1 |

---

## Full Inventory

| Routine | Last Modified | cron | Purpose |
|---------|---------------|------|---------|
| krab-ear-backend-log-scanner | 2026-05-22 | none | Daily backend log error/warning scan with extended categories (Wave 440+) |
| krab-ear-bench-monitor | 2026-05-12 | `30 11 * * 0` | Weekly Sunday: bench staleness + regression vs production model |
| krab-ear-bench-regression | 2026-05-12 | none | Monthly: detect LLM rewriter regression vs gemma-4-26b-a4b-it-optiq |
| krab-ear-disk-hygiene | 2026-04-29 | none | Weekly: 4TB SSD free space + LMStudio model junk detection |
| krab-ear-dsym-upload-verify | 2026-05-12 | `30 7 * * *` | Daily: verify bundle dSYM is uploaded to Sentry krab-ear-agent |
| krab-ear-e2e-smoke | 2026-05-12 | none | Every 6h: IPC ping, diagnostics, agent process count, Sentry, audio devices |
| krab-ear-figma-drift-check | 2026-04-20 | none | Weekly: full Figma token↔Swift token drift check, auto-PR if drift found |
| krab-ear-figma-usage-recap | 2026-04-20 | none | Weekly: lightweight file-only Figma token diff (no MCP) |
| krab-ear-fresh-mlx-models-watcher | 2026-04-30 | none | Weekly Monday: HuggingFace trending MLX abliterated models scan |
| krab-ear-hf-cache-audit | 2026-04-20 | none | Monthly: HF model cache size + used vs unused models |
| krab-ear-memory-sync | 2026-04-20 | none | Weekly: .remember/ and MEMORY.md consistency check |
| krab-ear-mlx-llm-upstream-watcher | 2026-04-29 | none | Weekly Monday: mlx-lm + LM Studio releases for Gemma 4 / qwen3_5 unblock |
| krab-ear-pr-digest | 2026-04-20 | none | Daily: open PR status digest (mergeable/CI-failing/conflicting/pending) |
| krab-ear-sentry-sweep | 2026-04-24 | none | Sweep unresolved Sentry issues for both projects; simple auto-fix |
| krab-ear-session-recap | 2026-04-20 | none | Weekly: git log 7d, categorize commits, save recap file |
| krab-ear-startup-diagnostic | 2026-05-12 | none | Daily: run StartupDiagnostics Python class, alert if any check != ok |
| krab-ear-swift-warnings-audit | 2026-04-20 | none | Weekly: swift build strict, compare warnings vs baseline |
| krab-ear-test-health | 2026-04-20 | none | Daily: Python pytest + Swift tests; open GitHub issue on failure |
| krab-ear-two-binary-drift-watch | 2026-05-13 | `30 6 * * *` | Daily: UUID mismatch between bundle and native/runtime binary |

---

## Stale Routines (>30 days, topic outdated)

Cutoff: last modified before **2026-04-26** (30 days before audit date 2026-05-26).

### 1. `krab-ear-figma-drift-check` — STALE
- **Last modified:** 2026-04-20
- **Days since edit:** 36
- **Why stale:** References `design-tokens/krab-ear-tokens.json` which exists but has not been updated since 2026-04-20 (same date). The Gemini design key was revoked 2026-04-20 (per memory: `reference_gemini_key_status.md`) and design workflow switched to Claude Design; Figma sync is effectively paused. The routine would auto-create PRs against a token file frozen for 36 days.
- **Risk:** Could open spurious PRs if run.

### 2. `krab-ear-figma-usage-recap` — STALE + DUPLICATE
- **Last modified:** 2026-04-20
- **Days since edit:** 36
- **Why stale:** Same token-file freeze as above. Lightweight version of `krab-ear-figma-drift-check` (see Duplicates section).

### 3. `krab-ear-mlx-llm-upstream-watcher` — STALE
- **Last modified:** 2026-04-29
- **Days since edit:** 27 (borderline, but topic is outdated)
- **Why stale:** References "currently blocked on mlx-lm 0.31.3 doesn't support Gemma 4". The project has since moved through multiple LLM bench rounds (R19+ shipped gemma-4-26b-a4b-it-optiq as winner per Wave 42). The "blocked on Gemma 4" framing is outdated; LM Studio + mlx-lm blockers noted in the SKILL.md have likely resolved or shifted. The routine still mentions R18/R19 as pending.
- **Risk:** Will fetch WebFetch URLs and produce stale-framed recommendations.

### 4. `krab-ear-fresh-mlx-models-watcher` — STALE (production model mismatch)
- **Last modified:** 2026-04-30
- **Days since edit:** 26 (borderline)
- **Why stale:** SKILL.md states "Production rewriter: `qwen3.5-9b@6bit` (R19 winner, 1.4 s avg, 7.7 GB)" — but per Wave 42 production audit (memory: `project_session_2026-05-12_wave42_routines_review.md`) the actual LM Studio model is `gemma-4-26b-a4b-it-optiq`, not qwen3.5-9b. The routine's cross-reference logic against the wrong production winner will produce misleading recommendations.
- **Risk:** Incorrect "already tested" cross-referencing.

### 5. `krab-ear-disk-hygiene` — STALE (broken dependency)
- **Last modified:** 2026-04-29
- **Days since edit:** 27 (borderline)
- **Why stale:** Step 3 references `docs/llm-bench-results.md` for its DELETE-tagged model list. That file **does not exist** in the repository (confirmed by filesystem check). `docs/BENCHMARK_M4_MAX.md` is the actual bench doc (exists, last modified 2026-05-14). The routine will silently fail to cross-reference any DELETE-tagged models.
- **Risk:** Disk cleanup recommendations will be incomplete; cross-reference step always produces empty list.

---

## Duplicate Routines

### Pair: `krab-ear-figma-drift-check` + `krab-ear-figma-usage-recap`

Both routines perform Figma design token drift checking between `design-tokens/krab-ear-tokens.json` and `KrabEarTheme.swift`. The stated differentiation:

| Attribute | figma-drift-check | figma-usage-recap |
|-----------|-------------------|-------------------|
| Figma MCP call | Yes (implied) | No |
| Auto-PR on drift | Yes | No |
| Label | "Weekly" full check | "Lightweight" weekly |
| Last modified | 2026-04-20 | 2026-04-20 |

The descriptions imply they are scheduled on different days (figma-drift-check on Fri, figma-usage-recap as lightweight alternative), but neither SKILL.md has a `cron:` field. With Gemini key revoked since 2026-04-20 and design workflow paused, both routines are effectively dormant duplicates. If design token work resumes, only one should be kept.

**Verdict:** Functional duplicate (same data sources, same comparison goal). Can consolidate into one with optional `--full` flag when design workflow is active.

---

## Missing cron Fields

16 of 19 routines lack a `cron:` field in their SKILL.md frontmatter. Only three have explicit schedules:

| Routine | cron |
|---------|------|
| krab-ear-bench-monitor | `30 11 * * 0` (Sunday 11:30) |
| krab-ear-dsym-upload-verify | `30 7 * * *` (daily 07:30) |
| krab-ear-two-binary-drift-watch | `30 6 * * *` (daily 06:30) |

The 16 without a `cron:` field rely on manual invocation or external scheduling not reflected in the SKILL.md. This is not necessarily a problem if they are triggered by the scheduler MCP with schedule stored elsewhere, but makes auditing difficult.

---

## Healthy Routines (no action needed)

| Routine | Status |
|---------|--------|
| krab-ear-backend-log-scanner | Active, script dep present, Wave 440+ extended categories |
| krab-ear-bench-monitor | Active, has cron, cross-ref logic current |
| krab-ear-bench-regression | Active, references correct BENCHMARK_M4_MAX.md |
| krab-ear-dsym-upload-verify | Active, has cron, Sentry auth pattern correct |
| krab-ear-e2e-smoke | Active, Wave 50 auto-recovery added, IPC paths correct |
| krab-ear-hf-cache-audit | Low risk, read-only, no broken deps |
| krab-ear-memory-sync | Low risk, read-only |
| krab-ear-pr-digest | Active, gh CLI command correct |
| krab-ear-sentry-sweep | Active, Sentry org/project slugs correct |
| krab-ear-session-recap | Active, git log command correct |
| krab-ear-startup-diagnostic | Active, Python one-liner correct |
| krab-ear-test-health | Active, but runs `python -m pytest` (note: CLAUDE.md says worktrees use `python -m unittest` — may fail in worktree context) |
| krab-ear-two-binary-drift-watch | Active, has cron, IPC method correct |

---

## Recommended Actions

| Priority | Routine | Action |
|----------|---------|--------|
| HIGH | krab-ear-disk-hygiene | Fix: replace `llm-bench-results.md` reference with `docs/BENCHMARK_M4_MAX.md` |
| HIGH | krab-ear-fresh-mlx-models-watcher | Fix: update production rewriter from `qwen3.5-9b@6bit` to `gemma-4-26b-a4b-it-optiq` |
| MED | krab-ear-mlx-llm-upstream-watcher | Update: remove "blocked on Gemma 4 / qwen3_5" framing; refresh with current known blockers |
| MED | krab-ear-figma-drift-check + figma-usage-recap | Consolidate or mark dormant until Gemini key is renewed |
| LOW | 16 routines | Add `cron:` field to SKILL.md frontmatter for audit visibility |

---

*This audit is read-only. No routines were unregistered or modified.*
