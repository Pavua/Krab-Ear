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
from backend.period_comparison import compare_periods as _compare_periods_fn
from backend.keyword_cloud import KeywordCloudGenerator
from backend.quality_trends import QualityTrendAnalyzer
from backend.daily_digest import DailyDigestGenerator
from backend.analytics_dashboard import AnalyticsDashboard
from core.utils import TextUtils
from core.term_extractor import TermExtractor
from core.text_comparator import TextComparator
from core.language_detector import LanguageDetector
from core.config import settings
from core.audio_converter import AudioConverter
from backend.translator import Translator
from backend.vocabulary_store import VocabularyStore
from backend.transcriber import Transcriber
from backend.state_store import StateStore
from backend.recorder import AudioRecorder
from contracts.translation_events import TranslationCompleted, TranslationFailed
from contracts.registry import EventType
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from backend.models import DEFAULT_SETTINGS
from backend.event_replay import EventReplayManager
from backend.event_bus import bus as event_bus
from backend.system_monitor import SystemMonitor
from backend.translation_service import TranslationService
from backend.settings_service import SettingsService
from backend.transcript_writer import TranscriptWriter
from backend.cost_estimator import CostEstimator
from backend.usage_tracker import UsageTracker
from backend.session_tracker import SessionTracker
from backend.speaker_manager import SpeakerManager
from backend.history_service import HistoryService
from backend.error_reporter import ErrorReporter
from backend.recording_scheduler import RecordingScheduler
from backend.recording_merger import RecordingMerger
from backend.recording_chain import RecordingChainManager
from backend.collection_manager import CollectionManager
from backend.call_assist_service import CallAssistService
from backend.request_signing import RequestSigner
from backend.ipc_throttle import IPCThrottle
from backend.export_scheduler import ExportScheduler
from backend.shutdown_handler import GracefulShutdownHandler
from backend.auto_backup import AutoBackupManager, AUTO_BACKUP_INTERVAL_HOURS, AUTO_BACKUP_MAX_COPIES
from backend.job_tracker import JobTracker
from backend.performance_profiler import profiler as performance_profiler

import argparse
from datetime import datetime, timedelta
import json
import tempfile
import logging
import os
from pathlib import Path
import re
import signal
import socket
import platform
import sys
import threading
import time
from typing import Any, Callable

