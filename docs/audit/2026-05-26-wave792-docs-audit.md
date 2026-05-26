# Docs Audit — Wave 792 (2026-05-26)

**Scope:** All `.md` files under `docs/` (excluding `docs/archive/` which does not yet exist)  
**Method:** `git log -1 --date=short`, line count, cross-reference grep against CLAUDE.md and sibling docs  
**Total files audited:** 142 markdown files  
**Counts:** 30 stale candidates · 28 superseded candidates · 84 active

---

## Summary table

| Category | Count | Action |
|----------|-------|--------|
| Active (still referenced / current) | 84 | Keep as-is |
| Superseded (newer doc covers same topic) | 28 | Archive to `docs/archive/` |
| Stale (>30 days, wave already closed, no references) | 30 | Archive to `docs/archive/` |

---

## Active Doc Map

These files are either referenced from CLAUDE.md, recent audit files, or cover ongoing concerns:

### Top-level docs — active
| File | Last commit | Why active |
|------|-------------|-----------|
| `docs/ARCHITECTURE-KRAB-EAR.md` | 2026-05-26 | Source of truth (Wave 764 audit note, replaces `ARCHITECTURE.md`) |
| `docs/ARCHITECTURE.md` | 2026-05-18 | Still referenced by `STT_ROUTER.md`, `GUI_DESIGN_BRIEF.md`, `ARCHITECTURE-KRAB-EAR.md` itself |
| `docs/DEPLOY_V2.0.5.md` | 2026-05-26 | Active deploy plan for v2.0.5 |
| `docs/DEV_CODESIGN.md` | 2026-04-24 | Referenced by CLAUDE.md ("See `docs/DEV_CODESIGN.md`") |
| `docs/DISTRIBUTION.md` | 2026-04-24 | Referenced by CLAUDE.md |
| `docs/IPC_API_REFERENCE.md` | 2026-05-26 | Primary IPC reference; referenced by CLAUDE.md, many audit docs |
| `docs/PERFORMANCE_BUDGET.md` | 2026-05-26 | Active CI gate |
| `docs/RELEASE_NOTES_v2.0.5.md` | 2026-05-26 | Current release |
| `docs/TROUBLESHOOTING_PERMISSIONS.md` | 2026-04-21 | Referenced implicitly (TCC section in CLAUDE.md) |
| `docs/USER_ACTION_CHECKLIST.md` | 2026-05-26 | Active (Wave 769); referenced from DEPLOY_V2.0.5, snapshots |
| `docs/USER_MANUAL.md` | 2026-05-24 | Canonical user guide in Russian; referenced by CLAUDE.md |
| `docs/WAVE_716_CRON_RETIREMENT.md` | 2026-05-26 | Active — cron retires after v2.0.5 7-day observation |
| `docs/ROADMAP.md` | 2026-02-15 | Still-active roadmap (old date but referenced from ARCHITECTURE docs) |
| `docs/STT_ROUTER.md` | 2026-04-25 | Current STT routing reference |
| `docs/STT_RU_GIGAAM.md` | 2026-04-25 | GigaAM setup/ops reference |
| `docs/STT_WHISPER_RU.md` | 2026-04-25 | Whisper RU reference |
| `docs/BENCHMARK_M4_MAX.md` | 2026-05-14 | Perf baseline |
| `docs/macos-sequoia-26-known-issues.md` | 2026-05-24 | Active — Sequoia 26 ongoing |
| `docs/cleanup-worktrees-usage.md` | 2026-05-24 | Operations guide for worktree cleanup |
| `docs/v2.0.4-ship-checklist.md` | 2026-05-25 | Still relevant as historical diff base for v2.0.5 |
| `docs/WAVE_716_CRON_RETIREMENT.md` | 2026-05-26 | Active criteria doc |

