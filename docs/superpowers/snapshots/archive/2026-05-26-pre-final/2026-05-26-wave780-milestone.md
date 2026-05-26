# Wave 780 Marathon Milestone Snapshot

**Date:** 2026-05-26  
**Waves covered:** W740 → W780  
**PRs merged this session:** 39 (#660–#702)  
**Tag shipped:** v2.0.5  
**Branch base:** codex/krab-ear-v2

---

## 1. Session Outcome

| Metric | Before (v2.0.4) | After (W780) | Delta |
|--------|-----------------|--------------|-------|
| service.py LOC | 5,478 | 3,872 (main) / ~2,940 (W757 tip) | −1,606 |
| Extracted services | 11 | 15+ (SearchAndAnalysis + Glossary added) | +4 |
| Active IPC handlers | ~300 | ~296 delegated | stable |
| Error codes registered | 57 | 57 (3 dead codes re-wired W774) | 0 net |
| Sentry breadcrumb coverage | ~6 services | 14+ services (3 breadcrumb batches) | +8 |
| Dispatch invariant tests | 0 | 20 critical methods covered | +20 |
| CI orphan-import guard | absent | audit_orphan_imports.py in CI (W750) | new |
| Version shipped | v2.0.4 | v2.0.5 | +1 tag |

---

## 2. Critical Find: W746 Hotfix Backstory

**Root cause:** The refactor wave that extracted `TextProcessingService` removed the
`from backend.text_processing_service import TextProcessingService` line from
`service.py` — but the `handle_request` dispatch table still referenced it by name.
Python deferred the `NameError` until the first runtime call to any of those handlers.
No static analysis caught it because the orphan import audit script (W750) did not yet
exist; tests also missed it because they mocked the handler at the method level.

**Impact:** All `text_processing_*` IPC calls would have silently 500'd in production
(`NameError: name 'TextProcessingService' is not defined`). The bug was present from
the moment the service was extracted until W746 (#673).

**Fix (W746, #673):** One-line restore of the import. Took 5 minutes to apply.

**Prevention (W750, #676):** `scripts/audit_orphan_imports.py` now runs in CI and
fails the build if any service name appears in the handler dispatch table but is missing
from the file's imports. W771 (#694) extended it to catch decorator-style references
and added `--strict` flag for function-call scanning.

**Lesson:** Service extractions must be paired with an import-presence check at the PR
level. The extraction pattern in CLAUDE.md now includes this step explicitly.

---

## 3. PRs Table (W740–W780)

| PR | Wave | Category | Description |
|----|------|----------|-------------|
| #660 | W730 | audit | test_full_workflow root-cause analysis (Wave 718 blocker) |
| #661 | W732 | fix | Add pytest xdist_group for test_full_workflow.py isolation |
| #663 | W734 | feat | Add missing stt_management_service + apple_integration_service modules |
| #667 | W740 | docs | Marathon final wrap + CLAUDE.md drift fix (318 handlers, 11 services) |
| #668 | — | release | v2.0.5 — Sentry release tag fix + SF Symbol regression + new binary |
| #669 | W734 | refactor | Wire AppleIntegrationService — 6 handlers, ~256 LOC out of service.py |
| #670 | W742 | refactor | Wire STTManagementService — 6 handlers, ~168 LOC out of service.py |
| #671 | W743 | feat | Sentry breadcrumbs batch 1 — settings_service + startup_diagnostics |
| #672 | W744 | docs | CLAUDE.md drift fix — service.py LOC + handler + service counts |
| #673 | W746 | fix CRITICAL | Restore missing TextProcessingService import in service.py |
| #674 | W747 | refactor | Wire TextScoringService + AnalyticsService — 9 handlers delegated |
| #675 | W748 | feat | Sentry breadcrumbs batch 2 — call_session + recording_core + audio_analytics |
| #676 | W750 | feat | audit_orphan_imports.py — catch W746-style missing imports in CI |
| #677 | W749 | docs | Marathon Batch 2 snapshot (Waves 740–750) |
| #678 | W745 | docs | Regenerate IPC_API_REFERENCE.md from authoritative sources |
| #679 | W752 | test | Wiring guard tests for AppleIntegration + TextScoring + Analytics |
| #680 | W753 | docs | v2.0.5 deployment plan — paranoid pre/post checks |
| #681 | W751 | refactor | Wire HealthCheckService — 6 handlers, ping contract preserved |
| #682 | W755 | test | Wiring guard test for HealthCheckService (W751) |
| #683 | W756 | docs | CLAUDE.md post-W751 refresh + worktree cleanup script |
| #684 | W760 | docs | CLAUDE.md architecture refresh — 14-service map |
| #685 | W761 | docs | W716 cron retirement criteria + verification plan |
| #686 | W759 | test | Cover apple_integration_service — 34 tests added |
| #687 | W758 | feat | Sentry breadcrumbs batch 3 — 4 services, 25 handlers |
| #688 | W767 | docs | RELEASE_CHECKLIST.md — add W750/W762/W704 verification steps |
| #689 | W763 | feat | IPC handler complexity audit script + initial report |
| #690 | W768 | test | Dispatch invariant tests for 20 critical IPC methods |
| #691 | W764 | docs | ARCHITECTURE-KRAB-EAR.md drift fix — 14 services, 300 handlers |
| #692 | W769 | docs | USER_ACTION_CHECKLIST.md — mark W716/W704 shipped, add deploy section |
| #693 | W770 | docs | requirements.txt audit — pinning + freshness report |
| #694 | W771 | feat | Extend audit_orphan_imports.py to catch decorators + --strict flag |
| #695 | W766 | refactor | Extract _push_registry_error — collapse 3 report handlers' boilerplate |
| #696 | W765 | docs | Error codes audit — identify stale ERROR_REGISTRY entries |
| #697 | W776 | test | Cover code_switching_detector — 21 tests added |
| #698 | W773 | refactor | Simplify _handle_get_recording_stats — 71→3 LOC (approach A) |
| #699 | W772 | refactor | Extract GlossaryService — 2 handlers, ~114 LOC |
| #700 | W775 | chore | Document + harden performance budget gate |
| #701 | W774 | fix | Wire 3 dead error codes — rewriter.circuit_open + rewriter.model_unloaded + ipc.audio_device_poll_flood |
| #702 | W757 | refactor | Extract SearchAndAnalysisService — 184 LOC out of service.py |

---

## 4. Architectural Learnings Codified

### 4.1 Service extraction must pair with import-presence test

Pattern before W746: extract service → update dispatch table → assume "it compiles, it works."

Pattern after W746 + W750:
1. Extract service into `backend/<name>_service.py`.
2. Add `from backend.<name>_service import <Name>Service` at top of `service.py`.
3. Add wiring guard test (`test_wire_<name>_service.py`) that imports `BackendService`
   and asserts the handler methods resolve without `NameError`.
4. `audit_orphan_imports.py` runs in CI as a final net.

### 4.2 Sentry breadcrumbs as per-extraction obligation

After W758, the rule is: every extracted service that handles user-visible IPC methods
gets Sentry breadcrumbs on its top-3 methods (by call frequency) before the PR is
merged. The breadcrumbs use `{"category": "ipc", "data": {"ok": bool}}` shape, never
include transcript text.

### 4.3 Handler complexity as extraction signal

W763 (#689) produced an IPC handler complexity audit script. Any handler over 50 LOC
is a refactor candidate; over 100 LOC is mandatory. `_handle_get_recording_stats` was
71 LOC → reduced to 3 LOC by delegating to the already-extracted `RecordingCoreService`
(W773, #698).

### 4.4 Dead error codes are a smell

W774 (#701) found 3 registered error codes (`rewriter.circuit_open`,
`rewriter.model_unloaded`, `ipc.audio_device_poll_flood`) that existed in
`ERROR_REGISTRY` but were never emitted at any call site. Wiring them to actual code
paths took 1 wave each. Lesson: add a CI check that every `ERROR_REGISTRY` key appears
in at least one `_push_error` call.

### 4.5 Dispatch invariant tests as regression net

W768 (#690) added 20 dispatch invariant tests: for each critical method, the test calls
`BackendService().handle_request({"method": name})` with a stub and asserts `ok=true`
(not a routing exception). This catches missing delegation lines immediately, unlike
integration tests which need a full live backend.

---

## 5. Live State Metrics (end of W780)

| Metric | Value |
|--------|-------|
| service.py LOC | ~3,872 (on main); target <3,000 by W800 |
| Extracted services | 15 (call_assist, history, translation, settings, live_subs, recording_core, analytics, call_session, audio_analytics, health_check, text_processing, text_scoring, apple_integration, stt_management, search_and_analysis) + GlossaryService |
| Active IPC handlers | ~296 |
| Dead handlers removed | 219 (total since Wave 65) |
| Error codes | 57 registered, 57 wired |
| Sentry breadcrumb coverage | 14+ services |
| Dispatch invariant tests | 20 critical methods |
| CI orphan-import guard | active (W750 + W771 extended) |
| Test methods | ~10,864+ / 404+ files |
| Latest release tag | v2.0.5 |
| Sentry backend unresolved | 0 |
| Sentry agent unresolved | 0 |

---

## 6. Outstanding for Next Session

| Priority | Item | Notes |
|----------|------|-------|
| HIGH | Ship v2.0.6 with GlossaryService + SearchAndAnalysisService in binary | Both extracted this session, binary not yet rebuilt |
| HIGH | CI check: every ERROR_REGISTRY key appears in ≥1 `_push_error` call | W774 lesson — prevent re-accumulation of dead codes |
| MED | service.py below 3,000 LOC | Currently ~3,872; next extraction targets: recording_scheduler cluster (~200 LOC), bulk_reprocess cluster (~150 LOC) |
| MED | Extend dispatch invariant tests to all 296 handlers | Currently 20 of 296 covered |
| MED | Sentry breadcrumbs for GlossaryService + SearchAndAnalysisService | Obligatory per W758 rule |
| LOW | Retire W716 cron if retirement criteria met (W761 plan) | Verification checklist written; needs runtime data |
| LOW | HF pyannote gate accept | Still pending manual HuggingFace accept for gated model |
| LOW | dSYM upload for v2.0.5 agent binary | Needed for symbolicated AppHang traces in Sentry |
