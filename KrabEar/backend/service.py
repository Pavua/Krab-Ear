"""IPC backend-сервис Krab Ear.

Сервис слушает Unix socket и обрабатывает JSON-RPC-подобные команды:
- start_recording / stop_recording
- get_history_page / search_history / delete_history_item
- get_settings / set_settings
- compact_history
"""

from __future__ import annotations
from __version__ import __version__ as APP_VERSION  # noqa: E402
from backend.model_cache_manager import ModelCacheManager
from backend.hotword_detector import HotwordDetector
from backend.openwakeword_adapter import OpenWakeWordAdapter
from backend.plugin_system import PluginManager
from backend.feature_flags import FeatureFlags
from backend.template_manager import TemplateManager
from backend.search_history import SearchHistoryManager
from backend.archive_manager import ArchiveManager
from backend.timeline_view import TimelineViewGenerator
from backend.timeline_export import TimelineExporter
from backend.auto_deduplication import AutoDeduplicator
from backend.metadata_enricher import MetadataEnricher
from backend.recording_insights import RecordingInsightsGenerator
from backend.smart_vocabulary import SmartVocabularyBuilder
from backend.recording_comparison import RecordingComparison, _view_to_dict as _comparison_view_to_dict
from backend.playback_tracker import PlaybackTracker
from backend.speaker_statistics import SpeakerStatisticsAnalyzer
from backend.obsidian_sync import ObsidianSyncManager
from backend.sentiment_trends import SentimentTrendAnalyzer
from backend.transcription_queue import TranscriptionQueue
from core.emotion_detector import EmotionDetector
from core.transcription_scorer import TranscriptionScorer
from core.topic_tracker import TopicTracker
from core.text_postprocessor import TextPostProcessor
from core.text_anonymizer import TextAnonymizer
from backend.data_migrator import DataMigrator
from backend.config_presets_library import ConfigPresetsLibrary
from core.paste_formatter import PasteFormatter
from backend.language_learning import LanguageLearningManager
from core.auto_title import AutoTitleGenerator
from core.context_memory import ContextMemory
from backend.transcript_versioning import TranscriptVersionManager
from backend.sharing_manager import SharingManager
from backend.semantic_search import SemanticSearcher, keyword_fallback_search
from core.word_timing import WordTimingAnalyzer
from core.speech_pace import SpeechPaceAnalyzer
from core.readability_scorer import ReadabilityScorer
from core.abbreviation_expander import AbbreviationExpander
from core.audio_fingerprint import AudioFingerprinter
from core.hallucination_manager import HallucinationManager
from core.normalization_profiles import NormalizationProfileRegistry
from backend.webhook_manager import WebhookManager
from backend.stats_report import StatsReportGenerator
from backend.activity_calendar import ActivityCalendar
from backend.integrity_checker import IntegrityChecker
from backend.keyword_cloud import KeywordCloudGenerator
from backend.quality_trends import QualityTrendAnalyzer
from backend.daily_digest import DailyDigestGenerator
from backend.analytics_dashboard import AnalyticsDashboard
from backend.period_comparison import compare_periods as _compare_periods_fn
from core.term_extractor import TermExtractor
from core.text_comparator import TextComparator
from core.config import settings
from core.audio_converter import AudioConverter
from core.auto_glossary import AutoGlossaryBuilder
from backend.translator import Translator
from backend.translation_cache import TranslationCache
from backend.vocabulary_store import VocabularyStore
from backend.transcriber import Transcriber
from backend.state_store import StateStore
from backend.recorder import AudioRecorder
from backend.models import DEFAULT_SETTINGS
from backend.event_replay import EventReplayManager
from backend.event_bus import bus as event_bus
from backend.system_monitor import SystemMonitor
from backend.translation_service import TranslationService
from backend.glossary_auto_learn import GlossaryAutoLearnService
from backend.settings_service import SettingsService
from backend.cost_estimator import CostEstimator
from backend.usage_tracker import UsageTracker
from backend.session_tracker import SessionTracker
from backend.speaker_manager import SpeakerManager
from backend.history_service import HistoryService
from backend.error_reporter import ErrorReporter
from backend.recording_scheduler import RecordingScheduler
from backend.recording_merger import RecordingMerger
from backend.bookmarks import BookmarkManager
from backend.recording_chain import RecordingChainManager
from backend.collection_manager import CollectionManager
from backend.call_assist_service import CallAssistService
from backend.audio_analytics_service import AudioAnalyticsService
from backend.call_session_service import CallSessionService
from backend.recording_core_service import RecordingCoreService
from backend.call_session_store import CallSessionStore
from backend.live_subs_service import LiveSubsService
from backend.tts_service import TTSService
from backend.request_signing import RequestSigner
from backend.ipc_throttle import IPCThrottle
from backend.ipc_constants import (
    IPC_SOCKET_BACKLOG,
    IPC_SOCKET_TIMEOUT_SEC,
    IPC_MAX_MESSAGE_BYTES,
    IPC_SOCKET_PERMISSIONS,
)
from backend.export_scheduler import ExportScheduler
from backend.call_cost_estimator import CallCostEstimator
from backend.call_silence_probe import CallSilenceProbe
from backend.call_auto_end import CallAutoEnd
from backend.shutdown_handler import GracefulShutdownHandler
from backend.auto_backup import AutoBackupManager, AUTO_BACKUP_INTERVAL_HOURS, AUTO_BACKUP_MAX_COPIES
from backend.email_sender import EmailSender
from backend.recap_scheduler import RecapScheduler
from backend.performance_profiler import profiler as performance_profiler
from backend.paste_app_memory import PasteAppMemory
from backend.telegram_bridge import CircuitBreakerOpen, TelegramBridge
from backend.disk_monitor import DiskSpaceMonitor
from backend.observability import (
    _BREADCRUMB_EXCLUDED_METHODS,
    add_breadcrumb,
    get_release_string,
    init_sentry,
    install_signal_handlers,
)
from backend.calendar_link import CalendarLinker
from backend.privacy_audit import get_privacy_audit_logger

import argparse
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import platform
import sys
import threading
import time
from typing import Any, Callable, Optional

# Обеспечиваем корректный импорт модулей KrabEar при запуске как standalone скрипта.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger("KrabEar.Backend.Service")


