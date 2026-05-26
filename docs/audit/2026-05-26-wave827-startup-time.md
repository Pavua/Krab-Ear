# Wave 827 — BackendService.__init__ Startup Time Audit

**Date:** 2026-05-26  
**Branch:** `feature/audit-startup-time-W827`  
**Scope:** `KrabEar/backend/service.py` lines 168–636 (`BackendService.__init__`)  
**Goal:** Identify slow init paths, parallelisation opportunities, and lazy-init candidates.

---

## Init Sequence (numbered, in order)

| # | Step | Code Location | Cost Category |
|---|------|--------------|---------------|
| 1 | `VocabularyStore(data_dir)` | L176 | Trivial — `mkdir parents=True` only; no disk read |
| 2 | `AudioRecorder(on_audio_level=...)` | L184 | Trivial — opens no device yet; PortAudio init deferred to `start()` |
| 3 | `_init_llm_rewriter()` | L187 | **SLOW** — creates `requests.Session`, then calls `rewriter.ping()` synchronously: `GET /models` with `LLM_TIMEOUT_SEC` default 45 s. If LM Studio is not running, hangs until timeout. |
| 4 | LLM warmup background thread | L195–200 | Async — daemon thread `warmup_sync()` with backoff (5+10+20+30+60 s retry chain). Non-blocking. |
| 5 | `_init_action_items_extractor()` | L201 | Trivial — builds `ActionItemsExtractor` with same LM Studio params; no network call. |
| 6 | `Transcriber(llm_rewriter, settings_get)` | L203–209 | **SLOW** — creates `AudioEngine.__init__` which: (a) imports STTRouter; (b) if `STT_GIGAAM_ENABLED`, spawns background GigaAM warmup thread (~30 s cold subprocess). Engine itself does NOT load mlx_whisper model at init — model is lazy-loaded on first transcription. |
| 7 | `Translator()` | L216 | Trivial — in-memory cache init only. |
| 8 | `SettingsService(store)` | L221 | **MODERATE** — calls `store.load_settings()` through `cached_settings()` on first use (deferred to first call, not at construction). Actually `__init__` only sets up cache fields. First `cached_settings()` call loads settings.json from disk. |
| 9 | `GlossaryService(settings_svc)` | L223 | Trivial |
| 10 | `LLMOpsService(store, settings_svc, transcriber)` | L225 | Trivial |
| 11 | `STTManagementService(settings_svc, transcriber)` | L231 | Trivial |
| 12 | Settings `after_save_hook` registration | L237–242 | Trivial |
| 13 | STT warmup background thread (opt-in) | L251–261 | Async — `stt_warmup_on_startup` default=False; if enabled, runs `AudioEngine.warmup()` in daemon thread: loads mlx_whisper model into GPU memory (cold = 1–5 s MLX + model size). |
| 14 | `ErrorBus`, `ERROR_REGISTRY`, `LLMHttpProbe` imports | L263–307 | **MODERATE** — three deferred imports (`error_bus`, `error_codes`, `llm_probe`, `sentry_sdk`). Import cost is once per process; on cold import: `error_codes.py` builds 57-entry dict, `sentry_sdk` import ~30–50 ms if SDK installed. |
| 15 | `LLMHttpProbe.start()` | L306–307 | Async — spawns daemon thread for periodic GET `/models` every 30 s. Non-blocking. |
| 16 | `_check_binary_drift_on_startup()` | L313–314 | **SLOW** — runs `dwarfdump` subprocess twice (bundle + runtime KrabEarAgent binaries). Typical: 200–800 ms per `dwarfdump` call on M-series. Opt-out via `binary_drift_check_on_startup=False`. |
| 17 | `SystemMonitor()` | L316 | Trivial — no threads started |
| 18 | `CollectionManager(store)` | L318 | Trivial |
| 19 | `NormalizationProfileRegistry(data_dir)` | L319 | Trivial — reads a small JSON file if present |
| 20 | `RecordingChainManager(store)` | L320 | Trivial |
| 21 | `BookmarkManager(data_dir)` | L321 | Trivial — NDJSON lazy-load |
| 22 | `RecordingScheduler(data_dir)` | L322 | Trivial |
| 23 | `HistoryService(store, clipboard, llm_rewriter)` | L323–327 | Trivial — no disk I/O at init |
| 24 | `CallAssistService(...)` | L328–334 | Trivial |
| 25 | `CallCostEstimator()` | L335 | Trivial |
| 26 | `CallSilenceProbe()` | L336 | Trivial |
| 27 | `CallAutoEnd(...)` | L337–340 | Trivial |
| 28 | `TTSService()` | L341 | **MODERATE** — checks for Silero/Kokoro library availability via `importlib.util.find_spec`; may trigger lazy imports if available. |
| 29 | `LiveSubsService(transcriber, translator)` | L342–345 | Trivial |
| 30 | `TranslationService(...)` | L346–352 | Trivial |
| 31 | `GlossaryAutoLearnService(...)` | L353–357 | Trivial |
| 32 | `HealthChecker(store, transcriber, ...)` | L358–364 | Trivial |
| 33 | `SessionTracker(data_dir)` | L365 | Trivial |
| 34 | `ErrorReporter()` | L366 | Trivial |
| 35 | `UsageTracker(data_dir)` | L367 | Trivial |
| 36 | `CostEstimator()` | L368 | Trivial |
| 37 | `AudioConverter()` | L369 | Trivial — wraps subprocess calls; no subprocess yet |
| 38 | `AudioConverter.is_ffmpeg_available()` + optional error push | L371–375 | **MODERATE** — calls `shutil.which("ffmpeg")`. Typically <1 ms, but on PATH with many entries: 5–20 ms. |
| 39 | `AutoBackupManager(store, ...)` | L376–381 | Trivial init |
| 40 | `ExportScheduler(data_dir)` | L382 | Trivial |
| 41 | `AnalyticsDashboard()` | L385 | Trivial |
| 42 | `DailyDigestGenerator()` | L386 | Trivial |
| 43 | `RecapScheduler(EmailSender.from_settings(settings), ...)` | L388–396 | **MODERATE** — `EmailSender.from_settings` reads 7 `getattr(settings, ...)` calls on Pydantic settings singleton; negligible but present. If `RECAP_EMAIL_ENABLED=True`, `_recap_scheduler.start()` spawns a background scheduling thread. Default=False. |
| 44 | Lightweight analytics helpers × 6 | L399–404 | Trivial — `QualityTrendAnalyzer`, `ActivityCalendar`, `StatsReportGenerator`, `SpeakerStatisticsAnalyzer`, `RecordingInsightsGenerator`, `KeywordCloudGenerator` |
| 45 | Utility objects × 7 | L405–415 | Trivial — `IntegrityChecker`, `HallucinationManager`, `TextComparator`, `TermExtractor`, `ReadabilityScorer`, `AudioFingerprinter`, `AutoTitleGenerator` |
| 46 | `ContextMemory(window_size=50)` | L412 | Trivial |
| 47 | `TranscriptionScorer`, `SpeechPaceAnalyzer`, `WordTimingAnalyzer` | L413–415 | Trivial |
| 48 | `EventReplayManager(persist_path)` | L416–418 | Trivial — opens no file handle |
| 49 | `WebhookManager(data_dir)` | L419 | Trivial — lazy NDJSON load |
| 50 | `SharingManager(store)` | L420 | Trivial |
| 51 | Misc managers × 9 | L421–432 | Trivial — `RecordingMerger`, `TranscriptVersionManager`, `LanguageLearningManager`, `ConfigPresetsLibrary`, `IPCThrottle`, `RequestSigner`, `PasteFormatter`, `PasteAppMemory` |
| 52 | Lightweight processors × 6 | L438–445 | Trivial — `TextAnonymizer`, `TextPostProcessor`, `TranscriptionQueue`, `EmotionDetector`, `SentimentTrendAnalyzer`, `TopicTracker`, `DataMigrator`, `AbbreviationExpander` |
| 53 | `TextProcessingService(...)` | L446–455 | Trivial |
| 54 | `ObsidianSyncManager(data_dir, event_bus)` | L456 | **MODERATE** — reads `obsidian_sync.json` from disk at init to restore last-sync timestamp. Typically <5 ms, but involves disk I/O. |
| 55 | `SpeakerManager(data_dir)` | L457 | Trivial — lazy NDJSON load |
| 56 | `PlaybackTracker`, `RecordingComparison`, `SmartVocabularyBuilder`, `MetadataEnricher`, `TimelineExporter`, `TimelineViewGenerator`, `AutoDeduplicator`, `SearchHistoryManager`, `ArchiveManager` | L460–468 | Trivial — mostly in-memory or lazy-file |
| 57 | `CallSessionStore(data_dir)` | L469 | Trivial |
| 58 | `CallSessionService(store, auto_end)` | L470–473 | Trivial |
| 59 | `AudioAnalyticsService(...)` | L474–480 | Trivial |
| 60 | Misc service objects × 5 | L481–485 | Trivial — `TemplateManager`, `FeatureFlags`, `PluginManager`, `HotwordDetector`, `ModelCacheManager` |
| 61 | `AutoGlossaryBuilder(store, data_dir, ...)` | L487–495 | **MODERATE** — `_load_cache_from_disk()` reads `auto_glossary.json`; typically <2 ms per disk hit. |
| 62 | `SemanticSearcher(data_dir, model_name, enabled)` | L497–501 | Trivial at init — model is lazy-loaded on first search (SentenceTransformer = 300 MB+ download + load if not cached). Default `enabled=False`. |
| 63 | `TelegramBridge(base_url, timeout_sec, ...)` | L503–508 | Trivial — creates `requests.Session`; no network call |
| 64 | `AppleIntegrationService(telegram_bridge)` | L510 | Trivial |
| 65 | `TextScoringService(...)` | L512–517 | Trivial |
| 66 | `AnalyticsService(...)` | L519–526 | Trivial |
| 67 | `OpenWakeWordAdapter(data_dir)` | L528 | **MODERATE** — calls `importlib.util.find_spec("openwakeword")` to detect availability; fast but involves Python import machinery. |
| 68 | `RecordingCoreService(recorder, transcriber, ..., 15 params)` | L533–550 | Trivial construction — no blocking work, but complex wiring of 15 collaborators |
| 69 | `CalendarLinker(cache_minutes=...)` | L551–553 | Trivial |
| 70 | `_auto_backup.check_and_backup()` | L555–558 | **SLOW** — **synchronous on main thread**. If backup interval has passed, performs file copy of `history.ndjson`. For large histories (e.g. 10 MB), this can block for 50–500 ms. Wrapped in `try/except` but still on init path. |
| 71 | `StartupDiagnostics(data_dir)` | L561–564 | Trivial construction |
| 72 | `HealthCheckService(store, ..., 12 params)` | L568–582 | Trivial |
| 73 | `SearchAndAnalysisService(...)` | L584–592 | Trivial |
| 74 | `startup_diagnostics.run_all_checks()` | L595–613 | **SLOW** — **synchronous on main thread**, runs 10 checks in sequence: python_version (<1 ms), required_packages (importlib.import_module × 4, ~20–80 ms cold), data_dir_writable (disk write+unlink, ~5 ms), socket_path (disk stat + optional TCP connect 0.5 s timeout, ~1–500 ms), ffmpeg (shutil.which, ~2 ms), hf_token (<1 ms), stt_model_cached (HF cache dir scan, ~5 ms), lm_studio_reachable (TCP connect with 2 s timeout, ~2–2000 ms), disk_space (shutil.disk_usage, ~1 ms), audio_devices (sounddevice.query_devices, ~10–50 ms). **Worst-case sequential total: ~2700 ms** (socket stale 500 ms + LM Studio unreachable 2000 ms + packages 80 ms + audio 50 ms + rest). |
| 75 | `DiskSpaceMonitor(settings, event_bus, data_dir).start()` | L616–621 | Async — spawns daemon thread for periodic disk check. Non-blocking. |
| 76 | `GracefulShutdownHandler(data_dir)` | L624 | Trivial |
| 77 | STT hotwords auto-seed (opt-in) | L627–636 | **MODERATE** — `seed_hotwords()` reads current settings via `cached_settings()` (TTL cache, effectively free if cached), then writes if list empty. One disk write first time. |

