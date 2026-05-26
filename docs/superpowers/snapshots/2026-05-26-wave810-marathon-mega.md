# Wave 810 — Marathon Mega-Summary (2026-05-26, Waves 740–810+)

**Date**: 2026-05-26 (session final snapshot)
**Branch**: codex/krab-ear-v2
**Base tag**: v2.0.4 → v2.0.5 SHIPPED (Wave 740 context)
**PRs in session (W740–W800+)**: 57 PRs (#667–#726)

---

## Top Stats

| Metric | Value |
|--------|-------|
| PRs merged (W740–W800+) | 57 |
| Total PRs since v2.0.4 | 163 |
| service.py at v2.0.4 | 5 478 LOC |
| service.py now | 3 873 LOC |
| LOC removed from monolith | −1 605 LOC |
| Active IPC handlers (grep) | 304 |
| Newly extracted services (this session) | 5 (STTMgmt, TextScoring, Analytics, GlossaryService, LLMOpsService + SearchAndAnalysis) |
| Total extracted services | 14 |
| Test files total | 433 |
| New tests added (W752–W790) | ~110 (wiring guards + dispatch invariants + module coverage) |
| CI gates added | 4 (flake8, coverage, CLAUDE.md check, Python 3.12) |
| Audit docs produced | 13 |

---

## Session Overview

This session continued where the Wave 735 "117-PR final" left off. The macro-theme
shifted from quantity (test + code coverage sweeps) to **quality hardening**:
formalising the service-extraction pattern, wiring previously-stubbed services,
closing audit debt across docs/configs/contracts, and locking in CI gates that
make regressions self-detectable.

Three parallel work-streams ran throughout:

1. **Monolith shrink** — 6 refactor waves removed ~750 LOC net from service.py
2. **Audit cascade** — 13 audit docs identified drift, orphans, dead code, stale configs
3. **Observability** — 4 breadcrumb batches wired Sentry coverage to 8 services

---

## Critical Fixes

### W746 — CRITICAL: TextProcessingService import missing (PR #673)

Severity: **P0 runtime crash**. `TextProcessingService` had been extracted in an
earlier wave but its `import` was never added to `service.py`. Any IPC method
that delegated to `TextProcessingService` would raise `NameError` on first call,
silently breaking ~12 handlers.

Fix: restore one `from backend.text_processing_service import TextProcessingService`
line. Root cause logged as the "W746-style import gap" — CI now catches future
instances via `audit_orphan_imports.py` (W750, PR #676).

### W774 — Wire 3 dead error codes (PR #701)

Three `ERROR_REGISTRY` entries existed but were never emitted at runtime:
- `rewriter.circuit_open` — circuit breaker trips silently
- `rewriter.model_unloaded` — LLM unload not reported
- `ipc.audio_device_poll_flood` — sounddevice polling not guarded

All three wired to their trigger sites. Sentry now captures these failure modes.

### W790 — Full dispatch invariant coverage (PR #715)

Formalised the "dispatch invariant test" pattern started in W768 (20 handlers)
into a comprehensive suite covering ≥250 handlers. Any future handler added to
`handle_request` but missing from its delegating service immediately fails CI.
This pattern was first introduced after W746 exposed the danger of silent wiring
gaps.

---

## Refactor Waves — Service Extractions

### W734 / W742 — AppleIntegrationService + STTManagementService (PRs #669, #670)

Both services had been created as stubs in Wave 734 (`feat(wave734): add missing
stt_management_service + apple_integration_service modules`). This session wired
their handlers:

- **AppleIntegrationService**: 6 handlers, ~256 LOC out of service.py
- **STTManagementService**: 6 handlers, ~168 LOC out of service.py

### W747 — TextScoringService + AnalyticsService (PR #674)

9 handlers delegated in a single wave. Both services existed from earlier
extraction waves (W392/W423); this wave completed the wiring.

### W751 + W755 — HealthCheckService (PRs #681, #682)

6 handlers including the `handshake` and `handle_ping` methods wired to
`HealthCheckService`. Ping contract preserved unchanged (Swift `HealthMonitor`
sends ping every 3 s — must not break). Wiring guard tests added in W755.

### W757 — SearchAndAnalysisService (PR #702)

184 LOC extracted. Consolidated search-related handlers
(`fuzzy_search_history`, `semantic_search`, `get_search_suggestions` etc.) into
a dedicated service.

### W766 — `_push_registry_error` helper (PR #695)

Not a full service extraction but a DRY refactor: 3 report handlers
(`report_paste_failure`, `report_hotkey_conflict`, `report_reconnect`) each
duplicated 12 lines of `KrabError` construction. Collapsed into a single
private helper, −36 LOC.

### W772 — GlossaryService (PR #699)

2 handlers, ~114 LOC: `get_glossary_suggestions` and `set_translation_glossary_item`
split out. Reduces the translation domain footprint in the monolith.

### W773 — Simplify `_handle_get_recording_stats` (PR #698)

Handler had 71 LOC of manual JSON assembly. Replaced with 3-line delegation to
`RecordingCoreService.get_recording_stats()`. Net: −68 LOC.

### W783 — LLMOpsService (PR #717)

3 handlers, ~154 LOC: `list_llm_models`, `load_llm_model`, `unload_llm_model`
extracted. LM Studio lifecycle calls are now isolated from the main service.

### W795 + W796 — HealthCheckService + RecordingCoreService micro-moves (PRs #724, #722)

- Handshake logic (27 LOC) moved from service.py into HealthCheckService (W795)
- `_handle_set_paste_status` moved into RecordingCoreService (W796)

---

## Audit Cascade (W750–W800)

| Wave | Subject | Key Findings |
|------|---------|--------------|
| W750 | `audit_orphan_imports.py` | CI script to catch W746-style gaps; detects decorator references too (W771) |
| W763 | IPC handler complexity | Audit script + initial report; identified top-10 complex handlers (LoC + cyclomatic) |
| W765 | Error codes | Stale `ERROR_REGISTRY` candidates; 3 confirmed dead |
| W778 | Config audit | Unused/duplicate settings in `core/config.py` |
| W780 | Marathon milestone snapshot (W740–W780) | Interim state capture; 35 PRs documented |
| W782 | HuggingFace cache orphans | Orphaned model dirs after extractions; cleanup candidates listed |
| W785 | Flaky-test audit | 35 candidates in 436 test files; top-5 marked with skip markers |
| W786 | Dead IPC handler audit | 289 active handlers confirmed; 8 dead candidates (not yet removed) |
| W787 | Contracts registry audit | 3 dead `EventType` entries; 14 `emit()` calls still untyped |
| W789 | Swift LOC audit | Top-25 Swift files by size; `HistoryPanelController.swift` (3 412 LOC) flagged |
| W792 | docs/ stale files audit | 58 archive candidates; docs/ has grown to 87 files |
| W797 | service.py split proposal | 4-way extraction spec (Routing + Pipeline + Infra + Audio) |
| W798 | IPC versioning audit | Handshake compatibility matrix documented |
| W799 | Scheduled routines audit | 19 routines, 5 stale, 1 duplicate pair (`two-binary-drift-watch`) |
| W800 | `event_bus.py` audit | 37 emit sites catalogued; backpressure and thread-safety gaps noted |

---

## Observability — Sentry Breadcrumbs

Four batches wired breadcrumbs across the service layer:

| Wave | Services covered | Handlers |
|------|-----------------|----------|
| W743 | `settings_service`, `startup_diagnostics` | ~8 |
| W748 | `call_session_service`, `recording_core_service`, `audio_analytics_service` | ~15 |
| W758 | 4 additional services | 25 |
| W781 | Sentry tags: STT engine, LLM model name, recording state | runtime tags |
| W791 | `engine.py`, `translator.py` (core layer) | ~6 |

Breadcrumbs follow the established pattern (no transcript text, only metadata:
method, duration_ms, ok/error). After W791, every IPC call that touches STT or
translation emits a breadcrumb — giving full crash context in Sentry reports.

---

## CI & Tooling Hardening

### W750 — audit_orphan_imports.py (PR #676)

Prevents recurrence of W746 (missing import). Checks that every class referenced
in `service.py`'s handler dispatch table has a corresponding `import` statement.
Extended in W771 to also catch decorator and optional function-call references
(`--strict` flag).

### W784 — krabear-ci.yml expanded (PR #710)

Added to the CI matrix:
- `flake8` lint gate (previously run manually)
- `coverage` threshold gate (fails below 60%)
- `verify_claude_md.py` CLAUDE.md drift check
- Python 3.12 added alongside Python 3.11 test run

### W788 — Wave 716 cron retirement check script (PR #709)

`scripts/check_wave716_cron_retirement.sh` automates the checklist for retiring
the `kill_dup_gigaam.command` cron (Wave 716 short-term fix). Outputs
pass/fail for each criterion so the decision is objective.

### W793 — Makefile audit targets (PR #713)

Added `audit-orphans`, `audit-handlers`, `dispatch-tests` Makefile targets so
the three key audit scripts are one command away for developers.

### W794 — Hotwords refresh (PR #711)

Audited `backend/default_hotwords.py` against the actual vocab in use. Added 23
domain terms (new AI model names, Russian product terms); removed 1 irrelevant
term (Haskell).

### W775 — Performance budget gate hardening (PR #700)

Documented the p95-latency regression gate (>15% = CI fail). Added guard against
baseline-file staleness and a `--reset-baseline` flag for intentional perf
changes.

---

## Documentation Waves

| Wave | Document | Change |
|------|----------|--------|
| W740 | CLAUDE.md | Marathon final wrap; 318 handlers, 11 services corrected |
| W744 | CLAUDE.md | service.py LOC + handler + service counts after v2.0.5 base |
| W745 | IPC_API_REFERENCE.md | Full regeneration from authoritative sources (58% drift fixed) |
| W749 | Snapshot doc | Marathon Batch 2 state (W740–W750) |
| W753 | v2.0.5 deployment plan | Paranoid pre/post-deploy checklist |
| W756 | CLAUDE.md | Post-W751 update + worktree cleanup script reference |
| W760 | CLAUDE.md | Architecture refresh: 14-service map |
| W761 | W716 cron retirement | Criteria + verification plan |
| W764 | ARCHITECTURE-KRAB-EAR.md | Drift fix: 14 services, 300 handlers |
| W767 | RELEASE_CHECKLIST.md | Add W750/W762/W704 steps |
| W769 | USER_ACTION_CHECKLIST.md | Mark W716/W704 shipped; add deploy section |
| W779 | Extraction pattern doc | Canonical service extraction pattern (for agents) |
| W780 | Snapshot doc | Marathon milestone W740–W780 |
| W797 | service.py split proposal | 4-way extraction spec |

---

## Memory Pattern Findings

The audit cascade this session surfaced a recurring anti-pattern: **documentation
drift accumulation**. Every major extraction wave (W525, W683, W691, W734) updated
code but not the docs that describe the handler count, service list, and LOC.

**Pattern**: extraction happens in wave N; CLAUDE.md drift goes unnoticed until
wave N+10 when another agent runs into stale counts and wastes research time.

**Fix applied**: W750 `audit_orphan_imports.py` + W784 `verify_claude_md.py` in
CI now make this class of drift self-detecting within one commit.

A second pattern: **stub services sitting unwired for multiple waves** (W734
stubs, wired in W742/W747/W751). Each unwired stub is a latent W746-class crash.
W750 CI script and W752/W755 wiring-guard tests close this gap.

---

## Service Architecture State (post-session)

```
service.py (3 873 LOC, ~304 handlers)
  delegates to 14 services:

  history_service             health_check_service
  settings_service            recording_core_service
  translation_service         audio_analytics_service
  call_assist_service         text_processing_service
  call_session_service        text_scoring_service
  live_subs_service           analytics_service
  tts_service                 stt_management_service
                              apple_integration_service
                              search_and_analysis_service (W757)
                              glossary_service (W772)
                              llm_ops_service (W783)
```

Target from W797 spec: 4-way split of remaining service.py core
(Routing / Pipeline / Infra / Audio) — planned for next session.

---

## Outstanding Items

1. **8 dead handler candidates** (W786 audit, PR #726) — not yet removed; need
   full scope verification (Swift + test + Python grep) per Wave 65 methodology.
2. **3 dead EventType entries** (W787) — `contracts/registry.py` cleanup pending.
3. **14 untyped `emit()` calls** (W787) — migration to `emit_typed()` pending.
4. **58 stale docs/** archive candidates (W792) — triage + archive pass needed.
5. **5 stale routines** (W799) — `two-binary-drift-watch` duplicate; 4 others
   with expired cron criteria.
6. **4-way service.py split** (W797 spec) — largest remaining monolith work;
   ~1 800 LOC target for next extraction wave.
7. **v2.0.5 binary ship** — RELEASE_NOTES_v2.0.5.md exists; binary not yet
   tagged/distributed.
8. **HF pyannote gated accept** — pyannote VAD requires manual HuggingFace accept
   (unchanged from prior sessions).

---

## Cumulative Project Stats (as of Wave 810)

| Metric | Value |
|--------|-------|
| Total waves since project start | ~810 |
| service.py at peak (pre-extraction) | ~5 821 LOC |
| service.py now | 3 873 LOC |
| Total LOC removed from monolith | ~1 948 LOC |
| Extracted services | 14 (+ 4 planned) |
| Test files | 433 |
| Error codes | 57 |
| Active IPC handlers | ~304 |
| CI gates | flake8, coverage, CLAUDE.md drift, Python 3.11+3.12, dispatch-invariants |
| Versions shipped | v2.0.3, v2.0.4, v2.0.5 |
