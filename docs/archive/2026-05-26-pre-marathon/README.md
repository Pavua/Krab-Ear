# Archive: 2026-05-26-pre-marathon

**Archived by:** Wave 803 (2026-05-26)  
**Reason:** W792 docs audit identified 58 stale/superseded files. This batch archives the 25 clearest candidates.  
**All files are preserved verbatim — no content was deleted.**

---

## Files archived (25 total)

### Ancient root docs (Feb 2026, ~100 days old, no current references)

| File | Original path | Why archived |
|------|--------------|--------------|
| `AI_WORKFLOW.md` | `docs/AI_WORKFLOW.md` | Pre-dates current agent model; CLAUDE.md is the live workflow doc |
| `AUTONOMOUS_EXECUTION_PLAN.md` | `docs/AUTONOMOUS_EXECUTION_PLAN.md` | Pre-dates current architecture; no active references |
| `BACKUP_AND_GITHUB.md` | `docs/BACKUP_AND_GITHUB.md` | Backup procedures superseded by RELEASE_CHECKLIST_V2 + DEPLOY_V2.0.5 |
| `CLEANUP_REPORT.md` | `docs/CLEANUP_REPORT.md` | One-time Feb 2026 repo cleanup; work completed |
| `COLLABORATION_SPLIT.md` | `docs/COLLABORATION_SPLIT.md` | Codex/Antigravity split was early-project; no longer relevant |
| `HANDOFF.md` | `docs/HANDOFF.md` | "Krab Voice v2 handoff" — pre-Krab Ear project era |
| `MASTER_PLAN.md` | `docs/MASTER_PLAN.md` | Early-stage master plan; superseded by ROADMAP.md |
| `RUNBOOK.md` | `docs/RUNBOOK.md` | 43-line runbook; superseded by USER_MANUAL.md + DEPLOY_V2.0.5 |
| `SOAK_TESTING.md` | `docs/SOAK_TESTING.md` | 31-line methodology; not referenced in active docs |

### Superseded IPC API documentation chain

| File | Original path | Why archived |
|------|--------------|--------------|
| `API.md` | `docs/API.md` | Pre-dates IPC_API.md; same topic, older schema |
| `IPC_API.md` | `docs/IPC_API.md` | Superseded by IPC_API_REFERENCE.md (W657 audit: 58% drift; REFERENCE is ground truth) |
| `IPC_API_REFERENCE_BACKFILL_2026-05.md` | `docs/IPC_API_REFERENCE_BACKFILL_2026-05.md` | Backfill merged into main reference; older dated |
| `REST_API_REFERENCE.md` | `docs/REST_API_REFERENCE.md` | No references found; covered by IPC_API_REFERENCE.md |

### Superseded user docs

| File | Original path | Why archived |
|------|--------------|--------------|
| `USER_GUIDE.md` | `docs/USER_GUIDE.md` | Superseded by USER_MANUAL.md (canonical guide, referenced in CLAUDE.md) |

### Closed phase docs (April 2026 — phases shipped)

| File | Original path | Why archived |
|------|--------------|--------------|
| `GUI_DESIGN_BRIEF.md` | `docs/GUI_DESIGN_BRIEF.md` | Gemini key revoked; brief never executed; design moved to Claude |
| `PHASE4_DETERMINISTIC_PIPELINE.md` | `docs/PHASE4_DETERMINISTIC_PIPELINE.md` | Phase 4 implemented; status was "Proposal" — closed |
| `PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md` | `docs/PHASE4_PIPELINE_IMPLEMENTATION_PLAN.md` | Implementation complete; design-stage doc only |
| `PHASE_1_VOICE_ASSISTANT_SETUP.md` | `docs/PHASE_1_VOICE_ASSISTANT_SETUP.md` | Phase 1 CLOSED 2026-04-18 (see CLAUDE.md) |
| `PHASE_4_ADAPTER_COMPARISON.md` | `docs/PHASE_4_ADAPTER_COMPARISON.md` | Phase 4 adapters shipped; comparison doc is historical |

### Stale May 2026 investigation docs (investigations closed)

| File | Original path | Why archived |
|------|--------------|--------------|
| `backend-j-investigation-2026-05-19.md` | `docs/backend-j-investigation-2026-05-19.md` | BACKEND-J (rewriter.timeout) resolved (Sentry: 0 backend unresolved) |
| `error-bus-self-fail-investigation.md` | `docs/error-bus-self-fail-investigation.md` | Root cause confirmed and fixed (PR #376, 2026-05-05) |
| `gui-redesign-brief-2026-05-12.md` | `docs/gui-redesign-brief-2026-05-12.md` | Gemini key revoked; brief never executed |
| `security-audit-2026-05-12.md` | `docs/security-audit-2026-05-12.md` | Wave 42 security audit; all fixes shipped (CRIT-1 fixed) |
| `wave358-gigaam-padding-investigation.md` | `docs/wave358-gigaam-padding-investigation.md` | Bug fixed (GigaAM padding fixes in v2.0.4) |

### One-time audit docs

| File | Original path | Why archived |
|------|--------------|--------------|
| `2026-04-18-imports-audit.md` | `docs/audits/2026-04-18-imports-audit.md` | One-time April 2026 imports audit; work done |

---

## References updated (4 files)

The following files had links to now-archived docs and were updated to point to the archive path:

| File | Old reference | Updated to |
|------|--------------|------------|
| `docs/ARCHITECTURE.md` | `docs/RUNBOOK.md` | `docs/archive/2026-05-26-pre-marathon/RUNBOOK.md` |
| `docs/STT_ROUTER.md` | `docs/PHASE_4_ADAPTER_COMPARISON.md` | `docs/archive/2026-05-26-pre-marathon/PHASE_4_ADAPTER_COMPARISON.md` |
| `docs/ROADMAP.md` | `docs/API.md`, `docs/RUNBOOK.md` | annotated as archived |
| `docs/wave521-sequoia-integration-tests.md` | `docs/SOAK_TESTING.md` | `docs/archive/2026-05-26-pre-marathon/SOAK_TESTING.md` |

---

## Notes

- CLAUDE.md had zero references to any of the archived files — no changes required there.
- The W792 audit identified 58 total candidates; this PR covers the 25 clearest (most stale / no active references). Remaining 33 candidates include intermediate session snapshots and superpowers plans that can be archived in a follow-up PR.
- Do not delete this directory — the files are historically valuable for regression analysis.