---

## Slow Steps (>100 ms expected in production)

### Critical — blocks IPC socket from accepting connections

| # | Step | Expected Latency | Condition |
|---|------|-----------------|-----------|
| 3 | `_init_llm_rewriter()` → `rewriter.ping()` | **up to 45 s** (LLM_TIMEOUT_SEC default) | LM Studio not running at startup |
| 74 | `startup_diagnostics.run_all_checks()` | **up to ~2700 ms** | LM Studio unreachable (2 s TCP timeout) + stale socket probe (0.5 s) |
| 70 | `_auto_backup.check_and_backup()` | **50–500 ms** | First backup or 24 h interval elapsed; proportional to history.ndjson size |
| 6 | `Transcriber` → `AudioEngine.__init__` → GigaAM warmup thread spawn | **~1–5 ms** (thread spawn only) | `STT_GIGAAM_ENABLED=True`; warmup itself is async |

### Moderate — add measurable latency

| # | Step | Expected Latency |
|---|------|-----------------|
| 16 | `_check_binary_drift_on_startup()` | **400–1600 ms** (two `dwarfdump` subprocesses) |
| 14 | Cold import of `sentry_sdk`, `error_bus`, `error_codes` | **30–100 ms** (first import) |
| 28 | `TTSService()` — `find_spec` checks | **5–20 ms** |
| 54 | `ObsidianSyncManager` — `obsidian_sync.json` disk read | **2–10 ms** |
| 61 | `AutoGlossaryBuilder` — `auto_glossary.json` disk read | **2–10 ms** |
| 67 | `OpenWakeWordAdapter` — `find_spec("openwakeword")` | **1–5 ms** |
| 77 | STT hotwords auto-seed | **5–30 ms** (settings read + possible disk write) |

