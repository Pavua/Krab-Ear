"""RecordingCoreService — lifecycle записи и оркестрация транскрибации.

Выделен из BackendService в Wave 172, чтобы изолировать самый крупный домен:
start/stop записи, состояние и аудиоустройства, синхронную/асинхронную
транскрибацию, прогресс/отмену и preview. Методы ``handle_*`` регистрируются
в таблице диспетчеризации ``BackendService.handle_request``.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import math
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from backend.auto_deduplication import AutoDeduplicator

import numpy as np

from core.config import settings as _cfg_settings
from backend.ipc_constants import (
    IPC_PREVIEW_THREAD_TIMEOUT_SEC,
    RT_PARTIAL_START_STOP_TIMEOUT_SEC,
)
from backend.job_tracker import JobTracker
from backend.observability import add_breadcrumb
from backend.realtime_partial import RealtimePartialTranscriber
from backend.recorder import AudioRecorderStopTimeout
from backend.realtime_silence_filter import RealtimeSilenceFilter
from backend.transcript_writer import TranscriptWriter
from contracts.registry import EventType
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.translation_events import TranslationCompleted, TranslationFailed
from backend.event_bus import bus as event_bus
from backend.models import DEFAULT_SETTINGS
from core.silence_constants import SILENCE_THRESHOLD_DB_PRESERVE_WHISPER
from core.utils import TextUtils

logger = logging.getLogger("KrabEar.Backend.RecordingCore")

# Memory Conductor: rewriter живёт только после записей, его idle меряется от
# последней STT-активности (спека v2.1 — внутрь rewriter не лезем).
_LAST_STT_ACTIVITY = {"ts": time.monotonic()}


def last_stt_activity_ts() -> float:
    return _LAST_STT_ACTIVITY["ts"]


def bump_stt_activity() -> None:
    """Отмечает "STT/rewriter только что использовались" — единая точка для
    ВСЕХ путей, реально прогоняющих аудио через транскрайбер (и, транзитивно,
    через LLM-rewriter пост-обработку внутри engine.transcribe).

    Memory Conductor MED-3 (адверсариальный гейт волны): раньше бамп на
    стоп-пути диктовки был гейтован ``brain_model and preload_enabled`` —
    настройками brain-preload, НЕ ФАКТОМ реальной STT-работы, а batch-импорт
    (``_transcribe_paths_core``) и CHUNK_STT живой встречи (``meeting_session_
    service._job_chunk_stt``) не бампали вовсе. Под enforce это давало
    выгрузку rewriter'а посреди активного использования → следующая вставка
    ловила синхронный ~90с self-heal load. Вызывается симметрично из ВСЕХ
    точек входа, где транскрайбер реально задействован: start_recording (об
    активности STT известно заранее), stop_recording (безусловно — STT точно
    отработает дальше по коду), batch-импорт (per-file) и meeting CHUNK_STT.
    """
    _LAST_STT_ACTIVITY["ts"] = time.monotonic()


# wave-25 MED: default auto-dedup similarity threshold. Used as the safe fallback when a
# persisted/overridden value is non-finite or outside [0.0, 1.0].
_DEFAULT_DEDUP_THRESHOLD = 0.9

# launchd даёт backend около 15 секунд на весь shutdown. Эти независимые
# бюджеты оставляют запас для остальных сервисов и финального flush.
_SHUTDOWN_RT_PARTIAL_TIMEOUT_SEC = 3.0
_SHUTDOWN_RSF_TIMEOUT_SEC = 2.5
_SHUTDOWN_RECORDER_TIMEOUT_SEC = 3.0
# Этот бюджет ограничивает только ожидание незавершённого start/setup.
# Остальные worker-ы сохраняют собственные независимые stop-таймауты ниже.
_SHUTDOWN_LIFECYCLE_LOCK_TIMEOUT_SEC = 1.5
# R2 Task 3: живые finalizing-хвосты не вытесняются. Восьмой может завершиться,
# а девятый fresh start временно получает уже известный recorder_stopping.
_MAX_FINALIZING_GENERATIONS = 8
# R2 Task 5: три terminal-ответа покрывают транспортный retry без бессрочного
# удержания транскриптов в RAM; TTL измеряется только монотонными часами.
_TERMINAL_CACHE_MAX = 3
_TERMINAL_CACHE_TTL_SEC = 300.0
# R2 Task 6: только эти категории разрешено выносить из lifecycle-протокола
# в широкую телеметрию. Произвольный source допустим для совместимости API,
# но может содержать PII и потому сворачивается в нейтральный ``other``.
_OWNER_TELEMETRY_ALLOWLIST = frozenset({
    "dictation",
    "meeting",
    "quick_capture",
})
# Client lease остаётся непрозрачным, но ограничение длины не даёт удерживать
# произвольные мегабайты во всех active-state ответах и снимках.
_START_REQUEST_ID_MAX_CHARS = 256

# R3 (спека 2026-08-13-incremental-preview-design.md): курсор вместо
# скользящего окна в _preview_loop. Зафиксированный префикс (committed_text)
# больше не перераспознаётся — превью распознаёт только НОВЫЙ хвост аудио
# (snapshot_range(cursor_sec, upto), тот же примитив, что MeetingSessionService
# ._job_chunk_stt уже использует для аккумулятора встреч).
_PREVIEW_MIN_TAIL_SEC = 0.9      # короче — ждать новых сэмплов, STT не звать
_PREVIEW_COMMIT_MIN_SEC = 3.0    # минимальный хвост, чтобы вообще фиксировать курсор
_PREVIEW_MAX_TAIL_SEC = 8.0      # форс-фиксация без паузы, если хвост дорос досюда
_PREVIEW_SILENCE_TAIL_SEC = 0.4  # окно RMS-проверки конца хвоста на тишину
# Порог тишины — тот же, что RealtimeSilenceFilter уже использует для
# STT-путей (сохраняет тихую речь/шёпот); новый порог "с потолка" не вводим.
_PREVIEW_SILENCE_THRESHOLD_AMP = 10.0 ** (SILENCE_THRESHOLD_DB_PRESERVE_WHISPER / 20.0)


def _sanitize_dedup_threshold(raw: Any) -> float:
    """Coerce *raw* into a valid auto-dedup similarity threshold in [0.0, 1.0].

    wave-25 MED: a negative auto_dedup_threshold (e.g. -1.0) made the guard
    ``similarity >= threshold`` always True → EVERY new recording was silently dropped
    as a duplicate (data-loss). A NaN/Inf was likewise unsafe. Values outside [0.0, 1.0]
    (or non-numeric) fall back to the safe default rather than being honoured.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "auto_dedup_threshold: нечисловое значение %r — откат к %.2f",
            raw, _DEFAULT_DEDUP_THRESHOLD,
        )
        return _DEFAULT_DEDUP_THRESHOLD
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        logger.warning(
            "auto_dedup_threshold: значение %r вне диапазона [0.0, 1.0] — откат к %.2f",
            value, _DEFAULT_DEDUP_THRESHOLD,
        )
        return _DEFAULT_DEDUP_THRESHOLD
    return value


