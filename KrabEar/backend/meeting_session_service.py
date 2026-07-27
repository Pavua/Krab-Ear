"""MeetingSessionService — backend-ядро живой панели встречи (C2a).

Спека: docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md §2.

Пассивен вне встречи. Внутри — один воркер-тред («GPU-слот»): на Metal не
больше одной тяжёлой операции meeting-механики одновременно. Типы задач —
enum MeetingJob; DIAR_WINDOW объявлен сразу (C2b добавит только исполнитель).
Приоритет при одновременной готовности: CHUNK_STT > ITEMS_LLM > DIAR_WINDOW.

Privacy: все хендлеры гейтятся privacy_mode_enabled; включение privacy
посреди встречи глушит live-обработку (воркер выходит, события прекращаются).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:  # ubuntu-CI без libsndfile: деградация, не падение импорта модуля
    import soundfile as _sf  # type: ignore
except Exception:  # pragma: no cover
    _sf = None

logger = logging.getLogger("krab_ear.backend")

_TAIL_CHARS = 600          # transcript_tail в get_meeting_live_state
_ITEMS_MIN_GROWTH = 200    # симв.: минимальный прирост текста для нового LLM-вызова
_LEASE_RENEW_SEC = 15.0    # период продления brain-lease
_LEASE_TTL_SEC = 45.0      # TTL lease (перекрывает период продления с запасом)
_WORKER_WAIT_SEC = 0.5     # шаг ожидания воркера
_WORKER_JOIN_TIMEOUT_SEC = 30.0
_SETUP_CLOSE_WAIT_SEC = 0.25  # bounded wait setup перед сохранением retry-handle
_DIAR_MIN_AUDIO_SEC = 5.0  # окно короче — эмбеддинги шумные, тик пропускаем
_RECOVERY_ABORT_OWNER = "abort_owner"
_RECOVERY_ROLLBACK_OWNER = "rollback_owner"


class LiveSpeakerTracker:
    """Сессионный реестр спикеров C2b (спека §2.5 + §2.5a).

    Локальные метки pyannote внутри окна анонимны и нестабильны между
    прогонами — идентичность спикеров держится ТОЛЬКО на эмбеддингах:
    cosine центроида окна против скользящего среднего центроида спикера.
    Реестр живёт в памяти сессии, на диск не пишется.

    Потокобезопасность НЕ нужна: все вызовы — из одного GPU-слот-треда;
    снапшот для IPC копируется в состояние сессии под её локом.
    """

    def __init__(self, threshold: float, max_speakers: int = 16) -> None:
        self._threshold = float(threshold)
        # Верхняя граница реестра: шумная многочасовая встреча (эхо, наложения,
        # короткие реплики) иначе плодит фантомных «Спикеров N» без предела —
        # растёт O(n)-матчинг каждого тика и payload state/событий.
        self._max_speakers = int(max_speakers)
        # список спикеров: label, centroid (unit-norm np.ndarray), n_windows,
        # talk_sec, last_active_ts
        self._speakers: list[dict[str, Any]] = []

    @staticmethod
    def _unit(vec: Any) -> "np.ndarray | None":
        arr = np.asarray(vec, dtype=np.float32).flatten()
        norm = float(np.linalg.norm(arr))
        if not np.isfinite(norm) or norm < 1e-8:
            return None
        return arr / norm

    def ingest(self, segments: list[dict[str, Any]],
               embeddings: dict[str, Any], now_ts: float) -> None:
        """Одно окно диаризации: сегменты + центроиды локальных меток."""
        talk_by_label: dict[str, float] = {}
        for seg in segments:
            dur = max(0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
            talk_by_label[str(seg.get("speaker"))] = (
                talk_by_label.get(str(seg.get("speaker")), 0.0) + dur)

        for label, raw in embeddings.items():
            emb = self._unit(raw)
            if emb is None:
                continue
            talk = talk_by_label.get(str(label), 0.0)
            best, best_cos = None, -1.0
            for sp in self._speakers:
                cos = float(np.dot(sp["centroid"], emb))
                if cos > best_cos:
                    best, best_cos = sp, cos
            if best is not None and best_cos >= self._threshold:
                n = best["n_windows"]
                merged = self._unit(best["centroid"] * n + emb)
                if merged is not None:
                    best["centroid"] = merged
                best["n_windows"] = n + 1
                best["talk_sec"] += talk
                best["last_active_ts"] = now_ts
            else:
                if len(self._speakers) >= self._max_speakers:
                    continue  # реестр полон — не-сматчившееся окно не создаёт нового
                self._speakers.append({
                    "label": f"Спикер {len(self._speakers) + 1}",
                    "centroid": emb,
                    "n_windows": 1,
                    "talk_sec": talk,
                    "last_active_ts": now_ts,
                })

    def snapshot(self) -> list[dict[str, Any]]:
        """Снимок для get_meeting_live_state / события (без numpy-объектов)."""
        return [{
            "label": sp["label"],
            "talk_sec": round(float(sp["talk_sec"]), 1),
            "last_active_ts": sp["last_active_ts"],
        } for sp in self._speakers]


class MeetingJob(str, Enum):
    CHUNK_STT = "chunk_stt"
    ITEMS_LLM = "items_llm"
    DIAR_WINDOW = "diar_window"  # C2b: объявлен сейчас, исполнителя нет


@dataclass
class _MeetingSession:
    started_at: float = field(default_factory=time.time)
    promoted: bool = False
    language: str = "ru"
    # Токен выдаёт только RecordingCore при старте G1. Meeting-слой хранит и
    # возвращает его как opaque-значение: сам UUID здесь никогда не генерируется.
    generation_token: str | None = None
    # CAS-ревизия владельца относится к тому же G1, что и token. При promote
    # token остаётся прежним, а revision растёт; поэтому stop обязан предъявить
    # оба значения, иначе запоздалый meeting-stop способен затронуть уже чужой
    # режим записи.
    owner_revision: int | None = None
    # Непрозрачный идентификатор клиентского запроса из исходного Core-start.
    # При promote это именно идентификатор первоначальной диктовки, а не
    # позднего meeting-клика: Core хранит его неизменным для доказательства
    # происхождения G1.
    start_request_id: str | None = None
    # После recorder_timeout/stop_in_progress сессия остаётся retry-handle.
    # Отдельный флаг не даёт фоновому воркеру ошибочно self-finalize её по
    # временному ``recorder.is_recording == False``.
    stop_retry_pending: bool = False
    # ``meeting.finalizing`` описывает начало одной логической финализации, а
    # не каждую попытку physical stop того же поколения.
    finalizing_emitted: bool = False
    cursor_sec: float = 0.0
    chunks: list[str] = field(default_factory=list)
    transcript_len: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    last_extract_len: int = 0
    degraded_llm: bool = False
    degraded_diarization: bool = False
    privacy_stopped: bool = False
    last_updated_ts: float = field(default_factory=time.time)
    speakers_enabled: bool = False
    tracker: Any = None                       # LiveSpeakerTracker | None
    speakers: list = field(default_factory=list)  # снапшот для IPC/событий

    def tail(self) -> str:
        return "".join(self.chunks)[-_TAIL_CHARS:]


class MeetingSessionService:
    """18-я сервис-экстракция: живая meeting-сессия поверх активной записи."""

    def __init__(
        self,
        recorder: Any,
        transcriber: Any,
        recording_core: Any,
        action_items_extractor: Any,
        settings_get: Callable[[str, Any], Any],
        event_bus: Any,
        diarize_window: Callable[[str], dict[str, Any]] | None = None,
        data_dir: Any = None,
    ) -> None:
        self._recorder = recorder
        self._transcriber = transcriber
        self._recording_core = recording_core
        self._extractor = action_items_extractor
        self._settings_get = settings_get
        self._bus = event_bus
        self._diarize_window = diarize_window
        self._data_dir = Path(data_dir) if data_dir is not None else None

        # RLock (не Lock): _items_interval() зовёт self._lock изнутри блока,
        # уже удерживаемого handle_meeting_start (dict-литерал _next_due
        # вычисляется под внешним `with self._lock`) — non-reentrant Lock
        # там дедлочился бы.
        self._lock = threading.RLock()          # состояние сессии
        # Целиком сериализует start-setup и stop. Одного _lock недостаточно:
        # start намеренно отпускает его на I/O, и stop→start раньше могли
        # подменить reservation до публикации worker первой сессии.
        self._transition_lock = threading.RLock()
        self._session: _MeetingSession | None = None
        self._next_due: dict[Any, float] = {}
        # Worker создаётся до физического recorder.start(), но не имеет права
        # читать idle-рекордер и self-finalize до успешной публикации встречи.
        # Отдельный Event нужен потому, что stop обязан разбудить и такой
        # «подготовленный, но ещё не вооружённый» поток.
        self._worker_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._worker_armed_event = threading.Event()
        # close() выставляет флаг без ожидания тяжёлого start/setup. Старт
        # повторно проверяет его на границах побочных эффектов и не публикует
        # успешную встречу после начала shutdown.
        self._closed_event = threading.Event()
        # Если fresh start уже захватил микрофон, а owner-bound abort не
        # подтвердился, session нельзя удалять: она остаётся retry-handle для
        # meeting_stop/повторного close.
        self._recovery_pending = False
        self._recovery_kind: str | None = None
        self._recovery_owner_revision: int | None = None
        self._setup_done_event = threading.Event()
        self._setup_done_event.set()
        # C2a Task 10 (Фикс 2): гейт идемпотентности handle_meeting_stop —
        # конкурентный/повторный вызов не должен звать handle_stop_recording
        # и эмиттить meeting.finished дважды.
        self._stopping = False
        # Снимок G1 берётся в линеаризационной точке остановки. Он остаётся
        # доступен конкурентному IPC даже после окончательного снятия, когда
        # self._session уже очищен, но первый stop ещё не вернул ответ.
        self._stopping_generation_token: str | None = None

    # ------------------------------------------------------------------ IPC

    def handle_meeting_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сериализовать полный setup встречи со stop и следующим start."""
        self._raise_if_closed()
        with self._transition_lock:
            self._raise_if_closed()
            return self._handle_meeting_start_serialized(params)

    def _handle_meeting_start_serialized(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """IPC meeting_start: старт записи+сессии ИЛИ повышение идущей записи."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "skipped": "privacy_mode"}

        with self._lock:
            if self._stopping:
                return {"ok": False, "error": "meeting_stopping"}
            if self._session is not None and not self._session.privacy_stopped:
                return {"ok": True, "already_active": True,
                        "started_at": self._session.started_at,
                        "promoted": self._session.promoted,
                        "generation_token": self._session.generation_token,
                        "owner_revision": self._session.owner_revision,
                        "start_request_id": self._session.start_request_id,
                        "stop_retry_pending": self._session.stop_retry_pending}
            # C2a Task 10 (аудит HIGH): предыдущий GPU-слот-воркер ещё жив
            # (застрявший MLX-вызов пережил 30с join в _stop_worker) —
            # второй воркер параллельно недопустим (см. докстринг модуля).
            # _stop_event старого НЕ очищаем: разблокировавшись, он выйдет сам.
            w = self._worker
            if w is not None and w.is_alive() and self._stop_event.is_set():
                return {"ok": False, "error": "gpu_slot_busy"}
            # C2a-фикс гонки (Task 5b): резервируем слот ДО unlocked I/O ниже —
            # иначе конкурентный handle_meeting_start() тоже проходит
            # None-проверку и стартует запись/воркер второй раз (двойной
            # клик в UI / два IPC-клиента). Резервация снимается в finally
            # при ошибке или заменяется реальной сессией при успехе.
            reservation = _MeetingSession()
            self._session = reservation
            self._setup_done_event.clear()

        session: _MeetingSession | None = None
        promoted = False
        recording_status: str | None = None
        owner_promoted = False
        owner_revision: int | None = None
        meeting_lease_acquired = False
        try:
            speakers_enabled = bool(self._settings_get(
                "meeting_live_speakers_enabled", True))
            session = _MeetingSession(
                language=str(params.get("language", self._settings_get(
                    "meeting_items_language", "ru")) or "ru"),
                speakers_enabled=speakers_enabled,
            )
            if speakers_enabled:
                session.tracker = LiveSpeakerTracker(threshold=float(
                    self._settings_get("meeting_speaker_match_threshold", 0.72)))
            now = time.monotonic()
            with self._lock:
                self._session = session
                self._next_due = {
                    MeetingJob.CHUNK_STT: now + self._chunk_interval(),
                    MeetingJob.ITEMS_LLM: now + self._items_interval(),
                }
                if speakers_enabled:
                    self._next_due[MeetingJob.DIAR_WINDOW] = now + self._diar_interval()

            # Worker — последний потенциально падающий setup-шаг ДО физического
            # recorder.start/promote. Так ошибка не оставляет скрытый захват
            # микрофона после сообщения UI «встреча не запущена».
            self._start_worker()
            self._raise_if_closed()
            # ``start_request_id`` создаёт клиент до IPC и должен дойти до
            # Core без генерации/нормализации в meeting-слое. Отсутствие ключа
            # сохраняет legacy-совместимость старого нативного агента.
            start_params: dict[str, Any] = {"source": "meeting"}
            if "start_request_id" in params:
                start_params["start_request_id"] = params.get(
                    "start_request_id"
                )
            start_resp = self._recording_core.handle_start_recording(
                start_params
            )
            status = str(start_resp.get("status", ""))
            if status not in {"recording", "already_recording"}:
                raise RuntimeError(
                    f"meeting: recorder start rejected with status={status}"
                )
            recording_status = status
            raw_generation_token = start_resp.get("generation_token")
            # R2-Core всегда выдаёт opaque token. Старый backend ещё может
            # вернуть его без поля; такой legacy-режим сохраняем только для
            # совместимости до одновременного рестарта agent/backend и никогда
            # не подменяем самодельным UUID.
            generation_token = (
                raw_generation_token
                if isinstance(raw_generation_token, str)
                and bool(raw_generation_token)
                else None
            )
            owner_promoted = bool(start_resp.get("owner_promoted", False))
            raw_owner_revision = start_resp.get("owner_revision")
            # Строгий stop принимает только положительный int. Некорректный
            # ответ старого/duck-typed Core не приводим к int молча: тогда
            # meeting остаётся в legacy-режиме вместо ложного CAS-lease.
            if (
                type(raw_owner_revision) is int
                and raw_owner_revision > 0
            ):
                owner_revision = raw_owner_revision
            raw_start_request_id = start_resp.get("start_request_id")
            start_request_id = (
                raw_start_request_id
                if isinstance(raw_start_request_id, str)
                and bool(raw_start_request_id)
                else None
            )
            self._raise_if_closed()
            promoted = status == "already_recording"
            cursor_sec = 0.0
            if promoted:
                try:
                    cursor_sec = float(self._recorder.get_duration_sec())
                except Exception:
                    logger.warning(
                        "meeting: не удалось снять cursor promote",
                        exc_info=True,
                    )
            with self._lock:
                session.promoted = promoted
                session.cursor_sec = cursor_sec
                session.generation_token = generation_token
                session.owner_revision = owner_revision
                session.start_request_id = start_request_id
            # RecordingCore на fresh start освобождает brain lease; встреча
            # должна приобрести его ПОСЛЕ успешного старта, а не до него.
            self._acquire_lease()
            meeting_lease_acquired = True
            self._arm_worker()
            # Линеаризационная точка успешного start: если close начался до
            # неё, свежую запись компенсируем и наружу успех не публикуем.
            self._raise_if_closed()
        except Exception as exc:
            # Worker мог успеть стартовать до ошибки RecordingCore — гасим его
            # до снятия session, чтобы не оставить GPU-slot без владельца.
            self._stop_worker()
            if meeting_lease_acquired:
                # close мог успеть release ДО того, как start приобрёл lease.
                # Поэтому rollback выполняется самим start-переходом.
                self._release_lease()

            compensation_failed = False
            retain_for_recovery = False
            recovery_kind: str | None = None
            recovery_owner_revision: int | None = None
            if recording_status == "recording":
                # Любое исключение после fresh recorder.start() требует
                # физической компенсации. Сейчас практически это close-race;
                # общий гард не даст будущему fallible setup вернуть orphan.
                abort_owned = getattr(
                    self._recording_core,
                    "abort_recording_if_owner",
                    None,
                )
                try:
                    compensation_failed = (
                        not callable(abort_owned)
                        or not abort_owned("meeting")
                    )
                except Exception:
                    compensation_failed = True
                    logger.exception(
                        "meeting: owner-bound компенсация fresh start упала"
                    )
                retain_for_recovery = compensation_failed
                if retain_for_recovery:
                    recovery_kind = _RECOVERY_ABORT_OWNER
            elif recording_status == "already_recording" and owner_promoted:
                rollback_owner = getattr(
                    self._recording_core,
                    "rollback_owner_transition",
                    None,
                )
                try:
                    if owner_revision is None or not callable(rollback_owner):
                        compensation_failed = True
                        retain_for_recovery = True
                        recovery_kind = _RECOVERY_ROLLBACK_OWNER
                        recovery_owner_revision = owner_revision
                    elif not rollback_owner(
                            expected_revision=owner_revision,
                            expected_owner="meeting",
                            restore_owner="dictation",
                    ):
                        # CAS mismatch/stopped = переход уже не наш. Это
                        # безопасный отказ менять owner, не recovery-handle.
                        logger.info(
                            "meeting: promote rollback уже не применим "
                            "(owner-переход замещён или запись остановлена)"
                        )
                except Exception:
                    compensation_failed = True
                    retain_for_recovery = True
                    recovery_kind = _RECOVERY_ROLLBACK_OWNER
                    recovery_owner_revision = owner_revision
                    logger.exception(
                        "meeting: CAS-rollback promote owner упал"
                    )
            with self._lock:
                if (
                    retain_for_recovery
                    and self._session is session
                ):
                    self._recovery_pending = True
                    self._recovery_kind = recovery_kind
                    self._recovery_owner_revision = recovery_owner_revision
                elif (
                    self._session is reservation
                    or self._session is session
                ):
                    self._session = None
                    self._next_due = {}
                    self._recovery_pending = False
                    self._recovery_kind = None
                    self._recovery_owner_revision = None
            if compensation_failed:
                raise RuntimeError(
                    "meeting: start завершился ошибкой, но owner-bound "
                    "компенсация не подтверждена"
                ) from exc
            raise
        finally:
            # close() ждёт этот барьер ограниченное время и никогда не чистит
            # reservation, пока именно start ещё решает rollback/recovery.
            self._setup_done_event.set()

        logger.info("meeting: сессия запущена", extra={
            "promoted": promoted, "language": session.language})
        return {
            "ok": True,
            "promoted": promoted,
            "started_at": session.started_at,
            "generation_token": session.generation_token,
            "owner_revision": session.owner_revision,
            "start_request_id": session.start_request_id,
        }

    def handle_meeting_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """Остановить встречу, не удерживая transition-lock на тяжёлом STT."""
        with self._lock:
            session = self._session
            token_is_supplied = "generation_token" in params
            supplied_token = params.get("generation_token")
            if (
                token_is_supplied
                and (
                    not isinstance(supplied_token, str)
                    or not supplied_token
                )
            ):
                # Пустой/нестроковый token — не legacy. Явно предъявленное
                # неверное значение не имеет права молча подменяться token'ом
                # живой сессии и трогать Core.
                return self._unknown_generation_response(supplied_token)
            if (
                token_is_supplied
                and session is not None
                and supplied_token != session.generation_token
            ):
                return self._unknown_generation_response(supplied_token)
            if self._stopping:
                # После окончательного снятия self._session уже None, но чужой
                # токен не вправе маскироваться под идущую остановку. Legacy-
                # клиент без токена получает серверный G1 из снимка первого stop.
                stopping_token = self._stopping_generation_token
                if (
                    token_is_supplied
                    and supplied_token != stopping_token
                ):
                    return self._unknown_generation_response(supplied_token)
                return {
                    "ok": True,
                    "status": "stop_in_progress",
                    "generation_token": stopping_token,
                    "active": session is not None,
                }
            # Флаг ставится ДО ожидания setup-lock: следующий start либо ждёт
            # текущий setup, либо быстро получает meeting_stopping.
            self._stopping = True
            self._stopping_generation_token = (
                session.generation_token
                if session is not None
                else supplied_token
                if token_is_supplied
                else None
            )
        try:
            return self._handle_meeting_stop_serialized(params)
        finally:
            with self._lock:
                self._stopping = False
                self._stopping_generation_token = None

    def _handle_meeting_stop_serialized(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """IPC meeting_stop: гасит live-сессию и останавливает запись обычным путём.

        C2a Task 10 (аудит MED): идемпотентен — дабл-клик в UI / два
        IPC-клиента, вызвавшие stop конкурентно, НЕ должны привести к
        двойному handle_stop_recording()/meeting.finished. Гейт _stopping
        под тем же self._lock, что и остальное состояние сессии; сбрасывается
        в finally, так что независимые ПОСЛЕДОВАТЕЛЬНЫЕ вызовы (новая сессия
        после предыдущего stop) не залипают.
        """
        token_is_supplied = "generation_token" in params
        supplied_token = params.get("generation_token")
        session: _MeetingSession | None = None
        generation_token: str | None = None
        # Событие finalizing нельзя публиковать до ответа строгого Core-stop:
        # owner_mismatch означает, что эта сессия уже не имеет права
        # финализировать чужой G1 даже только на уровне UI.
        finalizing_requested = False

        with self._transition_lock:
            with self._lock:
                promote_recovery = (
                    self._recovery_pending
                    and self._recovery_kind == _RECOVERY_ROLLBACK_OWNER
                )
            if promote_recovery:
                # Встреча не была опубликована, а физическая запись — исходная
                # диктовка. Ни обычный meeting_stop, ни privacy-ветка не имеют
                # права её гасить: повторяем только revision-bound CAS.
                if not self._stop_worker():
                    return {
                        "ok": False,
                        "error": "meeting_recovery_pending",
                    }
                self._release_lease()
                if not self._retry_pending_recovery():
                    return {
                        "ok": False,
                        "error": "meeting_recovery_pending",
                    }
                with self._lock:
                    self._session = None
                    self._next_due = {}
                    self._recovery_pending = False
                    self._recovery_kind = None
                    self._recovery_owner_revision = None
                return {
                    "ok": True,
                    "active": False,
                    "recovered": "owner_rollback",
                }

            if self._settings_get("privacy_mode_enabled", False):
                # Privacy, включённый посреди встречи, сохраняет прежний
                # fail-closed контракт: live-сессию закрываем без Core-
                # финализации, history и meeting.finished. Обычный privacy-
                # lifecycle записи отвечает за физическую остановку отдельно.
                with self._lock:
                    privacy_session = self._session
                    privacy_token = (
                        privacy_session.generation_token
                        if privacy_session is not None
                        else (
                            supplied_token
                            if isinstance(supplied_token, str)
                            else None
                        )
                    )
                teardown_finished = self._teardown_session(
                    emit_finished=False,
                    expected_session=privacy_session,
                )
                if not teardown_finished:
                    with self._lock:
                        # Ложный результат имеет значение только пока та же
                        # сессия всё ещё удерживает живой дескриптор воркера.
                        # Режим приватности скрывает данные, но G1 остаётся
                        # повторяемым для остановки.
                        retained = (
                            privacy_session is not None
                            and self._session is privacy_session
                        )
                        if retained:
                            privacy_session.stop_retry_pending = True
                            privacy_session.privacy_stopped = True
                    if retained:
                        return {
                            "ok": True,
                            "status": "stop_in_progress",
                            "active": True,
                            "privacy_mode_active": True,
                            "generation_token": privacy_token,
                        }
                return {
                    "ok": True,
                    "status": "privacy_mode",
                    "skipped": "privacy_mode",
                    "active": False,
                    "generation_token": privacy_token,
                }

            with self._lock:
                session = self._session
                if session is not None:
                    if token_is_supplied:
                        if (
                            not isinstance(supplied_token, str)
                            or not supplied_token
                            or supplied_token != session.generation_token
                        ):
                            return self._unknown_generation_response(
                                supplied_token
                            )
                        generation_token = supplied_token
                    else:
                        generation_token = session.generation_token

                    # Legacy session без token поддерживает только legacy stop.
                    # Новый R2-путь всегда имеет token и ниже предъявляет его
                    # даже если recorder уже успел сообщить False.
                    if token_is_supplied and generation_token is None:
                        return self._unknown_generation_response(
                            supplied_token
                        )
                    if not session.finalizing_emitted:
                        finalizing_requested = True
                elif token_is_supplied:
                    if not isinstance(supplied_token, str) or not supplied_token:
                        return self._unknown_generation_response(supplied_token)
                    # Потерянный IPC-ответ после terminal teardown: Core сам
                    # вернёт immutable replay либо unknown_generation.
                    generation_token = supplied_token
                else:
                    return {"ok": True, "active": False}

            if session is not None:
                worker_stopped = self._stop_worker()
                # При неостановившемся worker Core ещё не вызывается. Флаг
                # нужен сохранить прямо сейчас, чтобы повторные stop не
                # дублировали lifecycle-событие того же retry-handle.
                if (
                    not worker_stopped
                    and finalizing_requested
                    and self._claim_finalizing_event(session)
                ):
                    self._emit("meeting.finalizing", {
                        "generation_token": session.generation_token,
                    })
                if not worker_stopped:
                    # Сохранившийся живой воркер мог уже пройти ранние
                    # проверки перед самофинализацией. До подтверждённой смерти
                    # Core нельзя трогать: сохраняем G1 и маркер повторной
                    # попытки остановки.
                    with self._lock:
                        active = self._session is session
                        if active:
                            session.stop_retry_pending = True
                    return self._meeting_stop_response(
                        {"status": "stop_in_progress"},
                        generation_token,
                        active=active,
                    )

        # STT/LLM-фазы могут длиться минуты. transition-lock уже отпущен, но
        # _stopping остаётся True и не даёт новому start вклиниться в финализацию.
        stop_params: dict[str, Any] = {"source": "meeting"}
        if generation_token is not None:
            stop_params["generation_token"] = generation_token
        if (
            session is not None
            and generation_token is not None
            and session.owner_revision is not None
        ):
            # Строгая аренда намеренно берётся из серверного ответа
            # meeting_start,
            # а не из IPC meeting_stop: клиент не вправе понизить/подменить
            # revision и превратить защищённый stop обратно в legacy-shadow.
            stop_params["expected_owner_revision"] = session.owner_revision
        stop_resp = self._recording_core.handle_stop_recording(stop_params)
        status = str(stop_resp.get("status", "ok"))

        # Core подтвердил, что stop относится к предъявленному владельцу. Лишь
        # теперь это действительно начало финализации live-сессии. Для
        # типизированных owner_mismatch/unknown_generation событие сознательно
        # не публикуем.
        if (
            status not in {"owner_mismatch", "unknown_generation"}
            and finalizing_requested
            and self._claim_finalizing_event(session)
        ):
            self._emit("meeting.finalizing", {
                "generation_token": session.generation_token,
            })

        if status in {"recorder_timeout", "stop_in_progress"}:
            if session is not None:
                with self._lock:
                    # Сессия могла быть снята только аварийным внешним путём;
                    # identity-проверка не даёт протухшему RPC воскресить её.
                    if self._session is session:
                        session.stop_retry_pending = True
            return self._meeting_stop_response(
                stop_resp,
                generation_token,
                active=session is not None,
            )

        if status == "owner_mismatch":
            # CAS отказ не является terminal stop. Сохраняем session вместе с
            # её token+revision, чтобы поздний повтор не потерял strict lease
            # и не скатился к legacy stop чужой записи. Worker уже остановлен,
            # а stop_retry_pending блокирует его возможную self-finalization.
            active = False
            if session is not None:
                with self._lock:
                    if self._session is session:
                        session.stop_retry_pending = True
                        active = True
            return self._meeting_stop_response(
                stop_resp,
                generation_token,
                active=active,
            )

        # Unknown generation не имеет живого Core-объекта, за который можно
        # безопасно повторять CAS. Live-обработку снимаем без finished и без
        # повторного обращения к recorder.
        if status == "unknown_generation":
            if session is not None:
                self._teardown_session(
                    emit_finished=False,
                    expected_session=session,
                    stop_worker=False,
                )
            return self._meeting_stop_response(
                stop_resp,
                generation_token,
                active=False,
            )

        # Terminal response (включая Core terminal-cache replay): history уже
        # сохранена Core, поэтому только live session, существовавшая ДО этого
        # вызова, имеет право эмиттить meeting.finished.
        item_id = stop_resp.get("history_id")
        if session is not None:
            self._teardown_session(
                emit_finished=True,
                item_id=item_id,
                expected_session=session,
                stop_worker=False,
            )
        return self._meeting_stop_response(
            stop_resp,
            generation_token,
            active=False,
        )

    def _claim_finalizing_event(self, session: _MeetingSession | None) -> bool:
        """Однократно закрепить lifecycle-событие за ещё живой сессией."""
        if session is None:
            return False
        with self._lock:
            if self._session is not session or session.finalizing_emitted:
                return False
            session.finalizing_emitted = True
            return True

    @staticmethod
    def _unknown_generation_response(token: Any) -> dict[str, Any]:
        """Вернуть typed отказ без вызова RecordingCore."""
        return {
            "ok": True,
            "status": "unknown_generation",
            "generation_token": token,
        }

    @staticmethod
    def _meeting_stop_response(
        stop_resp: dict[str, Any],
        generation_token: str | None,
        *,
        active: bool,
    ) -> dict[str, Any]:
        """Адаптировать ответ Core к IPC-контракту meeting-панели.

        Верхнеуровневый ``ok`` остаётся True и для типизированных отказов
        Core: иначе IPCClient превратит полезный status в общий backendError,
        и Swift не сможет выбрать правильную ветку восстановления.
        """
        response = dict(stop_resp)
        response["ok"] = True
        response["status"] = str(stop_resp.get("status", "ok"))
        response["active"] = active
        response["generation_token"] = generation_token
        if "history_id" in stop_resp:
            response["item_id"] = stop_resp.get("history_id")
        return response

    def handle_get_meeting_live_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC get_meeting_live_state: снимок для панели/поллинга."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "active": False, "privacy_mode_active": True}
        with self._lock:
            s = self._session
            if s is None or s.privacy_stopped:
                return {"ok": True, "active": False}
            return {
                "ok": True,
                "active": True,
                "started_at": s.started_at,
                "promoted": s.promoted,
                "generation_token": s.generation_token,
                "owner_revision": s.owner_revision,
                "start_request_id": s.start_request_id,
                "stop_retry_pending": s.stop_retry_pending,
                "transcript_len": s.transcript_len,
                "transcript_tail": s.tail(),
                "items": list(s.items),
                "decisions": list(s.decisions),
                "questions": list(s.questions),
                "speakers": [dict(x) for x in s.speakers],
                "degraded": {"llm": s.degraded_llm or self._extractor is None,
                             "diarization": s.degraded_diarization},
                "last_updated_ts": s.last_updated_ts,
            }

    # ------------------------------------------------------------- lifecycle

    def begin_shutdown(self) -> None:
        """Неблокирующе запретить новые start до закрытия RecordingCore."""
        # Ставим флаг ДО любых ожиданий: параллельный start увидит shutdown
        # после возврата RecordingCore и выполнит owner-bound компенсацию.
        self._closed_event.set()
        begin_core_shutdown = getattr(
            self._recording_core,
            "begin_shutdown",
            None,
        )
        if callable(begin_core_shutdown):
            try:
                # Запрещаем новые recorder.start до owner-bound компенсации:
                # без generation/token это устраняет ABA именно в shutdown.
                begin_core_shutdown()
            except Exception:
                logger.exception(
                    "meeting: RecordingCore не принял begin_shutdown"
                )

    def close(self) -> bool:
        """Остановить meeting-worker; False сохраняет retry-handle."""
        self.begin_shutdown()
        worker_stopped = self._stop_worker()
        # C2a Task 10 (Фикс 3): зеркально остальным teardown-путям.
        self._release_lease()

        if not worker_stopped:
            logger.error(
                "meeting: close сохранил живой worker-handle; "
                "повторный close обязан завершить teardown"
            )
            return False

        if not self._setup_done_event.wait(_SETUP_CLOSE_WAIT_SEC):
            logger.error(
                "meeting: close сохранил незавершённый start-setup; "
                "повторный close выполнит recovery после его возврата"
            )
            return False

        if not self._retry_pending_recovery():
            logger.error(
                "meeting: close сохранил recovery-session — "
                "компенсация перехода не подтверждена"
            )
            return False

        with self._lock:
            self._session = None
            self._next_due = {}
            self._recovery_pending = False
            self._recovery_kind = None
            self._recovery_owner_revision = None
        return True

    def _retry_pending_recovery(self) -> bool:
        """Повторить безопасную компенсацию незавершённого start-перехода."""
        with self._lock:
            recovery_pending = self._recovery_pending
            recovery_kind = self._recovery_kind
            owner_revision = self._recovery_owner_revision
        if not recovery_pending:
            return True

        if recovery_kind == _RECOVERY_ROLLBACK_OWNER:
            rollback_owner = getattr(
                self._recording_core,
                "rollback_owner_transition",
                None,
            )
            if owner_revision is None or not callable(rollback_owner):
                logger.error(
                    "meeting: promote-recovery не имеет revision/CAS-handler"
                )
                return False
            try:
                # False здесь безопасен: revision уже замещена или запись
                # остановлена. В отличие от exception результат CAS известен.
                rollback_owner(
                    expected_revision=owner_revision,
                    expected_owner="meeting",
                    restore_owner="dictation",
                )
                return True
            except Exception:
                logger.exception(
                    "meeting: повторный CAS-rollback promote owner упал"
                )
                return False

        if recovery_kind in {None, _RECOVERY_ABORT_OWNER}:
            abort_owned = getattr(
                self._recording_core,
                "abort_recording_if_owner",
                None,
            )
            try:
                return (
                    callable(abort_owned)
                    and bool(abort_owned("meeting"))
                )
            except Exception:
                logger.exception(
                    "meeting: повторная recovery-остановка при close упала"
                )
                return False

        logger.error("meeting: неизвестный recovery-kind %r", recovery_kind)
        return False

    def _start_worker(self) -> None:
        # C2a Task 10 (Фикс 1в): защитный пояс. Основной гард — в
        # handle_meeting_start (условие self._stop_event.is_set()); сюда
        # можно попасть в обход него — например, self-finalize путь
        # _run_due_job_once, где старый воркер физически ещё не успел
        # завершиться, а self._stop_event не взводился вовсе.
        with self._worker_lock:
            self._raise_if_closed()
            w = self._worker
            if w is not None and w.is_alive():
                raise RuntimeError(
                    "meeting: предыдущий воркер ещё жив — второй GPU-слот запрещён")
            self._stop_event.clear()
            self._worker_armed_event.clear()
            t = threading.Thread(
                target=self._worker_loop, name="meeting-gpu-slot", daemon=True)
            self._worker = t
            t.start()

    def _arm_worker(self) -> None:
        """Разрешить preflight-worker читать состояние активной записи."""
        with self._worker_lock:
            self._raise_if_closed()
            worker = self._worker
            if (
                worker is None
                or not worker.is_alive()
                or self._stop_event.is_set()
            ):
                raise RuntimeError(
                    "meeting: preflight-worker завершился до публикации встречи"
                )
            self._worker_armed_event.set()

    def _stop_worker(self) -> bool:
        """Запросить остановку и подтвердить смерть retained worker-handle."""
        with self._worker_lock:
            self._stop_event.set()
            # Разбудить поток, который ещё ждёт arm; после пробуждения он
            # сначала увидит stop_event и не выполнит ни одного meeting-job.
            self._worker_armed_event.set()
            t = self._worker
            if (
                t is not None
                and t.is_alive()
                and t is not threading.current_thread()
            ):
                t.join(timeout=_WORKER_JOIN_TIMEOUT_SEC)
                if t.is_alive():
                    logger.warning(
                        "meeting: воркер не завершился за %.1fс",
                        _WORKER_JOIN_TIMEOUT_SEC,
                    )
                    # C2a Task 10 (аудит HIGH): handle НЕ обнуляем — тред может
                    # быть ещё жив после таймаута join. Обнуление здесь "теряло"
                    # бы его: следующий handle_meeting_start() решил бы, что
                    # слот свободен, и заспавнил бы второй воркер параллельно
                    # со старым (двойной GPU-доступ + чужие pause/resume/события).
                    return False
            if t is not None and t.is_alive():
                return False
            self._worker = None
            return True

    def _teardown_session(
        self,
        emit_finished: bool,
        item_id: Any = None,
        *,
        expected_session: _MeetingSession | None = None,
        claim_finalizing: bool = False,
        stop_worker: bool = True,
    ) -> bool:
        """Атомарно снять ровно ожидаемую сессию и вернуть факт владения.

        Старый воркер хранит локальную ссылку на прежнюю _MeetingSession.
        Поэтому проверка тождества и однократный захват события finalizing
        делаются под одной блокировкой: проигравший гонку не опубликует
        событие finished дважды.
        """
        if stop_worker and not self._stop_worker():
            return False
        with self._lock:
            session = self._session
            if (
                session is None
                or (
                    expected_session is not None
                    and session is not expected_session
                )
            ):
                return False
            if claim_finalizing:
                if session.finalizing_emitted:
                    return False
                session.finalizing_emitted = True
            self._session = None
            self._next_due = {}
            self._recovery_pending = False
            self._recovery_kind = None
            self._recovery_owner_revision = None
        self._release_lease()
        if claim_finalizing:
            self._emit("meeting.finalizing", {
                "generation_token": session.generation_token,
            })
        if emit_finished:
            self._emit("meeting.finished", {
                "item_id": item_id,
                "generation_token": session.generation_token,
            })
        return True

    # ---------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        # Preflight-worker существует до recorder.start(), чтобы возможная
        # ошибка создания потока не оставила скрытый захват микрофона. До arm
        # он не читает recorder.is_recording и не может self-finalize сессию.
        while not self._stop_event.is_set():
            if self._worker_armed_event.wait(_WORKER_WAIT_SEC):
                break
        if self._stop_event.is_set():
            return

        while not self._stop_event.is_set():
            self._stop_event.wait(_WORKER_WAIT_SEC)
            if self._stop_event.is_set():
                break
            try:
                self._run_due_job_once(time.monotonic())
            except Exception:
                logger.exception("meeting: тик воркера упал")
            with self._lock:
                if self._session is None or self._session.privacy_stopped:
                    break

    def _raise_if_closed(self) -> None:
        """Не разрешить новому meeting-start пережить начало shutdown."""
        if self._closed_event.is_set():
            raise RuntimeError("meeting: service closing")

    def _run_due_job_once(self, now: float) -> MeetingJob | None:
        """Одна итерация слота: выполняет ВСЕ созревшие задачи по приоритету
        (строго последовательно — GPU-слот запрещает ТОЛЬКО одновременность,
        не последовательность в одном тике), возвращает последнюю выполненную.
        Без этого CHUNK_STT (короткий интервал) вечно "перевыставляет" свой
        due раньше ITEMS_LLM (длинный интервал) при прыжке `now` далеко вперёд
        одним вызовом — ITEMS_LLM никогда не получает слот. Тестируется напрямую.
        """
        with self._lock:
            s = self._session
        if s is None or s.privacy_stopped:
            return None

        # recorder_timeout — не доказательство terminal stop: Core удерживает
        # G1/spill для повторной попытки. Даже если прежний worker ещё успел
        # проснуться, он не должен стереть этот recovery-handle самофинализацией.
        if s.stop_retry_pending:
            return None

        # privacy посреди встречи: глушим live-обработку (спека §3)
        if self._settings_get("privacy_mode_enabled", False):
            with self._lock:
                s.privacy_stopped = True
            self._release_lease()
            logger.info("meeting: privacy включён посреди встречи — live-обработка остановлена")
            return None

        # запись остановили в обход meeting_stop -> финализируемся сами
        if not getattr(self._recorder, "is_recording", False):
            self._teardown_session(
                emit_finished=True,
                item_id=None,
                expected_session=s,
                claim_finalizing=True,
                # Этот путь исполняет сам worker: join себя всегда вернёт
                # False, хотя сессией он ещё владеет.
                stop_worker=False,
            )
            return None

        self._renew_lease_if_due(now)

        ran: MeetingJob | None = None
        for job in (MeetingJob.CHUNK_STT, MeetingJob.ITEMS_LLM, MeetingJob.DIAR_WINDOW):
            due = self._next_due.get(job)
            if due is None or now < due:
                continue
            try:
                if job is MeetingJob.CHUNK_STT:
                    self._job_chunk_stt(s)
                elif job is MeetingJob.ITEMS_LLM:
                    self._job_items_llm(s)
                else:
                    self._job_diar_window(s)
            finally:
                # skip-tick: перепланируем от завершения, без лавины
                self._next_due[job] = time.monotonic() + self._job_interval(job)
            ran = job
        return ran

    # ------------------------------------------------------------------ jobs

    def _job_chunk_stt(self, s: _MeetingSession) -> None:
        upto = float(self._recorder.get_duration_sec())
        if upto <= s.cursor_sec + 0.25:  # диапазон вырожден — нечего снимать
            return
        audio = self._recorder.snapshot_range(s.cursor_sec, upto)
        if getattr(audio, "size", 0) == 0:
            return
        payload = self._transcriber.transcribe_preview(
            audio_data=audio, quality_profile="balanced")
        text = payload.get("text") if isinstance(payload, dict) else str(payload or "")
        text = (text or "").strip()
        with self._lock:
            if self._session is not s:  # протухший тик после stop (см. _job_diar_window)
                return
            s.cursor_sec = upto
            if text:
                s.chunks.append(text + " ")
                s.transcript_len += len(text) + 1
                s.last_updated_ts = time.time()
        if text:
            self._emit("meeting.transcript_appended",
                       {
                           "chunk_text": text,
                           "total_len": s.transcript_len,
                           "generation_token": s.generation_token,
                       })

    def _job_items_llm(self, s: _MeetingSession) -> None:
        if self._extractor is None:
            with self._lock:
                s.degraded_llm = True
            return
        with self._lock:
            full_text = "".join(s.chunks)
        if len(full_text) - s.last_extract_len < _ITEMS_MIN_GROWTH:
            return  # текст почти не вырос — экономим LLM
        self._recording_core.pause_realtime_partials()
        try:
            result = self._extractor.extract(full_text, language=s.language)
        finally:
            self._recording_core.resume_realtime_partials()
        with self._lock:
            if self._session is not s:  # протухший тик после stop (см. _job_diar_window)
                return
            s.degraded_llm = not result.ok
            if result.ok:
                s.items = [ai.to_dict() if hasattr(ai, "to_dict") else dict(ai)
                           for ai in result.action_items]
                s.decisions = list(result.decisions)
                s.questions = list(result.questions)
                s.last_extract_len = len(full_text)
                s.last_updated_ts = time.time()
        if result.ok:
            self._emit("meeting.items_updated", {
                "items": list(s.items), "decisions": list(s.decisions),
                "questions": list(s.questions),
                "generation_token": s.generation_token,
            })

    def _job_diar_window(self, s: _MeetingSession) -> None:
        """DIAR_WINDOW-тик (C2b, спека §2.5a): окно → WAV → диаризация+эмбеддинги
        одним прогоном → сшивка в сессионный реестр. Исключения гасятся в
        degraded-флаг — воркер и встреча живут дальше."""
        if s.tracker is None:
            return
        if self._diarize_window is None or _sf is None or self._data_dir is None:
            with self._lock:
                # Громкий WARN ровно один раз на сессию (тик каждые ~90с) —
                # иначе тихо сломавшаяся проводка (getattr в service.py вернул
                # None после переименования) деградирует спикеров навсегда без
                # единой строчки в логах (класс «декоративная проводка»).
                if not s.degraded_diarization:
                    logger.warning(
                        "meeting: DIAR_WINDOW недоступен (diarize_window=%s, "
                        "sf=%s, data_dir=%s) — спикеры деградированы",
                        self._diarize_window is not None,
                        _sf is not None,
                        self._data_dir is not None,
                    )
                s.degraded_diarization = True
            return
        try:
            upto = float(self._recorder.get_duration_sec())
            window = float(self._settings_get("meeting_diar_window_sec", 90.0))
            start = max(0.0, upto - window)
            if upto - start < _DIAR_MIN_AUDIO_SEC:
                return
            audio = self._recorder.snapshot_range(start, upto)
            if getattr(audio, "size", 0) == 0:
                return
            tmp_dir = self._data_dir / "tmp_meeting"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = tmp_dir / f"diar_{uuid.uuid4().hex}.wav"
            try:
                _sf.write(str(wav_path), audio,
                          int(getattr(self._recorder, "sample_rate", 16000)))
                self._recording_core.pause_realtime_partials()
                try:
                    result = self._diarize_window(str(wav_path))
                finally:
                    self._recording_core.resume_realtime_partials()
            finally:
                wav_path.unlink(missing_ok=True)
            s.tracker.ingest(
                segments=result.get("segments", []),
                embeddings=result.get("speaker_embeddings", {}),
                now_ts=time.time())
            snap = s.tracker.snapshot()
            with self._lock:
                # Fable-гейт Finding 2: воркер мог пережить _stop_worker
                # (join-таймаут на лок-контеншене диаризации) — протухший тик
                # не должен мутировать снятую сессию и эмиттить
                # speakers_updated ПОСЛЕ meeting.finished.
                if self._session is not s:
                    return
                s.speakers = snap
                s.degraded_diarization = False
                s.last_updated_ts = time.time()
            self._emit("meeting.speakers_updated", {
                "speakers": list(snap),
                "generation_token": s.generation_token,
            })
        except Exception:
            logger.warning("meeting: DIAR_WINDOW-тик упал", exc_info=True)
            with self._lock:
                s.degraded_diarization = True

    # ------------------------------------------------------------ intervals

    def _chunk_interval(self) -> float:
        return float(self._settings_get("meeting_chunk_stt_interval_sec", 25.0))

    def _items_interval(self) -> float:
        base = float(self._settings_get("meeting_items_interval_sec", 60.0))
        with self._lock:
            total = self._session.transcript_len if self._session else 0
        # адаптив (спека §2.2): на длинной встрече вызовы реже
        return max(base, total / 120.0)

    def _diar_interval(self) -> float:
        return float(self._settings_get("meeting_diar_interval_sec", 90.0))

    def _job_interval(self, job: MeetingJob) -> float:
        if job is MeetingJob.CHUNK_STT:
            return self._chunk_interval()
        if job is MeetingJob.ITEMS_LLM:
            return self._items_interval()
        return self._diar_interval()

    # ---------------------------------------------------------------- lease

    def _lease_enabled(self) -> bool:
        return bool(self._settings_get("llm_brain_lease_enabled", True))

    def _acquire_lease(self) -> None:
        if not self._lease_enabled():
            return
        try:
            from backend.brain_lease import acquire_brain_lease
            acquire_brain_lease("krab_ear", ttl_sec=_LEASE_TTL_SEC)
            self._next_due[("lease",)] = time.monotonic() + _LEASE_RENEW_SEC
        except Exception as exc:
            logger.debug("meeting: brain-lease acquire error (ignored): %s", exc)

    def _renew_lease_if_due(self, now: float) -> None:
        if not self._lease_enabled():
            return
        due = self._next_due.get(("lease",))
        if due is not None and now >= due:
            self._acquire_lease()

    def _release_lease(self) -> None:
        if not self._lease_enabled():
            return
        try:
            from backend.brain_lease import release_brain_lease
            release_brain_lease("krab_ear")
        except Exception as exc:
            logger.debug("meeting: brain-lease release error (ignored): %s", exc)

    # ---------------------------------------------------------------- events

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._bus.emit(event_type, payload)
        except Exception:
            logger.warning("meeting: emit %s упал", event_type, exc_info=True)
