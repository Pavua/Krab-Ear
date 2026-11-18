"""CallSessionService — управление жизненным циклом звонковых сессий (CRUD + переходы состояний).

Выделено из service.py для уменьшения размера монолитного BackendService.
Все handler-методы делегируются сюда из BackendService.handle_request.

Связи модуля:
1) CallSessionStore: NDJSON-backed хранилище сессий.
2) BackendService: делегирует IPC-обработчики через handle_request.

NB (W1775): прежде конструктор принимал `auto_end: CallAutoEnd`, но это поле
(`self._auto_end`) НИКОГДА не читалось — assigned-and-never-read. Логика
автоматического завершения вызывается напрямую из BackendService через
`call_check_auto_end` → `self._call_auto_end.handle_check_auto_end`, а не через
эту сессионную службу. Мёртвый параметр/поле удалены.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Optional

from backend.observability import add_breadcrumb, mask_phone

logger = logging.getLogger("KrabEar.Backend.CallSession")

_REDACTED = "REDACTED"

#: Как часто опрос статуса сверяет журнал со шлюзом (на одну сессию). UI опрашивает чаще,
#: а каждый запрос к шлюзу — синхронный HTTP внутри IPC-обработчика.
_GATEWAY_RECONCILE_MIN_INTERVAL_SEC = 3.0
#: Терминальные статусы сессии Voice Gateway → чем закрывать журнал (False = completed).
_GATEWAY_TERMINAL_STATUSES = {"stopped": False, "failed": True}
#: Потолок словаря отметок сверки: записи живут только у незакрытых сессий.
_GATEWAY_RECONCILE_MAX_TRACKED = 256


class CallSessionService:
    """Обработчики IPC для управления звонковыми сессиями."""

    def __init__(
        self,
        store: Any,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
        get_provider_fn: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """
        Args:
            store: CallSessionStore — NDJSON-хранилище сессий.
            settings_get: callable(key, default) → Any — runtime settings lookup
                (передаётся как BackendService._get_runtime_setting).
                Используется для privacy_mode_enabled gate в handle_call_session_get
                и handle_call_session_list (C1, wave-31).
                None → gate выключен (privacy_mode считается False).
            get_provider_fn: callable(settings) → CallProvider — фабрика провайдера телефонии.
        """
        self._store = store
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._get_provider_fn = get_provider_fn
        # Transient in-memory flag: tracks whether bot autopilot is active per session.
        # Mirrors Voice Gateway's agent.mode ("takeover" vs "autopilot").
        # Not persisted — resets to True (bot active) when the process restarts.
        self._bot_active: dict[str, bool] = {}
        self._bot_active_lock = threading.Lock()
        # Когда каждую сессию последний раз сверяли со шлюзом (monotonic).
        self._gateway_checked_at: dict[str, float] = {}
        self._gateway_checked_lock = threading.Lock()

    @staticmethod
    def _scrub_session(raw: dict[str, Any]) -> dict[str, Any]:
        """Возвращает копию словаря сессии с redacted phone и пустым транскриптом.

        Используется в privacy_mode: номер телефона и история транскрипций —
        PII; структура ответа (все ключи) сохраняется для schema-parity.
        """
        scrubbed = dict(raw)
        scrubbed["phone_number"] = _REDACTED
        scrubbed["transcript_history"] = []
        return scrubbed

    # ------------------------------------------------------------------ #
    # CRUD handlers                                                        #
    # ------------------------------------------------------------------ #

    def handle_call_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Инициирует исходящий звонок через провайдера и заводит запись журнала.

        🔴 Путь инициации до 03.09.2026 отсутствовал вовсе: `dial()` не вызывала
        ни одна строка прод-кода, `get_provider_fn` принимался и не звался, а
        `call_session_create` лишь писал в журнал звонок, которого никто не
        совершал. Волна консолидации отдала линию Voice Gateway (спека
        docs/superpowers/specs/2026-09-03-telephony-consolidation.md), и этот
        метод — единственная дорога от IPC до провайдера.

        Параметры: `phone` (E.164, обязателен), `goal_text` (обязателен),
        `prompt` (необязателен — с ним звонок ведёт агент шлюза).

        🔴 Порядок «сначала звонок, потом запись» намеренный: несостоявшийся
        звонок не должен оседать в журнале. Иначе история копит звонки, которых
        не было — ровно та болезнь, из-за которой прежняя телефония выглядела
        живой.
        """
        phone = str(params.get("phone") or "").strip()
        if not phone:
            raise ValueError("Параметр 'phone' обязателен")
        goal = str(params.get("goal_text") or "").strip()
        if not goal:
            raise ValueError("Параметр 'goal_text' обязателен")

        # Приватный режим запрещает исходящие целиком: номер — персональные
        # данные, а звонок — выход наружу, который нельзя отозвать.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "error": "privacy_mode",
                    "message": "Приватный режим запрещает исходящие звонки"}

        if self._get_provider_fn is None:
            return {"ok": False, "error": "call_provider_unavailable",
                    "message": "Провайдер звонков не подключён"}
        provider = self._get_provider_fn(None)
        if provider is None:
            return {"ok": False, "error": "call_provider_unavailable",
                    "message": "Провайдер звонков не подключён"}

        dial_kwargs: dict[str, Any] = {}
        prompt = str(params.get("prompt") or "").strip()
        if prompt:
            dial_kwargs["prompt"] = prompt
        for key in ("target_lang", "src_lang", "transport", "max_duration_sec"):
            if params.get(key) is not None:
                dial_kwargs[key] = params[key]

        result = provider.dial(phone, **dial_kwargs) or {}
        if not result.get("ok"):
            return {"ok": False,
                    "error": str(result.get("error") or "dial_failed"),
                    "message": str(result.get("message") or "")}

        gateway_session_id = str(result.get("call_control_id") or "")
        try:
            session = self._store.create(
                phone_number=phone, goal_text=goal, gateway_session_id=gateway_session_id,
            )
            # Линия уже набирается: журнал не должен оставаться в idle («не звонили»).
            self._store.update_status(session_id=session.id, new_status="dialing")
        except Exception:
            # Звонок идёт, а записи о нём нет — завершить его потом будет нечем. Кладём сразу.
            try:
                provider.hangup(gateway_session_id)
            except Exception:  # noqa: BLE001
                logger.exception("call_start: не удалось положить звонок после сбоя журнала")
            raise
        add_breadcrumb(
            category="call_session",
            message="call_start",
            level="info",
            data={"ok": True, "phone": mask_phone(phone),
                  "provider": str(result.get("provider") or "")},
        )
        return {
            "ok": True,
            "session_id": session.id,
            "gateway_session_id": str(result.get("call_control_id") or ""),
            "call_id": str(result.get("call_id") or ""),
            "status": str(result.get("status") or "dialing"),
        }

    def handle_call_session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Создаёт новую звонковую сессию.

        Параметры:
          - phone: str — номер телефона (обязательный).
          - goal_text: str — цель звонка (обязательный).

        Возвращает:
          {session_id, status, created_at}
        """
        _t0 = time.monotonic()
        phone = str(params.get("phone") or "").strip()
        if not phone:
            raise ValueError("Параметр 'phone' обязателен")
        goal = str(params.get("goal_text") or "").strip()
        if not goal:
            raise ValueError("Параметр 'goal_text' обязателен")

        try:
            session = self._store.create(
                phone_number=phone,
                goal_text=goal,
            )
            add_breadcrumb(
                category="call_session",
                message="call_session_create",
                level="info",
                data={
                    "ok": True,
                    "phone": mask_phone(phone),
                    "session_id": session.id,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return {"session_id": session.id, "status": session.status, "created_at": session.created_at}
        except Exception as exc:
            add_breadcrumb(
                category="call_session",
                message="call_session_create",
                level="error",
                data={
                    "ok": False,
                    "phone": mask_phone(phone),
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    def handle_call_session_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает полную запись сессии по id.

        Параметры:
          - id: str — идентификатор сессии.

        Возвращает полный словарь CallSession или ошибку "not_found".

        Privacy gate (C1, wave-31): когда privacy_mode_enabled=True возвращает
        сессию с redacted phone_number и пустым transcript_history — структура
        ответа сохраняется для schema-parity, PII не раскрывается.
        """
        session_id = str(params.get("id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'id' обязателен")
        session = self._store.get(session_id)
        if session is None:
            raise KeyError(f"Сессия не найдена: {session_id!r}")
        if self._reconcile_with_gateway(session):
            session = self._store.get(session_id) or session
        raw = session.to_dict()
        if self._settings_get("privacy_mode_enabled", False):
            return self._scrub_session(raw)
        return raw

    def handle_call_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список звонковых сессий.

        Параметры:
          - limit: int — макс. количество сессий (по умолчанию 50, макс. 500).
          - status_filter: str | None — фильтр по статусу (idle/dialing/...).

        Возвращает:
          {sessions: [...], total: N}

        Privacy gate (C1, wave-31): когда privacy_mode_enabled=True возвращает
        scrubbed сессии — phone_number=REDACTED, transcript_history=[] в каждой.
        """
        limit = max(1, min(int(params.get("limit", 50)), 500))
        status_filter = params.get("status_filter") or None
        if status_filter:
            status_filter = str(status_filter).strip() or None

        sessions = self._store.list_sessions(
            limit=limit,
            status_filter=status_filter,
        )
        if self._settings_get("privacy_mode_enabled", False):
            sessions = [self._scrub_session(s) for s in sessions]
        return {"sessions": sessions, "total": len(sessions)}

    def handle_call_session_update_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применяет переход статуса звонковой сессии.

        Параметры:
          - id: str        — идентификатор сессии (обязательный).
          - status: str    — новый статус (idle/dialing/connected/talking/ending/completed/failed).

        Возвращает:
          {session_id, status}
        """
        _t0 = time.monotonic()
        session_id = str(params.get("id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'id' обязателен")
        new_status = str(params.get("status") or "").strip()
        if not new_status:
            raise ValueError("Параметр 'status' обязателен")

        try:
            session = self._store.update_status(session_id=session_id, new_status=new_status)
            add_breadcrumb(
                category="call_session",
                message="call_session_update_status",
                level="info",
                data={
                    "ok": True,
                    "session_id": session_id,
                    "new_status": new_status,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return {"session_id": session.id, "status": session.status}
        except Exception as exc:
            add_breadcrumb(
                category="call_session",
                message="call_session_update_status",
                level="error",
                data={
                    "ok": False,
                    "session_id": session_id,
                    "new_status": new_status,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    def handle_call_session_add_transcript(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет реплику в транскрипт сессии.

        Параметры:
          - id: str      — идентификатор сессии (обязательный).
          - speaker: str — метка говорящего, например "agent" или "caller".
          - text: str    — текст реплики (обязательный).
          - ts: str      — ISO 8601 timestamp (опциональный, по умолчанию now).

        Возвращает:
          {session_id, transcript_count}
        """
        session_id = str(params.get("id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'id' обязателен")
        speaker = str(params.get("speaker") or "unknown").strip()
        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("Параметр 'text' обязателен")
        ts = params.get("ts") or None
        if ts:
            ts = str(ts).strip() or None

        session = self._store.add_transcript(
            session_id=session_id,
            speaker=speaker,
            text=text,
            ts=ts,
        )
        return {"session_id": session.id, "transcript_count": len(session.transcript_history)}

    def _reconcile_with_gateway(self, session: Any) -> bool:
        """Закрывает журнал, если шлюз уже закончил эту линию. Возвращает True, если закрыл.

        Журнал сам не узнаёт, что собеседник положил трубку: без сверки сессия навсегда
        оставалась `dialing`. Сверка только читает шлюз и закрывает журнал ИСКЛЮЧИТЕЛЬНО по
        явному терминальному статусу: 404, таймаут, сбой и ответ без статуса значат «не знаю»,
        а не «звонка нет» — закрыть журнал при живой линии хуже, чем опоздать.
        """
        gateway_session_id = getattr(session, "gateway_session_id", "")
        if not isinstance(gateway_session_id, str) or not gateway_session_id:
            return False
        if str(session.status) in {"completed", "failed"}:
            with self._gateway_checked_lock:
                self._gateway_checked_at.pop(session.id, None)
            return False
        now = time.monotonic()
        with self._gateway_checked_lock:
            last = self._gateway_checked_at.get(session.id)
            if last is not None and now - last < _GATEWAY_RECONCILE_MIN_INTERVAL_SEC:
                return False
            if len(self._gateway_checked_at) >= _GATEWAY_RECONCILE_MAX_TRACKED:
                self._gateway_checked_at.clear()
            self._gateway_checked_at[session.id] = now
        try:
            provider = self._get_provider_fn(None) if self._get_provider_fn is not None else None
            if provider is None:
                return False
            state = provider.get_call_status(gateway_session_id) or {}
            if not state.get("ok"):
                return False
            gateway_status = str(state.get("status") or "").strip().lower()
            if gateway_status not in _GATEWAY_TERMINAL_STATUSES:
                return False
            mark = (self._store.mark_failed if _GATEWAY_TERMINAL_STATUSES[gateway_status]
                    else self._store.mark_completed)
            # Хранилище ПРИСВАИВАЕТ стоимость: передаём уже накопленную, чтобы её не обнулить.
            mark(session_id=session.id, end_reason=f"gateway_{gateway_status}",
                 cost_usd=float(getattr(session, "cost_usd", 0.0) or 0.0), from_any_live_state=True)
        except Exception as exc:  # noqa: BLE001 — сверка не смеет ломать чтение журнала
            logger.warning("call_session_get: сверка со шлюзом не удалась (%s)", type(exc).__name__)
            return False
        logger.info("call_session_get: шлюз закончил линию (%s), журнал закрыт", gateway_status)
        with self._gateway_checked_lock:
            self._gateway_checked_at.pop(session.id, None)
        return True

    def _hangup_gateway_call(self, session_id: str) -> dict[str, Any] | None:
        """Кладёт звонок в шлюзе для сессии, начатой через call_start.

        Возвращает None, если класть нечего (журнальная сессия без линии, неизвестный id или
        уже терминальное состояние — эти случаи обрабатывает прежний путь), иначе
        ``{"ok": True}`` либо отказ ``{"ok": False, "error": ..., "reason": ...}``.
        """
        try:
            current = self._store.get(session_id)
        except Exception:  # noqa: BLE001
            current = None
        gateway_session_id = getattr(current, "gateway_session_id", "")
        # Только настоящая непустая строка: у журнальной сессии без линии поля нет вовсе.
        if current is None or not isinstance(gateway_session_id, str) or not gateway_session_id:
            return None
        if str(current.status) in {"completed", "failed"}:
            return None
        provider = self._get_provider_fn(None) if self._get_provider_fn is not None else None
        if provider is None:
            return {"ok": False, "error": "call_provider_unavailable",
                    "reason": "call_provider_unavailable",
                    "message": "Провайдер звонков не подключён: звонок не завершён"}
        result = provider.hangup(gateway_session_id) or {}
        if result.get("ok"):
            return {"ok": True}
        code = str(result.get("error") or "hangup_failed")
        logger.error("call_session_end: шлюз не положил звонок (%s)", code)
        return {"ok": False, "error": code, "reason": "hangup_failed",
                "message": "Voice Gateway не подтвердил завершение звонка"}

    def handle_call_session_end(self, params: dict[str, Any]) -> dict[str, Any]:
        """Завершает звонковую сессию: переводит в COMPLETED, вычисляет duration/cost.

        Параметры:
          - id: str — идентификатор сессии.
          - reason: str — причина завершения (completed / no_answer / voicemail / opt_out / timeout).
          - cost_usd: float — фактическая стоимость звонка в USD (по умолчанию 0.0).
          - failed: bool — если True, переводит в FAILED вместо COMPLETED.

        Возвращает:
          {session_id, status, duration_sec, cost_usd, end_reason}
        """
        _t0 = time.monotonic()
        session_id = str(params.get("id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'id' обязателен")
        reason = str(params.get("reason") or "completed").strip()
        # wave-1770 LOW: NaN/Inf cost_usd serializes to JSON NaN → Swift crash.
        _raw_cost = float(params.get("cost_usd") or 0.0)
        cost_usd = _raw_cost if math.isfinite(_raw_cost) and _raw_cost >= 0 else 0.0
        failed = bool(params.get("failed", False))

        # Сессия, начатая через call_start, — это живая линия в Voice Gateway: сначала кладём
        # её там, и только подтверждённый сброс закрывает журнал. Раньше метод лишь писал в
        # журнал (и то безуспешно: переход idle→completed запрещён), а звонок продолжался.
        gateway_end = self._hangup_gateway_call(session_id)
        if gateway_end is not None and not gateway_end.get("ok"):
            return gateway_end
        # Журнальная сессия без линии зовёт хранилище прежней подписью: её двойники в тестах и
        # сторонние реализации нового аргумента не знают.
        extra: dict[str, Any] = {"from_any_live_state": True} if gateway_end is not None else {}

        try:
            mark = self._store.mark_failed if failed else self._store.mark_completed
            session = mark(session_id=session_id, end_reason=reason, cost_usd=cost_usd, **extra)
            add_breadcrumb(
                category="call_session",
                message="call_session_end",
                level="info",
                data={
                    "ok": True,
                    "session_id": session_id,
                    "end_reason": reason,
                    "failed": failed,
                    "duration_sec": session.duration_sec,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return {
                "session_id": session.id,
                "status": session.status,
                "duration_sec": session.duration_sec,
                "cost_usd": session.cost_usd,
                "end_reason": session.end_reason,
            }
        except ValueError:
            # C4 (wave-31): FSM transition error on IDLE/terminal sessions
            # (e.g. call_session_end on an idle session that was never dialed).
            # Return a graceful structured error instead of propagating the
            # ValueError which would cause a 500-style IPC error.
            current_state: str = "unknown"
            try:
                current_sess = self._store.get(session_id)
                if current_sess is not None:
                    current_state = str(current_sess.status)
            except Exception:  # noqa: BLE001
                pass
            add_breadcrumb(
                category="call_session",
                message="call_session_end",
                level="warning",
                data={
                    "ok": False,
                    "session_id": session_id,
                    "end_reason": reason,
                    "error": "invalid_state_transition",
                    "current_state": current_state,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return {
                "ok": False,
                "reason": "invalid_state_transition",
                "current_state": current_state,
            }
        except Exception as exc:
            add_breadcrumb(
                category="call_session",
                message="call_session_end",
                level="error",
                data={
                    "ok": False,
                    "session_id": session_id,
                    "end_reason": reason,
                    "failed": failed,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    # ------------------------------------------------------------------ #
    # Operator takeover handlers                                           #
    # ------------------------------------------------------------------ #

    def handle_call_intervene(self, params: dict[str, Any]) -> dict[str, Any]:
        """Оператор берёт управление: бот замолкает (bot_active=False).

        Зеркало Voice Gateway POST /v1/sessions/{id}/agent/takeover.
        Состояние хранится в памяти (сбрасывается при рестарте процесса).

        Параметры:
          - session_id: str — идентификатор сессии.

        Возвращает:
          {ok, session_id, bot_active}
        """
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'session_id' обязателен")
        # Verify session exists (raises KeyError if not found)
        session = self._store.get(session_id)
        if session is None:
            raise KeyError(f"Сессия не найдена: {session_id!r}")
        with self._bot_active_lock:
            self._bot_active[session_id] = False
        add_breadcrumb(
            category="call_session",
            message="call_intervene",
            level="info",
            data={"ok": True, "session_id": session_id, "bot_active": False},
        )
        logger.info(
            "Оператор взял управление, бот приостановлен",
            extra={"session_id": session_id, "bot_active": False},
        )
        return {"ok": True, "session_id": session_id, "bot_active": False}

    def handle_call_resume_bot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает управление боту (bot_active=True).

        Зеркало Voice Gateway POST /v1/sessions/{id}/agent/resume.
        Состояние хранится в памяти (сбрасывается при рестарте процесса).

        Параметры:
          - session_id: str — идентификатор сессии.

        Возвращает:
          {ok, session_id, bot_active}
        """
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("Параметр 'session_id' обязателен")
        # Verify session exists (raises KeyError if not found)
        session = self._store.get(session_id)
        if session is None:
            raise KeyError(f"Сессия не найдена: {session_id!r}")
        with self._bot_active_lock:
            self._bot_active[session_id] = True
        add_breadcrumb(
            category="call_session",
            message="call_resume_bot",
            level="info",
            data={"ok": True, "session_id": session_id, "bot_active": True},
        )
        logger.info(
            "Управление возвращено боту",
            extra={"session_id": session_id, "bot_active": True},
        )
        return {"ok": True, "session_id": session_id, "bot_active": True}