### docs/audit/ — active (recent, Wave 551+)
| File | Last commit | Notes |
|------|-------------|-------|
| `docs/audit/2026-05-25-wave551-log-audit.md` | 2026-05-24 | Log audit baseline |
| `docs/audit/2026-05-26-wave575-ipc-throttle-candidates.md` | 2026-05-24 | IPC throttle candidates |
| `docs/audit/2026-05-26-wave635-settings-orphans.md` | 2026-05-24 | Settings orphan audit |
| `docs/audit/2026-05-26-wave655-breadcrumb.md` | 2026-05-24 | Breadcrumb coverage |
| `docs/audit/2026-05-26-wave657-ipc-ref-drift.md` | 2026-05-24 | IPC reference drift (58% drift noted in CLAUDE.md) |
| `docs/audit/2026-05-26-wave689-probe-urls.md` | 2026-05-24 | Probe URL audit |
| `docs/audit/2026-05-26-wave701-sentry.md` | 2026-05-25 | Sentry status |
| `docs/audit/2026-05-26-wave730-test-718-analysis.md` | 2026-05-26 | Test 718 blocker analysis |
| `docs/audit/2026-05-26-wave763-ipc-handler-complexity.md` | 2026-05-26 | Handler complexity |
| `docs/audit/2026-05-26-wave765-error-codes-audit.md` | 2026-05-26 | Error codes current state |
| `docs/audit/2026-05-26-wave770-requirements-audit.md` | 2026-05-26 | Requirements audit |
| `docs/audit/2026-05-28-wave718-test-full-workflow-blocker.md` | 2026-05-25 | Blocker doc (unresolved) |

### docs/superpowers/ — active
| File | Last commit | Notes |
|------|-------------|-------|
| `docs/superpowers/snapshots/2026-05-26-marathon-final-wrap.md` | 2026-05-26 | Current marathon state |
| `docs/superpowers/snapshots/2026-05-26-marathon-batch-2-snapshot.md` | 2026-05-26 | Batch 2 state |
| `docs/superpowers/snapshots/2026-05-26-wave703-milestone-700.md` | 2026-05-25 | Wave 700 milestone |
| `docs/superpowers/specs/2026-05-26-wave638-extraction-proposals.md` | 2026-05-24 | Active extraction proposals |
| `docs/superpowers/specs/2026-05-26-wave653-extraction.md` | 2026-05-26 | Current extraction candidates |
| `docs/superpowers/specs/2026-05-04-phase-c-roadmap-refinement-design.md` | 2026-05-04 | Referenced from CLAUDE.md (BackendSupervisor spec link) |
| `docs/superpowers/specs/2026-05-05-phase-d-roadmap-design.md` | 2026-05-05 | Phase D roadmap |
| `docs/superpowers/specs/2026-05-12-phase-c-mlx-flock-design.md` | 2026-05-14 | MLX flock design; referenced from lm-studio-backend-research |
| `docs/superpowers/specs/2026-05-12-va-phase2-design.md` | 2026-05-14 | VA Phase 2 spec |

### Ongoing investigations — active
| File | Last commit | Notes |
|------|-------------|-------|
| `docs/wave440-log-digest-enhancements.md` | 2026-05-24 | Active log digest work |
| `docs/wave521-sequoia-integration-tests.md` | 2026-05-24 | Sequoia tests ship info |
| `docs/phase-b-wave82-candidates.md` | 2026-05-24 | Wave 82 error codes — fully wired but authoritative record |
| `docs/service-py-audit-v3-2026-05-22.md` | 2026-05-25 | Current service.py audit (v3 is latest) |
| `docs/macos-sequoia-26-known-issues.md` | 2026-05-24 | Active reference |

---

## Superseded Doc Chains

These files have been explicitly replaced by a newer document covering the same topic.
Recommend archiving to `docs/archive/`.

### IPC API documentation chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/API.md` (2026-02-15, 71 lines) | `docs/IPC_API.md` | Pre-dates IPC_API.md; same topic, older schema |
| `docs/IPC_API.md` (2026-04-25, 4341 lines) | `docs/IPC_API_REFERENCE.md` | IPC_API_REFERENCE is the current canonical (CLAUDE.md says IPC_API.md has 58% drift per W657 audit; W657 uses IPC_API_REFERENCE as ground truth) |
| `docs/IPC_API_REFERENCE_BACKFILL_2026-05.md` (2026-05-14, 597 lines) | `docs/IPC_API_REFERENCE.md` | Backfill doc merged into main reference; BACKFILL date is older |
| `docs/REST_API_REFERENCE.md` (2026-04-16, 192 lines) | In-code Swagger / `docs/IPC_API_REFERENCE.md` | No references found; covered by IPC reference |

### Architecture chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/ARCHITECTURE.md` (2026-05-18) | `docs/ARCHITECTURE-KRAB-EAR.md` | ARCH-KRAB-EAR has "Last audit: 2026-05-26 (Wave 764)" header and is newer; however ARCHITECTURE.md still has references — partial supersession, downgrade to "stale" |