class RecordingCoreService:
    """Recording lifecycle + transcription orchestration service.

    Constructor accepts all collaborators as keyword arguments so that
    BackendService can wire them at init time, and tests can inject fakes.
    """

    _lifecycle_init_lock = threading.Lock()

    def __init__(
        self,
        *,
        recorder: Any,
        transcriber: Any,
        translator: Any,
        store: Any,
        vocabulary: Any,
        settings_svc: Any,
        llm_rewriter: Any,
        auto_glossary: Any,
        semantic_searcher: Any,
        context_memory: Any,
        clipboard_history: list,
        auto_backup: Any,
        session_tracker: Any,
        action_items_extractor: Any,
        transcription_counter_ref: list,  # [int] mutable box so BackendService sees updates
        last_stt_engine_ref: list,        # [str|None] mutable box
        auto_deduplicator: "AutoDeduplicator | None" = None,
        rescue_dir: "Path | None" = None,
    ) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.translator = translator
        self.store = store
        self.vocabulary = vocabulary
        self._settings_svc = settings_svc
        self._llm_rewriter = llm_rewriter
        self._auto_glossary = auto_glossary
        self._semantic_searcher = semantic_searcher
        self._context_memory = context_memory
        self._clipboard_history = clipboard_history
        self._auto_backup = auto_backup
        self._session_tracker = session_tracker
        self._action_items_extractor = action_items_extractor
        self._transcription_counter_ref = transcription_counter_ref
        self._last_stt_engine_ref = last_stt_engine_ref
        self._auto_deduplicator = auto_deduplicator
        # R1: каталог continuous-spill (<data_dir>/rescue/). None → spill выключен
        # безусловно (напр. старые тесты, не знающие про R1). Доступ только под
        # _recording_lifecycle_lock (start и phase_a уже живут под ним).
        self._rescue_dir = rescue_dir
        self._active_spill: Any = None
        # R2: active-slot описывает только текущий physical capture. После
        # успешной phase A G1 переходит в finalizing-map, чтобы тяжёлый хвост
        # G1 не мешал старту G2, но оставался адресуемым для token-retry.
        self._active_generation: dict[str, Any] | None = None
        self._finalizing_generations: dict[str, dict[str, Any]] = {}
        self._generation_revision = 0
        # Terminal-cache хранит полный уже публичный stop-ответ, включая PII.
        # Epoch не даёт pre-purge хвосту заново опубликовать старый ответ после
        # clear_terminal_cache(); lock не берёт lifecycle-lock в обратную сторону.
        self._terminal_cache: OrderedDict[
            str,
            tuple[float, dict[str, Any]],
        ] = OrderedDict()
        self._terminal_cache_lock = threading.Lock()
        self._terminal_cache_epoch = 0

        # Wired by BackendService after init (same pattern as llm_rewriter._error_bus).
        self._error_bus: Any = None

        # W1776: late-inject BookmarkManager so phase_e can rebind live-recording
        # bookmarks from the temp session_tracker UUID to the final HistoryItem id.
        self._bookmarks: Any = None

        # 2026-07-12: late-inject AudioSelfHealer (same pattern as _error_bus /
        # _bookmarks above). handle_stop_recording feeds it every empty-result
        # outcome; None-guarded at every call site so tests that don't care
        # about self-heal need not construct one. See backend/audio_selfheal.py.
        self._audio_selfheal: Any = None

        # Serializes history persistence in phase_e to prevent double-write races
        self._persist_lock = threading.Lock()

        # Start setup и shutdown должны быть линейными: IPC обслуживает запросы
        # в отдельных потоках, поэтому close иначе мог забрать handle до того,
        # как параллельный start успел его опубликовать.
        self._recording_lifecycle_lock = threading.RLock()
        self._closed_event = threading.Event()

        # Preview worker state (owned by this service)
        self._preview_lock = threading.Lock()
        # wave-1770 MED (race): _start/_stop_preview_worker mutate _preview_thread
        # without coordination — concurrent start/stop can overwrite the handle before
        # join(), orphaning the daemon. A DEDICATED reentrant lock serialises the
        # thread-handle lifecycle. It must NOT be _preview_lock: _stop joins the worker
        # while the worker may be blocked acquiring _preview_lock to write _preview_text
        # → deadlock. RLock because _start_preview_worker calls _stop_preview_worker.
        self._preview_thread_lock = threading.RLock()
        self._preview_thread: threading.Thread | None = None
        self._preview_stop_event = threading.Event()
        self._preview_text: str = ""
        self._preview_duration_sec: float = 0.0
        self._preview_updated_at: float = 0.0
        self._preview_error_count: int = 0
        self._preview_error_last_reset_ts: float | None = None
        # F2 (спека 2026-08-12): текущий бэкофф превью из-за переполнения
        # аудиобуфера recorder-а (0.0 — бэкофф выключен, штатный режим).
        # Инициализируется заново на КАЖДЫЙ вход в _preview_loop (свежий
        # поток на каждую запись) — здесь только стартовое значение и
        # публичная точка диагностики между запусками.
        self._preview_overflow_backoff_sec: float = 0.0

        # Realtime partial transcriber state
        # wave-27 MED (race): concurrent start_recording/stop_recording mutate
        # _rt_partial / _rsf without coordination — an interleaving where stop()
        # reads None while start() is mid-construction (or vice-versa) can orphan
        # the RealtimePartialTranscriber daemon thread (started but its handle
        # overwritten with None → never stopped). All lifecycle transitions of
        # _rt_partial and _rsf (construct+assign on start, stop()+None on stop)
        # are made atomic under this lock.
        self._rt_lock = threading.Lock()
        self._rt_partial: RealtimePartialTranscriber | None = None
        self._rt_session_id: str = ""

        # Realtime silence filter state (W1325 F1 HIGH wiring)
        self._rsf: RealtimeSilenceFilter | None = None
        self._last_silence_ranges: list[tuple[float, float]] = []

        # Async transcription jobs
        self._job_tracker = JobTracker()

        # Allow test monkey-patching of audio input enumeration
        self._list_audio_inputs = RecordingCoreService._list_audio_inputs_static

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _ensure_recording_lifecycle_state(
        self,
    ) -> tuple[Any, threading.Event]:
        """Вернуть lifecycle-примитивы для runtime и legacy ``__new__``-дублей.

        Старые узкие тесты намеренно обходят ``__init__`` и заполняют только
        зависимости исследуемого handler-а. Ленивая инициализация сохраняет
        этот контракт, не ослабляя потокобезопасность runtime-gate.
        """
        with type(self)._lifecycle_init_lock:
            lifecycle_lock = getattr(self, "_recording_lifecycle_lock", None)
            if lifecycle_lock is None:
                lifecycle_lock = threading.RLock()
                self._recording_lifecycle_lock = lifecycle_lock
            closed_event = getattr(self, "_closed_event", None)
            if closed_event is None:
                closed_event = threading.Event()
                self._closed_event = closed_event
        return lifecycle_lock, closed_event

    def _get_runtime_setting(self, key: str, default: Any) -> Any:
        """Read a setting from the live cached_settings dict."""
        try:
            return self._settings_svc.cached_settings().get(key, default)
        except Exception:
            return default

    # ------------------------------------------------------------------ #
    # Public accessors (BackendService may read these for diagnostics)    #
    # ------------------------------------------------------------------ #

    @property
    def preview_text(self) -> str:
        with self._preview_lock:
            return self._preview_text

    @property
    def preview_duration_sec(self) -> float:
        with self._preview_lock:
            return self._preview_duration_sec

    @property
    def preview_error_count(self) -> int:
        return self._preview_error_count

    @property
    def preview_error_last_reset_ts(self) -> float | None:
        return self._preview_error_last_reset_ts

    @property
    def preview_overflow_backoff_sec(self) -> float:
        """F2 (спека 2026-08-12): текущий бэкофф превью-транскрибации из-за
        переполнения аудиобуфера recorder-а. 0.0 — бэкофф выключен."""
        return self._preview_overflow_backoff_sec

    @property
    def preview_thread_alive(self) -> bool:
        return self._preview_thread is not None and self._preview_thread.is_alive()

    # ------------------------------------------------------------------ #
    # IPC handlers                                                         #
    # ------------------------------------------------------------------ #

    def handle_start_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запустить запись, если сервис ещё не вошёл в shutdown."""
        lifecycle_lock, closed_event = self._ensure_recording_lifecycle_state()
        # Быстрая проверка не даёт новому start встать за уже зависшим setup.
        # Повторная проверка под lock ниже закрывает гонку с началом shutdown.
        if closed_event.is_set():
            return {
                "status": "backend_closing",
                "is_recording": False,
            }
        with lifecycle_lock:
            if closed_event.is_set():
                return {
                    "status": "backend_closing",
                    "is_recording": False,
                }
            was_recording = bool(
                getattr(self.recorder, "is_recording", False)
            )
            try:
                return self._handle_start_recording_locked(params)
            except Exception:
                is_recording = bool(
                    getattr(self.recorder, "is_recording", False)
                )
                if not was_recording and is_recording:
                    # Последний защитный пояс: неизвестный post-start hook не
                    # имеет права скрыть уже захваченный микрофон исключением.
                    # Клиент получает честный recording и может продолжить
                    # либо выполнить owner-bound компенсацию.
                    generation = getattr(
                        self,
                        "_active_generation",
                        None,
                    )
                    if generation is None:
                        generation = self._publish_active_generation_locked(
                            token=uuid.uuid4().hex,
                            owner=self._requested_recording_owner(params),
                            start_request_id=(
                                self._requested_start_request_id(params)
                            ),
                        )
                    logger.exception(
                        "Post-start setup упал после захвата микрофона; "
                        "публикуем деградированный recording"
                    )
                    return {
                        "status": "recording",
                        "is_recording": True,
                        "generation_token": generation["token"],
                        "owner": generation["owner"],
                        "owner_revision": generation["revision"],
                        "owner_promoted": False,
                        "start_request_id": generation.get(
                            "start_request_id"
                        ),
                        "post_start_degraded": True,
                    }
                raise

    @staticmethod
    def _requested_recording_owner(params: dict[str, Any]) -> str:
        """Нормализовать owner старого/неполного start-запроса."""
        raw_owner = params.get("source")
        if raw_owner is None:
            return "dictation"
        owner = str(raw_owner).strip()
        return owner or "dictation"

    @staticmethod
    def _requested_start_request_id(
        params: dict[str, Any],
    ) -> str | None:
        """Проверить и вернуть непрозрачный client lease без нормализации."""
        if "start_request_id" not in params:
            return None
        request_id = params.get("start_request_id")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > _START_REQUEST_ID_MAX_CHARS
        ):
            # Значение намеренно не включается в исключение: dispatch пишет
            # сообщение в журнал, а request ID не должен туда попасть.
            raise ValueError(
                "start_request_id должен быть непустой строкой длиной "
                f"не более {_START_REQUEST_ID_MAX_CHARS} символов"
            )
        return request_id

    def _next_generation_revision_locked(self) -> int:
        """Выдать следующую монотонную CAS-ревизию generation-перехода."""
        revision = int(getattr(self, "_generation_revision", 0)) + 1
        self._generation_revision = revision
        return revision

    def _publish_active_generation_locked(
        self,
        *,
        token: str,
        owner: str,
        start_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Атомарно опубликовать поколение после успешного recorder.start."""
        generation = {
            "token": str(token),
            "owner": str(owner),
            "start_request_id": start_request_id,
            "state": "capturing",
            "started_at": time.monotonic(),
            "promoted_from": None,
            "revision": self._next_generation_revision_locked(),
            # Снимок epoch линеаризует start относительно privacy-purge:
            # хвост поколения, начатого до purge, больше не репопулирует PII.
            "terminal_cache_epoch": self._terminal_cache_epoch_snapshot(),
        }
        self._active_generation = generation
        return generation

    def _max_recording_samples_for_owner(self, owner: str) -> int | None:
        """Потолок длительности (в сэмплах) для owner или None (без потолка).

        2026-08-05 (Fable HIGH-1/HIGH-A): meeting (C2 Live Meeting Overlay)
        — легитимно длинная запись, потолка не получает. Любой другой owner
        (dictation/quick_capture/будущие) — тесный MAX_DICTATION_DURATION_SEC,
        чтобы незамеченная запись не обваливала STT-fallback конвейер
        (живой инцидент 2026-08-05: 52 минуты без остановки).
        """
        if owner == "meeting":
            return None
        # LOW-4 (Fable): env-опечатка (<=0) не должна давать cap=0 — тогда
        # самый первый чанк уже превышал бы потолок и КАЖДАЯ запись
        # мгновенно авто-останавливалась бы пустой.
        max_dictation_sec = max(60.0, float(_cfg_settings.MAX_DICTATION_DURATION_SEC))
        return int(max_dictation_sec * getattr(self.recorder, "sample_rate", 16000))

    def _transition_generation_owner_locked(
        self,
        owner: str,
        *,
        promoted_from: str | None,
    ) -> int:
        """Сменить owner того же token и вернуть новую CAS-ревизию."""
        generation = getattr(self, "_active_generation", None)
        if generation is None:
            raise RuntimeError("Owner-переход без активного поколения")
        revision = self._next_generation_revision_locked()
        generation["owner"] = str(owner)
        generation["promoted_from"] = promoted_from
        generation["revision"] = revision
        # 2026-08-05 (Fable HIGH-A): владелец мог смениться БЕЗ нового
        # физического start() (R2 promote dictation→meeting и его CAS-
        # rollback) — потолок записи должен смениться синхронно с owner,
        # иначе adopted-meeting наследует тесный dictation-потолок и тихо
        # самофинализируется на его пороге (тот же класс бага, что HIGH-1,
        # через другую дверь).
        set_session_max = getattr(self.recorder, "set_session_max_recording_samples", None)
        if callable(set_session_max):
            set_session_max(self._max_recording_samples_for_owner(str(owner)))
        spill = getattr(self, "_active_spill", None)
        rewrite_source = getattr(spill, "rewrite_source", None)
        if callable(rewrite_source):
            try:
                # Один helper обслуживает и promote, и revision-CAS rollback:
                # RAM-owner и rescue-meta не расходятся по разным веткам.
                rewrite_source(
                    str(owner),
                    promoted_from=promoted_from,
                )
            except Exception:
                # Реальный RecordingSpillWriter уже fail-open, но duck-typed
                # legacy writer не должен отменить состоявшийся owner-переход.
                logger.warning(
                    "RecordingSpill: owner изменён, но rewrite_source упал",
                    exc_info=True,
                )
        return revision

    def _active_generation_start_response_locked(
        self,
        generation: dict[str, Any],
        requested_owner: str,
        requested_start_request_id: str | None,
    ) -> dict[str, Any]:
        """Разрешить repeat/promote/conflict без нового physical start."""
        with self._preview_lock:
            preview_text = self._preview_text
            preview_duration = self._preview_duration_sec

        current_owner = generation.get("owner")
        active_start_request_id = generation.get("start_request_id")
        if current_owner == requested_owner:
            idempotent_replay = (
                requested_start_request_id is not None
                and requested_start_request_id == active_start_request_id
            )
            return {
                "status": (
                    "recording"
                    if idempotent_replay
                    else "already_recording"
                ),
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "generation_token": generation.get("token"),
                "owner": current_owner,
                "owner_revision": int(
                    generation.get("revision", 0)
                ),
                "owner_promoted": False,
                "promoted": False,
                "start_request_id": active_start_request_id,
            }

        if (
            current_owner == "dictation"
            and requested_owner == "meeting"
        ):
            owner_revision = self._transition_generation_owner_locked(
                "meeting",
                promoted_from="dictation",
            )
            return {
                "status": "already_recording",
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "generation_token": generation.get("token"),
                "owner": "meeting",
                "owner_revision": owner_revision,
                # Старый контракт нужен MeetingSessionService для CAS rollback;
                # promoted — аддитивный явный контракт R2 §4.3.
                "owner_promoted": True,
                "promoted": True,
                "start_request_id": active_start_request_id,
            }

        return {
            "status": "owner_conflict",
            "is_recording": True,
            "duration_sec": preview_duration,
            "preview_text": preview_text,
            "owner": current_owner,
            "requested": requested_owner,
            "start_request_id": active_start_request_id,
        }

    def _clear_active_generation_locked(self) -> None:
        """Снять физически завершённое поколение под lifecycle-lock."""
        if getattr(self, "_active_generation", None) is None:
            return
        self._next_generation_revision_locked()
        self._active_generation = None

    def _finalizing_generations_locked(
        self,
    ) -> dict[str, dict[str, Any]]:
        """Вернуть finalizing-реестр; caller держит lifecycle-lock.

        Ленивая ветка сохраняет совместимость узких legacy-тестов, которые
        создают сервис через ``__new__`` и намеренно обходят ``__init__``.
        """
        generations = getattr(self, "_finalizing_generations", None)
        if generations is None:
            generations = {}
            self._finalizing_generations = generations
        return generations

    def _move_active_generation_to_finalizing_locked(
        self,
    ) -> dict[str, Any] | None:
        """Атомарно передать завершённый physical capture тяжёлому хвосту."""
        generation = getattr(self, "_active_generation", None)
        if generation is None:
            return None
        token = str(generation.get("token") or "")
        if not token:
            # Generation без token не должна возникать после Task 2. Не
            # публикуем неадресуемый хвост, но снимаем stale active-slot.
            self._clear_active_generation_locked()
            return generation
        generation["state"] = "finalizing"
        self._finalizing_generations_locked()[token] = generation
        self._clear_active_generation_locked()
        return generation

    def _ensure_terminal_cache_state(
        self,
    ) -> tuple[
        OrderedDict[str, tuple[float, dict[str, Any]]],
        Any,
        int,
    ]:
        """Вернуть cache-примитивы, включая legacy ``__new__``-дубли."""
        cache = getattr(self, "_terminal_cache", None)
        cache_lock = getattr(self, "_terminal_cache_lock", None)
        epoch = getattr(self, "_terminal_cache_epoch", None)
        if cache is not None and cache_lock is not None and epoch is not None:
            return cache, cache_lock, int(epoch)

        with type(self)._lifecycle_init_lock:
            cache = getattr(self, "_terminal_cache", None)
            if cache is None:
                cache = OrderedDict()
                self._terminal_cache = cache
            cache_lock = getattr(self, "_terminal_cache_lock", None)
            if cache_lock is None:
                cache_lock = threading.Lock()
                self._terminal_cache_lock = cache_lock
            epoch = getattr(self, "_terminal_cache_epoch", None)
            if epoch is None:
                epoch = 0
                self._terminal_cache_epoch = epoch
        return cache, cache_lock, int(epoch)

    def _terminal_cache_epoch_snapshot(self) -> int:
        """Снять текущий purge-epoch под cache-lock."""
        _, cache_lock, _ = self._ensure_terminal_cache_state()
        with cache_lock:
            return int(self._terminal_cache_epoch)

    @staticmethod
    def _prune_expired_terminal_cache_locked(
        cache: OrderedDict[str, tuple[float, dict[str, Any]]],
        now: float,
    ) -> None:
        """Удалить все TTL-протухшие PII-снимки; caller держит cache-lock."""
        expired_tokens = [
            token
            for token, (stored_at, _) in cache.items()
            if now - stored_at >= _TERMINAL_CACHE_TTL_SEC
        ]
        for token in expired_tokens:
            cache.pop(token, None)

    def _replay_terminal_response(
        self,
        token: str,
    ) -> dict[str, Any] | None:
        """Вернуть независимый снимок terminal-ответа, если TTL/privacy разрешают."""
        # Privacy проверяется на чтении: кэш мог быть заполнен до включения
        # режима, но ни один replay-путь не должен выдать старый cleartext.
        if bool(
            self._get_runtime_setting(
                "privacy_mode_enabled",
                False,
            )
        ):
            return None

        try:
            cache, cache_lock, _ = self._ensure_terminal_cache_state()
            now = time.monotonic()
            with cache_lock:
                self._prune_expired_terminal_cache_locked(cache, now)
                entry = cache.get(token)
                if entry is None:
                    return None
                try:
                    replay = copy.deepcopy(entry[1])
                except Exception:
                    # Некопируемый снимок не должен ломать каждый retry.
                    cache.pop(token, None)
                    raise
        except Exception:
            # Любой сбой cache-read — безопасный miss. Ответ и token не
            # логируем: cached payload может содержать полный транскрипт.
            logger.warning(
                "Terminal cache: чтение replay не удалось",
                exc_info=True,
            )
            return None

        # Повторная проверка сужает окно переключения privacy между первым
        # чтением настройки и готовым снимком ответа.
        if bool(
            self._get_runtime_setting(
                "privacy_mode_enabled",
                False,
            )
        ):
            return None
        return replay

    def clear_terminal_cache(self) -> None:
        """Немедленно стереть terminal PII и закрыть epoch для старых хвостов."""
        cache, cache_lock, _ = self._ensure_terminal_cache_state()
        with cache_lock:
            cache.clear()
            self._terminal_cache_epoch = int(
                self._terminal_cache_epoch
            ) + 1

    @staticmethod
    def _requested_stop_owner(
        params: dict[str, Any],
    ) -> str | None:
        """Нормализовать owner stop-запроса, не ломая legacy-клиентов.

        В отличие от start, отсутствие/пустота здесь НЕ означает dictation:
        старый бинарь без source должен пройти только token-гейт и не создавать
        ложное owner-событие. Нестроковые JSON-значения также считаются legacy.
        """
        raw_owner = params.get("source")
        if not isinstance(raw_owner, str):
            return None
        owner = raw_owner.strip()
        return owner or None

    @staticmethod
    def _owner_telemetry_category(owner: Any) -> str:
        """Вернуть PII-безопасную категорию для WARNING и ErrorBus."""
        if (
            isinstance(owner, str)
            and owner in _OWNER_TELEMETRY_ALLOWLIST
        ):
            return owner
        return "other"

    def _report_owner_mismatch(
        self,
        owner: str | None,
        requested: str | None,
    ) -> None:
        """Громко, но fail-open сообщить о положительном owner-mismatch.

        В телеметрию не попадают token, текст, путь или произвольный source.
        Ошибка logger/ErrorBus не имеет права менять решение shadow/enforce и
        тем более оставлять микрофон захваченным из-за сбоя наблюдаемости.
        """
        try:
            safe_owner = self._owner_telemetry_category(owner)
            safe_requested = self._owner_telemetry_category(requested)
            logger.warning(
                "Несовпадение владельца записи: owner=%s requested=%s",
                safe_owner,
                safe_requested,
            )
            error_bus = getattr(self, "_error_bus", None)
            if error_bus is None:
                return

            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get(
                "recording.owner_mismatch",
                {},
            )
            error_bus.push(KrabError(
                severity=entry.get("severity", "warn"),
                component="recording",
                code="recording.owner_mismatch",
                message_user=entry.get(
                    "user_msg_ru",
                    "Обнаружено несовпадение режима при остановке записи",
                ),
                message_debug=(
                    "recording owner mismatch: "
                    f"owner={safe_owner} requested={safe_requested}"
                ),
                timestamp=datetime.now(timezone.utc),
                context={
                    "owner": safe_owner,
                    "requested": safe_requested,
                },
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            ))
        except Exception:
            # Даже logging handler может быть внешним и бросить исключение.
            # Второй лог — best-effort и не должен повторно сломать stop.
            try:
                logger.debug(
                    "Owner-mismatch telemetry недоступна",
                    exc_info=True,
                )
            except Exception:
                pass

    def _stop_gate_decision(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Решить, можно ли трогать recorder; caller держит lifecycle-lock.

        Legacy означает только отсутствие ключа generation_token. Если ключ
        присутствует, но его значение пустое/нестроковое, fail-closed защищает
        следующую запись от некорректного или протухшего клиента.
        """
        token_present = "generation_token" in params
        raw_token = params.get("generation_token")
        active = getattr(self, "_active_generation", None)

        if token_present:
            if not isinstance(raw_token, str) or not raw_token:
                return {
                    "status": "unknown_generation",
                    "generation_token": raw_token,
                }
            token = raw_token
            if active is not None and active.get("token") == token:
                if active.get("state") == "finalizing":
                    return {
                        "status": "stop_in_progress",
                        "generation_token": token,
                    }
            elif token in self._finalizing_generations_locked():
                return {
                    "status": "stop_in_progress",
                    "generation_token": token,
                }
            else:
                replayed = self._replay_terminal_response(token)
                if replayed is not None:
                    return replayed
                return {
                    "status": "unknown_generation",
                    "generation_token": token,
                }

        requested_owner = self._requested_stop_owner(params)
        if "expected_owner_revision" in params:
            raw_revision = params.get("expected_owner_revision")
            revision_valid = (
                type(raw_revision) is int
                and raw_revision > 0
            )
            current_owner = (
                active.get("owner") if active is not None else None
            )
            current_revision = (
                int(active.get("revision", 0))
                if active is not None
                else int(getattr(self, "_generation_revision", 0))
            )
            strict_match = (
                token_present
                and isinstance(raw_token, str)
                and bool(raw_token)
                and active is not None
                and active.get("token") == raw_token
                and requested_owner is not None
                and active.get("owner") == requested_owner
                and revision_valid
                and current_revision == raw_revision
            )
            if not strict_match:
                # Наличие revision переводит запрос в fail-closed CAS-режим.
                # Решение принимается до teardown и не зависит от shadow-флага.
                self._report_owner_mismatch(
                    (
                        str(current_owner)
                        if current_owner is not None
                        else None
                    ),
                    requested_owner,
                )
                return {
                    "status": "owner_mismatch",
                    "owner": current_owner,
                    "requested": requested_owner,
                    "owner_revision": current_revision,
                }

        if active is not None and requested_owner is None:
            try:
                logger.debug(
                    "Owner stop-gate: source отсутствует или legacy; "
                    "owner-политику пропускаю"
                )
            except Exception:
                # Диагностический breadcrumb не участвует в решении stop.
                pass
        if (
            active is not None
            and requested_owner
            and active.get("owner") != requested_owner
        ):
            current_owner = active.get("owner")
            self._report_owner_mismatch(
                (
                    str(current_owner)
                    if current_owner is not None
                    else None
                ),
                requested_owner,
            )
            if self._coerce_bool(
                self._get_runtime_setting(
                    "recording_owner_enforce",
                    False,
                ),
                default=False,
            ):
                return {
                    "status": "owner_mismatch",
                    "owner": current_owner,
                    "requested": requested_owner,
                }
        return None

    def _terminalize_generation(
        self,
        generation: dict[str, Any] | None,
        response: dict[str, Any],
    ) -> None:
        """Удалить только конкретную G1 через identity-CAS под общим lock.

        ``response`` уже передаётся сейчас, хотя Task 3 его не хранит: Task 5
        использует тот же единый хук для bounded TTL-replay без обходных путей.
        """
        if generation is None:
            return
        try:
            # Снимок делаем до lifecycle-lock: terminal response может нести
            # длинный транскрипт, но его копирование не должно блокировать G2.
            response_snapshot = copy.deepcopy(response)
        except Exception:
            response_snapshot = None
            logger.warning(
                "Terminal cache: не удалось создать снимок ответа",
                exc_info=True,
            )

        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            token = str(generation.get("token") or "")
            finalizing = self._finalizing_generations_locked()
            terminalized = False
            if token and finalizing.get(token) is generation:
                del finalizing[token]
                terminalized = True
            # Defensive-путь для orphan/already_stopped: compare+clear
            # выполняются в одной критической секции, поэтому G1 не сотрёт G2.
            if getattr(self, "_active_generation", None) is generation:
                self._clear_active_generation_locked()
                terminalized = True

            if terminalized and token and response_snapshot is not None:
                try:
                    cache, cache_lock, _ = (
                        self._ensure_terminal_cache_state()
                    )
                    now = time.monotonic()
                    with cache_lock:
                        self._prune_expired_terminal_cache_locked(
                            cache,
                            now,
                        )
                        if int(
                            generation.get(
                                "terminal_cache_epoch",
                                -1,
                            )
                        ) == int(self._terminal_cache_epoch):
                            # Первый terminal-ответ token неизменяем:
                            # defensive повтор не переписывает replay.
                            if token not in cache:
                                cache[token] = (
                                    now,
                                    response_snapshot,
                                )
                            while len(cache) > _TERMINAL_CACHE_MAX:
                                cache.popitem(last=False)
                        # При несовпавшем epoch history-финализация законна,
                        # но pre-purge transcript в RAM больше не публикуем.
                except Exception:
                    # Cache — best-effort adjunct ПОСЛЕ успешного identity-CAS.
                    # Его отказ не меняет terminal response и не оживляет G1.
                    logger.warning(
                        "Terminal cache: публикация ответа не удалась",
                        exc_info=True,
                    )

    @staticmethod
    def _build_finalization_failed_response(
        generation: dict[str, Any] | None,
        exc: BaseException,
    ) -> dict[str, Any]:
        """Собрать безопасный terminal-ответ после physical stop."""
        return {
            "ok": False,
            "status": "finalization_failed",
            "error": "finalization_failed",
            "error_type": type(exc).__name__,
            "is_recording": False,
            "generation_token": (
                generation.get("token")
                if generation is not None
                else None
            ),
        }

    def rollback_owner_transition(
        self,
        *,
        expected_revision: int,
        expected_owner: str,
        restore_owner: str,
    ) -> bool:
        """CAS-откатить promote, не затрагивая более новое поколение owner."""
        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            if not bool(getattr(self.recorder, "is_recording", False)):
                return False
            generation = getattr(self, "_active_generation", None)
            if (
                generation is None
                or generation.get("owner") != expected_owner
                or int(generation.get("revision", 0)) != int(expected_revision)
            ):
                return False
            self._transition_generation_owner_locked(
                restore_owner,
                promoted_from=None,
            )
            return True

    def _handle_start_recording_locked(self, params: dict[str, Any]) -> dict[str, Any]:
        """Выполнить цельный setup записи под lifecycle-lock."""
        requested_owner = self._requested_recording_owner(params)
        requested_start_request_id = self._requested_start_request_id(params)
        recorder_was_recording = bool(
            getattr(self.recorder, "is_recording", False)
        )
        active_generation = getattr(self, "_active_generation", None)
        if active_generation is not None and (
            not recorder_was_recording
            or active_generation.get("state") != "capturing"
        ):
            # После recorder_timeout физический worker уже может успеть
            # завершиться, но G1 всё ещё хранит право на retry/rescue. Новый
            # start G2 не должен перезаписать эту единственную идентичность.
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "status": "recorder_stopping",
                "is_recording": False,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "generation_token": active_generation.get("token"),
                "owner": active_generation.get("owner"),
                "owner_revision": int(
                    active_generation.get("revision", 0)
                ),
                "owner_promoted": False,
                "promoted": False,
                "start_request_id": active_generation.get(
                    "start_request_id"
                ),
            }
        if active_generation is not None:
            # Repeat/promote/conflict — логический переход существующей G1.
            # Решаем его до device/spill/recorder.start: новый physical capture
            # и placeholder B для уже занятого микрофона не создаются.
            return self._active_generation_start_response_locked(
                active_generation,
                requested_owner,
                requested_start_request_id,
            )
        if recorder_was_recording:
            # Call Assist пока захватывает AudioRecorder напрямую. Generation
            # отсутствует, поэтому безопасно доказать owner/promotion нельзя.
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "status": "unmanaged_recording",
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
            }
        if (
            len(self._finalizing_generations_locked())
            >= _MAX_FINALIZING_GENERATIONS
        ):
            # Проверка стоит до device/spill/UUID/recorder.start: отказ из-за
            # backlog не оставляет ложного rescue-файла и не трогает микрофон.
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            generation = getattr(self, "_active_generation", None)
            return {
                "status": "recorder_stopping",
                "is_recording": False,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "generation_token": (
                    generation.get("token")
                    if generation is not None
                    else None
                ),
                "owner": (
                    generation.get("owner")
                    if generation is not None
                    else None
                ),
                "owner_revision": (
                    int(generation.get("revision", 0))
                    if generation is not None
                    else int(getattr(self, "_generation_revision", 0))
                ),
                "owner_promoted": False,
            }
        # Apply selected_input_device from settings before starting (W1327 F2 HIGH).
        # Uses cached_settings() — runtime-safe per Wave 58 lesson.
        _settings_pre = self._settings_svc.cached_settings()
        _selected_device = _settings_pre.get("selected_input_device", None)
        if _selected_device is not None and hasattr(self.recorder, "set_device"):
            try:
                self.recorder.set_device(_selected_device)
            except Exception as _dev_err:
                logger.warning(
                    "Не удалось применить аудиоустройство %r: %s",
                    _selected_device,
                    _dev_err,
                )
        # R1: continuous spill — открыть writer ДО recorder.start(), чтобы
        # recorder-воркер сразу получил живой объект. Ошибки создания/открытия
        # НИКОГДА не роняют запись — fail-open (spill=None, один WARN).
        # getattr: старые узкие тесты обходят __init__ через __new__ (см.
        # _ensure_recording_lifecycle_state выше) и не знают про R1-поля.
        generation_token = uuid.uuid4().hex
        spill = None
        _rescue_dir = getattr(self, "_rescue_dir", None)
        if _rescue_dir is not None and bool(
            _settings_pre.get("recording_spill_enabled", True)
        ):
            try:
                from backend.recording_spill import RecordingSpillWriter
                spill = RecordingSpillWriter(
                    rescue_dir=_rescue_dir,
                    sample_rate=int(getattr(self.recorder, "sample_rate", 16000)),
                    channels=int(getattr(self.recorder, "channels", 1)),
                    source=requested_owner,
                    session_id=generation_token,
                )
                if not spill.open():
                    spill = None
            except Exception:
                logger.warning("RecordingSpill: не удалось создать writer — "
                               "запись продолжается без spill", exc_info=True)
                spill = None
        # 2026-08-05: жёсткий потолок диктовки/quick-capture — per-session
        # override recorder.start(), а НЕ конструктор recorder'а (Fable
        # HIGH-1: recorder ОБЩИЙ с meeting — конструкторный тесный потолок
        # тихо самофинализировал бы live-встречу C2 на её пороге). meeting
        # owner override не получает — recorder использует собственный
        # class-level дефолт (4ч memory-safety net). См.
        # _max_recording_samples_for_owner() — та же формула переиспользуется
        # в _transition_generation_owner_locked() для R2 promote/rollback
        # (Fable HIGH-A: owner может смениться БЕЗ нового физического start).
        dictation_max_samples = self._max_recording_samples_for_owner(requested_owner)

        try:
            start_callable = self.recorder.start
            start_accepts_spill = True
            start_extra_kwargs: dict[str, Any] = {}
            try:
                start_signature = inspect.signature(start_callable)
            except (TypeError, ValueError):
                # Для непрозрачного callable выполняем один современный вызов.
                # Retry после TypeError опасен: первый вызов мог уже захватить
                # микрофон, и второй создал бы две логические идентичности.
                start_signature = None

            if start_signature is not None:
                try:
                    start_signature.bind(spill=spill)
                except TypeError:
                    start_accepts_spill = False
                if dictation_max_samples is not None:
                    try:
                        start_signature.bind(max_recording_samples=dictation_max_samples)
                        start_extra_kwargs["max_recording_samples"] = dictation_max_samples
                    except TypeError:
                        # Старый/duck-typed recorder без поддержки — используем
                        # его собственный (конструкторный) дефолт.
                        pass

            if start_accepts_spill:
                started = start_callable(spill=spill, **start_extra_kwargs)
            else:
                # R1 fail-open для доказанного legacy-контракта без spill=.
                # Совместимость выяснена ДО вызова, поэтому recorder.start()
                # по-прежнему выполняется не более одного раза.
                logger.warning(
                    "RecordingSpill: сигнатура recorder.start() не принимает "
                    "spill= — запись продолжается без spill"
                )
                if spill is not None:
                    spill.discard()
                    spill = None
                started = start_callable(**start_extra_kwargs)
        except Exception:
            # Реальный AudioRecorder.start() failure-atomic, но внешний/legacy
            # recorder может бросить уже после физического захвата. Тогда
            # публикуем тот же token/spill, чтобы outer fail-open ответ не
            # создал вторую идентичность поверх живой записи.
            recorder_is_recording = bool(
                getattr(self.recorder, "is_recording", False)
            )
            if not recorder_was_recording and recorder_is_recording:
                self._active_spill = spill
                if getattr(self, "_active_generation", None) is None:
                    self._publish_active_generation_locked(
                        token=generation_token,
                        owner=requested_owner,
                        start_request_id=requested_start_request_id,
                    )
            elif spill is not None:
                # Физический захват не состоялся: этот writer — лишь
                # placeholder одной неудавшейся fresh-start попытки.
                spill.discard()
            raise
        if not started:
            if spill is not None:
                spill.discard()  # запись не началась — файл-пустышка не нужен
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            recorder_is_recording = bool(
                getattr(self.recorder, "is_recording", False)
            )
            generation = getattr(self, "_active_generation", None)
            if recorder_is_recording:
                if generation is not None:
                    # Defensive race-путь: обычные Core-переходы отсечены
                    # preflight выше, но duck-typed recorder мог опубликовать
                    # состояние во время своего start().
                    return self._active_generation_start_response_locked(
                        generation,
                        requested_owner,
                        requested_start_request_id,
                    )
                return {
                    "status": "unmanaged_recording",
                    "is_recording": True,
                    "duration_sec": preview_duration,
                    "preview_text": preview_text,
                }

            return {
                "status": "recorder_stopping",
                "is_recording": False,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "owner_revision": int(
                    getattr(self, "_generation_revision", 0)
                ),
                "owner_promoted": False,
                "promoted": False,
            }
        self._active_spill = spill
        generation = self._publish_active_generation_locked(
            token=generation_token,
            owner=requested_owner,
            start_request_id=requested_start_request_id,
        )
        owner_revision = int(generation["revision"])
        # После успешного recorder.start() IPC обязан вернуть ``recording``:
        # вспомогательные preview/telemetry-хуки не владеют микрофоном и не
        # имеют права превратить живую запись в «ошибку запуска» для клиента.
        try:
            self._reset_preview_state()
        except Exception:
            logger.warning(
                "Не удалось сбросить preview после старта записи",
                exc_info=True,
            )
        # Повторное cached_settings() раньше оставляло orphan-рекордер, если
        # cache-provider падал уже ПОСЛЕ recorder.start(). Снимок получен до
        # физического старта и остаётся единым для всего setup-перехода.
        settings = _settings_pre
        # LM Studio brain unload: освобождаем ~19 GB unified memory под Whisper+pyannote.
        try:
            bump_stt_activity()
            brain_model = str(settings.get("llm_brain_model", "")).strip()
            unload_enabled = bool(settings.get("llm_brain_unload_on_recording", True))
            conductor = getattr(self, "_memory_conductor", None)
            if brain_model and unload_enabled:
                # 🔴 H2 (ревью спеки): секвенс кондуктора включается ТОЛЬКО при
                # enforce; иначе живёт ЛЕГАСИ-путь — shadow-неделя не смеет молча
                # отключить выгрузку 19 ГБ перед диктовкой.
                if conductor is not None and conductor.enforce_for("recording_sequence"):
                    conductor.on_recording_start()
                else:
                    from backend.lm_studio_lifecycle import unload_model_async
                    base_url = str(settings.get("llm_base_url", "http://localhost:1234/v1"))
                    unload_model_async(base_url, brain_model)
        except Exception as exc:
            logger.debug("LM Studio brain unload hook failed: %s", exc)
        # Brain lease coordination: release the brain lease so Krab userbot can use
        # LM Studio while Ear is busy with STT/pyannote on the same Metal GPU.
        if bool(settings.get("llm_brain_lease_enabled", True)):
            try:
                from backend.brain_lease import release_brain_lease
                release_brain_lease("krab_ear")
            except Exception as exc:
                logger.debug("BrainLease: release hook error (ignored): %s", exc)
        try:
            add_breadcrumb(
                category="recording",
                message="started",
                level="info",
                data={
                    "quality_profile": str(
                        settings.get("quality_profile", "balanced")
                    )
                },
            )
        except Exception:
            logger.debug(
                "Sentry breadcrumb старта записи недоступен",
                exc_info=True,
            )
        if bool(settings.get("realtime_preview_enabled", True)):
            quality_profile = str(settings.get("quality_profile", "balanced"))
            try:
                self._start_preview_worker(quality_profile=quality_profile)
            except Exception:
                logger.warning(
                    "Не удалось запустить realtime preview; запись продолжается",
                    exc_info=True,
                )
        if bool(settings.get("realtime_partial_enabled", True)):
            if self._get_runtime_setting("privacy_mode_enabled", False):
                logger.info("RealtimePartialTranscriber не запущен: privacy_mode_enabled=True")
            else:
                import uuid as _uuid
                self._rt_session_id = _uuid.uuid4().hex
                _interval = float(settings.get("rt_partial_interval_sec", 3.0))
                _buffer = float(settings.get("rt_partial_buffer_sec", 8.0))
                _sample_rate = int(getattr(self.recorder, "sample_rate", 16000))
                _settings_svc = self._settings_svc

                def _privacy_getter() -> bool:
                    # FAIL CLOSED (W1768): если cached_settings() бросает исключение,
                    # состояние приватности неизвестно → считаем privacy ON и возвращаем
                    # True (emit подавляется в RealtimePartialTranscriber). Возврат False
                    # здесь был fail-OPEN: частичный транскрипт мог утечь, пока реальное
                    # состояние privacy_mode неизвестно. Нормальный путь по-прежнему
                    # возвращает реальный флаг — на безопасный режим переключает только
                    # исключение.
                    try:
                        return bool(_settings_svc.cached_settings().get("privacy_mode_enabled", False))
                    except Exception:
                        return True

                # wave-27 MED (race): build, start and publish the handle atomically
                # so a concurrent stop_recording cannot observe a half-constructed /
                # orphaned daemon. The handle is assigned only AFTER start() succeeds.
                try:
                    with self._rt_lock:
                        # wave-1770 MED: stop any existing daemon before overwriting the
                        # handle. A rapid start→start (without an intervening stop) would
                        # otherwise orphan the previous RealtimePartialTranscriber thread.
                        # stop() joins the daemon's own internal thread; that thread does
                        # not touch _rt_lock, so stopping under the lock cannot deadlock.
                        if self._rt_partial is not None:
                            try:
                                # 2026-08-01: КОРОТКИЙ бюджет, не дефолтные 30 с.
                                # Здесь путь СТАРТА записи: не дождались — идём
                                # дальше без превью (ветка else ниже), поэтому
                                # длинное ожидание только блокирует диктовку и
                                # даёт переполнение аудиобуфера. Дефолт stop()
                                # остаётся 30 с (W1323) для честной остановки.
                                old_rt_stopped = self._rt_partial.stop(
                                    timeout_sec=RT_PARTIAL_START_STOP_TIMEOUT_SEC
                                ) is not False
                            except Exception:
                                logger.warning("rt_partial: stop старого инстанса упал", exc_info=True)
                                old_rt_stopped = False
                            if old_rt_stopped:
                                self._rt_partial = None
                        if self._rt_partial is None:
                            _rt = RealtimePartialTranscriber(
                                transcriber=self.transcriber,
                                recorder=self.recorder,
                                event_bus=event_bus,
                                interval_sec=_interval,
                                buffer_sec=_buffer,
                                privacy_getter=_privacy_getter,
                            )
                            _rt.start(
                                session_id=self._rt_session_id,
                                sample_rate=_sample_rate,
                            )
                            self._rt_partial = _rt
                        else:
                            logger.warning(
                                "RealtimePartialTranscriber не перезапущен: "
                                "прежний worker ещё жив"
                            )
                except Exception:
                    logger.exception("Не удалось запустить RealtimePartialTranscriber")

        # W930 CRITICAL fix: wire SessionTracker start — skip in privacy mode
        _privacy_mode = bool(settings.get("privacy_mode_enabled", False))
        if not _privacy_mode:
            try:
                _audio_device = str(settings.get("audio_device", ""))
                _quality_profile = str(settings.get("quality_profile", "balanced"))
                _stt_model = str(settings.get("stt_model", ""))
                self._session_tracker.start_session(
                    audio_device=_audio_device,
                    quality_preset=_quality_profile,
                    stt_model=_stt_model,
                )
            except Exception:
                logger.warning("SessionTracker.start_session завершился с ошибкой (не критично)", exc_info=True)

        # W1325 F1 HIGH: wire RealtimeSilenceFilter (default OFF)
        # wave-27 MED (race): reset + construct + start + publish the handle
        # atomically under _rt_lock so a concurrent stop_recording cannot orphan
        # the filter's background work.
        try:
            with self._rt_lock:
                if self._rsf is not None:
                    try:
                        self._rsf.stop()
                    except Exception:
                        logger.warning("RSF: stop старого инстанса упал", exc_info=True)
                    if not self._rsf.is_running:
                        self._rsf = None
                if (
                    bool(settings.get("realtime_silence_filter_enabled", False))
                    and self._rsf is None
                ):
                    _rsf = RealtimeSilenceFilter(
                        recorder=self.recorder,
                        settings=settings,
                        event_bus_emit=event_bus.emit,
                    )
                    _rsf.start()
                    self._rsf = _rsf
                elif self._rsf is not None:
                    logger.warning(
                        "RealtimeSilenceFilter не перезапущен: прежний worker ещё жив"
                    )
        except Exception:
            logger.exception("Не удалось запустить RealtimeSilenceFilter")
        return {
            "status": "recording",
            "is_recording": True,
            "generation_token": generation["token"],
            "owner": generation["owner"],
            "owner_revision": owner_revision,
            "owner_promoted": False,
            "start_request_id": generation.get("start_request_id"),
        }

    def handle_stop_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        """Остановить capture и терминализировать ровно его generation."""
        settings = self._settings_svc.cached_settings()

        phase_a = self._stop_recording_phase_a(params, settings)
        if "early_return" in phase_a:
            response = phase_a["early_return"]
            generation = phase_a.get("generation")
            # Gate/recorder_timeout структурно не несут generation и потому
            # не могут случайно завершить ещё живой capture.
            if generation is not None:
                self._terminalize_generation(generation, response)
            early_spill = phase_a.get("spill")
            if early_spill is not None:
                early_spill.close()
            return response

        generation = phase_a.get("generation")
        try:
            response = self._run_stop_recording_tail(
                phase_a=phase_a,
                settings=settings,
            )
        except Exception as exc:
            # Physical stop уже состоялся. Не оставляем G1 навечно в
            # finalizing-map: отдаём типизированный терминальный ответ, а spill
            # сохраняем для rescue следующего запуска.
            logger.exception(
                "stop_recording: неожиданная ошибка тяжёлой финализации"
            )
            spill = phase_a.get("spill")
            if spill is not None:
                spill.close()
            response = self._build_finalization_failed_response(
                generation,
                exc,
            )

        self._terminalize_generation(generation, response)
        return response

    def _run_stop_recording_tail(
        self,
        *,
        phase_a: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Выполнить тяжёлые фазы B–E после необратимого physical stop."""

        audio = phase_a["audio"]
        duration_sec = phase_a["duration_sec"]
        stop_tail_trim_ms = phase_a["stop_tail_trim_ms"]
        _rt_session_id = phase_a["rt_session_id"]
        _bookmark_session_id = phase_a["bookmark_session_id"]
        sr = phase_a["sr"]
        # Phase A атомарно отвязывает spill вместе с физическим stop. Иначе
        # concurrent shutdown мог забрать writer между phase A и этой строкой,
        # а успешный normal stop оставлял rescue-дубль.
        spill = phase_a["spill"]

        # Phase B: audio quality guards (silence + background)
        phase_b = self._stop_recording_phase_b(audio, duration_sec, stop_tail_trim_ms, sr)
        if "early_return" in phase_b:
            # 2026-07-12 self-heal: an RMS-below-threshold silence-guard trip is
            # the passive "audio came back empty" signal (see audio_selfheal.py).
            # Background-guard rejections are a DIFFERENT heuristic (distant/
            # uniform speech) and must NOT feed this counter — only the
            # silence_detected branch of _build_empty_audio_response does.
            if self._audio_selfheal is not None and phase_b["early_return"].get("silence_detected"):
                self._audio_selfheal.record_empty_result()
            # R1: тишина/фоновая речь — записи в history не будет, файл не нужен.
            if spill is not None:
                spill.discard()
            return phase_b["early_return"]

        silence_detected = phase_b["silence_detected"]
        background_guard_rejected = phase_b["background_guard_rejected"]

        # Phase C: STT execution
        phase_c = self._stop_recording_phase_c(audio, duration_sec, sr)
        if "early_return" in phase_c:
            # R1: STT упал — спилл ОСТАВИТЬ (восстановление на следующем старте
            # вернёт аудио, которое сейчас потеряно). close() уже сделан
            # recorder.stop() внутри phase_a — повторный вызов безопасен.
            if spill is not None:
                spill.close()
            return phase_c["early_return"]
        transcribe_payload = phase_c["transcribe_payload"]

        # Phase D: post-processing
        phase_d = self._stop_recording_phase_d(
            transcribe_payload=transcribe_payload,
            duration_sec=duration_sec,
            sr=sr,
            stop_tail_trim_ms=stop_tail_trim_ms,
            silence_detected=silence_detected,
            silence_guard_enabled=sr["silence_guard_enabled"],
            background_guard_rejected=background_guard_rejected,
        )
        if "early_return" in phase_d:
            # 2026-07-12 self-heal: STT ran but produced no text at nonzero
            # duration — second passive empty-result signal (silence guard can
            # miss a noise floor that still confuses Whisper into silence).
            if self._audio_selfheal is not None:
                self._audio_selfheal.record_empty_result()
            # R1: пустой текст — та же логика, что STT-провал: оставить.
            if spill is not None:
                spill.close()
            return phase_d["early_return"]

        # A real, non-empty transcript came back — audio pipeline just proved
        # itself healthy. Reset the self-heal empty streak (2026-07-12).
        if self._audio_selfheal is not None:
            self._audio_selfheal.record_success()

        # Phase E: history persistence + response assembly
        resp = self._stop_recording_phase_e(
            phase_d=phase_d,
            sr=sr,
            duration_sec=duration_sec,
            stop_tail_trim_ms=stop_tail_trim_ms,
            silence_detected=silence_detected,
            silence_guard_enabled=sr["silence_guard_enabled"],
            background_guard_rejected=background_guard_rejected,
            rt_session_id=_rt_session_id,
            bookmark_session_id=_bookmark_session_id,
            settings=settings,
        )
        if spill is not None:
            if resp.get("history_id") or resp.get("skipped") == "duplicate":
                spill.discard()
            # иначе (persist_failed и т.п.): оставить для восстановления —
            # персист не состоялся, аудио ещё нигде не сохранено.
        return resp

    def pause_realtime_partials(self) -> None:
        """Пауза партиалов на время тяжёлой операции meeting-слота (C2a).

        Доступ к _rt_partial — под _rt_lock (конвенция lifecycle-лока);
        сам pause() зовётся вне лока (короткий, но чужой код).
        Нет активного инстанса → no-op.
        """
        with self._rt_lock:
            rt = self._rt_partial
        if rt is not None:
            try:
                rt.pause()
            except Exception:
                logger.warning("pause_realtime_partials: pause() упал", exc_info=True)

    def resume_realtime_partials(self) -> None:
        """Снять паузу партиалов (C2a). Нет инстанса → no-op."""
        with self._rt_lock:
            rt = self._rt_partial
        if rt is not None:
            try:
                rt.resume()
            except Exception:
                logger.warning("resume_realtime_partials: resume() упал", exc_info=True)

    def handle_get_recording_state(self, params: dict[str, Any]) -> dict[str, Any]:
        # is_recording+generation — один протокольный снимок. recorder.start()
        # публикует физический флаг раньше generation; без lifecycle-lock клиент
        # видел невозможную пару True/null посреди штатного meeting-start.
        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            is_recording = bool(
                getattr(self.recorder, "is_recording", False)
            )
            generation = getattr(self, "_active_generation", None)
            active_owner = (
                generation.get("owner") if generation is not None else None
            )
            generation_token = (
                generation.get("token") if generation is not None else None
            )
            start_request_id = (
                generation.get("start_request_id")
                if generation is not None
                else None
            )
        with self._preview_lock:
            preview_text = self._preview_text
            preview_duration = self._preview_duration_sec
        # wave-31 HIGH: gate preview_text behind privacy_mode — the SSE partial-transcript
        # stream was already gated (W1673), but the IPC poll path was leaking accumulated
        # partial transcript text even when privacy_mode_enabled=True.
        if self._get_runtime_setting("privacy_mode_enabled", False):
            preview_text = ""
        audio_rms = (
            self.recorder.snapshot_rms()
            if hasattr(self.recorder, "snapshot_rms")
            else 0.0
        )
        active_session = self._session_tracker._active_session
        session_id = (active_session.get("session_id", "__live__") if active_session else "__live__")
        elapsed_sec = 0.0
        if hasattr(self.recorder, "get_duration_sec"):
            try:
                elapsed_sec = float(self.recorder.get_duration_sec() or 0.0)
            except Exception:
                elapsed_sec = preview_duration or 0.0
        return {
            "is_recording": is_recording,
            "owner": active_owner,
            "generation_token": generation_token,
            "start_request_id": start_request_id,
            "duration_sec": preview_duration,
            "preview_text": preview_text,
            "audio_rms": audio_rms,
            "elapsed_sec": elapsed_sec,
            "session_id": session_id,
        }

    def current_recording_owner(self) -> str | None:
        """Владелец активного generation ("dictation"/"quick_capture"/"meeting")
        или None, если записи сейчас нет. 2026-08-05: лёгкий геттер для
        RecordingDurationWatchdog (нужен только owner, не весь снимок
        handle_get_recording_state) — тот же lifecycle_lock-паттерн.
        """
        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            generation = getattr(self, "_active_generation", None)
            return generation.get("owner") if generation is not None else None

    def handle_list_audio_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
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

    def handle_get_audio_devices(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных входных аудиоустройств (обёртка для GUI)."""
        return {"devices": self._list_audio_inputs()}

    def handle_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Синхронная транскрибация списка файлов (CLI/legacy путь)."""
        return self._transcribe_paths_core(params)

    def handle_transcribe_paths_async(self, params: dict[str, Any]) -> dict[str, Any]:
        """Асинхронный вариант `transcribe_paths`: возвращает job_id сразу."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")
        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [
            r.resolve()
            for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
        selected: list[str] = []
        # Пути за пределами разрешённых корней фиксируются в rejected_errors
        # и будут видны через get_transcribe_progress (поле errors).
        rejected_errors: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if self._is_path_allowed(resolved, allowed_roots):
                selected.append(str(resolved))
            else:
                msg = f"Path outside allowed directories: {resolved}"
                rejected_errors.append(msg)
                logger.warning("transcribe_paths_async: %s", msg)
        try:
            audio_paths = self._collect_audio_paths(selected, allowed_roots=allowed_roots) if selected else []
        except Exception:
            audio_paths = []
        total_files = len(audio_paths)

        job_id = self._job_tracker.create_job(total_files=total_files)
        # Сразу фиксируем отклонённые пути в errors задачи — видны через get_transcribe_progress.
        if rejected_errors:
            self._job_tracker.update(job_id, errors=list(rejected_errors))
        job_params = dict(params)
        # W1342: получаем Event сразу после создания задачи — пока он точно существует.
        # Если впоследствии задача будет вытеснена prune(), get_cancel_event() вернёт None
        # и _cancel_check упадёт обратно на dict-полинг.
        cancel_event = self._job_tracker.get_cancel_event(job_id)

        def _emit_status(
            op: str,
            stage: str = "",
            progress: float | None = None,
            current_file: str | None = None,
            file_index: int | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "op": op,
                "stage": stage,
                "total_files": total_files,
                "ts": time.time(),
            }
            if progress is not None:
                payload["progress"] = progress
            if current_file is not None:
                payload["current_file"] = current_file
            if file_index is not None:
                payload["file_index"] = file_index
            event_bus.emit("app.status", payload)

        def _on_file_start(index: int, audio_path: str) -> None:
            self._job_tracker.update(
                job_id,
                status="running",
                current_file=Path(audio_path).name,
                current_stage="idle",
                file_index=index + 1,
            )
            _emit_status(
                "transcribe_job",
                stage="idle",
                progress=index / total_files if total_files else 0.0,
                current_file=Path(audio_path).name,
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
            _emit_status(
                "transcribe_job",
                stage="idle",
                progress=(index + 1) / total_files if total_files else 1.0,
                file_index=index + 1,
            )

        def _progress_callback(stage: str) -> None:
            self._job_tracker.update(job_id, current_stage=str(stage))
            state = self._job_tracker.get(job_id) or {}
            fi = state.get("file_index") or 0
            _emit_status(
                "transcribe_job",
                stage=str(stage),
                progress=max(0, fi - 1) / total_files if total_files else 0.0,
                file_index=fi,
            )

        def _cancel_check() -> bool:
            # W1342: используем threading.Event для мгновенной проверки без lock.
            # Если Event недоступен (задача вытеснена prune) — fallback на dict-полинг.
            if cancel_event is not None:
                return cancel_event.is_set()
            state = self._job_tracker.get(job_id)
            return bool(state and state.get("cancel_requested"))

        def _worker() -> None:
            try:
                self._job_tracker.update(job_id, status="running")
                _emit_status("transcribe_job", stage="started", progress=0.0)
                result = self._transcribe_paths_core(
                    job_params,
                    progress_callback=_progress_callback,
                    cancel_check=_cancel_check,
                    on_file_start=_on_file_start,
                    on_file_done=_on_file_done,
                )
                state = self._job_tracker.get(job_id) or {}
                # Объединяем ошибки из ядра с ранее зафиксированными rejected_errors.
                core_errors = list(result.get("errors") or [])
                all_errors = rejected_errors + core_errors
                if state.get("cancel_requested"):
                    _emit_status("idle", stage="", progress=1.0)
                    self._job_tracker.update(
                        job_id,
                        status="cancelled",
                        items=list(result.get("items") or []),
                        errors=all_errors,
                        processed=len(result.get("items") or []),
                        current_stage="idle",
                        finished_at=time.monotonic(),
                    )
                else:
                    _emit_status("idle", stage="", progress=1.0)
                    self._job_tracker.mark_done(
                        job_id,
                        items=list(result.get("items") or []),
                        errors=all_errors,
                    )
            except Exception as exc:
                logger.exception("Async transcribe job %s упал", job_id)
                _emit_status("idle", stage="", progress=1.0)
                self._job_tracker.mark_failed(job_id, str(exc))

        thread = threading.Thread(
            target=_worker,
            name=f"transcribe-{job_id}",
            daemon=True,
        )
        thread.start()
        return {"job_id": job_id}

    def handle_get_transcribe_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние async-job'а."""
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        state = self._job_tracker.get(job_id)
        if state is None:
            raise RuntimeError(f"Неизвестный job_id: {job_id}")

        status = str(state.get("status") or "queued")
        items_raw = list(state.get("items") or [])
        items_out = items_raw if status in ("done", "failed", "cancelled") else []

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

    def handle_cancel_transcribe_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сигнализирует воркеру об отмене job'а."""
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        cancelled = self._job_tracker.cancel(job_id)
        return {"cancelled": bool(cancelled)}

    def handle_preview_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Быстрый предпросмотр импорта: считает аудиофайлы без транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [
            r.resolve()
            for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if self._is_path_allowed(resolved, allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected, allowed_roots=allowed_roots)
        sample_limit = int(params.get("sample_limit", 5) or 5)
        safe_sample_limit = max(1, min(sample_limit, 50))
        by_ext: dict[str, int] = {}
        total_bytes = 0
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

    # ------------------------------------------------------------------ #
    # Preview worker (used by CallAssistService too via reset/start fns)  #
    # ------------------------------------------------------------------ #

    def reset_preview_state(self) -> None:
        with self._preview_lock:
            self._preview_text = ""
            self._preview_duration_sec = 0.0
            self._preview_updated_at = 0.0

    # keep legacy underscore name as alias so BackendService internal callers continue to work
    _reset_preview_state = reset_preview_state

    def start_preview_worker(self, quality_profile: str) -> bool:
        return self._start_preview_worker(quality_profile=quality_profile)

    def _start_preview_worker(self, quality_profile: str) -> bool:
        """Запустить preview только до начала общего shutdown."""
        lifecycle_lock, closed_event = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            if closed_event.is_set():
                return False
            return self._start_preview_worker_locked(quality_profile)

    def _start_preview_worker_locked(self, quality_profile: str) -> bool:
        """Опубликовать новое поколение preview под lifecycle-lock."""
        # wave-1770 MED: serialise thread-handle lifecycle (reentrant — calls _stop below).
        with self._preview_thread_lock:
            if not self._stop_preview_worker():
                logger.warning(
                    "Realtime preview не перезапущен: прежний worker ещё жив"
                )
                return False
            if not callable(getattr(self.transcriber, "transcribe_preview", None)):
                logger.info(
                    "Realtime preview disabled: transcriber %s не имеет метода transcribe_preview",
                    type(self.transcriber).__name__,
                )
                return False
            # У каждого поколения свой Event. Старый заблокированный worker
            # никогда не увидит clear() нового запуска и не сможет ожить.
            stop_event = threading.Event()
            self._preview_stop_event = stop_event
            self._preview_thread = threading.Thread(
                target=self._preview_loop,
                args=(quality_profile, stop_event),
                daemon=True,
            )
            self._preview_thread.start()
            return True

    def _stop_preview_worker(self) -> bool:
        # wave-1770 MED: serialise thread-handle lifecycle. RLock so _start (which holds
        # it) can call this. join() is safe here — the worker writes _preview_text under
        # the SEPARATE _preview_lock, never _preview_thread_lock, so no deadlock.
        with self._preview_thread_lock:
            thread = self._preview_thread
            stop_event = self._preview_stop_event
            stop_event.set()
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=IPC_PREVIEW_THREAD_TIMEOUT_SEC)
            if thread is not None and thread.is_alive():
                logger.warning(
                    "Realtime preview worker не завершился за %.1f с",
                    IPC_PREVIEW_THREAD_TIMEOUT_SEC,
                )
                return False
            if self._preview_thread is thread:
                self._preview_thread = None
            return True

    def begin_shutdown(self) -> None:
        """Запретить новые start без ожидания текущего lifecycle-перехода."""
        _, closed_event = self._ensure_recording_lifecycle_state()
        closed_event.set()

    def abort_recording_if_owner(
        self,
        expected_owner: str,
        *,
        lifecycle_lock_timeout_sec: float = (
            _SHUTDOWN_LIFECYCLE_LOCK_TIMEOUT_SEC
        ),
    ) -> bool:
        """Аварийно погасить запись, если текущий владелец совпадает.

        Это компенсационный путь MeetingSessionService: свежий meeting-start
        мог завершить recorder.start() одновременно с ошибкой/close(). Проверка
        owner и аварийная остановка выполняются под одним lifecycle-lock.
        begin_shutdown() перед close-компенсацией запрещает новое поколение;
        generation остаётся retry-handle до подтверждённой остановки всех
        recorder/RT-worker-ов и только затем атомарно снимается.
        """
        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        lock_timeout = max(0.0, float(lifecycle_lock_timeout_sec))
        if not lifecycle_lock.acquire(timeout=lock_timeout):
            logger.error(
                "Recording lifecycle-lock не освобождён за %.2f с; "
                "owner-bound abort не выполнен",
                lock_timeout,
                extra={"shutdown_blocker": "recording_owner_abort"},
            )
            return False
        try:
            generation = getattr(self, "_active_generation", None)
            active_owner = (
                generation.get("owner") if generation is not None else None
            )
            recorder_is_recording = bool(
                getattr(self.recorder, "is_recording", False)
            )
            if (
                active_owner != str(expected_owner)
                and not (
                    active_owner is None
                    and not recorder_is_recording
                )
            ):
                logger.warning(
                    "Owner-bound abort отклонён: ожидался %r, активен %r",
                    expected_owner,
                    active_owner,
                )
                return False

            # Даже при уже сброшенном recorder-флаге повторяем весь teardown:
            # первый abort мог остановить микрофон, но сохранить preview/RT/RSF
            # handle после timeout. Idle-флаг сам по себе не является
            # подтверждением, что все owned worker-ы завершены.
            all_workers_stopped = self._abort_recording_workers_locked()
            microphone_stopped = not bool(
                getattr(self.recorder, "is_recording", False)
            )
            recorder_worker_stopped = not self._recorder_worker_alive()
            if not all_workers_stopped:
                logger.error(
                    "Owner-bound abort: flag_stopped=%s, worker_stopped=%s, "
                    "но не все worker-ы подтвердили остановку",
                    microphone_stopped,
                    recorder_worker_stopped,
                )
            return (
                all_workers_stopped
                and microphone_stopped
                and recorder_worker_stopped
            )
        finally:
            lifecycle_lock.release()

    def _recorder_worker_alive(self) -> bool:
        """Fail-closed проверить retained AudioRecorder thread-handle."""
        worker = getattr(self.recorder, "_thread", None)
        if worker is None:
            return False
        try:
            return bool(worker.is_alive())
        except Exception:
            logger.exception(
                "Не удалось проверить AudioRecorder worker — считаю живым"
            )
            return True

    def close_background_workers(
        self,
        *,
        lifecycle_lock_timeout_sec: float = (
            _SHUTDOWN_LIFECYCLE_LOCK_TIMEOUT_SEC
        ),
    ) -> bool:
        """Закрыть все recording-worker-ы без финальной транскрибации аудио.

        Порядок важен: потребители аудиобуфера завершаются до рекордера. Каждый
        этап изолирован от ошибок, а повторный вызов безопасен. False означает,
        что хотя бы один worker не подтвердил завершение за короткий shutdown-
        бюджет; его handle сохраняется для повторной попытки.
        """
        # Флаг ставится до ожидания lock: новый IPC-start сразу увидит shutdown,
        # а уже начавшийся setup сначала полностью опубликует свои handle.
        lifecycle_lock, closed_event = self._ensure_recording_lifecycle_state()
        closed_event.set()
        lock_timeout = max(0.0, float(lifecycle_lock_timeout_sec))
        if not lifecycle_lock.acquire(timeout=lock_timeout):
            logger.error(
                "Recording lifecycle-lock не освобождён за %.2f с; "
                "worker handles сохранены, но повторной остановки НЕ будет: "
                "координатор завершит процесс через os._exit (F5, ревью 2026-07-23)",
                lock_timeout,
                extra={"shutdown_blocker": "recording_lifecycle_lock"},
            )
            return False

        try:
            return self._abort_recording_workers_locked()
        finally:
            lifecycle_lock.release()

    def _abort_recording_workers_locked(self) -> bool:
        """Остановить consumers+recorder; caller уже держит lifecycle-lock."""
        all_stopped = True

        try:
            if self._stop_preview_worker() is False:
                all_stopped = False
        except Exception:
            all_stopped = False
            logger.exception("Ошибка при остановке realtime preview")

        # Атомарно забираем оба handle. При timeout возвращаем конкретный
        # объект в пустой slot, чтобы повторный close мог сделать retry.
        with self._rt_lock:
            rt_partial = self._rt_partial
            rsf = self._rsf
            self._rt_partial = None
            self._rsf = None

        if rt_partial is not None:
            try:
                rt_stopped = rt_partial.stop(
                    timeout_sec=_SHUTDOWN_RT_PARTIAL_TIMEOUT_SEC
                ) is not False
            except Exception:
                rt_stopped = False
                logger.exception(
                    "Ошибка при остановке RealtimePartialTranscriber"
                )
            if not rt_stopped:
                all_stopped = False
                with self._rt_lock:
                    if self._rt_partial is None:
                        self._rt_partial = rt_partial

        silence_ranges: list[tuple[float, float]] = []
        if rsf is not None:
            try:
                silence_ranges = rsf.stop(
                    timeout_sec=_SHUTDOWN_RSF_TIMEOUT_SEC
                ) or []
                rsf_stopped = not rsf.is_running
            except Exception:
                rsf_stopped = False
                logger.exception(
                    "Ошибка при остановке RealtimeSilenceFilter"
                )
            if not rsf_stopped:
                all_stopped = False
                with self._rt_lock:
                    if self._rsf is None:
                        self._rsf = rsf
        self._last_silence_ranges = silence_ranges

        try:
            abort = getattr(type(self.recorder), "abort", None)
            if callable(abort):
                abort_result = self.recorder.abort(
                    timeout_sec=_SHUTDOWN_RECORDER_TIMEOUT_SEC
                )
                recorder_stopped = (
                    abort_result is not False
                    and not bool(
                        getattr(self.recorder, "is_recording", False)
                    )
                )
            elif bool(getattr(self.recorder, "is_recording", False)):
                self.recorder.stop()
                recorder_stopped = not bool(
                    getattr(self.recorder, "is_recording", False)
                )
            else:
                recorder_stopped = True
            all_stopped = recorder_stopped and all_stopped
        except Exception:
            all_stopped = False
            recorder_stopped = False
            logger.exception("Ошибка при аварийной остановке AudioRecorder")

        recorder_worker_stopped = not self._recorder_worker_alive()
        if recorder_stopped:
            # AudioRecorder.abort() закрывает свой spill, но Core тоже владеет
            # ссылкой. Закрытие идемпотентно; файл оставляем для rescue
            # следующего запуска, а устаревший handle снимаем.
            spill = getattr(self, "_active_spill", None)
            self._active_spill = None
            if spill is not None:
                try:
                    spill.close()
                except Exception:
                    logger.debug(
                        "RecordingSpill: close при abort упал",
                        exc_info=True,
                    )

        all_stopped = (
            all_stopped
            and recorder_stopped
            and recorder_worker_stopped
        )
        if all_stopped:
            # Generation — тоже retry-handle. Снимать её раньше нельзя:
            # повторный owner-bound abort обязан отличать recovery своей записи
            # от попытки погасить чужое более новое поколение.
            self._clear_active_generation_locked()

        return all_stopped

    @staticmethod
    def _preview_tail_is_silent(audio: Any, sample_rate: int) -> bool:
        """RMS последних ``_PREVIEW_SILENCE_TAIL_SEC`` секунд хвоста ниже порога тишины.

        Дёшево — не требует отдельной транскрибации хвоста (спека §2). Порог —
        ``SILENCE_THRESHOLD_DB_PRESERVE_WHISPER`` (-55 дБ), тот же, что уже
        использует ``RealtimeSilenceFilter`` для STT-путей: сохраняет тихую
        речь и шёпот, не даёт ложно принять их за тишину.
        """
        flat = np.asarray(audio).reshape(-1)
        if flat.size == 0:
            return False
        window_samples = max(1, int(sample_rate * _PREVIEW_SILENCE_TAIL_SEC))
        window = flat[-window_samples:].astype(np.float64)
        rms = float(np.sqrt(np.mean(window ** 2)))
        return rms < _PREVIEW_SILENCE_THRESHOLD_AMP

    def _preview_loop(
        self,
        quality_profile: str,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Realtime-превью диктовки: курсор вместо скользящего окна (R3).

        Спека 2026-08-13-incremental-preview-design.md. Зафиксированный
        префикс (``committed_text``) больше не перераспознаётся — на каждой
        итерации STT получает только НОВЫЙ хвост
        ``snapshot_range(cursor_sec, upto)`` (тот же примитив, что
        ``MeetingSessionService._job_chunk_stt`` уже использует для
        аккумулятора встреч). Курсор сдвигается только когда хвост фиксации
        достаточно вырос И (кончается тишиной ИЛИ дорос до потолка) — иначе
        каждая итерация резала бы речь на произвольной границе.
        """
        # Optional оставлен для legacy-тестов, вызывающих loop напрямую.
        worker_stop_event = stop_event or self._preview_stop_event
        get_duration_sec = getattr(self.recorder, "get_duration_sec", None)
        snapshot_range = getattr(self.recorder, "snapshot_range", None)
        sample_rate = int(getattr(self.recorder, "sample_rate", 16000) or 16000)
        poll_interval = 0.35
        _POLL_MIN = 0.35
        _POLL_MAX = 1.5
        _POLL_BACKOFF = 1.5

        # F2 (спека 2026-08-12): переполнение аудиобуфера — сигнал, что
        # система не успевает вычитывать поток захвата. Приоритет — сама
        # запись, превью — украшение; growing overflow_count заставляет
        # превью пропустить транскрибацию этой итерации и отступить
        # экспоненциально, вместо того чтобы продолжать конкурировать с
        # захватом за GPU/CPU (живой инцидент: 9 превью-транскрибаций подряд
        # + переполнение буфера в одном окне).
        last_overflow_count = int(getattr(self.recorder, "overflow_count", 0) or 0)
        # Свежий вход в loop — свежий эпизод бэкоффа (новый поток на каждую
        # запись, см. комментарий на self._preview_overflow_backoff_sec).
        self._preview_overflow_backoff_sec = 0.0
        _PREVIEW_BACKOFF_MAX = 8.0
        # 2026-08-13: НИЖНЯЯ граница бэкоффа обязана превышать собственную
        # максимальную паузу цикла (_POLL_MAX). Иначе «отступление» ничего не
        # замедляет: прежний старт 0.5с был ВТРОЕ меньше наблюдаемой каденции
        # превью (~1.45с), поэтому первые два срабатывания F2 были фактическими
        # no-op — живой замер 08-13 показал 4 переполнения буфера за одну
        # запись УЖЕ ПОСЛЕ срабатывания бэкоффа. Выводим из _POLL_MAX, а не
        # берём константой с потолка: связь «бэкофф > каденции» обязана
        # пережить будущую правку _POLL_MAX.
        _PREVIEW_BACKOFF_MIN = _POLL_MAX * 2
        _PREVIEW_OVERFLOW_CLEAN_STREAK = 3
        overflow_clean_streak = 0
        overflow_backoff_logged = False

        # R3: оба поля инициализируются на КАЖДЫЙ вход в loop (новый поток на
        # каждую запись) — тот же паттерн, что _preview_overflow_backoff_sec.
        committed_text = ""
        cursor_sec = 0.0

        while not worker_stop_event.is_set():
            if not bool(getattr(self.recorder, "is_recording", False)):
                break

            if not callable(get_duration_sec) or not callable(snapshot_range):
                worker_stop_event.wait(poll_interval)
                continue

            try:
                upto = float(get_duration_sec())
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка get_duration_sec")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                worker_stop_event.wait(poll_interval)
                continue

            # get_duration_sec мог отработать уже после shutdown request;
            # новый STT уже нельзя запускать даже если значение валидно.
            if worker_stop_event.is_set():
                break

            with self._preview_lock:
                self._preview_duration_sec = upto

            tail_sec = upto - cursor_sec
            if tail_sec < _PREVIEW_MIN_TAIL_SEC:
                worker_stop_event.wait(_POLL_MIN)
                continue

            # F2: overflow_count вырос с прошлой проверки — система не
            # тянет захват аудио. Пропускаем эту транскрибацию и отступаем
            # экспоненциально (не ломая существующий poll_interval — тот
            # управляет ТОЛЬКО реакцией на исключения/недостаток аудио).
            current_overflow_count = int(getattr(self.recorder, "overflow_count", 0) or 0)
            if current_overflow_count > last_overflow_count:
                last_overflow_count = current_overflow_count
                overflow_clean_streak = 0
                self._preview_overflow_backoff_sec = min(
                    max(self._preview_overflow_backoff_sec * 2, _PREVIEW_BACKOFF_MIN),
                    _PREVIEW_BACKOFF_MAX,
                )
                if not overflow_backoff_logged:
                    logger.warning(
                        "Realtime preview: переполнение аудиобуфера (overflow_count=%d), "
                        "превью отступает: backoff=%.2fс",
                        current_overflow_count, self._preview_overflow_backoff_sec,
                    )
                    overflow_backoff_logged = True
                worker_stop_event.wait(self._preview_overflow_backoff_sec)
                continue
            elif self._preview_overflow_backoff_sec > 0.0:
                # Плавное снятие: N чистых итераций подряд делят бэкофф
                # пополам, а не сбрасывают его резко — система могла ещё не
                # остыть от перегруза.
                overflow_clean_streak += 1
                if overflow_clean_streak >= _PREVIEW_OVERFLOW_CLEAN_STREAK:
                    overflow_clean_streak = 0
                    self._preview_overflow_backoff_sec /= 2
                    # 2026-08-13: снимаем полностью, как только половина
                    # опустилась НИЖЕ рабочей границы. Значение меньше
                    # _PREVIEW_BACKOFF_MIN всё равно не замедляет цикл (оно
                    # короче собственной паузы _POLL_MAX) — тянуть спад через
                    # 1.5→0.75→0.37… было бы имитацией отступления, а не
                    # отступлением. Прежний порог 0.05 достался от старой
                    # нижней границы 0.5с.
                    if self._preview_overflow_backoff_sec < _PREVIEW_BACKOFF_MIN:
                        self._preview_overflow_backoff_sec = 0.0
                        overflow_backoff_logged = False

            try:
                audio = snapshot_range(cursor_sec, upto)
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка snapshot_range")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                worker_stop_event.wait(poll_interval)
                continue

            if worker_stop_event.is_set():
                break

            if int(getattr(audio, "size", 0)) == 0:
                # Гонка курсора со свежими чанками рекордера (ещё не долетели
                # до буфера) — не наш хвост потерян, просто ждём следующего тика.
                worker_stop_event.wait(_POLL_MIN)
                continue

            try:
                preview_payload = self.transcriber.transcribe_preview(
                    audio,
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
                worker_stop_event.wait(poll_interval)
                continue

            # stop() мог истечь по таймауту, пока transcribe_preview был
            # заблокирован. Такой результат уже не принадлежит живой сессии.
            if worker_stop_event.is_set():
                break

            if self._preview_error_count > 0:
                self._preview_error_last_reset_ts = time.time()
            self._preview_error_count = 0

            tail_silent = self._preview_tail_is_silent(audio, sample_rate)
            # Пустой текст имеет ДВА несовместимых источника: в хвосте правда
            # нет речи ИЛИ transcribe_preview не получил bounded mlx_lock и
            # вернул маркер (transcriber.py:152). Второй случай ничего не
            # говорит о содержимом хвоста, поэтому фиксировать курсор по нему
            # нельзя даже при тихих последних 0.4с: речь могла звучать раньше
            # в том же хвосте, а пауза в конце — просто совпадение.
            preview_skipped = (
                isinstance(preview_payload, dict)
                and bool(preview_payload.get("skipped"))
            )
            # None — отображение не трогаем вовсе (см. else-ветку ниже).
            display_text: str | None

            if preview_text:
                display_text = committed_text + preview_text
                commit_now = tail_sec >= _PREVIEW_COMMIT_MIN_SEC and (
                    tail_silent or tail_sec >= _PREVIEW_MAX_TAIL_SEC
                )
                if commit_now:
                    committed_text = display_text
                    cursor_sec = upto
                poll_interval = _POLL_MIN
            elif tail_silent and not preview_skipped:
                # Тихий хвост фиксируется БЕЗ добавления текста: курсор идёт
                # вперёд, committed_text не меняется — иначе минутная пауза
                # раз за разом гоняла бы через STT одну и ту же тишину до
                # MAX_TAIL_SEC на каждом проходе (спека §3).
                if tail_sec >= _PREVIEW_COMMIT_MIN_SEC:
                    cursor_sec = upto
                display_text = committed_text
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
            else:
                # Пустой текст, про который НЕЛЬЗЯ утверждать, что в хвосте не
                # было речи: либо хвост не тих, либо пришёл маркер skipped
                # (промах bounded mlx_lock). Фиксировать нельзя — превью
                # потеряло бы фразу; MAX_TAIL_SEC тут исключения не даёт.
                # Отображение не трогаем: иначе был бы виден откат текста.
                display_text = None
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)

            if display_text is not None:
                capped = display_text[-900:]
                with self._preview_lock:
                    self._preview_text = capped
                    self._preview_updated_at = upto
                if capped:
                    # W1673 F2: privacy gate — do not leak transcript text via SSE in privacy mode.
                    _preview_settings = self._settings_svc.cached_settings()
                    if not bool(_preview_settings.get("privacy_mode_enabled", False)):
                        event_bus.emit_typed(EventType.STT_PARTIAL, SttPartial(
                            text=capped,
                            duration_sec=upto,
                        ))

            worker_stop_event.wait(poll_interval)

    # ------------------------------------------------------------------ #
    # stop_recording phase helpers                                         #
    # ------------------------------------------------------------------ #

    def _build_empty_audio_response(
        self,
        duration_sec: float,
        quality_profile: str,
        cleanup_profile: str,
        translation_mode: str,
        translate_and_paste: bool,
        stop_tail_trim_ms: int,
        silence_detected: bool = False,
        silence_guard_enabled: bool = False,
        background_guard_rejected: bool = False,
    ) -> dict[str, Any]:
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
            "silence_detected": silence_detected,
            "silence_guard_enabled": silence_guard_enabled,
            "background_guard_rejected": background_guard_rejected,
        }

    def _load_stop_recording_settings(
        self, params: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "quality_profile": str(params.get("quality_profile") or settings.get("quality_profile", "balanced")),
            "cleanup_profile": str(params.get("cleanup_profile") or settings.get("cleanup_profile", "soft")),
            "lang_hint": params.get("lang_hint") or None,
            "translation_mode": str(params.get("translation_mode") or settings.get("translation_mode", "off")),
            "translation_style": str(params.get("translation_style") or settings.get("translation_style", "neutral")),
            "translation_glossary": settings.get("translation_glossary", {}),
            "translate_and_paste": bool(
                params.get("translate_and_paste")
                if "translate_and_paste" in params
                else settings.get("translate_and_paste", False)
            ),
            "network_mode": str(settings.get("network_mode", "offline_default")),
            "silence_guard_enabled": self._coerce_bool(settings.get("silence_guard_enabled", True), default=True),
            "silence_rms_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_rms_threshold", 0.0020),
                default=0.0020, min_value=0.0003, max_value=0.05,
            ),
            "silence_peak_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_peak_threshold", 0.0120),
                default=0.0120, min_value=0.001, max_value=0.2,
            ),
            "silence_active_ratio_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_active_ratio_threshold", 0.015),
                default=0.015, min_value=0.001, max_value=0.30,
            ),
            "background_guard_enabled": self._coerce_bool(settings.get("background_guard_enabled", True), default=True),
            "background_guard_min_peak": self._coerce_bounded(
                value=settings.get("background_guard_min_peak", 0.025),
                default=0.025, min_value=0.003, max_value=0.25,
            ),
            "background_guard_min_rms": self._coerce_bounded(
                value=settings.get("background_guard_min_rms", 0.0040),
                default=0.0040, min_value=0.0008, max_value=0.08,
            ),
            "background_guard_uniform_frame_threshold": self._coerce_bounded(
                value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
                default=0.0060, min_value=0.001, max_value=0.20,
            ),
            "background_guard_max_uniform_active_ratio": self._coerce_bounded(
                value=settings.get("background_guard_max_uniform_active_ratio", 0.92),
                default=0.92, min_value=0.40, max_value=0.99,
            ),
            "sample_rate": self._coerce_bounded(
                value=getattr(self.recorder, "sample_rate", 16000),
                default=16000, min_value=8000, max_value=192000,
            ),
            "privacy_mode_enabled": bool(settings.get("privacy_mode_enabled", False)),
        }

    def _stop_recording_phase_a(
        self, params: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Сериализовать короткую фазу остановки с setup новой записи."""
        lifecycle_lock, _closed_event = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            return self._stop_recording_phase_a_locked(params, settings)

    def _stop_recording_phase_a_locked(
        self, params: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Stop preview worker, stop realtime partial, stop recorder."""
        # R2 F6: гейт обязан быть первой операцией под lifecycle-lock. Даже
        # preview/RT/RSF живой чужой записи нельзя трогать до решения по token.
        gate_response = self._stop_gate_decision(params)
        if gate_response is not None:
            return {"early_return": gate_response}

        self._stop_preview_worker()
        rt_session_id = self._rt_session_id

        # W1776: capture bookmark_session_id (the session_tracker UUID that the Swift
        # client reads via get_recording_state and passes to add_bookmark) BEFORE the
        # recorder stops — _active_session is still valid here; end_session() will clear
        # it later in phase_e.  Falls back to "__live__" when no active session exists
        # (e.g. privacy_mode or testing without session_tracker.start_session).
        _active = getattr(self._session_tracker, "_active_session", None)
        bookmark_session_id: str = (
            (_active.get("session_id") or "__live__") if _active else "__live__"
        )
        # wave-27 MED (race): claim the handle and clear the field atomically under
        # _rt_lock, so a concurrent start_recording can never see a stopped-but-still
        # -published transcriber (and we never double-stop / orphan). The (idempotent)
        # stop() itself runs after the swap — outside the lock — to avoid holding it
        # across blocking thread-join work.
        with self._rt_lock:
            _rt_partial = self._rt_partial
            self._rt_partial = None
        if _rt_partial is not None:
            try:
                rt_stopped = _rt_partial.stop() is not False
            except Exception:
                rt_stopped = False
                logger.exception("Ошибка при остановке RealtimePartialTranscriber")
            if not rt_stopped:
                with self._rt_lock:
                    if self._rt_partial is None:
                        self._rt_partial = _rt_partial

        # W1325 F1 HIGH: stop RSF and capture silence_ranges
        _silence_ranges: list[tuple[float, float]] = []
        with self._rt_lock:
            _rsf = self._rsf
            self._rsf = None
        if _rsf is not None:
            try:
                _silence_ranges = _rsf.stop() or []
                rsf_stopped = not _rsf.is_running
            except Exception:
                rsf_stopped = False
                logger.exception("Ошибка при остановке RealtimeSilenceFilter")
                _silence_ranges = []
            if not rsf_stopped:
                with self._rt_lock:
                    if self._rsf is None:
                        self._rsf = _rsf
        self._last_silence_ranges = _silence_ranges

        stop_tail_trim_ms = self._coerce_bounded(
            value=params.get("stop_tail_trim_ms", settings.get("stop_tail_trim_ms", 180)),
            default=180,
            min_value=0,
            max_value=1200,
        )
        try:
            stopped = self._stop_recorder_guarded(stop_tail_trim_ms=stop_tail_trim_ms)
        except AudioRecorderStopTimeout:
            # F2 (Fable-ревью 2026-07-22): зависший audio-worker раньше выглядел
            # как идемпотентный already_stopped — Swift молчал, диктовка терялась
            # без следа. Отдаём различимый статус + превью как шанс спасения.
            logger.error(
                "stop_recording: audio worker завис — отдаю recorder_timeout"
            )
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "early_return": {
                    "status": "recorder_timeout",
                    "is_recording": False,
                    "duration_sec": preview_duration,
                    "preview_text": preview_text,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                }
            }
        if stopped is None:
            # Рекордер уже idle: stale generation больше не имеет живой записи.
            # Локальная ссылка нужна outer terminalizer-у и будущему replay.
            generation = (
                self._move_active_generation_to_finalizing_locked()
            )
            spill = getattr(self, "_active_spill", None)
            self._active_spill = None
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "spill": spill,
                "generation": generation,
                "early_return": {
                    "status": "already_stopped",
                    "is_recording": False,
                    "duration_sec": preview_duration,
                    "preview_text": preview_text,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                }
            }

        audio, duration_sec = stopped
        # Spill принадлежит этому физически завершённому поколению. Забираем
        # его под lifecycle-lock ДО допуска start/shutdown следующего перехода.
        spill = getattr(self, "_active_spill", None)
        self._active_spill = None
        # Physical stop необратим: G1 должна стать finalizing ДО любого
        # fallible hook, иначе новый start G2 мог бы перезаписать stale active G1.
        generation = self._move_active_generation_to_finalizing_locked()
        try:
            add_breadcrumb(
                category="recording",
                message="stopped",
                level="info",
                data={"duration_sec": round(float(duration_sec), 2)},
            )

            # Brain lease coordination: acquire lease before preloading brain
            # so Krab userbot knows Ear is about to use LM Studio on Metal GPU.
            if bool(settings.get("llm_brain_lease_enabled", True)):
                try:
                    from backend.brain_lease import acquire_brain_lease
                    ttl = float(
                        settings.get("llm_brain_lease_ttl_sec", 30.0)
                    )
                    acquire_brain_lease("krab_ear", ttl_sec=ttl)
                except Exception as exc:
                    logger.debug(
                        "BrainLease: acquire hook error (ignored): %s",
                        exc,
                    )

            try:
                # MED-3: бамп безусловный (не гейтован brain_model/preload) —
                # ниже по коду STT точно отработает независимо от настроек
                # brain-preload; сиблинг-асимметрия со start-path раньше
                # позволяла кондуктору посчитать rewriter простаивающим
                # ровно тогда, когда он вот-вот понадобится финализации.
                bump_stt_activity()
                brain_model = str(
                    settings.get("llm_brain_model", "")
                ).strip()
                preload_enabled = bool(
                    settings.get("llm_brain_preload_on_stop", True)
                )
                if brain_model and preload_enabled:
                    conductor = getattr(self, "_memory_conductor", None)
                    # C-NO-PINGPONG: под enforced pressure-streak reload 19 ГБ
                    # пропускается (иначе лестница и reload дерутся за память).
                    if conductor is None or conductor.reload_brain_allowed():
                        from backend.lm_studio_lifecycle import load_model_async
                        base_url = str(
                            settings.get(
                                "llm_base_url",
                                "http://localhost:1234/v1",
                            )
                        )
                        load_model_async(base_url, brain_model)
                    else:
                        logger.info("brain reload skipped by memory conductor (pressure)")
            except Exception as exc:
                logger.debug(
                    "LM Studio brain preload hook failed: %s",
                    exc,
                )

            sr = self._load_stop_recording_settings(params, settings)

            if getattr(audio, "size", 0) == 0:
                return {
                    "spill": spill,
                    "generation": generation,
                    "early_return": self._build_empty_audio_response(
                        duration_sec=duration_sec,
                        quality_profile=sr["quality_profile"],
                        cleanup_profile=sr["cleanup_profile"],
                        translation_mode=sr["translation_mode"],
                        translate_and_paste=sr["translate_and_paste"],
                        stop_tail_trim_ms=stop_tail_trim_ms,
                    )
                }

            return {
                "audio": audio,
                "duration_sec": duration_sec,
                "stop_tail_trim_ms": stop_tail_trim_ms,
                "rt_session_id": rt_session_id,
                "bookmark_session_id": bookmark_session_id,
                "sr": sr,
                "spill": spill,
                "generation": generation,
            }
        except Exception as exc:
            logger.exception(
                "stop_recording: phase A упала после physical stop"
            )
            if spill is not None:
                spill.close()
            return {
                "spill": spill,
                "generation": generation,
                "early_return": self._build_finalization_failed_response(
                    generation,
                    exc,
                ),
            }

    def _stop_recording_phase_b(
        self,
        audio: Any,
        duration_sec: float,
        stop_tail_trim_ms: int,
        sr: dict[str, Any],
    ) -> dict[str, Any]:
        """Run silence guard and background guard."""
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]
        translation_mode = sr["translation_mode"]
        translate_and_paste = sr["translate_and_paste"]
        sample_rate = sr["sample_rate"]

        silence_detected = False
        if sr["silence_guard_enabled"]:
            silence_detected = self._looks_like_silence_audio(
                audio=audio,
                sample_rate=sample_rate,
                rms_threshold=sr["silence_rms_threshold"],
                peak_threshold=sr["silence_peak_threshold"],
                active_ratio_threshold=sr["silence_active_ratio_threshold"],
            )
            if silence_detected:
                logger.info(
                    "Silence guard: stop_recording классифицирован как тишина, STT пропущен",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "rms_threshold": sr["silence_rms_threshold"],
                        "peak_threshold": sr["silence_peak_threshold"],
                        "active_ratio_threshold": sr["silence_active_ratio_threshold"],
                    },
                )
                return {
                    "early_return": self._build_empty_audio_response(
                        duration_sec=duration_sec,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        translation_mode=translation_mode,
                        translate_and_paste=translate_and_paste,
                        stop_tail_trim_ms=stop_tail_trim_ms,
                        silence_detected=True,
                        silence_guard_enabled=True,
                    )
                }

        background_guard_rejected = False
        if sr["background_guard_enabled"]:
            background_guard_rejected = self._looks_like_distant_background_speech(
                audio=audio,
                sample_rate=sample_rate,
                min_peak=sr["background_guard_min_peak"],
                min_rms=sr["background_guard_min_rms"],
                uniform_frame_threshold=sr["background_guard_uniform_frame_threshold"],
                max_uniform_active_ratio=sr["background_guard_max_uniform_active_ratio"],
            )
            if background_guard_rejected:
                logger.info(
                    "Background guard: stop_recording отклонен как фоновая речь",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "min_peak": sr["background_guard_min_peak"],
                        "min_rms": sr["background_guard_min_rms"],
                        "uniform_frame_threshold": sr["background_guard_uniform_frame_threshold"],
                        "max_uniform_active_ratio": sr["background_guard_max_uniform_active_ratio"],
                    },
                )
                return {
                    "early_return": self._build_empty_audio_response(
                        duration_sec=duration_sec,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        translation_mode=translation_mode,
                        translate_and_paste=translate_and_paste,
                        stop_tail_trim_ms=stop_tail_trim_ms,
                        silence_guard_enabled=sr["silence_guard_enabled"],
                        background_guard_rejected=True,
                    )
                }

        return {
            "silence_detected": silence_detected,
            "background_guard_rejected": background_guard_rejected,
        }

    def _stop_recording_phase_c(
        self,
        audio: Any,
        duration_sec: float,
        sr: dict[str, Any],
    ) -> dict[str, Any]:
        """Load vocabulary/context/glossary and run the transcriber."""
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]
        lang_hint: str | None = sr["lang_hint"]

        user_vocabulary = self.vocabulary.load() or []

        # W1669: gate history injection on privacy_mode — do not fetch or pass
        # past transcripts to Whisper initial_prompt when privacy mode is active.
        _privacy_mode = sr.get("privacy_mode_enabled", False)  # W1707: use .get to tolerate missing key in tests
        if _privacy_mode:
            _recent_history: list = []
        else:
            _recent_history, _ = self.store.get_history_page(cursor=None, limit=10)
        _cached_settings_hw = self._settings_svc.cached_settings()
        _stt_hotwords_enabled = bool(_cached_settings_hw.get("stt_hotwords_enabled", True))
        _stt_hotwords: list[str] = (
            _cached_settings_hw.get("stt_hotwords", []) if _stt_hotwords_enabled else []
        )

        _auto_glossary_terms: list[str] = []
        _cached_settings_ag = self._settings_svc.cached_settings()
        _ag_window_days = int(_cached_settings_ag.get("auto_glossary_window_days", DEFAULT_SETTINGS.get("auto_glossary_window_days", 7)))
        _ag_top_n = int(_cached_settings_ag.get("auto_glossary_top_n", DEFAULT_SETTINGS.get("auto_glossary_top_n", 30)))
        if _cached_settings_ag.get("auto_glossary_enabled", DEFAULT_SETTINGS.get("auto_glossary_enabled", True)):
            try:
                _auto_glossary_terms = self._auto_glossary.build(
                    window_days=_ag_window_days, top_n=_ag_top_n
                )
            except Exception as _ag_exc:
                logger.warning("auto_glossary: ошибка при построении глоссария: %s", _ag_exc)

        _combined_hotwords: list[str] | None = None
        if _stt_hotwords or _auto_glossary_terms:
            _seen_hw: set[str] = set()
            _combined_hw: list[str] = []
            for _w in list(_stt_hotwords) + list(_auto_glossary_terms):
                _w = _w.strip()
                if _w and _w.lower() not in _seen_hw:
                    _seen_hw.add(_w.lower())
                    _combined_hw.append(_w)
            _combined_hotwords = _combined_hw if _combined_hw else None

        add_breadcrumb(
            category="transcription",
            message="transcribe_start",
            level="info",
            data={
                "quality_profile": quality_profile,
                "audio_len_sec": round(float(duration_sec), 2),
                "lang_hint": lang_hint or "auto",
                "auto_glossary_terms": len(_auto_glossary_terms),
            },
        )

        _phase_c_settings = self._settings_svc.cached_settings()
        _diarize_enabled = _phase_c_settings.get("diarization_enabled", False)
        # F1 (спека 2026-08-12): гейт по длительности — прежде диаризация
        # (~1x realtime на MPS) гонялась безусловно, единственные гейты были
        # is_preview и отсутствие HF-токена; короткая диктовка (40-60с) тащила
        # полный прогон наравне с часовой записью (живой инцидент: 44с
        # диаризации на 42с диктовке). Гейт живёт ТОЛЬКО в этом методе — путь
        # встречи диаризует ДРУГИМ методом (engine.diarize_window(), не
        # _maybe_run_diarization(); см. source-контракт в
        # test_dictation_latency_overflow_2026_08_12.py).
        if _diarize_enabled:
            _diar_min_duration = _phase_c_settings.get(
                "diarization_min_duration_sec",
                DEFAULT_SETTINGS.get("diarization_min_duration_sec", 90.0),
            )
            try:
                _diar_min_duration_f = float(_diar_min_duration)
                _duration_sec_f = float(duration_sec)
            except (TypeError, ValueError):
                # Fail-open: не смогли распарсить длительность/порог —
                # диаризуем как раньше, а не теряем фичу молча.
                _diar_min_duration_f = 0.0
                _duration_sec_f = 0.0
            if _diar_min_duration_f > 0 and _duration_sec_f < _diar_min_duration_f:
                logger.info(
                    "phase_c: диаризация пропущена — запись короче порога",
                    extra={
                        "duration_sec": round(_duration_sec_f, 2),
                        "diarization_min_duration_sec": _diar_min_duration_f,
                    },
                )
                _diarize_enabled = False
        _sil_ranges = getattr(self, "_last_silence_ranges", None) or None

        # Спасательная копия ДО транскрибации: зависший STT (дедлок mlx_lock,
        # PortAudio-wedge) не бросает исключение — except ниже не выполнится, а
        # вотчдог перезапустит backend и аудио из памяти пропадёт (инцидент
        # 01.08.2026, три диктовки подряд). После успеха файл unlink'ается;
        # opt-in debug_keep_dictation_wav переносит его в debug_duration_wav/.
        _presaved_path: Path | None = None
        audio_recovery_path: str | None = None
        try:
            import uuid as _uuid

            import soundfile as _sf
            _failed_dir = Path(self.store.data_dir) / "failed_recordings"
            _failed_dir.mkdir(parents=True, exist_ok=True)
            _presaved_path = _failed_dir / f"{_uuid.uuid4().hex}.wav"
            _sf.write(
                str(_presaved_path), audio,
                int(getattr(self.recorder, "sample_rate", 16000)),
            )
            audio_recovery_path = str(Path("failed_recordings") / _presaved_path.name)
        except Exception as _pre_exc:
            _presaved_path = None
            logger.warning("phase_c: не удалось сохранить спасательную копию аудио: %s", _pre_exc)

        try:
            transcribe_payload = self.transcriber.transcribe(
                audio,
                quality_profile=quality_profile,
                cleanup_profile=cleanup_profile,
                lang_hint=lang_hint,
                extra_vocabulary=user_vocabulary if user_vocabulary else None,
                history_context=_recent_history if _recent_history else None,
                stt_hotwords=_combined_hotwords,
                settings=_phase_c_settings,
                diarize=_diarize_enabled if _diarize_enabled else None,
                silence_ranges=_sil_ranges,
            )
        except Exception as _stt_exc:
            logger.exception("phase_c: STT crashed", extra={"quality_profile": quality_profile})
            # Спасательная копия уже на диске (см. presave выше) — её и отдаём.
            if self._error_bus is not None:
                try:
                    from backend.error_bus import KrabError
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone as _tz
                    _e = ERROR_REGISTRY.get("stt.transcribe_failed", {})
                    self._error_bus.push(KrabError(
                        severity=_e.get("severity", "error"), component="stt",
                        code="stt.transcribe_failed",
                        message_user=_e.get("user_msg_ru", "STT: ошибка"),
                        message_debug=f"{type(_stt_exc).__name__}: {_stt_exc}",
                        timestamp=datetime.now(_tz.utc),
                        context={"quality_profile": quality_profile, "exc_type": type(_stt_exc).__name__},
                        actionable=False, action_id=None,
                    ))
                except Exception:
                    pass
            return {"early_return": {"ok": False, "error": "stt_failed",
                                     "error_detail": str(_stt_exc),
                                     "audio_recovery_path": audio_recovery_path,
                                     "status": "stt_failed"}}

        # STT дошёл до конца. По умолчанию спасательная копия больше не нужна.
        # Opt-in W2b: debug_keep_dictation_wav переносит WAV в debug_duration_wav/
        # для замера аномалии длительности (дефолт выкл).
        if _presaved_path is not None:
            try:
                if bool(self._get_runtime_setting("debug_keep_dictation_wav", False)):
                    self._keep_debug_dictation_wav(
                        src=_presaved_path,
                        audio=audio,
                        sample_rate=int(getattr(self.recorder, "sample_rate", 16000) or 16000),
                    )
                else:
                    _presaved_path.unlink(missing_ok=True)
                    _presaved_path.parent.rmdir()
            except OSError:
                pass  # каталог непустой (чужие спасённые записи) — так и надо

        return {"transcribe_payload": transcribe_payload}

    def _keep_debug_dictation_wav(
        self,
        src: Path,
        audio: Any,
        sample_rate: int,
    ) -> None:
        """W2b: перенести успешный presave WAV в debug_duration_wav/ + сидкар.

        Текст транскрипта в сидкар не пишется. history_id на этом этапе ещё
        неизвестен (история пишется в phase_d) — ключ оставляем null.
        """
        dest_dir = Path(self.store.data_dir) / "debug_duration_wav"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        src.replace(dest)
        try:
            src.parent.rmdir()
        except OSError:
            pass

        sr = int(sample_rate) if sample_rate else 16000
        arr = np.asarray(audio)
        nframes = int(arr.shape[0]) if arr.ndim >= 1 else 0
        vad_total_sec = float(nframes / sr) if sr > 0 else 0.0
        chunker_duration_sec = vad_total_sec
        try:
            from core.engine import AudioEngine

            mono = AudioEngine._resample_audio_to_mono_16k(arr, sr)
            chunker_duration_sec = float(len(mono) / 16000.0)
        except Exception:
            logger.warning(
                "debug_keep_dictation_wav: не удалось посчитать chunker_duration_sec",
                exc_info=True,
            )
        sidecar = {
            "vad_total_sec": vad_total_sec,
            "chunker_duration_sec": chunker_duration_sec,
            "wav_nframes": nframes,
            "sample_rate": sr,
            "history_id": None,
        }
        dest.with_suffix(".json").write_text(
            json.dumps(sidecar, ensure_ascii=True),
            encoding="utf-8",
        )

    def _stop_recording_phase_d(
        self,
        transcribe_payload: Any,
        duration_sec: float,
        sr: dict[str, Any],
        stop_tail_trim_ms: int,
        silence_detected: bool,
        silence_guard_enabled: bool,
        background_guard_rejected: bool,
    ) -> dict[str, Any]:
        """Extract text, apply soft-cleanup retry, translate, diarize."""
        translation_mode = sr["translation_mode"]
        translation_style = sr["translation_style"]
        translation_glossary = sr["translation_glossary"]
        translate_and_paste = sr["translate_and_paste"]
        network_mode = sr["network_mode"]
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]

        text = self._postprocess_transcribed_text(self._extract_transcribed_text(transcribe_payload))
        transcription_error = self._extract_transcribed_error(transcribe_payload)

        if not text and not transcription_error:
            raw_text = str(self._extract_transcribed_text(transcribe_payload) or "").strip()
            if len(raw_text) >= 30 and duration_sec >= 8.0:
                logger.warning(
                    "Retry transcribe с soft cleanup: raw_text len=%d, duration=%.1fs",
                    len(raw_text), duration_sec,
                )
                text = TextUtils.normalize_phrase(raw_text).strip()
                text = re.sub(r"\s+([,.;:!?])", r"\1", text)
                text = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    first_alpha = next((i for i, c in enumerate(text) if c.isalpha()), -1)
                    if first_alpha >= 0:
                        text = text[:first_alpha] + text[first_alpha].upper() + text[first_alpha + 1:]
                    if not re.search(r"[.!?…]$", text):
                        text = f"{text}."

        if not text:
            if transcription_error:
                event_bus.emit_typed(EventType.STT_FAILED, SttFailed(reason=transcription_error, duration_sec=duration_sec))
            return {
                "early_return": {
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
        # wave-1770 HIGH: gate translation events behind privacy_mode.
        # TRANSLATION_COMPLETED/FAILED carry source_text + translated_text (full transcript PII)
        # and were emitted to the SSE event bus unconditionally. sr carries the per-recording
        # privacy flag (same source as the live history-write gate).
        if not sr.get("privacy_mode_enabled", False):
            if translation.ok and translated_text:
                event_bus.emit_typed(EventType.TRANSLATION_COMPLETED, TranslationCompleted(
                    history_id="",
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
        if tp.get("engine"):
            self._last_stt_engine_ref[0] = str(tp["engine"])
        confidence = tp.get("confidence", 0.0)
        add_breadcrumb(
            category="transcription",
            message="transcribe_complete",
            level="info",
            data={
                "confidence": round(float(confidence), 3),
                "word_count": len(text.split()) if text else 0,
            },
        )
        if confidence < 0.4 and text:
            logger.warning("Низкая уверенность STT: %.2f — возможна ошибка распознавания", confidence)
        diarization_data = tp.get("diarization")
        display_text = self._format_text_with_speakers(final_text, diarization_data)

        return {
            "text": text,
            "display_text": display_text,
            "translated_text": translated_text,
            "final_text": final_text,
            "translation": translation,
            "translation_status": translation_status,
            "confidence": confidence,
            "diarization_data": diarization_data,
            "tp": tp,
        }

    def _stop_recording_phase_e(
        self,
        phase_d: dict[str, Any],
        sr: dict[str, Any],
        duration_sec: float,
        stop_tail_trim_ms: int,
        silence_detected: bool,
        silence_guard_enabled: bool,
        background_guard_rejected: bool,
        rt_session_id: str | None,
        settings: dict[str, Any],
        bookmark_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist history item, update side-caches, build final result dict."""
        text = phase_d["text"]
        display_text = phase_d["display_text"]
        translated_text = phase_d["translated_text"]
        final_text = phase_d["final_text"]
        translation = phase_d["translation"]
        translation_status = phase_d["translation_status"]
        confidence = phase_d["confidence"]
        diarization_data = phase_d["diarization_data"]
        tp = phase_d["tp"]

        # W1247 / W1572: AutoDeduplicator guard — check BEFORE persisting to history.
        # Respects auto_dedup_enabled and privacy_mode_enabled settings.
        # privacy_mode is already delegated to AutoDeduplicator._privacy_mode_enabled(),
        # but we also honour the settings flag here to avoid calling check_duplicate at all
        # when the feature is disabled (cheaper + matches test expectations).
        #
        # W1588 / W1592: _persist_lock serialises the dedup-check + add_history_item pair
        # so that two concurrent stop_recording IPC calls cannot both pass the dedup guard
        # before either write lands.  Lock is held only for the check+add critical section;
        # all heavy IO (STT, LLM, translation) runs outside.
        _dedup_enabled = bool(settings.get("auto_dedup_enabled", False))
        _privacy_mode = bool(settings.get("privacy_mode_enabled", False))
        # W1711: read runtime auto_dedup_threshold so the user-configured value is
        # forwarded to check_duplicate.  Previously the arg was omitted, leaving the
        # auto_dedup_threshold setting completely unwired (data-loss bug: sim>=0.85
        # always dropped even when threshold was 0.99).
        # wave-25 MED: sanitize — a negative/NaN/out-of-range value would make every
        # recording look like a duplicate (sim >= -1.0 is always True → data-loss).
        _dedup_threshold = _sanitize_dedup_threshold(settings.get("auto_dedup_threshold", 0.9))
        if _privacy_mode:
            logger.info("privacy_mode: recording persisted with privacy_mode=True",
                        extra={"duration_sec": round(float(duration_sec), 2)})
        with self._persist_lock:
            if self._auto_deduplicator is not None and _dedup_enabled and not _privacy_mode:
                try:
                    import time as _time_mod
                    _ts_now = _time_mod.strftime("%Y-%m-%dT%H:%M:%S")
                    _dedup_result = self._auto_deduplicator.check_duplicate(
                        text=display_text or text,
                        timestamp=_ts_now,
                        store=self.store,
                        threshold=_dedup_threshold,
                    )
                    if _dedup_result.is_duplicate:
                        logger.info(
                            "AutoDedup: запись пропущена как дубликат original_id=%s similarity=%.3f threshold=%.3f",
                            _dedup_result.duplicate_of,
                            _dedup_result.similarity,
                            _dedup_threshold,
                        )
                        return {
                            "status": "ok",
                            "skipped": "duplicate",
                            "duplicate_of": _dedup_result.duplicate_of,
                            "similarity": _dedup_result.similarity,
                            "duration_sec": duration_sec,
                            "quality_profile": sr["quality_profile"],
                            "cleanup_profile": sr["cleanup_profile"],
                            "translation_mode": translation.mode,
                            "translation_style": sr.get("translation_style", "neutral"),
                            "translate_and_paste": sr["translate_and_paste"],
                            "translation_status": translation_status,
                            "text": display_text,
                            "original_text": text,
                            "translated_text": translated_text,
                            "history_id": None,
                            "ts": None,
                            "stop_tail_trim_ms": stop_tail_trim_ms,
                            "silence_detected": silence_detected,
                            "silence_guard_enabled": silence_guard_enabled,
                            "background_guard_rejected": background_guard_rejected,
                        }
                except Exception:
                    logger.exception("AutoDedup: check_duplicate завершился с исключением, продолжаем запись")

            try:
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
                    audio_duration_sec=duration_sec if duration_sec else None,
                    confidence=confidence if confidence else None,
                    emotion=tp.get("emotion") if isinstance(tp.get("emotion"), str) else None,
                    word_timestamps=tp.get("word_timestamps") if isinstance(tp.get("word_timestamps"), list) else None,
                    speaker_turns=tp.get("speaker_turns") if isinstance(tp.get("speaker_turns"), list) else None,
                    privacy_mode=_privacy_mode,
                )
            except OSError as _disk_exc:
                import errno as _errno
                _is_enospc = getattr(_disk_exc, "errno", None) == _errno.ENOSPC
                _reason = "disk_full" if _is_enospc else "io_error"
                logger.error("Phase E: disk error %s: %s", _reason, _disk_exc)
                try:
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone
                    _e = ERROR_REGISTRY.get("history.write_fail", {})
                    event_bus.emit("krab_error", {
                        "severity": _e.get("severity", "critical"), "component": "history",
                        "code": "history.write_fail",
                        "message_user": _e.get("user_msg_ru", "Не удалось сохранить"),
                        "message_debug": f"OSError errno={getattr(_disk_exc, 'errno', None)}: {_disk_exc}",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "context": {"reason": _reason},
                    })
                except Exception:
                    pass
                return {"ok": False, "status": "persist_failed", "reason": _reason,
                        "transcript_text": text, "duration_sec": duration_sec,
                        "quality_profile": sr["quality_profile"],
                        "cleanup_profile": sr["cleanup_profile"],
                        "translation_status": translation_status, "history_id": None,
                        "stop_tail_trim_ms": stop_tail_trim_ms}
            # W1292: Invalidate AutoGlossary cache so new proper-noun terms are available
            # in the next STT initial-prompt without waiting for the TTL (restored W1659).
            if self._auto_glossary is not None:
                try:
                    self._auto_glossary.invalidate()
                except Exception as _ag_exc:
                    logger.warning("auto_glossary invalidate error after recording persist: %s", _ag_exc)

            # W1776: rebind live-recording bookmarks from the temp session_tracker UUID
            # (used during recording) to the final HistoryItem id.  update_session_id is
            # a no-op when bookmark_session_id is None/empty or no matching bookmarks exist.
            if self._bookmarks is not None and bookmark_session_id:
                try:
                    _rebind_count = self._bookmarks.update_session_id(
                        bookmark_session_id, item.id
                    )
                    if _rebind_count:
                        logger.info(
                            "W1776: %d bookmark(s) rebound %s → %s",
                            _rebind_count, bookmark_session_id, item.id,
                        )
                except Exception as _bm_exc:
                    logger.warning("W1776: bookmark rebind error: %s", _bm_exc)

        self._clipboard_history.append({
            "text": final_text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "history_id": item.id,
        })
        if len(self._clipboard_history) > 20:
            del self._clipboard_history[:-20]

        try:
            self._context_memory.update(text)
        except Exception:
            pass

        # wave-31 MED: skip semantic auto-index when privacy_mode is on — embeddings
        # encode transcript content and persist it across sessions, violating the
        # privacy guarantee that privacy-mode recordings leave no searchable traces.
        # _privacy_mode is already resolved above (line ~1366) in this same function.
        if self._semantic_searcher.is_enabled and _cfg_settings.SEMANTIC_SEARCH_AUTO_INDEX \
                and not _privacy_mode:
            _index_text = display_text or text
            _index_id = item.id
            threading.Thread(
                target=self._semantic_searcher.index_item,
                args=(_index_id, _index_text),
                daemon=True,
                name="semantic-index",
            ).start()

        self._transcription_counter_ref[0] += 1
        if self._transcription_counter_ref[0] % 100 == 0:
            try:
                self._auto_backup.check_and_backup()
            except Exception:
                pass

        result_payload = {
            "status": "ok",
            "duration_sec": duration_sec,
            "quality_profile": sr["quality_profile"],
            "cleanup_profile": sr["cleanup_profile"],
            "translation_mode": translation.mode,
            "translation_style": sr["translation_style"],
            "translate_and_paste": sr["translate_and_paste"],
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
            "privacy_mode": _privacy_mode,
        }
        # W1673 F2: privacy gate — do not leak transcript text via SSE in privacy mode.
        if not _privacy_mode:
            event_bus.emit_typed(EventType.STT_FINAL, SttFinal(
                history_id=item.id,
                text=final_text,
                duration_sec=duration_sec,
                language=tp.get("language"),
                confidence=tp.get("confidence"),
            ))
            if rt_session_id:
                try:
                    event_bus.emit(
                        "realtime.final_transcript",
                        {
                            "session_id": rt_session_id,
                            "text": final_text,
                            "is_partial": False,
                            "ts": time.time(),
                        },
                    )
                except Exception:
                    logger.debug("Не удалось emit realtime.final_transcript", exc_info=True)

        # wave-36 MED + crypto-audit (2026-06-20): не пишем plaintext .md когда активна
        # защита данных (privacy_mode ИЛИ history_encryption_enabled); live-путь также
        # требует auto_save_transcripts. Логика в _should_write_plaintext_md (тестируемо).
        if self._should_write_plaintext_md(settings, _privacy_mode, require_auto_save=True):
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

        # privacy-gate (recording_core_service #1911): action_items_auto_extract sends
        # display_text (full transcript) to an LLM and persists the result — must be
        # skipped in privacy_mode, same as the STT_FINAL emit above (line ~1602) and the
        # semantic auto-index guard above (line ~1561). _privacy_mode is already resolved
        # above (line ~1418) in this same function.
        if not _privacy_mode and self._coerce_bool(settings.get("action_items_auto_extract", False), default=False):
            min_dur = float(settings.get("action_items_min_duration_sec", 60.0))
            if self._action_items_extractor is not None and (duration_sec or 0.0) >= min_dur:
                try:
                    lang = str(tp.get("language", "ru") or "ru").lower()[:2]
                    ai_result = self._action_items_extractor.extract(display_text, language=lang)
                    if ai_result.ok:
                        self.store.update_history_item_action_items(
                            item_id=item.id,
                            action_items=[ai.to_dict() for ai in ai_result.action_items],
                            decisions=ai_result.decisions,
                            questions=ai_result.questions,
                        )
                        result_payload["action_items_extracted"] = True
                        result_payload["action_items_count"] = len(ai_result.action_items)
                except Exception:
                    logger.exception("Авто-извлечение action items провалилось для %s", item.id)

        # W930 CRITICAL fix: wire SessionTracker end — skip in privacy mode
        _privacy_mode = bool(settings.get("privacy_mode_enabled", False))
        if not _privacy_mode:
            try:
                self._session_tracker.end_session({
                    "duration_sec": duration_sec,
                    "stt_latency_ms": int(tp.get("stt_latency_ms", 0) or 0),
                    "confidence": float(tp.get("confidence", 0.0) or 0.0),
                    "text": display_text,
                    "had_diarization": bool(diarization_data and isinstance(diarization_data, dict) and diarization_data.get("enabled")),
                    "had_llm_rewrite": bool(tp.get("llm_applied", False)),
                    "translation_status": translation_status,
                    "paste_status": "pending",
                    "stt_model": str(tp.get("engine", "") or ""),
                    "quality_preset": sr.get("quality_profile", "balanced"),
                })
            except Exception:
                logger.warning("SessionTracker.end_session завершился с ошибкой (не критично)", exc_info=True)

        return result_payload

    # ------------------------------------------------------------------ #
    # _transcribe_paths_core (shared by sync + async paths)               #
    # ------------------------------------------------------------------ #

    def _transcribe_paths_core(
        self,
        params: dict[str, Any],
        *,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_file_start: Callable[[int, str], None] | None = None,
        on_file_done: Callable[[int, dict[str, Any] | None, str | None], None] | None = None,
    ) -> dict[str, Any]:
        """Общее ядро синхронной и асинхронной транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        settings = self._settings_svc.cached_settings()
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
            if self._is_path_allowed(resolved, allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected, allowed_roots=allowed_roots)
        if not audio_paths:
            return {"items": [], "processed": 0, "errors": ["Не найдено аудиофайлов для транскрибации"]}

        user_vocabulary = self.vocabulary.load() or []

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        skipped_duplicates: int = 0
        for file_index, audio_path in enumerate(audio_paths):
            if cancel_check is not None and cancel_check():
                break
            self._safe_callback(on_file_start, file_index, audio_path)
            started_at = time.monotonic()
            try:
                audio_duration_sec: float | None = None
                try:
                    import soundfile as sf
                    sf_info = sf.info(audio_path)
                    audio_duration_sec = round(sf_info.duration, 3)
                except Exception:
                    pass

                import_lang_hint = lang_hint if lang_hint else "auto"
                # MED-3: batch-импорт реально гоняет транскрайбер (+ rewriter
                # внутри engine.transcribe) — раньше эта активность вообще не
                # отражалась в last_stt_activity_ts, и длинный импорт мог
                # схватить выгрузку rewriter'а посреди себя.
                bump_stt_activity()
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
                    err_line = f"{audio_path}: {err}" if err else f"{audio_path}: пустой результат"
                    errors.append(err_line)
                    self._safe_callback(on_file_done, file_index, None, err_line)
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
                display_text = self._format_text_with_speakers(final_text, diarization_data)

                # W1602 / W1588 F2: mirror the W1572 dedup guard for the batch-import path.
                # Previously add_history_item was called unconditionally here, silently creating
                # duplicates when users re-imported the same file with auto_dedup_enabled=True.
                # _persist_lock serialises the check + add pair (atomicity, same as W1592).
                _dedup_enabled = bool(settings.get("auto_dedup_enabled", False))
                _privacy_mode = bool(settings.get("privacy_mode_enabled", False))
                # W1711: read runtime threshold — same fix as stop_recording path.
                # wave-25 MED: sanitize (negative/NaN/out-of-range → safe default 0.9).
                _dedup_threshold = _sanitize_dedup_threshold(settings.get("auto_dedup_threshold", 0.9))
                with self._persist_lock:
                    if self._auto_deduplicator is not None and _dedup_enabled and not _privacy_mode:
                        try:
                            import time as _time_mod
                            _ts_now = _time_mod.strftime("%Y-%m-%dT%H:%M:%S")
                            _dedup_result = self._auto_deduplicator.check_duplicate(
                                text=display_text or text,
                                timestamp=_ts_now,
                                store=self.store,
                                threshold=_dedup_threshold,
                            )
                            if _dedup_result.is_duplicate:
                                logger.info(
                                    "transcribe_paths: запись пропущена как дубликат"
                                    " original_id=%s similarity=%.3f threshold=%.3f path=%s",
                                    _dedup_result.duplicate_of,
                                    _dedup_result.similarity,
                                    _dedup_threshold,
                                    audio_path,
                                )
                                self._safe_callback(on_file_done, file_index, None, "duplicate")
                                skipped_duplicates += 1
                                continue
                        except Exception:
                            logger.exception(
                                "transcribe_paths: dedup check завершился с исключением,"
                                " продолжаем запись path=%s",
                                audio_path,
                            )

                    history_item = self.store.add_history_item(
                        text=display_text,
                        paste_status="failed",
                        # wave-1770: tag privacy_mode correctly for batch import — was
                        # always False, breaking privacy-tagged filtering/purge logic.
                        privacy_mode=_privacy_mode,
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
                        word_timestamps=(
                            transcribe_payload.get("word_timestamps")
                            if isinstance(transcribe_payload, dict)
                            and isinstance(transcribe_payload.get("word_timestamps"), list)
                            else None
                        ),
                        speaker_turns=(
                            transcribe_payload.get("speaker_turns")
                            if isinstance(transcribe_payload, dict)
                            and isinstance(transcribe_payload.get("speaker_turns"), list)
                            else None
                        ),
                    )

                summary: str | None = None
                if len(final_text) > 500:
                    summary = self._generate_summary(final_text)

                # wave-1770 HIGH + crypto-audit (2026-06-20): gate .md write behind data
                # protection (privacy_mode ИЛИ history_encryption_enabled), consistent with
                # the live path. require_auto_save=False — import пишет .md без этого флага.
                if self._should_write_plaintext_md(settings, _privacy_mode, require_auto_save=False):
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
                self._safe_callback(on_file_done, file_index, item_result, None)
            except Exception as exc:
                err_msg = str(exc)
                file_name = Path(audio_path).name
                if "Resource deadlock" in err_msg or "errno 11" in err_msg or "[Errno 11]" in err_msg or "[Errno 35]" in err_msg:
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
                self._safe_callback(on_file_done, file_index, None, err_msg)

        return {
            "items": items,
            "processed": len(items),
            "errors": errors,
            "skipped_duplicates": skipped_duplicates,
        }

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _generate_summary(self, text: str) -> str | None:
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

    def _stop_recorder_guarded(self, stop_tail_trim_ms: int) -> tuple[Any, float] | None:
        stop_callable = getattr(self.recorder, "stop", None)
        if not callable(stop_callable):
            raise RuntimeError("Рекордер не поддерживает stop()")
        try:
            return stop_callable(trim_tail_ms=stop_tail_trim_ms)
        except TypeError:
            return stop_callable()

    @staticmethod
    def _safe_callback(fn: Callable | None, *args: Any) -> None:
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                logger.exception("Callback %s упал с аргументами %s", fn, args[:1])

    @staticmethod
    def _is_path_allowed(resolved: Path, allowed_roots: list[Path]) -> bool:
        """Проверка принадлежности пути к разрешённым корням (без уязвимости sibling-prefix).

        Использует ``Path.is_relative_to`` (Python 3.9+) вместо ``startswith``,
        что предотвращает обход вида '/private/tmpEVIL/x.wav' → '/private/tmp'.
        """
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in allowed_roots
        )

    @staticmethod
    def _collect_audio_paths(
        paths: list[str],
        allowed_roots: list[Path] | None = None,
    ) -> list[str]:
        """Собирает аудиофайлы из списка путей/директорий.

        Args:
            paths: список абсолютных путей (файлы или директории).
            allowed_roots: если передан — каждый файл после ``resolve()`` повторно
                проверяется через ``_is_path_allowed``.  Это закрывает вектор
                post-validation symlink escape: symlink внутри разрешённой директории
                может вести за её пределы, и rglob вернёт реальный путь снаружи.
        """
        audio_ext = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".m4b", ".aif", ".aiff"}
        result: list[str] = []
        for raw in paths:
            path = Path(raw).expanduser()
            if not path.exists():
                continue
            if path.is_file():
                if path.suffix.lower() in audio_ext:
                    resolved_file = path.resolve()
                    if allowed_roots is not None and not RecordingCoreService._is_path_allowed(
                        resolved_file, allowed_roots
                    ):
                        logger.warning(
                            "transcribe_paths: файл за пределами разрешённых корней (симлинк?) — пропущен: %s",
                            resolved_file,
                        )
                        continue
                    result.append(str(resolved_file))
                continue
            if path.is_dir():
                candidates = sorted(
                    (c for c in path.rglob("*") if c.is_file() and c.suffix.lower() in audio_ext),
                    key=lambda c: str(c),
                )
                for c in candidates:
                    resolved_c = c.resolve()
                    if allowed_roots is not None and not RecordingCoreService._is_path_allowed(
                        resolved_c, allowed_roots
                    ):
                        logger.warning(
                            "transcribe_paths: файл за пределами разрешённых корней (симлинк?) — пропущен: %s",
                            resolved_c,
                        )
                        continue
                    result.append(str(resolved_c))
        unique: list[str] = []
        seen: set[str] = set()
        for item in result:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    @staticmethod
    def _list_audio_inputs_static() -> list[dict[str, Any]]:
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            logger.warning("Failed to list audio inputs: %s", exc)
            return []
        # 2026-08-05: sd.query_devices() ходит в CoreAudio без лока — при
        # активной записи (открытый sd.InputStream в AudioRecorder._worker)
        # теоретически возможна транзиентная гонка/hiccup на HAL. Один
        # bounded retry не меняет поведение happy-path (0 стоимости при
        # успехе первой попытки), но не даёт единичному transient сбою
        # молча превратиться в пустой список устройств для GUI.
        try:
            devices = sd.query_devices()
        except Exception as first_exc:
            logger.warning(
                "sd.query_devices() упал (%s: %s) — повторяю через 100мс",
                type(first_exc).__name__,
                first_exc,
            )
            time.sleep(0.1)
            try:
                devices = sd.query_devices()
            except Exception:
                logger.exception(
                    "Не удалось получить список аудиоустройств (retry тоже упал)"
                )
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
            results.append({
                "id": index,
                "name": str(device.get("name", f"Device {index}")),
                "channels": max_input_channels,
                "sample_rate": int(device.get("default_samplerate", 44100)),
                "hostapi": hostapi_name,
                "is_default": (index == default_input_idx),
            })
        return results

    @staticmethod
    def _format_text_with_speakers(text: str, diarization: dict | None) -> str:
        if not diarization or not isinstance(diarization, dict):
            return text
        if not diarization.get("enabled"):
            return text
        turns = diarization.get("speaker_turns", [])
        if not turns or len(turns) < 2:
            return text
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
    def _looks_like_silence_audio(
        audio: Any,
        sample_rate: int,
        rms_threshold: float,
        peak_threshold: float,
        active_ratio_threshold: float,
    ) -> bool:
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
        frame_size = max(1, int(sample_rate * 0.02))
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
        frame_size = max(1, int(sample_rate * 0.02))
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return low_level
        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        mean_rms = float(np.mean(frame_rms))
        std_rms = float(np.std(frame_rms))
        variation_coeff = std_rms / max(mean_rms, 1e-8)
        duration_sec = float(data.size) / max(float(sample_rate), 1.0)
        dynamic_uniform_threshold = max(0.0012, min(uniform_frame_threshold, max(min_rms * 0.35, 0.0012)))
        active_ratio = float(np.mean(frame_rms >= dynamic_uniform_threshold))
        background_pattern = active_ratio >= max_uniform_active_ratio and variation_coeff < 0.35
        very_uniform = active_ratio >= 0.96 and variation_coeff < 0.18
        return background_pattern and (low_level or (very_uniform and duration_sec >= 4.0))

    @staticmethod
    def _is_known_prompt_echo(normalized_text: str) -> bool:
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
        return RecordingCoreService._contains_repeated_chunk(words)

    @staticmethod
    def _postprocess_transcribed_text(text: str) -> str:
        _logger = logging.getLogger("KrabEar.Backend.Service")
        clean = str(text or "").strip()
        if not clean:
            return ""
        lowered = clean.lower()
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            _logger.warning(
                "postprocess: drop reason=tech_artifact, len=%d, sample=%r",
                len(clean), clean[:80],
            )
            return ""
        normalized = TextUtils.normalize_phrase(clean)
        if RecordingCoreService._is_known_prompt_echo(normalized):
            _logger.warning(
                "postprocess: drop reason=known_prompt_echo, len=%d, sample=%r",
                len(clean), clean[:80],
            )
            return ""
        collapsed_duplicate = RecordingCoreService._collapse_immediate_duplicate_phrase(normalized)
        if collapsed_duplicate:
            clean = collapsed_duplicate
            normalized = TextUtils.normalize_phrase(clean)
        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if RecordingCoreService._looks_like_looping_artifact(words, min_words=8, min_bigram_hits=4):
            _logger.warning(
                "postprocess: drop reason=looping_artifact, len=%d, sample=%r",
                len(clean), clean[:80],
            )
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
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return ""
        words = normalized.split()
        total = len(words)
        if total < 8:
            return ""
        if total % 2 == 0:
            half = total // 2
            if words[:half] == words[half:]:
                collapsed = " ".join(words[:half]).strip()
                if not collapsed:
                    return ""
                return f"{collapsed[0].upper()}{collapsed[1:]}."
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
        clean = str(text or "").strip()
        if not clean:
            return ""
        lowered = clean.lower()
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            return ""
        normalized = TextUtils.normalize_phrase(clean)
        if RecordingCoreService._is_known_prompt_echo(normalized):
            return ""
        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if RecordingCoreService._looks_like_looping_artifact(words, min_words=6, min_bigram_hits=3):
            return ""
        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    @staticmethod
    def _extract_transcribed_text(payload: Any) -> str:
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
        if isinstance(payload, dict):
            error = payload.get("error")
            if error is not None:
                return str(error).strip()
        return ""

    # ------------------------------------------------------------------ #
    # Utility coercers (copied from BackendService for self-contained use) #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        try:
            return bool(value)
        except Exception:
            return default

    @staticmethod
    def _should_write_plaintext_md(
        settings: dict[str, Any], privacy_mode: bool, *, require_auto_save: bool
    ) -> bool:
        """True, если plaintext .md-транскрипт допустимо записать на диск.

        Транскрипт НЕ должен лежать открытым, если активна любая защита данных:
        - privacy_mode  — ничего не персистится;
        - history_encryption_enabled — шифрование-at-rest; plaintext .md сайдкар
          подрывает гарантию «транскрипт не читаем на диске» (crypto-audit 2026-06-20).
        Live-путь дополнительно требует auto_save_transcripts; import-путь исторически
        пишет .md без этого флага (require_auto_save=False).
        """
        coerce = RecordingCoreService._coerce_bool
        if privacy_mode:
            return False
        if coerce(settings.get("history_encryption_enabled", False), default=False):
            return False
        if require_auto_save and not coerce(settings.get("auto_save_transcripts", False), default=False):
            return False
        return True

    @staticmethod
    def _coerce_bounded(
        value: Any,
        default: int | float,
        min_value: int | float,
        max_value: int | float,
    ) -> int | float:
        try:
            v = float(value)
            if not (min_value <= v <= max_value):
                return default
            return v
        except Exception:
            return default
