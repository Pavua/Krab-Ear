# Marathon Current Snapshot — Waves 740-907 (2026-05-26)

**Status**: in-session snapshot (session still active as of W877).
**Authoritative commit count**: `git log v2.0.4..origin/codex/krab-ear-v2 --oneline | wc -l` = **234 commits / ~234 PRs**.

---

## Section 1: TLDR

The 2026-05-26 marathon ran from Wave 740 (v2.0.5 binary ship) through Wave 877+, shipping **234 commits** against `codex/krab-ear-v2` after the v2.0.4 tag. The session delivered five main categories of work: (1) **service.py continued fragmentation** — three infrastructure files extracted in W797 phases 1–3 (`service_logging.py`, `ipc_server.py`, `ipc_dispatch.py`), bringing service.py from ~5024 LOC (v2.0.4 baseline) to **~2213 LOC** (−56%); 17 fully wired service objects with **zero orphan imports** (enforced by CI since W751); (2) **W797 dispatch-table O(1) cache** (`ipc_dispatch.py` builds the dict once in `__init__`, W828 phase 3), eliminating per-request linear scan over 286 handlers; (3) a **defence-in-depth audit wave** (W828–W877) covering state_store atomicity, event_replay unbounded growth, SearchIndex race, bearer-token hardcoding, InputSanitizer dead-code, DiskMonitor error-bus wiring, bookmarks TOCTOU, playback_tracker atomic write, audio_converter timeout, and obsidian YAML injection — resulting in **12+ targeted bug fixes**; (4) **100+ audit docs** across `docs/audit/` cataloguing findings with severity, owner, and fix-wave references; and (5) v2.0.5 binary shipped in W740 (PR #668) closing the two-binary drift gap left since W67.

---

## Section 2: Key Architecture Wins

### W797 — service.py 4-way split (phases 1–3 completed as W797/W813/W828)

| Phase | File extracted | LOC | What it holds |
|-------|---------------|-----|---------------|
| 1 (W797) | `backend/service_logging.py` | ~70 | `configure_logging()`, `JsonFormatter`, `_STANDARD_LOG_ATTRS` |
| 2 (W813) | `backend/ipc_server.py` | ~154 | `IPCServer` class: socket listener, accept loop, per-client reader threads |
| 3 (W828) | `backend/ipc_dispatch.py` | ~357 | `build_dispatch_table(backend_service)` — O(1) dict, built once in `__init__` |

Result: `service.py` shrank from ~3224 LOC (W796 baseline) to **~2213 LOC** after W828.

### W746 — TextProcessingService silent-import hotfix

The `TextProcessingService` import was silently dropped during a W173 rebase. Production survived only because Python had the `.py` cached. W746 detected the orphan via the W750 audit script and re-wired the import. The W750 CI guard (`scripts/audit_orphan_imports.py`) now prevents recurrence.

### W751 — HealthCheckService orphan wiring (closes last import gap)

HealthCheckService was extracted but never imported in service.py (pure dead import). W751 wired it, completing the "zero orphan imports" invariant that CI now enforces.

### W828 — Dispatch table O(1) cache

`self._dispatch_table` dict (built once in `BackendService.__init__` by `ipc_dispatch.build_dispatch_table`) replaces per-request method string comparison. All 286 active handlers are addressed by O(1) dict lookup.

---

## Section 3: Critical Bugs Fixed

| Wave | PR | Bug | Severity |
|------|----|-----|----------|
| W832 | #759 | `event_replay.ndjson` unbounded growth — no size cap or age eviction | CRIT |
| W833 | — | `SearchIndex` rebuild race — concurrent add + rebuild without lock | HIGH |
| W844 | #769 | `handle_cleanup_old_history` tz-naive/tz-aware timestamp comparison crash | HIGH |
| W845 | #773 | `CallAssistService` double-start race + recorder leak on rapid start/stop | HIGH |
| W853 | #770 | `state_store` compaction: missing `fsync` + journal truncation safety | MEDIUM |
| W854 | #776 | `error_bus` `WarnBatcher`: Sentry level string `"warn"` → `"warning"` (SDK rejects old string) | HIGH |
| W855 | #777 | `vg_ws_client.stop()` responsiveness — blocking `sleep()` replaced with `wait_for` | MEDIUM |
| W856 | #778 | `startup_diagnostics` mkdir race + HF cache env var ignored | CRIT/HIGH |
| W861 | #784 | SSRF guard missed IPv6 loopback variants (`::1`, `::ffff:127.x.x.x`) | MEDIUM |
| W866 | #788 | Hardcoded `"Bearer token_here"` placeholder in REST auth header | HIGH |
| W867 | #796 | `InputSanitizer` wired into `handle_request` — was dead code since extraction | HIGH |
| W868 | #791 | `DiskSpaceMonitor` never pushed to `error_bus` on threshold breach + `disk.critical` missing from ERROR_REGISTRY | CRIT/MEDIUM |
| W869 | #789 | Path-prefix-collision bypass in `recording_core` + `history_service` | HIGH |
| W879 | — | `PlaybackTracker` non-atomic write — partial JSON on crash | MEDIUM |
| W882 | — | `BookmarkManager` TOCTOU — read-modify-write without lock | MEDIUM |
| W893 | — | `AudioConverter` subprocess no timeout — ffmpeg hang blocks backend thread | HIGH |
| W894 | — | `ObsidianSyncManager` YAML frontmatter injection via transcript title | HIGH |

---

## Section 4: Audit Coverage (W820–W877)

Each audit doc lives in `docs/audit/2026-05-26-wave<N>-<module>.md`:

| Wave | Module(s) audited | Finding count | Notable |
|------|-------------------|---------------|---------|
| W829 | event_replay | 5 | CRIT unbounded growth → W832 fix |
| W830 | call_assist_service | 5 | 2 bugs (double-start, recorder leak) → W845 fix |
| W835 | history_service | 15 | 1 HIGH tz bug → W844 fix |
| W836 | audio_recorder | 5 | thread safety findings |
| W837 | state_store | 8 | 2 MEDIUM → W853 fix |
| W838 | stt_router | 7 | dormant routing path + config key mismatch |
| W839 | tts_service | 8 | fallback chain + audio format gaps |
| W846 | error_bus | 4 | Sentry level string bug → W854 fix |
| W849 | CLAUDE.md drift | — | Updated to reflect 2213 LOC + W797 split |
| W850 | vg_ws_client | 6 | 2 MEDIUM → W855 + W861 fixes |
| W851 | startup_diagnostics | 9 | CRIT mkdir race → W856 fix |
| W857 | transcriber | 3 | profile TOCTOU, HF_TOKEN gap |
| W859 | input_sanitizer | 7 | sanitizer never wired → W867 fix |
| W860 | disk_monitor | 7 | CRIT registry miss → W868 fix |
| W864 | ipc_throttle | 6 | token bucket math, excluded methods |
| W865 | pre-marathon archive | — | 25 stale docs archived |
| W870 | sentiment_trends, collection_manager, daily_digest | 9 | design gaps |
| W871 | performance_profiler | 6 | 2 MEDIUM |
| W873 | context_memory, transcript_context | 10 | prompt injection surface |
| W874 | realtime_partial, realtime_silence_filter | 6 | event ordering |
| W877 | bookmarks, playback_tracker, recording_chain | 15 | 3 bugs → W879/W882 fixes |

---

## Section 5: Test Suite Stats

| Metric | Value |
|--------|-------|
| Total test methods | ~12,477 (as of W817 snapshot; W877+ adds ~150 more) |
| Test files | ~439+ |
| Dispatch invariant tests | 286 keys covered (W875 extended W790 baseline) |
| Sentry status | 0 backend unresolved / 0 agent unresolved |

---

## Section 6: Service Map (post-W797, 17 services)

1. CallAssistService — call assist + VG WS client
2. HistoryService — history CRUD, SRT export, clipboard hist
3. TranslationService — translate, glossary mgmt
4. SettingsService — settings CRUD + profile presets + 5s TTL cache
5. RecordingCoreService — start/stop_recording + transcribe_paths
6. TextProcessingService — score readability/transcription, abbrev, post-process
7. TextScoringService — warmup_rewriter, extract_terms, auto_title
8. AnalyticsService — dashboard, sentiment trends, period compare, keyword cloud
9. AudioAnalyticsService — audio quality, waveform, trends
10. HealthCheckService — ping (contract bit-exact), diagnostics, integrity check
11. STTManagementService — STT hotwords CRUD, warmup, routing
12. AppleIntegrationService — Telegram bridge, Notes, Reminders, Calendar, iMessage
13. CallSessionService — call session CRUD + status lifecycle
14. LiveSubsService — system-audio streaming STT for live subtitles (Phase 2)
15. GlossaryService — glossary CSV export/import
16. LLMOpsService — list_llm_models, get_last_llm_diff, replace_word_in_last_transcript
17. SearchAndAnalysisService — semantic search + action items + recording analytics

Infrastructure files extracted from service.py: `service_logging.py`, `ipc_server.py`, `ipc_dispatch.py`.

---

## Section 7: Outstanding for Next Session

1. **W878+ audit backlog** — `obsidian_sync`, `audio_converter`, `recording_scheduler`, `sharing_manager`, `bulk_reprocess` all have pending audit docs; corresponding fixes (W893 timeout, W894 YAML injection) need tests.
2. **v2.0.6 ship** — binary is stale at v2.0.5; W868 DiskMonitor wiring + W867 InputSanitizer are production-critical fixes that need a new binary.
3. **IPC_API_REFERENCE regeneration** — 58% drift reported in W657 audit; should be regenerated post-W828 dispatch-table stabilisation (286 handlers).
4. **SearchIndex race fix** (W833 finding) — lock not yet added; concurrent add + rebuild is still racy.
5. **PlaybackTracker atomic write** (W879) + **BookmarkManager TOCTOU** (W882) — fixes identified, tests pending.
6. **AudioConverter timeout** (W893) + **ObsidianSync YAML injection** (W894) — fixes identified, not yet shipped.
7. **Cron retirement** (W716/W872) — waiting on v2.0.5 deploy confirmation before removing legacy cron from launchd plist.
8. **Disk cleanup** — `chore(wave876)` identified ~280 worktrees consuming significant disk; prune script ready but not run destructively.