### User docs chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/USER_GUIDE.md` (2026-04-16, 330 lines) | `docs/USER_MANUAL.md` | USER_MANUAL is the canonical guide (referenced in CLAUDE.md); USER_GUIDE covers the same setup content in older form |

### service.py audit chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/service-py-audit-2026-05.md` (Wave 161, LOC 5765) | `docs/service-py-audit-v3-2026-05-22.md` (LOC 5476) | v3 is explicitly titled "v3" and is the latest; v1 is superseded |

### Phase B error code candidate chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/phase-b-wave77-candidates.md` (2026-05-19) | `docs/phase-b-wave82-candidates.md` | Wave 77 candidates were folded into Wave 78 and Wave 82 |
| `docs/phase-b-wave78-candidates.md` (2026-05-19) | `docs/phase-b-wave82-candidates.md` | Wave 78 candidates resolved by Wave 82 (CLAUDE.md: "ERROR_REGISTRY 51→57") |

### Release / ship chain
| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/RELEASE_NOTES_2026-04-12.md` | `docs/RELEASE_NOTES_v2.0.5.md` | Old release notes for April; current is v2.0.5 |
| `docs/PHASE_COMPLETION_2026-04-24.md` | `docs/RELEASE_NOTES_v2.0.5.md` | Phase completion snapshot, no longer current |

### Session mega-snapshot chain (each supersedes the previous)
The session snapshots form a linear chain; intermediate ones are superseded by the final:

| Superseded | Supersedes | Reason |
|-----------|-----------|--------|
| `docs/session_2026-05-19_134_waves.md` | `docs/krab-ear-mega-marathon-2026-05-12-to-22.md` | Marathon wrap is the definitive cumulative |
| `docs/session_2026-05-20_160_waves_final.md` | `docs/krab-ear-mega-marathon-2026-05-12-to-22.md` | Same |
| `docs/session_2026-05-21_wave274_v203_ship.md` | `docs/krab-ear-mega-marathon-2026-05-12-to-22.md` + CLAUDE.md | CLAUDE.md has this data in memory |
| `docs/session_2026-05-22_full_wrap.md` | `docs/krab-ear-mega-marathon-2026-05-12-to-22.md` | Full wrap is subsection of mega-marathon |
| `docs/session_2026-05-22_wave428_wrap.md` | `docs/krab-ear-mega-marathon-2026-05-12-to-22.md` | Same |

### Wave snapshot chain (all intermediate snapshots superseded by final)
| Superseded | By | Reason |
|-----------|-----|--------|
| `docs/wave351-batch8-session-snapshot.md` | `docs/wave403-megafinal-snapshot.md` | Intermediate snapshot |
| `docs/wave369-batch9-session-snapshot.md` | `docs/wave403-megafinal-snapshot.md` | Intermediate snapshot |
| `docs/wave391-batch9cont-session-snapshot.md` | `docs/wave403-megafinal-snapshot.md` | Intermediate snapshot |
| `docs/wave446-final-session-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave466-evening-session-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave480-evening-session-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave485-ultimate-session-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave495-phase-b-82-complete-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave500-milestone-session-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |
| `docs/wave510-phase-b-82-full-complete-snapshot.md` | `docs/wave516-v204-shipped-session-snapshot.md` | Intermediate snapshot |

---

## Stale Candidates

Files >30 days old (relative to 2026-05-26) OR mentioning closed waves with no external references.
Recommend archiving to `docs/archive/`.

### Ancient root docs (2026-02-15, ~100 days old, no current references)
| File | Last commit | Reason stale |
|------|-------------|--------------|
| `docs/AI_WORKFLOW.md` | 2026-02-15 | Pre-dates current agent model; CLAUDE.md is the live workflow doc |
| `docs/AUTONOMOUS_EXECUTION_PLAN.md` | 2026-02-15 | Pre-dates current architecture; no references |
| `docs/BACKUP_AND_GITHUB.md` | 2026-02-15 | Backup procedures superseded by RELEASE_CHECKLIST_V2 + DEPLOY_V2.0.5 |
| `docs/CLEANUP_REPORT.md` | 2026-02-15 | Repo cleanup from February; work completed |
| `docs/COLLABORATION_SPLIT.md` | 2026-02-15 | Codex/Antigravity split was early-project; no longer relevant |
| `docs/HANDOFF.md` | 2026-02-15 | "Krab Voice v2 handoff" — pre-Krab Ear project era |
| `docs/MASTER_PLAN.md` | 2026-02-15 | Early-stage master plan; superseded by ROADMAP.md |
| `docs/RUNBOOK.md` | 2026-02-15 | 43-line runbook; superseded by USER_MANUAL.md + DEPLOY_V2.0.5 |
| `docs/SOAK_TESTING.md` | 2026-02-15 | 31 lines; methodology not referenced anywhere |