---

## Parallelisation Opportunities

### Group A — Network I/O (currently sequential, can be parallel)

Steps 3, 74-lm_studio_check, and 15 all hit `localhost:1234` (LM Studio):

- **Step 3** (`rewriter.ping()`) and **Step 74 lm_studio_reachable** both do TCP connects to LM Studio. They are currently sequential. After fixing step 3 to be async (see below), the diagnostics check would duplicate work.
- **Recommendation**: Skip `_check_lm_studio_reachable()` inside `run_all_checks()` when `_llm_rewriter` is already constructed (result is already known). Saves 2 s worst-case.

### Group B — Startup diagnostics (10 sequential checks → parallel)

`run_all_checks()` runs 10 checks one after another on the main thread. All checks are independent:

```python
# Current (sequential):
checks = [
    self._check_python_version(),      # <1 ms
    self._check_required_packages(),   # 20–80 ms
    self._check_data_dir_writable(),   # 5 ms
    self._check_socket_path_available(), # 1–500 ms
    self._check_ffmpeg_available(),    # 2 ms
    self._check_huggingface_token(),   # <1 ms
    self._check_stt_model_cached(),    # 5 ms
    self._check_lm_studio_reachable(), # 2–2000 ms
    self._check_disk_space(),          # 1 ms
    self._check_audio_devices(),       # 10–50 ms
]

# Proposed (parallel with ThreadPoolExecutor):
# The two blocking checks (socket_path + lm_studio) would run concurrently.
# Wall-clock improvement: ~2500 ms → ~2100 ms (lm_studio dominates, rest <100 ms total)
```

