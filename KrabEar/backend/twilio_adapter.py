"""Twilio REST adapter — Krab Ear Phase 3 outbound calls.

Использует Twilio REST API v2010 с Basic Auth (Account SID + Auth Token).
НЕ требует пакета twilio — только стандартный requests + HTTPBasicAuth.

При пустых credentials работает в stub-режиме: все методы возвращают
{"ok": False, "error": "twilio_not_configured"}.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

logger = logging.getLogger("KrabEar.Backend.TwilioAdapter")

# Twilio REST API base — включает Account SID как часть пути
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"

# Retry config: 3 попытки, экспоненциальная задержка, retry на 429 + 5xx
_RETRY_TOTAL = 3
_RETRY_BACKOFF = 1.0
_RETRY_STATUS = frozenset([429, 500, 502, 503, 504])

# Минимальная пауза при 429 если Retry-After не задан
_RATE_LIMIT_SLEEP_SEC = 2.0

# Regex E.164: +<1-15 digits>
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _is_valid_phone(number: str) -> bool:
    """Проверяет формат E.164."""
    return bool(_E164_RE.match(number or ""))


def _build_session() -> requests.Session:
    """Создаёт requests.Session с retry-адаптером."""
    session = requests.Session()
    retry = Retry(
        total=_RETRY_TOTAL,
        backoff_factor=_RETRY_BACKOFF,
        status_forcelist=list(_RETRY_STATUS),
        allowed_methods=["GET", "POST", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class TwilioError(Exception):
    """Базовый класс ошибок TwilioAdapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.code, "message": self.message}