### April 2026 docs — wave work closed
| File | Last commit | Reason stale |
|------|-------------|--------------|
| `docs/AUTONOMOUS_EXECUTION_PLAN.md` | 2026-02-15 | Covered above |
| `docs/GUI_DESIGN_BRIEF.md` | 2026-04-16 | Gemini key revoked; brief unused; design moved to Claude |
| `docs/PHASE4_DETERMINISTIC_PIPELINE.md` | 2026-04-12 | Phase 4 implemented; "Status: Proposal" — closed |
| `docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md` | 2026-04-16 | Same — implementation complete, doc is design-stage only |
| `docs/PHASE_1_VOICE_ASSISTANT_SETUP.md` | 2026-04-18 | Phase 1 CLOSED 2026-04-18 (CLAUDE.md: "Phase 1 CLOSED") |
| `docs/PHASE_4_ADAPTER_COMPARISON.md` | 2026-04-18 | Phase 4 adapters shipped; comparison doc is historical |
| `docs/PLAN_TRACK_D_KRAB_EAR.md` | 2026-04-08 | Track D (REST service) completed months ago |
| `docs/README-CONSOLIDATED.md` | 2026-04-19 | 32-line index of old consolidation; stale |
| `docs/RELEASE_CHECKLIST_V2.md` | 2026-04-16 | Superseded by `scripts/run_release_checklist.command` |

