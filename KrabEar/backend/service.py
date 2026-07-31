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
from backend.model_downloader import ModelDownloader
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
from backend.period_comparison import PeriodComparisonService
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
from backend.event_bridge import EventBridge
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
from backend.analytics_service import AnalyticsService
from backend.apple_integration_service import AppleIntegrationService
from backend.llm_ops_service import LLMOpsService
from backend.search_and_analysis_service import SearchAndAnalysisService
from backend.meeting_session_service import MeetingSessionService
from backend.stt_management_service import STTManagementService
from backend.text_scoring_service import TextScoringService
from backend.call_session_service import CallSessionService
from backend.recording_core_service import RecordingCoreService
from backend.audio_selfheal import AudioSelfHealer
from backend.audio_reinit import AudioReinitCoordinator
from backend.wake_word_watchdog import WakeWordWatchdog
from backend.text_processing_service import TextProcessingService
from backend.call_session_store import CallSessionStore
from backend.live_subs_service import LiveSubsService
from backend.tts_service import TTSService
from backend.request_signing import RequestSigner
from backend.ipc_throttle import IPCThrottle
# W1768 (W746-класс): production-вход main() обязан использовать ЗАКАЛЁННЫЙ
# IPCServer из ipc_server.py (W1767 #1595: per-conn recv-таймаут 30s,
# BoundedSemaphore(64) slow-loris guard, _recv_until_newline reassembly,
# bind()-fd-leak fix). Ранее service.py содержал ДУБЛИКАТ inline-класса —
# production запускал старую незакалённую копию, и HIGH-фикс был мёртвым.
# Константы IPC_SOCKET_* / IPC_MAX_MESSAGE_BYTES теперь нужны только внутри
# ipc_server.py — здесь импорт удалён (дубликат класса убран).
from backend.ipc_server import IPCServer
from backend.text_snippet_service import TextSnippetService
from backend.phonetic_vocab_service import PhoneticVocabService
from backend.export_scheduler import ExportScheduler
from backend.call_cost_estimator import CallCostEstimator
from backend.call_auto_end import CallAutoEnd
from backend.shutdown_handler import GracefulShutdownHandler
from backend.auto_backup import AutoBackupManager, AUTO_BACKUP_INTERVAL_HOURS, AUTO_BACKUP_MAX_COPIES
from backend.email_sender import EmailSender
from backend.recap_scheduler import RecapScheduler
from backend.purge_scheduler import PurgeScheduler
from backend.paste_app_memory import PasteAppMemory
from backend.telegram_bridge import TelegramBridge
from backend.disk_monitor import DiskSpaceMonitor
from backend.observability import (
    _BREADCRUMB_EXCLUDED_METHODS,
    add_breadcrumb,
    flush_sentry,
    get_release_string,
    init_sentry,
    install_signal_handlers,
)
from backend.calendar_link import CalendarLinker
from backend.audit_logger import AuditLogger
from backend.bulk_reprocess import BulkReprocessor
from backend.privacy_audit import get_privacy_audit_logger
from backend.ipc_errors import IpcOperationalError
import backend.cloud_stt as cloud_stt
import backend.cloud_rewriter as cloud_rewriter

import argparse
from datetime import datetime, timedelta, timezone
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

# Обеспечиваем корректный импорт модулей KrabEar при запуске как standalone скрипта.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger("KrabEar.Backend.Service")


def _exit_without_python_finalize_if_worker_hung(
    workers_stopped: bool | None,
    *,
    exit_fn: Callable[[int], None] | None = None,
    flush_fn: Callable[[], None] | None = None,
) -> None:
    """Завершить backend без `_Py_Finalize` при недоказанном барьере.

    Литеральный ``False`` может прийти от IPC-handler-а или native/audio
    worker-а. Обычная финализация в обоих случаях способна закрыть общий ресурс
    под живым потоком, поэтому launchd должен поднять чистый процесс.
    """
    # Hard-exit допустим только по ЯВНОМУ False от нового close-контракта.
    # Legacy/test double может вернуть None; считать его доказанным CFFI-клином
    # нельзя, иначе обычный тест main() или старый embedder внезапно завершится 70.
    if workers_stopped is not False:
        return
    logger.critical(
        "Shutdown-барьер не подтверждён: завершаем без Python-finalize, "
        "чтобы launchd поднял чистый экземпляр"
    )
    # Приёмочное ревью 2026-07-23 (F2): logger.critical порождает Sentry-событие
    # через LoggingIntegration, а os._exit не даёт ни второго flush, ни atexit —
    # без flush ИМЕННО ЗДЕСЬ причина аварийного выхода терялась навсегда.
    if flush_fn is not None:
        try:
            flush_fn()
        except Exception:
            logger.exception("flush telemetry перед hard-exit упал")
    (exit_fn or os._exit)(os.EX_SOFTWARE)


def _exit_without_python_finalize_if_wake_word_hung(
    wake_word_stopped: bool | None,
    *,
    exit_fn: Callable[[int], None] | None = None,
) -> None:
    """Совместимый alias старого имени для тестов и внешних embedder-ов."""
    _exit_without_python_finalize_if_worker_hung(
        wake_word_stopped,
        exit_fn=exit_fn,
    )


# Бюджет дренажа IPC-handler'ов при shutdown. Должен покрывать типовой
# синхронный STT-запрос в handler-потоке и укладываться в ExitTimeOut=15
# вместе с service.close() и записью metadata.
_IPC_DRAIN_BUDGET_SEC = 8.0


def _shutdown_backend(
    service: Any,
    server: IPCServer,
    shutdown_handler: GracefulShutdownHandler,
    *,
    flush_fn: Callable[[], None] = flush_sentry,
    exit_fn: Callable[[int], None] | None = None,
) -> bool:
    """Единожды выполнить shutdown в порядке IPC → workers → metadata.

    Явный ``False`` любого ownership-барьера запрещает переход к следующим
    ресурсам. Перед аварийным выходом telemetry сбрасывается, а при тестовой
    инъекции возвращается ``False`` без продолжения teardown.
    """
    try:
        # F1 (приёмочное ревью 2026-07-23): STT-пайплайн выполняется В
        # handler-потоке (handle_stop_recording/meeting_stop/transcribe_paths),
        # поэтому дефолтных 1.5 с не хватает — координатор объявлял барьер
        # недоказанным и делал os._exit ДО close()/metadata, теряя словарь,
        # usage, playback, компактирование и shutdown_info.json.
        # 8 с укладываются в ExitTimeOut=15 вместе с close() и metadata.
        ipc_quiesced = server.stop(timeout_sec=_IPC_DRAIN_BUDGET_SEC)
    except Exception:
        logger.exception("IPCServer.stop() выбросил исключение при shutdown")
        ipc_quiesced = False
    if ipc_quiesced is False:
        _exit_without_python_finalize_if_worker_hung(
            False, exit_fn=exit_fn, flush_fn=flush_fn
        )
        return False

    try:
        workers_stopped = service.close()
    except Exception:
        logger.exception("BackendService.close() выбросил исключение при shutdown")
        workers_stopped = False
    if workers_stopped is False:
        _exit_without_python_finalize_if_worker_hung(
            False, exit_fn=exit_fn, flush_fn=flush_fn
        )
        return False

    try:
        metadata_safe = shutdown_handler.shutdown(ipc_already_stopped=True)
    except Exception:
        logger.exception("Metadata shutdown выбросил исключение")
        metadata_safe = False
    flush_fn()
    if metadata_safe is False:
        _exit_without_python_finalize_if_worker_hung(False, exit_fn=exit_fn)
        return False
    return True


# wave1775: which EventBus events are forwarded to registered webhooks.
# Kept deliberately narrow — only genuinely-meaningful, low-frequency lifecycle
# events, NEVER high-frequency ones (recording.audio_level @ ~30 Hz,
# stt.partial, realtime.partial_transcript) which would flood external endpoints.
# To forward more events: add the type string here AND scrub any new PII fields
# in BackendService._webhook_safe_payload().
_WEBHOOK_FORWARDED_EVENTS: frozenset[str] = frozenset({
    "stt.final",                # транскрибация завершена (EventType.STT_FINAL)
    "translation.completed",    # перевод завершён (EventType.TRANSLATION_COMPLETED)
})

# Payload keys that carry user content (transcript / translation text) and must be
# stripped before a webhook payload leaves the device.  Webhook consumers receive
# only metadata (history_id, duration, language, confidence, …) by default.
_WEBHOOK_PII_KEYS: frozenset[str] = frozenset({
    "text", "translated_text", "source_text", "original_text", "segments",
})


