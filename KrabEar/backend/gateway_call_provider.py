"""Звонки через Voice Gateway — замена собственным адаптерам телефонии.

Волна консолидации 03.09.2026, спека
`docs/superpowers/specs/2026-09-03-telephony-consolidation.md`.

Krab Ear держал свою телефонию с 24.04.2026 (Telnyx / Twilio / local-SIP,
3049 строк) и не совершил через неё ни одного звонка: ключ пуст, журнал сессий
нулевой. У Voice Gateway телефония полнее (пять транспортов, входящие звонки,
barge-in, удержание, тональный набор, режимы агента) и работает ежедневно.
Контракт согласован 03.09 обеими сессиями шлюза.

Класс реализует существующий `CallProvider`, поэтому вызывающий код
(`CallSessionService`, IPC-хендлеры) не меняется — подменяется только то, ЧЕМ
звоним. HTTP идёт через уже имеющийся `VoiceGatewayClient`: второй клиент к
тому же сервису строить незачем.

🔴 Идентификаторы. Наружу как ``call_control_id`` отдаётся **session_id шлюза**:
именно он адресует звонок в его REST (`/v1/telephony/calls/{session_id}/hangup`).
``call_sid`` провайдера возвращается для справки — управлять по нему нельзя.
Путаница этих двух идентификаторов ломает завершение звонка молча, поэтому
имена разведены здесь, а не у вызывающего.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, Optional

from backend.call_assist_service import VoiceGatewayClient
from backend.call_provider import (
    ERR_INVALID_PHONE,
    ERR_MISSING_CALL_ID,
)

logger = logging.getLogger("KrabEar.Backend.GatewayCallProvider")

#: E.164: плюс, затем 8–15 цифр. Шлюз тоже валидирует, но негодный номер не
#: должен тратить сетевой вызов и висеть в его журнале как отказ.
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

#: Статусы сессии шлюза, означающие «звонок ещё идёт».
_ACTIVE_STATUSES = frozenset({"running", "dialing", "ringing", "connected", "talking"})

ERR_NOT_CONFIGURED = "gateway_not_configured"
#: Исход набора неизвестен: запрос оборвался, и сессию по ключу повтора найти не удалось.
#: Это НЕ «звонка нет» — линия могла подняться.
ERR_DIAL_UNKNOWN = "gateway_dial_unknown"

#: Набор в шлюзе синхронный (сессия, календарь до 3 с, create_call у провайдера): общие
#: 4 с REST-клиента обрывали его раньше ответа, и живой звонок оставался без идентификатора.
DIAL_TIMEOUT_SEC = 30.0
#: Поиск сессии оборвавшегося набора. Живой замер 18.11.2026: набор не уложился в 20 с, а
#: единственный поиск с общими 4 с оборвался, пока шлюз был занят, — Ear ответил «исход
#: неизвестен» при живой линии. Поэтому поиск повторяется в пределах бюджета.
#: Набор + бюджет + последний запрос обязаны уложиться в 60 с ожидания IPC у агента.
ADOPT_TIMEOUT_SEC = 8.0
#: Сброс: шлюз синхронно ждёт провайдера. Живой замер 18.11.2026 — 1,4 с и 20,5 с; с общими
#: 4 с Ear сообщал «не удалось завершить», пока сброс на самом деле шёл.
HANGUP_TIMEOUT_SEC = 30.0
ADOPT_BUDGET_SEC = 12.0
ADOPT_RETRY_PAUSE_SEC = 1.0

#: session_id шлюза подставляется в путь REST: только безопасный алфавит, без `/`, `?`, `..`.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: Ответы hangup, означающие «звонка уже нет»: неизвестная сессия и гонка с завершением.
#: Узнаются по ТЕЛУ (`detail` шлюза), а не только по коду: голый 404 (`Not Found`) — это
#: неверный адрес или старый шлюз без маршрута, и звонок при этом мог остаться живым.
_HANGUP_GONE_MARKERS = (("http_404", "session_not_found"), ("http_409", "session_terminal"))


class GatewayCallProvider:
    """`CallProvider` поверх Voice Gateway."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: Any | None = None,
        default_transport: str = "twilio",
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        # Инъекция клиента — ради тестов; в проде это статические методы
        # VoiceGatewayClient, уже умеющие таймауты и разбор ошибок.
        self._http = http or VoiceGatewayClient
        self._default_transport = default_transport

    # -- служебное -----------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    def _unconfigured(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": ERR_NOT_CONFIGURED,
            "message": "Voice Gateway не настроен: нужны URL и API-ключ",
        }

    @staticmethod
    def _unwrap(resp: Any) -> Dict[str, Any]:
        """Разворачивает конверт VoiceGatewayClient в ответ шлюза.

        🔴 Клиент возвращает ДВА уровня: ``{"ok": True, "payload": {...}}``,
        где внешний ``ok`` — успех транспорта, а результат операции лежит
        внутри. Чтение полей прямо из конверта даёт «успех» с пустым
        ``session_id``: живая проверка 03.09.2026 поймала ровно это —
        шлюз вернул `vs_813a731e3db1`, а провайдер отдал пустоту, потому
        что смотрел не на тот уровень. Тесты молчали: их двойник отвечал
        плоско, то есть проверял фантазию, а не поведение зависимости.
        """
        if not isinstance(resp, dict):
            return {"ok": False, "error": "gateway_bad_response"}
        if resp.get("ok") is False:
            return {"ok": False, "error": str(resp.get("error") or "gateway_error")}
        inner = resp.get("payload")
        if isinstance(inner, dict):
            return inner
        # Плоский ответ (двойник в тестах, будущая версия клиента) — как есть.
        return resp

    def _post(
        self, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._unwrap(
            self._http.post(self._base_url, self._api_key, path, payload, timeout=timeout)
        )

    def _adopt_by_request_id(self, client_request_id: str) -> Dict[str, Any]:
        """Ищет сессию, созданную оборвавшимся запросом набора (контракт шлюза).

        Повторяет поиск в пределах `ADOPT_BUDGET_SEC`: сразу после обрыва шлюз ещё занят набором
        и может не ответить или ещё не знать ключ (404).
        """
        deadline = time.monotonic() + ADOPT_BUDGET_SEC
        while True:
            found = self._unwrap(self._http.get(
                self._base_url, self._api_key,
                f"/v1/sessions/by-request-id/{client_request_id}", timeout=ADOPT_TIMEOUT_SEC,
            )) or {}
            if not (found.get("error") or found.get("ok") is False):
                if str(found.get("session_id") or found.get("id") or ""):
                    return found
            if time.monotonic() >= deadline:
                return {}
            time.sleep(ADOPT_RETRY_PAUSE_SEC)

    def _get(self, path: str) -> Dict[str, Any]:
        return self._unwrap(self._http.get(self._base_url, self._api_key, path))

    # -- операции звонка -----------------------------------------------

    def dial(
        self,
        to_number: str,
        call_control_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        prompt: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Инициирует звонок через шлюз.

        Без ``prompt`` — переводческий звонок (`/outbound`); с ``prompt`` —
        звонок с целью, который ведёт агент шлюза (`/prompt-call`).
        ``call_control_id`` и ``webhook_url`` приняты ради совместимости с
        протоколом и игнорируются: маршрутизацию событий держит шлюз.
        """
        if not self.is_configured():
            return self._unconfigured()
        number = (to_number or "").strip()
        if not _E164_RE.match(number):
            return {
                "ok": False,
                "error": ERR_INVALID_PHONE,
                "message": f"Номер не в формате E.164: {number!r}",
            }

        if prompt:
            path = "/v1/telephony/calls/prompt-call"
            payload: Dict[str, Any] = {
                "to": number,
                "prompt": prompt,
                "transport": extra.get("transport", self._default_transport),
            }
            for key in ("target_lang", "src_lang", "speak_first", "max_duration_sec", "voice_mode"):
                if key in extra:
                    payload[key] = extra[key]
            # Ключ повтора: один на нажатие «Позвонить». По нему шлюз отдаёт ту же сессию
            # при повторе и позволяет найти её после оборванного запроса.
            client_request_id = uuid.uuid4().hex
            payload["client_request_id"] = client_request_id
        else:
            path = "/v1/telephony/calls/outbound"
            payload = {"to": number}
            for key in ("translation_mode", "src_lang", "tgt_lang", "voice_mode", "device_id"):
                if key in extra:
                    payload[key] = extra[key]
            client_request_id = ""

        raw = self._http.post(self._base_url, self._api_key, path, payload, timeout=DIAL_TIMEOUT_SEC)
        resp = self._unwrap(raw) or {}
        # Транспортный сбой виден только на КОНВЕРТЕ клиента: отказ самого шлюза приходит
        # либо HTTP-кодом (`http_<code>:...`), либо телом внутри `payload`.
        transport_failed = (
            isinstance(raw, dict) and raw.get("ok") is False and "payload" not in raw
            and not str(raw.get("error") or "").startswith("http_")
        )
        if transport_failed:
            # Транспортный сбой (таймаут, обрыв): шлюз мог успеть поднять линию. Отказ с
            # HTTP-кодом — определённый ответ шлюза, там искать нечего.
            adopted = self._adopt_by_request_id(client_request_id) if client_request_id else {}
            if adopted:
                resp = {"ok": True, **adopted,
                        "session_id": str(adopted.get("session_id") or adopted.get("id") or "")}
            else:
                logger.error("Набор оборвался, исход неизвестен: %s", resp.get("error"))
                return {
                    "ok": False,
                    "error": ERR_DIAL_UNKNOWN,
                    "message": "Шлюз не ответил вовремя: звонок мог начаться. Проверьте активные звонки.",
                }
        if not resp.get("ok"):
            # Ошибку шлюза отдаём как есть: подменять её своим кодом значит
            # прятать причину, по которой звонок не состоялся.
            return {
                "ok": False,
                "error": str(resp.get("error") or "gateway_call_failed"),
                "message": str(resp.get("message") or ""),
            }
        session_id = str(resp.get("session_id") or "")
        if not session_id:
            # Успех без идентификатора нечем завершить и не о чем спросить —
            # это отказ, а не звонок. Молчаливое ok здесь означало бы висящую
            # линию, которую невозможно положить.
            logger.error("Шлюз ответил успехом без session_id: %r", resp)
            return {
                "ok": False,
                "error": "gateway_no_session_id",
                "message": "Шлюз принял звонок, но не вернул идентификатор сессии",
            }
        return {
            "ok": True,
            "call_control_id": session_id,
            "call_id": str(resp.get("call_sid") or ""),
            "status": str(resp.get("status") or "dialing"),
            "provider": "voice_gateway",
        }

    def hangup(self, call_control_id: str) -> Dict[str, Any]:
        if not self.is_configured():
            return self._unconfigured()
        session_id = (call_control_id or "").strip()
        if not session_id:
            return {"ok": False, "error": ERR_MISSING_CALL_ID}
        if not _SESSION_ID_RE.match(session_id):
            return {"ok": False, "error": ERR_MISSING_CALL_ID}
        resp = self._post(f"/v1/telephony/calls/{session_id}/hangup", timeout=HANGUP_TIMEOUT_SEC) or {}
        if resp.get("ok"):
            return {"ok": True}
        error = str(resp.get("error") or "hangup_failed")
        if any(error.startswith(code) and marker in error for code, marker in _HANGUP_GONE_MARKERS):
            # Сессии в шлюзе нет или она уже завершается: класть нечего, цель достигнута.
            return {"ok": True, "already_gone": True}
        # Тело ответа шлюза наружу не отдаём: только код причины.
        return {"ok": False, "error": error.split(":", 1)[0]}

    def get_call_status(self, call_control_id: str) -> Dict[str, Any]:
        if not self.is_configured():
            return self._unconfigured()
        session_id = (call_control_id or "").strip()
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return {"ok": False, "error": ERR_MISSING_CALL_ID}
        resp = self._get(f"/v1/sessions/{session_id}") or {}
        # 🔴 Формы ответов шлюза разные даже между соседними endpoint'ами:
        # список сессий приходит обёрнутым (`{"ok":true,"items":[...]}`), а
        # ОДНА сессия — голым SessionState, без поля `ok` вовсе (замер 03.09).
        # Требовать `ok` здесь значит читать живой ответ как отказ, что и
        # случилось при первой живой проверке. Успех определяем по существу:
        # ошибки нет и статус пришёл.
        if resp.get("error") or resp.get("ok") is False:
            return {"ok": False, "error": str(resp.get("error") or "status_unavailable")}
        status = str(resp.get("status") or "")
        if not status:
            return {"ok": False, "error": "status_unavailable"}
        return {"ok": True, "status": status, "data": resp}

    def list_active_calls(self) -> Dict[str, Any]:
        if not self.is_configured():
            return self._unconfigured()
        resp = self._get("/v1/sessions") or {}
        if resp.get("error") or resp.get("ok") is False:
            return {"ok": False, "error": str(resp.get("error") or "list_unavailable")}
        # Шлюз отдаёт список в `items`; `sessions` — форма, которую назвала
        # одна из его сессий. Принимаем обе и не считаем отсутствие обёртки
        # отказом (см. комментарий в get_call_status).
        items = resp.get("items") or resp.get("sessions") or []
        active = [
            it for it in items
            if isinstance(it, dict) and str(it.get("status", "")).lower() in _ACTIVE_STATUSES
        ]
        return {"ok": True, "calls": active}