import numpy as np

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
        self.recorder = recorder or AudioRecorder()

        # D.10a: LLM rewriter initialization (admin flag check via settings)
        self._llm_rewriter = self._init_llm_rewriter()

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
        self._settings_svc = SettingsService(store=self.store)
        self._system_monitor = SystemMonitor()
        self._preview_lock = threading.Lock()
        self._preview_thread: threading.Thread | None = None
        self._preview_stop_event = threading.Event()
        self._preview_text = ""
        self._preview_duration_sec = 0.0
        self._preview_updated_at = 0.0
        self._preview_error_count: int = 0
        self._clipboard_history: list[dict] = []
        self._collections = CollectionManager(store=self.store)
        self._norm_profiles = NormalizationProfileRegistry(data_dir=self.store.data_dir)
        self._chains = RecordingChainManager(store=self.store)
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
            reset_preview_fn=self._reset_preview_state,
            start_preview_fn=lambda qp: self._start_preview_worker(quality_profile=qp),
        )
        self._translation = TranslationService(
            translator=self.translator,
            store=self.store,
            cached_settings=self._cached_settings,
            invalidate_settings_cache=self._invalidate_settings_cache,
            vocabulary_store=self.vocabulary,
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
        self._auto_backup = AutoBackupManager(
            store=self.store,
            interval_hours=AUTO_BACKUP_INTERVAL_HOURS,
            max_copies=AUTO_BACKUP_MAX_COPIES,
            enabled=settings.AUTO_BACKUP_ENABLED,
        )
        self._export_scheduler = ExportScheduler(data_dir=self.store.data_dir)
        self._transcription_counter: int = 0
        self._analytics_dashboard = AnalyticsDashboard()
        self._daily_digest = DailyDigestGenerator()
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
        self._readability_scorer = ReadabilityScorer()
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
        self._text_anonymizer = TextAnonymizer()
        self._text_postprocessor = TextPostProcessor()
        self._transcription_queue = TranscriptionQueue()
        self._emotion_detector = EmotionDetector()
        self._sentiment_trends = SentimentTrendAnalyzer(detector=self._emotion_detector)
        self._topic_tracker = TopicTracker()
        self._data_migrator = DataMigrator()
        self._abbreviation_expander = AbbreviationExpander(data_dir=self.store.data_dir)
        self._obsidian_sync = ObsidianSyncManager(data_dir=self.store.data_dir)
        self._speaker_manager = SpeakerManager(data_dir=self.store.data_dir)
        self._playback_tracker = PlaybackTracker(data_dir=self.store.data_dir)
        self._recording_comparison = RecordingComparison()
        self._smart_vocabulary = SmartVocabularyBuilder()
        self._metadata_enricher = MetadataEnricher()
        self._timeline_exporter = TimelineExporter()
        self._timeline_view = TimelineViewGenerator()
        self._auto_deduplicator = AutoDeduplicator()
        self._search_history = SearchHistoryManager(data_dir=self.store.data_dir)
        self._archive_manager = ArchiveManager(store=self.store)
        self._template_manager = TemplateManager(data_dir=self.store.data_dir)
        self._feature_flags = FeatureFlags(data_dir=self.store.data_dir)
        self._plugin_manager = PluginManager(data_dir=self.store.data_dir)
        self._hotword_detector = HotwordDetector(data_dir=self.store.data_dir)
        self._model_cache_manager = ModelCacheManager()
        # Реестр асинхронных задач транскрибации (transcribe_paths_async).
        self._job_tracker = JobTracker()
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

        # Обработчик корректного завершения (регистрация сигналов — через register())
        self._shutdown_handler = GracefulShutdownHandler(data_dir=self.store.data_dir)

    def _init_llm_rewriter(self):
        """Создаёт LLMRewriter если settings.LLM_ENABLED. Возвращает None иначе."""
        if not settings.LLM_ENABLED:
            return None

        try:
            from backend.llm_rewriter import LLMRewriter
            rewriter = LLMRewriter(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                model=settings.LLM_MODEL,
                timeout_sec=settings.LLM_TIMEOUT_SEC,
                circuit_fail_threshold=settings.LLM_CIRCUIT_FAIL_THRESHOLD,
                circuit_initial_reset_sec=settings.LLM_CIRCUIT_INITIAL_RESET_SEC,
                circuit_max_reset_sec=settings.LLM_CIRCUIT_MAX_RESET_SEC,
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
            "get_diagnostics": self._handle_get_diagnostics,  # диагностика: system, stt, llm, history, settings_cache
            "set_translation_glossary_item": self._translation.handle_set_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
            # VERIFIED: called from Swift (HistoryPanel)
            "remove_translation_glossary_item": self._translation.handle_remove_translation_glossary_item,
            "get_glossary_suggestions": self._translation.handle_get_glossary_suggestions,  # авто-обучение глоссария: предлагает пары source→target из истории
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
            "summarize_text": self._handle_summarize_text,  # VERIFIED: called from Swift (HistoryPanel)
            "summarize_item": self._handle_summarize_item,  # LLM summary для элемента истории по ID
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
            "get_audio_devices": self._handle_get_audio_devices,  # список доступных аудиовходов для GUI
            "test_microphone": self._handle_test_microphone,  # тест микрофона: RMS/peak уровни
            "auto_summarize_batch": self._history.handle_auto_summarize_batch,  # авто-резюме пакета транскрипций через LLM
            "list_summary_profiles": self._history.handle_list_summary_profiles,  # список профилей резюмирования
            "add_summary_profile": self._history.handle_add_summary_profile,  # добавить кастомный профиль резюмирования
            "filter_by_confidence": self._history.handle_filter_by_confidence,  # фильтрация истории по STT confidence score
            "health_check": self._handle_health_check,  # агрегированный health check всех подсистем
            "analyze_audio_quality": self._handle_analyze_audio_quality,  # pre-flight анализ качества аудиофайла
            "analyze_silence": self._handle_analyze_silence,  # обнаружение тишины и доли речи в аудиофайле
            "get_session_history": self._handle_get_session_history,  # история сессий записи с метаданными
            "get_session_stats": self._handle_get_session_stats,  # агрегированная статистика сессий
            "get_error_report": self._error_reporter.handle_get_error_report,  # последние ошибки из ring-буфера
            "get_error_stats": self._error_reporter.handle_get_error_stats,  # счётчики ошибок по компоненту/типу/окну
            "detect_language": self._handle_detect_language,  # эвристическое определение языка текста
            "get_usage_stats": self._handle_get_usage_stats,
            "convert_audio": self._handle_convert_audio,  # конвертация аудио в WAV
            "get_audio_info": self._handle_get_audio_info,  # метаданные аудиофайла  # ежедневная статистика использования: записи, длительность, слова
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
            "apply_normalization_profile": self._handle_apply_normalization_profile,  # применить профиль нормализации к тексту
            "get_collection_items": self._collections.handle_get_collection_items,  # получить записи истории из коллекции
            "start_chain": self._chains.handle_start_chain,  # начать цепочку связанных записей
            "add_to_chain": self._chains.handle_add_to_chain,  # добавить запись в цепочку
            "end_chain": self._chains.handle_end_chain,  # завершить цепочку
            "get_chain": self._chains.handle_get_chain,  # получить цепочку с деталями
            "list_chains": self._chains.handle_list_chains,  # список цепочек
            "merge_chain_text": self._chains.handle_merge_chain_text,  # объединённый текст цепочки
            "schedule_recording": self._recording_scheduler.handle_schedule_recording,  # запланировать запись на определённое время
            "cancel_scheduled_recording": self._recording_scheduler.handle_cancel_scheduled_recording,  # отменить запланированную запись
            "list_scheduled_recordings": self._recording_scheduler.handle_list_scheduled_recordings,  # список запланированных записей
            "generate_daily_digest": self._handle_generate_daily_digest,  # ежедневный дайджест транскрипций
            "analyze_quality_trends": self._handle_analyze_quality_trends,  # анализ трендов качества
            "get_activity_calendar": self._handle_get_activity_calendar,  # GitHub-style activity calendar данные
            "get_speaker_statistics": self._handle_get_speaker_statistics,  # per-speaker статистика речи из диаризованных записей
            "get_recording_insights": self._handle_get_recording_stats,  # эвристические инсайты по записям
            "get_sentiment_trends": self._handle_get_sentiment_trends,  # анализ трендов тональности транскрипций за N дней
            "compare_periods": self._handle_compare_periods,  # сравнение двух периодов использования
            "check_integrity": self._handle_check_integrity,  # проверка целостности данных
            "repair_integrity": self._handle_repair_integrity,  # исправление проблем целостности данных
            "extract_terms": self._handle_extract_terms,  # извлечение терминов из текста
            "compare_texts": self._handle_compare_texts,  # сравнение двух текстов/транскрипций
            "get_context_memory": self._handle_get_context_memory,  # контекстная память STT: слова и темы из последних транскрибаций
            "score_readability": self._handle_score_readability,  # оценка читабельности текста транскрибации
            "score_transcription": self._handle_score_transcription,  # оценка качества транскрибации (0–100, A–F)
            "get_event_log": self._event_replay.handle_get_event_log,  # лог событий для отладки (фильтрация по типу/времени)
            "get_event_stats": self._event_replay.handle_get_event_stats,  # статистика событий: счётчики, скорость/мин
            "replay_events": self._event_replay.handle_replay_events,  # воспроизведение событий в диапазоне времени
            "get_waveform": self._handle_get_waveform,  # генерация waveform-данных для GUI-визуализации
            "get_throttle_stats": self._handle_get_throttle_stats,  # статистика IPC throttle: вызовы, отклонения
            "check_audio_duplicate": self._handle_check_audio_duplicate,  # аудио-фингерпринтинг для обнаружения дубликатов
            "batch": self._handle_batch,  # пакетное выполнение нескольких IPC-методов за один вызов (макс. 50)
            "get_keyword_cloud": self._handle_get_keyword_cloud,  # данные облака ключевых слов для визуализации word cloud
            "prepare_share": self._sharing.handle_prepare_share,  # подготовить пакет для шаринга транскрипций
            "list_shared": self._sharing.handle_list_shared,  # список сохранённых пакетов шаринга
            "get_shared": self._sharing.handle_get_shared,  # получить пакет шаринга по share_id
            "save_transcript_version": self._transcript_versioning.handle_save_transcript_version,  # сохранить новую версию текста транскрипции
            "get_transcript_versions": self._transcript_versioning.handle_get_transcript_versions,  # получить все версии транскрипции по item_id
            "revert_transcript_version": self._transcript_versioning.handle_revert_transcript_version,  # откат транскрипции к указанной версии
            "analyze_speech_pace": self._handle_analyze_speech_pace,  # анализ темпа речи: WPM, CPM, категория темпа
            "analyze_word_timing": self._handle_analyze_word_timing,  # анализ ритма речи по пословным таймстемпам Whisper
            "generate_auto_title": self._handle_generate_auto_title,  # автоматическая генерация заголовка для транскрибации
            # форматирование текста под целевое приложение (telegram, notes, email и др.)
            "format_for_paste": self._paste_formatter.handle_format_for_paste,
            "merge_recordings": lambda p: self._merger.handle_merge_recordings(p, self.store),  # объединить несколько записей истории в одну
            "preview_merge": lambda p: self._merger.handle_preview_merge(p, self.store),  # предпросмотр объединения без сохранения
            "list_paste_formatters": self._paste_formatter.handle_list_paste_formatters,  # список доступных форматтеров вставки
            "extract_learning_vocabulary": self._handle_extract_learning_vocabulary,  # режим изучения языков: извлечение словаря из двуязычных транскрипций
            "generate_flashcards": self._handle_generate_flashcards,  # режим изучения языков: генерация флеш-карточек
            "get_learning_stats": self._handle_get_learning_stats,  # режим изучения языков: статистика прогресса
            "get_analytics_dashboard": self._handle_get_analytics_dashboard,  # комплексный дашборд аналитики: все метрики за один вызов
            "get_topic_timeline": self._handle_get_topic_timeline,  # таймлайн смен тем разговора из истории транскрибаций
            "list_config_presets": self._config_presets.handle_list_config_presets,  # список конфигурационных пресетов (встроенных и кастомных)
            "apply_config_preset": self._config_presets.handle_apply_config_preset,  # применить конфигурационный пресет — вернуть settings_patch
            "create_config_preset": self._config_presets.handle_create_config_preset,  # создать кастомный конфигурационный пресет
            "anonymize_text": self._handle_anonymize_text,  # редактирование персональных данных из транскрипции
            "enqueue_transcription": self._transcription_queue.handle_enqueue,  # добавить аудиофайл в очередь транскрипции с приоритетом
            "cancel_transcription": self._transcription_queue.handle_cancel,  # отменить задание транскрипции по job_id
            "get_queue_status": self._transcription_queue.handle_get_status,  # статус задания транскрипции по job_id
            "list_transcription_queue": self._transcription_queue.handle_list_queue,  # список всех заданий очереди транскрипции
            "detect_emotion": self._handle_detect_emotion,  # эвристическое определение эмоции в тексте транскрипции
            "estimate_recording_cost": self._handle_estimate_recording_cost,  # оценка вычислительной стоимости обработки записи
            "get_daily_cost_summary": self._handle_get_daily_cost_summary,  # сводка вычислительных расходов за сегодня
            "check_migration": self._data_migrator.handle_check_migration,  # проверка необходимости миграции данных
            "run_migration": self._data_migrator.handle_run_migration,  # выполнение миграции данных между версиями
            "expand_abbreviations": self._handle_expand_abbreviations,  # раскрытие аббревиатур в тексте транскрипции
            "add_abbreviation": self._handle_add_abbreviation,  # добавить пользовательскую аббревиатуру
            "remove_abbreviation": self._handle_remove_abbreviation,  # удалить аббревиатуру
            "list_abbreviations": self._handle_list_abbreviations,  # список аббревиатур для языка
            "detect_voice_activity": self._handle_detect_voice_activity,  # VAD: обнаружение участков речи/тишины в аудиофайле
            "profile_noise": self._handle_profile_noise,  # профилирование фонового шума: тип, уровень, SNR, рекомендации
            "configure_obsidian_sync": self._obsidian_sync.handle_configure,  # настроить Obsidian vault для синхронизации транскрипций
            "run_obsidian_sync": self._obsidian_sync.handle_sync,  # синхронизировать записи истории с Obsidian vault
            "get_obsidian_sync_status": self._obsidian_sync.handle_get_status,  # статус синхронизации с Obsidian vault
            # зарегистрировать воспроизведение записи (item_id, duration_listened_sec)
            "record_playback": self._playback_tracker.handle_record_playback,
            # статистика воспроизведения одной записи: play_count, total_listened_sec, last_played
            "get_playback_stats": self._playback_tracker.handle_get_playback_stats,
            "get_most_replayed": self._playback_tracker.handle_get_most_replayed,  # топ N наиболее часто воспроизводимых записей
            # прогнать текст через настраиваемый конвейер пост-обработки (пробелы, пунктуация, сущности, аббревиатуры, анонимизация)
            "post_process_text": self._handle_post_process_text,
            "list_post_process_steps": self._handle_list_post_process_steps,  # список доступных шагов пост-обработки текста
            "compare_recordings": self._handle_compare_recordings,  # сравнение нескольких записей side-by-side: матрица сходства, статистика, общие/уникальные слова
            "select_model": self._handle_select_model,  # умный выбор STT-модели на основе условий записи
            "auto_update_vocabulary": self._handle_auto_update_vocabulary,  # умный авто-апдейт словаря STT из истории транскрибаций
            "get_smart_vocabulary_suggestions": self._handle_get_smart_vocabulary_suggestions,  # предложения для словаря STT на основе паттернов использования
            "get_startup_diagnostics": self._handle_get_startup_diagnostics,  # диагностика при старте: результаты всех startup-проверок
            # автоматическое обогащение метаданных записи: word_count, emotion, pace, quality, topics и др.
            "enrich_recording": self._metadata_enricher.handle_enrich_recording,
            "get_shutdown_status": self._handle_get_shutdown_status,  # статус последнего graceful shutdown: clean, last_shutdown_time
            "check_duplicate": self._handle_check_duplicate,  # проверка одной транскрипции на дублирование по текстовому сходству
            "run_deduplication": self._handle_run_deduplication,  # полное сканирование истории на дубликаты
            "get_dedup_stats": self._handle_get_dedup_stats,  # статистика дедупликатора: проверено, найдено, символов сохранено
            "get_timeline_view": self._handle_get_timeline_view,  # группировка истории по временным блокам (timeline)
            "export_timeline": self._handle_export_timeline,  # экспорт временной шкалы записей в SVG, JSON или iCal
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
            # --- plugins ---
            "list_plugins": self._plugin_manager.handle_list_plugins,  # список обнаруженных плагинов
            "get_plugin_info": self._plugin_manager.handle_get_plugin_info,  # информация о конкретном плагине
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
            "get_wake_word_config": self._handle_get_wake_word_config,  # конфигурация wake word: enabled, access_key_present, ppn_present
            "set_wake_word_config": self._handle_set_wake_word_config,  # обновить wake word настройки (enabled, engine, brain)
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
                return self._error(
                    request_id,
                    "rate_limit_exceeded",
                    f"Превышен лимит запросов для метода {method!r}. Повторите через {wait_sec:.1f}s",
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
        try:
            history_count = self.store.count_active_items()
        except Exception:
            history_count = -1
        return {
            "status": "ok",
            "service": "krabear-backend",
            "version": APP_VERSION,
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "is_recording": bool(getattr(self.recorder, "is_recording", False)),
            "history_count": history_count,
        }

    def _handle_start_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        started = self.recorder.start()
        if not started:
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            # Идемпотентный контракт: повторный start не считается ошибкой.
            return {
                "status": "already_recording",
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
            }
        self._reset_preview_state()
        settings = self._cached_settings()
        if bool(settings.get("realtime_preview_enabled", True)):
            quality_profile = str(settings.get("quality_profile", "balanced"))
            self._start_preview_worker(quality_profile=quality_profile)
        return {"status": "recording"}

    def _handle_stop_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        self._stop_preview_worker()
        settings = self._cached_settings()
        stop_tail_trim_ms = self._coerce_bounded_int(
            value=params.get("stop_tail_trim_ms", settings.get("stop_tail_trim_ms", 180)),
            default=180,
            min_value=0,
            max_value=1200,
        )
        stopped = self._stop_recorder_guarded(stop_tail_trim_ms=stop_tail_trim_ms)
        if stopped is None:
            # Идемпотентный контракт: повторный stop не считается ошибкой.
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "status": "already_stopped",
                "is_recording": False,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "stop_tail_trim_ms": stop_tail_trim_ms,
            }

        audio, duration_sec = stopped
        quality_profile = str(
            params.get("quality_profile") or settings.get("quality_profile", "balanced")
        )
        cleanup_profile = str(
            params.get("cleanup_profile") or settings.get("cleanup_profile", "soft")
        )
        lang_hint: str | None = params.get("lang_hint") or None
        translation_mode = str(
            params.get("translation_mode") or settings.get("translation_mode", "off")
        )
        translation_style = str(
            params.get("translation_style") or settings.get("translation_style", "neutral")
        )
        translation_glossary = settings.get("translation_glossary", {})
        translate_and_paste = bool(
            params.get("translate_and_paste")
            if "translate_and_paste" in params
            else settings.get("translate_and_paste", False)
        )
        network_mode = str(settings.get("network_mode", "offline_default"))
        silence_guard_enabled = self._coerce_bool(settings.get("silence_guard_enabled", True), default=True)
        silence_rms_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_rms_threshold", 0.0020),
            default=0.0020,
            min_value=0.0003,
            max_value=0.05,
        )
        silence_peak_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_peak_threshold", 0.0120),
            default=0.0120,
            min_value=0.001,
            max_value=0.2,
        )
        silence_active_ratio_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_active_ratio_threshold", 0.015),
            default=0.015,
            min_value=0.001,
            max_value=0.30,
        )
        background_guard_enabled = self._coerce_bool(settings.get("background_guard_enabled", True), default=True)
        background_guard_min_peak = self._coerce_bounded_float(
            value=settings.get("background_guard_min_peak", 0.025),
            default=0.025,
            min_value=0.003,
            max_value=0.25,
        )
        background_guard_min_rms = self._coerce_bounded_float(
            value=settings.get("background_guard_min_rms", 0.0040),
            default=0.0040,
            min_value=0.0008,
            max_value=0.08,
        )
        background_guard_uniform_frame_threshold = self._coerce_bounded_float(
            value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
            default=0.0060,
            min_value=0.001,
            max_value=0.20,
        )
        background_guard_max_uniform_active_ratio = self._coerce_bounded_float(
            value=settings.get("background_guard_max_uniform_active_ratio", 0.92),
            default=0.92,
            min_value=0.40,
            max_value=0.99,
        )
        sample_rate = self._coerce_bounded_int(
            value=getattr(self.recorder, "sample_rate", 16000),
            default=16000,
            min_value=8000,
            max_value=192000,
        )

        if getattr(audio, "size", 0) == 0:
            return {
                "status": "empty_audio",
                "duration_sec": duration_sec,
                "quality_profile": quality_profile,
                "cleanup_profile": cleanup_profile,
                "translation_mode": translation_mode,
                "translate_and_paste": translate_and_paste,
                "text": "",
                "original_text": "",
                "translated_text": "",
                "translation_status": "not_requested",
                "history_id": None,
                "stop_tail_trim_ms": stop_tail_trim_ms,
                "silence_detected": False,
                "silence_guard_enabled": silence_guard_enabled,
                "background_guard_rejected": False,
            }

        silence_detected = False
        if silence_guard_enabled:
            silence_detected = self._looks_like_silence_audio(
                audio=audio,
                sample_rate=sample_rate,
                rms_threshold=silence_rms_threshold,
                peak_threshold=silence_peak_threshold,
                active_ratio_threshold=silence_active_ratio_threshold,
            )
            if silence_detected:
                logger.info(
                    "Silence guard: stop_recording классифицирован как тишина, STT пропущен",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "rms_threshold": silence_rms_threshold,
                        "peak_threshold": silence_peak_threshold,
                        "active_ratio_threshold": silence_active_ratio_threshold,
                    },
                )
                return {
                    "status": "empty_audio",
                    "duration_sec": duration_sec,
                    "quality_profile": quality_profile,
                    "cleanup_profile": cleanup_profile,
                    "translation_mode": translation_mode,
                    "translate_and_paste": translate_and_paste,
                    "text": "",
                    "original_text": "",
                    "translated_text": "",
                    "translation_status": "not_requested",
                    "history_id": None,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                    "silence_detected": True,
                    "silence_guard_enabled": True,
                    "background_guard_rejected": False,
                }

        background_guard_rejected = False
        if background_guard_enabled:
            background_guard_rejected = self._looks_like_distant_background_speech(
                audio=audio,
                sample_rate=sample_rate,
                min_peak=background_guard_min_peak,
                min_rms=background_guard_min_rms,
                uniform_frame_threshold=background_guard_uniform_frame_threshold,
                max_uniform_active_ratio=background_guard_max_uniform_active_ratio,
            )
            if background_guard_rejected:
                logger.info(
                    "Background guard: stop_recording отклонен как фоновая речь",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "min_peak": background_guard_min_peak,
                        "min_rms": background_guard_min_rms,
                        "uniform_frame_threshold": background_guard_uniform_frame_threshold,
                        "max_uniform_active_ratio": background_guard_max_uniform_active_ratio,
                    },
                )
                return {
                    "status": "empty_audio",
                    "duration_sec": duration_sec,
                    "quality_profile": quality_profile,
                    "cleanup_profile": cleanup_profile,
                    "translation_mode": translation_mode,
                    "translate_and_paste": translate_and_paste,
                    "text": "",
                    "original_text": "",
                    "translated_text": "",
                    "translation_status": "not_requested",
                    "history_id": None,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                    "silence_detected": False,
                    "silence_guard_enabled": silence_guard_enabled,
                    "background_guard_rejected": True,
                }

        # Загружаем пользовательский vocabulary для подсказок Whisper
        user_vocabulary = self.vocabulary.load() or []

        transcribe_payload = self.transcriber.transcribe(
            audio,
            quality_profile=quality_profile,
            cleanup_profile=cleanup_profile,
            lang_hint=lang_hint,
            extra_vocabulary=user_vocabulary if user_vocabulary else None,
        )
        text = self._postprocess_transcribed_text(self._extract_transcribed_text(transcribe_payload))
        transcription_error = self._extract_transcribed_error(transcribe_payload)
        if not text:
            if transcription_error:
                event_bus.emit_typed(EventType.STT_FAILED, SttFailed(reason=transcription_error, duration_sec=duration_sec))
            return {
                "status": "empty_text",
                "duration_sec": duration_sec,
                "quality_profile": quality_profile,
                "cleanup_profile": cleanup_profile,
                "translation_mode": translation_mode,
                "translate_and_paste": translate_and_paste,
                "text": "",
                "original_text": "",
                "translated_text": "",
                "translation_status": "not_requested",
                "history_id": None,
                "transcription_error": transcription_error,
                "stop_tail_trim_ms": stop_tail_trim_ms,
                "silence_detected": silence_detected,
                "silence_guard_enabled": silence_guard_enabled,
                "background_guard_rejected": background_guard_rejected,
            }

        translation = self.translator.translate(
            text=text,
            mode=translation_mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=translation_glossary,
        )
        translated_text = translation.text.strip() if translation.ok else ""
        final_text = translated_text if (translate_and_paste and translated_text) else text
        translation_status = translation.status
        if translation.ok and translated_text:
            event_bus.emit_typed(EventType.TRANSLATION_COMPLETED, TranslationCompleted(
                history_id="",  # будет обновлено ниже после сохранения в store
                source_text=text,
                translated_text=translated_text,
                source_lang=translation.source_lang or "",
                target_lang=translation.target_lang or "",
                engine=translation.engine or "",
                mode=translation.mode or "",
            ))
        elif not translation.ok and translation_status not in ("not_requested", "off"):
            event_bus.emit_typed(EventType.TRANSLATION_FAILED, TranslationFailed(
                history_id=None,
                source_text=text,
                reason=translation.status or "unknown",
                source_lang=translation.source_lang,
                target_lang=translation.target_lang,
            ))

        tp = transcribe_payload if isinstance(transcribe_payload, dict) else {}
        confidence = tp.get("confidence", 0.0)
        if confidence < 0.4 and text:
            logger.warning("Низкая уверенность STT: %.2f — возможна ошибка распознавания", confidence)
        diarization_data = tp.get("diarization")

        # Format text with speaker labels if diarization produced multiple speakers
        display_text = self._format_text_with_speakers(final_text, diarization_data)

        item = self.store.add_history_item(
            text=display_text,
            paste_status="failed",
            source_text=text,
            translated_text=translated_text,
            translation_mode=translation.mode,
            source_lang=translation.source_lang,
            target_lang=translation.target_lang,
            translation_status=translation_status,
            translation_engine=translation.engine,
            cleaned_text=tp.get("cleaned_text", ""),
            llm_applied=bool(tp.get("llm_applied", False)),
            llm_latency_ms=int(tp.get("llm_latency_ms", 0) or 0),
            diarization=diarization_data,
            emotion=tp.get("emotion") if isinstance(tp.get("emotion"), str) else None,
        )
        self._clipboard_history.append({
            "text": final_text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "history_id": item.id,
        })
        if len(self._clipboard_history) > 20:
            self._clipboard_history = self._clipboard_history[-20:]

        # Обновляем контекстную память для улучшения следующего STT
        try:
            self._context_memory.update(text)
        except Exception:
            pass

        # Авто-бэкап каждые 100 транскрибаций
        self._transcription_counter += 1
        if self._transcription_counter % 100 == 0:
            try:
                self._auto_backup.check_and_backup()
            except Exception:
                pass

        result_payload = {
            "status": "ok",
            "duration_sec": duration_sec,
            "quality_profile": quality_profile,
            "cleanup_profile": cleanup_profile,
            "translation_mode": translation.mode,
            "translation_style": translation_style,
            "translate_and_paste": translate_and_paste,
            "translation_status": translation_status,
            "source_lang": translation.source_lang,
            "target_lang": translation.target_lang,
            "translation_engine": translation.engine,
            "text": display_text,
            "original_text": text,
            "translated_text": translated_text,
            "history_id": item.id,
            "ts": item.ts,
            "stop_tail_trim_ms": stop_tail_trim_ms,
            "silence_detected": silence_detected,
            "silence_guard_enabled": silence_guard_enabled,
            "background_guard_rejected": background_guard_rejected,
        }
        event_bus.emit_typed(EventType.STT_FINAL, SttFinal(
            history_id=item.id,
            text=final_text,
            duration_sec=duration_sec,
            language=tp.get("language"),
            confidence=tp.get("confidence"),
        ))

        # Автосохранение транскрибации в .md файл
        if self._coerce_bool(settings.get("auto_save_transcripts", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                item_dict = {
                    "text": display_text,
                    "ts": item.ts,
                    "audio_duration_sec": duration_sec,
                    "confidence": tp.get("confidence"),
                    "translated_text": translated_text,
                    "translation_status": translation_status,
                    "diarization": diarization_data,
                }
                saved_path = TranscriptWriter.write_transcript(item_dict, transcripts_dir)
                result_payload["transcript_file"] = str(saved_path)
            except Exception:
                logger.exception("Не удалось автосохранить транскрибацию в .md")

        return result_payload

    def _handle_get_recording_state(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._preview_lock:
            preview_text = self._preview_text
            preview_duration = self._preview_duration_sec
        return {
            "is_recording": bool(getattr(self.recorder, "is_recording", False)),
            "duration_sec": preview_duration,
            "preview_text": preview_text,
        }

    def _handle_get_session_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние N сессий записи с метаданными."""
        limit = int(params.get("limit", 50))
        sessions = self._session_tracker.get_sessions(limit=limit)
        return {"sessions": sessions, "count": len(sessions)}

    def _handle_get_session_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает агрегированную статистику по всем сессиям в памяти."""
        return self._session_tracker.get_session_stats()

    def _handle_get_usage_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает ежедневную статистику использования: записи, длительность, слова."""

    def _handle_list_normalization_profiles(self, params: dict) -> dict:
        """Возвращает список всех профилей нормализации текста."""
        return {"profiles": self._norm_profiles.list_profiles()}

    def _handle_apply_normalization_profile(self, params: dict) -> dict:
        """Применяет профиль нормализации к переданному тексту."""
        text = params.get("text", "")
        profile_name = params.get("profile", "clean")
        result = self._norm_profiles.apply_profile(text, profile_name)
        return {"text": result, "profile": profile_name}

        return self._usage_tracker.get_usage_stats()

    def _handle_get_system_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает информацию о системных ресурсах: CPU, RAM, диск, GPU."""
        return self._system_monitor.get_system_info()

    def _handle_detect_language(self, params: dict[str, Any]) -> dict[str, Any]:
        """Определяет язык текста (или пакета текстов) эвристически."""
        detector = LanguageDetector()
        texts = params.get("texts")
        if texts is not None:
            # Пакетный режим
            if not isinstance(texts, list):
                raise ValueError("Параметр 'texts' должен быть массивом строк")
            results = detector.detect_batch([str(t) for t in texts])
            return {
                "results": [
                    {"language": r.language, "confidence": r.confidence, "script": r.script}
                    for r in results
                ]
            }
        # Одиночный режим
        text = str(params.get("text", ""))
        result = detector.detect(text)
        return {"language": result.language, "confidence": result.confidence, "script": result.script}

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

    def _handle_convert_audio(self, params: dict) -> dict:
        """Конвертирует аудиофайл в указанный формат (по умолчанию WAV 16kHz mono)."""
        input_path = str(params.get("input_path", "")).strip()
        if not input_path:
            raise ValueError("Параметр 'input_path' обязателен")
        output_format = str(params.get("output_format", "wav")).strip() or "wav"
        sample_rate = int(params.get("sample_rate", 16000))
        output_path = params.get("output_path")
        output = self._audio_converter.convert(
            input_path=input_path,
            output_format=output_format,
            sample_rate=sample_rate,
            output_path=str(output_path) if output_path else None,
        )
        return {"output_path": output, "format": output_format, "sample_rate": sample_rate}

    def _handle_get_audio_info(self, params: dict) -> dict:
        """Возвращает метаданные аудиофайла."""
        path = str(params.get("path", "")).strip()
        if not path:
            raise ValueError("Параметр 'path' обязателен")
        info = self._audio_converter.get_audio_info(path)
        return {
            "duration": info.duration,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "format": info.format,
            "size_mb": info.size_mb,
        }

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
    def _format_text_with_speakers(text: str, diarization: dict | None) -> str:
        """Форматирует текст с метками спикеров из diarization speaker_turns.

        Если diarization неактивен или менее 2 спикеров — возвращает исходный текст.
        Использует speaker_turns (склеенные реплики) для читаемого вывода.
        """
        if not diarization or not isinstance(diarization, dict):
            return text
        if not diarization.get("enabled"):
            return text
        turns = diarization.get("speaker_turns", [])
        if not turns or len(turns) < 2:
            return text
        # Check that there are actually multiple speakers
        speakers = {t.get("speaker") for t in turns if t.get("speaker")}
        if len(speakers) < 2:
            return text
        parts: list[str] = []
        current_speaker = None
        for turn in turns:
            speaker = turn.get("speaker", "?")
            turn_text = str(turn.get("text", "")).strip()
            if not turn_text:
                continue
            if speaker != current_speaker:
                current_speaker = speaker
                parts.append(f"\n[{speaker}]: {turn_text}")
            else:
                parts.append(f" {turn_text}")
        if parts:
            return "".join(parts).strip()
        return text

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
        """Возвращает комплексную диагностику: системная информация, STT, LLM, история и кэш настроек."""
        try:
            diarization_device = str(self.transcriber.engine._resolve_diarization_device())
        except Exception:
            diarization_device = "unknown"

        try:
            history_count = self.store.count_active_items()
        except Exception:
            history_count = -1

        # Агрегированный отчёт профайлера по всем отслеживаемым span'ам (STT/translate/LLM).
        try:
            profiler_report = performance_profiler.get_profile_report()
        except Exception as exc:
            logger = logging.getLogger("KrabEar.Backend.Service")
            logger.warning("Не удалось получить отчёт профайлера: %s", exc)
            profiler_report = {
                "methods": {},
                "slowest_methods": [],
                "total_profiled_time_sec": 0.0,
                "error": str(exc),
            }

        return {
            "system": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "uptime_sec": time.monotonic() - self._start_time,
            },
            "stt": {
                "model_balanced": settings.MODEL_BALANCED,
                "model_max": settings.MODEL_MAX_CANDIDATES,
                "quality_profile": self.transcriber.engine.quality_profile,
                "current_model": self.transcriber.engine.current_model,
                "diarization_enabled": settings.DIARIZATION_ENABLED,
                "diarization_device": diarization_device,
            },
            "llm": self._llm_rewriter.status() if self._llm_rewriter else {"enabled": False},
            "history": {
                "total_items": history_count,
                "data_dir": str(self.store.data_dir),
                "transcripts_dir": str(Path(self.store.data_dir) / "transcripts"),
            },
            "settings_cache": {
                "ttl_sec": self._settings_svc._cache_ttl,
                "cached": self._settings_svc._cache is not None,
            },
            "profiler": profiler_report,
        }

    def _handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Агрегированный health check всех ключевых подсистем бэкенда."""
        return self._health_checker.check_all()

    def _handle_get_shutdown_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус последнего graceful shutdown.

        Returns:
            dict с ключами: clean (bool|None), last_shutdown_time (str|None),
            shutdown_in_progress (bool).
        """
        return self._shutdown_handler.get_shutdown_status()

    def _handle_get_startup_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает результаты диагностики при старте бэкенда."""
        report = self._startup_diagnostics.run_all_checks()
        return report.to_dict()

    def _handle_analyze_audio_quality(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pre-flight анализ качества аудиофайла перед транскрипцией.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)

        Returns:
            Словарь с метриками качества: rms_level, peak_level, snr_estimate_db,
            clipping_ratio, silence_ratio, duration_sec, quality_score, warnings.
        """
        from core.audio_quality import analyze_file

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        report = analyze_file(file_path)
        return report.to_dict()

    def _handle_analyze_silence(self, params: dict[str, Any]) -> dict[str, Any]:
        """Обнаруживает участки тишины в аудиофайле.

        Params:
            file_path (str): путь к аудиофайлу.
            threshold_db (float, optional): порог тишины в дБ (по умолчанию -40).

        Returns:
            Словарь с silence_regions, speech_ratio, total_silence_sec, duration_sec.
        """
        from core.silence_detector import analyze_silence_file

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        threshold_db = float(params.get("threshold_db", -40.0))
        return analyze_silence_file(file_path, threshold_db=threshold_db)

    def _handle_profile_noise(self, params: dict[str, Any]) -> dict[str, Any]:
        """Профилирует фоновый шум в аудиофайле.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)

        Returns:
            Словарь с полями: noise_type, noise_level_db, snr_db,
            frequency_profile, recommendations, suitable_for_stt.
        """
        from core.noise_profiler import NoiseProfiler

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        import soundfile as sf  # lazy import, аналогично analyze_audio_quality

        path = Path(file_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Аудиофайл не найден: {path}")

        audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
        profiler = NoiseProfiler()
        result = profiler.profile(audio_data, sample_rate)
        return result.to_dict()

    def _handle_detect_voice_activity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Обнаруживает участки речи/тишины в аудиофайле (VAD).

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)
            frame_ms (int, optional): длина фрейма анализа в мс (по умолчанию 30).
            margin_db (float, optional): добавка к шумовому порогу в дБ (по умолчанию 10).
            onset_frames (int, optional): фреймов для открытия сегмента (по умолчанию 3).
            offset_frames (int, optional): тихих фреймов для закрытия сегмента (по умолчанию 5).

        Returns:
            Словарь с speech_segments, speech_ratio, total_speech_sec, total_silence_sec.
        """
        import soundfile as sf
        from core.vad import VoiceActivityDetector

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        frame_ms = int(params.get("frame_ms", 30))
        margin_db = float(params.get("margin_db", 10.0))
        onset_frames = int(params.get("onset_frames", 3))
        offset_frames = int(params.get("offset_frames", 5))

        audio, sr = sf.read(file_path, dtype="float32", always_2d=False)
        detector = VoiceActivityDetector(
            margin_db=margin_db,
            onset_frames=onset_frames,
            offset_frames=offset_frames,
        )
        result = detector.detect(audio, sample_rate=sr, frame_ms=frame_ms)
        out = result.to_dict()
        out["file_path"] = file_path
        out["sample_rate"] = sr
        out["duration_sec"] = round(len(audio) / sr, 4)
        return out

    def _handle_get_waveform(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует waveform-данные из аудиофайла для GUI-визуализации.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)
            num_points (int, optional): количество точек waveform (по умолчанию 200).

        Returns:
            Словарь с полями: points, duration_sec, sample_rate, peak_amplitude, rms_amplitude.
        """
        from core.waveform_generator import WaveformGenerator

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        num_points = int(params.get("num_points", 200))
        gen = WaveformGenerator()
        wf = gen.generate_from_file(file_path, num_points=num_points)
        return {
            "points": wf.points,
            "duration_sec": wf.duration_sec,
            "sample_rate": wf.sample_rate,
            "peak_amplitude": wf.peak_amplitude,
            "rms_amplitude": wf.rms_amplitude,
        }

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
        preview_active = self._preview_thread is not None and self._preview_thread.is_alive()

        return {
            "session": {
                "recording_active": bool(getattr(self.recorder, 'is_recording', False)),
                "preview_active": preview_active,
                "preview_text_length": len(self._preview_text),
                "preview_duration_sec": self._preview_duration_sec,
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

    def _handle_summarize_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Локальный lightweight-summary для длинных заметок/транскриптов."""
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("text обязателен")
        mode = str(params.get("mode", "summary_short")).strip() or "summary_short"
        max_points = int(params.get("max_points", 3) or 3)
        max_points = max(1, min(max_points, 12))
        summary = self._summarize_text_locally(text=text, mode=mode, max_points=max_points)
        return {
            "mode": summary["mode"],
            "summary": summary["summary"],
            "bullets": summary["bullets"],
            "source_chars": len(text),
        }

    @staticmethod
    def _summarize_text_locally(text: str, mode: str, max_points: int) -> dict[str, Any]:
        """Простая эвристика summary без внешних зависимостей."""
        normalized = " ".join(text.replace("\r", "\n").split())
        if not normalized:
            return {"mode": mode, "summary": "", "bullets": []}

        chunks = []
        for raw in re.split(r"(?<=[.!?])\s+", normalized):
            sentence = raw.strip()
            if sentence:
                chunks.append(sentence)
        if not chunks:
            chunks = [normalized]

        if mode == "summary_detailed":
            bullets = chunks[:max_points]
            summary = " ".join(chunks[: min(len(chunks), max_points + 1)])
        else:
            # Короткий summary: первая смысловая фраза + маркеры.
            head = chunks[0]
            bullets = chunks[1: 1 + max_points]
            if not bullets:
                bullets = chunks[:max_points]
            summary = head
        return {"mode": mode, "summary": summary, "bullets": bullets}

    def _generate_summary(self, text: str) -> str | None:
        """Генерирует краткое LLM-summary для длинного текста. Возвращает None если LLM недоступен."""
        if self._llm_rewriter is None:
            return None
        try:
            result = self._llm_rewriter.summarize(text, max_sentences=3)
            if result.ok and result.text:
                logger.info("LLM summary сгенерировано (%d мс)", result.latency_ms or 0)
                return result.text
            logger.debug("LLM summary не удалось: %s", result.fallback_reason)
            return None
        except Exception as exc:
            logger.warning("Ошибка генерации LLM summary: %s", exc)
            return None

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

    def _handle_list_audio_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных входных аудиоустройств."""
        items = self._list_audio_inputs()
        default_input_id = None
        for item in items:
            if item.get("is_default"):
                default_input_id = item.get("id")
                break
        return {
            "items": items,
            "count": len(items),
            "default_input_id": default_input_id,
        }

    def _handle_get_audio_devices(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных входных аудиоустройств (обёртка для GUI)."""
        return {"devices": self._list_audio_inputs()}

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
                "devices": self._list_audio_inputs(),
            }
        except Exception as exc:
            logger.warning("test_microphone: ошибка записи — %s", exc)
            return {
                "ok": False,
                "error": str(exc),
                "devices": self._list_audio_inputs(),
            }

    def _handle_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Синхронная транскрибация списка файлов (CLI/legacy путь).

        Делегирует в `_transcribe_paths_core` без progress/cancel коллбеков.
        """
        return self._transcribe_paths_core(params)

    def _transcribe_paths_core(
        self,
        params: dict[str, Any],
        *,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_file_start: Callable[[int, str], None] | None = None,
        on_file_done: Callable[[int, dict[str, Any] | None, str | None], None] | None = None,
    ) -> dict[str, Any]:
        """Общее ядро синхронной и асинхронной транскрибации.

        Args:
            params: параметры IPC (paths, quality_profile, ...).
            progress_callback: вызывается движком STT с именем стадии
                (audio_load/normalize/stt/cleanup/diarize/...). Передаётся в
                `AudioEngine.transcribe(progress_callback=...)`.
            cancel_check: предикат — если возвращает True между файлами, цикл
                прекращается (мид-файл не прерываем, чтобы не оставить STT
                в неопределённом состоянии).
            on_file_start: вызывается перед обработкой файла index (0-based) с путём.
            on_file_done: вызывается после файла — (index, item_dict|None, err|None).
        """
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        settings = self._cached_settings()
        quality_profile = str(params.get("quality_profile") or settings.get("quality_profile", "balanced"))
        cleanup_profile = str(params.get("cleanup_profile") or settings.get("cleanup_profile", "soft"))
        lang_hint: str | None = params.get("lang_hint") or None
        translation_mode = str(params.get("translation_mode") or settings.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style") or settings.get("translation_style", "neutral"))
        translation_glossary = settings.get("translation_glossary", {})
        translate_and_paste = bool(
            params.get("translate_and_paste")
            if "translate_and_paste" in params
            else settings.get("translate_and_paste", False)
        )
        network_mode = str(settings.get("network_mode", "offline_default"))

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected)
        if not audio_paths:
            return {"items": [], "processed": 0, "errors": ["Не найдено аудиофайлов для транскрибации"]}

        # Загружаем пользовательский vocabulary для подсказок Whisper
        user_vocabulary = self.vocabulary.load() or []

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for file_index, audio_path in enumerate(audio_paths):
            # Cancel-between-files: если запрошена отмена, ровно прерываем цикл.
            if cancel_check is not None and cancel_check():
                break
            if on_file_start is not None:
                try:
                    on_file_start(file_index, audio_path)
                except Exception:
                    logger.exception("on_file_start callback упал для %s", audio_path)
            started_at = time.monotonic()
            try:
                # Determine audio file duration before transcription
                audio_duration_sec: float | None = None
                try:
                    import soundfile as sf
                    sf_info = sf.info(audio_path)
                    audio_duration_sec = round(sf_info.duration, 3)
                except Exception:
                    pass  # Non-critical: duration is informational

                # For file imports, default to auto-detect if no explicit hint
                import_lang_hint = lang_hint if lang_hint else "auto"
                # Если есть progress_callback — идём напрямую через engine, чтобы
                # передать kwarg. Иначе используем стабильный путь через Transcriber.
                if progress_callback is not None:
                    self.transcriber.engine.set_quality_profile(quality_profile)
                    transcribe_payload = self.transcriber.engine.transcribe(
                        audio_path,
                        cleanup_profile=cleanup_profile,
                        is_preview=False,
                        domain="casual",
                        extra_vocabulary=user_vocabulary if user_vocabulary else None,
                        lang_hint=import_lang_hint,
                        progress_callback=progress_callback,
                    )
                else:
                    transcribe_payload = self.transcriber.transcribe(
                        audio_path,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        lang_hint=import_lang_hint,
                        extra_vocabulary=user_vocabulary if user_vocabulary else None,
                    )
                text = self._extract_transcribed_text(transcribe_payload)
                elapsed = round(time.monotonic() - started_at, 3)
                if not text:
                    err = self._extract_transcribed_error(transcribe_payload)
                    if err:
                        err_line = f"{audio_path}: {err}"
                    else:
                        err_line = f"{audio_path}: пустой результат"
                    errors.append(err_line)
                    if on_file_done is not None:
                        try:
                            on_file_done(file_index, None, err_line)
                        except Exception:
                            logger.exception("on_file_done callback упал для %s", audio_path)
                    continue
                diarization_data = transcribe_payload.get("diarization") if isinstance(transcribe_payload, dict) else None
                detected_lang = transcribe_payload.get("language", "?") if isinstance(transcribe_payload, dict) else "?"

                translation = self.translator.translate(
                    text=text,
                    mode=translation_mode,
                    network_mode=network_mode,
                    translation_style=translation_style,
                    glossary=translation_glossary,
                )
                translated_text = translation.text.strip() if translation.ok else ""
                final_text = translated_text if (translate_and_paste and translated_text) else text

                # Format text with speaker labels if diarization produced multiple speakers
                display_text = self._format_text_with_speakers(final_text, diarization_data)

                history_item = self.store.add_history_item(
                    text=display_text,
                    paste_status="failed",
                    source_text=text,
                    translated_text=translated_text,
                    translation_mode=translation.mode,
                    source_lang=translation.source_lang,
                    target_lang=translation.target_lang,
                    translation_status=translation.status,
                    translation_engine=translation.engine,
                    diarization=diarization_data,
                    audio_duration_sec=audio_duration_sec,
                    emotion=(
                        transcribe_payload.get("emotion")
                        if isinstance(transcribe_payload, dict)
                        and isinstance(transcribe_payload.get("emotion"), str)
                        else None
                    ),
                )

                # Auto-summary для длинных транскрипций (>500 символов)
                summary: str | None = None
                if len(final_text) > 500:
                    summary = self._generate_summary(final_text)

                # Save transcript to file
                try:
                    transcripts_dir = Path(self.store.data_dir) / "transcripts"
                    transcripts_dir.mkdir(exist_ok=True)
                    source_name = Path(audio_path).stem
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    transcript_filename = f"{timestamp}_{source_name}.md"
                    transcript_path = transcripts_dir / transcript_filename
                    with open(transcript_path, "w", encoding="utf-8") as f:
                        f.write(f"# Транскрипт: {Path(audio_path).name}\n\n")
                        f.write(f"- Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        if audio_duration_sec is not None:
                            _mins = int(audio_duration_sec) // 60
                            _secs = audio_duration_sec - _mins * 60
                            f.write(f"- Аудио: {_mins}м {_secs:.1f}с\n")
                        f.write(f"- Обработка: {elapsed:.1f}с\n")
                        f.write(f"- Источник: {audio_path}\n")
                        f.write(f"- Язык: {detected_lang}\n")
                        diar_info = transcribe_payload.get("diarization", {}) if isinstance(transcribe_payload, dict) else {}
                        if diar_info and diar_info.get("enabled"):
                            speakers = diar_info.get("speaker_turns", [])
                            unique_speakers = len(set(t.get("speaker") for t in speakers))
                            f.write(f"- Спикеры: {unique_speakers}\n")
                        if summary:
                            f.write(f"\n## Краткое содержание\n\n{summary}\n")
                        # Use speaker-labeled text if diarization is active
                        if diar_info and diar_info.get("enabled") and diar_info.get("speaker_turns"):
                            f.write(f"\n## Диалог\n\n{display_text}\n")
                        else:
                            f.write(f"\n## Текст\n\n{final_text}\n")
                        if translated_text:
                            f.write(f"\n## Перевод ({translation.mode})\n\n{translated_text}\n")
                except Exception as exc:
                    logger.warning("Не удалось сохранить транскрипт в файл: %s", exc)

                item_result: dict[str, Any] = {
                    "path": audio_path,
                    "text": display_text,
                    "original_text": text,
                    "translated_text": translated_text,
                    "translation_mode": translation.mode,
                    "translation_style": translation_style,
                    "translation_status": translation.status,
                    "source_lang": translation.source_lang,
                    "target_lang": translation.target_lang,
                    "history_id": history_item.id,
                    "duration_sec": elapsed,
                    "audio_duration_sec": audio_duration_sec,
                    "language": detected_lang,
                }
                if summary:
                    item_result["summary"] = summary
                items.append(item_result)
                if on_file_done is not None:
                    try:
                        on_file_done(file_index, item_result, None)
                    except Exception:
                        logger.exception("on_file_done callback упал для %s", audio_path)
            except Exception as exc:
                err_msg = str(exc)
                file_name = Path(audio_path).name
                if "Resource deadlock" in err_msg:
                    err_msg = f"Файл заблокирован (возможно iCloud): {file_name}"
                elif "timeout" in err_msg.lower():
                    err_msg = f"Превышено время транскрибации: {file_name}"
                elif "No such file" in err_msg:
                    err_msg = f"Файл не найден: {file_name}"
                elif "Permission denied" in err_msg:
                    err_msg = f"Нет доступа к файлу: {file_name}"
                elif (
                    "too large" in err_msg.lower()
                    or "MAX_AUDIO_MB" in err_msg
                    or "слишком большой" in err_msg.lower()
                ):
                    err_msg = f"{file_name}: {err_msg}"
                elif "Unsupported" in err_msg or "codec" in err_msg.lower():
                    err_msg = f"Неподдерживаемый формат аудио: {file_name}"
                else:
                    err_msg = f"{file_name}: {err_msg}"
                errors.append(err_msg)
                if on_file_done is not None:
                    try:
                        on_file_done(file_index, None, err_msg)
                    except Exception:
                        logger.exception("on_file_done callback упал для %s", audio_path)

        return {
            "items": items,
            "processed": len(items),
            "errors": errors,
        }

    def _handle_transcribe_paths_async(self, params: dict[str, Any]) -> dict[str, Any]:
        """Асинхронный вариант `transcribe_paths`: возвращает job_id сразу.

        Запускает фоновый worker-поток, прогресс доступен через
        `get_transcribe_progress(job_id)`. Отмена — `cancel_transcribe_job`.
        Полный контракт — см. /tmp/krab-ear-async/API_CONTRACT.md.
        """
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")
        # Валидируем/предсчитываем список аудио-путей заранее, чтобы
        # total_files в прогрессе соответствовал реально обрабатываемым файлам.
        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [
            r.resolve()
            for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
        try:
            audio_paths = self._collect_audio_paths(selected) if selected else []
        except Exception:
            audio_paths = []
        total_files = len(audio_paths)

        job_id = self._job_tracker.create_job(total_files=total_files)

        # Копия параметров (params mutable — защищаемся от побочных мутаций).
        job_params = dict(params)

        def _on_file_start(index: int, audio_path: str) -> None:
            self._job_tracker.update(
                job_id,
                status="running",
                current_file=Path(audio_path).name,
                current_stage="idle",
                file_index=index + 1,
            )

        def _on_file_done(
            index: int,
            item: dict[str, Any] | None,
            err: str | None,
        ) -> None:
            state = self._job_tracker.get(job_id) or {}
            new_items = list(state.get("items") or [])
            new_errors = list(state.get("errors") or [])
            if item is not None:
                new_items.append(item)
            if err is not None:
                new_errors.append(err)
            self._job_tracker.update(
                job_id,
                items=new_items,
                errors=new_errors,
                processed=len(new_items),
            )

        def _progress_callback(stage: str) -> None:
            self._job_tracker.update(job_id, current_stage=str(stage))

        def _cancel_check() -> bool:
            state = self._job_tracker.get(job_id)
            return bool(state and state.get("cancel_requested"))

        def _worker() -> None:
            try:
                self._job_tracker.update(job_id, status="running")
                result = self._transcribe_paths_core(
                    job_params,
                    progress_callback=_progress_callback,
                    cancel_check=_cancel_check,
                    on_file_start=_on_file_start,
                    on_file_done=_on_file_done,
                )
                # Финальное состояние: cancelled | done.
                state = self._job_tracker.get(job_id) or {}
                if state.get("cancel_requested"):
                    self._job_tracker.update(
                        job_id,
                        status="cancelled",
                        items=list(result.get("items") or []),
                        errors=list(result.get("errors") or []),
                        processed=len(result.get("items") or []),
                        current_stage="idle",
                        finished_at=time.monotonic(),
                    )
                else:
                    self._job_tracker.mark_done(
                        job_id,
                        items=list(result.get("items") or []),
                        errors=list(result.get("errors") or []),
                    )
            except Exception as exc:
                logger.exception("Async transcribe job %s упал", job_id)
                self._job_tracker.mark_failed(job_id, str(exc))

        thread = threading.Thread(
            target=_worker,
            name=f"transcribe-{job_id}",
            daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    def _handle_get_transcribe_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние async-job'а.

        См. схему ответа в API_CONTRACT.md. Поле `items` заполнено только
        после `status == "done"`, но private-накопление ведётся по мере обработки.
        """
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        state = self._job_tracker.get(job_id)
        if state is None:
            raise RuntimeError(f"Неизвестный job_id: {job_id}")

        status = str(state.get("status") or "queued")
        items_raw = list(state.get("items") or [])
        # Контракт: items отдаём только когда job завершён.
        items_out = items_raw if status in ("done", "failed", "cancelled") else []

        # ETA: грубая оценка при наличии audio_duration_sec у последнего item'а
        # (при отсутствии движковой метрики). 10× реалтайм для max-профиля.
        elapsed_sec = float(state.get("elapsed_sec") or 0.0)
        eta_sec: float | None = None
        total_audio = 0.0
        for it in items_raw:
            dur = it.get("audio_duration_sec") if isinstance(it, dict) else None
            if isinstance(dur, (int, float)):
                total_audio += float(dur)
        if total_audio > 0:
            eta_sec = max(0.0, total_audio * 10.0 - elapsed_sec)

        return {
            "status": status,
            "current_file": str(state.get("current_file") or ""),
            "current_stage": str(state.get("current_stage") or "idle"),
            "file_index": int(state.get("file_index") or 0),
            "total_files": int(state.get("total_files") or 0),
            "elapsed_sec": round(elapsed_sec, 3),
            "eta_sec": round(eta_sec, 3) if eta_sec is not None else None,
            "processed": int(state.get("processed") or 0),
            "errors": list(state.get("errors") or []),
            "items": items_out,
        }

    def _handle_cancel_transcribe_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сигнализирует воркеру об отмене job'а.

        Статус реально меняется на 'cancelled' после завершения текущего файла —
        мид-файл прерываний не делаем, чтобы STT не оставался в inconsistent state.
        """
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        cancelled = self._job_tracker.cancel(job_id)
        return {"cancelled": bool(cancelled)}

    def _handle_preview_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Быстрый предпросмотр импорта: считает аудиофайлы без транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected)
        sample_limit = int(params.get("sample_limit", 5) or 5)
        safe_sample_limit = max(1, min(sample_limit, 50))
        by_ext: dict[str, int] = {}
        total_bytes = 0
        # Группировка по родительской папке для отображения структуры.
        by_folder: dict[str, int] = {}
        for audio_path in audio_paths:
            suffix = Path(audio_path).suffix.lower() or "<none>"
            by_ext[suffix] = by_ext.get(suffix, 0) + 1
            folder = str(Path(audio_path).parent)
            by_folder[folder] = by_folder.get(folder, 0) + 1
            try:
                total_bytes += Path(audio_path).stat().st_size
            except FileNotFoundError:
                continue
        return {
            "input_count": len(selected),
            "audio_count": len(audio_paths),
            "folder_count": len(by_folder),
            "by_folder": by_folder,
            "sample": audio_paths[:safe_sample_limit],
            "by_ext": by_ext,
            "total_bytes": total_bytes,
        }

    @staticmethod
    def _collect_audio_paths(paths: list[str]) -> list[str]:
        audio_ext = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".m4b", ".aif", ".aiff"}
        result: list[str] = []

        for raw in paths:
            path = Path(raw).expanduser()
            if not path.exists():
                continue

            if path.is_file():
                if path.suffix.lower() in audio_ext:
                    result.append(str(path.resolve()))
                continue

            if path.is_dir():
                # Сортируем по пути, чтобы части записей звонков
                # (part1.m4a, part2.m4a, ...) шли в правильном порядке.
                candidates = sorted(
                    (c for c in path.rglob("*") if c.is_file() and c.suffix.lower() in audio_ext),
                    key=lambda c: str(c),
                )
                result.extend(str(c.resolve()) for c in candidates)

        # Убираем дубликаты, сохраняем порядок.
        unique: list[str] = []
        seen: set[str] = set()
        for item in result:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    def _start_preview_worker(self, quality_profile: str) -> None:
        self._stop_preview_worker()
        self._preview_stop_event.clear()
        self._preview_thread = threading.Thread(
            target=self._preview_loop,
            args=(quality_profile,),
            daemon=True,
        )
        self._preview_thread.start()

    def _stop_preview_worker(self) -> None:
        self._preview_stop_event.set()
        if self._preview_thread and self._preview_thread.is_alive():
            self._preview_thread.join(timeout=1.5)
        self._preview_thread = None

    def _reset_preview_state(self) -> None:
        with self._preview_lock:
            self._preview_text = ""
            self._preview_duration_sec = 0.0
            self._preview_updated_at = 0.0

    def _preview_loop(self, quality_profile: str) -> None:
        snapshot_audio = getattr(self.recorder, "snapshot_audio", None)
        min_samples = int(getattr(self.recorder, "sample_rate", 16000) * 0.8)
        last_refresh_duration = 0.0
        # Adaptive backoff: увеличивается при пустых результатах, сбрасывается при речи.
        poll_interval = 0.35
        _POLL_MIN = 0.35
        _POLL_MAX = 1.5
        _POLL_BACKOFF = 1.5

        while not self._preview_stop_event.is_set():
            if not bool(getattr(self.recorder, "is_recording", False)):
                break

            if not callable(snapshot_audio):
                self._preview_stop_event.wait(poll_interval)
                continue

            try:
                audio_data, duration_sec = snapshot_audio(max_duration_sec=12.0)
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка snapshot_audio")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue

            with self._preview_lock:
                self._preview_duration_sec = float(duration_sec)

            current_size = int(getattr(audio_data, "size", 0))
            if current_size < min_samples:
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue
            # Важный нюанс: после достижения лимита snapshot-а размер буфера стабилизируется.
            # Поэтому ориентируемся на прогресс времени записи, а не на size.
            if duration_sec - last_refresh_duration < 0.9:
                self._preview_stop_event.wait(_POLL_MIN)
                continue

            try:
                preview_payload = self.transcriber.transcribe_preview(
                    audio_data,
                    quality_profile=quality_profile,
                )
                preview_text = self._extract_transcribed_text(preview_payload)
                preview_text = self._postprocess_preview_text(preview_text)
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка transcribe_preview")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue

            self._preview_error_count = 0
            if preview_text:
                with self._preview_lock:
                    self._preview_text = preview_text[-900:]
                    self._preview_updated_at = float(duration_sec)
                event_bus.emit_typed(EventType.STT_PARTIAL, SttPartial(
                    text=preview_text[-900:],
                    duration_sec=float(duration_sec),
                ))
                poll_interval = _POLL_MIN
            else:
                with self._preview_lock:
                    self._preview_text = ""
                    self._preview_updated_at = float(duration_sec)
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
            last_refresh_duration = float(duration_sec)
            self._preview_stop_event.wait(poll_interval)

    @staticmethod
    def _list_audio_inputs() -> list[dict[str, Any]]:
        """Пытается безопасно получить список входных аудиоустройств."""
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            logger.warning("Failed to list audio inputs: %s", exc)
            return []

        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("Не удалось получить список аудиоустройств")
            return []

        hostapis: list[str] = []
        try:
            hostapi_payload = sd.query_hostapis()
            hostapis = [str(item.get("name", "")) for item in hostapi_payload]
        except Exception:
            hostapis = []

        default_input_idx = None
        try:
            default_device = sd.default.device
            if isinstance(default_device, (list, tuple)) and default_device:
                default_input_idx = int(default_device[0])
        except Exception:
            default_input_idx = None

        results: list[dict[str, Any]] = []
        for index, device in enumerate(devices):
            try:
                max_input_channels = int(device.get("max_input_channels", 0))
            except Exception:
                max_input_channels = 0
            if max_input_channels <= 0:
                continue
            hostapi_index = int(device.get("hostapi", -1))
            hostapi_name = hostapis[hostapi_index] if 0 <= hostapi_index < len(hostapis) else ""
            name = str(device.get("name", f"Input {index}")).strip()
            lowered = name.lower()
            tags: list[str] = []
            if "blackhole" in lowered:
                tags.append("loopback")
            if "shure" in lowered or "mic" in lowered or "microphone" in lowered:
                tags.append("mic")
            if "loopback" in lowered and "loopback" not in tags:
                tags.append("loopback")
            results.append(
                {
                    "id": index,
                    "name": name,
                    "hostapi": hostapi_name,
                    "max_input_channels": max_input_channels,
                    "default_samplerate": int(float(device.get("default_samplerate", 0) or 0)),
                    "is_default": bool(default_input_idx == index),
                    "tags": tags,
                }
            )
        return results

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
    def _coerce_bounded(value: Any, default: int | float, min_value: int | float, max_value: int | float) -> int | float:
        """Нормализует числовое значение в допустимый диапазон. Тип определяется default."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    # Aliases for backward compatibility with existing call sites
    _coerce_bounded_int = _coerce_bounded
    _coerce_bounded_float = _coerce_bounded

    def _stop_recorder_guarded(self, stop_tail_trim_ms: int) -> tuple[Any, float] | None:
        """
        Останавливает рекордер с поддержкой старых сигнатур stop().

        Нужен для совместимости фейков/старых реализаций, где метод `stop`
        ещё не принимает `trim_tail_ms`.
        """
        stop_callable = getattr(self.recorder, "stop", None)
        if not callable(stop_callable):
            raise RuntimeError("Рекордер не поддерживает stop()")
        try:
            return stop_callable(trim_tail_ms=stop_tail_trim_ms)
        except TypeError:
            return stop_callable()

    @staticmethod
    def _looks_like_silence_audio(
        audio: Any,
        sample_rate: int,
        rms_threshold: float,
        peak_threshold: float,
        active_ratio_threshold: float,
    ) -> bool:
        """
        Эвристически определяет, есть ли в буфере реальная речь.

        Логика:
        - очень низкие peak/rms -> считаем тишиной;
        - иначе считаем долю «активных» 20мс фреймов и отсекаем фоновой шум.
        """
        try:
            data = np.asarray(audio, dtype=np.float32).reshape(-1)
        except Exception:
            return False
        if data.size == 0:
            return True

        abs_data = np.abs(data)
        peak = float(abs_data.max(initial=0.0))
        rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64)))
        if peak <= peak_threshold and rms <= rms_threshold:
            return True

        frame_size = max(1, int(sample_rate * 0.02))  # 20мс
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return peak <= (peak_threshold * 1.2) and rms <= (rms_threshold * 1.4)

        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        activity_threshold = max(rms_threshold * 2.0, 0.0035)
        active_ratio = float(np.mean(frame_rms >= activity_threshold))

        return active_ratio < active_ratio_threshold and peak <= (peak_threshold * 1.5)

    @staticmethod
    def _looks_like_distant_background_speech(
        audio: Any,
        sample_rate: int,
        min_peak: float,
        min_rms: float,
        uniform_frame_threshold: float,
        max_uniform_active_ratio: float,
    ) -> bool:
        """
        Эвристика "дальняя фоновая речь", чтобы не коммитить ТВ/видео вместо диктовки.

        Идея:
        - если уровень слишком низкий (нет близкой речи);
        - и при этом энергия распределена почти равномерно без естественных пауз,
          что характерно для далёкого источника/фона.
        """
        try:
            data = np.asarray(audio, dtype=np.float32).reshape(-1)
        except Exception:
            return False
        if data.size == 0:
            return False

        abs_data = np.abs(data)
        peak = float(abs_data.max(initial=0.0))
        rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64)))
        low_level = peak < min_peak and rms < min_rms

        frame_size = max(1, int(sample_rate * 0.02))  # 20мс
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return low_level

        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        mean_rms = float(np.mean(frame_rms))
        std_rms = float(np.std(frame_rms))
        variation_coeff = std_rms / max(mean_rms, 1e-8)
        duration_sec = float(data.size) / max(float(sample_rate), 1.0)

        # Для тихих сигналов опускаем порог активности, иначе равномерный фон
        # может казаться "неактивным" и проскальзывать мимо фильтра.
        dynamic_uniform_threshold = max(0.0012, min(uniform_frame_threshold, max(min_rms * 0.35, 0.0012)))
        active_ratio = float(np.mean(frame_rms >= dynamic_uniform_threshold))

        # Равномерный плотный поток без естественных пауз считаем фоном даже при чуть
        # более высоком уровне: это типичный паттерн "ролик на фоне".
        background_pattern = active_ratio >= max_uniform_active_ratio and variation_coeff < 0.35
        very_uniform = active_ratio >= 0.96 and variation_coeff < 0.18
        return background_pattern and (low_level or (very_uniform and duration_sec >= 4.0))

    @staticmethod
    def _is_known_prompt_echo(normalized_text: str) -> bool:
        """
        Отлавливает типовые фразы-артефакты, которые не должны попадать в финальный текст.

        Проверяем как точные совпадения, так и вхождения фрагментов: в реальности
        артефакт часто приходит с обрывами или повтором одной и той же инструкции.
        """
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return True

        blocked_fragments = (
            "продолжение следует",
            "to be continued",
            "сохраняй смысл ставь корректную пунктуац",
            "сохраняй смысл ставь корректную пункту",
            "ставь корректную пунктуац",
            "ставь корректную пункту",
        )
        if any(fragment in normalized for fragment in blocked_fragments):
            return True

        words = normalized.split()
        compact = " ".join(words)
        if (
            "сохраняй" in words
            and "смысл" in words
            and any(token.startswith("корр") for token in words)
            and any(token.startswith("пункт") for token in words)
        ):
            return True

        return bool(re.search(r"сохраняй\s+смысл.*корр\w*.*пункт\w*", compact))

    @staticmethod
    def _contains_repeated_chunk(words: list[str], min_repeats: int = 3) -> bool:
        """
        Ищет подряд повторяющиеся куски фразы (типичный зацикленный артефакт модели).
        """
        total = len(words)
        if total < 6:
            return False

        max_chunk = min(7, total // min_repeats)
        for chunk_size in range(2, max_chunk + 1):
            start = 0
            while start + (chunk_size * min_repeats) <= total:
                chunk = words[start: start + chunk_size]
                repeats = 1
                while start + (chunk_size * (repeats + 1)) <= total:
                    next_chunk = words[
                        start + (chunk_size * repeats): start + (chunk_size * (repeats + 1))
                    ]
                    if next_chunk != chunk:
                        break
                    repeats += 1
                if repeats >= min_repeats:
                    return True
                start += 1
        return False

    @staticmethod
    def _looks_like_looping_artifact(words: list[str], min_words: int, min_bigram_hits: int) -> bool:
        """
        Детектирует «петли» и низкоинформативные повторы в транскрибе.
        """
        if len(words) < min_words:
            return False

        counts: dict[str, int] = {}
        for token in words:
            counts[token] = counts.get(token, 0) + 1

        unique_ratio = len(counts) / max(1, len(words))
        max_freq = max(counts.values()) if counts else 0
        if unique_ratio <= 0.42 and max_freq >= max(3, int(len(words) * 0.34)):
            return True

        if len(counts) <= 2 and len(words) >= 5 and max_freq >= 4:
            return True

        bigram_counts: dict[tuple[str, str], int] = {}
        for idx in range(len(words) - 1):
            key = (words[idx], words[idx + 1])
            bigram_counts[key] = bigram_counts.get(key, 0) + 1
        top_bigram_freq = max(bigram_counts.values()) if bigram_counts else 0
        if top_bigram_freq >= max(min_bigram_hits, len(words) // 5):
            return True

        return BackendService._contains_repeated_chunk(words)

    @staticmethod
    def _postprocess_transcribed_text(text: str) -> str:
        """
        Дополнительная фильтрация и базовая нормализация пунктуации.

        Цель: уменьшить артефакты на пустом/шумовом вводе и чуть улучшить читаемость.
        """
        clean = str(text or "").strip()
        if not clean:
            return ""

        lowered = clean.lower()
        # Явные тех-артефакты инструментального вывода.
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            return ""

        normalized = TextUtils.normalize_phrase(clean)
        if BackendService._is_known_prompt_echo(normalized):
            return ""

        collapsed_duplicate = BackendService._collapse_immediate_duplicate_phrase(normalized)
        if collapsed_duplicate:
            clean = collapsed_duplicate
            normalized = TextUtils.normalize_phrase(clean)

        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if BackendService._looks_like_looping_artifact(words, min_words=8, min_bigram_hits=4):
            return ""

        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        first_alpha_idx = next((idx for idx, char in enumerate(clean) if char.isalpha()), -1)
        if first_alpha_idx >= 0:
            clean = clean[:first_alpha_idx] + clean[first_alpha_idx].upper() + clean[first_alpha_idx + 1:]

        if not re.search(r"[.!?…]$", clean):
            if len(words) >= 4:
                clean = f"{clean}."

        return clean.strip()

    @staticmethod
    def _collapse_immediate_duplicate_phrase(normalized_text: str) -> str:
        """
        Схлопывает паттерн «одна и та же фраза подряд два раза».

        Пример:
        «ну он просто два раза теперь пишет ну он просто два раза теперь пишет»
        -> «Ну он просто два раза теперь пишет.»
        """
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return ""

        words = normalized.split()
        total = len(words)
        if total < 8:
            return ""

        # Базовый сценарий: точное дублирование 1-в-1.
        if total % 2 == 0:
            half = total // 2
            if words[:half] == words[half:]:
                collapsed = " ".join(words[:half]).strip()
                if not collapsed:
                    return ""
                return f"{collapsed[0].upper()}{collapsed[1:]}."

        # Допуск ±1 токен на хвосте (из-за пунктуации/обрезки).
        for shift in (-1, 1):
            left = total // 2
            right = total - left
            if abs(left - right) != 1:
                continue
            if shift < 0 and left > right:
                if words[:right] == words[left:]:
                    collapsed = " ".join(words[:right]).strip()
                    if collapsed:
                        return f"{collapsed[0].upper()}{collapsed[1:]}."
            if shift > 0 and right > left:
                if words[:left] == words[right:]:
                    collapsed = " ".join(words[:left]).strip()
                    if collapsed:
                        return f"{collapsed[0].upper()}{collapsed[1:]}."

        return ""

    @staticmethod
    def _postprocess_preview_text(text: str) -> str:
        """
        Лёгкая фильтрация realtime-preview без агрессивной пунктуации.

        Нужна, чтобы в live-subtitles не проскакивали тех-артефакты/промпт-эхо.
        """
        clean = str(text or "").strip()
        if not clean:
            return ""

        lowered = clean.lower()
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            return ""

        normalized = TextUtils.normalize_phrase(clean)
        if BackendService._is_known_prompt_echo(normalized):
            return ""

        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if BackendService._looks_like_looping_artifact(words, min_words=6, min_bigram_hits=3):
            return ""

        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    @staticmethod
    def _extract_transcribed_text(payload: Any) -> str:
        """
        Нормализует результат транскрибации в строку.

        Исторически backend получал `str`, но текущий Transcriber отдает `dict`.
        Метод поддерживает оба контракта, чтобы не ломать stop/preview pipelines.
        """
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            direct_text = payload.get("text")
            if direct_text is not None:
                return str(direct_text).strip()
            nested = payload.get("result")
            if isinstance(nested, dict):
                nested_text = nested.get("text")
                if nested_text is not None:
                    return str(nested_text).strip()
            return ""
        return str(payload).strip()

    @staticmethod
    def _extract_transcribed_error(payload: Any) -> str:
        """Извлекает текст ошибки из payload транскрибации, если он присутствует."""
        if isinstance(payload, dict):
            error = payload.get("error")
            if error is not None:
                return str(error).strip()
        return ""

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

    def _handle_analyze_quality_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует тренды качества распознавания за последние N дней."""
        days = int(params.get("days", 30))
        try:
            with self.store._lock():
                items = self.store._load_active_items_unlocked()
        except Exception:
            items = []
        report = self._quality_trends.analyze_trends(items, days=days)
        return {
            "daily_confidence": report.daily_confidence,
            "overall_trend": report.overall_trend,
            "trend_slope": report.trend_slope,
            "best_day": report.best_day,
            "worst_day": report.worst_day,
            "confidence_distribution": report.confidence_distribution,
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

    def _handle_analyze_word_timing(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует ритм речи по пословным таймстемпам Whisper.

        Params:
            segments: list[dict] — список сегментов Whisper (с полем 'words' или без).

        Возвращает TimingReport в виде словаря.
        """
        segments = params.get("segments")
        if not isinstance(segments, list):
            raise ValueError("Параметр 'segments' должен быть списком")
        report = self._word_timing_analyzer.analyze(segments)
        return report.as_dict()

    def _handle_get_speaker_statistics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает per-speaker статистику речи из диаризованных записей истории."""
        return self._speaker_statistics.handle_get_speaker_statistics(
            params,
            store=self.store,
            speaker_manager=self._speaker_manager,
        )

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

    def _handle_compare_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает несколько записей side-by-side."""
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError("Параметр item_ids обязателен (список строк)")
        view = self._recording_comparison.compare(item_ids=item_ids, store=self.store)
        return _comparison_view_to_dict(view)

    def _handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет целостность файлов данных Krab Ear."""
        report = self._integrity_checker.check_integrity(self.store.data_dir)
        return {
            "status": report.status,
            "total_items": report.total_items,
            "orphaned_tombstones": report.orphaned_tombstones,
            "invalid_json_lines": report.invalid_json_lines,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "auto_fixable": c.auto_fixable,
                }
                for c in report.checks
            ],
        }

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

    def _handle_compare_texts(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сравнивает два текста или две записи истории по ID."""
        item_id_1 = params.get("item_id_1")
        item_id_2 = params.get("item_id_2")
        text1 = params.get("text1", "")
        text2 = params.get("text2", "")

        if item_id_1 and item_id_2:
            result = self._text_comparator.compare_items(item_id_1, item_id_2, self.store)
        else:
            result = self._text_comparator.compare_texts(text1, text2)

        return {
            "similarity": result.similarity,
            "text_1": result.text_1,
            "text_2": result.text_2,
            "common_phrases": result.common_phrases,
            "unique_to_1": result.unique_to_1,
            "unique_to_2": result.unique_to_2,
            "word_count_diff": result.word_count_diff,
            "summary": result.summary,
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

    def _handle_score_readability(self, params: dict[str, Any]) -> dict[str, Any]:
        """Оценивает читабельность текста транскрибации."""
        text = params.get("text", "")
        if not text:
            return {
                "flesch_score": 0.0,
                "avg_sentence_length": 0.0,
                "avg_word_length": 0.0,
                "vocabulary_level": "simple",
                "sentence_count": 0,
                "word_count": 0,
                "longest_sentence": "",
                "shortest_sentence": "",
            }
        report = self._readability_scorer.score(text)
        return {
            "flesch_score": report.flesch_score,
            "avg_sentence_length": report.avg_sentence_length,
            "avg_word_length": report.avg_word_length,
            "vocabulary_level": report.vocabulary_level,
            "sentence_count": report.sentence_count,
            "word_count": report.word_count,
            "longest_sentence": report.longest_sentence,
            "shortest_sentence": report.shortest_sentence,
        }

    def _handle_score_transcription(self, params: dict[str, Any]) -> dict[str, Any]:
        """Оценивает качество транскрибации и возвращает балл 0–100 с оценкой A–F.

        Params:
            text (str): транскрибированный текст.
            confidence (float): уверенность STT-модели, 0.0–1.0.
            duration_sec (float): длительность аудио в секундах.
            has_diarization (bool, optional): была ли применена диаризация. Default False.
            has_llm_enhancement (bool, optional): был ли применён LLM-рерайтер. Default False.

        Returns:
            Словарь с полями QualityScore: overall_score, grade, factors, recommendations.
        """
        text = params.get("text", "")
        confidence = float(params.get("confidence", 0.0))
        duration_sec = float(params.get("duration_sec", 0.0))
        has_diarization = bool(params.get("has_diarization", False))
        has_llm_enhancement = bool(params.get("has_llm_enhancement", False))

        result = self._transcription_scorer.score(
            text=text,
            confidence=confidence,
            duration_sec=duration_sec,
            has_diarization=has_diarization,
            has_llm_enhancement=has_llm_enhancement,
        )
        return {
            "overall_score": result.overall_score,
            "grade": result.grade,
            "factors": result.factors,
            "recommendations": result.recommendations,
        }

    def _handle_analyze_speech_pace(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует темп речи транскрибации.

        Params:
            text (str): транскрибированный текст.
            duration_sec (float): длительность аудиозаписи в секундах.

        Returns:
            Словарь с полями PaceReport: words_per_minute, chars_per_minute,
            pace_category, estimated_reading_time_sec, word_count, char_count, duration_sec.
        """
        text = params.get("text", "")
        duration_sec = float(params.get("duration_sec", 0.0))
        report = self._speech_pace_analyzer.analyze(text=text, duration_sec=duration_sec)
        return report.as_dict()

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

    def _handle_check_audio_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет, являются ли два аудио-сигнала дубликатами по фингерпринту.

        Параметры (params):
          - audio1: list[float] — первый аудио-сигнал (PCM float32).
          - audio2: list[float] — второй аудио-сигнал (PCM float32).
          - sample_rate: int — частота дискретизации (по умолчанию 16000).
          - threshold: float — порог сходства [0..1] (по умолчанию 0.95).

        Возвращает dict с ключами:
          fingerprint1, fingerprint2, similarity, is_duplicate.
        """
        audio1_raw = params.get("audio1")
        audio2_raw = params.get("audio2")
        if audio1_raw is None or audio2_raw is None:
            raise RuntimeError("audio1 и audio2 обязательны")

        sample_rate = int(params.get("sample_rate", 16000))
        threshold = float(params.get("threshold", 0.95))

        audio1 = np.asarray(audio1_raw, dtype=np.float32)
        audio2 = np.asarray(audio2_raw, dtype=np.float32)

        fp1 = self._audio_fingerprinter.fingerprint(audio1, sample_rate)
        fp2 = self._audio_fingerprinter.fingerprint(audio2, sample_rate)
        similarity = self._audio_fingerprinter.compare(fp1, fp2)

        return {
            "fingerprint1": fp1,
            "fingerprint2": fp2,
            "similarity": round(similarity, 6),
            "is_duplicate": similarity >= threshold,
        }

    # ── Hallucination patterns management ───────────────────────────────────

    def _handle_add_hallucination_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет пользовательский паттерн галлюцинации."""
        pattern = str(params.get("pattern", "")).strip()
        category = str(params.get("category", "custom")).strip() or "custom"
        if not pattern:
            raise RuntimeError("Параметр 'pattern' обязателен")
        entry = self._hallucination_manager.add_pattern(pattern, category=category)
        return {"added": entry}

    def _handle_remove_hallucination_pattern(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет пользовательский паттерн галлюцинации."""
        pattern = str(params.get("pattern", "")).strip()
        if not pattern:
            raise RuntimeError("Параметр 'pattern' обязателен")
        removed = self._hallucination_manager.remove_pattern(pattern)
        return {"removed": removed}

    def _handle_list_hallucination_patterns(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает все паттерны галлюцинаций (встроенные + пользовательские)."""
        patterns = self._hallucination_manager.list_patterns()
        return {"patterns": patterns, "total": len(patterns)}

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

    # ── Timeline export ──────────────────────────────────────────────────────

    def _handle_export_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует временную шкалу записей в SVG, JSON или iCalendar.

        Параметры:
          - format: str — формат экспорта: "svg", "json", "ical" (по умолчанию "json").
          - group_by: str — гранулярность блоков: "hour", "day", "week" (по умолчанию "day").
          - limit: int — макс. записей для анализа (по умолчанию 500, макс. 5000).
          - svg_width: int — ширина SVG в пикселях (по умолчанию 1200, только для format=svg).
          - svg_height: int — высота SVG в пикселях (по умолчанию 400, только для format=svg).

        Ответ:
          - content: str — экспортированный контент.
          - format: str — фактический формат экспорта.
          - total_blocks: int — количество блоков.
          - mime_type: str — MIME-тип контента.
        """
        fmt = str(params.get("format", "json")).strip().lower()
        if fmt not in ("svg", "json", "ical"):
            raise ValueError(
                f"Неизвестный формат экспорта: {fmt!r}. Допустимые: svg, json, ical"
            )

        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))

        raw_items = self.store._load_active_items_with_lock()[:limit]
        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        blocks_dicts = [b.to_dict() for b in blocks]

        if fmt == "svg":
            svg_width = max(200, int(params.get("svg_width", 1200)))
            svg_height = max(100, int(params.get("svg_height", 400)))
            content = self._timeline_exporter.export_svg(
                blocks_dicts, width=svg_width, height=svg_height
            )
            mime_type = "image/svg+xml"
        elif fmt == "ical":
            content = self._timeline_exporter.export_ical(blocks_dicts)
            mime_type = "text/calendar"
        else:
            content = self._timeline_exporter.export_json(blocks_dicts)
            mime_type = "application/json"

        return {
            "content": content,
            "format": fmt,
            "total_blocks": len(blocks_dicts),
            "mime_type": mime_type,
        }

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

    def _handle_extract_learning_vocabulary(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: extract_learning_vocabulary — извлечение словаря из двуязычных транскрипций."""
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_extract_learning_vocabulary(params_with_store)

    def _handle_generate_flashcards(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: generate_flashcards — генерация флеш-карточек для изучения языка."""
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_generate_flashcards(params_with_store)

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

    def _handle_anonymize_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: anonymize_text — редактирование персональных данных из транскрипции.

        Параметры:
            text  (str)       — исходный текст.
            rules (list[str]) — опциональный список имён правил (phone, email, credit_card и др.).
                                Если не указан, применяются все правила.

        Возвращает:
            anonymized_text, redactions, redaction_count
        """
        text = params.get("text", "")
        rules = params.get("rules")  # None → все правила
        if rules is not None and not isinstance(rules, list):
            raise ValueError("Параметр 'rules' должен быть списком строк или null")

        result = self._text_anonymizer.anonymize(text, rules=rules)
        return {
            "anonymized_text": result.anonymized_text,
            "redaction_count": result.redaction_count,
            "redactions": [
                {
                    "original": r.original,
                    "replacement": r.replacement,
                    "category": r.category,
                    "position": r.position,
                }
                for r in result.redactions
            ],
        }

    def _handle_detect_emotion(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: detect_emotion — эвристическое определение эмоции в тексте транскрипции.

        Параметры:
            text     (str) — исходный текст для анализа.
            language (str) — язык текста ("ru", "es", "en"). По умолчанию "ru".

        Возвращает:
            primary_emotion, confidence, indicators, exclamation_count,
            question_count, caps_ratio
        """
        text = str(params.get("text", ""))
        language = str(params.get("language", "ru"))
        result = self._emotion_detector.detect(text, language=language)
        return {
            "primary_emotion": result.primary_emotion,
            "confidence": result.confidence,
            "indicators": result.indicators,
            "exclamation_count": result.exclamation_count,
            "question_count": result.question_count,
            "caps_ratio": result.caps_ratio,
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

    def _handle_expand_abbreviations(self, params: dict) -> dict:
        """IPC: expand_abbreviations — раскрыть аббревиатуры в тексте транскрипции.

        Params:
            text (str): Исходный текст.
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"expanded": str, "changed": bool}
        """
        text = str(params.get("text", ""))
        language = str(params.get("language", "ru"))
        expanded = self._abbreviation_expander.expand(text, language=language)
        return {"expanded": expanded, "changed": expanded != text}

    def _handle_add_abbreviation(self, params: dict) -> dict:
        """IPC: add_abbreviation — добавить пользовательскую аббревиатуру.

        Params:
            abbr (str): Аббревиатура.
            expansion (str): Полная форма.
            language (str, optional): Код языка (по умолчанию "ru").
            flags (str, optional): Дополнительные флаги.

        Returns:
            {"ok": true}
        """
        abbr = str(params.get("abbr", "")).strip()
        expansion = str(params.get("expansion", "")).strip()
        language = str(params.get("language", "ru"))
        flags = str(params.get("flags", ""))
        if not abbr or not expansion:
            raise ValueError("abbr и expansion не должны быть пустыми")
        self._abbreviation_expander.add_abbreviation(abbr, expansion, language=language, flags=flags)
        return {"ok": True}

    def _handle_remove_abbreviation(self, params: dict) -> dict:
        """IPC: remove_abbreviation — удалить аббревиатуру.

        Params:
            abbr (str): Аббревиатура.
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"removed": bool}
        """
        abbr = str(params.get("abbr", "")).strip()
        language = str(params.get("language", "ru"))
        removed = self._abbreviation_expander.remove_abbreviation(abbr, language=language)
        return {"removed": removed}

    def _handle_list_abbreviations(self, params: dict) -> dict:
        """IPC: list_abbreviations — список аббревиатур для языка.

        Params:
            language (str, optional): Код языка (по умолчанию "ru").

        Returns:
            {"abbreviations": list[dict], "language": str, "count": int}
        """
        language = str(params.get("language", "ru"))
        abbreviations = self._abbreviation_expander.list_abbreviations(language=language)
        return {"abbreviations": abbreviations, "language": language, "count": len(abbreviations)}

    # ── Text post-processing IPC handlers ──────────────────────────────────────

    def _handle_post_process_text(self, params: dict) -> dict:
        """IPC: post_process_text — прогнать текст через конвейер пост-обработки.

        Params:
            text  (str)       — исходный текст для обработки.
            steps (list[str]) — список имён шагов в нужном порядке.
                                Если не указан, применяется цепочка по умолчанию:
                                [strip_whitespace, fix_punctuation, normalize_entities].

        Возвращает:
            text           — обработанный текст.
            steps_applied  — список имён выполненных шагов.
            changes_count  — число шагов, изменивших текст.
        """
        text = str(params.get("text", ""))
        steps = params.get("steps")  # None → цепочка по умолчанию
        if steps is not None and not isinstance(steps, list):
            raise ValueError("Параметр 'steps' должен быть списком строк или null")
        if steps is not None:
            steps = [str(s) for s in steps]

        result = self._text_postprocessor.process(text, steps=steps)
        return {
            "text": result.text,
            "steps_applied": result.steps_applied,
            "changes_count": result.changes_count,
        }

    def _handle_list_post_process_steps(self, params: dict) -> dict:
        """IPC: list_post_process_steps — список доступных шагов пост-обработки.

        Возвращает:
            steps — список имён доступных шагов.
        """
        return {"steps": self._text_postprocessor.list_steps()}

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

    def _handle_auto_update_vocabulary(self, params: dict) -> dict:
        """IPC: auto_update_vocabulary — полный цикл умного авто-обновления словаря STT."""
        min_frequency = max(1, int(params.get("min_frequency", 3) or 3))
        scan_limit = max(10, min(int(params.get("scan_limit", 200) or 200), 500))
        update = self._smart_vocabulary.auto_update(
            store=self.store,
            vocabulary_store=self.vocabulary,
            min_frequency=min_frequency,
            scan_limit=scan_limit,
        )
        return {
            "new_words": update.new_words,
            "removed_words": update.removed_words,
            "total": update.total,
            "sources": update.sources,
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

    # ------------------------------------------------------------------ PR 1.5

    def _handle_get_wake_word_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущую конфигурацию wake word.

        Проверяет наличие:
          - AccessKey (env var KRAB_EAR_PORCUPINE_ACCESS_KEY или файл в DATA_DIR)
          - .ppn файла «Краб» в ~/Library/Application Support/KrabEar/

        Returns:
            dict с полями:
              wake_word_enabled (bool): текущее значение из settings.
              access_key_present (bool): AccessKey найден.
              ppn_present (bool): .ppn файл найден.
              ppn_path (str | None): путь к .ppn если найден.
              engine_preference (str): "auto" | "moshi" | "seamless".
              brain_preference (str): "auto" | "qwen3-30b" | "qwen3-4b".
        """
        import os
        from pathlib import Path

        current_settings = self._settings_svc.cached_settings()
        wake_word_enabled = current_settings.get("wake_word_enabled", False)
        engine_pref = current_settings.get("conversation_engine", "auto")
        brain_pref = current_settings.get("conversation_brain", "auto")

        # Проверить наличие AccessKey
        access_key = (
            os.environ.get("KRAB_EAR_PORCUPINE_ACCESS_KEY", "")
            or settings.PORCUPINE_ACCESS_KEY
        )
        key_file_path = Path(settings.DATA_DIR) / "porcupine_access_key"
        if not access_key and key_file_path.exists():
            access_key = key_file_path.read_text(encoding="utf-8").strip()
        access_key_present = bool(access_key)

        # Проверить наличие .ppn файла
        ppn_candidates = [
            Path.home() / "Library" / "Application Support" / "KrabEar" / "Краб_ru_mac_v3_0_0.ppn",
            Path.home() / "Library" / "Application Support" / "KrabEar" / "Krab_ru_mac.ppn",
            Path(settings.DATA_DIR) / "Краб_ru_mac_v3_0_0.ppn",
        ]
        ppn_path = next((str(p) for p in ppn_candidates if p.exists()), None)

        return {
            "wake_word_enabled": wake_word_enabled,
            "access_key_present": access_key_present,
            "ppn_present": ppn_path is not None,
            "ppn_path": ppn_path,
            "engine_preference": engine_pref,
            "brain_preference": brain_pref,
        }

    def _handle_set_wake_word_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Обновить конфигурацию wake word и разговора с AI.

        Параметры:
            wake_word_enabled (bool, optional): включить/выключить wake word.
            conversation_engine (str, optional): "auto" | "moshi" | "seamless".
            conversation_brain (str, optional): "auto" | "qwen3-30b" | "qwen3-4b".

        Returns:
            dict: updated — количество обновлённых полей.
        """
        allowed_fields = {"wake_word_enabled", "conversation_engine", "conversation_brain"}
        patch: dict[str, Any] = {}

        if "wake_word_enabled" in params:
            enabled = bool(params["wake_word_enabled"])
            patch["wake_word_enabled"] = enabled

        engine = params.get("conversation_engine")
        if engine in ("auto", "moshi", "seamless"):
            patch["conversation_engine"] = engine

        brain = params.get("conversation_brain")
        if brain in ("auto", "qwen3-30b", "qwen3-4b"):
            patch["conversation_brain"] = brain

        if patch:
            # handle_set_settings принимает patch (только изменённые поля)
            self._settings_svc.handle_set_settings(patch)

        return {"updated": len(patch), "fields": list(patch.keys())}


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
        server.bind(str(self.socket_path))
        os.chmod(str(self.socket_path), 0o600)
        server.listen(32)
        server.settimeout(0.8)

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
                raw = conn.recv(1024 * 1024)
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
        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return _json.dumps({
                    "ts": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                })
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
    service = build_service(data_dir)
    server = IPCServer(socket_path=socket_path, service=service)

    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Получен сигнал %s, завершаем backend", signum)
        server.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    server.serve_forever()


if __name__ == "__main__":
    main()
