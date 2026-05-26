# Marathon Final Wrap — Wave 817 (2026-05-26)

**Supersedes**: all earlier 2026-05-26 snapshots (W703, W734, W780, W810).

---

## Section 1: TLDR

The 2026-05-26 session ran from Wave 736 through Wave 817, shipping **172 commits / 172 PRs** against
`codex/krab-ear-v2` after the v2.0.4 tag (106 PRs between v2.0.4→v2.0.5 plus 66 additional PRs post-v2.0.5).
The session delivered four main categories of work: (1) continued service extraction shrinking
`service.py` to **2706 LOC** with **18 extracted service files** and **292 live IPC handlers**;
(2) a defence-in-depth audit wave (W750–W810) that added an orphan-import CI guard, 250+ dispatch
invariant tests, a handler complexity script, Sentry breadcrumb saturation across all services, and a
dead-handler audit confirming 8 removable candidates (8 removed in W801); (3) documentation hygiene
that archived 25 stale docs, fixed CLAUDE.md drift, updated ARCHITECTURE, RELEASE_CHECKLIST,
USER_ACTION_CHECKLIST, and produced a service.py 4-way split proposal; and (4) the v2.0.5 binary ship
(W740/PR #668) that closed the two-binary drift gap for the SF Symbol / AGENT-J fix and the Sentry
release-tag fix (W704). Test suite reached **12 477 methods across 439 files**. Sentry stands at 0
unresolved issues backend + 0 agent at session end.

---

## Section 2: PR List by Category

### Release / binary ships
| Wave | PR | Description |
|---|---|---|
| W740 | #668 | **v2.0.5 release** — Sentry tag fix + SF Symbol regression + new binary |

### Service extractions (refactor)
| Wave | PR | Description |
|---|---|---|
| W172 | #524 | RecordingCoreService extraction — largest monolith cleanup |
| W392 | #602 | AnalyticsService — 6 handlers, ~98 LOC |
| W404 | #604 | TextScoringService extraction |
| W423 | #606 | HealthCheckService extraction |
| W757 | #702 | SearchAndAnalysisService — 184 LOC |
| W766 | #695 | `_push_registry_error` — collapse 3 KrabError boilerplate handlers |
| W772 | #699 | GlossaryService — 2 handlers, ~114 LOC |
| W783 | #717 | LLMOpsService — 3 handlers, ~154 LOC |
| W795 | #724 | Move handshake logic to HealthCheckService — 27 LOC |
| W796 | #722 | Move `_handle_set_paste_status` into RecordingCoreService |
| W801 | #733 | Remove 8 confirmed-dead IPC handlers (W786 audit) |
| W804 | #728 | Remove `set_paste_status` shim, wire dispatch directly |

### Bug fixes (fix)
| Wave | PR | Description |
|---|---|---|
| W187 | #535 | REST legacy auth timing attack (constant-time compare) |
| W546 | #625 | Defensive float cast for `HISTORY_LARGE_MB` in disk_monitor |
| W547 | #624 | CallAutomationController SF Symbols (AGENT-J sister) |
| W554 | #623 | Swift 6 strict concurrency warnings — unblocked all Swift PRs |
| W577–578 | #629 | Finish Wave 547+554 incomplete fixes |
| W746 | #673 | CRITICAL — restore missing TextProcessingService import in service.py |
| W774 | #701 | Wire 3 dead error codes: rewriter.circuit_open, rewriter.model_unloaded, ipc.audio_device_poll_flood |
| W794 | #711 | Hotwords audit — +23 terms, -1 irrelevant (Haskell) |
| W802 | #727 | Wire IPCClient.performHandshake at backend connection (W798 fix) |
| W804 | #728 | Remove set_paste_status shim |
| W805 | #729 | Correct capture_exception component tags in 6 files |

### Features (feat)
| Wave | PR | Description |
|---|---|---|
| W490 | #615 | Phase B Wave 82 — wire 3 HIGH priority error codes |
| W656 | #642 | Wire AgentRecoveryLogger into main.swift bootstrap |
| W686 | #646 | TCC permissions audit script |
| W743 | #671 | Sentry breadcrumbs in settings_service + startup_diagnostics |
| W748 | #675 | Sentry breadcrumbs batch 2 — call_session + recording_core + audio_analytics |
| W750 | #676 | `audit_orphan_imports.py` — catch W746-style missing imports in CI |
| W758 | #687 | Sentry breadcrumbs batch 3 — 4 services, 25 handlers |
| W763 | #689 | IPC handler complexity audit script + initial report |
| W771 | #694 | Extend audit_orphan_imports.py to catch decorators (+ optional `--strict`) |
| W781 | #705 | Audit + wire Sentry tags for STT engine + LLM model + recording state |
| W788 | #709 | `scripts/check_wave716_cron_retirement.sh` — automated criteria check |
| W791 | #712 | Sentry breadcrumbs in engine.py + translator.py |

### Tests (test)
| Wave | PR | Description |
|---|---|---|
| W521 | #618 | macOS Sequoia 26 deeper integration tests |
| W622 | #633 | TelegramBridge unit tests (5 cases) |
| W654 | #641 | Dispatch invariant tests x5 for recent IPC handlers |
| W693 | #650 | Dispatch invariant tests x5 — probe_llm_http, extract_action_items, get_last_llm_diff, get_activity_calendar, check_integrity |
| W759 | #686 | Cover apple_integration_service — 34 tests added |
| W768 | #690 | Dispatch invariant tests for 20 critical IPC methods |
| W776 | #697 | Cover code_switching_detector — 21 tests added |
| W790 | #715 | Full dispatch invariant coverage (target >=250 handlers) |
| W806 | #735 | De-flake top-3 tests — remove sleep, use threading.Event |

### CI improvements (ci)
| Wave | PR | Description |
|---|---|---|
| W784 | #710 | Add flake8, coverage, CLAUDE.md check, Python 3.12 to krabear-ci.yml |

### Documentation (docs)
| Wave | PR | Description |
|---|---|---|
| W657 | #637 | IPC_API_REFERENCE drift audit |
| W655 | #638 | Sentry breadcrumb audit — 5 services |
| W660 | #635 | v2.0.5 candidate section in RELEASE_CHECKLIST |
| W689 | #648 | LM Studio probe URL audit — 9 call sites, 0 live JIT risks |
| W690 | #645 | USER_MANUAL.md v2.0.5 — Что нового subsection |
| W701 | #653 | Sentry sweep — 0 unresolved, AGENT-J/M fix validated |
| W718 | #658 | Document test_full_workflow.py cross-cutting blocker |
| W740 | #667 | Marathon final wrap + CLAUDE.md drift fix (318 handlers, 11 services) |
| W744 | #672 | CLAUDE.md drift fix — service.py LOC + handler + service counts |
| W761 | #685 | W716 cron retirement criteria + verification plan |
| W764 | #691 | ARCHITECTURE-KRAB-EAR.md drift fix — 14 services, 300 handlers |
| W765 | #696 | Error codes audit — find stale ERROR_REGISTRY entries |
| W767 | #688 | RELEASE_CHECKLIST.md — add W750/W762/W704 verification steps |
| W769 | #692 | USER_ACTION_CHECKLIST.md — mark W716/W704 shipped, add deploy section |
| W770 | #693 | requirements.txt audit — pinning + freshness report |
| W775 | #700 | Document + harden performance budget gate |
| W778 | #707 | Config audit — unused settings + duplicates |
| W779 | #704 | Canonical service extraction pattern |
| W780 | #703 | Marathon milestone snapshot (Waves 740-780, ~35 PRs) |
| W782 | #706 | HuggingFace cache orphans audit |
| W785 | #716 | Flaky-test audit — 35 candidates in 436 test files |
| W786 | #726 | Dead IPC handler audit — 289 handlers, 8 dead candidates |
| W787 | #708 | Contracts registry audit — 3 dead event types, 14 untyped emit calls |
| W789 | #714 | Swift LOC audit — top-25 files, refactor candidates, complexity hotspots |
| W792 | #718 | Audit docs/ for stale/superseded files — 58 archive candidates |
| W797 | #725 | service.py split proposal — 4-way extraction spec |
| W798 | #721 | IPC versioning & handshake compatibility audit |
| W799 | #720 | Scheduled routines audit — 19 routines, 5 stale, 1 duplicate pair |
| W800 | #723 | event_bus.py audit — emit sites, backpressure, thread safety |
| W803 | #731 | Archive 25 stale/superseded docs (W792 audit) |
| W809 | #732 | rest_server.py audit — auth + validation + error handling |
| W810 | #736 | scripts/ directory audit — 68 total, 18 obsolete, 3 duplicate pairs |

### Chores / infra
| Wave | PR | Description |
|---|---|---|
| W537 | #621 | Standalone drift-check script + verify v2.0.4 binary sync |
| W557 | #628 | Prefetch whisper-large-v3-mlx to eliminate cold-cache warning |
| W651 | #643 | Prune 141 stale worktrees, 9 GB freed |
| W775 | #700 | Performance budget gate hardening |
| W793 | #713 | Makefile targets — audit-orphans + audit-handlers + dispatch-tests |
| W807 | #730 | Fix krab-ear-disk-hygiene + fresh-mlx-models-watcher routine references |

---

## Section 3: Architectural Wins

### service.py monolith reduction
| Metric | v2.0.4 baseline | Wave 817 (now) |
|---|---|---|
| LOC | ~5024 | **2706** |
| Active IPC handlers | 296 | **292** (8 removed W801) |
| Extracted service files | 9 | **18** |

The 18 extracted service files are:
`analytics_service`, `apple_integration_service`, `audio_analytics_service`,
`call_assist_service`, `call_session_service`, `glossary_service`,
`health_check_service`, `history_service`, `live_subs_service`,
`llm_ops_service`, `recording_core_service`, `search_and_analysis_service`,
`settings_service`, `stt_management_service`, `text_processing_service`,
`text_scoring_service`, `translation_service`, `tts_service`.

Four-way split proposal (W797/PR #725) documents the remaining extraction roadmap —
targeting the ~2700 LOC monolith down toward a ~500-LOC coordinator shell.

### Test suite growth
| Metric | v2.0.4 | Wave 817 |
|---|---|---|
| Test files | 399 | **439** |
| Test methods | ~10 864 | **12 477** |

### Error system
| Metric | Value |
|---|---|
| Error codes in ERROR_REGISTRY | **49** |
| Phase B Wave 82 codes wired | 3 HIGH (disk.critical, system.proc_cmdline_permission, startup.stt_model_cache_miss) |
| Dead error codes wired | 3 (rewriter.circuit_open, rewriter.model_unloaded, ipc.audio_device_poll_flood) |

### v2.0.5 ship (W740 / PR #668)
Closed the two-binary drift gap that had kept AGENT-J (SF Symbol `●` font hang) and Sentry
release-tag stale at `2.0.0` as source-only fixes since Wave 274. The binary shipped with the stable
`Krab Ear Dev Local` codesign identity.

---

## Section 4: Defence in Depth

### W750 — audit_orphan_imports.py (PR #676)
CI guard that detects W746-style missing module imports before they reach production. W746 was a
CRITICAL regression (TextProcessingService import wiped) that silently dropped ~15 IPC handlers.
The script now runs in CI via `make audit-orphans`. The `--strict` flag (W771) extends detection to
decorator-based registrations.

### W768 + W790 — Dispatch invariant tests (PRs #690, #715)
`test_dispatch_invariants_wave*.py` files verify that every wired handler name resolves to a callable
in the handler table at import time. W768 covered 20 critical methods. W790 extended coverage to the
full set of >=250 handlers, making silent dispatch-table gaps a CI failure rather than a runtime 404.

### W758 + W791 — Sentry breadcrumb saturation (PRs #687, #712)
Breadcrumbs now cover all 18 service files plus `engine.py` and `translator.py` — the two
highest-volume paths. Every IPC dispatch records method name, duration_ms, and ok/error without
touching transcript text (privacy-safe pattern).

### W786 — Dead IPC handler audit (PR #726)
Systematic audit of all 289 handlers using the Wave 65 three-scope methodology (Swift callers,
Python test dispatch, direct Python calls). Found 8 confirmed-dead candidates; all 8 removed in W801.
Active handler count: **292**.

### W784 — CI matrix expansion (PR #710)
`krabear-ci.yml` now runs: flake8, test coverage, CLAUDE.md drift check, Python 3.12. Previously
only ran pytest on the default Python version.

---

## Section 5: Documentation Refresh

### CLAUDE.md
W744 (PR #672) and W740 (PR #667) corrected handler count (→292), service count (→18),
and service.py LOC (→2706). These three numbers are the most frequently drifted fields and are
now verified by the `make audit-handlers` Makefile target (W793/PR #713).

### ARCHITECTURE-KRAB-EAR.md
W764 (PR #691) synced service list to 14 (at that point), handler count to 300. Superseded by
current CLAUDE.md counts; the ARCHITECTURE file is acknowledged to have ~58% drift (W657 audit).

### IPC_API_REFERENCE.md
W657 audit found 58% drift relative to the live handler table. The file remains as human-authored
reference. Ground truth is always `grep -cE '"[a-z_]+":\s*self\._' KrabEar/backend/service.py`.

### Docs archive
W792 (PR #718) audited docs/ and identified 58 stale/superseded files.
W803 (PR #731) archived 25 of them. Remaining 33 are either pending further review or
actively referenced.

### Other doc updates
- RELEASE_CHECKLIST.md (W767/PR #688): added W750/W762/W704 verification steps
- USER_ACTION_CHECKLIST.md (W769/PR #692): marked W716/W704 shipped, added deploy section
- USER_MANUAL.md v2.0.5 (W690/PR #645): Что нового subsection for 10 waves
- requirements.txt audit (W770/PR #693): pinning + freshness report
- Scheduled routines audit (W799/PR #720): 19 routines documented, 5 stale, 1 duplicate identified
- event_bus.py audit (W800/PR #723): backpressure and emit-site inventory
- rest_server.py audit (W809/PR #732): auth + validation + error handling gaps

---

## Section 6: Outstanding Items

These items were identified but not resolved in this session:

| Item | Source | Status |
|---|---|---|
| 4-way service.py split execution | W797/PR #725 spec | Spec written, not yet implemented |
| 18 obsolete scripts removal | W810/PR #736 audit | Audit only; deletion pending |
| 33 remaining stale docs archival | W792 partial | 25/58 archived, 33 remain |
| 3 dead event types in contracts/registry.py | W787/PR #708 | Audit only; removal pending |
| 14 untyped `emit()` calls → typed `emit_typed()` | W787/PR #708 | Migration pending |
| W716 GigaAM cron script retirement | W761/PR #685 criteria | Automated check ships; human decision pending |
| 5 stale scheduled routines | W799/PR #720 | Audit only; retirement pending |
| flaky-test remediation (35 candidates) | W785/PR #716 | 3 de-flaked (W806/PR #735); 32 remain |
| Swift LOC hotspots refactor | W789/PR #714 | Audit only; refactor pending |
| HuggingFace cache orphan cleanup | W782/PR #706 | Audit only; cleanup pending |
| Config orphan keys removal | W635/PR #634 | Audit only; removal pending |
| HF pyannote model gate accept | User action | Blocked on user accepting HuggingFace gated model |

---

## Authoritative counts (2026-05-26 end of session)

```
service.py LOC:         2706
Active IPC handlers:     292
Extracted service files:  18
Test files:              439
Test methods:         12 477
Error codes (registry):   49
PRs since v2.0.4:        172  (106 v2.0.4→v2.0.5 + 66 post-v2.0.5)
Current release:       v2.0.5
Sentry unresolved:         0 backend / 0 agent
```
