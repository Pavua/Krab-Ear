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
from backend.stt_management_service import STTManagementService
from backend.recording_comparison import RecordingComparison
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
from backend.semantic_search import SemanticSearcher
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
from backend.health_check_service import HealthCheckService
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
from backend.text_processing_service import TextProcessingService
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
from backend.analytics_service import AnalyticsService
from backend.apple_integration_service import AppleIntegrationService
from backend.search_and_analysis_service import SearchAndAnalysisService
from backend.calendar_link import CalendarLinker
from backend.text_scoring_service import TextScoringService
from backend.privacy_audit import get_privacy_audit_logger
from backend.glossary_service import GlossaryService
from backend.llm_ops_service import LLMOpsService

import argparse
import collections
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
        self._start_time: float = time.monotonic()
        # W774: track timestamps of audio-device poll IPC calls for flood detection.
        # deque maxlen=10 keeps only the 10 most recent call times (one per method combined).
        self._audio_poll_timestamps: collections.deque = collections.deque(maxlen=10)
        self._settings_svc = SettingsService(store=self.store)
        # Wave 772: GlossaryService — IPC handlers for glossary CSV export/import.
        self._glossary_svc = GlossaryService(settings_svc=self._settings_svc)
        # Wave 783: LLMOpsService — list_llm_models, get_last_llm_diff, replace_word_in_last_transcript.
        self._llm_ops_svc = LLMOpsService(
            store=self.store,
            settings_svc=self._settings_svc,
            transcriber=self.transcriber,
        )
        # Wave 734: STTManagementService — IPC handlers for STT hotwords, warmup, routing, model select.
        self._stt_mgmt_svc = STTManagementService(
            settings_svc=self._settings_svc,
            transcriber=self.transcriber,
        )
        # Hot-propagate api_key changes to the running LLMRewriter without restart.
        _rewriter_ref = self._llm_rewriter
        if _rewriter_ref is not None:
            def _on_settings_saved(old: dict, new: dict) -> None:
                new_key = str(new.get("lm_studio_api_key", ""))
                if new_key != str(old.get("lm_studio_api_key", "")):
                    _rewriter_ref.set_api_key(new_key)
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
        # Telegram Bridge — мост Krab Ear → main Krab userbot.
        self._telegram_bridge = TelegramBridge(
            base_url=settings.TELEGRAM_BRIDGE_URL,
            timeout_sec=settings.TELEGRAM_BRIDGE_TIMEOUT_SEC,
            circuit_fail_threshold=settings.TELEGRAM_BRIDGE_CB_FAIL_THRESHOLD,
            circuit_reset_sec=settings.TELEGRAM_BRIDGE_CB_RESET_SEC,
        )
        # Wave 734: AppleIntegrationService — IPC handlers for Telegram + macOS app integrations.
        self._apple_integration_svc = AppleIntegrationService(self._telegram_bridge)
        # Wave 747: TextScoringService wiring (W404 orphan module).
        self._text_scoring_svc = TextScoringService(
            llm_rewriter=self._llm_rewriter,
            term_extractor=self._term_extractor,
            auto_title_generator=self._auto_title_generator,
            get_runtime_setting=self._get_runtime_setting,
        )
        # Wave 747: AnalyticsService wiring (W392 orphan module).
        self._analytics_svc = AnalyticsService(
            analytics_dashboard=self._analytics_dashboard,
            sentiment_trends=self._sentiment_trends,
            activity_calendar=self._activity_calendar,
            keyword_cloud_gen=self._keyword_cloud_gen,
            timeline_view=self._timeline_view,
            store=self.store,
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
        )
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
        # Wave 751: HealthCheckService wiring (W423 orphan module — last extracted service).
        # Must come after _startup_diagnostics, _integrity_checker, _health_checker,
        # _llm_probe, _llm_rewriter, _settings_svc, _start_time, recorder, and _last_stt_engine_ref.
        self._health_check_svc = HealthCheckService(
            store=self.store,
            health_checker=self._health_checker,
            startup_diagnostics=self._startup_diagnostics,
            integrity_checker=self._integrity_checker,
            llm_probe=self._llm_probe,
            metrics_collector=None,
            transcriber=self.transcriber,
            llm_rewriter=self._llm_rewriter,
            settings_svc=self._settings_svc,
            start_time=self._start_time,
            app_version=APP_VERSION,
            recorder=self.recorder,
            last_stt_engine_ref=self._last_stt_engine_ref,
        )
        # Wave 757: SearchAndAnalysisService — semantic search + action items + recording analytics.
        self._search_analysis_svc = SearchAndAnalysisService(
            store=self.store,
            semantic_searcher=self._semantic_searcher,
            action_items_extractor=self._action_items_extractor,
            topic_tracker=self._topic_tracker,
            recording_insights=self._recording_insights,
            recording_comparison=self._recording_comparison,
            stats_report=self._stats_report,
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

        # W828: кэш таблицы диспетчеризации — строится один раз после инициализации
        # всех сервисов; bound-методы стабильны на протяжении жизни объекта.
        from backend.ipc_dispatch import build_dispatch_table as _build_dispatch_table
        self._dispatch_table: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = (
            _build_dispatch_table(self)
        )

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
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_semantic_search(params)

    def _handle_semantic_search_status(self, params: dict) -> dict:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_semantic_search_status(params)

    def _handle_semantic_search_reindex(self, params: dict) -> dict:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_semantic_search_reindex(params)

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

        # W828: dispatch table кэширован в self._dispatch_table (built once in __init__
        # via backend.ipc_dispatch.build_dispatch_table — ~300 bound-method entries).
        handlers = self._dispatch_table
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
        """Delegated to HealthCheckService (Wave 751 wiring). Contract bit-exact."""
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

    def _handle_get_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService (Wave 751 wiring)."""
        return self._health_check_svc.handle_get_diagnostics(params)

    def _handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService (Wave 751 wiring)."""
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

    # ── Swift→backend telemetry helpers ─────────────────────────────────────

    _UNSET = object()  # sentinel for _push_registry_error optional overrides

    def _push_registry_error(
        self,
        code: str,
        debug_msg: str,
        context: dict | None = None,
        *,
        actionable: bool | None = None,
        action_id: object = _UNSET,
    ) -> None:
        """Build a KrabError from ERROR_REGISTRY and push it to the error bus.

        actionable / action_id default to the registry values; pass explicit
        values to override (e.g. actionable=False, action_id=None for info-only
        codes that are never actionable at a specific call site).
        """
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY
        from datetime import datetime, timezone
        entry = ERROR_REGISTRY[code]
        component = code.split(".")[0] if "." in code else "system"
        err = KrabError(
            severity=entry["severity"],
            component=component,
            code=code,
            message_user=entry["user_msg_ru"],
            message_debug=debug_msg,
            timestamp=datetime.now(timezone.utc),
            context=context or {},
            actionable=entry["actionable"] if actionable is None else actionable,
            action_id=entry["action_id"] if action_id is BackendService._UNSET else action_id,
        )
        self._error_bus.push(err)

    def _handle_report_paste_failure(self, params: dict) -> dict:
        """Swift→backend report когда paste fails (AX denied / app unsupported).

        Params:
            reason (str): "ax_denied" | "app_unsupported"
            app_bundle (str): bundle identifier of the target app
        """
        reason = params.get("reason", "")
        app_bundle = params.get("app_bundle", "")
        code_map = {
            "ax_denied": "paste.ax_denied",
            "app_unsupported": "paste.app_unsupported",
        }
        code = code_map.get(reason)
        if code is None:
            return {"ok": False, "reason": "unknown_paste_reason"}
        self._push_registry_error(
            code,
            debug_msg=f"paste failed reason={reason} app={app_bundle}",
            context={"app_bundle": app_bundle, "reason": reason},
        )
        return {"ok": True, "code": code}

    def _handle_report_hotkey_conflict(self, params: dict) -> dict:
        """Swift→backend report когда RegisterEventHotKey returns eventHotKeyExistsErr.

        Params:
            chord (str): chord identifier e.g. "right_option"
        """
        chord = params.get("chord", "")
        self._push_registry_error(
            "hotkey.conflict",
            debug_msg=f"hotkey conflict chord={chord}",
            context={"chord": chord},
        )
        return {"ok": True}

    def _handle_handshake(self, params: dict) -> dict:
        """Delegated to HealthCheckService (Wave 795 — handshake logic moved there)."""
        return self._health_check_svc.handle_handshake(params)

    def _handle_report_reconnect(self, params: dict) -> dict:
        """Swift→backend reconnect telemetry.

        Called after Swift IPCClient successfully reconnects after N retries.
        Pushes an ipc.reconnect info-severity event so the user gets visibility
        on transient IPC breaks.

        Params:
            attempts (int): number of retry attempts before success (1-5)
            duration_ms (int): total elapsed reconnect time in milliseconds
        """
        attempts = int(params.get("attempts", 0))
        duration_ms = int(params.get("duration_ms", 0))
        self._push_registry_error(
            "ipc.reconnect",
            debug_msg=f"reconnected after {attempts} attempts in {duration_ms}ms",
            context={"attempts": attempts, "duration_ms": duration_ms},
            actionable=False,
            action_id=None,
        )
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
        """Delegated to HealthCheckService (Wave 751 wiring)."""
        return self._health_check_svc.handle_probe_llm_http(params)

    def _handle_warmup_stt(self, params: dict) -> dict:
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_warmup_stt(params)

    def _handle_warmup_rewriter(self, params: dict) -> dict:
        """Delegated to TextScoringService (Wave 747 wiring)."""
        return self._text_scoring_svc.handle_warmup_rewriter(params)

    def _handle_get_shutdown_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус последнего graceful shutdown.

        Returns:
            dict с ключами: clean (bool|None), last_shutdown_time (str|None),
            shutdown_in_progress (bool).
        """
        return self._shutdown_handler.get_shutdown_status()

    def _handle_get_startup_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService (Wave 751 wiring)."""
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
        """Delegated to AnalyticsService (W773 extraction)."""
        return self._analytics_svc.handle_get_recording_stats(params)

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

    # --- D.2.3: Scored STT routing decision ---

    def _handle_get_stt_routing_decision(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_get_stt_routing_decision(params)

    def _handle_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_extract_action_items(params)

    def _handle_batch_extract_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_batch_extract_action_items(params)

    def _handle_get_pending_action_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_get_pending_action_items(params)

    def _check_audio_poll_flood(self) -> None:
        """Push ipc.audio_device_poll_flood if called >5 times in 60 s (W774)."""
        now = time.monotonic()
        self._audio_poll_timestamps.append(now)
        # Count calls within the last 60 s window
        recent = sum(1 for t in self._audio_poll_timestamps if now - t <= 60.0)
        if recent > 5:
            try:
                from backend.error_bus import KrabError
                from backend.error_codes import ERROR_REGISTRY
                from datetime import datetime, timezone
                _entry = ERROR_REGISTRY.get("ipc.audio_device_poll_flood", {})
                self._error_bus.push(KrabError(
                    severity=_entry.get("severity", "warn"),
                    component="ipc",
                    code="ipc.audio_device_poll_flood",
                    message_user=_entry.get("user_msg_ru", "Слишком частые запросы аудиоустройств"),
                    message_debug=f"audio device poll flood: {recent} calls in last 60s",
                    timestamp=datetime.now(timezone.utc),
                    context={"recent_count": recent},
                    actionable=False,
                    action_id=None,
                ))
            except Exception:
                pass

    def _handle_list_audio_inputs(self, params):
        """Delegated to RecordingCoreService."""
        self._check_audio_poll_flood()
        return self._recording_core_svc.handle_list_audio_inputs(params)

    def _handle_get_audio_devices(self, params):
        """Delegated to RecordingCoreService."""
        self._check_audio_poll_flood()
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
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_generate_stats_report(params)

    def _handle_generate_mini_stats_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_generate_mini_stats_report(params)

    def _handle_compare_periods(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_compare_periods(params)

    def _handle_get_activity_calendar(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_get_activity_calendar(params)

    def _handle_get_recording_insights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_get_recording_insights(params)

    def _handle_get_sentiment_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_get_sentiment_trends(params)

    def _handle_compare_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_compare_recordings(params)

    def _handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to HealthCheckService (Wave 751 wiring)."""
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
        """Delegated to TextScoringService (Wave 747 wiring)."""
        return self._text_scoring_svc.handle_extract_terms(params)

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
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_get_keyword_cloud(params)

    # ── Audio fingerprinting ─────────────────────────────────────────────────

    # ── Telegram Bridge ──────────────────────────────────────────────────────

    def _handle_send_to_telegram(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_send_to_telegram(params)

    # ── Apple Notes integration (Phase D.4) ─────────────────────────────────

    def _handle_create_apple_note(self, params: dict) -> dict:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_create_apple_note(params)

    def _handle_create_apple_reminder(self, params: dict) -> dict:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_create_apple_reminder(params)

    # ── Apple Calendar integration (Phase D.4) ──────────────────────────────

    def _handle_create_calendar_event(self, params: dict) -> dict:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_create_calendar_event(params)

    # ── iMessage integration (Phase D.4) ────────────────────────────────────

    def _handle_send_imessage(self, params: dict) -> dict:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_send_imessage(params)

    def _handle_list_telegram_chats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AppleIntegrationService (Wave 734 wiring)."""
        return self._apple_integration_svc.handle_list_telegram_chats(params)

    # ── Phase 3: Call Session CRUD ───────────────────────────────────────────

    # ------------------------------------------------------------------
    # STT hotwords (initial_prompt boost)
    # ------------------------------------------------------------------

    def _handle_add_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_add_stt_hotword(params)

    def _handle_remove_stt_hotword(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_remove_stt_hotword(params)

    def _handle_list_stt_hotwords(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_list_stt_hotwords(params)

    # ── Timeline view ────────────────────────────────────────────────────────

    def _handle_get_timeline_view(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_get_timeline_view(params)

    def _handle_generate_auto_title(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to TextScoringService (Wave 747 wiring)."""
        return self._text_scoring_svc.handle_generate_auto_title(params)

    def _handle_get_learning_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_learning_stats — статистика прогресса изучения языка."""
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_get_learning_stats(params_with_store)

    def _handle_get_analytics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to AnalyticsService (Wave 747 wiring)."""
        return self._analytics_svc.handle_get_analytics_dashboard(params)

    def _handle_get_topic_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delegated to SearchAndAnalysisService (Wave 757)."""
        return self._search_analysis_svc.handle_get_topic_timeline(params)

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
        """Delegated to STTManagementService (Wave 734 wiring)."""
        return self._stt_mgmt_svc.handle_select_model(params)

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
        """Сканирует всю историю и возвращает отчёт о дублирующихся транскрипциях.

        Params:
            threshold (float, optional): порог сходства [0..1], по умолчанию 0.9.

        Returns:
            dict: total_scanned, duplicate_groups, duplicates.
        """
        params["_store"] = self.store
        return self._auto_deduplicator.handle_run_deduplication(params)

    def _handle_get_dedup_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статистику дедупликатора за текущую сессию.

        Returns:
            dict: total_checked, duplicates_found, chars_saved, dedup_rate.
        """
        return self._auto_deduplicator.handle_get_dedup_stats(params)

    def _handle_score_transcription(self, params: dict) -> dict:
        """Delegated to TextProcessingService."""
        return self._text_processing_svc.handle_score_transcription(params)


# IPCServer, default_data_dir, default_socket_path выделены в backend/ipc_server.py
# (W797 phase 2, W813). Re-exported здесь для обратной совместимости с тестами
# и любым кодом, который делает `from backend.service import IPCServer`.
from backend.ipc_server import IPCServer, default_data_dir, default_socket_path  # noqa: F401, E402


from backend.service_logging import configure_logging, JsonFormatter, _STANDARD_LOG_ATTRS  # noqa: F401, E402


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
