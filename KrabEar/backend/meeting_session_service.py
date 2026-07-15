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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np

logger = logging.getLogger("krab_ear.backend")

_TAIL_CHARS = 600          # transcript_tail в get_meeting_live_state
_ITEMS_MIN_GROWTH = 200    # симв.: минимальный прирост текста для нового LLM-вызова
_LEASE_RENEW_SEC = 15.0    # период продления brain-lease
_LEASE_TTL_SEC = 45.0      # TTL lease (перекрывает период продления с запасом)
_WORKER_WAIT_SEC = 0.5     # шаг ожидания воркера


class LiveSpeakerTracker:
    """Сессионный реестр спикеров C2b (спека §2.5 + §2.5a).

    Локальные метки pyannote внутри окна анонимны и нестабильны между
    прогонами — идентичность спикеров держится ТОЛЬКО на эмбеддингах:
    cosine центроида окна против скользящего среднего центроида спикера.
    Реестр живёт в памяти сессии, на диск не пишется.

    Потокобезопасность НЕ нужна: все вызовы — из одного GPU-слот-треда;
    снапшот для IPC копируется в состояние сессии под её локом.
    """

    def __init__(self, threshold: float) -> None:
        self._threshold = float(threshold)
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
    ) -> None:
        self._recorder = recorder
        self._transcriber = transcriber
        self._recording_core = recording_core
        self._extractor = action_items_extractor
        self._settings_get = settings_get
        self._bus = event_bus

        # RLock (не Lock): _items_interval() зовёт self._lock изнутри блока,
        # уже удерживаемого handle_meeting_start (dict-литерал _next_due
        # вычисляется под внешним `with self._lock`) — non-reentrant Lock
        # там дедлочился бы.
        self._lock = threading.RLock()          # состояние сессии
        self._session: _MeetingSession | None = None
        self._next_due: dict[Any, float] = {}
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        # C2a Task 10 (Фикс 2): гейт идемпотентности handle_meeting_stop —
        # конкурентный/повторный вызов не должен звать handle_stop_recording
        # и эмиттить meeting.finished дважды.
        self._stopping = False

    # ------------------------------------------------------------------ IPC

    def handle_meeting_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC meeting_start: старт записи+сессии ИЛИ повышение идущей записи."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "skipped": "privacy_mode"}

        with self._lock:
            if self._session is not None and not self._session.privacy_stopped:
                return {"ok": True, "already_active": True,
                        "started_at": self._session.started_at,
                        "promoted": self._session.promoted}
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

        session: _MeetingSession | None = None
        try:
            start_resp = self._recording_core.handle_start_recording({})
            promoted = start_resp.get("status") == "already_recording"

            session = _MeetingSession(
                promoted=promoted,
                language=str(params.get("language", self._settings_get(
                    "meeting_items_language", "ru")) or "ru"),
                cursor_sec=float(self._recorder.get_duration_sec()) if promoted else 0.0,
            )
            now = time.monotonic()
            with self._lock:
                self._session = session
                self._next_due = {
                    MeetingJob.CHUNK_STT: now + self._chunk_interval(),
                    MeetingJob.ITEMS_LLM: now + self._items_interval(),
                }
            # C2a Task 10 (Фикс 4): внутри try — исключение из _start_worker()
            # (защитный пояс Фикс 1в: второй живой воркер) обязано откатить
            # сессию и освободить lease через except-ветку ниже, а не утечь
            # с "полу-стартованной" сессией без воркера.
            self._acquire_lease()
            self._start_worker()
        except Exception:
            with self._lock:
                if self._session is reservation or self._session is session:
                    self._session = None
                    self._next_due = {}
            self._release_lease()
            raise

        logger.info("meeting: сессия запущена", extra={
            "promoted": promoted, "language": session.language})
        return {"ok": True, "promoted": promoted, "started_at": session.started_at}

    def handle_meeting_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC meeting_stop: гасит live-сессию и останавливает запись обычным путём.

        C2a Task 10 (аудит MED): идемпотентен — дабл-клик в UI / два
        IPC-клиента, вызвавшие stop конкурентно, НЕ должны привести к
        двойному handle_stop_recording()/meeting.finished. Гейт _stopping
        под тем же self._lock, что и остальное состояние сессии; сбрасывается
        в finally, так что независимые ПОСЛЕДОВАТЕЛЬНЫЕ вызовы (новая сессия
        после предыдущего stop) не залипают.
        """
        with self._lock:
            if self._stopping:
                return {"ok": True, "already_stopping": True}
            self._stopping = True
        try:
            if self._settings_get("privacy_mode_enabled", False):
                # privacy включили посреди встречи: сессию всё равно закрываем,
                # но запись останавливает обычный privacy-путь записи.
                self._teardown_session(emit_finished=False)
                return {"ok": True, "skipped": "privacy_mode"}

            with self._lock:
                had_session = self._session is not None
            if not had_session:
                return {"ok": True, "active": False}

            self._stop_worker()
            self._emit("meeting.finalizing", {})
            stop_resp: dict[str, Any] = {}
            if getattr(self._recorder, "is_recording", False):
                stop_resp = self._recording_core.handle_stop_recording({})
            item_id = stop_resp.get("history_id")
            self._teardown_session(emit_finished=True, item_id=item_id)
            return {"ok": True, "item_id": item_id}
        finally:
            with self._lock:
                self._stopping = False

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
                "transcript_len": s.transcript_len,
                "transcript_tail": s.tail(),
                "items": list(s.items),
                "decisions": list(s.decisions),
                "questions": list(s.questions),
                "speakers": [],  # C2b
                "degraded": {"llm": s.degraded_llm or self._extractor is None,
                             "diarization": s.degraded_diarization},
                "last_updated_ts": s.last_updated_ts,
            }

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Останов воркера (BackendService.close())."""
        self._stop_worker()
        self._release_lease()  # C2a Task 10 (Фикс 3): зеркально остальным teardown-путям
        with self._lock:
            self._session = None

    def _start_worker(self) -> None:
        # C2a Task 10 (Фикс 1в): защитный пояс. Основной гард — в
        # handle_meeting_start (условие self._stop_event.is_set()); сюда
        # можно попасть в обход него — например, self-finalize путь
        # _run_due_job_once, где старый воркер физически ещё не успел
        # завершиться, а self._stop_event не взводился вовсе.
        w = self._worker
        if w is not None and w.is_alive():
            raise RuntimeError(
                "meeting: предыдущий воркер ещё жив — второй GPU-слот запрещён")
        self._stop_event.clear()
        t = threading.Thread(
            target=self._worker_loop, name="meeting-gpu-slot", daemon=True)
        self._worker = t
        t.start()

    def _stop_worker(self) -> None:
        self._stop_event.set()
        t = self._worker
        if t is not None and t.is_alive():
            t.join(timeout=30.0)
            if t.is_alive():
                logger.warning("meeting: воркер не завершился за 30с")
                # C2a Task 10 (аудит HIGH): handle НЕ обнуляем — тред может
                # быть ещё жив после таймаута join. Обнуление здесь "теряло"
                # бы его: следующий handle_meeting_start() решил бы, что
                # слот свободен, и заспавнил бы второй воркер параллельно
                # со старым (двойной GPU-доступ + чужие pause/resume/события).
                return
        self._worker = None

    def _teardown_session(self, emit_finished: bool,
                          item_id: Any = None) -> None:
        self._stop_worker()
        self._release_lease()
        if emit_finished:
            self._emit("meeting.finished", {"item_id": item_id})
        with self._lock:
            self._session = None
            self._next_due = {}

    # ---------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
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

        # privacy посреди встречи: глушим live-обработку (спека §3)
        if self._settings_get("privacy_mode_enabled", False):
            with self._lock:
                s.privacy_stopped = True
            self._release_lease()
            logger.info("meeting: privacy включён посреди встречи — live-обработка остановлена")
            return None

        # запись остановили в обход meeting_stop -> финализируемся сами
        if not getattr(self._recorder, "is_recording", False):
            self._release_lease()
            self._emit("meeting.finalizing", {})
            self._emit("meeting.finished", {"item_id": None})
            with self._lock:
                self._session = None
                self._next_due = {}
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
                else:  # DIAR_WINDOW: исполнитель придёт в C2b
                    pass
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
            s.cursor_sec = upto
            if text:
                s.chunks.append(text + " ")
                s.transcript_len += len(text) + 1
                s.last_updated_ts = time.time()
        if text:
            self._emit("meeting.transcript_appended",
                       {"chunk_text": text, "total_len": s.transcript_len})

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
                "questions": list(s.questions)})

    # ------------------------------------------------------------ intervals

    def _chunk_interval(self) -> float:
        return float(self._settings_get("meeting_chunk_stt_interval_sec", 25.0))

    def _items_interval(self) -> float:
        base = float(self._settings_get("meeting_items_interval_sec", 60.0))
        with self._lock:
            total = self._session.transcript_len if self._session else 0
        # адаптив (спека §2.2): на длинной встрече вызовы реже
        return max(base, total / 120.0)

    def _job_interval(self, job: MeetingJob) -> float:
        if job is MeetingJob.CHUNK_STT:
            return self._chunk_interval()
        if job is MeetingJob.ITEMS_LLM:
            return self._items_interval()
        return 120.0  # DIAR_WINDOW (C2b уточнит из настроек)

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