class BackendService:
    """Бизнес-логика сервиса: запись, транскрибация, история и настройки."""

    def __init__(
        self,
        store: StateStore,
        recorder: AudioRecorder | None = None,
        transcriber: Transcriber | None = None,
        translator: Translator | None = None,
    ) -> None:
        self.store = store
        self.vocabulary = VocabularyStore(data_dir=store.data_dir)

        def _emit_audio_level(rms: float) -> None:
            """Callback для VU meter: эмитит событие recording.audio_level ~30 Hz."""
            event_bus.emit("recording.audio_level", {"rms": rms})

        # Use staging attr during init; after _recording_core_svc is created,
        # the 'recorder' property will proxy through to RecordingCoreService.
        object.__setattr__(self, "_recorder_init", recorder or AudioRecorder(on_audio_level=_emit_audio_level))

        # D.10a: LLM rewriter initialization (admin flag check via settings)
        self._llm_rewriter = self._init_llm_rewriter()
        # Fire background warmup if enabled in settings — pre-loads model before first dictation.
        # Wave 58: read RUNTIME settings (settings.json) instead of static DEFAULT_SETTINGS so
        # user-overridden values (e.g. rewriter_warmup_timeout_sec=60 in production) actually
        # apply. Previously hardcoded 15s default caused chronic warmup-timeout warnings on
        # cold LM Studio loads (gemma-4-26b takes 20-60s cold).
        _warmup_enabled = bool(self._get_runtime_setting("rewriter_warmup_on_startup", True))
        _warmup_timeout = float(self._get_runtime_setting("rewriter_warmup_timeout_sec", 60))
        if self._llm_rewriter is not None and _warmup_enabled:
            threading.Thread(
                target=self._llm_rewriter.warmup_sync,
                kwargs={"timeout_sec": _warmup_timeout},
                daemon=True,
            ).start()
        self._action_items_extractor = self._init_action_items_extractor()

        if transcriber is None:
            self.transcriber = Transcriber(
                llm_rewriter=self._llm_rewriter,
                settings_get=self._get_runtime_setting,
            )
        else:
            self.transcriber = transcriber
            if self._llm_rewriter is not None:
                if hasattr(transcriber, "engine"):
                    if getattr(transcriber.engine, "_llm_rewriter", None) is None:
                        transcriber.engine._llm_rewriter = self._llm_rewriter
                    transcriber.engine._settings_get = self._get_runtime_setting

        self.translator = translator or Translator()
        # Персистентный кэш переводов — инжектируем в translator сразу после создания
        # чтобы успешные переводы пережили перезапуск процесса. W1190 (wire fix).
        self._translation_cache = TranslationCache(data_dir=str(store.data_dir))
        self.translator._translation_cache = self._translation_cache
        self._start_time: float = time.monotonic()
        self._settings_svc = SettingsService(store=self.store)
        # Hot-propagate LLMRewriter settings changes without restart.
        # Covers: lm_studio_api_key, llm_model, llm_base_url.
        _rewriter_ref = self._llm_rewriter
        if _rewriter_ref is not None:
            def _on_settings_saved(old: dict, new: dict) -> None:
                new_key = str(new.get("lm_studio_api_key", ""))
                if new_key != str(old.get("lm_studio_api_key", "")):
                    _rewriter_ref.set_api_key(new_key)
                new_model = str(new.get("llm_model", ""))
                if new_model and new_model != str(old.get("llm_model", "")):
                    _rewriter_ref.set_model(new_model)
                new_url = str(new.get("llm_base_url", ""))
                if new_url and new_url != str(old.get("llm_base_url", "")):
                    _rewriter_ref.set_base_url(new_url)
            self._settings_svc.register_after_save_hook(_on_settings_saved)

        # Best-effort STT warmup — pre-loads Whisper model in background before
        # first dictation, eliminating the 1–3 s cold-start latency the user feels
        # as "первая диктовка медленнее остальных".
        # Opt-out: set stt_warmup_on_startup=False in settings.
        # Wave 58 follow-up: read runtime setting (same fix pattern as rewriter warmup
        # above on line 187 — DEFAULT_SETTINGS is static, runtime override in settings.json
        # was previously ignored).
        _stt_warmup_enabled = bool(self._get_runtime_setting("stt_warmup_on_startup", False))
        if (_stt_warmup_enabled
                and hasattr(self.transcriber, "engine")
                and hasattr(self.transcriber.engine, "warmup")
                and callable(getattr(self.transcriber.engine, "warmup"))):
            threading.Thread(
                target=self.transcriber.engine.warmup,
                daemon=True,
                name="stt-warmup",
            ).start()
            logger.info("STT startup warmup запущен в background thread")

        # Phase B.1 — error bus + active LLM probe
        from backend.error_bus import ErrorBus
        from backend.error_codes import ERROR_REGISTRY
        from backend.llm_probe import LLMHttpProbe
        from backend import error_actions as _error_actions  # noqa: F401  side-effect: registers ACTION_HANDLERS for ErrorActionRouter
        try:
            import sentry_sdk as _sentry_sdk
        except ImportError:
            _sentry_sdk = None

        self._error_bus = ErrorBus(
            event_bus=event_bus,
            registry=ERROR_REGISTRY,
            sentry_client=_sentry_sdk,
            default_dedupe_window_sec=30.0,
            ring_buffer_size=200,
        )

        # Wire error_bus into rewriter so it can push timeout/connection/etc errors
        if self._llm_rewriter is not None:
            self._llm_rewriter._error_bus = self._error_bus

        # Wire error_bus into transcriber for diarization.no_token and related push
        if self.transcriber is not None:
            self.transcriber._error_bus = self._error_bus

        # Wire error_bus into mlx_subprocess module for stt.mlx_watchdog_hang push
        try:
            import core.mlx_subprocess as _mlx_sub  # noqa: PLC0415
            _mlx_sub._error_bus = self._error_bus
        except Exception:  # noqa: BLE001
            pass

        self._llm_probe: LLMHttpProbe | None = None
        if self._llm_rewriter is not None:
            _settings_dict = self._settings_svc.cached_settings()
            self._llm_probe = LLMHttpProbe(
                rewriter=self._llm_rewriter,
                error_bus=self._error_bus,
                event_bus=event_bus,
                settings_provider=lambda: self._settings_svc.cached_settings(),
                base_interval_sec=float(_settings_dict.get("llm_probe_interval_sec", 30.0)),
            )
            if _settings_dict.get("llm_probe_enabled", True):
                self._llm_probe.start()

        # Startup binary-drift self-check (Option B).
        # Compares dwarfdump UUIDs of bundle vs runtime KrabEarAgent binaries.
        # Fires agent.binary_drift on the error_bus when they diverge.
        # Opt-out via setting binary_drift_check_on_startup=False.
        if self._settings_svc.cached_settings().get("binary_drift_check_on_startup", True):
            self._check_binary_drift_on_startup()

        self._system_monitor = SystemMonitor()
        self._clipboard_history: list[dict] = []
        self._collections = CollectionManager(store=self.store)
        self._norm_profiles = NormalizationProfileRegistry(data_dir=self.store.data_dir)
        self._chains = RecordingChainManager(store=self.store)
        self._bookmarks = BookmarkManager(data_dir=self.store.data_dir)
        self._recording_scheduler = RecordingScheduler(data_dir=self.store.data_dir)
        self._history = HistoryService(
            store=self.store,
            clipboard_history=self._clipboard_history,
            llm_rewriter=self._llm_rewriter,
        )
        self._call_assist = CallAssistService(
            store=self.store,
            recorder=self.recorder,
            transcriber=self.transcriber,
            reset_preview_fn=lambda: self._recording_core_svc.reset_preview_state(),
            start_preview_fn=lambda qp: self._recording_core_svc.start_preview_worker(qp),
        )
        self._call_cost_estimator = CallCostEstimator()
        self._call_silence_probe = CallSilenceProbe()
        self._call_auto_end = CallAutoEnd(
            cost_estimator=self._call_cost_estimator,
            silence_probe=self._call_silence_probe,
        )
        self._tts = TTSService()
        self._live_subs = LiveSubsService(
            transcriber=self.transcriber,
            translator=self.translator,
        )
        self._translation = TranslationService(
            translator=self.translator,
            store=self.store,
            cached_settings=self._cached_settings,
            invalidate_settings_cache=self._invalidate_settings_cache,
            vocabulary_store=self.vocabulary,
        )
        self._glossary_auto_learn = GlossaryAutoLearnService(
            store=self.store,
            cached_settings=self._cached_settings,
            invalidate_settings_cache=self._invalidate_settings_cache,
        )
        from backend.health_checker import HealthChecker
        self._health_checker = HealthChecker(
            store=self.store,
            transcriber=self.transcriber,
            llm_rewriter=self._llm_rewriter,
            start_time=self._start_time,
        )
        self._session_tracker = SessionTracker(data_dir=self.store.data_dir)
        self._error_reporter = ErrorReporter()
        self._usage_tracker = UsageTracker(data_dir=self.store.data_dir)
        self._cost_estimator = CostEstimator()
        self._audio_converter = AudioConverter()
        # Wave 64: stt.gigaam.ffmpeg_missing — push once at startup if ffmpeg absent.
        if not self._audio_converter.is_ffmpeg_available():
            self._push_startup_error(
                "stt.gigaam.ffmpeg_missing",
                "ffmpeg not found in PATH — REST STT disabled",
            )
        self._auto_backup = AutoBackupManager(
            store=self.store,
            interval_hours=AUTO_BACKUP_INTERVAL_HOURS,
            max_copies=AUTO_BACKUP_MAX_COPIES,
            enabled=settings.AUTO_BACKUP_ENABLED,
        )
        self._export_scheduler = ExportScheduler(data_dir=self.store.data_dir)
        # Note: _transcription_counter is now a property that proxies to
        # _transcription_counter_ref[0] (set below after RecordingCoreService init).
        self._analytics_dashboard = AnalyticsDashboard()
        self._daily_digest = DailyDigestGenerator()
        # Recap email scheduler (opt-in via RECAP_EMAIL_ENABLED setting)
        self._recap_scheduler = RecapScheduler(
            email_sender=EmailSender.from_settings(settings),
            digest_generator=self._daily_digest,
            store=self.store,
            data_dir=self.store.data_dir,
            recap_email_to=settings.RECAP_EMAIL_TO,
            recap_time_hour=settings.RECAP_TIME_HOUR,
            enabled=settings.RECAP_EMAIL_ENABLED,
        )
        if settings.RECAP_EMAIL_ENABLED:
            self._recap_scheduler.start()
        self._quality_trends = QualityTrendAnalyzer()
        self._activity_calendar = ActivityCalendar()
        self._stats_report = StatsReportGenerator()
        self._speaker_statistics = SpeakerStatisticsAnalyzer()
        self._recording_insights = RecordingInsightsGenerator()
        self._keyword_cloud_gen = KeywordCloudGenerator()
        self._integrity_checker = IntegrityChecker()
        self._hallucination_manager = HallucinationManager(data_dir=self.store.data_dir)
        self._text_comparator = TextComparator()
        self._term_extractor = TermExtractor()
        self._readability_scorer = ReadabilityScorer()
        self._audio_fingerprinter = AudioFingerprinter()
        self._auto_title_generator = AutoTitleGenerator()
        self._context_memory = ContextMemory(window_size=50)
        self._transcription_scorer = TranscriptionScorer()
        self._speech_pace_analyzer = SpeechPaceAnalyzer()
        self._word_timing_analyzer = WordTimingAnalyzer()
        self._event_replay = EventReplayManager(
            persist_path=self.store.data_dir / "event_replay.ndjson",
        )
        self._webhook_manager = WebhookManager(data_dir=self.store.data_dir)
        self._sharing = SharingManager(store=self.store)
        self._merger = RecordingMerger()
        self._transcript_versioning = TranscriptVersionManager(data_dir=self.store.data_dir)
        self._language_learning = LanguageLearningManager()
        self._config_presets = ConfigPresetsLibrary(data_dir=self.store.data_dir)
        # IPC throttle — защита от злоупотребления тяжёлыми методами.
        # Отключается через KRAB_EAR_IPC_THROTTLE_ENABLED=false.
        self._ipc_throttle = IPCThrottle() if settings.IPC_THROTTLE_ENABLED else None
        # IPC request signing — HMAC-SHA256 верификация входящих запросов.
        # Включается через KRAB_EAR_IPC_SIGNING_ENABLED=true.
        self._request_signer: RequestSigner | None = (
            RequestSigner() if settings.IPC_SIGNING_ENABLED else None
        )
        self._paste_formatter = PasteFormatter(data_dir=self.store.data_dir)
        self._paste_app_memory = PasteAppMemory(
            data_dir=self.store.data_dir,
            enabled=settings.PASTE_APP_MEMORY_ENABLED,
        )
        self._text_anonymizer = TextAnonymizer()
        self._text_postprocessor = TextPostProcessor()
        self._transcription_queue = TranscriptionQueue()
        # W1182 F3 HIGH fix: wire a background dequeue worker so enqueued jobs
        # are actually processed.  Previously process_next() had NO caller.
        self._tq_shutdown_event = threading.Event()
        _tq_poll_interval = float(
            self._get_runtime_setting("transcription_queue_poll_interval_sec", 2.0)
        )
        self._tq_worker_thread = threading.Thread(
            target=self._run_transcription_queue_worker,
            kwargs={"poll_interval_sec": _tq_poll_interval},
            daemon=True,
            name="tq-dequeue-worker",
        )
        self._tq_worker_thread.start()
        logger.info(
            "TranscriptionQueue dequeue worker started (poll_interval=%.1fs)",
            _tq_poll_interval,
        )
        self._emotion_detector = EmotionDetector()
        self._sentiment_trends = SentimentTrendAnalyzer(detector=self._emotion_detector)
        self._topic_tracker = TopicTracker()
        self._data_migrator = DataMigrator()
        self._abbreviation_expander = AbbreviationExpander(data_dir=self.store.data_dir)
        self._text_processing_svc = TextProcessingService(
            readability_scorer=self._readability_scorer,
            transcription_scorer=self._transcription_scorer,
            emotion_detector=self._emotion_detector,
            text_comparator=self._text_comparator,
            abbreviation_expander=self._abbreviation_expander,
            text_postprocessor=self._text_postprocessor,
            store=self.store,
            llm_rewriter=self._llm_rewriter,
        )
        self._obsidian_sync = ObsidianSyncManager(data_dir=self.store.data_dir, event_bus=event_bus)
        self._speaker_manager = SpeakerManager(data_dir=self.store.data_dir)
        # Wire speaker_manager into HistoryService for name resolution during exports
        self._history._speaker_manager = self._speaker_manager
        self._playback_tracker = PlaybackTracker(data_dir=self.store.data_dir)
        self._recording_comparison = RecordingComparison()
        self._smart_vocabulary = SmartVocabularyBuilder()
        self._metadata_enricher = MetadataEnricher()
        self._timeline_exporter = TimelineExporter()
        self._timeline_view = TimelineViewGenerator()
        self._auto_deduplicator = AutoDeduplicator()
        self._search_history = SearchHistoryManager(data_dir=self.store.data_dir)
        self._archive_manager = ArchiveManager(store=self.store)
        self._call_session_store = CallSessionStore(data_dir=self.store.data_dir)
        self._call_session_service = CallSessionService(
            store=self._call_session_store,
            auto_end=self._call_auto_end,
        )
        self._audio_analytics_svc = AudioAnalyticsService(
            audio_converter=self._audio_converter,
            quality_trends=self._quality_trends,
            audio_fingerprinter=self._audio_fingerprinter,
            word_timing_analyzer=self._word_timing_analyzer,
            store=self.store,
        )
        self._template_manager = TemplateManager(data_dir=self.store.data_dir)
        self._feature_flags = FeatureFlags(data_dir=self.store.data_dir)
        self._plugin_manager = PluginManager(data_dir=self.store.data_dir)
        self._hotword_detector = HotwordDetector(data_dir=self.store.data_dir)
        self._model_cache_manager = ModelCacheManager()
        # Auto-Glossary — автоматический глоссарий из истории транскрибаций
        self._auto_glossary = AutoGlossaryBuilder(
            store=self.store,
            data_dir=self.store.data_dir,
            refresh_hours=float(
                self._settings_svc.cached_settings().get(
                    "auto_glossary_refresh_hours", settings.AUTO_GLOSSARY_REFRESH_HOURS
                )
            ),
        )
        # Семантический поиск (opt-in, lazy model load)
        self._semantic_searcher = SemanticSearcher(
            data_dir=self.store.data_dir,
            model_name=settings.SEMANTIC_SEARCH_MODEL,
            enabled=settings.SEMANTIC_SEARCH_ENABLED,
        )
        # W1261: late-inject semantic_searcher into ArchiveManager so archived
        # items are removed from the embedding index (W1255 F1+F3).
        self._archive_manager._semantic_searcher = self._semantic_searcher
        # Telegram Bridge — мост Krab Ear → main Krab userbot.
        self._telegram_bridge = TelegramBridge(
            base_url=settings.TELEGRAM_BRIDGE_URL,
            timeout_sec=settings.TELEGRAM_BRIDGE_TIMEOUT_SEC,
            circuit_fail_threshold=settings.TELEGRAM_BRIDGE_CB_FAIL_THRESHOLD,
            circuit_reset_sec=settings.TELEGRAM_BRIDGE_CB_RESET_SEC,
        )
        # openWakeWord adapter (default disabled via WAKE_WORD_ENGINE setting)
        self._oww_adapter = OpenWakeWordAdapter(data_dir=self.store.data_dir)
        # Wave 172: RecordingCoreService owns all recording lifecycle, preview worker,
        # transcription pipeline, and async job tracking.
        self._transcription_counter_ref: list[int] = [0]
        self._last_stt_engine_ref: list = [None]
        self._recording_core_svc = RecordingCoreService(
            recorder=self.recorder,
            transcriber=self.transcriber,
            translator=self.translator,
            store=self.store,
            vocabulary=self.vocabulary,
            settings_svc=self._settings_svc,
            llm_rewriter=self._llm_rewriter,
            auto_glossary=self._auto_glossary,
            semantic_searcher=self._semantic_searcher,
            context_memory=self._context_memory,
            clipboard_history=self._clipboard_history,
            auto_backup=self._auto_backup,
            session_tracker=self._session_tracker,
            action_items_extractor=self._action_items_extractor,
            transcription_counter_ref=self._transcription_counter_ref,
            last_stt_engine_ref=self._last_stt_engine_ref,
            auto_deduplicator=self._auto_deduplicator,  # W1247: wire dedup into recording flow
        )
        # Wire error_bus into recording_core_svc for stt.transcribe_failed push (W1177)
        self._recording_core_svc._error_bus = self._error_bus

        self._calendar_linker = CalendarLinker(
            cache_minutes=int(settings.CALENDAR_LINK_CACHE_MIN)
        )
        # Проверяем авто-бэкап при старте
        try:
            self._auto_backup.check_and_backup()
        except Exception:
            pass

        # Диагностика при старте
        from backend.startup_diagnostics import StartupDiagnostics
        self._startup_diagnostics = StartupDiagnostics(
            data_dir=self.store.data_dir,
        )
        # W1181 F3 MED: wire HealthCheckService — делегирует 6 IPC-обработчиков диагностики.
        # Инициализируется здесь, после _startup_diagnostics и _integrity_checker.
        from backend.health_check_service import HealthCheckService
        self._health_check_svc = HealthCheckService(
            store=self.store,
            health_checker=self._health_checker,
            startup_diagnostics=self._startup_diagnostics,
            integrity_checker=self._integrity_checker,
            llm_probe=getattr(self, "_llm_probe", None),
            metrics_collector=getattr(self, "_metrics_collector", None),
            transcriber=self.transcriber,
            llm_rewriter=self._llm_rewriter,
            settings_svc=self._settings_svc,
            start_time=self._start_time,
            app_version=APP_VERSION,
            recorder=self.recorder,
            last_stt_engine_ref=self._last_stt_engine_ref,
        )
        logger.info("Krab Ear backend version %s starting up", APP_VERSION)
        try:
            _startup_report = self._startup_diagnostics.run_all_checks()
            if _startup_report.status == "critical":
                logger.error(
                    "Startup diagnostics CRITICAL — errors: %s",
                    "; ".join(_startup_report.errors),
                )
            elif _startup_report.status == "degraded":
                logger.warning(
                    "Startup diagnostics DEGRADED — warnings: %s",
                    "; ".join(_startup_report.warnings),
                )
            else:
                logger.info(
                    "Startup diagnostics OK (%.0f ms, %d checks passed)",
                    _startup_report.startup_time_ms,
                    len(_startup_report.checks),
                )
        except Exception:
            logger.exception("Startup diagnostics завершились с исключением")

        # Мониторинг дискового пространства
        self._disk_monitor = DiskSpaceMonitor(
            settings=settings,
            event_bus=event_bus,
            data_dir=self.store.data_dir,
        )
        self._disk_monitor.start()

        # Обработчик корректного завершения (регистрация сигналов — через register())
        self._shutdown_handler = GracefulShutdownHandler(data_dir=self.store.data_dir)

        # Авто-сид дефолтных STT hotwords при первом запуске (только если список пуст)
        if settings.STT_AUTO_SEED_HOTWORDS:
            try:
                from backend.default_hotwords import seed_hotwords as _seed_hotwords
                _seeded_count = _seed_hotwords(self._settings_svc, only_if_empty=True)
                if _seeded_count > 0:
                    logger.info(
                        "STT hotwords: авто-сид %d дефолтных брендов/терминов", _seeded_count
                    )
            except Exception:
                logger.exception("STT hotwords: ошибка авто-сида")

    # ------------------------------------------------------------------
    # TranscriptionQueue dequeue worker (W1182 F3 HIGH fix)
    # ------------------------------------------------------------------

    def _run_transcription_queue_worker(self, poll_interval_sec: float = 2.0) -> None:
        """Background worker thread: dequeues and processes pending TranscriptionQueue jobs.

        Polls process_next() every *poll_interval_sec* seconds.  When a job is
        available it transcribes the file using self.transcriber.transcribe()
        while holding mlx_lock (W63 rule), then marks it completed or failed.
        Exits cleanly when _tq_shutdown_event is set.
        """
        from core.mlx_lock import mlx_lock  # local import avoids circular at module level

        logger.info("TranscriptionQueue dequeue worker running")
        while not self._tq_shutdown_event.wait(timeout=poll_interval_sec):
            try:
                job_dict = self._transcription_queue.process_next()
            except Exception:
                logger.exception("TranscriptionQueue: unexpected error in process_next()")
                continue

            if job_dict is None:
                # Queue empty — sleep already handled by wait() above
                continue

            job_id = job_dict.get("job_id", "")
            file_path = job_dict.get("file_path", "")
            logger.info(
                "TranscriptionQueue: processing job %s file=%r",
                job_id,
                file_path,
            )
            try:
                with mlx_lock():
                    result = self.transcriber.transcribe(file_path)
                self._transcription_queue.mark_completed(job_id, result)
                logger.info("TranscriptionQueue: job %s completed", job_id)
            except Exception as exc:  # noqa: BLE001
                error_msg = str(exc)
                logger.error(
                    "TranscriptionQueue: job %s failed: %s",
                    job_id,
                    error_msg,
                    exc_info=True,
                )
                self._transcription_queue.mark_failed(job_id, error_msg)

        logger.info("TranscriptionQueue dequeue worker stopped")

    def _init_llm_rewriter(self):
        """Создаёт LLMRewriter если settings.LLM_ENABLED. Возвращает None иначе."""
        if not settings.LLM_ENABLED:
            return None

        try:
            from backend.llm_rewriter import LLMRewriter
            _default_timeout = float(settings.LLM_TIMEOUT_SEC)
            rewriter = LLMRewriter(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                model=settings.LLM_MODEL,
                timeout_sec=_default_timeout,
                circuit_fail_threshold=settings.LLM_CIRCUIT_FAIL_THRESHOLD,
                circuit_initial_reset_sec=settings.LLM_CIRCUIT_INITIAL_RESET_SEC,
                circuit_max_reset_sec=settings.LLM_CIRCUIT_MAX_RESET_SEC,
                idle_keepalive_enabled=bool(self._get_runtime_setting("llm_idle_keepalive_enabled", getattr(settings, "LLM_IDLE_KEEPALIVE_ENABLED", False))),
                runtime_timeout_provider=lambda: self._get_runtime_setting(
                    "llm_timeout_sec", _default_timeout
                ),
            )
            if rewriter.ping():
                logger.info(
                    "LLM rewriter инициализирован: %s @ %s",
                    settings.LLM_MODEL,
                    settings.LLM_BASE_URL,
                )
            else:
                logger.warning(
                    "LLM rewriter не отвечает на ping (%s), будет circuit-break'нут при первом rewrite",
                    settings.LLM_BASE_URL,
                )
            return rewriter
        except Exception as exc:
            logger.exception("Не удалось инициализировать LLM rewriter: %s", exc)
            return None

    def _init_action_items_extractor(self):
        """Создаёт ActionItemsExtractor если LLM_ENABLED. Разделяет circuit breaker с LLMRewriter."""
        if not settings.LLM_ENABLED:
            return None
        try:
            from backend.action_items_extractor import ActionItemsExtractor
            return ActionItemsExtractor(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                model=settings.LLM_MODEL,
                timeout_sec=max(settings.LLM_TIMEOUT_SEC * 4, 20.0),
                circuit_fail_threshold=settings.LLM_CIRCUIT_FAIL_THRESHOLD,
                circuit_initial_reset_sec=settings.LLM_CIRCUIT_INITIAL_RESET_SEC,
                circuit_max_reset_sec=settings.LLM_CIRCUIT_MAX_RESET_SEC,
            )
        except Exception as exc:
            logger.exception("Не удалось инициализировать ActionItemsExtractor: %s", exc)
            return None

    def _cached_settings(self) -> dict[str, Any]:
        """Делегирует к SettingsService. Обратная совместимость."""
        return self._settings_svc.cached_settings()

    def _invalidate_settings_cache(self) -> None:
        """Делегирует к SettingsService. Обратная совместимость."""
        self._settings_svc.invalidate_cache()

    def _get_runtime_setting(self, key: str, default: Any) -> Any:
        """Callback для AudioEngine: читает runtime toggle из StateStore.

        Используется для проверки llm_rewrite_enabled на каждой транскрипции.
        """
        try:
            return self._cached_settings().get(key, default)
        except Exception:
            return default

    def _trigger_auto_extract_action_items(
        self, item_id: str, text: str, language: str, duration_sec: float
    ) -> None:
        """Авто-извлечение в daemon-потоке."""
        import threading as _ait

        def _run() -> None:
            try:
                logger.info("Auto-extract action items: item=%s dur=%.1fs", item_id, duration_sec)
                result = self._action_items_extractor.extract(text, language=language)
                if result["action_items"] or result["decisions"] or result["questions"]:
                    self.store.update_history_item_action_items(
                        item_id=item_id,
                        action_items=result["action_items"],
                        decisions=result["decisions"],
                        questions=result["questions"],
                    )
            except Exception:
                logger.exception("Auto-extract action items failed for item=%s", item_id)

        _ait.Thread(target=_run, daemon=True, name=f"ai-{item_id[:8]}").start()
    # ------------------------------------------------------------------
    # Semantic search IPC handlers
    # ------------------------------------------------------------------

    def _handle_semantic_search(self, params: dict) -> dict:
        """Семантический поиск по истории транскрипций через embeddings.

        Params:
            query     — поисковый запрос (строка, обязательный)
            top_k     — максимальное число результатов (int, default 10)
            fallback  — bool, использовать keyword fallback если модель недоступна (default True)
        Returns:
            {"results": [{"id": str, "score": float}], "mode": "semantic"|"keyword"|"disabled"}
        """
        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError("Параметр query обязателен")
        top_k = int(params.get("top_k", 10))
        top_k = max(1, min(top_k, 100))
        use_fallback = bool(params.get("fallback", True))

        if not self._semantic_searcher.is_enabled:
            if use_fallback:
                items = [{"id": it.id, "text": it.text}
                         for it in self.store._load_active_items_with_lock()]
                results = keyword_fallback_search(query, items, top_k=top_k)
                return {"results": results, "mode": "keyword", "reason": "semantic_disabled"}
            return {"results": [], "mode": "disabled"}

        results = self._semantic_searcher.search(query, top_k=top_k)
        if not results and use_fallback:
            items = [{"id": it.id, "text": it.text}
                     for it in self.store._load_active_items_with_lock()]
            results = keyword_fallback_search(query, items, top_k=top_k)
            return {"results": results, "mode": "keyword", "reason": "model_unavailable"}

        return {"results": results, "mode": "semantic"}

    def _handle_semantic_search_status(self, params: dict) -> dict:
        """Возвращает статус семантического поиска.

        Returns:
            {"enabled": bool, "model_loaded": bool, "model_name": str,
             "model_error": str|null, "indexed_count": int}
        """
        return self._semantic_searcher.status()

    def _handle_semantic_search_reindex(self, params: dict) -> dict:
        """Переиндексирует всю историю транскрипций.

        Params:
            force — bool, перестроить индекс с нуля (default False)
        Returns:
            {"indexed": int, "skipped": int, "errors": int}
        """
        if not self._semantic_searcher.is_enabled:
            return {"indexed": 0, "skipped": 0, "errors": 0, "reason": "semantic_search_disabled"}
        force = bool(params.get("force", False))
        items = [{"id": it.id, "text": it.text}
                 for it in self.store._load_active_items_with_lock()]
        result = self._semantic_searcher.index_all(items, force=force)
        return result

    def close(self) -> None:
        """Graceful shutdown: останавливает фоновые потоки (LLM probe и др.).

        Идемпотентен — безопасно вызывать несколько раз. Используется в
        signal handler run_server() и в finally serve_forever().
        """
        probe = getattr(self, "_llm_probe", None)
        if probe is not None:
            try:
                probe.stop()
            except Exception:
                logger.exception("LLMHttpProbe.stop() raised during close()")

        # Stop TranscriptionQueue dequeue worker (W1184)
        tq_event = getattr(self, "_tq_shutdown_event", None)
        if tq_event is not None:
            tq_event.set()
        tq_thread = getattr(self, "_tq_worker_thread", None)
        if tq_thread is not None and tq_thread.is_alive():
            tq_thread.join(timeout=3.0)

    # ------------------------------------------------------------------ #
    # Backwards-compatible proxy properties for Wave 172 migration         #
    # Tests and any code that read/write these attrs on BackendService     #
    # are routed through to the RecordingCoreService.                     #
    # ------------------------------------------------------------------ #

    @property
    def recorder(self):  # type: ignore[override]
        """Proxy recorder through RecordingCoreService so test monkey-patches propagate."""
        svc = self.__dict__.get("_recording_core_svc")
        if svc is not None:
            return svc.recorder
        return self.__dict__.get("_recorder_init")

    @recorder.setter
    def recorder(self, value):
        svc = self.__dict__.get("_recording_core_svc")
        if svc is not None:
            svc.recorder = value
        object.__setattr__(self, "_recorder_init", value)

    @property
    def _clipboard_history(self) -> list:
        svc = object.__getattribute__(self, "_recording_core_svc") if "_recording_core_svc" in self.__dict__ else None
        if svc is not None:
            return svc._clipboard_history
        return object.__getattribute__(self, "_clipboard_history_init")

    @_clipboard_history.setter
    def _clipboard_history(self, value: list):
        # During init, _recording_core_svc doesn't exist yet — store in staging attr.
        svc = self.__dict__.get("_recording_core_svc")
        if svc is not None:
            # Propagate: clear and extend so the service list stays the same object.
            svc_list = svc._clipboard_history
            svc_list.clear()
            svc_list.extend(value)
        else:
            # Pre-init: store in staging attr, will be passed to RecordingCoreService.
            object.__setattr__(self, "_clipboard_history_init", value)

    @property
    def _rt_partial(self):
        return self._recording_core_svc._rt_partial

    @_rt_partial.setter
    def _rt_partial(self, value):
        self._recording_core_svc._rt_partial = value

    @property
    def _rt_session_id(self) -> str:
        return self._recording_core_svc._rt_session_id

    @_rt_session_id.setter
    def _rt_session_id(self, value: str):
        self._recording_core_svc._rt_session_id = value

    @property
    def _transcription_counter(self) -> int:
        return self._transcription_counter_ref[0]

    @_transcription_counter.setter
    def _transcription_counter(self, value: int):
        self._transcription_counter_ref[0] = value

    @property
    def _last_stt_engine(self):
        return self._last_stt_engine_ref[0]

    @_last_stt_engine.setter
    def _last_stt_engine(self, value):
        self._last_stt_engine_ref[0] = value

    @property
    def _preview_updated_at(self) -> float:
        return self._recording_core_svc._preview_updated_at

    @_preview_updated_at.setter
    def _preview_updated_at(self, value: float):
        self._recording_core_svc._preview_updated_at = value

    @property
    def _preview_lock(self):
        return self._recording_core_svc._preview_lock

    @property
    def _preview_error_count(self) -> int:
        return self._recording_core_svc._preview_error_count

    @_preview_error_count.setter
    def _preview_error_count(self, value: int):
        self._recording_core_svc._preview_error_count = value

    @property
    def _preview_error_last_reset_ts(self) -> float | None:
        return self._recording_core_svc._preview_error_last_reset_ts

    @_preview_error_last_reset_ts.setter
    def _preview_error_last_reset_ts(self, value: float | None):
        self._recording_core_svc._preview_error_last_reset_ts = value

    @property
    def _list_audio_inputs(self):  # type: ignore[override]
        return self._recording_core_svc._list_audio_inputs

    @_list_audio_inputs.setter
    def _list_audio_inputs(self, value):
        self._recording_core_svc._list_audio_inputs = value

    # ------------------------------------------------------------------
    # Critical static helpers used directly in handle_request and
    # elsewhere in service.py. Removed from monolith during Wave 172
    # extraction — re-added here so service.py remains self-consistent.
    # ------------------------------------------------------------------

    @staticmethod
    def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        """Нормализует bool-поля из UI/JSON с поддержкой строковых значений."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False
        return default

    @staticmethod
    def _coerce_bounded(value: Any, default: "int | float", min_value: "int | float", max_value: "int | float") -> "int | float":
        """Нормализует числовое значение в допустимый диапазон. Тип определяется default."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Обрабатывает один JSON-запрос и возвращает JSON-ответ."""
        request_id = payload.get("id")
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, "invalid_params", "Параметр params должен быть объектом")

        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ping": self._handle_ping,  # VERIFIED: called from Swift (BackendSupervisor)
            "start_recording": self._handle_start_recording,  # VERIFIED: called from Swift (main)
            "stop_recording": self._handle_stop_recording,  # VERIFIED: called from Swift (main)
            "get_recording_state": self._handle_get_recording_state,  # VERIFIED: called from Swift (main, HistoryPanel)
            "start_call_assist": self._call_assist.handle_start,  # VERIFIED: called from Swift (HistoryPanel)
            "stop_call_assist": self._call_assist.handle_stop,  # VERIFIED: called from Swift (HistoryPanel)
            "get_call_assist_state": self._call_assist.handle_get_state,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_diagnostics": self._call_assist.handle_diagnostics,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_summary": self._call_assist.handle_summary,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_quick_phrase": self._call_assist.handle_quick_phrase,  # VERIFIED: called from Swift (HistoryPanel)
            "list_call_assist_quick_phrases": self._call_assist.handle_list_quick_phrases,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_cost_estimate": self._call_assist.handle_cost_estimate,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline": self._call_assist.handle_timeline,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_stats": self._call_assist.handle_timeline_stats,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_summary": self._call_assist.handle_timeline_summary,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_export": self._call_assist.handle_timeline_export,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_clear": self._call_assist.handle_timeline_clear,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_to_history": self._call_assist.handle_timeline_to_history,  # VERIFIED: called from Swift (HistoryPanel)
            "list_audio_inputs": self._handle_list_audio_inputs,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_page": self._history.handle_get_history_page,  # VERIFIED: called from Swift (HistoryPanel)
            "search_history": self._history.handle_search_history,  # VERIFIED: called from Swift (HistoryPanel)
            "fuzzy_search": self._history.handle_fuzzy_search,  # нечёткий поиск по истории транскрипций
            "search_with_highlights": self._history.handle_search_with_highlights,  # поиск с подсветкой совпадений в результатах
            "search_by_speaker": self._history.handle_search_by_speaker,
            "delete_history_item": self._history.handle_delete_history_item,  # VERIFIED: called from Swift (HistoryPanel)
            "set_paste_status": self._handle_set_paste_status,  # VERIFIED: called from Swift (main)
            "get_settings": self._settings_svc.handle_get_settings,  # VERIFIED: called from Swift (main)
            "set_settings": self._settings_svc.handle_set_settings,  # VERIFIED: called from Swift (main)
            "compact_history": self._history.handle_compact_history,  # VERIFIED: called from Swift (main, HistoryPanel)
            "add_history_item": self._history.handle_add_history_item,  # VERIFIED: called from Swift (main, HistoryPanel)
            "transcribe_paths": self._handle_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
            "transcribe_paths_async": self._handle_transcribe_paths_async,  # PR #14: фоновый job + прогресс
            "get_transcribe_progress": self._handle_get_transcribe_progress,  # PR #14: опрос прогресса job'а
            "cancel_transcribe_job": self._handle_cancel_transcribe_job,  # PR #14: запрос отмены job'а
            "preview_transcribe_paths": self._handle_preview_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
            "translate_text": self._translation.handle_translate_text,  # VERIFIED: called from Swift (main, HistoryPanel)
            "translate_selection": self._translation.handle_translate_selection,  # Phase 2A: selection-translate workflow
            "get_diagnostics": self._handle_get_diagnostics,  # диагностика: system, stt, llm, history, settings_cache
            "set_translation_glossary_item": self._translation.handle_set_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
            # VERIFIED: called from Swift (HistoryPanel)
            "remove_translation_glossary_item": self._translation.handle_remove_translation_glossary_item,
            "get_glossary_suggestions": self._translation.handle_get_glossary_suggestions,  # авто-обучение глоссария: предлагает пары source→target из истории
            "clear_translation_cache": self._handle_clear_translation_cache,  # очистить персистентный LRU-кэш переводов (память + файл)
            "suggest_medical_glossary_terms": self._glossary_auto_learn.handle_suggest_medical_glossary_terms,  # мед. домен auto-learn: предлагает пары ES↔RU из истории переводов
            "apply_glossary_suggestions": self._glossary_auto_learn.handle_apply_glossary_suggestions,  # применяет выбранные мед. термины в translation_glossary
            "export_glossary_csv": self._handle_export_glossary_csv,  # экспорт глоссария в CSV-строку
            "import_glossary_csv": self._handle_import_glossary_csv,  # импорт CSV в translation_glossary (merge|replace)
            "import_history_ndjson": self._history.handle_import_history_ndjson,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_stats": self._history.handle_get_history_stats,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_overview": self._history.handle_get_history_overview,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_item": self._history.handle_get_history_item,  # полные детали одной записи истории по ID
            "add_tag": self._history.handle_add_tag,
            "remove_tag": self._history.handle_remove_tag,
            "get_tags": self._history.handle_get_tags,
            "search_by_tag": self._history.handle_search_by_tag,
            "list_all_tags": self._history.handle_list_all_tags,
            "get_recording_stats": self._handle_get_recording_stats,  # recording metadata statistics
            "get_metrics_dashboard": self._handle_get_metrics_dashboard,  # real-time metrics dashboard snapshot
            "summarize_text": self._text_processing_svc.handle_summarize_text,  # VERIFIED: called from Swift (HistoryPanel)
            "summarize_item": self._text_processing_svc.handle_summarize_item,  # LLM summary для элемента истории по ID
            "extract_action_items": self._handle_extract_action_items,  # LLM извлечение задач/решений/вопросов по item_id
            "batch_extract_action_items": self._handle_batch_extract_action_items,  # пакетное извлечение для нескольких item_id
            "get_pending_action_items": self._handle_get_pending_action_items,  # все items у которых action_items=None
            "get_last_llm_diff": self._handle_get_last_llm_diff,  # последний word-level diff от LLM rewriter'а

            "get_vocabulary_suggestions": self._translation.handle_get_vocabulary_suggestions,
            "toggle_favorite": self._history.handle_toggle_favorite,
            "get_favorites": self._history.handle_get_favorites,
            "is_favorite": self._history.handle_is_favorite,
            "export_history": self._history.handle_export_history,
            "export_history_srt": self._history.handle_export_history_srt,
            "export_history_csv": self._history.handle_export_history_csv,
            "batch_export": self._history.handle_batch_export,  # пакетный экспорт в нескольких форматах
            "export_history_markdown": self._history.handle_export_history_markdown,
            "export_obsidian": self._history.handle_export_obsidian,  # Obsidian-совместимый .md экспорт
            "export_history_json": self._history.handle_export_history_json,
            "export_html_report": self._history.handle_export_html_report,  # автономный HTML-отчёт с аналитикой
            "generate_html_report": self._history.handle_export_html_report,  # алиас для Swift UI (Analytics Dashboard)
            "repaste_item": self._history.handle_repaste_item,
            "get_clipboard_history": self._history.handle_get_clipboard_history,  # история буфера обмена: последние N вставленных транскрипций
            "cleanup_old_history": self._history.handle_cleanup_old_history,  # удаляет записи старше N дней
            "get_storage_info": self._history.handle_get_storage_info,  # размер файлов данных
            "get_transcripts_path": self._history.handle_get_transcripts_path,  # путь к папке транскриптов
            "backup_history": self._history.handle_backup_history,  # создаёт timestamped-резервную копию истории
            "get_auto_backup_status": lambda p: self._auto_backup.get_auto_backup_status(),  # статус авто-резервного копирования
            "configure_auto_export": self._handle_configure_auto_export,  # настроить расписание авто-экспорта
            "get_export_schedule_status": lambda p: self._export_scheduler.get_schedule_status(),  # статус расписания авто-экспорта
            "list_auto_exports": lambda p: {"exports": self._export_scheduler.list_exports()},  # список файлов авто-экспорта
            "restore_history": self._history.handle_restore_history,  # восстанавливает историю из резервной копии
            "list_backups": self._history.handle_list_backups,  # список доступных резервных копий
            "get_history_statistics": self._history.handle_get_history_statistics,  # агрегированная статистика по истории
            "word_frequency_analysis": self._history.handle_word_frequency_analysis,  # частотный анализ слов по истории
            "apply_profile_preset": self._settings_svc.handle_apply_profile_preset,  # применяет пресет настроек профиля
            "list_profile_presets": self._settings_svc.handle_list_profile_presets,  # список доступных пресетов профилей
            "get_notification_preferences": self._settings_svc.handle_get_notification_preferences,  # настройки уведомлений
            "set_notification_preferences": self._settings_svc.handle_set_notification_preferences,  # обновление настроек уведомлений
            "export_settings": self._settings_svc.handle_export_settings,  # экспорт настроек в JSON-файл
            "import_settings": self._settings_svc.handle_import_settings,  # импорт настроек из JSON-файла
            "list_settings_backups": self._settings_svc.handle_list_settings_backups,  # список rolling-бэкапов настроек
            "restore_settings_backup": self._settings_svc.handle_restore_settings_backup,  # восстановить из бэкапа
            "create_manual_settings_backup": self._settings_svc.handle_create_manual_settings_backup,  # ручной бэкап настроек
            # --- Per-app paste profile memory ---
            "get_paste_profile_for_app": self._paste_app_memory.handle_get_paste_profile_for_app,  # VERIFIED: called from Swift (PasteService)
            "record_paste_app_profile": self._paste_app_memory.handle_record_paste_app_profile,  # VERIFIED: called from Swift (PasteService)
            "list_app_profiles": self._paste_app_memory.handle_list_app_profiles,  # список сохранённых профилей по приложениям
            "delete_app_profile": self._paste_app_memory.handle_delete_app_profile,  # удалить профиль приложения
            "cleanup_stale_app_profiles": self._paste_app_memory.handle_cleanup_stale_app_profiles,  # удалить устаревшие записи
            "get_audio_devices": self._handle_get_audio_devices,  # список доступных аудиовходов для GUI
            "test_microphone": self._handle_test_microphone,  # тест микрофона: RMS/peak уровни
            "auto_summarize_batch": self._history.handle_auto_summarize_batch,  # авто-резюме пакета транскрипций через LLM
            "list_summary_profiles": self._history.handle_list_summary_profiles,  # список профилей резюмирования
            "add_summary_profile": self._history.handle_add_summary_profile,  # добавить кастомный профиль резюмирования
            "filter_by_confidence": self._history.handle_filter_by_confidence,  # фильтрация истории по STT confidence score
            "health_check": self._handle_health_check,  # агрегированный health check всех подсистем
            # --- Phase B.1: error bus + LLM probe ---
            "report_paste_failure": self._handle_report_paste_failure,  # Swift→backend paste failure report (ax_denied / app_unsupported)
            "report_hotkey_conflict": self._handle_report_hotkey_conflict,  # Swift→backend hotkey conflict (chord taken by another app)
            "handshake": self._handle_handshake,  # Swift→backend handshake on connect: version + capabilities exchange
            "report_reconnect": self._handle_report_reconnect,  # Swift→backend reconnect telemetry: pushes ipc.reconnect info event
            "list_recent_errors": self._handle_list_recent_errors,  # ring-буфер KrabError: последние N ошибок
            "clear_recent_errors": self._handle_clear_recent_errors,  # очистить ring-буфер ошибок
            "handle_error_action": self._handle_handle_error_action,  # выполнить actionable-действие из toast/diagnostics
            "probe_llm_http": self._handle_probe_llm_http,  # однократный ping LM Studio HTTP endpoint
            "warmup_stt": self._handle_warmup_stt,  # ручной запуск STT warmup (после смены профиля/модели)
            "warmup_rewriter": self._handle_warmup_rewriter,  # явный warmup-probe для "Load Model" кнопки
            "analyze_audio_quality": self._audio_analytics_svc.handle_analyze_audio_quality,  # pre-flight анализ качества аудиофайла
            "analyze_silence": self._audio_analytics_svc.handle_analyze_silence,  # обнаружение тишины и доли речи в аудиофайле
            "get_error_report": self._error_reporter.handle_get_error_report,  # последние ошибки из ring-буфера
            "get_error_stats": self._error_reporter.handle_get_error_stats,  # счётчики ошибок по компоненту/типу/окну
            "send_diagnostics_to_sentry": self._handle_send_diagnostics_to_sentry,  # экспортирует ring-буфер ошибок в Sentry (breadcrumbs + capture_message)
            "get_memory_stats": self._handle_get_memory_stats,  # RSS/VSZ для backend/agent/worker процессов (psutil)
            "get_usage_stats": self._handle_get_usage_stats,
            "get_audio_info": self._audio_analytics_svc.handle_get_audio_info,  # метаданные аудиофайла  # ежедневная статистика использования: записи, длительность, слова
            "get_system_info": self._handle_get_system_info,  # мониторинг системных ресурсов: CPU, RAM, диск, GPU
            "find_duplicates": self._history.handle_find_duplicates,  # обнаружение дублирующихся транскрипций по текстовому сходству
            "set_annotation": self._history.handle_set_annotation,  # сохранить пользовательскую заметку к записи истории
            "get_annotation": self._history.handle_get_annotation,  # получить заметку для записи истории
            "search_annotations": self._history.handle_search_annotations,  # полнотекстовый поиск по заметкам
            "create_collection": self._collections.handle_create_collection,  # создать коллекцию/папку для организации истории
            "delete_collection": self._collections.handle_delete_collection,  # удалить коллекцию
            "list_collections": self._collections.handle_list_collections,  # список всех коллекций
            "add_to_collection": self._collections.handle_add_to_collection,  # добавить запись истории в коллекцию
            "remove_from_collection": self._collections.handle_remove_from_collection,  # удалить запись из коллекции
            "list_normalization_profiles": self._handle_list_normalization_profiles,  # список профилей нормализации текста
            "get_collection_items": self._collections.handle_get_collection_items,  # получить записи истории из коллекции
            "start_chain": self._chains.handle_start_chain,  # начать цепочку связанных записей
            "add_to_chain": self._chains.handle_add_to_chain,  # добавить запись в цепочку
            "end_chain": self._chains.handle_end_chain,  # завершить цепочку
            "get_chain": self._chains.handle_get_chain,  # получить цепочку с деталями
            "list_chains": self._chains.handle_list_chains,  # список цепочек
            "merge_chain_text": self._chains.handle_merge_chain_text,  # объединённый текст цепочки
            "unlink_recording_from_chain": self._chains.handle_unlink_recording_from_chain,  # убрать запись из цепочки
            "schedule_recording": self._recording_scheduler.handle_schedule_recording,  # запланировать запись на определённое время
            "cancel_scheduled_recording": self._recording_scheduler.handle_cancel_scheduled_recording,  # отменить запланированную запись
            "list_scheduled_recordings": self._recording_scheduler.handle_list_scheduled_recordings,  # список запланированных записей
            "generate_daily_digest": self._handle_generate_daily_digest,  # ежедневный дайджест транскрипций
            "analyze_quality_trends": self._audio_analytics_svc.handle_analyze_quality_trends,  # анализ трендов качества
            "compare_periods": self._handle_compare_periods,  # сравнение двух периодов использования
            "get_activity_calendar": self._handle_get_activity_calendar,  # GitHub-style activity calendar данные
            "get_recording_insights": self._handle_get_recording_insights,  # эвристические инсайты по записям (Wave 54: alias was wrongly pointed at _handle_get_recording_stats)
            "get_sentiment_trends": self._handle_get_sentiment_trends,  # анализ трендов тональности транскрипций за N дней

            "check_integrity": self._handle_check_integrity,  # проверка целостности данных
            "repair_integrity": self._handle_repair_integrity,  # исправление проблем целостности данных
            "extract_terms": self._handle_extract_terms,  # извлечение терминов из текста
            "compare_texts": self._text_processing_svc.handle_compare_texts,  # сравнение двух текстов/транскрипций
            "get_context_memory": self._handle_get_context_memory,  # контекстная память STT: слова и темы из последних транскрибаций
            "score_readability": self._text_processing_svc.handle_score_readability,  # оценка читабельности текста транскрибации
            "score_transcription": self._handle_score_transcription,  # оценка качества транскрибации (0–100, A–F)
            "get_event_log": self._event_replay.handle_get_event_log,  # лог событий для отладки (фильтрация по типу/времени)
            "get_event_stats": self._event_replay.handle_get_event_stats,  # статистика событий: счётчики, скорость/мин
            "replay_events": self._event_replay.handle_replay_events,  # воспроизведение событий в диапазоне времени
            "get_waveform": self._audio_analytics_svc.handle_get_waveform,  # генерация waveform-данных для GUI-визуализации
            "get_throttle_stats": self._handle_get_throttle_stats,  # статистика IPC throttle: вызовы, отклонения
            "check_audio_duplicate": self._audio_analytics_svc.handle_check_audio_duplicate,  # аудио-фингерпринтинг для обнаружения дубликатов
            "batch": self._handle_batch,  # пакетное выполнение нескольких IPC-методов за один вызов (макс. 50)
            "get_keyword_cloud": self._handle_get_keyword_cloud,  # данные облака ключевых слов для визуализации word cloud
            "prepare_share": self._sharing.handle_prepare_share,  # подготовить пакет для шаринга транскрипций
            "list_shared": self._sharing.handle_list_shared,  # список сохранённых пакетов шаринга
            "get_shared": self._sharing.handle_get_shared,  # получить пакет шаринга по share_id
            "revoke_share_link": self._sharing.handle_revoke_share_link,  # отозвать пакет шаринга по токену (Wave 158)
            "save_transcript_version": self._transcript_versioning.handle_save_transcript_version,  # сохранить новую версию текста транскрипции
            "get_transcript_versions": self._transcript_versioning.handle_get_transcript_versions,  # получить все версии транскрипции по item_id
            "revert_transcript_version": self._transcript_versioning.handle_revert_transcript_version,  # откат транскрипции к указанной версии
            "generate_auto_title": self._handle_generate_auto_title,  # автоматическая генерация заголовка для транскрибации
            # форматирование текста под целевое приложение (telegram, notes, email и др.)
            "format_for_paste": self._paste_formatter.handle_format_for_paste,
            "merge_recordings": lambda p: self._merger.handle_merge_recordings(p, self.store),  # объединить несколько записей истории в одну
            "preview_merge": lambda p: self._merger.handle_preview_merge(p, self.store),  # предпросмотр объединения без сохранения
            "list_paste_formatters": self._paste_formatter.handle_list_paste_formatters,  # список доступных форматтеров вставки
            "get_learning_stats": self._handle_get_learning_stats,  # режим изучения языков: статистика прогресса
            "get_analytics_dashboard": self._handle_get_analytics_dashboard,  # комплексный дашборд аналитики: все метрики за один вызов
            "get_topic_timeline": self._handle_get_topic_timeline,  # таймлайн смен тем разговора из истории транскрибаций
            "list_config_presets": self._config_presets.handle_list_config_presets,  # список конфигурационных пресетов (встроенных и кастомных)
            "apply_config_preset": self._config_presets.handle_apply_config_preset,  # применить конфигурационный пресет — вернуть settings_patch
            "create_config_preset": self._config_presets.handle_create_config_preset,  # создать кастомный конфигурационный пресет
            "enqueue_transcription": self._transcription_queue.handle_enqueue,  # добавить аудиофайл в очередь транскрипции с приоритетом
            "cancel_transcription": self._transcription_queue.handle_cancel,  # отменить задание транскрипции по job_id
            "get_queue_status": self._transcription_queue.handle_get_status,  # статус задания транскрипции по job_id
            "list_transcription_queue": self._transcription_queue.handle_list_queue,  # список всех заданий очереди транскрипции
            "detect_emotion": self._text_processing_svc.handle_detect_emotion,  # эвристическое определение эмоции в тексте транскрипции
            "estimate_recording_cost": self._handle_estimate_recording_cost,  # оценка вычислительной стоимости обработки записи
            "get_daily_cost_summary": self._handle_get_daily_cost_summary,  # сводка вычислительных расходов за сегодня
            "check_migration": self._data_migrator.handle_check_migration,  # проверка необходимости миграции данных
            "run_migration": self._data_migrator.handle_run_migration,  # выполнение миграции данных между версиями
            "expand_abbreviations": self._text_processing_svc.handle_expand_abbreviations,  # раскрытие аббревиатур в тексте транскрипции
            "remove_abbreviation": self._text_processing_svc.handle_remove_abbreviation,  # удалить аббревиатуру
            "list_abbreviations": self._text_processing_svc.handle_list_abbreviations,  # список аббревиатур для языка
            "profile_noise": self._audio_analytics_svc.handle_profile_noise,  # профилирование фонового шума: тип, уровень, SNR, рекомендации
            "configure_obsidian_sync": self._obsidian_sync.handle_configure,  # настроить Obsidian vault для синхронизации транскрипций
            "run_obsidian_sync": self._obsidian_sync.handle_sync,  # синхронизировать записи истории с Obsidian vault
            "get_obsidian_sync_status": self._obsidian_sync.handle_get_status,  # статус синхронизации с Obsidian vault
            # зарегистрировать воспроизведение записи (item_id, duration_listened_sec)
            "record_playback": self._playback_tracker.handle_record_playback,
            # статистика воспроизведения одной записи: play_count, total_listened_sec, last_played
            "get_playback_stats": self._playback_tracker.handle_get_playback_stats,
            "get_most_replayed": self._playback_tracker.handle_get_most_replayed,  # топ N наиболее часто воспроизводимых записей
            # прогнать текст через настраиваемый конвейер пост-обработки (пробелы, пунктуация, сущности, аббревиатуры, анонимизация)
            "post_process_text": self._text_processing_svc.handle_post_process_text,
            "list_post_process_steps": self._text_processing_svc.handle_list_post_process_steps,  # список доступных шагов пост-обработки текста
            "compare_recordings": self._handle_compare_recordings,  # сравнение нескольких записей side-by-side: матрица сходства, статистика, общие/уникальные слова
            "select_model": self._handle_select_model,  # умный выбор STT-модели на основе условий записи
            "get_smart_vocabulary_suggestions": self._handle_get_smart_vocabulary_suggestions,  # предложения для словаря STT на основе паттернов использования
            "get_startup_diagnostics": self._handle_get_startup_diagnostics,  # диагностика при старте: результаты всех startup-проверок
            # автоматическое обогащение метаданных записи: word_count, emotion, pace, quality, topics и др.
            "enrich_recording": self._metadata_enricher.handle_enrich_recording,
            "get_shutdown_status": self._handle_get_shutdown_status,  # статус последнего graceful shutdown: clean, last_shutdown_time
            "check_duplicate": self._handle_check_duplicate,  # проверка одной транскрипции на дублирование по текстовому сходству
            "run_deduplication": self._handle_run_deduplication,  # полное сканирование истории на дубликаты (фоновый поток, возвращает job_id)
            "dedup_progress": self._handle_dedup_progress,  # опрос статуса фоновой задачи run_deduplication по job_id
            "get_dedup_stats": self._handle_get_dedup_stats,  # статистика дедупликатора: проверено, найдено, символов сохранено
            "get_timeline_view": self._handle_get_timeline_view,  # группировка истории по временным блокам (timeline)
            "get_recent_searches": self._search_history.handle_get_recent_searches,  # последние поисковые запросы пользователя
            "get_popular_searches": self._search_history.handle_get_popular_searches,  # наиболее частые поисковые запросы
            "clear_search_history": self._search_history.handle_clear_search_history,  # очистить историю поисковых запросов
            "archive_items": self._archive_manager.handle_archive_items,  # переместить записи истории в архив
            "unarchive_items": self._archive_manager.handle_unarchive_items,  # восстановить записи из архива
            "list_archived": self._archive_manager.handle_list_archived,  # список архивированных записей
            "get_archive_stats": self._archive_manager.handle_get_archive_stats,  # статистика архива: количество, размер, oldest/newest
            "generate_stats_report": self._handle_generate_stats_report,  # полный Markdown-отчёт статистики за период
            "generate_mini_stats_report": self._handle_generate_mini_stats_report,  # краткий 5-строчный отчёт состояния
            # --- call_assist template management ---
            "call_assist_list_templates": self._call_assist.handle_list_templates,  # список шаблонов быстрых реплик call assist
            "call_assist_add_template": self._call_assist.handle_add_template,  # добавить шаблон быстрой реплики
            "call_assist_remove_template": self._call_assist.handle_remove_template,  # удалить шаблон быстрой реплики
            "call_assist_template": self._call_assist.handle_template,  # отправить шаблонную реплику в Gateway
            "call_assist_cost_report": self._call_assist.handle_cost_report,  # подробный cost report текущей звонковой сессии
            # --- Phase 3 safeguards ---
            "call_estimate_cost": self._call_cost_estimator.handle_estimate_cost,  # оценить стоимость звонка по провайдеру и стране
            "call_check_auto_end": self._call_auto_end.handle_check_auto_end,  # проверить правила автоматического завершения
            # --- text templates ---
            "get_templates": self._template_manager.handle_get_templates,  # список шаблонов быстрой вставки текста
            "add_template": self._template_manager.handle_add_template,  # добавить шаблон текста
            "remove_template": self._template_manager.handle_remove_template,  # удалить шаблон текста
            "apply_template": self._template_manager.handle_apply_template,  # применить шаблон (подставить переменные)
            # --- webhooks ---
            "register_webhook": self._webhook_manager.handle_register_webhook,  # зарегистрировать webhook для событий
            "unregister_webhook": self._webhook_manager.handle_unregister_webhook,  # отменить регистрацию webhook
            "list_webhooks": self._webhook_manager.handle_list_webhooks,  # список зарегистрированных webhook-ов
            # --- speaker aliases ---
            "set_speaker_alias": self._speaker_manager.handle_set_speaker_alias,  # назначить псевдоним для спикера
            "get_speaker_aliases": self._speaker_manager.handle_get_speaker_aliases,  # список псевдонимов спикеров
            "remove_speaker_alias": self._speaker_manager.handle_remove_speaker_alias,  # удалить псевдоним спикера
            # --- live subtitles (Sprint 2B) ---
            "live_subs_ingest": self._live_subs.handle_ingest,  # потоковая STT+translate (частый вызов)
            "live_subs_stop": self._live_subs.handle_stop,  # flush и сброс буфера
            # --- plugins ---
            "list_plugins": self._plugin_manager.handle_list_plugins,  # список обнаруженных плагинов
            "get_plugin_info": self._plugin_manager.handle_get_plugin_info,  # информация о конкретном плагине
            "unload_plugin": self._plugin_manager.handle_unload_plugin,  # полная выгрузка плагина из памяти
            # --- feature flags ---
            "get_feature_flags": self._feature_flags.handle_get_feature_flags,  # получить все feature-флаги с описаниями
            "set_feature_flag": self._feature_flags.handle_set_feature_flag,  # установить значение feature-флага
            # --- hotwords ---
            "add_hotword": self._hotword_detector.handle_add_hotword,  # добавить горячее слово для отслеживания
            "remove_hotword": self._hotword_detector.handle_remove_hotword,  # удалить горячее слово
            "get_hotwords": self._hotword_detector.handle_get_hotwords,  # список горячих слов
            "check_hotwords": self._hotword_detector.handle_check_hotwords,  # проверить текст на наличие горячих слов
            # --- model cache ---
            "list_cached_models": self._model_cache_manager.handle_list_cached_models,  # список кэшированных ML-моделей
            "get_model_cache_info": self._model_cache_manager.handle_get_model_cache_info,  # информация о кэше конкретной модели
            # --- Voice Assistant wake word config (PR 1.5) ---
            # --- openWakeWord adapter (free, Apache 2.0) ---
            "wake_word_list_models": self._oww_adapter.handle_wake_word_list_models,  # список builtin+custom моделей
            "wake_word_start": self._oww_adapter.handle_wake_word_start,  # запустить прослушивание
            "wake_word_stop": self._oww_adapter.handle_wake_word_stop,  # остановить прослушивание
            "wake_word_status": self._oww_adapter.handle_wake_word_status,  # статус адаптера
            # --- Dual-mode TTS (Silero RU + Kokoro EN + macOS say fallback) ---
            "synthesize_speech": self._tts.handle_synthesize_speech,  # синтез речи: text, language (ru/en/auto), voice
            "analyze_word_timing": self._audio_analytics_svc.handle_analyze_word_timing,  # анализ ритма речи по пословным таймстемпам Whisper
            # --- Telegram Bridge (Krab Ear → main Krab userbot) ---
            "send_to_telegram": self._handle_send_to_telegram,  # отправить транскрипцию в Telegram через main Krab userbot
            # --- Apple Notes integration (Phase D.4) ---
            "create_apple_note": self._handle_create_apple_note,  # создать заметку в Apple Notes через osascript
            # --- Apple Reminders integration (Phase D.4) ---
            "create_apple_reminder": self._handle_create_apple_reminder,  # создать напоминание в Apple Reminders через osascript
            # --- Apple Calendar integration (Phase D.4) ---
            "create_calendar_event": self._handle_create_calendar_event,  # создать событие в Apple Calendar через osascript
            # --- iMessage integration (Phase D.4) ---
            "send_imessage": self._handle_send_imessage,  # отправить сообщение через iMessage/SMS через osascript
            "list_telegram_chats": self._handle_list_telegram_chats,  # получить список доступных чатов Telegram через main Krab userbot
            # --- Phase 3: Call Session CRUD (outbound call automation) ---
            "call_session_create": self._call_session_service.handle_call_session_create,  # создать звонковую сессию
            "call_session_get": self._call_session_service.handle_call_session_get,  # получить сессию по id
            "call_session_list": self._call_session_service.handle_call_session_list,  # список сессий с опциональным фильтром по статусу
            "call_session_update_status": self._call_session_service.handle_call_session_update_status,  # переход статуса сессии
            "call_session_add_transcript": self._call_session_service.handle_call_session_add_transcript,  # добавить реплику в транскрипт
            "call_session_end": self._call_session_service.handle_call_session_end,  # завершить сессию: compute duration, total_cost
            # --- STT hotwords (initial_prompt boost) ---
            "add_stt_hotword": self._handle_add_stt_hotword,  # добавить термин в STT hotwords список
            "remove_stt_hotword": self._handle_remove_stt_hotword,  # удалить термин из STT hotwords списка
            "list_stt_hotwords": self._handle_list_stt_hotwords,  # получить весь список STT hotwords
            # --- Recording bookmarks (Cmd+Shift+B) ---
            "add_bookmark": self._bookmarks.handle_add_bookmark,  # создать закладку на текущей позиции записи
            "list_bookmarks": self._bookmarks.handle_list_bookmarks,  # список закладок для item_id
            "list_all_bookmarks": self._bookmarks.handle_list_all_bookmarks,  # все активные закладки
            "delete_bookmark": self._bookmarks.handle_delete_bookmark,  # удалить закладку (tombstone)
            "jump_to_bookmark": self._bookmarks.handle_jump_to_bookmark,  # перейти к закладке (эмитит playback.seek)
            # --- Semantic search (opt-in, multilingual embeddings) ---
            "semantic_search": self._handle_semantic_search,  # семантический поиск по истории через embeddings
            "semantic_search_status": self._handle_semantic_search_status,  # статус семантического поиска: модель, индекс
            "semantic_search_reindex": self._handle_semantic_search_reindex,  # переиндексировать всю историю
            # --- LM Studio model discovery ---
            "list_llm_models": self._handle_list_llm_models,  # список моделей из LM Studio /v1/models (для dropdown в GUI)
            # --- Quick word replacement (Cmd+Shift+R) ---
            "replace_word_in_last_transcript": self._handle_replace_word_in_last_transcript,  # заменить слово в последней транскрипции без перезаписи
            # --- Privacy audit log ---
            "get_privacy_audit_log": self._handle_get_privacy_audit_log,  # последние записи privacy audit log
            "clear_privacy_audit_log": self._handle_clear_privacy_audit_log,  # удалить файл privacy audit log
            # --- D.2.3: Scored STT routing decision ---
            "get_stt_routing_decision": self._handle_get_stt_routing_decision,  # scored adapter selection debug
            # --- Default STT hotwords seed ---
        }

        handler = handlers.get(method)
        if handler is None:
            return self._error(request_id, "unknown_method", f"Неизвестный метод: {method}")

        # IPC signing: верифицируем HMAC-SHA256 подпись если включено
        if self._request_signer is not None:
            sig = payload.get("signature", "")
            ts = payload.get("timestamp")
            nc = payload.get("nonce")
            secret = settings.IPC_SIGNING_SECRET
            if not sig:
                logger.warning("IPC signing: запрос без подписи метод=%s", method)
                return self._error(request_id, "unauthorized", "Запрос не подписан")
            try:
                ts_float = float(ts) if ts is not None else None
                valid = self._request_signer.verify_request(
                    method, params, sig, secret, timestamp=ts_float, nonce=nc
                )
            except Exception:
                valid = False
            if not valid:
                logger.warning("IPC signing: неверная подпись метод=%s", method)
                return self._error(request_id, "unauthorized", "Неверная подпись запроса")

        # IPC throttle: проверяем rate limit перед вызовом обработчика
        if self._ipc_throttle is not None:
            if not self._ipc_throttle.check_rate(method):
                wait_sec = self._ipc_throttle.get_wait_time(method)
                logger.warning("IPC rate limit exceeded: method=%s wait=%.2fs", method, wait_sec)
                # Wave 77: push ipc.rate_limit_exceeded (2779 occurrences in production logs)
                try:
                    from backend.error_bus import KrabError
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone
                    _entry = ERROR_REGISTRY.get("ipc.rate_limit_exceeded", {})
                    self._error_bus.push(KrabError(
                        severity=_entry.get("severity", "warn"),
                        component="ipc",
                        code="ipc.rate_limit_exceeded",
                        message_user=_entry.get("user_msg_ru", "Превышен лимит запросов IPC"),
                        message_debug=f"rate limit hit: method={method!r} wait={wait_sec:.2f}s",
                        timestamp=datetime.now(timezone.utc),
                        context={"method": method, "wait_sec": wait_sec},
                        actionable=False,
                        action_id=None,
                    ))
                except Exception:
                    pass
                return self._error(
                    request_id,
                    "rate_limit_exceeded",
                    f"Превышен лимит запросов для метода {method!r}. Повторите через {wait_sec:.1f}s",
                )

        if method not in _BREADCRUMB_EXCLUDED_METHODS:
            add_breadcrumb(
                category="ipc",
                message=method,
                level="info",
            )

        try:
            result = handler(params)
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            logger.exception("Ошибка метода %s", method)
            return self._error(request_id, "internal_error", str(exc))

    _BATCH_MAX_REQUESTS = 50

    def _handle_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Пакетное выполнение нескольких IPC-методов за один вызов.

        Принимает список sub-запросов, выполняет их последовательно через
        существующий handle_request. Ошибка в одном запросе не прерывает остальные.

        Параметры:
            requests — список объектов {method, params?}. Макс. 50 элементов.

        Ответ:
            {results: [...], total: N, succeeded: N, failed: N}
        """
        requests = params.get("requests")
        if not isinstance(requests, list):
            raise ValueError("Параметр 'requests' должен быть списком")
        if len(requests) > self._BATCH_MAX_REQUESTS:
            raise ValueError(
                f"Превышен лимит пакетного запроса: {len(requests)} > {self._BATCH_MAX_REQUESTS}"
            )

        results = []
        succeeded = 0
        failed = 0
        for i, sub_req in enumerate(requests):
            if not isinstance(sub_req, dict):
                results.append({
                    "method": None,
                    "ok": False,
                    "error": {"code": "invalid_request", "message": f"Элемент #{i} не является объектом"},
                })
                failed += 1
                continue

            method = sub_req.get("method")
            sub_params = sub_req.get("params", {})
            if not isinstance(sub_params, dict):
                sub_params = {}

            response = self.handle_request({"id": f"batch_{i}", "method": method, "params": sub_params})
            entry: dict[str, Any] = {"method": method, "ok": response.get("ok", False)}
            if response.get("ok"):
                entry["result"] = response.get("result")
                succeeded += 1
            else:
                entry["error"] = response.get("error", {"code": "unknown", "message": "Неизвестная ошибка"})
                failed += 1
            results.append(entry)

        return {
            "results": results,
            "total": len(requests),
            "succeeded": succeeded,
            "failed": failed,
        }

    def _handle_configure_auto_export(self, params: dict[str, Any]) -> dict[str, Any]:
        """Настраивает расписание авто-экспорта.

        Параметры:
            format (str): формат экспорта — srt, csv, markdown, json, obsidian, html
            interval_hours (int): интервал в часах (по умолчанию 24)
            output_dir (str|None): папка для файлов (None = авто)
            enabled (bool): включить авто-экспорт (по умолчанию True)

        Возвращает:
            Обновлённый статус расписания (dict).
        """
        fmt = str(params.get("format", "json")).strip()
        interval_hours = int(params.get("interval_hours", 24))
        output_dir = params.get("output_dir")
        enabled = bool(params.get("enabled", True))
        return self._export_scheduler.configure(
            fmt=fmt,
            interval_hours=interval_hours,
            output_dir=output_dir,
            enabled=enabled,
        )

    def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService.handle_ping (W1181 F3 MED)."""
        return self._health_check_svc.handle_ping(params)

    def _handle_start_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._recording_core_svc.handle_start_recording(params)

    @staticmethod
    def _safe_callback(fn: Callable | None, *args: Any) -> None:
        """Delegated to RecordingCoreService._safe_callback."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        _RCS._safe_callback(fn, *args)

    def _build_empty_audio_response(self, duration_sec, quality_profile, cleanup_profile, translation_mode, translate_and_paste, stop_tail_trim_ms, silence_detected=False, silence_guard_enabled=False, background_guard_rejected=False):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._build_empty_audio_response(duration_sec=duration_sec, quality_profile=quality_profile, cleanup_profile=cleanup_profile, translation_mode=translation_mode, translate_and_paste=translate_and_paste, stop_tail_trim_ms=stop_tail_trim_ms, silence_detected=silence_detected, silence_guard_enabled=silence_guard_enabled, background_guard_rejected=background_guard_rejected)

    def _load_stop_recording_settings(self, params, settings):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._load_stop_recording_settings(params, settings)

    # ------------------------------------------------------------------ #
    #   _handle_stop_recording — delegates to RecordingCoreService         #
    # ------------------------------------------------------------------ #

    def _handle_stop_recording(self, params):
        """Delegated to RecordingCoreService.handle_stop_recording."""
        return self._recording_core_svc.handle_stop_recording(params)

    def _stop_recording_phase_a(self, params, settings):
        """Delegated to RecordingCoreService._stop_recording_phase_a."""
        return self._recording_core_svc._stop_recording_phase_a(params, settings)

    def _stop_recording_phase_b(self, audio, duration_sec, stop_tail_trim_ms, sr):
        """Delegated to RecordingCoreService._stop_recording_phase_b."""
        return self._recording_core_svc._stop_recording_phase_b(audio, duration_sec, stop_tail_trim_ms, sr)

    def _stop_recording_phase_c(self, audio, duration_sec, sr):
        """Delegated to RecordingCoreService._stop_recording_phase_c."""
        return self._recording_core_svc._stop_recording_phase_c(audio, duration_sec, sr)

    def _stop_recording_phase_d(self, transcribe_payload, duration_sec, sr, stop_tail_trim_ms, silence_detected, silence_guard_enabled, background_guard_rejected):
        """Delegated to RecordingCoreService._stop_recording_phase_d."""
        return self._recording_core_svc._stop_recording_phase_d(transcribe_payload=transcribe_payload, duration_sec=duration_sec, sr=sr, stop_tail_trim_ms=stop_tail_trim_ms, silence_detected=silence_detected, silence_guard_enabled=silence_guard_enabled, background_guard_rejected=background_guard_rejected)

    def _stop_recording_phase_e(self, phase_d, sr, duration_sec, stop_tail_trim_ms, silence_detected, silence_guard_enabled, background_guard_rejected, rt_session_id, settings):
        """Delegated to RecordingCoreService._stop_recording_phase_e."""
        return self._recording_core_svc._stop_recording_phase_e(phase_d=phase_d, sr=sr, duration_sec=duration_sec, stop_tail_trim_ms=stop_tail_trim_ms, silence_detected=silence_detected, silence_guard_enabled=silence_guard_enabled, background_guard_rejected=background_guard_rejected, rt_session_id=rt_session_id, settings=settings)

    def _handle_get_recording_state(self, params):
        """Delegated to RecordingCoreService.handle_get_recording_state."""
        return self._recording_core_svc.handle_get_recording_state(params)

    def _handle_get_usage_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает ежедневную статистику использования: записи, длительность, слова."""

    def _handle_list_normalization_profiles(self, params: dict) -> dict:
        """Возвращает список всех профилей нормализации текста."""
        return {"profiles": self._norm_profiles.list_profiles()}

    def _handle_get_system_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает информацию о системных ресурсах: CPU, RAM, диск, GPU."""
        return self._system_monitor.get_system_info()

    def _handle_set_paste_status(self, params: dict[str, Any]) -> dict[str, Any]:
        item_id = str(params.get("id", "")).strip()
        paste_status = str(params.get("paste_status", "failed")).strip() or "failed"
        ok = self.store.set_paste_status(item_id=item_id, paste_status=paste_status)
        if not ok:
            raise RuntimeError("Не удалось обновить paste_status")
        return {"updated": True, "id": item_id, "paste_status": paste_status}

    # ------------------------------------------------------------------
    # Audio converter IPC handlers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Readiness probing — честная проверка доступности компонентов
    # ------------------------------------------------------------------

    @staticmethod
    def _hf_model_cached(hf_repo: str) -> bool:
        """Проверяет наличие модели в локальном кэше HuggingFace Hub."""
        cache_base = Path.home() / ".cache" / "huggingface" / "hub"
        folder = "models--" + hf_repo.replace("/", "--")
        return (cache_base / folder).exists()

    @staticmethod
    def _probe_stt() -> dict[str, Any]:
        """Проверяет доступность STT моделей без их загрузки."""
        from core.config import settings as cfg
        balanced_cached = BackendService._hf_model_cached(cfg.MODEL_BALANCED)
        max_cached = [m for m in cfg.model_max_list if BackendService._hf_model_cached(m)]
        return {
            "balanced_model": cfg.MODEL_BALANCED,
            "balanced_cached": balanced_cached,
            "max_models_cached": max_cached,
            "ready": balanced_cached,
        }

    @staticmethod
    def _probe_diarization() -> dict[str, Any]:
        """Проверяет доступность pyannote diarization без загрузки pipeline."""
        from core.config import settings as cfg
        hf_token = os.environ.get("HF_TOKEN") or cfg.HF_TOKEN
        has_token = bool(hf_token)
        model_cached = BackendService._hf_model_cached(cfg.DIARIZATION_MODEL)
        return {
            "model": cfg.DIARIZATION_MODEL,
            "has_hf_token": has_token,
            "model_cached": model_cached,
            "ready": has_token and model_cached,
        }

    @staticmethod
    def _probe_translation() -> dict[str, Any]:
        """Проверяет наличие моделей перевода Helsinki-NLP в локальном кэше."""
        _TRANSLATION_MODELS = {
            "ru_to_es": "Helsinki-NLP/opus-mt-ru-es",
            "es_to_ru": "Helsinki-NLP/opus-mt-es-ru",
            "en_to_ru": "Helsinki-NLP/opus-mt-en-ru",
        }
        cache_base = Path.home() / ".cache" / "huggingface" / "hub"
        cached: list[str] = []
        missing: list[str] = []
        for mode, repo in _TRANSLATION_MODELS.items():
            folder = "models--" + repo.replace("/", "--")
            if (cache_base / folder).exists():
                cached.append(mode)
            else:
                missing.append(mode)
        return {
            "modes_cached": cached,
            "modes_missing_offline": missing,
            "any_ready": bool(cached),
        }

    @staticmethod
    def _build_readiness_report_static() -> dict[str, Any]:
        """Собирает полный отчёт о готовности всех компонентов.

        Статический метод: вызывается и из IPC-сервиса, и из REST server
        без необходимости создавать полный инстанс BackendService.
        """
        stt = BackendService._probe_stt()
        diarization = BackendService._probe_diarization()
        translation = BackendService._probe_translation()
        return {
            "overall_ready": stt["ready"],
            "stt": stt,
            "diarization": diarization,
            "translation": translation,
        }

    def _handle_export_glossary_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует translation_glossary в CSV-строку.

        Returns: {"ok": True, "csv": "source,target\\n...", "row_count": N}
        """
        import csv
        import io

        settings = self._settings_svc.cached_settings()
        glossary: dict = settings.get("translation_glossary", {}) or {}

        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["source", "target"])
        for source, target in sorted(glossary.items()):
            writer.writerow([source, target])

        return {"ok": True, "csv": buf.getvalue(), "row_count": len(glossary)}

    def _handle_import_glossary_csv(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует CSV в translation_glossary.

        params:
          csv: str — CSV-строка с заголовком source,target
          mode: "merge" | "replace" — merge добавляет/обновляет, replace полностью заменяет
          on_conflict: "skip" | "overwrite" | "error" — поведение при конфликте в merge-режиме
            "skip"      — (по умолчанию) существующий термин сохраняется, конфликт записывается
            "overwrite" — существующий термин перезаписывается
            "error"     — импорт прерывается на первом конфликте

        Returns:
          {ok, imported_count, skipped_count, conflict_count,
           conflicts: [{source, existing_target, new_target}], total}
        """
        import csv
        import io

        csv_str = params.get("csv", "")
        mode = params.get("mode", "merge").lower()
        on_conflict = params.get("on_conflict", "skip").lower()

        if mode not in ("merge", "replace"):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if on_conflict not in ("skip", "overwrite", "error"):
            return {"ok": False, "error": f"invalid on_conflict: {on_conflict}"}

        settings = self._settings_svc.cached_settings()
        current: dict = dict(settings.get("translation_glossary", {}) or {})
        new_entries: dict = {} if mode == "replace" else dict(current)
        skipped = 0
        conflicts: list = []
        # Track sources seen in this CSV file for within-CSV deduplication
        seen_in_csv: dict = {}

        try:
            reader = csv.reader(io.StringIO(csv_str))
            header = next(reader, None)
            if not header or [h.strip().lower() for h in header] != ["source", "target"]:
                return {"ok": False, "error": "header must be: source,target"}
            for row in reader:
                if len(row) != 2:
                    skipped += 1
                    continue
                src, tgt = row[0].strip(), row[1].strip()
                if not src or not tgt:
                    skipped += 1
                    continue
                # Skip rows where source == target (no-op entries)
                if src == tgt:
                    skipped += 1
                    continue
                # Within-CSV deduplication: skip duplicate source rows, keep first
                if src in seen_in_csv:
                    skipped += 1
                    continue
                seen_in_csv[src] = tgt

                # Conflict detection in merge mode
                if mode == "merge" and src in current and current[src] != tgt:
                    conflicts.append({
                        "source": src,
                        "existing_target": current[src],
                        "new_target": tgt,
                    })
                    if on_conflict == "error":
                        return {
                            "ok": False,
                            "error": f"conflict on source '{src}': existing='{current[src]}' new='{tgt}'",
                            "imported_count": 0,
                            "skipped_count": skipped,
                            "conflict_count": len(conflicts),
                            "conflicts": conflicts,
                        }
                    elif on_conflict == "skip":
                        # Keep existing — don't overwrite
                        continue
                    # on_conflict == "overwrite": fall through to set new value

                new_entries[src] = tgt
        except Exception as exc:
            return {"ok": False, "error": f"parse error: {exc}"}

        self._settings_svc.handle_set_settings({"translation_glossary": new_entries})

        prev_count = len(current)
        imported = len(new_entries) - (prev_count if mode == "merge" else 0)
        return {
            "ok": True,
            "imported_count": max(imported, 0),
            "skipped_count": skipped,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "total": len(new_entries),
        }

    def _handle_get_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService.handle_get_diagnostics (W1181 F3 MED)."""
        return self._health_check_svc.handle_get_diagnostics(params)

    def _handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService.handle_health_check (W1181 F3 MED)."""
        return self._health_check_svc.handle_health_check(params)

    # ------------------------------------------------------------------
    # Phase B.1 — error bus + LLM probe handlers
    # ------------------------------------------------------------------

    def _handle_list_recent_errors(self, params: dict) -> dict:
        """Возвращает до *limit* последних KrabError из ring-буфера ErrorBus."""
        limit = int(params.get("limit", 200))
        items = self._error_bus.list_recent(limit)
        return {"errors": [item.model_dump(mode="json") for item in items]}

    def _handle_clear_recent_errors(self, params: dict) -> dict:
        """Очищает ring-буфер и dedupe-состояние ErrorBus. Возвращает количество удалённых записей."""
        n = self._error_bus.clear()
        return {"cleared": n}

    def _handle_send_diagnostics_to_sentry(self, params: dict) -> dict:
        """Отправляет последние N ошибок в Sentry — последние 20 как breadcrumbs, остальные в extras.

        Позволяет отлаживать shipped-сборки одним кликом из вкладки «Диагностика».
        Возвращает {"ok": True, "sent_count": N} или {"ok": False, "reason": "..."}.
        """
        if self._error_bus is None:
            return {"ok": False, "reason": "error_bus_not_initialized"}
        try:
            import sentry_sdk
        except ImportError:
            return {"ok": False, "reason": "sentry_sdk_not_available"}

        items = self._error_bus.list_recent(limit=200)
        if not items:
            return {"ok": False, "reason": "no_errors_to_send"}

        # Последние 20 — в breadcrumbs, остальные попадают в extras capture_message.
        for err in items[-20:]:
            sentry_sdk.add_breadcrumb(
                category=err.component,
                message=err.code,
                level=err.severity,
                data=err.context,
            )

        sentry_sdk.capture_message(
            f"Diagnostics export: {len(items)} errors over recent window",
            level="info",
            tags={"phase": "diagnostics_export"},
            extras={"error_count": len(items), "first_code": items[0].code if items else None},
        )
        sentry_sdk.flush(timeout=2.0)
        return {"ok": True, "sent_count": len(items)}

    def _handle_get_memory_stats(self, params: dict) -> dict:
        """Возвращает RSS/VSZ для backend, agent и worker процессов через psutil.

        Ищет процессы по подстроке cmdline: KrabEarAgent, KrabEar/backend/service.py, gigaam_worker.
        Возвращает {"ok": True, "processes": [...]} или {"ok": False, "reason": "..."}.
        """
        try:
            import psutil
        except ImportError:
            return {"ok": False, "reason": "psutil_not_installed"}

        matches: list[dict] = []
        # KRAB-EAR-BACKEND-H: process_iter(attrs=...) eagerly calls cmdline()
        # inside the iterator; on macOS system procs (e.g. mdworker_shared)
        # proc_cmdline raises PermissionError → wrapped as SystemError by the
        # psutil C ext, which bubbles out before any inner try/except. Iterate
        # bare and fetch fields manually under a wide except.
        for proc in psutil.process_iter():
            try:
                cmd = " ".join(proc.cmdline() or [])
                if any(s in cmd for s in ("KrabEarAgent", "KrabEar/backend/service.py", "gigaam_worker")):
                    mem = proc.memory_info()
                    kind = "agent" if "KrabEarAgent" in cmd else (
                        "worker" if "gigaam_worker" in cmd else "backend"
                    )
                    matches.append({
                        "pid": proc.pid,
                        "name": proc.name(),
                        "rss_mb": round(mem.rss / 1024 / 1024, 1),
                        "vsz_mb": round(mem.vms / 1024 / 1024, 1),
                        "kind": kind,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, PermissionError, SystemError, OSError):
                continue

        return {"ok": True, "processes": matches}

    def _handle_handle_error_action(self, params: dict) -> dict:
        """Выполняет actionable-действие по action_id из toast/diagnostics кнопки."""
        from backend import error_actions as _error_actions  # noqa: F401  side-effect: registers ACTION_HANDLERS for ErrorActionRouter
        action_id = params.get("action_id")
        if not action_id:
            return {"executed": False, "reason": "missing action_id", "side_effect": None}
        return _error_actions.handle_action(
            action_id,
            settings_service=self._settings_svc,
            store=getattr(self, "store", None),
        )

    def _handle_report_paste_failure(self, params: dict) -> dict:
        """Swift→backend report когда paste fails (AX denied / app unsupported).

        Backend transforms into KrabError and pushes to error_bus.

        Params:
            reason (str): "ax_denied" | "app_unsupported"
            app_bundle (str): bundle identifier of the target app
        """
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        reason = params.get("reason", "")
        app_bundle = params.get("app_bundle", "")
        code_map = {
            "ax_denied": "paste.ax_denied",
            "app_unsupported": "paste.app_unsupported",
        }
        code = code_map.get(reason)
        if code is None:
            return {"ok": False, "reason": "unknown_paste_reason"}
        entry = ERROR_REGISTRY[code]
        err = KrabError(
            severity=entry["severity"],
            component="paste",
            code=code,
            message_user=entry["user_msg_ru"],
            message_debug=f"paste failed reason={reason} app={app_bundle}",
            timestamp=datetime.now(timezone.utc),
            context={"app_bundle": app_bundle, "reason": reason},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        self._error_bus.push(err)
        return {"ok": True, "code": code}

    def _handle_report_hotkey_conflict(self, params: dict) -> dict:
        """Swift→backend report когда RegisterEventHotKey returns eventHotKeyExistsErr.

        Backend transforms into KrabError and pushes to error_bus.

        Params:
            chord (str): chord identifier e.g. "right_option"
        """
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        chord = params.get("chord", "")
        entry = ERROR_REGISTRY["hotkey.conflict"]
        err = KrabError(
            severity=entry["severity"],
            component="hotkey",
            code="hotkey.conflict",
            message_user=entry["user_msg_ru"],
            message_debug=f"hotkey conflict chord={chord}",
            timestamp=datetime.now(timezone.utc),
            context={"chord": chord},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        self._error_bus.push(err)
        return {"ok": True}

    def _handle_handshake(self, params: dict) -> dict:
        """Swift→backend handshake on connect.

        Verifies version compatibility and returns backend metadata.
        Swift sends this once immediately after establishing a connection.

        Params:
            swift_agent_version (str): Swift agent bundle version, e.g. "1.0.0"
            capabilities (list[str]): declared Swift capabilities,
                e.g. ["error_bus_consumer", "live_subs", "selection_translator"]
        """
        swift_version = params.get("swift_agent_version", "unknown")
        swift_capabilities = params.get("capabilities", [])
        logger.info(
            "IPC handshake: swift_version=%s capabilities=%s",
            swift_version, swift_capabilities,
        )
        # Collect registered method names for capability negotiation.
        # We can't reference _dispatch (local variable) here, so enumerate
        # a representative stable subset for phase compatibility checks.
        return {
            "ok": True,
            "backend_version": "1.0.0",
            "phase_b_capable": True,   # has list_recent_errors, report_paste_failure, etc.
            "phase_c_capable": True,   # has handshake, report_reconnect
            "swift_version_ack": swift_version,
        }

    def _handle_report_reconnect(self, params: dict) -> dict:
        """Swift→backend reconnect telemetry.

        Called after Swift IPCClient successfully reconnects after N retries.
        Pushes an ipc.reconnect info-severity event so the user gets visibility
        on transient IPC breaks.

        Params:
            attempts (int): number of retry attempts before success (1-5)
            duration_ms (int): total elapsed reconnect time in milliseconds
        """
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        attempts = int(params.get("attempts", 0))
        duration_ms = int(params.get("duration_ms", 0))
        entry = ERROR_REGISTRY["ipc.reconnect"]
        err = KrabError(
            severity=entry["severity"],
            component="ipc",
            code="ipc.reconnect",
            message_user=entry["user_msg_ru"],
            message_debug=f"reconnected after {attempts} attempts in {duration_ms}ms",
            timestamp=datetime.now(timezone.utc),
            context={"attempts": attempts, "duration_ms": duration_ms},
            actionable=False,
            action_id=None,
        )
        self._error_bus.push(err)
        return {"ok": True}

    # ── Binary drift helpers ─────────────────────────────────────────────────

    @staticmethod
    def _dwarfdump_uuid(path: Path) -> str | None:
        """Return the first UUID reported by dwarfdump for *path*, or None on error."""
        import subprocess
        try:
            result = subprocess.run(
                ["dwarfdump", "--uuid", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                # Example line: "UUID: AABBCC... (arm64) /path/to/binary"
                parts = line.split()
                if parts and parts[0] == "UUID:":
                    return parts[1]
        except Exception:
            pass
        return None

    def _push_binary_drift_error(self, bundle_uuid: str | None, runtime_uuid: str | None) -> None:
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        entry = ERROR_REGISTRY["agent.binary_drift"]
        err = KrabError(
            severity=entry["severity"],
            component="agent",
            code="agent.binary_drift",
            message_user=entry["user_msg_ru"],
            message_debug=(
                f"binary drift detected: bundle_uuid={bundle_uuid} runtime_uuid={runtime_uuid}"
            ),
            timestamp=datetime.now(timezone.utc),
            context={"bundle_uuid": bundle_uuid, "runtime_uuid": runtime_uuid},
            actionable=entry["actionable"],
            action_id=entry["action_id"],
        )
        self._error_bus.push(err)

    def _push_startup_error(self, code: str, debug_msg: str) -> None:
        """Push a KrabError to the error bus using a registry-defined code.

        Used for one-time startup checks (e.g. missing ffmpeg) that don't
        have a dedicated call site deeper in the stack. Never raises.
        """
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code)
            if entry is None:
                logger.warning("_push_startup_error: unknown code=%s", code)
                return
            component = code.split(".")[0] if "." in code else "system"
            err = KrabError(
                severity=entry["severity"],
                component=component,
                code=code,
                message_user=entry["user_msg_ru"],
                message_debug=debug_msg,
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=entry["actionable"],
                action_id=entry["action_id"],
            )
            self._error_bus.push(err)
        except Exception:
            logger.exception("_push_startup_error failed for code=%s", code)

    def _check_binary_drift_on_startup(self) -> None:
        """Startup Option-B drift check.

        Compares dwarfdump UUIDs of the two KrabEarAgent binaries:
          - bundle:  <PROJECT_ROOT>/Krab Ear.app/Contents/MacOS/KrabEarAgent
          - runtime: <PROJECT_ROOT>/native/runtime/KrabEarAgent

        Silently skips if either path is absent or dwarfdump is unavailable.
        """
        bundle_bin = PROJECT_ROOT / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent"
        runtime_bin = PROJECT_ROOT / "native" / "runtime" / "KrabEarAgent"
        if not bundle_bin.exists() or not runtime_bin.exists():
            logger.debug(
                "binary_drift_check skipped: one or both paths absent "
                "(bundle=%s exists=%s, runtime=%s exists=%s)",
                bundle_bin, bundle_bin.exists(), runtime_bin, runtime_bin.exists(),
            )
            return
        bundle_uuid = self._dwarfdump_uuid(bundle_bin)
        runtime_uuid = self._dwarfdump_uuid(runtime_bin)
        if bundle_uuid is None or runtime_uuid is None:
            logger.debug(
                "binary_drift_check skipped: dwarfdump unavailable or returned no UUID "
                "(bundle_uuid=%s, runtime_uuid=%s)", bundle_uuid, runtime_uuid,
            )
            return
        if bundle_uuid != runtime_uuid:
            logger.warning(
                "binary_drift detected at startup: bundle=%s runtime=%s",
                bundle_uuid, runtime_uuid,
            )
            self._push_binary_drift_error(bundle_uuid, runtime_uuid)
        else:
            logger.debug("binary_drift_check OK: UUIDs match (%s)", bundle_uuid)

    def _handle_probe_llm_http(self, params: dict) -> dict:
        """Delegated to HealthCheckService.handle_probe_llm_http (W1181 F3 MED)."""
        return self._health_check_svc.handle_probe_llm_http(params)

    def _handle_warmup_stt(self, params: dict) -> dict:
        """Ручной запуск STT warmup — полезен после смены профиля или модели.

        Загружает текущую активную Whisper-модель через tiny (1s silent) inference.
        Блокирующий вызов — выполняется в потоке IPC handler'а, возвращает
        результат только после завершения warmup (или ошибки).

        Returns:
            {
              "loaded": bool,      # True если warmup inference прошёл без ошибок
              "latency_ms": int,   # время inference в мс
              "model_name": str,   # имя прогретой модели
              "error": str | None  # сообщение об ошибке (None если loaded=True)
            }
        """
        if not hasattr(self.transcriber, "engine"):
            return {"loaded": False, "latency_ms": 0, "model_name": "", "error": "engine not available"}
        return self.transcriber.engine.warmup()

    def _handle_warmup_rewriter(self, params: dict) -> dict:
        """Ручной запуск LLM rewriter warmup probe.

        Отправляет минимальный (max_tokens=1) запрос в LM Studio для прогрева модели.
        НЕ трогает circuit breaker — warmup не является user-facing вызовом.

        Params:
            timeout_sec (float | None): таймаут в секундах; по умолчанию из настроек.

        Returns:
            {
              "ok": bool,          # True если HTTP 200
              "latency_ms": int,   # время ответа в мс
              "error": str | None, # описание ошибки или None
              "model": str | None  # имя используемой модели
            }
        """
        if self._llm_rewriter is None:
            return {"ok": False, "latency_ms": 0, "error": "rewriter_disabled", "model": None}
        runtime_timeout = self._get_runtime_setting("rewriter_warmup_timeout_sec", 15)
        timeout_sec = float(params.get("timeout_sec") or runtime_timeout)
        result = self._llm_rewriter.warmup_probe(timeout_sec=timeout_sec)
        result["model"] = getattr(self._llm_rewriter, "_model", None)
        return result

    def _handle_get_shutdown_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус последнего graceful shutdown.

        Returns:
            dict с ключами: clean (bool|None), last_shutdown_time (str|None),
            shutdown_in_progress (bool).
        """
        return self._shutdown_handler.get_shutdown_status()

    def _handle_get_startup_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService.handle_get_startup_diagnostics (W1181 F3 MED)."""
        return self._health_check_svc.handle_get_startup_diagnostics(params)

    def _handle_get_throttle_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статистику IPC throttle.

        Полезно для диагностики: показывает вызовы и отклонения по методам.
        Returns dict из IPCThrottle.get_throttle_stats() или {"enabled": false}.
        """
        if self._ipc_throttle is None:
            return {"enabled": False, "total_calls": 0, "total_throttled": 0, "methods": {}}
        stats = self._ipc_throttle.get_throttle_stats()
        stats["enabled"] = True
        return stats

    def _handle_get_recording_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает кумулятивную статистику записей: длительность, языки, LLM, диаризация.

        Сканирует всю активную историю через store и агрегирует метаданные.
        """
        active = self.store._load_active_items_with_lock()

        now = datetime.now()
        today_iso = now.date().isoformat()
        week_start = (now - timedelta(days=now.weekday())).date().isoformat()

        total_count = 0
        total_duration_sec = 0.0
        today_count = 0
        today_duration_sec = 0.0
        week_count = 0
        week_duration_sec = 0.0
        llm_applied_count = 0
        diarization_used_count = 0
        lang_counts: dict[str, int] = {}

        for item in active:
            total_count += 1
            dur = item.audio_duration_sec or 0.0
            total_duration_sec += dur

            day_str = item.ts[:10]  # "YYYY-MM-DD"
            if day_str == today_iso:
                today_count += 1
                today_duration_sec += dur
            if day_str >= week_start:
                week_count += 1
                week_duration_sec += dur

            if item.llm_applied:
                llm_applied_count += 1

            if item.diarization is not None and isinstance(item.diarization, dict):
                if item.diarization.get("enabled"):
                    diarization_used_count += 1

            lang = item.source_lang.strip()
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

        avg_duration = round(total_duration_sec / total_count, 2) if total_count else 0.0
        llm_rate = round(llm_applied_count / total_count, 4) if total_count else 0.0
        diarization_rate = round(diarization_used_count / total_count, 4) if total_count else 0.0

        most_used_lang = ""
        if lang_counts:
            most_used_lang = max(lang_counts, key=lambda k: lang_counts[k])

        return {
            "total_count": total_count,
            "total_duration_sec": round(total_duration_sec, 2),
            "today_count": today_count,
            "today_duration_sec": round(today_duration_sec, 2),
            "week_count": week_count,
            "week_duration_sec": round(week_duration_sec, 2),
            "avg_duration_sec": avg_duration,
            "most_used_lang": most_used_lang,
            "lang_distribution": [
                {"lang": lang, "count": cnt}
                for lang, cnt in sorted(lang_counts.items(), key=lambda p: p[1], reverse=True)[:10]
            ],
            "llm_applied_count": llm_applied_count,
            "llm_correction_rate": llm_rate,
            "diarization_used_count": diarization_used_count,
            "diarization_usage_rate": diarization_rate,
        }

    def _handle_get_metrics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Снимок метрик реального времени: сессия, LLM, call_assist, конфиг."""
        settings = self._cached_settings()

        # Active session info
        preview_active = self._recording_core_svc.preview_thread_alive

        return {
            "session": {
                "recording_active": bool(getattr(self.recorder, 'is_recording', False)),
                "preview_active": preview_active,
                "preview_text_length": len(self._recording_core_svc.preview_text),
                "preview_duration_sec": self._recording_core_svc.preview_duration_sec,
            },
            "preview_loop": {
                "error_count": self._recording_core_svc.preview_error_count,
                "last_reset_ts": self._recording_core_svc.preview_error_last_reset_ts,
            },
            "llm": {
                "enabled": settings.get("llm_rewrite_enabled", False),
                "model": settings.get("llm_model", "?"),
                "status": self._llm_rewriter.status() if self._llm_rewriter else None,
            },
            "call_assist": self._call_assist.state,
            "import": {
                # Check if import is active by looking at import queue state
                "active": False,  # Would need import state tracking
            },
            "config_snapshot": {
                "quality": settings.get("quality_profile", "balanced"),
                "cleanup": settings.get("cleanup_profile", "soft"),
                "translation_mode": settings.get("translation_mode", "off"),
                "diarization": settings.get("diarization_enabled", False),
                "network_mode": settings.get("network_mode", "offline_default"),
            },
        }

    def _handle_list_llm_models(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список моделей доступных в LM Studio через /api/v1/models.

        Используется GUI для динамического заполнения dropdown'а выбора LLM-модели.
        При недоступности LM Studio возвращает пустой список с описанием ошибки.
        Таймаут 3 секунды — не блокирует UI.
        """
        try:
            import re as _re
            import requests as _requests
            cached = self._settings_svc.cached_settings()
            base_url = str(cached.get("llm_base_url", "http://127.0.0.1:1234/v1")).rstrip("/")
            api_key = str(cached.get("llm_api_key", ""))
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            # Wave 68 (LM Studio probe fix): /v1/models возвращает 200 но логирует ERROR
            # в LM Studio. /api/v1/models — корректный endpoint. Same pattern as PR #396
            # для llm_rewriter.py:1064 (passive_health_check).
            _host = _re.sub(r"/v\d+$", "", base_url)
            resp = _requests.get(
                f"{_host}/api/v1/models",
                headers=headers,
                timeout=3,
            )
            if resp.status_code != 200:
                return {"models": [], "error": f"http_{resp.status_code}"}
            data = resp.json()
            ids = [
                item.get("id")
                for item in data.get("data", [])
                if item.get("id")
            ]
            recommended_models = [
                "qwen3-4b-abliterated",
                "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
                "qwen3-8b-abliterated",
            ]
            return {
                "models": sorted(ids),
                "recommended_models": recommended_models,
                "error": None,
            }
        except Exception as exc:
            return {"models": [], "recommended_models": [], "error": str(exc)}

    def _handle_replace_word_in_last_transcript(self, params: dict[str, Any]) -> dict[str, Any]:
        """Заменяет слово в последней (или указанной) записи истории без перезаписи.

        Параметры:
          - old_word: str — слово для замены (не пустое).
          - new_word: str — новое слово (не пустое).
          - history_id: str | None — ID записи; если не указан, берётся последняя запись.

        Возвращает:
          {"ok": bool, "replaced_count": int, "history_id": str | None, "new_text": str | None}

        Ошибки (ok=False):
          - "missing_words"    — old_word или new_word пусты.
          - "no_recent_history" — история пуста и history_id не указан.
          - "item_not_found"   — запись с history_id не найдена.
          - "word_not_found"   — слово не найдено в тексте (с учётом границ слова).
        """
        import re

        old = str(params.get("old_word", "")).strip()
        new = str(params.get("new_word", "")).strip()
        if not old or not new:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "missing_words"}

        history_id = str(params.get("history_id", "")).strip() or None

        if history_id is None:
            # Берём самую последнюю запись
            with self.store._lock():
                active = self.store._load_active_items_unlocked()
            history_id = active[-1].id if active else None

        if history_id is None:
            return {"ok": False, "replaced_count": 0, "history_id": None, "error": "no_recent_history"}

        item = self.store.get_history_item_by_id(history_id)
        if item is None:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "item_not_found"}

        # Замена с учётом границ слова и без учёта регистра
        pattern = re.compile(r'\b' + re.escape(old) + r'\b', re.IGNORECASE)
        new_text, replaced_count = pattern.subn(new, item.text)

        if replaced_count == 0:
            return {"ok": False, "replaced_count": 0, "history_id": history_id, "error": "word_not_found"}

        self.store.update_history_item_text(history_id, new_text)
        logger.info(
            "replace_word_in_last_transcript: history_id=%s old=%r new=%r count=%d",
            history_id,
            old,
            new,
            replaced_count,
        )
        return {"ok": True, "replaced_count": replaced_count, "history_id": history_id, "new_text": new_text}
    # --- Privacy audit log handlers ---

    def _handle_get_privacy_audit_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние записи privacy audit log.

        Параметры:
            limit — максимальное число записей (default 100).

        Возвращает:
            entries     — список NDJSON-записей {ts, category, action, details}.
            total_count — общее число записей в файле.
        """
        limit = int(params.get("limit", 100))
        audit = get_privacy_audit_logger()
        entries = audit.read_entries(limit=limit)
        total = audit.total_count()
        return {
            "entries": entries,
            "total_count": total,
        }

    def _handle_clear_privacy_audit_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет файл privacy audit log. Идемпотентен.

        Возвращает:
            ok — всегда True.
        """
        audit = get_privacy_audit_logger()
        audit.clear()
        return {"ok": True}

    def _handle_clear_translation_cache(self, params: dict[str, Any]) -> dict[str, Any]:
        """Очищает персистентный LRU-кэш переводов (память + файл на диске).

        Полезно при смене языковой пары, обновлении моделей или ручном сбросе.
        Возвращает:
            ok       — True.
            entries  — количество записей до очистки.
        """
        entries_before = 0
        if self._translation_cache is not None:
            stats = self._translation_cache.get_stats()
            entries_before = stats.get("entries", 0)
            self._translation_cache.clear()
        return {"ok": True, "entries_cleared": entries_before}

    # --- D.2.3: Scored STT routing decision ---

    def _handle_get_stt_routing_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает результат scored STT adapter selection для отладки.

        Параметры:
            language         — ISO 639-1 код языка (например «ru», «en», «zh»).
            audio_duration_s — длительность аудио в секундах (float, опционально).

        Возвращает:
            selected_engine — имя выбранного адаптера или null.
            scores          — dict {engine_name: score} для всех доступных адаптеров.
            language        — нормализованный код языка.
            audio_duration_s — длительность из params или null.
        """
        from core.stt_router import score_adapters, select_adapter_scored

        language = str(params.get("language", "")).strip().lower() or "und"
        raw_dur = params.get("audio_duration_s")
        audio_duration_s: Optional[float] = float(raw_dur) if raw_dur is not None else None

        # Собираем доступные адаптеры из AudioEngine (если есть)
        # Используем duck-typed stubs на основе настроек — без реального импорта адаптеров.
        adapters = self._build_virtual_adapters_for_routing()
        scores = score_adapters(adapters, language, audio_duration_s)
        best = select_adapter_scored(language, audio_duration_s, adapters)
        selected_name: Optional[str] = getattr(best, "name", None) if best is not None else None

        return {
            "selected_engine": selected_name,
            "scores": scores,
            "language": language,
            "audio_duration_s": audio_duration_s,
        }

    def _build_virtual_adapters_for_routing(self) -> "list[Any]":
        """Создаёт список виртуальных адаптеров для scored selection.

        Не загружает реальные модели — только описывает возможности каждого
        адаптера на основе настроек. Используется в IPC для отладки routing.
        """
        from types import SimpleNamespace

        def _make(name: str, languages: "set[str]", enabled: bool) -> "Any":
            ns = SimpleNamespace(
                name=name,
                supported_languages=languages,
            )
            ns.is_available = lambda: enabled  # type: ignore[attr-defined]
            return ns

        adapters = []

        # GigaAM — RU-only specialist
        gigaam_enabled = getattr(settings, "STT_GIGAAM_ENABLED", False)
        adapters.append(_make("gigaam", {"ru", "uk"}, bool(gigaam_enabled)))

        # Parakeet — EN-only specialist
        parakeet_enabled = getattr(settings, "PARAKEET_ENABLED", False)
        adapters.append(_make("parakeet", {"en"}, bool(parakeet_enabled)))

        # SenseVoice — ZH/JA/KO/YUE specialist + decent EN/RU
        sensevoice_enabled = getattr(settings, "SENSEVOICE_ENABLED", False)
        adapters.append(_make("sensevoice", {"zh", "ja", "ko", "yue", "en", "ru"}, bool(sensevoice_enabled)))

        # Whisper-MLX — multilingual generalist (empty set = multilingual)
        adapters.append(_make("whisper-mlx", set(), True))

        return adapters

    def _handle_summarize_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует LLM-summary для элемента истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("Параметр id обязателен")

        # Найти элемент в истории
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        target = None
        for item in items:
            if item.id == item_id:
                target = item
                break
        if target is None:
            raise RuntimeError(f"Элемент не найден: {item_id}")

        text = target.text or ""
        if len(text) < 50:
            raise RuntimeError("Текст слишком короткий для summary")

        summary = self._generate_summary(text)
        if summary is None:
            # Fallback на локальный summary
            local = self._summarize_text_locally(text, mode="summary_short", max_points=3)
            return {
                "id": item_id,
                "summary": local["summary"],
                "llm": False,
                "source_chars": len(text),
            }

        return {
            "id": item_id,
            "summary": summary,
            "llm": True,
            "source_chars": len(text),
        }

    def _handle_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Извлекает задачи/решения/вопросы из транскрипта по item_id через LLM."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("Параметр id обязателен")

        if self._action_items_extractor is None:
            raise RuntimeError("LLM не включён (LLM_ENABLED=False)")

        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        target = next((it for it in items if it.id == item_id), None)
        if target is None:
            raise RuntimeError(f"Элемент не найден: {item_id}")

        text = target.text or ""
        language = str(params.get("language", "ru")).lower()

        result = self._action_items_extractor.extract(text, language=language)

        if result.ok:
            self.store.update_history_item_action_items(
                item_id=item_id,
                action_items=[ai.to_dict() for ai in result.action_items],
                decisions=result.decisions,
                questions=result.questions,
            )

        return {
            "id": item_id,
            "ok": result.ok,
            "action_items": [ai.to_dict() for ai in result.action_items],
            "decisions": result.decisions,
            "questions": result.questions,
            "fallback_reason": result.fallback_reason,
            "latency_ms": result.latency_ms,
        }

    def _handle_batch_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Пакетное извлечение задач/решений/вопросов для нескольких item_id."""
        item_ids = params.get("ids", [])
        if not isinstance(item_ids, list):
            raise RuntimeError("Параметр ids должен быть списком")
        language = str(params.get("language", "ru")).lower()

        if self._action_items_extractor is None:
            raise RuntimeError("LLM не включён (LLM_ENABLED=False)")

        with self.store._lock():
            all_items = self.store._load_active_items_unlocked()
        items_by_id = {it.id: it for it in all_items}

        results = []
        for item_id in item_ids:
            item_id = str(item_id).strip()
            target = items_by_id.get(item_id)
            if target is None:
                results.append({"id": item_id, "ok": False, "error": "not_found"})
                continue
            text = target.text or ""
            result = self._action_items_extractor.extract(text, language=language)
            if result.ok:
                self.store.update_history_item_action_items(
                    item_id=item_id,
                    action_items=[ai.to_dict() for ai in result.action_items],
                    decisions=result.decisions,
                    questions=result.questions,
                )
            results.append({
                "id": item_id,
                "ok": result.ok,
                "action_items": [ai.to_dict() for ai in result.action_items],
                "decisions": result.decisions,
                "questions": result.questions,
                "fallback_reason": result.fallback_reason,
                "latency_ms": result.latency_ms,
            })

        return {"results": results, "count": len(results)}

    def _handle_get_pending_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает все items у которых action_items=None (ещё не анализировались).

        Параметр min_duration_sec: минимальная длительность для фильтрации (опционально).
        """
        min_duration = float(params.get("min_duration_sec", 0.0))

        with self.store._lock():
            items = self.store._load_active_items_unlocked()

        pending = []
        for item in items:
            if item.action_items is not None:
                continue
            if min_duration > 0 and (item.audio_duration_sec or 0.0) < min_duration:
                continue
            pending.append({
                "id": item.id,
                "ts": item.ts,
                "text_preview": (item.text or "")[:100],
                "audio_duration_sec": item.audio_duration_sec,
            })

        return {"pending": pending, "count": len(pending)}

    def _handle_get_last_llm_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последний word-level diff от LLM rewriter'а."""
        engine = self.transcriber.engine
        diff = getattr(engine, '_last_llm_diff', None)
        if diff is None:
            return {"available": False, "diff": None}
        return {
            "available": True,
            "diff": {
                "similarity_ratio": diff.similarity_ratio,
                "words_added": diff.words_added,
                "words_removed": diff.words_removed,
                "words_unchanged": diff.words_unchanged,
                "summary": diff.summary,
                "changes": [
                    {"type": c.type, "text": c.text, "position": c.position}
                    for c in diff.changes
                ],
            },
        }

    def _handle_list_audio_inputs(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_list_audio_inputs(params)

    def _handle_get_audio_devices(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_get_audio_devices(params)

    def _handle_transcribe_paths(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_transcribe_paths(params)

    def _transcribe_paths_core(self, params, *, progress_callback=None, cancel_check=None, on_file_start=None, on_file_done=None):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._transcribe_paths_core(params, progress_callback=progress_callback, cancel_check=cancel_check, on_file_start=on_file_start, on_file_done=on_file_done)

    def _handle_transcribe_paths_async(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_transcribe_paths_async(params)

    def _handle_get_transcribe_progress(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_get_transcribe_progress(params)

    def _handle_cancel_transcribe_job(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_cancel_transcribe_job(params)

    def _handle_preview_transcribe_paths(self, params):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc.handle_preview_transcribe_paths(params)

    @staticmethod
    def _collect_audio_paths(paths):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._collect_audio_paths(paths)

    def _start_preview_worker(self, quality_profile: str) -> None:
        """Delegated to RecordingCoreService."""
        self._recording_core_svc._start_preview_worker(quality_profile=quality_profile)

    def _stop_preview_worker(self) -> None:
        """Delegated to RecordingCoreService."""
        self._recording_core_svc._stop_preview_worker()

    def _reset_preview_state(self) -> None:
        """Delegated to RecordingCoreService."""
        self._recording_core_svc.reset_preview_state()

    def _preview_loop(self, quality_profile: str) -> None:
        """Delegated to RecordingCoreService."""
        self._recording_core_svc._preview_loop(quality_profile)

    def _stop_recorder_guarded(self, stop_tail_trim_ms: int):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._stop_recorder_guarded(stop_tail_trim_ms)

    @staticmethod
    def _looks_like_silence_audio(audio, sample_rate, rms_threshold, peak_threshold, active_ratio_threshold):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._looks_like_silence_audio(audio=audio, sample_rate=sample_rate, rms_threshold=rms_threshold, peak_threshold=peak_threshold, active_ratio_threshold=active_ratio_threshold)

    @staticmethod
    def _looks_like_distant_background_speech(audio, sample_rate, min_peak, min_rms, uniform_frame_threshold, max_uniform_active_ratio):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._looks_like_distant_background_speech(audio=audio, sample_rate=sample_rate, min_peak=min_peak, min_rms=min_rms, uniform_frame_threshold=uniform_frame_threshold, max_uniform_active_ratio=max_uniform_active_ratio)

    @staticmethod
    def _is_known_prompt_echo(normalized_text):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._is_known_prompt_echo(normalized_text)

    @staticmethod
    def _contains_repeated_chunk(words, min_repeats=3):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._contains_repeated_chunk(words, min_repeats)

    @staticmethod
    def _looks_like_looping_artifact(words, min_words, min_bigram_hits):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._looks_like_looping_artifact(words, min_words, min_bigram_hits)

    @staticmethod
    def _postprocess_transcribed_text(text):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._postprocess_transcribed_text(text)

    @staticmethod
    def _collapse_immediate_duplicate_phrase(normalized_text):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._collapse_immediate_duplicate_phrase(normalized_text)

    @staticmethod
    def _postprocess_preview_text(text):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._postprocess_preview_text(text)

    @staticmethod
    def _extract_transcribed_text(payload):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._extract_transcribed_text(payload)

    @staticmethod
    def _extract_transcribed_error(payload):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._extract_transcribed_error(payload)

    def _generate_summary(self, text):
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._generate_summary(text)

    @staticmethod
    def _format_text_with_speakers(text, diarization):
        """Delegated to RecordingCoreService."""
        from backend.recording_core_service import RecordingCoreService as _RCS
        return _RCS._format_text_with_speakers(text, diarization)

    # ------------------------------------------------------------------
    # Handlers: Disk status, storage breakdown, microphone test
    # ------------------------------------------------------------------

    def _handle_get_disk_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущий статус дискового пространства (немедленная проверка)."""
        return self._disk_monitor.check_now()

    def _handle_get_storage_breakdown(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает разбивку использования диска по компонентам."""
        return self.store.get_storage_breakdown()

    def _handle_test_microphone(self, params: dict[str, Any]) -> dict[str, Any]:
        """Записывает короткий фрагмент аудио и возвращает RMS/peak уровни."""
        import numpy as np
        duration = min(float(params.get("duration_sec", 2.0)), 5.0)
        try:
            import sounddevice as sd  # type: ignore
            sample_rate = 16000
            frames = int(duration * sample_rate)
            audio_data = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
            sd.wait()
            audio_flat = audio_data.flatten()
            rms = float(np.sqrt(np.mean(audio_flat ** 2)))
            peak = float(np.max(np.abs(audio_flat)))
            return {
                "ok": True,
                "duration_sec": duration,
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "devices": self._recording_core_svc._list_audio_inputs(),
            }
        except Exception as exc:
            logger.warning("test_microphone: ошибка записи — %s", exc)
            return {
                "ok": False,
                "error": str(exc),
                "devices": self._recording_core_svc._list_audio_inputs(),
            }

    # ------------------------------------------------------------------
    # Handlers: ActivityCalendar
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Handlers: DailyDigest, QualityTrends, PeriodComparison, IntegrityChecker,
    #           TermExtractor, TextComparator
    # ------------------------------------------------------------------

    def _handle_generate_daily_digest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует ежедневный дайджест транскрипций за указанную дату."""
        date_str = params.get("date")  # None → today
        digest = self._daily_digest.generate_digest(date_str=date_str, store=self.store)
        return {
            "date": digest.date,
            "total_recordings": digest.total_recordings,
            "total_duration_min": digest.total_duration_min,
            "total_words": digest.total_words,
            "languages_used": digest.languages_used,
            "top_topics": digest.top_topics,
            "highlights": digest.highlights,
            "markdown": digest.formatted_markdown,
        }

    def _handle_generate_stats_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует полный Markdown-отчёт статистики использования за период."""
        days = int(params.get("days", 30))
        markdown = self._stats_report.generate_report(store=self.store, days=days)
        return {"markdown": markdown, "days": days}

    def _handle_generate_mini_stats_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует краткий 5-строчный Markdown-отчёт состояния."""
        markdown = self._stats_report.generate_mini_report(store=self.store)
        return {"markdown": markdown}

    def _handle_compare_periods(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает статистику двух временных периодов."""
        p1_start = params.get("period1_start")
        p1_end = params.get("period1_end")
        p2_start = params.get("period2_start")
        p2_end = params.get("period2_end")
        if not all([p1_start, p1_end, p2_start, p2_end]):
            raise ValueError("Необходимы параметры: period1_start, period1_end, period2_start, period2_end")
        report = _compare_periods_fn(
            store=self.store,
            period1_start=p1_start,
            period1_end=p1_end,
            period2_start=p2_start,
            period2_end=p2_end,
        )
        return {
            "period1": {
                "recordings": report.period1.recordings,
                "duration_sec": report.period1.duration_sec,
                "words": report.period1.words,
                "avg_confidence": report.period1.avg_confidence,
                "languages": report.period1.languages,
            },
            "period2": {
                "recordings": report.period2.recordings,
                "duration_sec": report.period2.duration_sec,
                "words": report.period2.words,
                "avg_confidence": report.period2.avg_confidence,
                "languages": report.period2.languages,
            },
            "recordings_change_pct": report.recordings_change_pct,
            "duration_change_pct": report.duration_change_pct,
            "confidence_change": report.confidence_change,
            "new_languages": report.new_languages,
            "summary": report.summary,
        }

    def _handle_get_activity_calendar(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает GitHub-style activity calendar данные за последние N месяцев."""
        months = int(params.get("months", 12))
        months = max(1, min(months, 24))
        include_svg = bool(params.get("include_svg", False))
        cell_size = int(params.get("cell_size", 12))
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []
        calendar = self._activity_calendar.generate_calendar(items, months=months)
        result = calendar.to_dict()
        if include_svg:
            result["svg"] = self._activity_calendar.generate_calendar_svg(
                items, months=months, cell_size=cell_size
            )
        return result

    def _handle_get_recording_insights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует эвристические инсайты по записям за последние N дней."""
        days = int(params.get("days", 7))
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []
        insights = self._recording_insights.generate_insights(items, days=days)
        return {
            "insights": [i.to_dict() for i in insights],
            "count": len(insights),
            "days": days,
        }

    def _handle_get_sentiment_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует тренды тональности транскрипций за последние N дней."""
        days = int(params.get("days", 30))
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []
        report = self._sentiment_trends.analyze_sentiment_trends(items, days=days)
        return self._sentiment_trends.to_dict(report)

    def _handle_compare_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает несколько записей side-by-side."""
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError("Параметр item_ids обязателен (список строк)")
        view = self._recording_comparison.compare(item_ids=item_ids, store=self.store)
        return _comparison_view_to_dict(view)

    def _handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService.handle_check_integrity (W1181 F3 MED)."""
        return self._health_check_svc.handle_check_integrity(params)

    def _handle_repair_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Исправляет автоматически устраняемые проблемы целостности данных."""
        report = self._integrity_checker.check_integrity(self.store.data_dir)
        result = self._integrity_checker.repair(self.store.data_dir, report)
        return {
            "fixed": result.fixed,
            "skipped": result.skipped,
            "details": result.details,
        }

    def _handle_extract_terms(self, params: dict[str, Any]) -> dict[str, Any]:
        """Извлекает ключевые термины из текста."""
        text = params.get("text", "")
        language = params.get("language", "ru")
        if not text:
            return {"terms": []}
        terms = self._term_extractor.extract_terms(text, language=language)
        return {
            "terms": [
                {
                    "term": t.term,
                    "score": t.score,
                    "frequency": t.frequency,
                    "language": t.language,
                    "category": t.category,
                }
                for t in terms
            ]
        }

    def _handle_get_context_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние контекстной памяти STT.

        Params (опционально):
            max_words (int): максимальное кол-во контекстных слов (по умолчанию 20).
            last_n (int): кол-во последних транскрибаций для тем (по умолчанию 10).
            clear (bool): если true — очищает память перед возвратом результата.
        """
        if params.get("clear"):
            self._context_memory.clear()
            return {"cleared": True, "context_words": [], "recent_topics": [], "size": 0}

        max_words = int(params.get("max_words", 20))
        last_n = int(params.get("last_n", 10))
        return {
            "context_words": self._context_memory.get_context_words(max_words=max_words),
            "recent_topics": self._context_memory.get_recent_topics(last_n=last_n),
            "size": self._context_memory.size(),
            "window_size": 50,
        }

    def _handle_get_keyword_cloud(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует данные облака ключевых слов из истории транскрипций."""
        max_words = int(params.get("max_words", 100))
        language = params.get("language")
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []
        cloud_words = self._keyword_cloud_gen.generate_cloud(
            items, max_words=max_words, language=language
        )
        return {
            "words": [
                {
                    "word": cw.word,
                    "count": cw.count,
                    "weight": cw.weight,
                    "font_size": cw.font_size,
                }
                for cw in cloud_words
            ]
        }

    # ── Audio fingerprinting ─────────────────────────────────────────────────

    # ── Telegram Bridge ──────────────────────────────────────────────────────

    def _handle_send_to_telegram(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет текст в Telegram через main Krab userbot.

        Параметры:
          - text: str — текст сообщения (обязательный, не пустой).
          - chat_id: int | str — ID или username чата Telegram (обязательный).
          - reply_to: int | None — ID сообщения для цитирования (опционально).

        Возвращает:
          {message_id, sent_at, chat_title}

        Ошибки:
          - "bridge_disabled" — если TELEGRAM_BRIDGE_ENABLED=false.
          - "krab_unavailable" — если main Krab недоступен (503 / ConnectionError).
          - "circuit_open" — если circuit breaker разомкнут после 3 ошибок подряд.
        """
        if not settings.TELEGRAM_BRIDGE_ENABLED:
            raise RuntimeError("bridge_disabled: Telegram Bridge отключён в настройках")

        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("Параметр 'text' обязателен и не может быть пустым")

        raw_chat_id = params.get("chat_id")
        if raw_chat_id is None or str(raw_chat_id).strip() == "":
            raise ValueError("Параметр 'chat_id' обязателен")
        chat_id: int | str
        try:
            chat_id = int(raw_chat_id)
        except (ValueError, TypeError):
            chat_id = str(raw_chat_id).strip()

        reply_to_raw = params.get("reply_to")
        reply_to: int | None = None
        if reply_to_raw is not None:
            try:
                reply_to = int(reply_to_raw)
            except (ValueError, TypeError):
                pass

        try:
            result = self._telegram_bridge.send_message(
                text=text,
                chat_id=chat_id,
                reply_to=reply_to,
            )
        except CircuitBreakerOpen as exc:
            raise RuntimeError(f"circuit_open: {exc}") from exc
        except (Exception,) as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise RuntimeError(msg) from exc
            # ConnectionError, Timeout и др. — оборачиваем в понятный код
            raise RuntimeError(f"krab_unavailable: {msg}") from exc

        return result

    # ── Apple Notes integration (Phase D.4) ─────────────────────────────────

    def _handle_create_apple_note(self, params: dict) -> dict:
        """Create Apple Note from text via osascript.

        params: {"title": str, "body": str, "folder": str | None}
        Returns: {"ok": bool, "note_id": str | None, "error": str | None}
        """
        import subprocess

        title = params.get("title", "Krab Ear note").replace('"', '\\"')
        body = params.get("body", "").replace('"', '\\"')
        folder = params.get("folder", "") or ""

        if folder:
            folder_escaped = folder.replace('"', '\\"')
            script = f'''
tell application "Notes"
    tell account "iCloud"
        set targetFolder to folder "{folder_escaped}"
        make new note at targetFolder with properties {{name:"{title}", body:"{body}"}}
    end tell
end tell
'''
        else:
            script = f'''
tell application "Notes"
    make new note with properties {{name:"{title}", body:"{body}"}}
end tell
'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"ok": True, "note_id": result.stdout.strip(), "error": None}
            return {"ok": False, "note_id": None, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "note_id": None, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "note_id": None, "error": str(exc)}

    def _handle_create_apple_reminder(self, params: dict) -> dict:
        """Create Apple Reminder from text via osascript.

        params: {"title": str, "body": str, "list_name": str | None, "due_date": str | None}
        Returns: {"ok": bool, "error": str | None}
        """
        import subprocess

        title = params.get("title", "Krab Ear reminder").replace('"', '\\"')
        body = params.get("body", "").replace('"', '\\"')
        list_name = params.get("list_name") or None
        due_date = params.get("due_date") or None

        # Build properties clause
        properties = f'name:"{title}"'
        if body:
            properties += f', body:"{body}"'
        if due_date:
            due_date_escaped = due_date.replace('"', '\\"')
            properties += f', due date:date "{due_date_escaped}"'

        if list_name:
            list_name_escaped = list_name.replace('"', '\\"')
            script = f'''
tell application "Reminders"
    tell list "{list_name_escaped}"
        make new reminder with properties {{{properties}}}
    end tell
end tell
'''
        else:
            script = f'''
tell application "Reminders"
    make new reminder with properties {{{properties}}}
end tell
'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Apple Calendar integration (Phase D.4) ──────────────────────────────

    def _handle_create_calendar_event(self, params: dict) -> dict:
        """Create Apple Calendar event via osascript.

        params:
          title: str (required)
          notes: str (optional, default "")
          start_date: str (required, ISO 8601 or AppleScript-parseable date string)
          duration_minutes: int (optional, default 30)
          calendar_name: str | None (optional, default first writable calendar)
        Returns: {"ok": bool, "error": str | None}
        """
        import subprocess

        title = params.get("title", "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}

        title_esc = title.replace('"', '\\"')
        notes = params.get("notes", "") or ""
        notes_esc = notes.replace('"', '\\"')
        start_date = str(params.get("start_date", "")).strip()
        if not start_date:
            return {"ok": False, "error": "start_date is required"}
        start_date_esc = start_date.replace('"', '\\"')
        duration_minutes = int(params.get("duration_minutes", 30) or 30)
        calendar_name = params.get("calendar_name") or None

        event_block = f'''
        set startDate to date "{start_date_esc}"
        set endDate to startDate + ({duration_minutes} * minutes)
        make new event with properties {{summary:"{title_esc}", description:"{notes_esc}", start date:startDate, end date:endDate}}'''

        if calendar_name:
            cal_esc = calendar_name.replace('"', '\\"')
            script = f'''tell application "Calendar"
    tell calendar "{cal_esc}"{event_block}
    end tell
end tell'''
        else:
            script = f'''tell application "Calendar"
    tell (first calendar whose writable is true){event_block}
    end tell
end tell'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── iMessage integration (Phase D.4) ────────────────────────────────────

    def _handle_send_imessage(self, params: dict) -> dict:
        """Send iMessage/SMS via Messages.app using osascript.

        params:
          recipient: str (required) — phone number, email, or contact name
          body: str (required) — message text
          service: str (optional, default "iMessage") — "iMessage" | "SMS"
        Returns: {"ok": bool, "error": str | None}
        """
        import subprocess

        recipient = params.get("recipient", "").strip()
        if not recipient:
            return {"ok": False, "error": "recipient is required"}

        body = params.get("body", "").strip()
        if not body:
            return {"ok": False, "error": "body is required"}

        service_name = params.get("service", "iMessage") or "iMessage"
        if service_name not in ("iMessage", "SMS"):
            service_name = "iMessage"

        # Map service name to AppleScript service type constant
        service_type = "iMessage" if service_name == "iMessage" else "SMS"

        # Escape double quotes to prevent AppleScript injection
        recipient_esc = recipient.replace('"', '\\"')
        body_esc = body.replace('"', '\\"')

        script = f'''tell application "Messages"
    set targetService to 1st service whose service type = {service_type}
    set targetBuddy to buddy "{recipient_esc}" of targetService
    send "{body_esc}" to targetBuddy
end tell'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _handle_list_telegram_chats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных чатов через main Krab userbot.

        Параметры: нет.

        Возвращает:
          {chats: [{id, title, type}, ...]}

        Ошибки:
          - "bridge_disabled" — если TELEGRAM_BRIDGE_ENABLED=false.
          - "krab_unavailable" — если main Krab недоступен (503 / ConnectionError).
          - "circuit_open" — если circuit breaker разомкнут.
        """
        if not settings.TELEGRAM_BRIDGE_ENABLED:
            raise RuntimeError("bridge_disabled: Telegram Bridge отключён в настройках")

        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": True, "chats": [], "skipped": "privacy_mode"}

        try:
            chats = self._telegram_bridge.get_chats()
        except CircuitBreakerOpen as exc:
            raise RuntimeError(f"circuit_open: {exc}") from exc
        except Exception as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise RuntimeError(msg) from exc
            raise RuntimeError(f"krab_unavailable: {msg}") from exc

        return {"chats": chats}

    # ── Phase 3: Call Session CRUD ───────────────────────────────────────────

    # ------------------------------------------------------------------
    # STT hotwords (initial_prompt boost)
    # ------------------------------------------------------------------

    # Whisper initial_prompt hard limit: ~224 tokens ≈ ~170 avg words.
    # We cap hotwords at 100 entries (≈ safe budget) to avoid prompt overflow.
    # When the list exceeds this limit, oldest entries are dropped (FIFO).
    _STT_HOTWORDS_MAX: int = 100

    def _handle_add_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет термин в список STT hotwords.

        Параметры:
          - word: str — термин для добавления (имя, бренд, технический термин).

        Возвращает: {hotwords: list[str], truncated: bool} — обновлённый список.
          truncated=True если список обрезан до _STT_HOTWORDS_MAX.
        """
        word = str(params.get("word") or "").strip()
        if not word:
            raise ValueError("Параметр 'word' обязателен и не может быть пустым")
        current: list[str] = self._settings_svc.cached_settings().get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        truncated = False
        if word not in current:
            current = current + [word]
            # Enforce per-IPC budget: drop oldest entries when limit exceeded.
            if len(current) > self._STT_HOTWORDS_MAX:
                excess = len(current) - self._STT_HOTWORDS_MAX
                logger.warning(
                    "stt_hotwords: список превышает лимит %d — удаляем %d старых записей",
                    self._STT_HOTWORDS_MAX, excess,
                )
                current = current[excess:]
                truncated = True
            self._settings_svc.handle_set_settings({"stt_hotwords": current})
        return {"hotwords": current, "truncated": truncated}

    def _handle_remove_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет термин из списка STT hotwords.

        Параметры:
          - word: str — термин для удаления.

        Возвращает: {hotwords: list[str]} — обновлённый список.
        """
        word = str(params.get("word") or "").strip()
        if not word:
            raise ValueError("Параметр 'word' обязателен и не может быть пустым")
        current: list[str] = self._settings_svc.cached_settings().get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        updated = [w for w in current if w != word]
        if len(updated) != len(current):
            self._settings_svc.handle_set_settings({"stt_hotwords": updated})
        return {"hotwords": updated}

    def _handle_list_stt_hotwords(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущий список STT hotwords.

        Учитывает флаг stt_hotwords_enabled: если False — возвращает пустой список.

        Возвращает: {hotwords: list[str], enabled: bool}
        """
        s = self._settings_svc.cached_settings()
        enabled = bool(s.get("stt_hotwords_enabled", True))
        if not enabled:
            return {"hotwords": [], "enabled": False}
        current: list[str] = s.get("stt_hotwords", [])
        if not isinstance(current, list):
            current = []
        return {"hotwords": sorted(current), "enabled": True}

    # ── Timeline view ────────────────────────────────────────────────────────

    def _handle_get_timeline_view(self, params: dict[str, Any]) -> dict[str, Any]:
        """Группирует историю транскрипций по временным блокам (timeline).

        Параметры:
          - group_by: str — гранулярность: "hour", "day", "week" (по умолчанию "day").
          - limit: int — макс. записей для анализа (по умолчанию 500, макс. 5000).
          - include_heatmap: bool — включить activity heatmap (по умолчанию False).
          - heatmap_days: int — горизонт heatmap в днях (по умолчанию 30).
        """
        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        include_heatmap = bool(params.get("include_heatmap", False))
        heatmap_days = max(1, min(int(params.get("heatmap_days", 30)), 365))

        raw_items = self.store._load_active_items_with_lock()[:limit]
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        result: dict[str, Any] = {
            "blocks": [b.to_dict() for b in blocks],
            "total_blocks": len(blocks),
            "group_by": group_by,
        }

        if include_heatmap:
            heatmap = self._timeline_view.generate_activity_heatmap(raw_items, days=heatmap_days)
            result["activity_heatmap"] = heatmap

        return result

    def _handle_generate_auto_title(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует автоматический заголовок для транскрибации.

        Параметры:
            text (str): текст транскрибации (обязательный).
            timestamp (str): ISO 8601 timestamp (опциональный) — включает дату в заголовок.
            max_length (int): максимальная длина заголовка (по умолчанию 50).
            with_date (bool): если true и timestamp указан — включает дату.
            items (list): список записей для пакетной генерации (альтернатива text).

        Ответ (одиночный):
            {title: str}

        Ответ (пакетный):
            {titles: [{id, title, generated_at}]}
        """
        # Пакетный режим
        items = params.get("items")
        if items is not None:
            if not isinstance(items, list):
                raise ValueError("Параметр 'items' должен быть списком")
            titles = self._auto_title_generator.batch_generate(items)
            return {"titles": titles}

        # Одиночный режим
        text = str(params.get("text", "") or "")
        timestamp = str(params.get("timestamp", "") or "")
        max_length = int(params.get("max_length", 50))
        with_date = bool(params.get("with_date", False))

        if not text:
            return {"title": "Запись"}

        if with_date and timestamp:
            title = self._auto_title_generator.generate_title_with_date(text, timestamp)
        else:
            title = self._auto_title_generator.generate_title(text, max_length=max_length)

        return {"title": title}

    def _handle_get_learning_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_learning_stats — статистика прогресса изучения языка."""
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_get_learning_stats(params_with_store)

    def _handle_get_analytics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_analytics_dashboard — комплексный дашборд всех метрик аналитики.

        Параметры:
            days (int): окно анализа в днях (по умолчанию 30, макс. 365)

        Возвращает:
            overview, today, trends, languages, quality, engagement, storage, performance
        """
        days = max(1, min(int(params.get("days", 30) or 30), 365))
        return self._analytics_dashboard.get_full_dashboard(store=self.store, days=days)

    def _handle_get_topic_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_topic_timeline — таймлайн смен тем разговора из истории транскрибаций.

        Параметры:
            window_size (int): размер скользящего окна (по умолчанию 5).
            limit       (int): максимальное количество последних записей
                               для анализа (по умолчанию 100, 0 — все).

        Возвращает:
            segments     (list) — список сегментов с полями start_index,
                                  end_index, topic_words, summary, items_count, is_shift.
            total_shifts (int)  — количество смен темы.
            current_topic (dict) — текущая тема (last_n=window_size).
        """
        window_size = max(1, int(params.get("window_size", 5) or 5))
        limit = int(params.get("limit", 100) or 100)
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []

        if limit > 0:
            items = items[-limit:]

        timeline = self._topic_tracker.get_topic_timeline(items, window_size=window_size)
        current_topic = self._topic_tracker.get_current_topic(items, last_n=window_size)
        shifts = sum(1 for entry in timeline if entry.get("is_shift"))

        return {
            "segments": timeline,
            "total_shifts": shifts,
            "current_topic": current_topic,
        }

    def _handle_estimate_recording_cost(self, params: dict) -> dict:
        """IPC: estimate_recording_cost — оценка вычислительной стоимости обработки записи.

        Параметры:
            duration_sec  — длительность аудио в секундах (обязательно).
            quality       — профиль STT: "balanced" (по умолчанию), "max", "remote".
            features      — объект с булевыми флагами: diarization, llm, translation.

        Ответ: CostEstimate в виде словаря.
        """
        duration_sec = float(params.get("duration_sec", 0.0))
        quality = str(params.get("quality", "balanced"))
        features = params.get("features") or {}
        est = self._cost_estimator.estimate_cost(
            duration_sec=duration_sec,
            quality=quality,
            features=features,
        )
        return {
            "compute_time_sec": est.compute_time_sec,
            "memory_mb": est.memory_mb,
            "disk_mb": est.disk_mb,
            "features_cost": est.features_cost,
            "total_relative_cost": est.total_relative_cost,
        }

    def _handle_get_daily_cost_summary(self, params: dict) -> dict:
        """IPC: get_daily_cost_summary — сводка вычислительных расходов за сегодня."""
        return self._cost_estimator.get_daily_cost_summary(self._usage_tracker)

    # ── Abbreviation expander IPC handlers ────────────────────────────────────

    def _handle_select_model(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: select_model — умный выбор STT-модели на основе условий.

        Параметры:
            duration_sec  — длительность аудио в секундах (float, обязательный).
            quality       — "balanced" | "max" (строка, опциональный, по умолчанию "balanced").
            is_preview    — True если это превью-транскрибация (bool, опциональный).
            system_load   — нагрузка CPU 0.0–1.0 (float, опциональный, по умолчанию 0).

        Возвращает:
            {model_name, reason, estimated_latency_ms, quality_tier}
        """
        from core.model_selector import SmartModelSelector

        try:
            duration_sec = float(params.get("duration_sec", 0.0))
        except (TypeError, ValueError):
            raise ValueError("Параметр 'duration_sec' должен быть числом")

        quality = str(params.get("quality", "balanced")).strip()
        is_preview = bool(params.get("is_preview", False))

        try:
            system_load = float(params.get("system_load", 0.0))
        except (TypeError, ValueError):
            system_load = 0.0

        selector = SmartModelSelector()
        sel = selector.select_model(
            duration_sec=duration_sec,
            quality=quality,
            is_preview=is_preview,
            system_load=system_load,
        )
        return {
            "model_name": sel.model_name,
            "reason": sel.reason,
            "estimated_latency_ms": sel.estimated_latency_ms,
            "quality_tier": sel.quality_tier,
        }

    def _handle_get_smart_vocabulary_suggestions(self, params: dict) -> dict:
        """IPC: get_smart_vocabulary_suggestions — предложения для словаря STT."""
        scan_limit = max(10, min(int(params.get("scan_limit", 100) or 100), 500))
        min_frequency = max(1, int(params.get("min_frequency", 2) or 2))
        top_k = max(5, min(int(params.get("top_k", 30) or 30), 100))

        items, _ = self.store.get_history_page(cursor=None, limit=scan_limit)
        raw_items = [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in items]

        existing = self.vocabulary.load()
        suggestions = self._smart_vocabulary.get_vocabulary_suggestions(
            items=raw_items,
            existing=existing,
            min_frequency=min_frequency,
            top_k=top_k,
        )
        return {"suggestions": suggestions, "total": len(suggestions)}

    def _handle_check_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет, является ли текст дубликатом существующей записи в истории.

        Params:
            text (str): текст транскрипции для проверки.
            timestamp (str, optional): ISO-8601 метка времени (по умолчанию — сейчас).
            threshold (float, optional): порог сходства [0..1], по умолчанию 0.9.

        Returns:
            dict: is_duplicate, duplicate_of, similarity, action_taken.
        """
        params["_store"] = self.store
        return self._auto_deduplicator.handle_check_duplicate(params)

    def _handle_run_deduplication(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запускает сканирование истории на дубликаты в фоновом потоке.

        W1243 F2: возвращает немедленно {"ok": True, "job_id": "..."}.
        Прогресс и результат доступны через dedup_progress.

        Params:
            threshold (float, optional): порог сходства [0..1], по умолчанию 0.9.

        Returns:
            dict: ok=True, job_id (str).
        """
        params["_store"] = self.store
        return self._auto_deduplicator.handle_run_deduplication(params)

    def _handle_dedup_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        """Опрос статуса фоновой задачи run_deduplication.

        Params:
            job_id (str): идентификатор задачи, полученный из run_deduplication.

        Returns:
            dict: found, job_id, status, elapsed_sec, result, error.
        """
        return self._auto_deduplicator.handle_dedup_progress(params)

    def _handle_get_dedup_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статистику дедупликатора за текущую сессию.

        Returns:
            dict: total_checked, duplicates_found, chars_saved, dedup_rate.
        """
        return self._auto_deduplicator.handle_get_dedup_stats(params)

    def _handle_score_transcription(self, params: dict) -> dict:
        """Delegated to TextProcessingService."""
        return self._text_processing_svc.handle_score_transcription(params)


class IPCServer:
    """Unix socket сервер, который проксирует запросы в BackendService."""

    def __init__(self, socket_path: Path, service: BackendService) -> None:
        self.socket_path = socket_path
        self.service = service
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Останавливает accept loop."""
        self._stop_event.set()

    def serve_forever(self) -> None:
        """Основной цикл обработки входящих подключений."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Wave 58 LOW-2 closure (Wave 47 B2 audit): tighten umask BEFORE bind so the
        # socket is created with owner-only perms from the start. Combined with the
        # explicit `os.chmod()` below this eliminates the TOCTOU window where a
        # concurrent process could open the socket during creation (umask of 0o022
        # would have initial perms 0o755). `listen()` is not called yet, so no
        # accept() can happen even in the theoretical window, but defense-in-depth
        # is cheap here.
        _old_umask = os.umask(0o077)
        try:
            server.bind(str(self.socket_path))
        finally:
            os.umask(_old_umask)
        os.chmod(str(self.socket_path), IPC_SOCKET_PERMISSIONS)
        server.listen(IPC_SOCKET_BACKLOG)
        server.settimeout(IPC_SOCKET_TIMEOUT_SEC)

        logger.info("IPC сервер запущен на %s", self.socket_path)
        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise

                # PR #14: thread-per-connection. Без этого длинный STT-запрос
                # блокирует accept-loop и другие IPC-клиенты не могут опрашивать
                # прогресс. daemon=True — потоки умирают вместе с процессом.
                threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    name="ipc-conn",
                    daemon=True,
                ).start()
        finally:
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            logger.info("IPC сервер остановлен")

    def _handle_connection(self, conn: socket.socket) -> None:
        """Чтение одной JSON-команды и возврат JSON-ответа.

        Выполняется в отдельном потоке на коннект. Socket закрываем здесь же
        через `with conn:` — вызывающая сторона (accept-loop) не trackает.
        """
        with conn:
            try:
                raw = conn.recv(IPC_MAX_MESSAGE_BYTES)
                if not raw:
                    return
                text = raw.decode("utf-8").strip()
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("payload должен быть JSON-объектом")
            except Exception as exc:
                response = {
                    "id": None,
                    "ok": False,
                    "error": {"code": "invalid_json", "message": str(exc)},
                }
                try:
                    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    # Swift client disconnected before response sent — normal during
                    # crash/quit mid-call.  Log at debug, not error.
                    logger.debug(
                        "IPC client disconnected before invalid_json response: %s", exc
                    )
                except Exception:
                    logger.exception("Ошибка отправки invalid_json-ответа")
                return

            try:
                response = self.service.handle_request(payload)
            except Exception as exc:
                logger.exception("Непойманная ошибка в handle_request")
                response = {
                    "id": payload.get("id"),
                    "ok": False,
                    "error": {"code": "internal_error", "message": str(exc)},
                }
            try:
                conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                # Swift client disconnected before response sent — common when the
                # agent crashes or quits mid-call.  Log at debug, not error.
                logger.debug(
                    "IPC client disconnected before response: %s", exc
                )
            except Exception:
                logger.exception("Ошибка отправки ответа клиенту")


def default_data_dir() -> Path:
    """Каталог состояния приложения в профиле пользователя."""
    return Path.home() / "Library" / "Application Support" / "KrabEar"


def default_socket_path(data_dir: Path) -> Path:
    """Путь Unix socket внутри того же каталога состояния."""
    return data_dir / "krabear.sock"


def configure_logging(data_dir: Path) -> None:
    """Настраивает логирование backend в файл и stdout."""
    import json as _json
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "backend.log"

    if settings.LOG_FORMAT == "json":
        _STANDARD_LOG_ATTRS = frozenset({
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime", "taskName",
        })

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                log_entry: dict = {
                    "ts": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                # Merge extra= fields — any attribute not in the standard set
                extra = {
                    k: v for k, v in record.__dict__.items()
                    if k not in _STANDARD_LOG_ATTRS
                }
                if extra:
                    log_entry.update(extra)
                # Append exception info if present
                if record.exc_info:
                    log_entry["exc"] = self.formatException(record.exc_info)
                return _json.dumps(log_entry, default=str)
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers)


def build_service(data_dir: Path) -> BackendService:
    """Фабрика backend-сервиса с запуском проверок на старте."""
    store = StateStore(data_dir=data_dir)
    # Гарантируем наличие полного набора дефолтных настроек.
    store.save_settings(store.load_settings() or dict(DEFAULT_SETTINGS))
    store.maybe_compact()
    return BackendService(store=store)


def _trigger_sentry_release_async() -> None:
    """Запускает sentry_create_release.py в фоне — не блокирует старт.

    Вызывается только когда SENTRY_AUTO_RELEASE=1 и Sentry инициализирован.
    Ошибки логируются, но не останавливают сервис.
    """
    script = Path(__file__).parent.parent.parent / "scripts" / "sentry_create_release.py"

    def _run() -> None:
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info("Sentry release created: %s", result.stdout.strip())
            else:
                logger.warning(
                    "sentry_create_release.py failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to trigger Sentry release: %s", exc)

    thread = threading.Thread(target=_run, daemon=True, name="sentry-release")
    thread.start()


def main() -> None:
    """CLI entrypoint backend-сервиса."""
    parser = argparse.ArgumentParser(description="Krab Ear backend service")
    parser.add_argument("--data-dir", default=None, help="Каталог для settings/history/socket")
    parser.add_argument("--socket-path", default=None, help="Путь Unix socket")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    socket_path = (
        Path(args.socket_path).expanduser()
        if args.socket_path
        else default_socket_path(data_dir)
    )

    configure_logging(data_dir)

    # Sentry / GlitchTip crash telemetry (no-op если DSN не задан).
    # W704: release string читается из Info.plist через get_release_string()
    # (priority: env KRAB_EAR_RELEASE → Info.plist → __version__.py).
    sentry_ok = init_sentry(
        dsn=settings.SENTRY_DSN or None,
        environment=settings.SENTRY_ENVIRONMENT,
        release=get_release_string(),
    )
    if sentry_ok:
        logger.info("Sentry telemetry активна (env=%s)", settings.SENTRY_ENVIRONMENT)
    else:
        logger.debug("Sentry telemetry отключена (DSN не задан)")

    # Phase C C.7: Sentry-aware signal handlers (SIGTERM/SIGABRT/SIGSEGV).
    # Idempotent; no-op if Sentry not initialized.
    install_signal_handlers()

    # Auto-create Sentry release + deploy when SENTRY_AUTO_RELEASE=1
    if os.environ.get("SENTRY_AUTO_RELEASE") == "1" and sentry_ok:
        _trigger_sentry_release_async()

    service = build_service(data_dir)
    server = IPCServer(socket_path=socket_path, service=service)

    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Получен сигнал %s, завершаем backend", signum)
        server.stop()
        service.close()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        server.serve_forever()
    finally:
        service.close()


if __name__ == "__main__":
    main()