### Group C — Binary drift check (subprocess, main thread)

Step 16 (`_check_binary_drift_on_startup`) calls `dwarfdump` twice synchronously. This should run in a daemon thread, posting the result to the error bus asynchronously. The check has no real-time dependency — a 2-second-delayed warning is fine.

---

## Lazy-Init Candidates

These objects are constructed unconditionally at startup but are only needed on first use. Making them lazy would reduce startup time and memory footprint.

| Object | Current | Impact if lazy | Risk |
|--------|---------|---------------|------|
| `SemanticSearcher` | Constructed eagerly (disabled by default but still instantiated) | Save ~1 ms + 200 MB RAM if model loaded | Low — already `enabled=False` by default; make `_semantic_searcher` a property |
| `TTSService` | Eager `find_spec` checks | Save ~20 ms on import path scan | Low — used only when TTS IPC called |
| `PluginManager` | Eager construction | Trivial; no scan at init — already lazy | None |
| `RecordingScheduler`, `RecordingChain`, `PlaybackTracker`, `TranscriptVersionManager` | Eager | ~1 ms each; no blocking I/O | Low |
| `ObsidianSyncManager` | Eager, reads `obsidian_sync.json` | Save ~10 ms disk read if Obsidian not configured | Low — check `OBSIDIAN_VAULT_PATH` setting before constructing |
| `RecapScheduler` + `EmailSender.from_settings` | Eager | Save ~5 ms; `start()` only called if `RECAP_EMAIL_ENABLED=True` | Low — construction can be gated on `RECAP_EMAIL_ENABLED` |
| `LLMOpsService`, `GlossaryService`, `STTManagementService` | Eager | Trivial | None |
| `OpenWakeWordAdapter` | Eager `find_spec` check | Save ~5 ms | Low — gate construction on `WAKE_WORD_ENGINE != "none"` |
| `AutoGlossaryBuilder` | Eager + disk read | Save ~10 ms | Low — gate on first call to `build()` |