class TwilioAdapter:
    """Клиент Twilio REST API для Krab Ear.

    При пустых account_sid / auth_token работает в stub-режиме: все методы
    возвращают {"ok": False, "error": "twilio_not_configured"}.

    Соответствует интерфейсу CallProvider (call_provider.py).
    """

    def __init__(
        self,
        account_sid: str = "",
        auth_token: str = "",
        from_number: str = "",
    ) -> None:
        self._account_sid = (account_sid or "").strip()
        self._auth_token = (auth_token or "").strip()
        self._from_number = (from_number or "").strip()
        self._session: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _configured(self) -> bool:
        return bool(self._account_sid and self._auth_token)

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = _build_session()
            self._session.headers.update(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                }
            )
        return self._session

    def _auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self._account_sid, self._auth_token)

    def _base_url(self) -> str:
        return f"{TWILIO_API_BASE}/{self._account_sid}"

    def _not_configured_result(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": "twilio_not_configured",
            "message": "Twilio не сконфигурирован: задайте TWILIO_ACCOUNT_SID и TWILIO_AUTH_TOKEN",
        }

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST запрос к Twilio API (form-encoded). Возвращает {"ok": True/False, ...}."""
        url = f"{self._base_url()}{path}"
        try:
            resp = self._get_session().post(
                url, data=payload, auth=self._auth(), timeout=10.0
            )
            return self._handle_response(resp)
        except requests.exceptions.RequestException as exc:
            logger.error("Twilio POST %s network error: %s", path, exc)
            return {"ok": False, "error": "network_error", "message": str(exc)}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET запрос к Twilio API."""
        url = f"{self._base_url()}{path}"
        try:
            resp = self._get_session().get(
                url, params=params or {}, auth=self._auth(), timeout=10.0
            )
            return self._handle_response(resp)
        except requests.exceptions.RequestException as exc:
            logger.error("Twilio GET %s network error: %s", path, exc)
            return {"ok": False, "error": "network_error", "message": str(exc)}

    def _handle_response(self, resp: requests.Response) -> Dict[str, Any]:
        """Разбирает HTTP-ответ Twilio в унифицированный dict."""
        status = resp.status_code

        # Успешные ответы
        if status in (200, 201, 202, 204):
            if status == 204 or not resp.content:
                return {"ok": True, "data": {}, "status": status}
            try:
                data = resp.json()
            except ValueError:
                data = {}
            return {"ok": True, "data": data, "status": status}

        # 400 — Validation / bad request
        if status == 400:
            try:
                body = resp.json()
                detail = body.get("message", resp.text)
                code = body.get("code", "")
            except ValueError:
                detail = resp.text or "Bad request"
                code = ""
            return {
                "ok": False,
                "error": "validation_error",
                "message": detail,
                "twilio_code": code,
                "status": status,
            }

        # 401 — Неверный auth
        if status == 401:
            return {
                "ok": False,
                "error": "unauthorized",
                "message": "Неверные TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN",
                "status": status,
            }

        # 402 — Нет средств (Twilio-специфика: insufficient_funds code 20003)
        if status == 402:
            return {
                "ok": False,
                "error": "insufficient_balance",
                "message": "Недостаточно средств на счёте Twilio",
                "status": status,
            }

        # 429 — Rate limit
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
            logger.warning("Twilio rate limit hit, waiting %.1fs", wait)
            time.sleep(wait)
            return {
                "ok": False,
                "error": "rate_limit",
                "message": "Превышен лимит запросов Twilio",
                "status": status,
                "retry_after": wait,
            }

        # 404 — Звонок не найден
        if status == 404:
            return {
                "ok": False,
                "error": "not_found",
                "message": "Звонок не найден",
                "status": status,
            }

        # Прочие ошибки
        try:
            body = resp.json()
            detail = body.get("message", str(body))
            twilio_code = body.get("code", "")
        except ValueError:
            detail = resp.text or f"HTTP {status}"
            twilio_code = ""

        logger.error("Twilio API error %s: %s", status, detail)
        return {
            "ok": False,
            "error": f"http_{status}",
            "message": detail,
            "twilio_code": twilio_code,
            "status": status,
        }

    # ------------------------------------------------------------------
    # Public API — CallProvider interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Возвращает True если Twilio credentials заданы."""
        return self._configured

    def dial(
        self,
        to_number: str,
        call_control_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Инициирует исходящий вызов через Twilio REST API.

        Args:
            to_number: Номер назначения в формате E.164.
            call_control_id: Игнорируется (Twilio не использует pre-assigned IDs).
            webhook_url: URL для StatusCallback (Twilio call events).

        Returns:
            {"ok": True, "call_id": str, "call_control_id": str, "to_number": str, "data": dict}
            {"ok": False, "error": str, "message": str}
        """
        if not self._configured:
            return self._not_configured_result()

        if not _is_valid_phone(to_number):
            return {
                "ok": False,
                "error": "invalid_phone_number",
                "message": f"Номер '{to_number}' не соответствует формату E.164",
            }

        payload: Dict[str, Any] = {
            "To": to_number,
            "From": self._from_number,
            # Twilio требует Url или Twiml для описания звонка; минимальный TwiML
            "Twiml": "<Response><Say>Connected</Say></Response>",
        }
        if webhook_url:
            payload["StatusCallback"] = webhook_url
            payload["StatusCallbackMethod"] = "POST"

        result = self._post("/Calls.json", payload)
        if not result["ok"]:
            return result

        data = result.get("data", {})
        # Twilio возвращает sid как идентификатор звонка
        call_sid = data.get("sid", "")

        logger.info("Twilio call initiated: sid=%s to=%s", call_sid, to_number)
        return {
            "ok": True,
            "call_id": call_sid,
            # Для совместимости с CallProvider interface используем sid как call_control_id
            "call_control_id": call_sid,
            "to_number": to_number,
            "data": data,
        }

    def hangup(self, call_control_id: str) -> Dict[str, Any]:
        """Завершает звонок. call_control_id — это Twilio Call SID (CAXXXXXXXX…).

        Returns:
            {"ok": True} при успехе.
            {"ok": False, "error": str} при ошибке.
        """
        if not self._configured:
            return self._not_configured_result()

        if not call_control_id:
            return {"ok": False, "error": "missing_call_control_id"}

        # Twilio завершает звонок через POST с Status=completed
        result = self._post(
            f"/Calls/{call_control_id}.json",
            {"Status": "completed"},
        )
        if result["ok"]:
            logger.info("Twilio hangup sent: sid=%s", call_control_id)
            return {"ok": True}
        return result

    def get_call_status(self, call_control_id: str) -> Dict[str, Any]:
        """Возвращает статус звонка по Twilio Call SID.

        Returns:
            {"ok": True, "status": str, "data": dict}
            {"ok": False, "error": str}
        """
        if not self._configured:
            return self._not_configured_result()

        if not call_control_id:
            return {"ok": False, "error": "missing_call_control_id"}

        result = self._get(f"/Calls/{call_control_id}.json")
        if not result["ok"]:
            return result

        data = result.get("data", {})
        # Twilio статусы: queued, ringing, in-progress, completed, busy, failed, no-answer
        status = data.get("status", "unknown")
        return {"ok": True, "status": status, "data": data}

    def list_active_calls(self) -> Dict[str, Any]:
        """Возвращает список активных звонков (status=in-progress).

        Returns:
            {"ok": True, "calls": [...]}
        """
        if not self._configured:
            return self._not_configured_result()

        result = self._get(
            "/Calls.json",
            params={"Status": "in-progress", "PageSize": 50},
        )
        if not result["ok"]:
            return result

        data = result.get("data", {})
        raw_calls = data.get("calls", []) if isinstance(data, dict) else []

        calls: List[Dict[str, Any]] = []
        for item in raw_calls:
            duration_str = item.get("duration") or "0"
            try:
                duration_sec = int(duration_str)
            except (ValueError, TypeError):
                duration_sec = 0

            calls.append(
                {
                    "id": item.get("sid", ""),
                    "call_control_id": item.get("sid", ""),
                    "to_number": item.get("to", ""),
                    "from_number": item.get("from", ""),
                    "duration_sec": duration_sec,
                    "status": item.get("status", "unknown"),
                }
            )

        return {"ok": True, "calls": calls}
