# Wave 810 — scripts/ Directory Audit

**Date:** 2026-05-26  
**Scope:** `/scripts/` directory — all `.sh`, `.command`, `.py`, `.md` files  
**Total scripts:** 68 (57 `.command`, 8 `.py`, 2 `.sh`, 1 `.md`)  
**Method:** last-git-commit date, grep for references in CLAUDE.md / Makefile / .github/workflows/, docs/ cross-reference

---

## Summary

| Category | Count |
|---|---|
| Total scripts | 68 |
| Active / referenced in live workflow | 37 |
| Potentially obsolete (no live caller, last edit >60 days) | 18 |
| Confirmed duplicates | 3 pairs |
| Undocumented (not in CLAUDE.md, Makefile, or CI) | 9 |
| Security note (token was present, now env-read) | 1 |

---

## Section 1 — Confirmed Active (referenced from CI, Makefile, CLAUDE.md, or USER_MANUAL)

These scripts are wired into live workflows and should not be touched.

| Script | Last commit | Referenced from |
|---|---|---|
| `verify_claude_md.py` | 2026-05-16 | CI (ci.yml:164) + CLAUDE.md |
| `compare_benchmarks.py` | 2026-04-25 | CI (krabear-ci.yml:53) |
| `build_and_deploy.command` | 2026-05-14 | Makefile `release` target |
| `cleanup_worktree_builds.command` | 2026-05-14 | Makefile `clean-worktree-builds` target |
| `install_agent_launchagent.command` | 2026-05-14 | CLAUDE.md, USER_MANUAL, docs/wave316 |
| `install_backend_launchagent.command` | 2026-04-08 | CLAUDE.md (IPC patterns section) |
| `repair_permissions.command` | 2026-04-25 | CLAUDE.md (PR #234), USER_MANUAL |
| `create_local_signing_identity.command` | 2026-04-25 | CLAUDE.md (PR #235) |
| `build_distribution_dmg.command` | 2026-05-14 | CLAUDE.md (PR #229) |
| `memory_baseline.py` | 2026-05-14 | CLAUDE.md (Phase C additions) |
| `cleanup_worktree_shadows.command` | 2026-05-06 | CLAUDE.md, docs/krab-ear-mega-marathon |
| `profile_gigaam_worker.command` | 2026-05-06 | CLAUDE.md (Phase C) |
| `audit_dead_ipc_handlers.py` | 2026-05-22 | Internal tooling (Wave 65+) |
| `backend_log_digest.py` | 2026-05-26 | docs/wave440-log-digest-enhancements.md |
| `sentry_create_release.py` | 2026-04-25 | docs/ARCHITECTURE references |
| `start_agent.command` | 2026-05-06 | Root-level `.command` wrappers, TROUBLESHOOTING_PERMISSIONS |
| `ensure_agent_running.command` | 2026-05-18 | Wave 50 self-recovery (CLAUDE.md) |
| `update_agent.command` | 2026-04-25 | Root-level `Update Krab Ear Agent.command` |
| `install_rest_launchagent.command` | 2026-05-20 | Root-level `start_rest_service.command` |
| `prefetch_whisper_models.command` | 2026-05-26 | USER_MANUAL (two references) |
| `install_gigaam_venv.command` | 2026-04-26 | USER_MANUAL, docs |
| `observe_production.command` | 2026-05-26 | docs/krab-ear-mega-marathon (Wave 387) |
| `check_two_binary_drift.sh` | 2026-05-26 | Two-binary drift detection tooling |
| `cleanup_merged_worktrees.command` | 2026-05-26 | Maintenance tooling |
| `kill_dup_gigaam.command` | 2026-05-26 | CHANGELOG.md, RELEASE_NOTES_v2.0.5 |
| `audit_tcc_permissions.command` | 2026-05-26 | RELEASE_NOTES_v2.0.5 (Wave 686) |
| `run_release_checklist.command` | 2026-04-19 | CLAUDE.md, RELEASE_CHECKLIST.md |
| `run_smoke_release.command` | 2026-02-15 | RELEASE_CHECKLIST.md, ROADMAP |
| `create_stable_backup.command` | 2026-02-15 | `Create Stable Backup.command` root wrapper, RELEASE_CHECKLIST_V2 |
| `restore_backup_preview.command` | 2026-02-15 | `Preview Restore Backup.command` root wrapper |
| `validate_backup.command` | 2026-02-15 | `Validate Latest Backup.command` root wrapper |
| `run_soak_backend.command` | 2026-02-15 | SOAK_TESTING.md, RUNBOOK.md, `Run Backend Soak Test.command` wrapper |
| `migrate_to_canonical_launchagent.command` | 2026-05-06 | Phase A launchd migration tooling |
| `verify_binaries.command` | 2026-04-26 | Two-binary drift workflow |
| `check_performance_budget.py` | 2026-02-15 | CLAUDE.md (Performance benchmarks section) |
| `stt_engine_bench.py` | 2026-05-06 | STT benchmarking workflow |
| `r22_bench.py` | 2026-05-14 | Active bench series (most recent round) |

---

## Section 2 — Potentially Obsolete

Scripts with no references in CLAUDE.md, Makefile, CI, or current docs/, and last edited > 60 days before today (2026-05-26 = cutoff 2026-03-27) or superseded by newer tooling.

### 2a — Pre-Krab-Ear-v2 autonomous cycle scripts (all last commit: 2026-02-15)

These were part of a "self-driving sprint" concept from the initial baseline import. No reference from any live workflow. The underlying Python helpers they call (`roadmap_self_update.py`, `regression_radar.py`, `score_roadmap_sprints.py`) are similarly unreferenced from code.

| Script | Last commit | Reason |
|---|---|---|
| `run_autonomous_cycle.command` | 2026-02-15 | Self-driving sprint runner; no CI/Makefile reference |
| `run_autonomous_hour.command` | 2026-02-15 | Timed variant of above; only doc ref is stale ARCHITECTURE-KRAB-EAR.md |
| `run_daily_driver_validation.command` | 2026-02-15 | Daily driver concept; superseded by CI pipeline |
| `run_sprint_prioritizer.command` | 2026-02-15 | Sprint scoring runner; only doc ref is stale ARCHITECTURE-KRAB-EAR.md |
| `run_roadmap_self_update.command` | 2026-02-15 | Roadmap self-update runner; only doc ref is stale ARCHITECTURE-KRAB-EAR.md |
| `run_regression_radar.command` | 2026-02-15 | Regression radar runner; only doc ref is stale ARCHITECTURE-KRAB-EAR.md |
| `roadmap_self_update.py` | 2026-02-15 | Python driver for above |
| `regression_radar.py` | 2026-02-15 | Python driver for above |
| `score_roadmap_sprints.py` | 2026-02-15 | Python driver for above |

**Action recommended:** Mark for deletion in a dedicated cleanup wave. ARCHITECTURE-KRAB-EAR.md references should be updated.

### 2b — Superseded agent install scripts

| Script | Last commit | Reason |
|---|---|---|
| `install_agent.command` | 2026-04-17 | Script itself is marked **DEPRECATED** in its header comment. Superseded by LaunchServices + `.app` bundle path (PR fixes duplicate Dock icon). Root wrapper `Install Krab Ear Agent.command` still exists but the comment says "should be deleted after cleanup". |

**Action recommended:** Delete in next cleanup wave along with root `Install Krab Ear Agent.command` wrapper.

### 2c — Phase D.10a smoke test (superseded)

| Script | Last commit | Reason |
|---|---|---|
| `smoke_test_d10a.command` | 2026-04-09 | Tests LLM rewriter via socket IPC; superseded by proper pytest IPC E2E tests (Wave 568+). No CI/Makefile reference. |

**Action recommended:** Retire; E2E tests cover the same ground.

### 2d — UX telemetry (no callers in live code)

| Script | Last commit | Reason |
|---|---|---|
| `run_ux_telemetry.command` | 2026-02-15 | Wrapper for `collect_ux_telemetry.py`; referenced only from root-level `.command` wrapper which itself has no documented use |
| `collect_ux_telemetry.py` | 2026-02-15 | Underlying Python script; no references in CI, CLAUDE.md, or live docs |

**Action recommended:** Evaluate whether UX telemetry is actively used; if not, retire both.

### 2e — Agent boundary checker (orphaned concept)

| Script | Last commit | Reason |
|---|---|---|
| `run_agent_boundary_check.command` | 2026-02-15 | Checks agent responsibility boundaries (`codex`/`antigravity`); root wrapper exists but concept is not referenced in any current workflow doc |
| `check_agent_boundaries.py` | 2026-02-15 | Python driver; 9 KB; no CI reference |

**Note:** `Run Agent Boundary Check.command` root wrapper references these. If the multi-agent boundary concept is still used, keep. If Krab Ear is now single-agent context only, retire.

### 2f — History health report (orphaned)

| Script | Last commit | Reason |
|---|---|---|
| `run_history_health.command` | 2026-02-15 | Runs `history_health_report.py`; root `Run History Health.command` wrapper exists; no CI/CLAUDE.md reference |
| `history_health_report.py` | 2026-02-15 | 3.8 KB Python script; checks NDJSON store integrity |

**Note:** Functionality may be superseded by `IntegrityChecker` + `get_diagnostics` IPC. Worth verifying before removal.

---

## Section 3 — Confirmed Duplicates

### 3a — LLM benchmark rounds R19/R20/R21/R22 overlap

| Scripts | Relationship |
|---|---|
| `r19_bench.py`, `r20_bench.py` | Superseded by `r21_bench.py` and `r22_bench.py`. R22 is the most recent. R19/R20 documented in `docs/llm-conversion-guide.md` as historical reference only. |
| `r21_bench.py` | Likely superseded by R22; still referenced in `docs/lm-studio-backend-research-2026-05-13.md` as a "pattern" to copy. |

**Action recommended:** R19 and R20 can be archived/deleted. R21 keep as reference pattern if the docs cite it. R22 is the active script.

**Security note:** `r19_bench.py` and `r20_bench.py` previously contained a hardcoded LM Studio token (`sk-lm-***`). Both were fixed post-Wave 47 (CRIT-1) to read from env/`.env` file. The security-audit-2026-05-12.md flagged `scripts/r*_bench.py` as not in `.gitignore`. This remains open: **add `scripts/r*_bench.py` to `.gitignore`** or at minimum verify no token is present in current committed versions.

### 3b — Performance budget: command wrapper vs Python caller

| Scripts | Relationship |
|---|---|
| `run_performance_budget.command` | Thin wrapper; calls `check_performance_budget.py` |
| `check_performance_budget.py` | Actual implementation (2506 bytes); referenced in CLAUDE.md |

These are not true duplicates (wrapper + impl), but the `.command` wrapper is only referenced from the root-level `Run Performance Budget.command` file and has no CI/Makefile entry. The Python script is the canonical artifact.

### 3c — Voice Gateway start/stop vs Voice Assistant start/stop

| Scripts | Relationship |
|---|---|
| `start_voice_gateway.command`, `stop_voice_gateway.command` | Thin wrappers for starting/stopping the external Voice Gateway process. Last edit: 2026-02-15. Referenced in `README.md`. |
| `start_voice_assistant.command`, `stop_voice_assistant.command`, `healthcheck_voice_assistant.command` | Full Phase 1 ecosystem launchers (LM Studio + VG + backend). Last edit: 2026-04-18. Referenced in `README.md` and `scripts/README_voice_assistant.md`. |

The gateway scripts are subsets of the assistant scripts. They are not exact duplicates but are partially redundant. If the project always starts the full ecosystem, the gateway-only scripts are rarely needed standalone.

---

## Section 4 — Undocumented Scripts

Scripts not mentioned in CLAUDE.md, Makefile, or CI, but also not clearly obsolete:

| Script | Last commit | Notes |
|---|---|---|
| `open_reports.command` | 2026-02-15 | Opens `docs/reports/` in Finder. Referenced from root wrapper only. Trivial utility; useful for developers. |
| `open_control_panel.command` | 2026-02-15 | Delegates to `start_agent.command --show-history`. Referenced from root `Open Krab Ear Panel.command`. Not in CLAUDE.md. |
| `run_performance_budget.command` | 2026-02-15 | Wrapper for `check_performance_budget.py`. See Section 3b. |
| `memory_soak_test.command` | 2026-05-06 | Memory soak test (N IPC transcribe calls). Not in CLAUDE.md. Related to `validate_c1_mps_fix.command` which references it. |
| `validate_c1_mps_fix.command` | 2026-05-06 | A/B comparison for MPS pool fix (C.1). Referenced in `KrabEar/tests/test_validation_script.py`. Has a test that checks it exists — keep. |
| `remove_agent.command` | 2026-04-08 | Uninstall launchd + stop agent. Referenced from `Remove Krab Ear Agent.command` root wrapper. Not in CLAUDE.md. |
| `start_rest_production.command` | 2026-04-16 | Starts REST server in production mode. Only 176 bytes. Referenced from root `start_rest_service.command`. Not in CLAUDE.md. |
| `plot_benchmark_trends.py` | 2026-04-25 | Plot benchmark trends from `.benchmarks/history.jsonl`. Not in CLAUDE.md or CI. Companion to `compare_benchmarks.py`. |
| `scripts/README_voice_assistant.md` | 2026-04-18 | Markdown docs inside `scripts/`. Referenced from root `README.md`. Not a script but lives in scripts/. |

---

## Section 5 — Reference Table (all 68 scripts)

| Script | Last commit | CLAUDE.md | Makefile | CI | Status |
|---|---|---|---|---|---|
| `audit_dead_ipc_handlers.py` | 2026-05-22 | yes | — | — | Active |
| `audit_tcc_permissions.command` | 2026-05-26 | — | — | — | Active (RELEASE_NOTES) |
| `backend_log_digest.py` | 2026-05-26 | — | — | — | Active (docs/wave440) |
| `build_and_deploy.command` | 2026-05-14 | — | yes | — | Active |
| `build_distribution_dmg.command` | 2026-05-14 | yes | — | — | Active |
| `check_agent_boundaries.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `check_performance_budget.py` | 2026-02-15 | yes | — | — | Active |
| `check_two_binary_drift.sh` | 2026-05-26 | — | — | — | Active |
| `cleanup_merged_worktrees.command` | 2026-05-26 | — | — | — | Active |
| `cleanup_worktree_builds.command` | 2026-05-14 | — | yes | — | Active |
| `cleanup_worktree_shadows.command` | 2026-05-06 | yes | — | — | Active |
| `collect_ux_telemetry.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `compare_benchmarks.py` | 2026-04-25 | — | — | yes | Active |
| `create_local_signing_identity.command` | 2026-04-25 | yes | — | — | Active |
| `create_stable_backup.command` | 2026-02-15 | — | — | — | Active (root wrapper) |
| `ensure_agent_running.command` | 2026-05-18 | yes | — | — | Active |
| `healthcheck_voice_assistant.command` | 2026-04-18 | — | — | — | Active (README) |
| `history_health_report.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `install_agent.command` | 2026-04-17 | — | — | — | **DEPRECATED** (see header) |
| `install_agent_launchagent.command` | 2026-05-14 | yes | — | — | Active |
| `install_backend_launchagent.command` | 2026-04-08 | yes | — | — | Active |
| `install_gigaam_venv.command` | 2026-04-26 | — | — | — | Active (USER_MANUAL) |
| `install_rest_launchagent.command` | 2026-05-20 | — | — | — | Active (root wrapper) |
| `kill_dup_gigaam.command` | 2026-05-26 | — | — | — | Active (CHANGELOG, RELEASE_NOTES) |
| `memory_baseline.py` | 2026-05-14 | yes | — | — | Active |
| `memory_soak_test.command` | 2026-05-06 | — | — | — | Undocumented but useful |
| `migrate_to_canonical_launchagent.command` | 2026-05-06 | — | — | — | Active (Phase A) |
| `observe_production.command` | 2026-05-26 | — | — | — | Active (marathon docs) |
| `open_control_panel.command` | 2026-02-15 | — | — | — | Undocumented |
| `open_reports.command` | 2026-02-15 | — | — | — | Undocumented |
| `plot_benchmark_trends.py` | 2026-04-25 | — | — | — | Undocumented |
| `prefetch_whisper_models.command` | 2026-05-26 | — | — | — | Active (USER_MANUAL) |
| `profile_gigaam_worker.command` | 2026-05-06 | yes | — | — | Active |
| `r19_bench.py` | 2026-05-14 | — | — | — | Superseded by R22; **security note** |
| `r20_bench.py` | 2026-05-14 | — | — | — | Superseded by R22; **security note** |
| `r21_bench.py` | 2026-05-14 | — | — | — | Partially superseded by R22 |
| `r22_bench.py` | 2026-05-14 | — | — | — | Active (current bench round) |
| `README_voice_assistant.md` | 2026-04-18 | — | — | — | Active (README reference) |
| `regression_radar.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `remove_agent.command` | 2026-04-08 | — | — | — | Undocumented (root wrapper exists) |
| `repair_permissions.command` | 2026-04-25 | yes | — | — | Active |
| `restore_backup_preview.command` | 2026-02-15 | — | — | — | Active (root wrapper) |
| `roadmap_self_update.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_agent_boundary_check.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_autonomous_cycle.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_autonomous_hour.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_daily_driver_validation.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_history_health.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_performance_budget.command` | 2026-02-15 | — | — | — | Undocumented wrapper |
| `run_regression_radar.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_release_checklist.command` | 2026-04-19 | yes | — | — | Active |
| `run_roadmap_self_update.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_smoke_release.command` | 2026-02-15 | — | — | — | Active (RELEASE_CHECKLIST) |
| `run_soak_backend.command` | 2026-02-15 | — | — | — | Active (SOAK_TESTING, RUNBOOK) |
| `run_sprint_prioritizer.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `run_ux_telemetry.command` | 2026-02-15 | — | — | — | Potentially obsolete |
| `score_roadmap_sprints.py` | 2026-02-15 | — | — | — | Potentially obsolete |
| `sentry_create_release.py` | 2026-04-25 | — | — | — | Active (Sentry release workflow) |
| `smoke_test_d10a.command` | 2026-04-09 | — | — | — | Potentially obsolete (superseded by E2E) |
| `start_agent.command` | 2026-05-06 | — | — | — | Active (root wrapper chain) |
| `start_rest_production.command` | 2026-04-16 | — | — | — | Undocumented |
| `start_voice_assistant.command` | 2026-04-18 | — | — | — | Active (README) |
| `start_voice_gateway.command` | 2026-02-15 | — | — | — | Active (README), partial dup |
| `stop_voice_assistant.command` | 2026-04-18 | — | — | — | Active (README) |
| `stop_voice_gateway.command` | 2026-02-15 | — | — | — | Active (README), partial dup |
| `stt_engine_bench.py` | 2026-05-06 | — | — | — | Active (STT bench) |
| `update_agent.command` | 2026-04-25 | — | — | — | Active (root wrapper) |
| `validate_backup.command` | 2026-02-15 | — | — | — | Active (root wrapper) |
| `validate_c1_mps_fix.command` | 2026-05-06 | — | — | — | Active (test checks existence) |
| `verify_binaries.command` | 2026-04-26 | — | — | — | Active (two-binary drift) |
| `verify_claude_md.py` | 2026-05-16 | yes | — | yes | Active |

---

## Section 6 — Recommended Actions (priority order)

### High priority
1. **Delete `install_agent.command`** — self-marked DEPRECATED in header. Root wrapper `Install Krab Ear Agent.command` should also be retired.
2. **Add `scripts/r19_bench.py` and `scripts/r20_bench.py` to `.gitignore`** (or delete them). Security audit flagged these; they are superseded by R22. Ensure no token is in the current commit.

### Medium priority
3. **Retire autonomous-cycle cluster** (9 scripts: `run_autonomous_cycle`, `run_autonomous_hour`, `run_daily_driver_validation`, `run_sprint_prioritizer`, `run_roadmap_self_update`, `run_regression_radar`, `roadmap_self_update.py`, `regression_radar.py`, `score_roadmap_sprints.py`). Also update `docs/ARCHITECTURE-KRAB-EAR.md` to remove stale references.
4. **Evaluate `run_ux_telemetry.command` + `collect_ux_telemetry.py`** — confirm no active use, then retire.
5. **Evaluate `check_agent_boundaries.py` + `run_agent_boundary_check.command`** — if multi-agent boundary enforcement is no longer active, retire both plus root wrapper.
6. **Evaluate `history_health_report.py` + `run_history_health.command`** — if superseded by `IntegrityChecker` IPC, retire.

### Low priority
7. **Add CLAUDE.md entries** for undocumented but useful scripts: `audit_tcc_permissions.command`, `kill_dup_gigaam.command`, `observe_production.command`, `validate_c1_mps_fix.command`.
8. **Retire `smoke_test_d10a.command`** — functionality covered by Wave 568+ E2E IPC tests.
9. **Consider retiring `r21_bench.py`** once R22 is the established baseline; update `docs/lm-studio-backend-research-2026-05-13.md` to reference R22 instead.

---

*Audit performed by Wave 810. No scripts were modified or deleted — audit only.*