---

## High-Priority Fixes (by impact)

### P0 — `_init_llm_rewriter()` synchronous ping (up to 45 s)

The `rewriter.ping()` call at line 659 blocks `__init__` for up to `LLM_TIMEOUT_SEC` seconds if LM Studio is not running. This is the most dangerous init cost — it delays the IPC socket becoming available.

**Current code:**
```python
rewriter = LLMRewriter(...)
if rewriter.ping():         # ← synchronous HTTP GET, up to 45 s
    logger.info(...)
else:
    logger.warning(...)
return rewriter
```

**Fix:** Remove the synchronous `ping()`. The `LLMHttpProbe` (started at step 15) already handles health detection asynchronously. The `warmup_sync` daemon thread (step 4) will also surface any connectivity issues via retries and logging. The ping is redundant.

```python
# After fix:
rewriter = LLMRewriter(...)
logger.info("LLM rewriter создан: %s @ %s (ping пропущен — LLMHttpProbe мониторит)", ...)
return rewriter
```

**Expected saving:** 0–45 s (eliminates blocking on cold start).

### P1 — `startup_diagnostics.run_all_checks()` on main thread (up to 2700 ms)

Move `run_all_checks()` to a background daemon thread. Post results to the error bus if critical. The IPC `get_diagnostics` handler already serves cached results — callers can query after a short delay.

```python
# Instead of blocking call at L595:
threading.Thread(
    target=self._run_startup_diagnostics_bg,
    name="startup-diagnostics",
    daemon=True,
).start()
```

**Expected saving:** 0–2700 ms (eliminates blocking wait for LM Studio TCP connect).

### P2 — `_auto_backup.check_and_backup()` on main thread (50–500 ms)

Wrap in a daemon thread. Backup is a background housekeeping task — no IPC handler needs to wait for it during init.

```python
# Instead of:
try:
    self._auto_backup.check_and_backup()
except Exception:
    pass

# Use:
threading.Thread(
    target=lambda: self._safe_call(self._auto_backup.check_and_backup),
    name="startup-backup",
    daemon=True,
).start()
```

**Expected saving:** 50–500 ms.

### P3 — `_check_binary_drift_on_startup()` on main thread (400–1600 ms)

Already wrapped in a settings guard (`binary_drift_check_on_startup`). Move to daemon thread:

```python
if self._settings_svc.cached_settings().get("binary_drift_check_on_startup", True):
    threading.Thread(
        target=self._check_binary_drift_on_startup,
        name="startup-binary-drift",
        daemon=True,
    ).start()
```

**Expected saving:** 400–1600 ms.

---

## Summary

The `BackendService.__init__` initialises **77 steps** with a theoretical worst-case wall-clock time of:

| Phase | Worst-case | After fixes |
|-------|-----------|-------------|
| LLM rewriter ping (P0) | ~45 000 ms | 0 ms |
| Binary drift check (P3) | ~1 600 ms | 0 ms (async) |
| Startup diagnostics (P1) | ~2 700 ms | 0 ms (async) |
| Auto backup (P2) | ~500 ms | 0 ms (async) |
| Remaining synchronous work | ~200 ms | ~200 ms |
| **Total worst-case** | **~50 000 ms** | **~200 ms** |

The four high-priority fixes collectively reduce blocking startup from up to 50 s to under 200 ms, with no functional regression — all deferred work runs in daemon threads and surfaces via the existing `error_bus` / `LLMHttpProbe` / `run_all_checks` IPC path.

Parallelising the 10 startup diagnostics checks via `ThreadPoolExecutor` provides an additional ~2.5 s speedup if P1 is implemented as sync rather than fully async.

**Background threads launched by `__init__` (current):**
1. `LLMRewriter.warmup_sync` (daemon, retries up to ~125 s total)
2. `STTRouter._warmup_bg` for GigaAM (if `STT_GIGAAM_ENABLED`)
3. `AudioEngine` STT warmup (if `stt_warmup_on_startup=True`)
4. `LLMHttpProbe` periodic health check (30 s interval)
5. `DiskSpaceMonitor` periodic disk check

All five are correctly non-blocking. The four blocking paths identified above (P0–P3) are the only ones that should be moved to daemon threads.