class _RestInProcessTombstone:
    """Надгробие: сборка REST-приложения упала, лечить нечего.

    S3/Задача 4. До этого класса внешний ``except`` вокруг подъёма
    in-process REST при сбое сборки (импорт, ``adopt_external_singletons``,
    ``create_app()``, конструктор ``InProcessRestServer``) ставил
    ``self._rest_inprocess = None`` — а диагностика на ``None`` отдаёт
    словарь, байт в байт совпадающий с «рубильник выключен». Для
    двухнедельной канарейки это ложноотрицательный сигнал: владелец видел бы
    штатную картину при мёртвом REST.

    Существует, чтобы диагностика отличала «рубильник выключен» от «включён,
    но не поднялся». Поле ``tombstone`` — не косметика: сторож REST
    (отдельная задача этой же волны) обязан по нему отличать нелечимое,
    иначе будет вечно лечить надгробие и выдаст эскалацию поверх уже
    отправленной ошибки — двойная тревога об одном отказе.
    """

    def __init__(self, *, enabled: bool, port: int, error: str) -> None:
        self._status = {
            "enabled": enabled,
            "running": False,
            "port": port,
            "error": error,
            "tombstone": True,
        }

    def status(self) -> dict:
        return dict(self._status)

    def stop(self, timeout: float | None = None) -> None:
        """Останавливать нечего — метод есть ради единообразия с close()."""


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
        self._text_snippet_svc = TextSnippetService(data_dir=store.data_dir)
        self._phonetic_vocab_svc = PhoneticVocabService(data_dir=store.data_dir)

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
        # Wire snippet provider into engine so TextSnippetExpander in engine.py
        # can access the current snippet list at transcription time (late-injection
        # pattern, mirrors _llm_rewriter wiring above).
        if hasattr(self.transcriber, "engine"):
            self.transcriber.engine._snippets_provider = self._text_snippet_svc.get_snippets
        # Wire phonetic vocab provider into engine so PhoneticVocabulary in engine.py
        # can access current entries at transcription time (late-injection pattern,
        # mirrors _snippets_provider wiring above).
        if hasattr(self.transcriber, "engine"):
            self.transcriber.engine._phonetic_provider = self._phonetic_vocab_svc.get_entries

        self.translator = translator or Translator()
        # W1429 — wire persistent translation cache (late-injection pattern)
        self._translation_cache = TranslationCache(data_dir=str(store.data_dir))
        self.translator._translation_cache = self._translation_cache
        # W1500 — wire runtime settings getter for privacy-mode detection
        self.translator._settings_getter = self._get_runtime_setting
        # W1755 — wire runtime settings getter to LLMRewriter for privacy-mode detection in
        # fix_punctuation_only() / summarize().  Previously _settings_getter was initialized to
        # None and NEVER assigned on the rewriter instance (only the translator was wired at
        # W1500), making the privacy guard dead: with privacy_mode_enabled=True the rewriter
        # still POSTed transcript text to LM Studio.  Mirror the translator pattern exactly.
        if self._llm_rewriter is not None:
            self._llm_rewriter._settings_getter = self._get_runtime_setting
        self._start_time: float = time.monotonic()
        self._settings_svc = SettingsService(store=self.store)
        # S3/Задача 2: cloud_stt/cloud_rewriter раньше строили СОБСТВЕННЫЙ
        # StateStore(settings.DATA_DIR) — после выравнивания каталога данных
        # (S3/Задача 1) это те же файлы, что у self.store, а per-thread
        # depth-counter реентерабельности (#1872) живёт в поле экземпляра и
        # между двумя StateStore не защищает — лок-мина. Подключаем оба модуля
        # к аксессору владельца процесса. БЕЗУСЛОВНО и ВНЕ блока in-process
        # REST: cloud_rewriter зовётся из engine.py уже сегодня, а этот вызов
        # не должен зависеть от рубильника REST_IN_PROCESS_ENABLED.
        cloud_stt.adopt_settings_reader(self._cached_settings)
        cloud_rewriter.adopt_settings_reader(self._cached_settings)
        # Hot-propagate api_key changes to the running LLMRewriter without restart.
        _rewriter_ref = self._llm_rewriter
        if _rewriter_ref is not None:
            def _on_settings_saved(old: dict, new: dict) -> None:
                new_key = str(new.get("lm_studio_api_key", ""))
                if new_key != str(old.get("lm_studio_api_key", "")):
                    _rewriter_ref.set_api_key(new_key)
                # Hot-swap model when user changes it via GUI dropdown (llm_model setting).
                # Previously the rewriter was initialized once from the static config and
                # never updated — GUI dropdown changes were silently ignored.
                new_model = str(new.get("llm_model", "")).strip()
                old_model = str(old.get("llm_model", "")).strip()
                if new_model and new_model != old_model:
                    logger.info(
                        "LLM rewriter: hot-swap model %r → %r (settings change)",
                        old_model, new_model,
                    )
                    _rewriter_ref.set_model(new_model)
            self._settings_svc.register_after_save_hook(_on_settings_saved)

        # W1603 / W1599 F2 MED: Re-initialize Sentry when privacy_mode toggles OFF.
        # W1601 handled the ON path (clears _sentry_initialized).  This hook covers
        # the inverse: when the user disables privacy_mode at runtime, Sentry must be
        # re-initialized from the current sentry_dsn so crash reporting resumes.
        def _on_privacy_mode_off(old: dict, new: dict) -> None:
            old_privacy = bool(old.get("privacy_mode_enabled", False))
            new_privacy = bool(new.get("privacy_mode_enabled", False))
            if old_privacy and not new_privacy:
                # Privacy mode just toggled OFF — re-init Sentry if DSN is configured.
                from backend.observability import init_sentry, is_sentry_initialized  # noqa: PLC0415
                if not is_sentry_initialized():
                    dsn = str(new.get("sentry_dsn", "")).strip()
                    if dsn:
                        init_sentry(
                            dsn=dsn,
                            settings=new,
                        )
                        logger.info(
                            "observability: Sentry re-initialized after privacy_mode disabled"
                        )
        self._settings_svc.register_after_save_hook(_on_privacy_mode_off)

        # W1265 F1 MED / W1340: Evict AudioLanguageID._model_cache when MODEL_BALANCED
        # changes so the first recording after an STT profile switch does NOT block the
        # STT pipeline thread with a cold-load stall inside mlx_lock().
        #
        # W1334 F2 HIGH (case-mismatch fix): settings.json uses pydantic field names
        # (uppercase "MODEL_BALANCED") while DEFAULT_SETTINGS uses lowercase
        # "model_balanced"; both keys must be checked so the comparison never silently
        # short-circuits to "" == "" and skips eviction.
        def _get_model_balanced(d: dict) -> str:
            """Case-tolerant helper: checks MODEL_BALANCED, model_balanced, stt_model_balanced."""
            for k in ("MODEL_BALANCED", "model_balanced", "stt_model_balanced"):
                v = d.get(k)
                if v is not None:
                    return str(v)
            return ""

        def _on_settings_saved_lang_id(old: dict, new: dict) -> None:
            old_model = _get_model_balanced(old)
            new_model = _get_model_balanced(new)
            if new_model != old_model:
                try:
                    from core.audio_lang_id import AudioLanguageID
                    AudioLanguageID.clear_model_cache()
                    logger.info(
                        "AudioLanguageID cache evicted: model_balanced changed %s → %s",
                        old_model or "(empty)",
                        new_model or "(empty)",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "AudioLanguageID cache evict failed: %s", exc
                    )
        self._settings_svc.register_after_save_hook(_on_settings_saved_lang_id)

        # W1755 — propagate runtime hf_token/stt_gigaam_hf_token to os.environ so that
        # pyannote diarization finds HF_TOKEN at startup.  A GUI token change (set_settings)
        # also re-runs propagation via the after_save hook below so no restart is needed.
        # overwrite=True at init: the GUI/canonical token is authoritative and must beat a
        # STALE token baked into the launchd plist's EnvironmentVariables at install time —
        # a revoked plist HF_TOKEN otherwise wins via setdefault → pyannote 401. When no GUI
        # token exists the method returns early, so a deliberate env override is preserved.
        self._propagate_hf_token_to_env(overwrite=True)

        def _on_hf_token_saved(old: dict, new: dict) -> None:
            _changed = (
                new.get("hf_token", "") != old.get("hf_token", "")
                or new.get("stt_gigaam_hf_token", "") != old.get("stt_gigaam_hf_token", "")
            )
            if _changed:
                # overwrite=True: GUI-токен изменился — перезаписываем env и инвалидируем pipeline
                self._propagate_hf_token_to_env(overwrite=True)
        self._settings_svc.register_after_save_hook(_on_hf_token_saved)

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
            # W1688 (W1686 F4 fix): also wire into the underlying AudioEngine so
            # _transcribe_gigaam() can forward it to GigaAMAdapter / subprocess session.
            # Without this, engine._error_bus stays None and GigaAM worker
            # timeout/crash errors disappear silently.
            if getattr(self.transcriber, "engine", None) is not None:
                self.transcriber.engine._error_bus = self._error_bus

        # Wire error_bus into recorder so audio.max_duration_reached /
        # audio.buffer_overflow errors are forwarded to the error bus (W1652 F1 fix).
        if self.recorder is not None:
            self.recorder._error_bus = self._error_bus

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
        self._collections = CollectionManager(store=self.store, settings_fn=self._cached_settings)
        self._norm_profiles = NormalizationProfileRegistry(data_dir=self.store.data_dir)
        self._chains = RecordingChainManager(store=self.store, settings_fn=self._cached_settings)
        self._bookmarks = BookmarkManager(
            data_dir=self.store.data_dir,
            settings_get=self._get_runtime_setting,  # wave-1770: privacy gate wiring
        )
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
            settings_get=self._get_runtime_setting,
        )
        self._call_cost_estimator = CallCostEstimator()
        # NB (W1775): CallAutoEnd — advisory-проверка по таймеру/скалярам, она НЕ
        # инспектирует аудио, поэтому больше НЕ принимает silence_probe (декоративное
        # поле было удалено). Прежний `self._call_silence_probe` существовал ТОЛЬКО
        # ради этого удалённого аргумента и нигде больше не читался — поэтому он
        # тоже убран, чтобы не плодить новое декоративное поле. CallSilenceProbe
        # (анализ PCM) остаётся как отдельный класс и может быть подключён, когда
        # появится реальный потребитель аудио-подтверждения тишины.
        self._call_auto_end = CallAutoEnd(
            cost_estimator=self._call_cost_estimator,
        )
        self._tts = TTSService()
        self._live_subs = LiveSubsService(
            transcriber=self.transcriber,
            translator=self.translator,
            settings_get=self._get_runtime_setting,
        )
        self._translation = TranslationService(
            translator=self.translator,
            store=self.store,
            cached_settings=self._cached_settings,
            invalidate_settings_cache=self._invalidate_settings_cache,
            vocabulary_store=self.vocabulary,
            settings_svc=self._settings_svc,  # W1767: атомарный glossary RMW под _save_lock
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
        self._error_reporter = ErrorReporter(
            settings_provider=self._settings_svc.cached_settings,
        )
        # W1687 F3 HIGH: wire settings_provider so privacy-mode redaction in
        # get_error_report is honoured at runtime (was silently skipped before).
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
            settings_fn=self._settings_svc.cached_settings,  # wave-1770 HIGH: privacy gate
        )
        self._export_scheduler = ExportScheduler(
            data_dir=self.store.data_dir,
            settings_provider=self._settings_svc.cached_settings,
        )
        # W1687 F6 MED: wire settings_provider so privacy-mode guard in
        # check_and_export() and runtime schedule changes are honoured.
        # W982: Wire periodic worker thread for ExportScheduler (F1 fix).
        # check_and_export() is a no-op when disabled — cheap to call every 5 min.
        self._export_scheduler_stop = threading.Event()
        self._export_scheduler_thread = threading.Thread(
            target=self._export_scheduler_loop,
            daemon=True,
            name="export-scheduler",
        )
        self._export_scheduler_thread.start()
        # Note: _transcription_counter is now a property that proxies to
        # _transcription_counter_ref[0] (set below after RecordingCoreService init).
        self._analytics_dashboard = AnalyticsDashboard()
        self._daily_digest = DailyDigestGenerator()
        self._period_comparison_svc = PeriodComparisonService(store=self.store)
        # Recap email scheduler (opt-in via RECAP_EMAIL_ENABLED setting)
        self._recap_scheduler = RecapScheduler(
            email_sender=EmailSender.from_settings(settings),
            digest_generator=self._daily_digest,
            store=self.store,
            data_dir=self.store.data_dir,
            recap_email_to=settings.RECAP_EMAIL_TO,
            recap_time_hour=settings.RECAP_TIME_HOUR,
            enabled=settings.RECAP_EMAIL_ENABLED,
            settings_provider=self._settings_svc.cached_settings,
        )
        # W1687 F5 MED: wire settings_provider so runtime changes to
        # recap_enabled / recap_time_hour are picked up on each scheduler tick.
        if settings.RECAP_EMAIL_ENABLED:
            self._recap_scheduler.start()
        # W1771 MED: start RecapScheduler when recap_email_enabled toggled on at runtime.
        # Without this hook, enabling the digest via set_settings({recap_email_enabled: True})
        # persists the setting but the daemon thread is never started (the init-time guard
        # above only fires once), so emails are silently never sent until a backend restart.
        # RecapScheduler.start() is idempotent (is_alive guard inside).

        def _on_recap_enabled(old: dict, new: dict) -> None:
            old_enabled = bool(old.get("recap_email_enabled", False))
            new_enabled = bool(new.get("recap_email_enabled", False))
            if not old_enabled and new_enabled:
                self._recap_scheduler.start()
        self._settings_svc.register_after_save_hook(_on_recap_enabled)

        # Scheduled auto-purge: periodically delete history entries older than
        # auto_purge_retention_days days when auto_purge_enabled=True.
        # purge_fn delegates directly to HistoryService.cleanup_old_history_days
        # so both the IPC handler and the scheduler share one implementation.
        self._purge_scheduler = PurgeScheduler(
            settings_get=self._get_runtime_setting,
            purge_fn=self._history.cleanup_old_history_days,
        )
        self._purge_scheduler.start()

        self._quality_trends = QualityTrendAnalyzer()
        self._activity_calendar = ActivityCalendar()
        self._stats_report = StatsReportGenerator()
        self._speaker_statistics = SpeakerStatisticsAnalyzer(
            settings_get=self._get_runtime_setting,
        )
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
            settings_provider=self._settings_svc.cached_settings,
        )
        # W1687 F2 HIGH / W23 MED: wire settings_provider so privacy-mode redaction
        # in get_event_log / replay_events / get_event_stats is honoured at runtime.
        # W23 made this TRUE on the READ path too: get_events/replay_events/
        # get_event_stats now redact payloads whenever privacy_mode is active,
        # so cleartext recorded before privacy was enabled is never returned cleartext.
        # W1677 F1 HIGH: wire late-injection so EventBus.emit() actually records
        # to the replay ring-buffer. Without this, _event_replay stays None and
        # get_event_log / get_event_stats / replay_events always return empty.
        event_bus._event_replay = self._event_replay
        # W23 MED (defense-in-depth): when privacy_mode flips OFF->ON at runtime,
        # WIPE the existing cleartext ring buffer + event_replay.ndjson immediately.
        # Read-path redaction (above) already prevents leaks WHILE privacy is on; this
        # hook additionally destroys the pre-privacy cleartext so it can never resurface
        # if privacy is later toggled back OFF. Mirrors _on_privacy_mode_webhooks.

        def _on_privacy_mode_wipe_event_replay(old: dict, new: dict) -> None:
            old_privacy = bool(old.get("privacy_mode_enabled", False))
            new_privacy = bool(new.get("privacy_mode_enabled", False))
            if not old_privacy and new_privacy:
                try:
                    self._event_replay.clear()
                except Exception:
                    logger.exception(
                        "wave23: failed to wipe event_replay on privacy_mode enable"
                    )
        self._settings_svc.register_after_save_hook(_on_privacy_mode_wipe_event_replay)
        self._webhook_manager = WebhookManager(data_dir=self.store.data_dir)
        # wave1775: wire fire_webhook into the EventBus so REGISTERED webhooks actually
        # FIRE.  Before this, register/list/unregister IPC handlers were wired but
        # fire_webhook had ZERO production callers — users could register a webhook but
        # nothing ever delivered to it (shipped-but-dead feature).
        #
        # Design:
        #  - We register a single listener (_forward_event_to_webhooks) on the global bus.
        #    The bus invokes it inline inside emit() for every event; fire_webhook returns
        #    immediately (it submits to its own bounded ThreadPoolExecutor + retry path),
        #    so the hot recording/STT thread is never blocked on network I/O.
        #  - The forwarder only forwards genuinely-meaningful lifecycle events
        #    (_WEBHOOK_FORWARDED_EVENTS) and strips transcript/translation text so the
        #    default payload is PII-safe metadata only (history_id, duration, language,
        #    confidence).  fire_webhook still applies per-webhook event filtering + the
        #    SSRF-pinned, retrying delivery path.
        #  - Privacy: WebhookManager has its own privacy gate (set_privacy_mode); we sync
        #    it below from privacy_mode_enabled at startup and on every settings save.
        #    Note STT_FINAL is additionally suppressed at its emit site in privacy mode
        #    (recording_core_service), so this is defence-in-depth.
        # Extension point: to forward more events, add their type strings to
        # _WEBHOOK_FORWARDED_EVENTS and extend _webhook_safe_payload() to scrub any new
        # PII fields they carry.
        try:
            event_bus.add_listener(self._forward_event_to_webhooks)
        except Exception:
            logger.exception("wave1775: failed to wire webhook EventBus listener")
        # Sync the webhook privacy gate with the current persisted setting at startup,
        # then keep it in sync via an after_save hook (mirrors _on_privacy_mode_off).
        try:
            self._webhook_manager.set_privacy_mode(
                bool(self._get_runtime_setting("privacy_mode_enabled", False))
            )
        except Exception:
            logger.exception("wave1775: failed to set initial webhook privacy_mode")

        def _on_privacy_mode_webhooks(old: dict, new: dict) -> None:
            old_privacy = bool(old.get("privacy_mode_enabled", False))
            new_privacy = bool(new.get("privacy_mode_enabled", False))
            if old_privacy != new_privacy:
                # Suppress (or resume) webhook delivery to match privacy mode.
                self._webhook_manager.set_privacy_mode(new_privacy)
        self._settings_svc.register_after_save_hook(_on_privacy_mode_webhooks)

        # Event-мост IPC -> REST (spec 2026-07-07-event-bridge-design.md): доставляет
        # события ЛОКАЛЬНОЙ (IPC-процесса) шины в REST-процесс, откуда их уже
        # раздают существующие SSE/WS подписчики. Закрывает класс багов
        # "эмитится в IPC, слушается REST" (wake word/krab_error чинились
        # IPC-поллингом; rewriter_recovered/live_subs агентским путём — нет,
        # см. Задача 1 плана волны).
        self._event_bridge = EventBridge(settings=settings, data_dir=self.store.data_dir)
        try:
            event_bus.add_listener(self._event_bridge.on_event)
        except Exception:
            logger.exception("event-bridge: failed to wire EventBus listener")
        self._event_bridge.start()

        # M2: REST внутри этого же процесса (спека 2026-07-16 §4.2). Рубильник
        # по умолчанию выключен — прод продолжает работать на двух процессах,
        # пока владелец не решит иначе. Мост событий выше сам выключился по
        # тому же рубильнику, поэтому echo в общей шине невозможен.
        #
        # Зависимости ПОДМЕНЯЮТСЯ, а не создаются заново: импорт rest_server
        # строит свой standalone-комплект (так работают 20 тест-файлов, которые
        # патчат этот модуль), и без подмены в одном процессе жили бы два
        # AudioEngine/StateStore — ровно тот дубль, ради устранения которого
        # затевалась серия M. Прежний комплект становится недостижим и уходит
        # в сборщик мусора.
        self._rest_inprocess = None
        _rest_enabled = bool(getattr(settings, "REST_IN_PROCESS_ENABLED", False))
        if _rest_enabled:
            try:
                import backend.rest_server as _rest_module
                from backend.rest_inprocess import InProcessRestServer

                _rest_module.adopt_external_singletons(
                    engine=self.transcriber.engine,
                    store=self.store,
                    transcriber=self.transcriber,
                    translator=self.translator,
                    tts_service=self._tts,
                )
                self._rest_inprocess = InProcessRestServer(
                    app=_rest_module.create_app(),
                    settings=settings,
                    enabled=_rest_enabled,
                    error_push=self._push_rest_error,
                )
                self._rest_inprocess.start()
            except Exception as exc:
                # S3/Задача 4: это сбой СБОРКИ приложения (импорт,
                # adopt_external_singletons, create_app(), сам конструктор) —
                # не то же самое, что штатный fail-open внутри start() (тот
                # документирован как "НИКОГДА не бросает" и сам обрабатывает
                # EADDRINUSE). Раньше здесь стояло self._rest_inprocess = None,
                # а диагностика на None отдаёт словарь, байт в байт совпадающий
                # с «рубильник выключен» — канарейка две недели видела бы
                # штатную картину при мёртвом REST. Надгробие делает «включён,
                # но не поднялся» отличимым состоянием, а не только строкой в
                # логе; error_push доводит это же состояние до ErrorBus.
                logger.exception("in-process REST: не удалось поднять")
                _detail = f"{type(exc).__name__}: {exc}"
                self._rest_inprocess = _RestInProcessTombstone(
                    enabled=_rest_enabled,
                    port=int(getattr(settings, "REST_SERVER_PORT", 5005)),
                    error=_detail,
                )
                self._push_rest_error("rest.startup_failed", _detail)

        self._sharing = SharingManager(
            store=self.store,
            privacy_mode_fn=lambda: self._get_runtime_setting("privacy_mode_enabled", False),
        )
        # wave-33 A1: wire SharingManager into HistoryService so handle_purge_all_data
        # can clear SharingManager._index — a RAM copy of share packages holding full
        # transcript text (content/text/translated_text) that survives the rmtree(shares/)
        # step and keeps serving data via get_shared/list_shared.
        self._history._sharing_manager = self._sharing
        self._merger = RecordingMerger(privacy_mode_fn=lambda: self._get_runtime_setting('privacy_mode_enabled', False))
        self._transcript_versioning = TranscriptVersionManager(data_dir=self.store.data_dir, settings_fn=self._cached_settings)
        # BulkReprocessor — массовое перетранскрибирование истории (W1037 F4 / W1044 re-wire)
        # wave-25 HIGH: wire is_recording_fn so the anti-SIGSEGV guard is live.  Without it the
        # guard was dead (always False) and a bulk MLX reprocess could start mid-recording →
        # concurrent Metal GPU access → SIGSEGV (PR #71 class crash).  recorder.is_recording is a
        # property; the lambda re-reads it on each call so a recording that starts AFTER the
        # constructor is still observed.
        self._bulk_reprocessor = BulkReprocessor(
            store=self.store,
            transcriber=self.transcriber,
            version_manager=self._transcript_versioning,
            event_bus=event_bus,
            is_recording_fn=lambda: bool(getattr(self.recorder, "is_recording", False)),
        )
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
        self._text_postprocessor = TextPostProcessor()
        self._transcription_queue = TranscriptionQueue(
            privacy_mode_fn=lambda: self._get_runtime_setting("privacy_mode_enabled", False),
        )
        self._emotion_detector = EmotionDetector()
        self._sentiment_trends = SentimentTrendAnalyzer(detector=self._emotion_detector)
        self._topic_tracker = TopicTracker()
        # W1761: передаём data_dir, чтобы IPC-обработчики игнорировали
        # произвольный путь из запроса (path-write уязвимость).
        self._data_migrator = DataMigrator(data_dir=self.store.data_dir)
        # W1034: auto-migrate history schema at startup
        try:
            if self._data_migrator.check_migration_needed(self.store.data_dir):
                _plan = self._data_migrator.get_migration_plan(self.store.data_dir)
                logger.info("data_migrator: migration needed — plan: %s", _plan)
                _mig_result = self._data_migrator.migrate(self.store.data_dir)
                logger.info(
                    "data_migrator: migration complete %s → %s "
                    "(migrated=%d skipped=%d backup=%s)",
                    _mig_result.from_version,
                    _mig_result.to_version,
                    _mig_result.items_migrated,
                    _mig_result.items_skipped,
                    _mig_result.backup_path,
                )
            else:
                logger.debug("data_migrator: schema up-to-date, no migration needed")
        except Exception:
            logger.exception("data_migrator: startup migration failed (continuing with current schema)")
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
        # wave-1770 MED: inject settings_get for privacy gates on text analysis handlers.
        self._text_processing_svc._settings_get = self._get_runtime_setting
        # C3a wave: privacy gate for run_obsidian_sync (sibling-gate asymmetry —
        # handle_create_apple_note already gated, handle_sync did not).
        self._obsidian_sync = ObsidianSyncManager(
            data_dir=self.store.data_dir,
            event_bus=event_bus,
            settings_get=self._get_runtime_setting,
        )
        self._speaker_manager = SpeakerManager(
            data_dir=self.store.data_dir,
            settings_fn=self._settings_svc.cached_settings,  # wave-1770 HIGH: privacy gate for PII handlers
        )
        # Wire speaker_manager into HistoryService for name resolution during exports
        self._history._speaker_manager = self._speaker_manager
        self._playback_tracker = PlaybackTracker(
            data_dir=self.store.data_dir,
            privacy_mode_fn=lambda: self._get_runtime_setting("privacy_mode_enabled", False),
        )
        self._recording_comparison = RecordingComparison()
        self._smart_vocabulary = SmartVocabularyBuilder()
        # W1765 MED: wire settings_provider so privacy_mode_enabled suppresses topic
        # enrichment at runtime.  MetadataEnricher expects Callable[[], dict] (zero-arg →
        # returns full settings dict), so wire _cached_settings — same pattern used by
        # ObsidianSyncManager and other consumers of the settings dict.
        self._metadata_enricher = MetadataEnricher(settings_provider=self._cached_settings)
        self._timeline_exporter = TimelineExporter()
        self._timeline_view = TimelineViewGenerator()
        self._auto_deduplicator = AutoDeduplicator(settings_provider=self._get_runtime_setting)
        self._search_history = SearchHistoryManager(
            data_dir=self.store.data_dir,
            settings_fn=self._get_runtime_setting,
        )
        self._archive_manager = ArchiveManager(
            store=self.store,
            settings_get=self._get_runtime_setting,  # wave-1770: privacy gate wiring
        )
        # W1687 F7 MED: wire recording chain manager so archived items are
        # removed from their chains (ghost references prevented).
        self._archive_manager._recording_chain_mgr = self._chains
        # W1730: wire recording chain manager into HistoryService so that
        # delete_history_item cascades ghost-ref removal AND purge_all_data
        # calls delete_all_chains() — previously only ArchiveManager had this wire.
        self._history._recording_chain_mgr = self._chains
        self._call_session_store = CallSessionStore(data_dir=self.store.data_dir)
        # W1734: wire archive/bookmarks/call_session_store into HistoryService
        # so handle_purge_all_data can reach them without a BackendService reference.
        # _archive_manager is already constructed above; _bookmarks at line ~376.
        self._history._archive_manager = self._archive_manager
        self._history._bookmarks = self._bookmarks
        self._history._call_session_store = self._call_session_store
        # W1749: wire error_bus into HistoryService so handle_purge_all_data can push
        # history.purge_incomplete loud errors when secondary cleanup steps fail.
        self._history._error_bus = self._error_bus
        # W1765: wire speaker_manager + playback_tracker into HistoryService so
        # handle_purge_all_data can call clear_all() on both (privacy-purge gap fix).
        # _speaker_manager already wired above (строка ~594) for alias resolution;
        # здесь явно подтверждаем, что оба поля заполнены для purge.
        self._history._playback_tracker = self._playback_tracker
        # W1766 #7 (MED): wire webhook_manager into HistoryService so
        # handle_purge_all_data can call purge_all() to erase HMAC secrets.
        self._history._webhook_manager = self._webhook_manager
        # W1766 #10 (MED): wire obsidian_sync into HistoryService so
        # handle_purge_all_data can delete synced .md files from the vault.
        self._history._obsidian_sync = self._obsidian_sync
        # W1767: wire translation_cache, vocabulary, settings_svc + settings_backup
        # into HistoryService so handle_purge_all_data can erase all PII-bearing artefacts.
        self._history._translation_cache = self._translation_cache
        self._history._vocabulary_store = self.vocabulary
        self._history._settings_svc = self._settings_svc
        self._history._settings_backup = self._settings_svc._backup
        # W1770: wire transcript_versions into HistoryService — ИСПРАВЛЕНИЕ ЛАТЕНТНОГО БАГА.
        # handle_purge_all_data уже вызывает self._transcript_versions.cleanup_for_ids(...),
        # но поле НИКОГДА не заполнялось из service.py (оставалось None из конструктора →
        # каскадная очистка версий была мёртвой). Версии содержат полный текст транскрипций
        # (transcript_versions.ndjson), поэтому без этого wire они переживали privacy-purge.
        # Дополнительно вешаем _on_compact_hook как fallback: при компактировании StateStore
        # (которое purge вызывает после tombstone всех записей) хук удаляет версии для
        # item_id-ов, исчезнувших из активной истории — подчищает осиротевшие версии
        # ранее удалённых записей, не попавших в снимок active текущего purge.
        self._history._transcript_versions = self._transcript_versioning
        if getattr(self.store, "_on_compact_hook", None) is None:
            self.store._on_compact_hook = self._transcript_versioning.purge_orphaned_versions
        # W1770: wire collection_manager + session_tracker into HistoryService so
        # handle_purge_all_data can erase free-text collection names (collections.json,
        # added in #1613) and device/timing usage-pattern metadata (sessions.ndjson,
        # added in #1605). Both expose dedicated purge methods that also clear in-memory state.
        self._history._collection_manager = self._collections
        self._history._session_tracker = self._session_tracker
        # W1771 GAP-3: wire event_replay + live_subs into HistoryService so
        # handle_purge_all_data can call the proper in-memory clear hooks (not just a
        # raw file unlink). event_replay.clear() truncates event_replay.ndjson AND
        # empties the ring buffer (which holds cleartext STT/translation payloads);
        # live_subs.reset() drops the accumulated system-audio PCM buffer (raw voice)
        # that a file-only purge would never touch.
        self._history._event_replay = self._event_replay
        self._history._live_subs_service = self._live_subs
        # Wave-18 GAP-1: wire context_memory into HistoryService so handle_purge_all_data
        # can clear ContextMemory._texts — a RAM-only deque of the last 50 raw transcript
        # strings (full PII, re-exposable via get_context_memory IPC) with no file artefact.
        self._history._context_memory = self._context_memory
        self._call_session_service = CallSessionService(
            store=self._call_session_store,
            settings_get=self._get_runtime_setting,
        )
        self._audio_analytics_svc = AudioAnalyticsService(
            audio_converter=self._audio_converter,
            quality_trends=self._quality_trends,
            audio_fingerprinter=self._audio_fingerprinter,
            word_timing_analyzer=self._word_timing_analyzer,
            store=self.store,
            settings_get=self._get_runtime_setting,
        )
        self._template_manager = TemplateManager(data_dir=self.store.data_dir)
        # W1771 GAP-2: wire template_manager into HistoryService so handle_purge_all_data
        # can call purge_all() — templates.json stores free-text `text` (email signatures
        # with real names/phones) and is PII (was wrongly allowlisted by W1770).
        self._history._template_manager = self._template_manager
        self._feature_flags = FeatureFlags(data_dir=self.store.data_dir)
        # W979 F4: late-inject feature_flags into LLMRewriter so set_feature_flag IPC
        # changes are reflected immediately during rewrite() without a restart.
        if self._llm_rewriter is not None:
            self._llm_rewriter._feature_flags = self._feature_flags
        self._plugin_manager = PluginManager(data_dir=self.store.data_dir)
        self._hotword_detector = HotwordDetector(data_dir=self.store.data_dir)
        self._model_cache_manager = ModelCacheManager()
        self._model_downloader = ModelDownloader(
            event_bus=event_bus,
            stall_timeout_sec=float(
                self._settings_svc.cached_settings().get(
                    "stt_download_stall_timeout_sec", 300.0
                )
            ),
        )
        # Auto-Glossary — автоматический глоссарий из истории транскрибаций
        self._auto_glossary = AutoGlossaryBuilder(
            store=self.store,
            data_dir=self.store.data_dir,
            refresh_hours=float(
                self._settings_svc.cached_settings().get(
                    "auto_glossary_refresh_hours", settings.AUTO_GLOSSARY_REFRESH_HOURS
                )
            ),
            settings_provider=self._settings_svc.cached_settings,
        )
        # Семантический поиск (opt-in, lazy model load)
        self._semantic_searcher = SemanticSearcher(
            data_dir=self.store.data_dir,
            model_name=settings.SEMANTIC_SEARCH_MODEL,
            enabled=settings.SEMANTIC_SEARCH_ENABLED,
            # wave-22 LOW: cap index growth (FIFO eviction); 0/<=0 = unbounded.
            max_items=getattr(settings, "SEMANTIC_SEARCH_MAX_ITEMS", 0),
        )
        # wave-22 LOW: surface _save_locked persistence failures via ErrorBus
        # (late-injected, same pattern as HistoryService._error_bus above).
        self._semantic_searcher._error_bus = self._error_bus
        # Wire semantic_searcher into HistoryService so deletes remove embeddings (W1426 F2).
        self._history._semantic_searcher = self._semantic_searcher
        # W1687 F8 MED: wire semantic_searcher into ArchiveManager so that
        # archive/unarchive operations remove or re-index embeddings respectively.
        self._archive_manager._semantic_searcher = self._semantic_searcher
        # wave1776: late-inject RecordingMerger collaborators (previously bare → None).
        #  HIGH 1: cascade_delete_fn routes each source delete through the canonical
        #    HistoryService cascade tail (.md erase W1762 + semantic remove + chain
        #    ghost-ref removal + playback + transcript versions) — closes the
        #    .md-transcript privacy gap that merge's direct tombstone left behind.
        #    merge writes the tombstone itself (atomically) and passes the source ts
        #    captured BEFORE tombstone so the .md glob still resolves.
        #  HIGH 2: _semantic_searcher indexes the MERGED item (genuine need, not a
        #    delete-cascade); recording_chain_mgr REPLACES originals with the merged
        #    id in chains (merge-specific — canonical delete only REMOVES ghost refs).
        self._merger._semantic_searcher = self._semantic_searcher
        self._merger.recording_chain_mgr = self._chains
        self._merger.cascade_delete_fn = self._history.cascade_delete_artifacts
        # Late-inject AutoGlossaryBuilder into HistoryService so that
        # add_history_item immediately invalidates the glossary cache (W1288 F1).
        self._history._auto_glossary = self._auto_glossary
        # Telegram Bridge — мост Krab Ear → main Krab userbot.
        self._telegram_bridge = TelegramBridge(
            base_url=settings.TELEGRAM_BRIDGE_URL,
            timeout_sec=settings.TELEGRAM_BRIDGE_TIMEOUT_SEC,
            circuit_fail_threshold=settings.TELEGRAM_BRIDGE_CB_FAIL_THRESHOLD,
            circuit_reset_sec=settings.TELEGRAM_BRIDGE_CB_RESET_SEC,
        )
        # W1695 Variant B: wire 6 decorative services — fixes W752 wiring guard tests.
        self._analytics_svc = AnalyticsService(
            analytics_dashboard=self._analytics_dashboard,
            sentiment_trends=self._sentiment_trends,
            activity_calendar=self._activity_calendar,
            keyword_cloud_gen=self._keyword_cloud_gen,
            timeline_view=self._timeline_view,
            store=self.store,
            settings_get=self._get_runtime_setting,
        )
        self._apple_integration_svc = AppleIntegrationService(
            telegram_bridge=self._telegram_bridge,
            settings_get=self._get_runtime_setting,
        )
        self._llm_ops_svc = LLMOpsService(
            store=self.store,
            settings_svc=self._settings_svc,
            transcriber=self.transcriber,
        )
        self._search_and_analysis_svc = SearchAndAnalysisService(
            store=self.store,
            semantic_searcher=self._semantic_searcher,
            action_items_extractor=self._action_items_extractor,
            topic_tracker=self._topic_tracker,
            recording_insights=self._recording_insights,
            recording_comparison=self._recording_comparison,
            stats_report=self._stats_report,
            settings_get=self._get_runtime_setting,
        )
        self._stt_mgmt_svc = STTManagementService(
            settings_svc=self._settings_svc,
            transcriber=self.transcriber,
        )
        self._text_scoring_svc = TextScoringService(
            llm_rewriter=self._llm_rewriter,
            term_extractor=self._term_extractor,
            auto_title_generator=self._auto_title_generator,
            get_runtime_setting=self._get_runtime_setting,
        )
        # openWakeWord adapter (default disabled via WAKE_WORD_ENGINE setting).
        # settings_get ОБЯЗАТЕЛЕН: без него privacy-гейт в handle_wake_word_start
        # и loop-guard читают дефолт (False) и являются декоративными.
        self._oww_adapter = OpenWakeWordAdapter(
            data_dir=self.store.data_dir,
            settings_get=self._get_runtime_setting,
        )
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
            auto_deduplicator=self._auto_deduplicator,
            rescue_dir=Path(self.store.data_dir) / "rescue",
        )
        # R2 Task 6: owner-mismatch обязан попадать не только в WARNING, но и
        # в тот же ErrorBus, который опрашивает native-agent. Core создаётся
        # после ErrorBus, поэтому явный late-inject не зависит от глобалов.
        self._recording_core_svc._error_bus = self._error_bus
        # W1776: late-inject _bookmarks so phase_e can rebind live-recording bookmarks.
        # _bookmarks is created earlier in __init__ (line ~396).
        self._recording_core_svc._bookmarks = self._bookmarks
        # 2026-07-12: AudioSelfHealer — passive self-heal for a wedged PortAudio
        # stack (root-cause: prod incident 2026-07-12, streams open without error
        # but return silence; see backend/audio_selfheal.py). Late-injected into
        # RecordingCoreService the same way as _bookmarks/_error_bus above.

        def _reinit_audio_backend() -> None:
            try:
                import sounddevice as _sd  # type: ignore
            except Exception:
                logger.warning("AudioReinit: sounddevice недоступен, reinit пропущен")
                return
            _sd._terminate()
            _sd._initialize()

        # 2026-07-15 (спека wake-word-watchdog): единый single-flight владелец
        # танца reinit — им пользуются пассивный AudioSelfHealer (пустые
        # диктовки) и активный WakeWordWatchdog (stale heartbeat).
        self._audio_reinit_coordinator = AudioReinitCoordinator(
            reinit_audio_backend=_reinit_audio_backend,
            is_recording=lambda: bool(getattr(self.recorder, "is_recording", False)),
            wake_word_adapter=self._oww_adapter,
        )
        self._audio_selfheal = AudioSelfHealer(
            reinit_coordinator=self._audio_reinit_coordinator,
            error_bus=self._error_bus,
            settings_get=self._get_runtime_setting,
        )
        self._recording_core_svc._audio_selfheal = self._audio_selfheal
        # Активный сторож wake-word потока (живой инцидент 2026-07-13):
        # heartbeat staleness → мягкий reinit → wedged:true (эскалация на
        # Swift-agent, который выполняет kickstart -k). Останавливается в
        # close() — правило #1782 про daemon-треды в chunked CI.
        self._wake_word_watchdog = WakeWordWatchdog(
            adapter=self._oww_adapter,
            reinit_coordinator=self._audio_reinit_coordinator,
            error_bus=self._error_bus,
            # Инцидент 2026-07-16: активная запись (meeting-путь не снимает
            # слушатель) — легитимная пауза, не staleness-эпизод.
            is_recording=lambda: bool(getattr(self.recorder, "is_recording", False)),
            settings_get=self._get_runtime_setting,
        )
        self._wake_word_watchdog.start()
        # Wave-22: wire RecordingCoreService._job_tracker into HistoryService so
        # handle_purge_all_data can call clear() — terminal jobs hold transcript
        # text in items[].text (full PII) and survive privacy-purge without this wire.
        self._history._job_tracker = self._recording_core_svc._job_tracker
        # R2 Task 5: HistoryService создан раньше RecordingCoreService, поэтому
        # прямой late-inject повторяет тот же безопасный порядок, что JobTracker.
        # Privacy-purge очищает terminal-response cache и повышает его epoch.
        self._history._recording_core = self._recording_core_svc
        # wave-1770 HIGH: inject SearchHistoryManager so handle_purge_all_data can call
        # clear_search_history() (clears in-memory _entries) instead of just unlinking
        # the file (which left RAM entries returning stale queries until restart).
        self._history._search_history_mgr = self._search_history
        # C2a: MeetingSessionService — live meeting overlay backend-ядро.
        # Требует recording_core (уже сконструирован выше) + action_items_extractor.
        self._meeting_svc = MeetingSessionService(
            recorder=self.recorder,
            transcriber=self.transcriber,
            recording_core=self._recording_core_svc,
            action_items_extractor=self._action_items_extractor,
            settings_get=self._get_runtime_setting,
            event_bus=event_bus,
            # Двойной getattr, не прямой доступ: десятки тестовых фейков
            # transcriber (test_backend_service.py и сиблинги) не несут ни
            # .engine, ни тем более .engine.diarize_window —
            # MeetingSessionService уже умеет None (DIAR_WINDOW-тик деградирует
            # с одним громким WARN на сессию), это НЕ костыль, а использование
            # существующего контракта.
            diarize_window=getattr(
                getattr(self.transcriber, "engine", None), "diarize_window", None
            ),
            data_dir=self.store.data_dir,
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
        # W1622 (W1615 F1 HIGH): wire error_bus so _push_stt_cache_miss_error
        # actually fires instead of silently returning.  _error_bus is guaranteed
        # to exist here — it was initialised ~280 lines above.
        self._startup_diagnostics._error_bus = self._error_bus

        # W1690 (W1686 F9 HIGH): wire HealthCheckService — extraction was done but
        # delegation was never completed (W746-class lesson).  All 7 IPC health handlers
        # now delegate to this service; inline duplicates replaced by single-line stubs.
        from backend.health_check_service import HealthCheckService
        from backend.metrics_collector import metrics as _metrics_singleton
        self._health_check_svc = HealthCheckService(
            store=self.store,
            health_checker=self._health_checker,
            startup_diagnostics=self._startup_diagnostics,
            integrity_checker=self._integrity_checker,
            llm_probe=self._llm_probe,
            metrics_collector=_metrics_singleton,
            event_bridge=self._event_bridge,
            transcriber=self.transcriber,
            llm_rewriter=self._llm_rewriter,
            settings_svc=self._settings_svc,
            start_time=self._start_time,
            app_version=APP_VERSION,
            recorder=self.recorder,
            last_stt_engine_ref=self._last_stt_engine_ref,
            wake_word_watchdog=self._wake_word_watchdog,
            rest_inprocess=self._rest_inprocess,
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
        # W1687 F1 HIGH: wire error_bus so disk.warn / disk.critical KrabErrors
        # actually reach the ErrorBus and the Loud Errors UI toast.
        self._disk_monitor._error_bus = self._error_bus

        # R1: единый тред старт-recovery (Task 6 амендмент к Task 4).
        # Порядок ОБЯЗАТЕЛЕН: форензика прошлой жизни СНАЧАЛА (пока dirty-
        # маркер ещё несёт улику предыдущей жизни процесса), маркер ТЕКУЩЕЙ
        # жизни пишется ПОСЛЕ сбора (иначе свежий маркер затрёт улику ДО
        # того, как check_and_collect успеет её прочитать), скан
        # восстановления записей — последним. Фоновый тред — старт IPC не
        # ждёт (спека §4.2/§4.3). check_and_collect/write_alive_marker/
        # run_rescue_scan контрактно НИКОГДА не бросают сами по себе
        # (fail-open с внутренним WARN); внешний try/except здесь страхует
        # только сам импорт модулей и создание треда.
        try:
            from backend.shutdown_forensics import _MARKER as _ALIVE_MARKER_FILE
            from backend.shutdown_forensics import check_and_collect, write_alive_marker
            from backend.recording_rescue import run_rescue_scan
            _data_dir = Path(self.store.data_dir)
            _rescue_dir = _data_dir / "rescue"
            _own_log_dirs = [
                PROJECT_ROOT / "logs" / "krab-ear-backend.out.log",
                PROJECT_ROOT / "logs" / "krab-ear-backend.err.log",
            ]
            # R1 HIGH-1 (adversarial-гейт целого диффа, 2026-07-24): снимок
            # .part-кандидатов ЗАМОРАЖИВАЕТСЯ здесь — СИНХРОННО, ВНУТРИ
            # __init__, ДО старта фонового треда. IPC-сокет конструируется
            # только в main() ПОСЛЕ возврата из этого __init__ — значит на
            # этой строке ни один клиент физически не мог вызвать
            # start_recording, и любой .part в этом снимке гарантированно
            # принадлежит ПРОШЛОЙ жизни процесса. Фоновый тред ниже может
            # провести десятки секунд в check_and_collect() (subprocess-
            # таймауты до ~60с на UNCLEAN-смерти) ДО того, как дойдёт до
            # run_rescue_scan — не замораживать список значило бы отдать
            # ему живой glob() уже ПОСЛЕ того, как IPC успел принять новую
            # (легитимную) запись, и рескью-скан мог бы удалить её
            # crash-safety файл как «восстановленный».
            try:
                _frozen_rescue_parts = sorted(_rescue_dir.glob("*.f32.part")) if _rescue_dir.is_dir() else []
            except Exception:
                logger.warning("startup-recovery: не удалось заморозить снимок rescue/", exc_info=True)
                _frozen_rescue_parts = []

            # Ubuntu-CI амендмент (2026-07-24, найдено красным CI после
            # adversarial-гейта): голый фоновый daemon-тред, стартующий
            # РЕАЛЬНОЕ дисковое I/O (write_alive_marker создаёт data_dir/
            # маркер немедленно) сразу при КАЖДОМ конструировании
            # BackendService — а таких конструирований в тест-сьюте сотни,
            # почти все на свежих tempfile.TemporaryDirectory(). Даже
            # микросекундная гонка thread-write vs test tearDown's rmtree
            # даёт `OSError: Directory not empty` под нагрузкой CI-раннера
            # (3 файла упали в чанк-прогоне: test_error_codes.py — отдельная
            # причина, registry-count; test_ipc_dispatch_integration.py/
            # test_ipc_roundtrip.py — ИМЕННО эта гонка). Корневая причина:
            # тред спавнится БЕЗУСЛОВНО, хотя в подавляющем большинстве
            # случаев (чистый старт: маркера нет, rescue/ пуст) ему нечего
            # делать — check_and_collect() сам вернул бы "first_run"/"clean"
            # мгновенно, без единого subprocess-вызова. Фикс: дешёвая
            # синхронная проверка "есть ли реальная работа" ДО решения
            # спавнить тред. Если маркера нет И нет замороженных .part —
            # тред НЕ нужен вообще; маркер текущей жизни пишем синхронно
            # (одна короткая запись файла, уже совершённая до возврата из
            # __init__ — с тестом больше нечему racing). Тред спавнится
            # ТОЛЬКО когда есть реальная (потенциально медленная) работа:
            # либо UNCLEAN-маркер прошлой жизни (форензика), либо
            # незавершённые записи (rescue-скан с возможной транскрипцией).
            _had_dirty_marker = (_data_dir / _ALIVE_MARKER_FILE).exists()
            _needs_background_recovery = _had_dirty_marker or bool(_frozen_rescue_parts)

            if _needs_background_recovery:
                def _startup_recovery() -> None:
                    check_and_collect(data_dir=_data_dir, log_dirs=_own_log_dirs)
                    write_alive_marker(_data_dir)
                    run_rescue_scan(
                        rescue_dir=_rescue_dir,
                        recording_core=self._recording_core_svc,
                        error_bus=self._error_bus,
                        settings_get=self._get_runtime_setting,
                        collection_manager=self._collections,
                        parts=_frozen_rescue_parts,
                    )

                threading.Thread(
                    target=_startup_recovery,
                    daemon=True,
                    name="startup-recovery",
                ).start()
            else:
                # Чистый старт: нечего восстанавливать/собирать — пишем
                # маркер ТЕКУЩЕЙ жизни синхронно (быстро, без потока).
                write_alive_marker(_data_dir)
        except Exception:
            logger.warning("startup-recovery: старт треда провалился", exc_info=True)

        # Обработчик корректного завершения (регистрация сигналов — через register())
        self._shutdown_handler = GracefulShutdownHandler(data_dir=self.store.data_dir)

        # Audit logger — structured NDJSON log of all IPC requests (W1351 F1 fix).
        # Always enabled (core observability). Flushed at shutdown via GracefulShutdownHandler.
        self._audit_logger = AuditLogger(data_dir=self.store.data_dir)

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

        # Таблица диспетчеризации IPC: строится ОДИН раз в конце __init__, после
        # того как все сервисы-коллабораторы (self._<svc>) уже сконструированы.
        # handle_request делает O(1) lookup по self._dispatch_table (W1769).
        self._dispatch_table: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = (
            self._build_dispatch_table()
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

    def _propagate_hf_token_to_env(self, overwrite: bool = False) -> None:
        """W1755: копирует hf_token / stt_gigaam_hf_token из runtime settings.json в os.environ.

        pyannote Pipeline.from_pretrained и транскрайбер читают HF_TOKEN из os.environ, а не из
        settings.json — поэтому токен из GUI никогда не доходил до дирайзации → 401 «no token».
        Вызывается при старте (overwrite=False) и из after_save hook (overwrite=True) чтобы
        токен работал без рестарта.

        Приоритет: боевые call-site'ы (init И after_save hook) вызывают с overwrite=True —
        GUI/канонический токен авторитетен и побеждает STALE-токен, запечённый в launchd-plist
        EnvironmentVariables на install (revoked plist HF_TOKEN иначе побеждал бы через
        setdefault → pyannote 401). overwrite=False (setdefault, существующий env побеждает)
        сохранён в сигнатуре для прямых вызовов/тестов. Когда GUI-токена нет — метод выходит
        рано (см. ниже), поэтому явный env-override (KRAB_EAR_HF_TOKEN) сохраняется.

        Источник: hf_token (общий) имеет приоритет над stt_gigaam_hf_token для generic ключей
        (gigaam-специфичный токен может не иметь прав на pyannote gating → spurious 401).
        Значение токена НИКОГДА не логируется.
        """
        _hf = self._get_runtime_setting("hf_token", "").strip()
        _gigaam = self._get_runtime_setting("stt_gigaam_hf_token", "").strip()
        # Общий hf_token предпочтителен для generic env-ключей.
        # stt_gigaam_hf_token — фолбэк когда hf_token пустой.
        _token = _hf or _gigaam
        _token_source = "generic" if _hf else "gigaam"
        if not _token:
            return
        _env_keys = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")
        _propagated: list[str] = []
        try:
            for _k in _env_keys:
                if overwrite or not os.environ.get(_k):
                    os.environ[_k] = _token
                    _propagated.append(_k)
        except Exception as exc:  # noqa: BLE001
            # Например, null-byte в токене вызывает ValueError в os.environ[k]=value.
            # Не логируем сам токен — только тип ошибки.
            logger.warning(
                "hf_token env propagation failed",
                extra={"error": type(exc).__name__},
            )
            return
        if _propagated:
            logger.info(
                "hf token propagated to env",
                extra={
                    "source": "runtime_settings",
                    "keys": ",".join(_propagated),
                    "token_source": _token_source,
                    "overwrite": overwrite,
                },
            )
        # При overwrite=True инвалидируем кэшированный diarization pipeline — новый токен
        # будет использован при следующем вызове без рестарта.
        if overwrite and _propagated:
            _engine = getattr(getattr(self, "transcriber", None), "engine", None)
            if _engine is not None:
                _run_lock = getattr(_engine, "_diarization_run_lock", None)
                _load_lock = getattr(_engine, "_diarization_load_lock", None)
                if _run_lock is not None and _load_lock is not None:
                    try:
                        # Единый порядок во всём AudioEngine: run → load.
                        # Так invalidation ждёт текущий инференс и не оставляет
                        # ожидающему потоку заранее захваченный старый pipeline.
                        with _run_lock:
                            with _load_lock:
                                _engine._diarization_pipeline = None
                                _engine._diarization_load_error = None
                                logger.info(
                                    "diarization pipeline and cached load error "
                                    "invalidated after hf_token change",
                                    extra={"token_source": _token_source},
                                )
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(
                            "diarization pipeline invalidation failed",
                            extra={"error": type(exc2).__name__},
                        )
                else:
                    # Совместимость с лёгкими test doubles/старыми engine:
                    # сбрасываем оба поля, но громко фиксируем отсутствие locks.
                    try:
                        _engine._diarization_pipeline = None
                        _engine._diarization_load_error = None
                        logger.info(
                            "diarization pipeline invalidated without locks after hf_token change",
                            extra={"token_source": _token_source},
                        )
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(
                            "hf_token changed but diarization pipeline invalidation failed — "
                            "restart required to apply new token",
                            extra={"error": type(exc2).__name__},
                        )

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

    def _push_rest_error(self, code: str, detail: str) -> None:
        """Колбэк для InProcessRestServer: заворачивает сбой в KrabError (M2).

        Зовётся из чужого треда (rest-inprocess), поэтому тело целиком под
        try/except — необработанное исключение здесь тихо уронило бы тред
        сервера. Образец заполнения полей — WakeWordWatchdog._escalate()
        (backend/wake_word_watchdog.py): severity/текст берутся из
        ERROR_REGISTRY, а не хардкодятся здесь, чтобы код ошибки оставался
        единственным источником правды для UI-сообщения.
        """
        try:
            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get(code, {})
            self._error_bus.push(KrabError(
                severity=entry.get("severity", "error"),
                component="rest",
                code=code,
                message_user=entry.get(
                    "user_msg_ru", "Встроенный REST-сервер недоступен.",
                ),
                message_debug=detail,
                timestamp=datetime.now(timezone.utc),
                context={"detail": detail},
                actionable=bool(entry.get("actionable", False)),
                action_id=entry.get("action_id"),
            ))
        except Exception:
            logger.exception("in-process REST: _push_rest_error упал")

    @staticmethod
    def _webhook_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Возвращает PII-безопасную копию payload для отправки во внешний webhook.

        Удаляет ключи с пользовательским контентом (текст транскрипта/перевода,
        сегменты) — webhook-получатель по умолчанию видит только метаданные
        (history_id, duration_sec, language, confidence и т.п.).  Это намеренно
        консервативно: webhook-и уходят на внешние URL, поэтому транскрипт не
        должен покидать устройство без явного opt-in (на сегодня opt-in нет —
        расширяемая точка задокументирована у _WEBHOOK_FORWARDED_EVENTS).
        """
        if not isinstance(payload, dict):
            return {}
        return {k: v for k, v in payload.items() if k not in _WEBHOOK_PII_KEYS}

    def _forward_event_to_webhooks(self, event_type: str, payload: dict[str, Any]) -> None:
        """EventBus-листенер: доставляет lifecycle-события зарегистрированным webhook-ам.

        Вызывается inline внутри EventBus.emit() для КАЖДОГО события (см.
        EventBus.add_listener), поэтому:
          - быстро отсеивает нерелевантные/высокочастотные события по allowlist;
          - строит PII-безопасный payload (без текста транскрипта/перевода);
          - вызывает fire_webhook, который возвращается немедленно (доставка идёт
            в собственном bounded ThreadPoolExecutor + retry внутри WebhookManager),
            так что горячий STT-поток не блокируется на сетевом I/O.

        Privacy/SSRF/фильтрация по типу события на стороне WebhookManager:
          - privacy_mode gate (set_privacy_mode, синхронизируется в __init__);
          - per-webhook event filter (пустой список = все события);
          - SSRF-pinned + retrying delivery path.
        Исключения проглатываются — сбой доставки webhook не должен ломать pipeline
        (EventBus.emit тоже оборачивает листенеры в try/except как доп. страховку).
        """
        if event_type not in _WEBHOOK_FORWARDED_EVENTS:
            return
        try:
            safe_payload = self._webhook_safe_payload(payload)
            self._webhook_manager.fire_webhook(event_type, safe_payload)
        except Exception:
            logger.warning(
                "wave1775: webhook forward failed for event %s", event_type, exc_info=True
            )

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

    # ---------------------------------------------------------------------- #
    # Export scheduler periodic worker (W982 — F1 fix)                      #
    # ---------------------------------------------------------------------- #

    _EXPORT_SCHEDULER_INTERVAL_SEC: int = 300  # check every 5 minutes

    def _export_scheduler_loop(self) -> None:
        """Фоновый поток периодически вызывает ExportScheduler.check_and_export().

        Интервал проверки: 5 минут. check_and_export() возвращает None если
        авто-экспорт отключён или ещё не подошёл срок — в обоих случаях
        метод завершается быстро (только чтение файла расписания).
        """
        stop = self._export_scheduler_stop
        while not stop.is_set():
            try:
                result = self._export_scheduler.check_and_export(self.store)
                if result is not None:
                    logger.info(
                        "export_scheduler: авто-экспорт выполнен",
                        extra={
                            "file_path": result.get("path"),
                            "format": result.get("format"),
                            "size_bytes": result.get("size_bytes"),
                        },
                    )
            except Exception:
                logger.exception("export_scheduler tick failed")
            stop.wait(self._EXPORT_SCHEDULER_INTERVAL_SEC)

    def close(self) -> bool:
        """Graceful shutdown: останавливает фоновые потоки (LLM probe и др.).

        Идемпотентен — безопасно вызывать несколько раз. Используется в
        signal handler run_server() и в finally serve_forever(). Возвращает
        False, когда любой нативный/audio worker не подтвердил завершение.
        """
        all_workers_stopped = True
        meeting_service = getattr(self, "_meeting_svc", None)
        if meeting_service is not None:
            try:
                # Двухфазный shutdown: meeting-start должен увидеть closing
                # ДО того, как RecordingCore остановит уже опубликованный
                # recorder. Иначе start мог вернуть ok=True для мёртвой записи.
                meeting_service.begin_shutdown()
            except Exception:
                all_workers_stopped = False
                logger.exception(
                    "MeetingSessionService.begin_shutdown() raised during close()"
                )

        recording_core = getattr(self, "_recording_core_svc", None)
        if recording_core is not None:
            try:
                if recording_core.close_background_workers() is False:
                    all_workers_stopped = False
                    logger.error(
                        "RecordingCoreService не завершил все worker-ы при close()"
                    )
            except Exception:
                all_workers_stopped = False
                logger.exception(
                    "RecordingCoreService.close_background_workers() raised during close()"
                )

        wake_word_stopped = True
        # Stop export-scheduler worker thread.
        stop_event = getattr(self, "_export_scheduler_stop", None)
        if stop_event is not None:
            stop_event.set()
        es_thread = getattr(self, "_export_scheduler_thread", None)
        if es_thread is not None and es_thread.is_alive():
            es_thread.join(timeout=2.0)

        probe = getattr(self, "_llm_probe", None)
        if probe is not None:
            try:
                probe.stop()
            except Exception:
                logger.exception("LLMHttpProbe.stop() raised during close()")

        # Stop DiskSpaceMonitor daemon thread so it cannot write to stderr
        # after interpreter shutdown begins (causes "could not acquire lock for
        # <_io.BufferedWriter name='<stderr>'>" fatal error in chunked CI runs).
        disk_monitor = getattr(self, "_disk_monitor", None)
        if disk_monitor is not None:
            try:
                disk_monitor.stop()
            except Exception:
                logger.exception("DiskSpaceMonitor.stop() raised during close()")

        # Stop RecapScheduler daemon thread for the same reason.
        recap_scheduler = getattr(self, "_recap_scheduler", None)
        if recap_scheduler is not None:
            try:
                recap_scheduler.stop()
            except Exception:
                logger.exception("RecapScheduler.stop() raised during close()")

        # Stop PurgeScheduler daemon thread — mirrors the RecapScheduler stop above.
        purge_scheduler = getattr(self, "_purge_scheduler", None)
        if purge_scheduler is not None:
            try:
                purge_scheduler.stop()
            except Exception:
                logger.exception("PurgeScheduler.stop() raised during close()")

        # Stop EventBridge sender daemon thread — mirrors DiskSpaceMonitor/
        # RecapScheduler/PurgeScheduler stop above (та же CI daemon-thread
        # teardown rule, feedback_backendservice_teardown_ci.md).
        event_bridge = getattr(self, "_event_bridge", None)
        if event_bridge is not None:
            try:
                event_bridge.stop()
            except Exception:
                logger.exception("EventBridge.stop() raised during close()")

        # Stop in-process REST (M2) — тот же daemon-thread teardown rule.
        rest_inprocess = getattr(self, "_rest_inprocess", None)
        if rest_inprocess is not None:
            try:
                rest_inprocess.stop()
            except Exception:
                logger.exception("InProcessRestServer.stop() raised during close()")

        # Stop MeetingSessionService worker thread (C2a) — mirrors the
        # EventBridge/PurgeScheduler stop above (same CI daemon-thread rule).
        try:
            if self._meeting_svc.close() is False:
                all_workers_stopped = False
                logger.error(
                    "MeetingSessionService сохранил recovery-session при close()"
                )
        except Exception:
            all_workers_stopped = False
            logger.exception("MeetingSessionService.close() raised during close()")

        # Stop WakeWordWatchdog daemon thread — same CI daemon-thread teardown
        # rule (feedback_backendservice_teardown_ci.md).
        watchdog = getattr(self, "_wake_word_watchdog", None)
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:
                logger.exception("WakeWordWatchdog.stop() raised during close()")

        # Listener обязан завершиться ДО выгрузки CFFI/PortAudio. Раньше close()
        # останавливал только watchdog, оставляя OpenWakeWordListener живым до
        # teardown интерпретатора; три последовательных kickstart завершились
        # SIGSEGV внутри cffi/libffi уже после сообщения о чистом shutdown.
        wake_word_adapter = getattr(self, "_oww_adapter", None)
        if wake_word_adapter is not None:
            try:
                if not wake_word_adapter.stop():
                    wake_word_stopped = False
                    logger.error(
                        "OpenWakeWordAdapter не завершился при остановке backend"
                    )
            except Exception:
                wake_word_stopped = False
                logger.exception("OpenWakeWordAdapter.stop() raised during close()")
        return all_workers_stopped and wake_word_stopped

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

    def _build_dispatch_table(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """Строит таблицу диспетчеризации IPC-методов {method: handler}.

        Вызывается ОДИН раз в конце ``__init__`` и кэшируется в
        ``self._dispatch_table`` (O(1) lookup, без перестройки на каждый запрос).
        Все записи — bound-методы или лямбды, захватывающие ``self``; сервисы
        стабильны после ``__init__``, поэтому перестройка не требуется.

        ВНИМАНИЕ (W957 SECURITY): ``clear_privacy_audit_log`` сюда НЕ добавляется —
        уничтожение compliance audit trail через неавторизованный IPC запрещено.
        """
        return {
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
            "get_voice_gateway_credential": self._settings_svc.handle_get_voice_gateway_credential,
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
            "clear_translation_cache": self._handle_clear_translation_cache,  # очистить персистентный LRU-кэш переводов (память + файл)
            "get_diagnostics": self._handle_get_diagnostics,  # диагностика: system, stt, llm, history, settings_cache
            "set_translation_glossary_item": self._translation.handle_set_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
            # VERIFIED: called from Swift (HistoryPanel)
            "remove_translation_glossary_item": self._translation.handle_remove_translation_glossary_item,
            "get_glossary_suggestions": self._translation.handle_get_glossary_suggestions,  # авто-обучение глоссария: предлагает пары source→target из истории
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
            "extract_action_items": self._search_and_analysis_svc.handle_extract_action_items,  # LLM извлечение задач/решений/вопросов по item_id
            "batch_extract_action_items": self._search_and_analysis_svc.handle_batch_extract_action_items,  # пакетное извлечение для нескольких item_id
            "get_pending_action_items": self._search_and_analysis_svc.handle_get_pending_action_items,  # все items у которых action_items=None
            "meeting_start": self._meeting_svc.handle_meeting_start,  # C2a: старт/повышение live-встречи
            "meeting_stop": self._meeting_svc.handle_meeting_stop,  # C2a: финализация live-встречи
            "get_meeting_live_state": self._meeting_svc.handle_get_meeting_live_state,  # C2a: снимок для панели
            "get_last_llm_diff": self._llm_ops_svc.handle_get_last_llm_diff,  # последний word-level diff от LLM rewriter'а

            "get_vocabulary_suggestions": self._translation.handle_get_vocabulary_suggestions,
            "toggle_favorite": self._history.handle_toggle_favorite,
            "get_favorites": self._history.handle_get_favorites,
            "is_favorite": self._history.handle_is_favorite,
            "export_history": self._history.handle_export_history,
            "export_history_srt": self._history.handle_export_history_srt,
            "export_history_csv": self._history.handle_export_history_csv,
            "batch_export": self._history.handle_batch_export,  # пакетный экспорт в нескольких форматах
            "export_history_markdown": self._history.handle_export_history_markdown,
            "export_selected_items": self._history.handle_export_selected_items,  # экспорт ВЫБРАННЫХ записей (markdown/srt)
            "export_obsidian": self._history.handle_export_obsidian,  # Obsidian-совместимый .md экспорт
            "export_history_json": self._history.handle_export_history_json,
            "export_html_report": self._history.handle_export_html_report,  # автономный HTML-отчёт с аналитикой
            "generate_html_report": self._history.handle_export_html_report,  # алиас для Swift UI (Analytics Dashboard)
            "repaste_item": self._history.handle_repaste_item,
            "get_clipboard_history": self._history.handle_get_clipboard_history,  # история буфера обмена: последние N вставленных транскрипций
            "cleanup_old_history": self._history.handle_cleanup_old_history,  # удаляет записи старше N дней
            "purge_all_data": self._handle_purge_all_data,  # wave-25: purge + auto_backup TOCTOU-guard (обёртка над HistoryService.handle_purge_all_data)
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
            "apply_recommended_setup": self._handle_apply_recommended_setup,  # A1: рекомендованная настройка в один тап (dry_run превью + apply)
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
            "check_mic_noise": self._handle_check_mic_noise,  # pre-flight: RMS/peak + профиль шума (noise_type/SNR/STT-пригодность)
            "get_disk_status": self._handle_get_disk_status,  # статус дискового пространства (HEAVY: recursive walk data_dir, wave-33)
            "get_storage_breakdown": self._handle_get_storage_breakdown,  # разбивка использования диска по компонентам
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
            "get_audit_log": self._handle_get_audit_log,  # последние записи IPC audit log; privacy_mode блокирует
            "handle_error_action": self._handle_handle_error_action,  # выполнить actionable-действие из toast/diagnostics
            "probe_llm_http": self._handle_probe_llm_http,  # однократный ping LM Studio HTTP endpoint
            "get_brain_lease_status": self._health_check_svc.handle_get_brain_lease_status,  # B3: кто держит LM Studio brain-лиз
            "warmup_stt": self._stt_mgmt_svc.handle_warmup_stt,  # ручной запуск STT warmup (после смены профиля/модели)
            "warmup_rewriter": self._text_scoring_svc.handle_warmup_rewriter,  # явный warmup-probe для "Load Model" кнопки
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
            "rename_collection": self._collections.handle_rename_collection,  # W1773: переименовать коллекцию (old_name → new_name)
            "list_normalization_profiles": self._handle_list_normalization_profiles,  # список профилей нормализации текста
            "add_normalization_profile": self._handle_add_normalization_profile,  # добавить пользовательский профиль нормализации
            "remove_normalization_profile": self._handle_remove_normalization_profile,  # удалить пользовательский профиль нормализации
            "apply_normalization_profile": self._handle_apply_normalization_profile,  # применить профиль нормализации к тексту
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
            "get_meeting_report": self._handle_get_meeting_report,  # полный отчёт о встрече: summary, задачи, решения, вопросы, спикеры
            "analyze_quality_trends": self._audio_analytics_svc.handle_analyze_quality_trends,  # анализ трендов качества
            "compare_periods": self._analytics_svc.handle_compare_periods,  # сравнение двух периодов использования
            "get_activity_calendar": self._analytics_svc.handle_get_activity_calendar,  # GitHub-style activity calendar данные
            "get_recording_insights": self._search_and_analysis_svc.handle_get_recording_insights,  # эвристические инсайты по записям (Wave 54: alias was wrongly pointed at _handle_get_recording_stats)
            "get_daily_insight": self._handle_get_daily_insight,  # один наиболее релевантный инсайт за сегодня (W1274 F3)
            "get_sentiment_trends": self._analytics_svc.handle_get_sentiment_trends,  # анализ трендов тональности транскрипций за N дней

            "check_integrity": self._handle_check_integrity,  # проверка целостности данных
            "repair_integrity": self._handle_repair_integrity,  # исправление проблем целостности данных
            "extract_terms": self._text_scoring_svc.handle_extract_terms,  # извлечение терминов из текста
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
            "get_keyword_cloud": self._analytics_svc.handle_get_keyword_cloud,  # данные облака ключевых слов для визуализации word cloud
            "prepare_share": self._sharing.handle_prepare_share,  # подготовить пакет для шаринга транскрипций
            "list_shared": self._sharing.handle_list_shared,  # список сохранённых пакетов шаринга
            "get_shared": self._sharing.handle_get_shared,  # получить пакет шаринга по share_id
            "revoke_share_link": self._sharing.handle_revoke_share_link,  # отозвать пакет шаринга по токену (Wave 158)
            "save_transcript_version": self._transcript_versioning.handle_save_transcript_version,  # сохранить новую версию текста транскрипции
            "get_transcript_versions": self._transcript_versioning.handle_get_transcript_versions,  # получить все версии транскрипции по item_id
            "revert_transcript_version": self._transcript_versioning.handle_revert_transcript_version,  # откат транскрипции к указанной версии
            "generate_auto_title": self._text_scoring_svc.handle_generate_auto_title,  # автоматическая генерация заголовка для транскрибации
            # форматирование текста под целевое приложение (telegram, notes, email и др.)
            "format_for_paste": self._paste_formatter.handle_format_for_paste,
            "merge_recordings": lambda p: self._merger.handle_merge_recordings(p, self.store),  # объединить несколько записей истории в одну
            "preview_merge": lambda p: self._merger.handle_preview_merge(p, self.store),  # предпросмотр объединения без сохранения
            "list_paste_formatters": self._paste_formatter.handle_list_paste_formatters,  # список доступных форматтеров вставки
            "get_learning_stats": self._handle_get_learning_stats,  # режим изучения языков: статистика прогресса
            "get_analytics_dashboard": self._analytics_svc.handle_get_analytics_dashboard,  # комплексный дашборд аналитики: все метрики за один вызов
            "get_topic_timeline": self._search_and_analysis_svc.handle_get_topic_timeline,  # таймлайн смен тем разговора из истории транскрибаций
            "list_config_presets": self._config_presets.handle_list_config_presets,  # список конфигурационных пресетов (встроенных и кастомных)
            "apply_config_preset": self._config_presets.handle_apply_config_preset,  # атомарно применить пресет: merge + save + after_save_hooks
            "delete_config_preset": self._config_presets.handle_delete_config_preset,  # удалить кастомный конфигурационный пресет по имени
            "export_config_preset": self._config_presets.handle_export_config_preset,  # экспортировать пресет в JSON-строку для передачи/сохранения
            "import_config_preset": self._config_presets.handle_import_config_preset,  # импортировать пресет из JSON-строки (envelope или прямой объект)
            "create_config_preset": self._config_presets.handle_create_config_preset,  # создать кастомный конфигурационный пресет
            "enqueue_transcription": self._transcription_queue.handle_enqueue,  # добавить аудиофайл в очередь транскрипции с приоритетом
            "cancel_transcription": self._transcription_queue.handle_cancel,  # отменить задание транскрипции по job_id
            "get_queue_status": self._transcription_queue.handle_get_status,  # статус задания транскрипции по job_id
            "list_transcription_queue": self._transcription_queue.handle_list_queue,  # список всех заданий очереди транскрипции
            "detect_emotion": self._text_processing_svc.handle_detect_emotion,  # эвристическое определение эмоции в тексте транскрипции
            "estimate_recording_cost": self._handle_estimate_recording_cost,  # оценка вычислительной стоимости обработки записи
            "estimate_batch_cost": self._handle_estimate_batch_cost,  # суммарная оценка стоимости пакетного импорта записей
            "get_daily_cost_summary": self._handle_get_daily_cost_summary,  # сводка вычислительных расходов за сегодня
            "check_migration": self._data_migrator.handle_check_migration,  # проверка необходимости миграции данных
            "run_migration": self._data_migrator.handle_run_migration,  # выполнение миграции данных между версиями
            "rollback_migration": self._data_migrator.handle_rollback_migration,  # откат последней миграции из резервной копии (#1592)
            "expand_abbreviations": self._text_processing_svc.handle_expand_abbreviations,  # раскрытие аббревиатур в тексте транскрипции
            "add_abbreviation": self._text_processing_svc.handle_add_abbreviation,  # добавить пользовательскую аббревиатуру
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
            # W1773: записи истории, ни разу не воспроизводившиеся (нужен store для пересечения с активной историей)
            "get_never_played": lambda p: self._playback_tracker.handle_get_never_played(p, store=self.store),
            # прогнать текст через настраиваемый конвейер пост-обработки (пробелы, пунктуация, сущности, аббревиатуры, анонимизация)
            "post_process_text": self._text_processing_svc.handle_post_process_text,
            "list_post_process_steps": self._text_processing_svc.handle_list_post_process_steps,  # список доступных шагов пост-обработки текста
            "compare_recordings": self._search_and_analysis_svc.handle_compare_recordings,  # сравнение нескольких записей side-by-side: матрица сходства, статистика, общие/уникальные слова
            "select_model": self._stt_mgmt_svc.handle_select_model,  # умный выбор STT-модели на основе условий записи
            "get_smart_vocabulary_suggestions": self._handle_get_smart_vocabulary_suggestions,  # предложения для словаря STT на основе паттернов использования
            "get_startup_diagnostics": self._handle_get_startup_diagnostics,  # диагностика при старте: результаты всех startup-проверок
            # автоматическое обогащение метаданных записи: word_count, emotion, pace, quality, topics и др.
            "enrich_recording": self._metadata_enricher.handle_enrich_recording,
            "get_shutdown_status": self._handle_get_shutdown_status,  # статус последнего graceful shutdown: clean, last_shutdown_time
            "check_duplicate": self._handle_check_duplicate,  # проверка одной транскрипции на дублирование по текстовому сходству
            "run_deduplication": self._handle_run_deduplication,  # полное сканирование истории на дубликаты
            "get_dedup_stats": self._handle_get_dedup_stats,  # статистика дедупликатора: проверено, найдено, символов сохранено
            "get_timeline_view": self._analytics_svc.handle_get_timeline_view,  # группировка истории по временным блокам (timeline)
            "get_recent_searches": self._search_history.handle_get_recent_searches,  # последние поисковые запросы пользователя
            "get_popular_searches": self._search_history.handle_get_popular_searches,  # наиболее частые поисковые запросы
            "clear_search_history": self._search_history.handle_clear_search_history,  # очистить историю поисковых запросов
            "archive_items": self._archive_manager.handle_archive_items,  # переместить записи истории в архив
            "unarchive_items": self._archive_manager.handle_unarchive_items,  # восстановить записи из архива
            "list_archived": self._archive_manager.handle_list_archived,  # список архивированных записей
            "get_archive_stats": self._archive_manager.handle_get_archive_stats,  # статистика архива: количество, размер, oldest/newest
            "generate_stats_report": self._search_and_analysis_svc.handle_generate_stats_report,  # полный Markdown-отчёт статистики за период
            "generate_mini_stats_report": self._search_and_analysis_svc.handle_generate_mini_stats_report,  # краткий 5-строчный отчёт состояния
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
            # --- speaker statistics ---
            "get_speaker_statistics": lambda p: self._speaker_statistics.handle_get_speaker_statistics(  # per-speaker stats из истории диаризации
                p, store=self.store, speaker_manager=self._speaker_manager
            ),
            # --- speaker fingerprints (W951 F4) ---
            "register_speaker": self._speaker_manager.handle_register_speaker,  # зарегистрировать эмбеддинг спикера
            "delete_speaker_fingerprint": self._speaker_manager.handle_delete_speaker_fingerprint,  # удалить отпечаток спикера
            "list_speaker_fingerprints": self._speaker_manager.handle_list_speaker_fingerprints,  # список всех отпечатков спикеров
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
            "send_to_telegram": self._apple_integration_svc.handle_send_to_telegram,  # отправить транскрипцию в Telegram через main Krab userbot
            # --- Apple Notes integration (Phase D.4) ---
            "create_apple_note": self._apple_integration_svc.handle_create_apple_note,  # создать заметку в Apple Notes через osascript
            # --- Apple Reminders integration (Phase D.4) ---
            "create_apple_reminder": self._apple_integration_svc.handle_create_apple_reminder,  # создать напоминание в Apple Reminders через osascript
            # --- Apple Calendar integration (Phase D.4) ---
            "create_calendar_event": self._apple_integration_svc.handle_create_calendar_event,  # создать событие в Apple Calendar через osascript
            # --- CalendarLinker — auto-link transcriptions to Calendar.app events (W942 MEDIUM-1) ---
            "link_to_calendar_event": self._handle_link_to_calendar_event,  # явно связать запись с текущим событием Calendar
            "get_calendar_link": self._handle_get_calendar_link,  # получить сохранённую ссылку на событие Calendar для записи (W1030: canonical)
            "search_by_calendar_event": self._handle_search_by_calendar_event,  # поиск записей по названию события Calendar (W1030: canonical)
            # --- iMessage integration (Phase D.4) ---
            "send_imessage": self._apple_integration_svc.handle_send_imessage,  # отправить сообщение через iMessage/SMS через osascript
            "list_telegram_chats": self._apple_integration_svc.handle_list_telegram_chats,  # получить список доступных чатов Telegram через main Krab userbot
            # --- Phase 3: Call Session CRUD (outbound call automation) ---
            "call_session_create": self._call_session_service.handle_call_session_create,  # создать звонковую сессию
            "call_session_get": self._call_session_service.handle_call_session_get,  # получить сессию по id
            "call_session_list": self._call_session_service.handle_call_session_list,  # список сессий с опциональным фильтром по статусу
            "call_session_update_status": self._call_session_service.handle_call_session_update_status,  # переход статуса сессии
            "call_session_add_transcript": self._call_session_service.handle_call_session_add_transcript,  # добавить реплику в транскрипт
            "call_session_end": self._call_session_service.handle_call_session_end,  # завершить сессию: compute duration, total_cost
            "call_intervene": self._call_session_service.handle_call_intervene,  # VERIFIED: called from Swift (CallAutomationController) — оператор берёт управление, бот замолкает
            "call_resume_bot": self._call_session_service.handle_call_resume_bot,  # VERIFIED: called from Swift (CallAutomationController) — вернуть управление боту
            # --- STT hotwords (initial_prompt boost) ---
            "add_stt_hotword": self._stt_mgmt_svc.handle_add_stt_hotword,  # добавить термин в STT hotwords список
            "remove_stt_hotword": self._stt_mgmt_svc.handle_remove_stt_hotword,  # удалить термин из STT hotwords списка
            "list_stt_hotwords": self._stt_mgmt_svc.handle_list_stt_hotwords,  # получить весь список STT hotwords
            "clear_unavailable_models": self._handle_clear_unavailable_models,  # W1304: сбросить TTL blacklist недоступных STT-моделей
            # --- Recording bookmarks (Cmd+Shift+B) ---
            "add_bookmark": self._bookmarks.handle_add_bookmark,  # создать закладку на текущей позиции записи
            "list_bookmarks": self._bookmarks.handle_list_bookmarks,  # список закладок для item_id
            "list_all_bookmarks": self._bookmarks.handle_list_all_bookmarks,  # все активные закладки
            "delete_bookmark": self._bookmarks.handle_delete_bookmark,  # удалить закладку (tombstone)
            "jump_to_bookmark": self._bookmarks.handle_jump_to_bookmark,  # перейти к закладке (эмитит playback.seek)
            # --- Semantic search (opt-in, multilingual embeddings) ---
            "semantic_search": self._search_and_analysis_svc.handle_semantic_search,  # семантический поиск по истории через embeddings
            "semantic_search_status": self._search_and_analysis_svc.handle_semantic_search_status,  # статус семантического поиска: модель, индекс
            "semantic_search_reindex": self._search_and_analysis_svc.handle_semantic_search_reindex,  # переиндексировать всю историю
            # W1773: сброс зафиксированной ошибки загрузки SentenceTransformer (без него semantic_search молча мёртв)
            "semantic_search_reset": self._search_and_analysis_svc.handle_semantic_search_reset,
            # --- LM Studio model discovery ---
            "list_llm_models": self._llm_ops_svc.handle_list_llm_models,  # список моделей из LM Studio /v1/models (для dropdown в GUI)
            # --- Quick word replacement (Cmd+Shift+R) ---
            "replace_word_in_last_transcript": self._llm_ops_svc.handle_replace_word_in_last_transcript,  # заменить слово в последней транскрипции без перезаписи
            # --- Privacy audit log ---
            "get_privacy_audit_log": self._handle_get_privacy_audit_log,  # последние записи privacy audit log
            # W957 SECURITY: "clear_privacy_audit_log" INTENTIONALLY REMOVED from IPC dispatch.
            # Exposing audit-log destruction over unauthenticated IPC (IPC_SIGNING_ENABLED=False
            # by default) allows any local process to permanently erase the compliance trail.
            # PrivacyAuditLogger.clear() and _handle_clear_privacy_audit_log() are retained for
            # unit tests and explicit migration scripts ONLY — they must never be re-added here
            # without mandatory request signing + an explicit ALLOW_PRIVACY_AUDIT_CLEAR=true flag.
            # --- D.2.3: Scored STT routing decision ---
            "get_stt_routing_decision": self._stt_mgmt_svc.handle_get_stt_routing_decision,  # scored adapter selection debug
            "list_stt_engines": self._stt_mgmt_svc.handle_list_stt_engines,  # перечислить все STT-движки (включая отключённые) для model-picker GUI
            "list_voice_commands": self._stt_mgmt_svc.handle_list_voice_commands,  # статический справочник голосовых команд диктовки
            # --- Text snippet expansions (voice-triggered post-STT substitutions) ---
            "add_text_snippet": self._text_snippet_svc.handle_add_text_snippet,  # добавить/обновить пару trigger→expansion
            "list_text_snippets": self._text_snippet_svc.handle_list_text_snippets,  # получить все сниппеты
            "remove_text_snippet": self._text_snippet_svc.handle_remove_text_snippet,  # удалить сниппет по триггеру
            # --- Phonetic correction vocabulary (post-STT many-to-one variant→canonical substitutions) ---
            "add_phonetic_entry": self._phonetic_vocab_svc.handle_add_phonetic_entry,  # добавить/обновить запись {canonical, variants}
            "list_phonetic_entries": self._phonetic_vocab_svc.handle_list_phonetic_entries,  # получить все записи
            "remove_phonetic_entry": self._phonetic_vocab_svc.handle_remove_phonetic_entry,  # удалить запись по canonical
            # --- Speech pace analysis (W1048 F2) ---
            "analyze_speech_pace": self._handle_analyze_speech_pace,  # анализ темпа речи: wpm, cpm, категория, расчётное время чтения
            # --- Bulk reprocess (Wave 1044 — re-wired after Wave 65 removal) ---
            "bulk_reprocess_start": self._handle_bulk_reprocess_start,  # массовое перетранскрибирование с текущими настройками STT
            "bulk_reprocess_cancel": self._handle_bulk_reprocess_cancel,  # отменить текущий запуск bulk reprocess
            "bulk_reprocess_status": self._handle_bulk_reprocess_status,  # статус: активен ли cancel_event
            # --- W1284: TimelineExporter IPC (W1279 F3 LOW) ---
            "export_timeline_svg": self._handle_export_timeline_svg,  # экспорт таймлайна в SVG-файл
            "export_timeline_json": self._handle_export_timeline_json,  # экспорт таймлайна в JSON-файл
            "export_timeline_ical": self._handle_export_timeline_ical,  # экспорт таймлайна в iCalendar (.ics) файл
            # --- Default STT hotwords seed ---
            # --- Auto-Glossary IPC (W1104) ---
            "get_auto_glossary": self._handle_get_auto_glossary,  # W1104: возвращает текущий auto-glossary из кэша
            "refresh_auto_glossary": self._handle_refresh_auto_glossary,  # W1104: принудительно пересчитывает auto-glossary
            # --- Шифрование истории (Chunk 2) ---
            "set_history_encryption": self._handle_set_history_encryption,  # включить/выключить AES-256-GCM шифрование NDJSON-истории
            "get_encryption_status": self._handle_get_encryption_status,  # статус шифрования: enabled + available (наличие Keychain)
            "migrate_history_encryption": self._handle_migrate_history_encryption,  # зашифровать существующие plaintext-записи (at-rest migration)
            "get_history_encryption_status": self._handle_get_history_encryption_status,  # статистика шифрования: total/encrypted/plaintext/pct/migrating
            # --- Загрузка STT-моделей (fresh-install unblock) ---
            "download_stt_model": self._handle_download_stt_model,  # запустить фоновую загрузку STT-модели из HuggingFace
            "get_stt_model_status": self._handle_get_stt_model_status,  # статус кэша/загрузки STT-модели
            "cancel_stt_model_download": self._handle_cancel_stt_model_download,  # отменить текущую фоновую загрузку STT-модели
            # --- Privacy Dashboard (aggregate view) ---
            "get_privacy_dashboard": self._handle_get_privacy_dashboard,  # агрегированный дашборд privacy/security: режим, шифрование, хранилище, retention, audit
            # --- Auto-calibration: hardware profile + STT recommendation ---
            "get_hardware_profile": self._handle_get_hardware_profile,  # chip/RAM/cores/tier для автокалибровки
            "get_calibration_recommendation": self._handle_get_calibration_recommendation,  # рекомендация STT-модели по tier+mic
        }

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Обрабатывает один JSON-запрос и возвращает JSON-ответ.

        Таблица диспетчеризации строится один раз в ``__init__``
        (``self._dispatch_table``) — здесь только O(1) lookup, без перестройки.
        """
        request_id = payload.get("id")
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, "invalid_params", "Параметр params должен быть объектом")

        handler = self._dispatch_table.get(method)
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

        _t0 = time.monotonic()
        try:
            result = handler(params)
            response = {"id": request_id, "ok": True, "result": result}
        except IpcOperationalError as exc:
            # Genuine operational failure (remote service down, disk/IO error) —
            # stays loud (internal_error + Sentry), not downgraded to invalid_request.
            logger.exception("Операционный сбой метода %s", method)
            response = self._error(request_id, "internal_error", str(exc))
        except (ValueError, RuntimeError) as exc:
            # Handlers deliberately raise ValueError/RuntimeError for EXPECTED
            # conditions — a missing/invalid param or a not-found item
            # ("Параметр id обязателен", "Элемент не найден: ..."). RuntimeError is
            # this codebase's dominant validation idiom (~76 such raises). These are
            # normal user outcomes, not internal failures, so surface a semantic
            # `invalid_request` code and log at WARNING. The previous bare
            # `except Exception` turned them into `internal_error` +
            # logger.exception (ERROR + traceback → Sentry), so a stale-id summarize
            # click or a malformed param looked like a backend crash. Genuine bugs
            # raise AttributeError/KeyError/TypeError/IndexError/... (e.g. the
            # HistoryItem-vs-dict crash raised AttributeError) and still fall through
            # to the internal_error path below — they remain loud (ERROR + Sentry).
            logger.warning("Метод %s отклонён (invalid_request): %s", method, exc)
            response = self._error(request_id, "invalid_request", str(exc))
        except Exception as exc:
            logger.exception("Ошибка метода %s", method)
            response = self._error(request_id, "internal_error", str(exc))

        # Audit log — пропускаем в privacy_mode (настройка считывается из кэша)
        try:
            _privacy_on = bool(self._get_runtime_setting("privacy_mode_enabled", False))
            if not _privacy_on:
                self._audit_logger.log_request(
                    method=method,
                    params=params if isinstance(params, dict) else {},
                    result=response,
                    duration_ms=(time.monotonic() - _t0) * 1000,
                )
        except Exception:
            pass  # audit logging никогда не должен ронять IPC-ответ

        return response

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

    def _handle_purge_all_data(self, params: dict[str, Any]) -> dict[str, Any]:
        """Privacy-purge с guard'ом авто-резервного копирования (wave-25 B2).

        Тонкая обёртка над HistoryService.handle_purge_all_data. Замораживает
        AutoBackupManager на время очистки, чтобы фоновый/оппортунистический
        backup-цикл не пересоздал backups/ с PII сразу после rmtree() в purge-теле
        (TOCTOU). set_purged() взводится ДО очистки (и сам удаляет backups/);
        clear_purged() снимается в finally ПОСЛЕ завершения всех wipe-шагов, чтобы
        будущие бэкапы возобновились. Вся остальная логика purge не тронута.

        wave-26 MED: после того как HistoryService удалит hotwords.json с диска,
        очищаем in-memory коллекции HotwordDetector — иначе горячие слова (имена,
        термины, ПДн) выживают в RAM до перезапуска и доступны через check_hotwords IPC.
        ``_hotword_detector`` живёт в BackendService, а не в HistoryService, поэтому
        wire сделан здесь, а не в handle_purge_all_data — намеренно (см. комментарий
        в audit_inmemory_purge_coverage.py).
        """
        self._auto_backup.set_purged()
        try:
            result = self._history.handle_purge_all_data(params)
        finally:
            self._auto_backup.clear_purged()
        # wave-26 MED: wipe in-memory hotwords after disk file was deleted by history purge.
        # Guard: only clear if the purge actually ran (confirm check passed → ok key present
        # and is not False).  If confirm was missing, handle_purge_all_data returned early
        # with ok=False and nothing was deleted — do not clear in-memory state.
        if isinstance(result, dict) and result.get("ok") is not False:
            try:
                self._hotword_detector.clear()
            except Exception:
                import logging as _logging
                _logging.getLogger("KrabEar.BackendService").warning(
                    "purge_all_data: hotword_detector.clear() failed", exc_info=True
                )
            # Wave-30 MED: wipe in-memory transcription queue (file_path + result fields)
            # after history purge.  _transcription_queue lives in BackendService (not
            # HistoryService), so the clear is wired here — same pattern as _hotword_detector.
            # Documented in audit_inmemory_purge_coverage.py registry comment.
            try:
                self._transcription_queue.clear()
            except Exception:
                import logging as _logging
                _logging.getLogger("KrabEar.BackendService").warning(
                    "purge_all_data: transcription_queue.clear() failed", exc_info=True
                )
            # wave-41 MED: wipe usage_stats.json from disk + reset in-memory counters.
            # UsageTracker._stats_file survives purge otherwise — daily_history / streak /
            # peak_day all contain indirect usage-pattern PII.
            try:
                self._usage_tracker.clear_all()
            except Exception:
                import logging as _logging
                _logging.getLogger("KrabEar.BackendService").warning(
                    "purge_all_data: usage_tracker.clear_all() failed", exc_info=True
                )
        return result

    def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к HealthCheckService.handle_ping (W1690)."""
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
        """Возвращает ежедневную статистику использования: записи, длительность, слова.

        wave-25 MED: ранее тело было пустым (dead stub → None), и IPC-ответ был
        бессмысленным. Делегируем реальному UsageTracker.get_usage_stats().

        wave-41 MED: добавлен privacy gate — stats раскрывают паттерны использования
        (количество записей, длительность, streak, пиковые дни), что является
        косвенными метаданными в privacy_mode.
        """
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
        return self._usage_tracker.get_usage_stats()

    def _handle_list_normalization_profiles(self, params: dict) -> dict:
        """Возвращает список всех профилей нормализации текста."""
        return {"profiles": self._norm_profiles.list_profiles()}

    def _handle_add_normalization_profile(self, params: dict) -> dict:
        """Добавляет пользовательский профиль нормализации текста."""
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        rules = list(params.get("rules", []))
        description = str(params.get("description", ""))
        overwrite = bool(params.get("overwrite", False))
        profile = self._norm_profiles.add_profile(name, rules, description, overwrite=overwrite)
        return {"profile": profile.to_dict()}

    def _handle_remove_normalization_profile(self, params: dict) -> dict:
        """Удаляет пользовательский профиль нормализации текста."""
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        removed = self._norm_profiles.remove_profile(name)
        return {"removed": removed, "name": name}

    def _handle_apply_normalization_profile(self, params: dict) -> dict:
        """Применяет профиль нормализации к тексту и возвращает результат."""
        text = str(params.get("text", ""))
        profile_name = str(params.get("profile_name", "")).strip()
        if not profile_name:
            raise ValueError("Параметр 'profile_name' обязателен")
        normalized = self._norm_profiles.apply_profile(text, profile_name)
        return {"text": normalized, "profile_name": profile_name}

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

    def _handle_clear_translation_cache(self, params: dict[str, Any]) -> dict[str, Any]:
        """Очищает персистентный LRU-кэш переводов (W1429).

        Сбрасывает оба слоя: in-memory translator._cache и disk-файл translation_cache.json.
        Возвращает количество записей до очистки для диагностики.
        """
        entries_before = 0
        if self._translation_cache is not None:
            stats = self._translation_cache.get_stats()
            entries_before = stats.get("entries", 0)
            self._translation_cache.clear()
        # Также сбрасываем in-memory LRU-кэш транслятора
        self.translator.clear_cache()
        return {"ok": True, "entries_cleared": entries_before}

    def _handle_get_auto_glossary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущий auto-glossary из кэша (без пересчёта).

        Privacy guard: в режиме privacy_mode_enabled возвращает пустой список
        (история недоступна для извлечения терминов).

        Returns:
            {"ok": True, "terms": [...], "count": N, "from_cache": True}
        """
        settings_dict = self._settings_svc.cached_settings()
        if settings_dict.get("privacy_mode_enabled"):
            return {"ok": True, "terms": [], "count": 0, "from_cache": False}

        terms = self._auto_glossary.get_cached()
        return {"ok": True, "terms": terms, "count": len(terms), "from_cache": True}

    def _handle_refresh_auto_glossary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Принудительно пересчитывает auto-glossary из истории транскрибаций.

        Privacy guard: в режиме privacy_mode_enabled возвращает пустой список.

        params (optional):
            window_days: int — горизонт истории в днях (default 7).
            top_n: int — максимальное число терминов (default 30).

        Returns:
            {"ok": True, "terms": [...], "count": N, "refreshed": True}
        """
        settings_dict = self._settings_svc.cached_settings()
        if settings_dict.get("privacy_mode_enabled"):
            return {"ok": True, "terms": [], "count": 0, "refreshed": False}

        window_days = int(params.get("window_days", 7))
        top_n = int(params.get("top_n", 30))

        terms = self._auto_glossary.build(
            window_days=window_days,
            top_n=top_n,
            force=True,
        )
        return {"ok": True, "terms": terms, "count": len(terms), "refreshed": True}

    def _handle_get_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к HealthCheckService.handle_get_diagnostics (W1690)."""
        return self._health_check_svc.handle_get_diagnostics(params)

    def _handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к HealthCheckService.handle_health_check (W1690)."""
        return self._health_check_svc.handle_health_check(params)

    # ------------------------------------------------------------------
    # Phase B.1 — error bus + LLM probe handlers
    # ------------------------------------------------------------------

    def _handle_list_recent_errors(self, params: dict) -> dict:
        """Возвращает до *limit* последних KrabError из ring-буфера ErrorBus.

        ``since_seq`` (опционально) включает поллинг-контракт для агента:
        возвращает только записи с seq > since_seq + ``latest_seq`` для
        следующего опроса (SSE между IPC- и REST-процессами не работает —
        см. ErrorBus.list_recent_since / native ErrorBusPoller.swift).
        """
        limit = int(params.get("limit", 200))
        if "since_seq" in params:
            since_seq = int(params.get("since_seq", 0))
            items, latest_seq = self._error_bus.list_recent_since(since_seq, limit)
        else:
            items = self._error_bus.list_recent(limit)
            latest_seq = self._error_bus.latest_seq()
        return {
            "errors": [item.model_dump(mode="json") for item in items],
            "latest_seq": latest_seq,
        }

    def _handle_clear_recent_errors(self, params: dict) -> dict:
        """Очищает ring-буфер и dedupe-состояние ErrorBus. Возвращает количество удалённых записей."""
        n = self._error_bus.clear()
        return {"cleared": n}

    def _handle_get_audit_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние записи IPC audit log для операторов/отладки.

        Параметры:
            days_back — количество дней для выборки (default 7, range 1–90).
            limit     — максимальное число записей (default 200).

        Возвращает:
            entries — список записей {ts, method, params_keys, success, duration_ms}.
            reason  — «privacy_mode» когда данные недоступны.
        """
        # Privacy mode: возвращаем пустой ответ без утечки метаданных
        if self._cached_settings().get("privacy_mode_enabled", False):
            return {"ok": True, "entries": [], "reason": "privacy_mode"}

        raw_days = params.get("days_back", 7)
        try:
            days_back = int(raw_days)
        except (TypeError, ValueError):
            days_back = 7
        days_back = max(1, min(days_back, 90))

        limit = int(params.get("limit", 200))
        limit = max(1, min(limit, 1000))

        entries = self._audit_logger.get_audit_log(limit=limit)
        return {"ok": True, "entries": entries}

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
        try:
            proc_iter = list(psutil.process_iter())
        except (PermissionError, SystemError, OSError) as exc:
            # Wave 490: Sequoia KERN_PROCARGS2 blocks process_iter at the top level.
            # Push system.proc_cmdline_permission and return gracefully.
            self._push_proc_cmdline_permission_error(exc)
            return {"ok": True, "processes": []}
        for proc in proc_iter:
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

    def _push_proc_cmdline_permission_error(self, exc: Exception) -> None:
        """Push system.proc_cmdline_permission error to error_bus. Never raises.

        Wave 490: Sequoia KERN_PROCARGS2 blocks psutil.process_iter() with
        PermissionError/SystemError. Push once per hour (dedupe_seconds=3600).
        """
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("system.proc_cmdline_permission", {})
            err = KrabError(
                severity="error",
                component="system",
                code="system.proc_cmdline_permission",
                message_user=entry.get(
                    "user_msg_ru",
                    "Не удалось прочитать список процессов (Sequoia блокирует KERN_PROCARGS2).",
                ),
                message_debug=f"psutil.process_iter raised {type(exc).__name__}: {exc}",
                timestamp=datetime.now(timezone.utc),
                context={"exc_type": type(exc).__name__, "exc_msg": str(exc)},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            if hasattr(self, "_error_bus") and self._error_bus is not None:
                self._error_bus.push(err)
        except Exception:
            pass  # never raise from error reporting path

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
        """Делегирует к HealthCheckService.handle_handshake (W1690)."""
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
        """Делегирует к HealthCheckService.handle_probe_llm_http (W1690)."""
        return self._health_check_svc.handle_probe_llm_http(params)

    def _handle_apply_recommended_setup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к SettingsService.handle_apply_recommended_setup, инжектируя
        probe-колбэки (LM Studio ping + SenseVoice HF-кэш проверка) — A1 план
        docs/superpowers/plans/2026-07-07-recommended-setup.md, Задача 1 Шаг 4.

        probe_llm_fn — 0-arg callable по контракту SettingsService (см. Задача 2 плана);
        HealthCheckService.handle_probe_llm_http требует позиционный params, поэтому
        оборачивается лямбдой с пустым dict, а не передаётся как bound method напрямую."""
        return self._settings_svc.handle_apply_recommended_setup(
            params,
            probe_llm_fn=lambda: self._health_check_svc.handle_probe_llm_http({}),
            sensevoice_cached_fn=lambda: self._model_downloader.get_status(
                "FunAudioLLM/SenseVoiceSmall"
            ).get("cached", False),
        )

    def _handle_get_shutdown_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус последнего graceful shutdown.

        Returns:
            dict с ключами: clean (bool|None), last_shutdown_time (str|None),
            shutdown_in_progress (bool).
        """
        return self._shutdown_handler.get_shutdown_status()

    def _handle_get_startup_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к HealthCheckService.handle_get_startup_diagnostics (W1690)."""
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

        Privacy gate (wave-37): когда privacy_mode_enabled=True возвращает пустой
        ответ — агрегированная активность записей раскрывает паттерны использования
        в режиме приватности. Схема ключей совпадает с нормальным ответом.
        """
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {
                "ok": False,
                "reason": "privacy_mode_active",
                "total_count": 0,
                "total_duration_sec": 0.0,
                "today_count": 0,
                "today_duration_sec": 0.0,
                "week_count": 0,
                "week_duration_sec": 0.0,
                "avg_duration_sec": 0.0,
                "most_used_lang": "",
                "lang_distribution": [],
                "llm_applied_count": 0,
                "llm_correction_rate": 0.0,
                "diarization_used_count": 0,
                "diarization_usage_rate": 0.0,
            }
        active = self.store._load_active_items_with_lock()

        now = datetime.now(timezone.utc)
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
        """Снимок метрик реального времени: сессия, LLM, call_assist, конфиг, MetricsCollector."""
        from backend.metrics_collector import metrics as _metrics_collector
        settings = self._cached_settings()

        # Active session info
        preview_active = self._recording_core_svc.preview_thread_alive

        # W1685 F3: include MetricsCollector snapshot (latency percentiles + confidence).
        # get_summary() is thread-safe and returns "waiting_data" status when empty.
        try:
            metrics_snapshot = _metrics_collector.get_summary()
        except Exception:
            metrics_snapshot = {"status": "unavailable", "total_requests": 0}

        return {
            "session": {
                "recording_active": bool(getattr(self.recorder, 'is_recording', False)),
                "preview_active": preview_active,
                # wave-1770 MED: mask preview_text_length in privacy mode (reveals recording activity).
                "preview_text_length": 0 if settings.get("privacy_mode_enabled") else len(self._recording_core_svc.preview_text),
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
            "metrics": metrics_snapshot,
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

    def _handle_get_privacy_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Агрегированный дашборд privacy/security — все поля в одном вызове.

        Агрегирует из существующих источников (не пересоздаёт их):
          - privacy_mode_enabled       → _get_runtime_setting
          - history_encryption_enabled → settings cache
          - storage                    → HistoryService.handle_get_storage_info
                                         + StateStore.get_history_stats (item_count)
          - retention                  → auto_cleanup_enabled / auto_cleanup_after_days settings
          - audit                      → PrivacyAuditLogger (counts + last_event_ts + by_type)
          - purge_available            → всегда True (handle_purge_all_data доступен через IPC)

        Privacy-safe: только счётчики/флаги/размеры — ни один транскрипт/словарь/
        псевдоним спикера не попадает в ответ. Хендлер читает privacy-метаданные,
        а не пользовательский контент, поэтому privacy-mode gate НЕ нужен.
        Каждый источник обёрнут в try/except — сбой одного не валит весь дашборд.

        Возвращает:
            privacy_mode        (bool)  — режим конфиденциальности.
            encryption_enabled  (bool)  — шифрование истории (AES-256-GCM).
            storage             (dict)  — item_count, history_bytes, history_file_size_mb,
                                          transcripts_count, transcripts_size_mb,
                                          total_bytes, total_data_mb.
            retention           (dict)  — auto_cleanup_enabled, auto_cleanup_after_days,
                                          auto_purge_enabled, auto_purge_retention_days.
            audit               (dict)  — total_events, last_event_ts, by_type.
            purge_available     (bool)  — всегда True.
        """
        result: dict[str, Any] = {
            "privacy_mode": False,
            "encryption_enabled": False,
            "storage": {},
            "retention": {},
            "audit": {},
            "purge_available": True,
        }

        # --- privacy_mode ---
        try:
            result["privacy_mode"] = bool(
                self._get_runtime_setting("privacy_mode_enabled", False)
            )
        except Exception:
            logger.exception("get_privacy_dashboard: ошибка чтения privacy_mode_enabled")

        # --- encryption_enabled: read flag directly from settings (no IPC cross-call) ---
        try:
            result["encryption_enabled"] = bool(
                self._get_runtime_setting("history_encryption_enabled", False)
            )
        except Exception:
            logger.exception(
                "get_privacy_dashboard: ошибка чтения history_encryption_enabled"
            )

        # --- storage: sizes from HistoryService + item count from StateStore ---
        try:
            storage_info = self._history.handle_get_storage_info({})
            item_count = 0
            try:
                stats = self.store.get_history_stats()
                item_count = int(stats.get("active_count", 0))
            except Exception:
                logger.warning(
                    "get_privacy_dashboard: не удалось получить item_count из StateStore"
                )
            result["storage"] = {
                "item_count": item_count,
                "history_bytes": storage_info.get("history_bytes", 0),
                "history_file_size_mb": storage_info.get("history_file_size_mb", 0.0),
                "transcripts_count": storage_info.get("transcripts_count", 0),
                "transcripts_size_mb": storage_info.get("transcripts_size_mb", 0.0),
                "total_bytes": storage_info.get("total_bytes", 0),
                "total_data_mb": storage_info.get("total_data_mb", 0.0),
            }
        except Exception:
            logger.exception("get_privacy_dashboard: ошибка получения storage info")
            result["storage"] = {
                "item_count": 0,
                "history_bytes": 0,
                "history_file_size_mb": 0.0,
                "transcripts_count": 0,
                "transcripts_size_mb": 0.0,
                "total_bytes": 0,
                "total_data_mb": 0.0,
            }

        # --- retention: auto_cleanup + auto_purge settings ---
        try:
            s = self._cached_settings()
            result["retention"] = {
                "auto_cleanup_enabled": bool(s.get("auto_cleanup_enabled", False)),
                "auto_cleanup_after_days": int(s.get("auto_cleanup_after_days", 365)),
                "auto_purge_enabled": bool(s.get("auto_purge_enabled", False)),
                "auto_purge_retention_days": int(s.get("auto_purge_retention_days", 90)),
            }
        except Exception:
            logger.exception("get_privacy_dashboard: ошибка чтения retention settings")
            result["retention"] = {
                "auto_cleanup_enabled": False,
                "auto_cleanup_after_days": 365,
                "auto_purge_enabled": False,
                "auto_purge_retention_days": 90,
            }

        # --- audit: summary counts from PrivacyAuditLogger (no PII, no transcript content) ---
        try:
            audit = get_privacy_audit_logger()
            total_events = audit.total_count()
            all_entries = audit.read_entries(limit=max(total_events, 1))
            last_event_ts: str | None = None
            by_type: dict[str, int] = {}
            for entry in all_entries:
                action = str(entry.get("action", "unknown"))
                by_type[action] = by_type.get(action, 0) + 1
                ts = entry.get("ts")
                if ts and (last_event_ts is None or ts > last_event_ts):
                    last_event_ts = ts
            result["audit"] = {
                "total_events": total_events,
                "last_event_ts": last_event_ts,
                "by_type": by_type,
            }
        except Exception:
            logger.exception("get_privacy_dashboard: ошибка чтения privacy audit log")
            result["audit"] = {
                "total_events": 0,
                "last_event_ts": None,
                "by_type": {},
            }

        return result

    def _handle_clear_privacy_audit_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет файл privacy audit log. Идемпотентен.

        WARNING (W957): Этот метод НЕ зарегистрирован в таблице IPC dispatch и недоступен
        через IPC. Оставлен только для unit-тестов и явных migration-скриптов.
        НЕ добавляй его обратно в dispatch без mandatory request signing и флага
        ALLOW_PRIVACY_AUDIT_CLEAR=true (W952 CRITICAL finding F-1).

        Returns:
            ok — всегда True.
        """
        audit = get_privacy_audit_logger()
        audit.clear()
        return {"ok": True}

    def _handle_clear_unavailable_models(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: clear_unavailable_models — сбрасывает TTL blacklist недоступных STT-моделей.

        Позволяет оператору/тесту вручную снять блокировку с адаптеров, занесённых
        в _unavailable_models, не перезапуская backend.  Полезно после:
          - смены профиля STT
          - установки нового адаптера (Parakeet, SenseVoice, WhisperX)
          - временного сбоя MLX GPU, который уже устранён

        Возвращает:
            count   — число удалённых записей.
            cleared — список {model_id, age_sec} для каждой удалённой записи.

        W1304/W1475: handler reverted by W1497 cherry-pick train, restored in W1534.
        """
        import time as _time

        transcriber = getattr(self, "transcriber", None)
        if transcriber is None:
            logger.warning("[service] clear_unavailable_models: transcriber is None")
            return {"count": 0, "cleared": [], "error": "transcriber not available"}

        engine = getattr(transcriber, "engine", None)
        if engine is None:
            logger.warning("[service] clear_unavailable_models: transcriber.engine is None")
            return {"count": 0, "cleared": [], "error": "engine not available"}

        unavail: dict[str, float] = getattr(engine, "_unavailable_models", {})
        now = _time.monotonic()
        cleared = [
            {"model_id": mid, "age_sec": round(now - ts, 2)}
            for mid, ts in list(unavail.items())
        ]
        unavail.clear()
        logger.info(
            "[service] clear_unavailable_models: сброшено %d записей",
            len(cleared),
            extra={"cleared": [c["model_id"] for c in cleared]},
        )
        return {"count": len(cleared), "cleared": cleared}

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

    def _start_preview_worker(self, quality_profile: str) -> bool:
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._start_preview_worker(quality_profile=quality_profile)

    def _stop_preview_worker(self) -> bool:
        """Delegated to RecordingCoreService."""
        return self._recording_core_svc._stop_preview_worker()

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

    def _handle_check_mic_noise(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pre-flight проверка микрофона: записывает короткий фрагмент и
        профилирует фоновый шум ДО реальной записи.

        Возвращает RMS/peak (как test_microphone) плюс вложенный профиль шума
        под ключом ``noise``: noise_type, noise_level_db, snr_db,
        frequency_profile, recommendations, suitable_for_stt.

        Переиспользует тот же механизм записи, что и test_microphone, и
        core.NoiseProfiler (работает по in-memory массиву — без временного файла).
        Шум — это окружающие метаданные, не производный от транскрипта контент,
        поэтому privacy-gate не нужен (как и у test_microphone).
        """
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

            from core.noise_profiler import NoiseProfiler
            noise = NoiseProfiler().profile(audio_flat, sample_rate).to_dict()

            return {
                "ok": True,
                "duration_sec": duration,
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "noise": noise,
                "devices": self._recording_core_svc._list_audio_inputs(),
            }
        except Exception as exc:
            logger.warning("check_mic_noise: ошибка записи/профилирования — %s", exc)
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
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
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

    def _handle_get_meeting_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_meeting_report — полный отчёт о встрече для одной записи истории.

        Оркестрирует TextProcessingService (summary) + SearchAndAnalysisService
        (action_items/decisions/questions) + прямой разбор speaker_turns из HistoryItem.

        Privacy gate (критично — возвращает производный от транскрипта контент):
        при privacy_mode_enabled=True возвращает ok=False + пустые поля.

        Params:
            id  — item_id истории (обязательный).
        Returns:
            {id, ok, summary, summary_is_llm, action_items, decisions, questions,
             speakers, speaker_count, word_count, ts, markdown, fallback_reason}
        """
        item_id = str(params.get("id", "")).strip()

        _empty = {
            "id": item_id,
            "ok": False,
            "summary": "",
            "summary_is_llm": False,
            "action_items": [],
            "decisions": [],
            "questions": [],
            "speakers": [],
            "speaker_count": 0,
            "word_count": 0,
            "ts": "",
            "markdown": "",
            "fallback_reason": "",
        }

        # Privacy gate — MUST be first (returns transcript-derived content).
        if self._get_runtime_setting("privacy_mode_enabled", False):
            result = dict(_empty)
            result["fallback_reason"] = "privacy_mode"
            return result

        if not item_id:
            result = dict(_empty)
            result["fallback_reason"] = "not_found"
            return result

        # Fetch HistoryItem from store.
        try:
            active_items = self.store._load_active_items_with_lock()
        except Exception:
            active_items = []

        target = next((it for it in active_items if it.id == item_id), None)
        if target is None:
            result = dict(_empty)
            result["fallback_reason"] = "not_found"
            return result

        item_ts = getattr(target, "ts", "") or ""
        item_text = getattr(target, "text", "") or ""
        word_count = len(item_text.split()) if item_text else 0

        fallback_reasons: list[str] = []

        # --- Summary (TextProcessingService) ---
        summary = ""
        summary_is_llm = False
        try:
            sum_result = self._text_processing_svc.handle_summarize_item({"id": item_id})
            summary = sum_result.get("summary", "") or ""
            summary_is_llm = bool(sum_result.get("llm", False))
        except Exception as exc:
            fallback_reasons.append(f"summary_failed:{type(exc).__name__}")

        # --- Action items / decisions / questions (SearchAndAnalysisService) ---
        action_items: list[str] = []
        decisions: list[str] = []
        questions: list[str] = []
        try:
            ai_result = self._search_and_analysis_svc.handle_extract_action_items({"id": item_id})
            raw_action_items = ai_result.get("action_items", []) or []
            # action_items may be list of dicts (ActionItem.to_dict()) or plain strings.
            for ai in raw_action_items:
                if isinstance(ai, dict):
                    action_items.append(str(ai.get("text", ai.get("description", str(ai)))))
                else:
                    action_items.append(str(ai))
            decisions = [str(d) for d in (ai_result.get("decisions", []) or [])]
            questions = [str(q) for q in (ai_result.get("questions", []) or [])]
            if ai_result.get("fallback_reason"):
                fallback_reasons.append(f"action_items:{ai_result['fallback_reason']}")
        except Exception as exc:
            fallback_reasons.append(f"action_items_failed:{type(exc).__name__}")

        # --- Speakers from speaker_turns ---
        speakers: list[dict] = []
        try:
            speaker_turns = getattr(target, "speaker_turns", None) or []
            if speaker_turns:
                agg: dict[str, dict] = {}
                for seg in speaker_turns:
                    if not isinstance(seg, dict):
                        continue
                    label = str(seg.get("speaker", "UNKNOWN"))
                    start = seg.get("start", 0.0)
                    end = seg.get("end", 0.0)
                    try:
                        start_f = float(start)
                        end_f = float(end)
                    except (TypeError, ValueError):
                        start_f, end_f = 0.0, 0.0
                    dur = end_f - start_f if math.isfinite(end_f - start_f) else 0.0
                    if dur < 0:
                        dur = 0.0
                    if label not in agg:
                        agg[label] = {"label": label, "turns": 0, "duration_sec": 0.0}
                    agg[label]["turns"] += 1
                    agg[label]["duration_sec"] += dur
                for entry in agg.values():
                    entry["duration_sec"] = (
                        entry["duration_sec"]
                        if math.isfinite(entry["duration_sec"])
                        else 0.0
                    )
                speakers = sorted(agg.values(), key=lambda x: x["label"])
        except Exception as exc:
            fallback_reasons.append(f"speakers_failed:{type(exc).__name__}")

        speaker_count = len(speakers)

        # --- Markdown digest ---
        def _fmt_dur(sec: float) -> str:
            sec = max(0.0, sec)
            m = int(sec) // 60
            s = int(sec) % 60
            return f"{m}:{s:02d}"

        md_lines = [f"# Встреча — {item_ts}", ""]
        if summary:
            md_lines += ["## Резюме", summary, ""]
        if action_items:
            md_lines.append("## Задачи")
            md_lines += [f"- {a}" for a in action_items]
            md_lines.append("")
        if decisions:
            md_lines.append("## Решения")
            md_lines += [f"- {d}" for d in decisions]
            md_lines.append("")
        if questions:
            md_lines.append("## Вопросы")
            md_lines += [f"- {q}" for q in questions]
            md_lines.append("")
        if speakers:
            md_lines.append("## Спикеры")
            for sp in speakers:
                md_lines.append(
                    f"- {sp['label']} — {sp['turns']} реплик, {_fmt_dur(sp['duration_sec'])}"
                )
            md_lines.append("")

        markdown = "\n".join(md_lines).rstrip() + "\n"

        return {
            "id": item_id,
            "ok": True,
            "summary": summary,
            "summary_is_llm": summary_is_llm,
            "action_items": action_items,
            "decisions": decisions,
            "questions": questions,
            "speakers": speakers,
            "speaker_count": speaker_count,
            "word_count": word_count,
            "ts": item_ts,
            "markdown": markdown,
            "fallback_reason": "; ".join(fallback_reasons),
        }

    def _handle_get_daily_insight(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает один наиболее релевантный инсайт за сегодня (W1274 F3).

        Privacy gate: если privacy_mode_enabled=True — возвращает пустой результат
        без обращения к истории записей.
        """
        if self._cached_settings().get("privacy_mode_enabled"):
            return {"insight": None, "privacy_mode": True}
        try:
            items = self.store._load_active_items_with_lock()
        except Exception:
            items = []
        insight = self._recording_insights.get_daily_insight(items)
        return {
            "insight": insight.to_dict() if insight is not None else None,
            "privacy_mode": False,
        }

    def _handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Делегирует к HealthCheckService.handle_check_integrity (W1690)."""
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

    def _handle_get_context_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние контекстной памяти STT.

        Params (опционально):
            max_words (int): максимальное кол-во контекстных слов (по умолчанию 20).
            last_n (int): кол-во последних транскрибаций для тем (по умолчанию 10).
            clear (bool): если true — очищает память перед возвратом результата.

        Privacy gate (wave-27/28): когда privacy_mode_enabled=True подавляет фактические
        слова и темы (производные от транскрипций), но возвращает size чтобы
        вызывающий код видел, что память не пуста.
        """
        if self._cached_settings().get("privacy_mode_enabled"):
            return {
                "context_words": [],
                "recent_topics": [],
                "size": self._context_memory.size(),
                "window_size": 50,
                "privacy_mode": True,
            }

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

    # ── Audio fingerprinting ─────────────────────────────────────────────────

    # ── Telegram Bridge ──────────────────────────────────────────────────────

    # ── Apple Notes / Reminders / Calendar / iMessage / Telegram integration ──
    # (Phase D.4) The IPC handlers send_to_telegram, list_telegram_chats,
    # create_apple_note, create_apple_reminder, create_calendar_event and
    # send_imessage now live exclusively in
    # backend/apple_integration_service.py (AppleIntegrationService); the
    # dispatch table routes each key straight to self._apple_integration_svc.
    # The dead in-class duplicates were removed (#47, W797 follow-up). The
    # `_escape_as_str` helper below is retained as the canonical AppleScript
    # escaper referenced by tests.

    @staticmethod
    def _escape_as_str(s: str) -> str:
        """Escape a string for safe embedding inside an AppleScript double-quoted string.

        Backslashes MUST be doubled before quotes so that a trailing backslash
        cannot cancel the closing-quote escape (W1028-F5 / W944 fix).
        Also strips control characters (CR, LF, NUL) that would break the script.

        Defensive: non-str input is coerced via ``str()`` so a numeric/None param
        (e.g. a JSON number in ``title``) cannot raise inside this security helper
        and leak an unsanitised value downstream (W1442 restored the W942 coercion).
        """
        if not isinstance(s, str):
            s = str(s)
        s = re.sub(r'[\r\n\x00]', ' ', s)
        s = s.replace('\\', '\\\\')  # backslash FIRST — prevents Stand\" → Stand\\"
        s = s.replace('"', '\\"')
        return s

    # ── CalendarLinker IPC handlers (W942 MEDIUM-1) ─────────────────────────

    def _handle_link_to_calendar_event(self, params: dict) -> dict:
        """Явно связывает запись истории с активным событием Calendar.app.

        params:
          history_item_id: str (required) — id записи в истории
          at_time: str | None (optional) — ISO 8601 момент записи; по умолчанию now()

        Returns:
          {"ok": bool, "calendar_event": dict | None, "skipped": bool, "reason": str | None}

        Поведение:
        - Если calendar_link_enabled=False → skipped=True, reason="disabled".
        - Если privacy_mode_enabled=True → skipped=True, reason="privacy_mode".
        - TCC denial / timeout → soft-fail (ok=True, calendar_event=None, reason="tcc_denied"|"timeout").
        - Если событие найдено — сохраняет в StateStore и возвращает его.
        """
        item_id = str(params.get("history_item_id", "")).strip()
        if not item_id:
            return {"ok": False, "error": "history_item_id is required"}

        # Privacy mode guard — calendar event titles are sensitive
        if self._get_runtime_setting("privacy_mode_enabled", False):
            logger.debug(
                "link_to_calendar_event: пропуск — privacy_mode включён",
                extra={"item_id": item_id},
            )
            return {"ok": True, "calendar_event": None, "skipped": True, "reason": "privacy_mode"}

        # Feature flag guard
        if not self._get_runtime_setting("calendar_link_enabled", False):
            logger.debug(
                "link_to_calendar_event: пропуск — calendar_link_enabled=False",
                extra={"item_id": item_id},
            )
            return {"ok": True, "calendar_event": None, "skipped": True, "reason": "disabled"}

        # Parse optional at_time
        at_time = None
        at_time_raw = params.get("at_time")
        if at_time_raw:
            try:
                from datetime import datetime as _dt
                at_time = _dt.fromisoformat(str(at_time_raw))
            except (ValueError, TypeError):
                pass  # fall through to now()

        # Query Calendar — CalendarLinker already does all error-handling internally
        try:
            event = self._calendar_linker.find_active_event(at_time=at_time)
        except Exception as exc:
            logger.warning(
                "link_to_calendar_event: неожиданная ошибка CalendarLinker",
                extra={"item_id": item_id, "error": str(exc)},
            )
            return {"ok": True, "calendar_event": None, "skipped": False, "reason": "error"}

        if event is None:
            return {"ok": True, "calendar_event": None, "skipped": False, "reason": "no_active_event"}

        # Persist the link — best-effort; StateStore validates item_id exists
        try:
            saved = self.store.update_history_item_calendar(item_id, event)
        except Exception as exc:
            logger.warning(
                "link_to_calendar_event: ошибка сохранения в StateStore",
                extra={"item_id": item_id, "error": str(exc)},
            )
            saved = False

        logger.info(
            "link_to_calendar_event: %s → event found",
            item_id,
            extra={"item_id": item_id, "found": True, "saved": saved},
        )
        return {"ok": True, "calendar_event": event, "skipped": False, "reason": None}

    def _handle_get_calendar_link(self, params: dict) -> dict:
        """Возвращает сохранённое событие Calendar для записи или None.

        params:
          history_item_id: str (required)

        Returns:
          {"ok": bool, "calendar_event": dict | None}

        Note: W947 introduced this as _handle_get_calendar_link_v2; W1030 renamed
        to canonical form _handle_get_calendar_link.
        """
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
        item_id = str(params.get("history_item_id", "")).strip()
        if not item_id:
            return {"ok": False, "error": "history_item_id is required"}
        try:
            event = self.store.get_history_item_calendar(item_id)
        except Exception as exc:
            logger.warning(
                "get_calendar_link: ошибка StateStore",
                extra={"item_id": item_id, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "calendar_event": event}

    def _handle_search_by_calendar_event(self, params: dict) -> dict:
        """Ищет записи, связанные с событием Calendar по подстроке в названии.

        params:
          event_title: str (required, пустая строка = все)

        Returns:
          {"ok": bool, "results": [{"item_id": str, "calendar_event": dict}, ...]}

        Note: W947 introduced this as _handle_search_by_calendar_event_v2; W1030 renamed
        to canonical form _handle_search_by_calendar_event.
        """
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
        event_title = str(params.get("event_title", ""))
        try:
            results = self.store.search_by_calendar_event(event_title)
        except Exception as exc:
            logger.warning(
                "search_by_calendar_event: ошибка StateStore",
                extra={"event_title": event_title, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "results": results}

    # ── Phase 3: Call Session CRUD ───────────────────────────────────────────

    # ── Timeline view ────────────────────────────────────────────────────────

    def _handle_get_learning_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_learning_stats — статистика прогресса изучения языка."""
        if self._get_runtime_setting('privacy_mode_enabled', False):
            return {'ok': False, 'reason': 'privacy_mode_active'}
        params_with_store = dict(params)
        params_with_store.setdefault("store", self.store)
        return self._language_learning.handle_get_learning_stats(params_with_store)

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
        # wave-42 MED: today_recordings_count + total_duration reveal activity patterns.
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active"}
        return self._cost_estimator.get_daily_cost_summary(self._usage_tracker)

    def _handle_estimate_batch_cost(self, params: dict) -> dict:
        """IPC: estimate_batch_cost — суммарная оценка стоимости пакетного импорта.

        Параметры:
            files — список объектов, каждый: {"duration_sec": float,
                    "quality": str, "features": dict}.

        Ответ: суммарные вычислительные затраты по всем файлам.
        """
        files = list(params.get("files") or [])
        return self._cost_estimator.estimate_batch_cost(files)

    # ── Abbreviation expander IPC handlers ────────────────────────────────────

    def _handle_get_smart_vocabulary_suggestions(self, params: dict) -> dict:
        """IPC: get_smart_vocabulary_suggestions — предложения для словаря STT."""
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": True, "suggestions": [], "reason": "privacy_mode_active"}  # W973 F4
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
        # wave-25 LOW: validate threshold before delegating. A NaN/Inf or negative value
        # makes `similarity >= threshold` always True → the whole history is reported as
        # one giant duplicate group (and downstream removal would be over-aggressive).
        if "threshold" in params:
            try:
                _thr = float(params["threshold"])
            except (TypeError, ValueError):
                return {"ok": False, "reason": "invalid_threshold"}
            if not math.isfinite(_thr) or _thr < 0 or _thr > 1:
                return {"ok": False, "reason": "invalid_threshold"}
            params["threshold"] = _thr
        params["_store"] = self.store
        params["_semantic_searcher"] = self._semantic_searcher
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

    # ------------------------------------------------------------------ #
    #  W1284 — TimelineExporter IPC handlers (W1279 F3 LOW)               #
    # ------------------------------------------------------------------ #

    def _resolve_timeline_export_dir(self, output_dir: str | None) -> "Path":
        """Резолвит директорию экспорта с проверкой allowlist.

        Допустимые корни: data_dir, home, /tmp, tempfile.gettempdir().
        При None возвращает <data_dir>/exports/timeline.
        Бросает ValueError при path traversal.
        """
        import tempfile
        from pathlib import Path

        if output_dir is None:
            out = Path(self.store.data_dir) / "exports" / "timeline"
            out.mkdir(parents=True, exist_ok=True)
            return out

        resolved = Path(output_dir).expanduser().resolve()
        allowed_roots = [
            Path(self.store.data_dir).resolve(),
            Path.home().resolve(),
            Path("/tmp").resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                f"output_dir вне разрешённых директорий: {resolved}"
            )
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    def _handle_analyze_speech_pace(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует темп речи по тексту и длительности аудио.

        Параметры:
            text         — транскрибированный текст (обязательно).
            duration_sec — длительность аудиозаписи в секундах (float, обязательно).

        Возвращает:
            words_per_minute           — слов в минуту.
            chars_per_minute           — символов в минуту.
            pace_category              — «slow» | «normal» | «fast» | «very_fast».
            estimated_reading_time_sec — расчётное время чтения при 150 wpm.
            word_count                 — количество слов.
            char_count                 — количество символов (без пробелов).
            duration_sec               — фактическая длительность записи.
        """
        text = str(params.get("text", ""))
        raw_dur = params.get("duration_sec")
        if raw_dur is None:
            return {"error": "duration_sec is required"}
        try:
            duration_sec = float(raw_dur)
        except (TypeError, ValueError):
            return {"error": "duration_sec must be a number"}
        report = self._speech_pace_analyzer.analyze(text, duration_sec)
        return report.as_dict()

    # ------------------------------------------------------------------
    # BulkReprocessor handlers (Wave 1044 — re-wired after Wave 65 removal)
    # ------------------------------------------------------------------

    def _handle_bulk_reprocess_start(self, params: dict) -> dict:
        """Запускает массовое перетранскрибирование истории с текущими настройками STT.

        Params:
            only_low_confidence (bool, optional): перетранскрибировать только записи с
                confidence < threshold (по умолчанию True).
            threshold (float, optional): порог confidence [0..1] (по умолчанию 0.7).
            dry_run (bool, optional): только подсчёт, без реального STT (по умолчанию False).
            task_id (str, optional): произвольный ID задачи для событий прогресса.

        Returns:
            dict: total, reprocessed, skipped, errors, cancelled.
        """
        # wave-36 MED: privacy gate must live HERE in the IPC dispatcher, not only in
        # BulkReprocessor.reprocess() — the reprocessor gate is conditional on `settings`
        # being passed (wave-35 added it as an optional kwarg).  The IPC path
        # (_handle_bulk_reprocess_start → self._bulk_reprocessor.reprocess()) never passes
        # `settings`, so the inner gate is dead on the production IPC path.  Always gate
        # at the IPC boundary first (matches the "privacy gate = IPC boundary" pattern).
        if self._get_runtime_setting("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active",
                    "total": 0, "reprocessed": 0, "skipped": 0, "errors": [], "cancelled": False}
        only_low_confidence = bool(params.get("only_low_confidence", True))
        try:
            threshold = float(params.get("threshold", 0.7))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_threshold"}
        # wave-25 MED: a NaN/Inf or out-of-range confidence threshold from the socket
        # defeats the only-low-confidence guard (NaN comparisons are always False →
        # every record would be reprocessed / no record would be skipped). Reject it.
        if not math.isfinite(threshold) or threshold < 0 or threshold > 1:
            return {"ok": False, "reason": "invalid_threshold"}
        dry_run = bool(params.get("dry_run", False))
        task_id = str(params.get("task_id", ""))
        # wave-25 HIGH: BulkReprocessor refuses (raises) while a recording is active to avoid
        # concurrent MLX GPU access (SIGSEGV, PR #71 class). Translate that into a structured
        # IPC response instead of letting the RuntimeError bubble as a generic error.
        try:
            return self._bulk_reprocessor.reprocess(
                only_low_confidence=only_low_confidence,
                threshold=threshold,
                dry_run=dry_run,
                task_id=task_id,
            )
        except RuntimeError as exc:
            if "active recording" in str(exc):
                logger.warning("bulk_reprocess_start отклонён: идёт активная запись")
                return {"ok": False, "reason": "recording_active"}
            raise

    def _handle_bulk_reprocess_cancel(self, params: dict) -> dict:
        """Запрашивает отмену текущего запуска BulkReprocessor.

        Returns:
            dict: {"ok": True}
        """
        self._bulk_reprocessor.cancel()
        return {"ok": True}

    def _handle_bulk_reprocess_status(self, params: dict) -> dict:
        """Возвращает статус BulkReprocessor: активен ли cancel_event.

        Returns:
            dict: {"cancel_requested": bool}
        """
        return {"cancel_requested": self._bulk_reprocessor._cancel_event.is_set()}

    def _handle_export_timeline_svg(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует таймлайн записей истории в SVG-файл.

        Параметры:
            output_dir  (str|None): директория для файла (None = <data_dir>/exports/timeline).
            group_by    (str):      гранулярность блоков: «hour»|«day»|«week» (по умолчанию «day»).
            limit       (int):      макс. записей для анализа (по умолчанию 500).
            width       (int):      ширина SVG в пикселях (по умолчанию 1200).
            height      (int):      высота SVG в пикселях (по умолчанию 400).

        Возвращает:
            path        (str): абсолютный путь к сохранённому SVG-файлу.
            blocks      (int): количество временных блоков.
        """
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}

        from pathlib import Path
        from datetime import datetime, timezone

        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}

        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))
        width = max(200, int(params.get("width", 1200)))
        height = max(100, int(params.get("height", 400)))

        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []

        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        # wave-1770 HIGH (defense-in-depth): pass privacy_mode to exporter so that
        # even if the IPC gate above were bypassed, the method would suppress content.
        svg_content = self._timeline_exporter.export_svg(
            block_dicts, width=width, height=height,
            privacy_mode=bool(settings.get("privacy_mode_enabled"))
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.svg"
        file_path = Path(out_dir) / filename
        file_path.write_text(svg_content, encoding="utf-8")

        return {"path": str(file_path), "blocks": len(blocks)}

    def _handle_export_timeline_json(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует таймлайн записей истории в структурированный JSON-файл.

        Параметры:
            output_dir  (str|None): директория для файла (None = <data_dir>/exports/timeline).
            group_by    (str):      гранулярность блоков: «hour»|«day»|«week» (по умолчанию «day»).
            limit       (int):      макс. записей для анализа (по умолчанию 500).

        Возвращает:
            path        (str): абсолютный путь к сохранённому JSON-файлу.
            blocks      (int): количество временных блоков.
        """
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}

        from pathlib import Path
        from datetime import datetime, timezone

        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}

        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))

        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []

        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        # wave-1770 HIGH (defense-in-depth): pass privacy_mode to exporter.
        json_content = self._timeline_exporter.export_json(
            block_dicts,
            privacy_mode=bool(settings.get("privacy_mode_enabled"))
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.json"
        file_path = Path(out_dir) / filename
        file_path.write_text(json_content, encoding="utf-8")

        return {"path": str(file_path), "blocks": len(blocks)}

    def _handle_export_timeline_ical(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует таймлайн записей истории в iCalendar (.ics) файл.

        Параметры:
            output_dir  (str|None): директория для файла (None = <data_dir>/exports/timeline).
            group_by    (str):      гранулярность блоков: «hour»|«day»|«week» (по умолчанию «day»).
            limit       (int):      макс. записей для анализа (по умолчанию 500).

        Возвращает:
            path        (str): абсолютный путь к сохранённому .ics-файлу.
            blocks      (int): количество временных блоков (VEVENT в файле).
        """
        settings = self._cached_settings()
        if settings.get("privacy_mode_enabled"):
            return {"error": {"code": "privacy_mode", "message": "Экспорт отключён в режиме приватности"}}

        from pathlib import Path
        from datetime import datetime, timezone

        output_dir_param = params.get("output_dir")
        try:
            out_dir = self._resolve_timeline_export_dir(output_dir_param)
        except ValueError as exc:
            return {"error": {"code": "invalid_path", "message": str(exc)}}

        group_by = str(params.get("group_by", "day")).strip()
        limit = max(1, min(int(params.get("limit", 500)), 5000))

        try:
            with self.store._lock():
                raw_items = self.store._load_active_items_unlocked()[:limit]
        except Exception:
            raw_items = []

        blocks = self._timeline_view.generate_timeline(raw_items, group_by=group_by)
        block_dicts = [b.to_dict() for b in blocks]
        # wave-1770 HIGH (defense-in-depth): pass privacy_mode to exporter.
        ical_content = self._timeline_exporter.export_ical(
            block_dicts,
            privacy_mode=bool(settings.get("privacy_mode_enabled"))
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"timeline_{ts}.ics"
        file_path = Path(out_dir) / filename
        file_path.write_text(ical_content, encoding="utf-8")

        return {"path": str(file_path), "blocks": len(blocks)}

    # ------------------------------------------------------------------
    # Шифрование истории (Chunk 2)
    # ------------------------------------------------------------------

    def _handle_get_encryption_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус шифрования истории.

        Returns:
            {"ok": True, "enabled": bool, "available": bool}

        ``available`` — True когда macOS Keychain доступен (``security`` CLI есть).
        Не создаёт ключ. Нет privacy gate (конфигурация, не данные транскрипций).
        """
        from backend.crypto_keystore import keychain_available
        enabled = bool(self._settings_svc.cached_settings().get("history_encryption_enabled", False))
        available = keychain_available()
        return {"ok": True, "enabled": enabled, "available": available}

    def _handle_set_history_encryption(self, params: dict[str, Any]) -> dict[str, Any]:
        """Включает или выключает AES-256-GCM шифрование NDJSON-истории.

        params:
            enabled: bool — True чтобы включить, False чтобы выключить.

        При включении: проверяет доступность Keychain через
        ``build_history_crypto()``; если недоступен — отказывает без изменения
        настроек и возвращает ``{"ok": False, "error": "keychain_unavailable"}``.
        При выключении: сразу сохраняет настройку (ключ из Keychain не удаляется).

        Returns:
            {"ok": True, "enabled": bool, "available": True}
            или
            {"ok": False, "error": "keychain_unavailable", "enabled": bool}
        """
        enabled = bool(params.get("enabled", False))

        if enabled:
            from backend.history_crypto import build_history_crypto
            crypto = build_history_crypto()
            if crypto is None:
                current = bool(
                    self._settings_svc.cached_settings().get("history_encryption_enabled", False)
                )
                return {
                    "ok": False,
                    "error": "keychain_unavailable",
                    "enabled": current,
                }

        result = self._settings_svc.handle_set_settings({"history_encryption_enabled": enabled})
        if not result.get("ok", True):
            return result
        return {"ok": True, "enabled": enabled, "available": True}

    def _handle_migrate_history_encryption(self, params: dict[str, Any]) -> dict[str, Any]:
        """Encrypt existing plaintext history.ndjson entries (at-rest migration).

        Starts migration in a background thread.  Calling while migration is active
        returns {ok: True, status: "already_running"}.  Calling after completion is
        idempotent (0 lines will be re-encrypted).

        Returns:
            {"ok": True, "status": "started"}
            {"ok": True, "status": "already_running"}
            {"ok": False, "status": "encryption_unavailable"}
        """
        if getattr(self, "_history_migration_running", False):
            return {"ok": True, "status": "already_running"}

        from backend.history_crypto import build_history_crypto
        if build_history_crypto() is None:
            return {"ok": False, "status": "encryption_unavailable"}

        def _run() -> None:
            from backend.event_bus import bus as _event_bus

            def _progress(total: int, done: int, encrypted: int, pct: int, status: str) -> None:
                try:
                    _event_bus.emit("history_encryption.migrate.progress", {
                        "total": total,
                        "done": done,
                        "encrypted": encrypted,
                        "pct": pct,
                        "status": status,
                    })
                except Exception:
                    pass

            try:
                result = self.store.migrate_history_encryption(progress_cb=_progress)
                _event_bus.emit("history_encryption.migrate.progress", {
                    "total": result.get("total", 0),
                    "done": result.get("total", 0),
                    "encrypted": result.get("encrypted", 0),
                    "pct": 100 if result.get("ok") else 0,
                    "status": "done" if result.get("ok") else result.get("reason", "error"),
                })
            except Exception:
                logger.exception("migrate_history_encryption: migration error")
            finally:
                self._history_migration_running = False

        import threading as _threading
        self._history_migration_running = True
        t = _threading.Thread(target=_run, daemon=True, name="history-enc-migration")
        t.start()
        return {"ok": True, "status": "started"}

    def _handle_get_history_encryption_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return encryption statistics for history.ndjson.

        Counts ENC1: lines vs plaintext only (signatures not content).
        No privacy gate needed: returns metadata only, not transcript text.

        Returns:
            {
                "ok": True,
                "enabled": bool,
                "total": int,
                "encrypted": int,
                "plaintext": int,
                "migrating": bool,
                "pct": int,
            }
        """
        status = self.store.get_history_encryption_status()
        status["ok"] = True
        status["migrating"] = getattr(self, "_history_migration_running", False)
        return status

    # ------------------------------------------------------------------
    # STT model download (fresh-install unblock)
    # ------------------------------------------------------------------

    def _handle_download_stt_model(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запускает фоновую загрузку STT-модели.

        params:
            model_id (str, optional): HuggingFace repo_id.
                Дефолт: settings.MODEL_BALANCED (mlx-community/whisper-large-v3-turbo).

        Returns:
            {"ok": True, "status": "started"|"already_cached"|"in_progress", "model_id": str}

        Raises ValueError если model_id явно задан, но пустой / не строка / слишком длинный.
        """
        from backend.model_downloader import MAX_MODEL_ID_LEN
        raw_model_id = params.get("model_id")
        if raw_model_id is not None:
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                raise ValueError("Параметр 'model_id' должен быть непустой строкой")
            if len(raw_model_id) > MAX_MODEL_ID_LEN:
                raise ValueError(
                    f"Параметр 'model_id' слишком длинный (макс. {MAX_MODEL_ID_LEN} символов)"
                )
            model_id = raw_model_id.strip()
        else:
            model_id = self._get_runtime_setting("MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")

        status = self._model_downloader.start_download(model_id)
        add_breadcrumb(
            category="stt",
            message="download_stt_model",
            data={"model_id": model_id, "status": status},
        )
        return {"ok": True, "status": status, "model_id": model_id}

    def _handle_get_stt_model_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус кэша/загрузки STT-модели.

        params:
            model_id (str, optional): HuggingFace repo_id.
                Дефолт: settings.MODEL_BALANCED.

        Returns:
            {
                "ok": True,
                "model_id": str,
                "cached": bool,
                "downloading": bool,
                "status": "idle"|"downloading"|"done"|"error"|"cancelled",
                "pct": float (0..100),
                "downloaded": int (bytes),
                "total": int (bytes),
                "error_msg": str,
            }

        NOTE: "path" field intentionally omitted (F2-LOW privacy: no absolute FS path).
        """
        from backend.model_downloader import MAX_MODEL_ID_LEN
        raw_model_id = params.get("model_id")
        if raw_model_id is not None:
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                raise ValueError("Параметр 'model_id' должен быть непустой строкой")
            if len(raw_model_id) > MAX_MODEL_ID_LEN:
                raise ValueError(
                    f"Параметр 'model_id' слишком длинный (макс. {MAX_MODEL_ID_LEN} символов)"
                )
            model_id = raw_model_id.strip()
        else:
            model_id = self._get_runtime_setting("MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")

        status_dict = self._model_downloader.get_status(model_id)
        return {"ok": True, **status_dict}

    def _handle_cancel_stt_model_download(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отменяет текущую фоновую загрузку STT-модели (F1-MED wave2).

        params:
            model_id (str, optional): HuggingFace repo_id.
                Дефолт: settings.MODEL_BALANCED.

        Returns:
            {"ok": True, "cancelled": bool, "model_id": str}
            cancelled=True означает, что загрузка активно шла и сигнал отмены отправлен.
            cancelled=False — загрузка не шла (не было нужды отменять).

        Raises ValueError если model_id явно задан, но пустой / не строка / слишком длинный.
        """
        from backend.model_downloader import MAX_MODEL_ID_LEN
        raw_model_id = params.get("model_id")
        if raw_model_id is not None:
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                raise ValueError("Параметр 'model_id' должен быть непустой строкой")
            if len(raw_model_id) > MAX_MODEL_ID_LEN:
                raise ValueError(
                    f"Параметр 'model_id' слишком длинный (макс. {MAX_MODEL_ID_LEN} символов)"
                )
            model_id = raw_model_id.strip()
        else:
            model_id = self._get_runtime_setting("MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")

        cancelled = self._model_downloader.cancel(model_id)
        logger.info(
            "cancel_stt_model_download",
            extra={"model_id": model_id, "cancelled": cancelled},
        )
        return {"ok": True, "cancelled": cancelled, "model_id": model_id}

    # ------------------------------------------------------------------
    # Handlers: Auto-calibration
    # ------------------------------------------------------------------

    def _handle_get_hardware_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает аппаратный профиль Mac для автокалибровки STT.

        Не требует параметров.  Не читает данные пользователя — только железо.
        Нет privacy gate.

        Returns::

            {
                "ok": True,
                "chip": str,            # Apple M4 Max / Intel Core i9 / unknown
                "ram_gb": int,          # объём памяти в ГБ
                "cores": int,           # логических CPU-ядер
                "is_apple_silicon": bool,
                "tier": "low"|"mid"|"high",
            }
        """
        from core.hardware_profile import detect_hardware_profile
        profile = detect_hardware_profile()
        return {"ok": True, **profile.to_dict()}

    def _handle_get_calibration_recommendation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Рекомендует STT-модель и движок на основе hardware tier.

        Не делает запись аудио — использует кэшированный профиль шума
        (ключи _last_mic_snr_db / _last_mic_suitable_for_stt в settings).
        Нет privacy gate.

        Returns::

            {
                "ok": True,
                "recommended_model": "balanced"|"max",
                "recommended_engine": str,
                "tier": "low"|"mid"|"high",
                "mic": {"snr_db": float, "suitable_for_stt": bool} | null,
                "rationale": str,
            }
        """
        from core.hardware_profile import detect_hardware_profile, TIER_HIGH, TIER_MID

        profile = detect_hardware_profile()
        tier = profile.tier

        if tier == TIER_HIGH:
            recommended_model = "max"
            rationale = (
                f"RAM {profile.ram_gb} GB (high tier) — модель max обеспечивает "
                "максимальную точность на этом железе."
            )
        elif tier == TIER_MID:
            recommended_model = "balanced"
            rationale = (
                f"RAM {profile.ram_gb} GB (mid tier) — модель balanced оптимальна: "
                "достаточная точность без перегрузки памяти."
            )
        else:  # TIER_LOW
            recommended_model = "balanced"
            rationale = (
                f"RAM {profile.ram_gb} GB (low tier) — рекомендуется balanced; "
                "запуск max-модели может исчерпать память."
            )

        recommended_engine = "mlx_whisper" if profile.is_apple_silicon else "whisper"

        mic_info: dict[str, Any] | None = None
        try:
            cached = self._settings_svc.cached_settings()
            snr = cached.get("_last_mic_snr_db")
            suitable = cached.get("_last_mic_suitable_for_stt")
            if snr is not None:
                _snr_f = float(snr) if math.isfinite(float(snr)) else 0.0
                mic_info = {"snr_db": _snr_f, "suitable_for_stt": bool(suitable)}
                if not mic_info["suitable_for_stt"]:
                    rationale += (
                        " Последняя проверка микрофона показала низкое SNR "
                        f"({snr:.1f} dB) — рекомендуется улучшить качество записи."
                    )
        except Exception:  # noqa: BLE001
            mic_info = None

        return {
            "ok": True,
            "recommended_model": recommended_model,
            "recommended_engine": recommended_engine,
            "tier": tier,
            "mic": mic_info,
            "rationale": rationale,
        }


# W1768: inline-дубликат IPCServer-класса УДАЛЁН. Каноничный, закалённый класс
# живёт в ``backend/ipc_server.py`` и импортируется выше (см. блок импортов
# рядом с ``backend.ipc_throttle``). ``main()`` ниже использует именно его —
# production теперь получает W1767 #1595 slow-loris guard.


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

    from logging.handlers import RotatingFileHandler as _RotatingFileHandler  # noqa: PLC0415
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        _RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB — wave687 log rotation
            backupCount=3,
            encoding="utf-8",
        ),
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

    # W1634 / W1631 F2 HIGH: load persisted settings BEFORE init_sentry so that
    # privacy_mode_enabled from settings.json is respected at startup.
    # StateStore.load_settings() is a lightweight read-only JSON parse — safe to
    # call here before build_service() (which will reload and normalise later).
    _early_store = StateStore(data_dir=data_dir)
    _startup_settings: dict = _early_store.load_settings() or {}

    # Sentry / GlitchTip crash telemetry (no-op если DSN не задан).
    # W704: release string читается из Info.plist через get_release_string()
    # (priority: env KRAB_EAR_RELEASE → Info.plist → __version__.py).
    sentry_ok = init_sentry(
        dsn=settings.SENTRY_DSN or None,
        environment=settings.SENTRY_ENVIRONMENT,
        release=get_release_string(),
        settings=_startup_settings,
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

    # Даём metadata-handler ссылку на сервис, но не право перехватывать сигналы:
    # production-порядком IPC → workers → metadata владеет один finally ниже.
    service._ipc_server = server
    service._shutdown_handler.bind(service)

    def _signal_handler(signum: int, frame: Any) -> None:
        """Снять форензический контекст сигнала (R1 Task 5) и попросить
        accept-loop выйти; полный teardown выполнит finally ниже.

        R1 Task 8 амендмент (найдено живым e2e-смоком, 2026-07-24):
        GracefulShutdownHandler._signal_handler САМ по себе НИКОГДА не
        регистрируется как OS-обработчик сигнала в production — bind() (в
        отличие от legacy register()) намеренно не трогает регистрацию
        сигналов ОС, единственный владелец сигналов здесь. Без явного
        вызова _capture_signal_context()
        signal/recording_active/meeting_active в shutdown_info.json всегда
        оставались бы дефолтными (None/False) на КАЖДОМ реальном сигнале —
        unit-тесты этого не ловили, т.к. вызывали handler._signal_handler()
        напрямую, в обход этой функции.
        """
        del frame
        service._shutdown_handler._capture_signal_context(signum)
        server.request_stop_from_signal()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        server.serve_forever()
    finally:
        _shutdown_backend(
            service,
            server,
            service._shutdown_handler,
        )


if __name__ == "__main__":
    main()