### May 2026 investigation docs — investigations closed
| File | Last commit | Reason stale |
|------|-------------|--------------|
| `docs/backend-j-investigation-2026-05-19.md` | 2026-05-22 | BACKEND-J (rewriter.timeout) resolved (CLAUDE.md: "Sentry: 0 backend + 0 agent") |
| `docs/drift-report-2026-05-12.md` | 2026-05-14 | Wave 42 drift report; current drift tracked in `audit/2026-05-26-wave657-ipc-ref-drift.md` |
| `docs/error-bus-self-fail-investigation.md` | 2026-05-20 | Root cause confirmed and fixed (PR #376, 2026-05-05) |
| `docs/gui-redesign-brief-2026-05-12.md` | 2026-05-14 | Gemini key revoked; brief never executed |
| `docs/llm-conversion-guide.md` | 2026-05-14 | R21+ bench guide; LLM R-series benches complete |
| `docs/lm-studio-backend-research-2026-05-13.md` | 2026-05-14 | Investigation closed (Wave 48 CPU-bench failed; LM Studio Stream(gpu,N) reclassified) |
| `docs/main-swift-extensions-audit-2026-05.md` | 2026-05-21 | Wave 177 audit completed; no open follow-ups |
| `docs/memory-baseline-comparison-wave195.md` | 2026-05-21 | Wave 195 analysis; Wave 63 fix validated (CLAUDE.md: "Wave 63 memory leak fix") |
| `docs/perf-regression-analysis-wave196.md` | 2026-05-20 | Wave 196 perf gates; current state in `PERFORMANCE_BUDGET.md` |
| `docs/security-audit-2026-05-12.md` | 2026-05-14 | Wave 42 security audit; fixes shipped (CRIT-1 fixed, CLAUDE.md confirms) |
| `docs/sentry-sweep-2026-05-20.md` | 2026-05-21 | Sweep from 2026-05-20; current state: 0 backend + 0 agent unresolved |
| `docs/wave56-gguf-cpu-bench-2026-05-13.md` | 2026-05-14 | FAILED bench; closed (CLAUDE.md: "GGUF CPU research closed") |
| `docs/wave301-agent-recovery-investigation.md` | 2026-05-21 | Root cause found + fixed (Wave 316 VPN mitigation) |
| `docs/wave316-reboot-vpn-mitigation.md` | 2026-05-21 | VPN plist mitigation documented; CLAUDE.md has summary |
| `docs/wave321-v203-sentry-verify.md` | 2026-05-21 | v2.0.3 verified; current is v2.0.5 |
| `docs/wave336-sentry-dist-verify.md` | 2026-05-21 | dist:2.0.3 verification; current is v2.0.5 |
| `docs/wave358-gigaam-padding-investigation.md` | 2026-05-22 | Bug fixed (CLAUDE.md: "GigaAM padding fixes" in v2.0.4) |
| `docs/wave403-megafinal-snapshot.md` | 2026-05-24 | Superseded by `wave516-v204-shipped-session-snapshot.md` + `krab-ear-mega-marathon` |

### Early April docs — phases closed
| File | Last commit | Reason stale |
|------|-------------|--------------|
| `docs/audits/2026-04-18-imports-audit.md` | 2026-04-19 | One-time imports audit; work done |
| `docs/superpowers/plans/2026-04-06-e4-voice-ear-interop.md` | 2026-04-06 | E4 COMPLETE (CLAUDE.md: "E4 complete") |
| `docs/superpowers/plans/2026-04-09-d10a-lm-studio-integration.md` | 2026-04-09 | D10a complete (CLAUDE.md: "D.10a MEGA session") |
| `docs/superpowers/plans/2026-04-17-voice-assistant-mode.md` | 2026-04-18 | Phase 1 CLOSED 2026-04-18 |
| `docs/superpowers/designs/2026-04-18-phase-2.1-vg-translation-stream.md` | 2026-04-18 | Phase 2 implemented (Live Translation live) |
| `docs/superpowers/designs/2026-04-18-phase-2.2-krab-ear-live-translation-ui.md` | 2026-04-19 | Same |
| `docs/superpowers/designs/2026-04-18-phase-2.3-translation-contracts.md` | 2026-04-19 | Same |
| `docs/superpowers/designs/2026-04-18-phase-2.4-e2e-tests.md` | 2026-04-19 | Same |
| `docs/superpowers/specs/2026-04-06-e4-voice-ear-interop-design.md` | 2026-04-06 | E4 COMPLETE |
| `docs/superpowers/specs/2026-04-09-d10a-lm-studio-integration-design.md` | 2026-04-09 | D10a complete |
| `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md` | 2026-04-17 | Phase 1 CLOSED (referenced in CLAUDE.md as historical; keep 1 copy in archive) |
| `docs/superpowers/specs/2026-04-18-phase-2-live-translation-design.md` | 2026-04-18 | Phase 2 live |
| `docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md` | 2026-04-18 | Phase 3 live (Call Automation shipped) |
| `docs/superpowers/decisions/2026-04-18-phase-3-adr.md` | 2026-04-19 | ADR for Phase 3; decision locked |

### Docs covering topics absorbed into CLAUDE.md
| File | Last commit | Reason stale |
|------|-------------|--------------|
| `docs/dependencies-audit-2026-05-20.md` | 2026-05-21 | Wave 275 one-time audit; no open items |

---

## Docs with Naming Anomalies

These are active docs whose names may be confusing because the wave number in the filename
predates the actual content (filed on a branch that later landed):

| File | Actual date (git) | Filename date | Notes |
|------|-------------------|---------------|-------|
| `docs/audit/2026-05-26-wave575-ipc-throttle-candidates.md` | 2026-05-24 | wave575 | OK — pre-landed |
| `docs/audit/2026-05-26-wave635-settings-orphans.md` | 2026-05-24 | wave635 | OK |
| `docs/audit/2026-05-28-wave718-test-full-workflow-blocker.md` | 2026-05-25 | 2026-05-28 | Future-dated filename; content is valid |

---

## Recommended Archive Targets (58 files)

All files below should move to `docs/archive/`. They are historically valuable but
should not clutter the active docs namespace.

```
# === Ancient (Feb 2026) ===
docs/AI_WORKFLOW.md
docs/AUTONOMOUS_EXECUTION_PLAN.md
docs/BACKUP_AND_GITHUB.md
docs/CLEANUP_REPORT.md
docs/COLLABORATION_SPLIT.md
docs/HANDOFF.md
docs/MASTER_PLAN.md
docs/RUNBOOK.md
docs/SOAK_TESTING.md

# === Superseded IPC docs ===
docs/API.md
docs/IPC_API.md
docs/IPC_API_REFERENCE_BACKFILL_2026-05.md
docs/REST_API_REFERENCE.md

# === Superseded user docs ===
docs/USER_GUIDE.md

# === Closed phase docs (April 2026) ===
docs/GUI_DESIGN_BRIEF.md
docs/PHASE4_DETERMINISTIC_PIPELINE.md
docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md
docs/PHASE_1_VOICE_ASSISTANT_SETUP.md
docs/PHASE_4_ADAPTER_COMPARISON.md
docs/PLAN_TRACK_D_KRAB_EAR.md
docs/README-CONSOLIDATED.md
docs/RELEASE_CHECKLIST_V2.md
docs/RELEASE_NOTES_2026-04-12.md
docs/PHASE_COMPLETION_2026-04-24.md
docs/audits/2026-04-18-imports-audit.md

# === Superseded service.py audit ===
docs/service-py-audit-2026-05.md

# === Superseded error code candidate docs ===
docs/phase-b-wave77-candidates.md
docs/phase-b-wave78-candidates.md

# === Superseded session snapshots ===
docs/session_2026-05-19_134_waves.md
docs/session_2026-05-20_160_waves_final.md
docs/session_2026-05-21_wave274_v203_ship.md
docs/session_2026-05-22_full_wrap.md
docs/session_2026-05-22_wave428_wrap.md

# === Superseded wave snapshots ===
docs/wave351-batch8-session-snapshot.md
docs/wave369-batch9-session-snapshot.md
docs/wave391-batch9cont-session-snapshot.md
docs/wave403-megafinal-snapshot.md
docs/wave446-final-session-snapshot.md
docs/wave466-evening-session-snapshot.md
docs/wave480-evening-session-snapshot.md
docs/wave485-ultimate-session-snapshot.md
docs/wave495-phase-b-82-complete-snapshot.md
docs/wave500-milestone-session-snapshot.md
docs/wave510-phase-b-82-full-complete-snapshot.md

# === Stale investigations (closed) ===
docs/backend-j-investigation-2026-05-19.md
docs/drift-report-2026-05-12.md
docs/error-bus-self-fail-investigation.md
docs/gui-redesign-brief-2026-05-12.md
docs/llm-conversion-guide.md
docs/lm-studio-backend-research-2026-05-13.md
docs/main-swift-extensions-audit-2026-05.md
docs/memory-baseline-comparison-wave195.md
docs/perf-regression-analysis-wave196.md
docs/security-audit-2026-05-12.md
docs/sentry-sweep-2026-05-20.md
docs/wave56-gguf-cpu-bench-2026-05-13.md
docs/wave301-agent-recovery-investigation.md
docs/wave316-reboot-vpn-mitigation.md
docs/wave321-v203-sentry-verify.md
docs/wave336-sentry-dist-verify.md
docs/wave358-gigaam-padding-investigation.md
docs/dependencies-audit-2026-05-20.md

# === Closed superpowers docs ===
docs/superpowers/plans/2026-04-06-e4-voice-ear-interop.md
docs/superpowers/plans/2026-04-09-d10a-lm-studio-integration.md
docs/superpowers/plans/2026-04-17-voice-assistant-mode.md
docs/superpowers/designs/2026-04-18-phase-2.1-vg-translation-stream.md
docs/superpowers/designs/2026-04-18-phase-2.2-krab-ear-live-translation-ui.md
docs/superpowers/designs/2026-04-18-phase-2.3-translation-contracts.md
docs/superpowers/designs/2026-04-18-phase-2.4-e2e-tests.md
docs/superpowers/specs/2026-04-06-e4-voice-ear-interop-design.md
docs/superpowers/specs/2026-04-09-d10a-lm-studio-integration-design.md
docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md
docs/superpowers/specs/2026-04-18-phase-2-live-translation-design.md
docs/superpowers/specs/2026-04-18-phase-3-call-automation-design.md
docs/superpowers/decisions/2026-04-18-phase-3-adr.md
```

---

## Notes

1. **Do not delete** any of the above — archive only. Several stale docs contain
   implementation decisions that may be relevant for regression analysis.
2. **`docs/ARCHITECTURE.md`** is partially superseded by `ARCHITECTURE-KRAB-EAR.md`
   but still has cross-references; leave active until all references are updated.
3. **`docs/krab-ear-mega-marathon-2026-05-12-to-22.md`** is the definitive historical
   record of the May marathon; it supersedes all intermediate session snapshots but
   should itself remain in active docs (not archived) as a key reference.
4. **`docs/wave516-v204-shipped-session-snapshot.md`** is the final wave snapshot
   for the v2.0.4 era; keep active as release anchor.
5. The `docs/research/` subtree (13 files, all 2026-04-18) is entirely Phase 1
   research. Considered low-priority stale but kept out of archive candidates
   because the research index (`docs/research/INDEX.md`) may be consulted for
   STT adapter context. Recommend separate research/ archive pass.
