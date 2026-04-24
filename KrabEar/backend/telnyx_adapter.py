"""Telnyx REST API adapter — Krab Ear Phase 3 outbound calls.

Прямой fallback к Telnyx Call Control API (без FreeSWITCH).
Используется когда TELNYX_API_KEY задан в настройках; иначе — stub-режим.

Все HTTP-запросы выполняются через requests.Session с Bearer-аутентификацией
и троекратным повтором с экспоненциальной задержкой.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("KrabEar.Backend.TelnyxAdapter")

# Telnyx Call Control API v2
TELNYX_API_BASE = "https://api.telnyx.com/v2"

# Retry config: 3 attempts, exp backoff 1s/2s/4s, retry on 429 + 5xx
_RETRY_TOTAL = 3
_RETRY_BACKOFF = 1.0  # seconds; urllib3 uses backoff_factor × (2 ** (attempt - 1))
_RETRY_STATUS = frozenset([429, 500, 502, 503, 504])

# Минимальная пауза между попытками при 429 (если Retry-After не указан)
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
        allowed_methods=["GET", "POST", "DELETE", "PATCH"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class TelnyxError(Exception):
    """Базовый класс ошибок TelnyxAdapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {"error": self.code, "message": self.message}


class TelnyxAdapter:
    """Клиент Telnyx Call Control API для Krab Ear.

    При пустом api_key работает в stub-режиме: все методы возвращают
    {"ok": False, "error": "telnyx_not_configured"}.
    """

    def __init__(
        self,
        api_key: str = "",
        connection_id: str = "",
        from_number: str = "",
        api_base: str = TELNYX_API_BASE,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._connection_id = (connection_id or "").strip()
        self._from_number = (from_number or "").strip()
        self._api_base = api_base.rstrip("/")
        self._session: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _configured(self) -> bool:
        return bool(self._api_key)

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = _build_session()
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
        return self._session

    def _not_configured_result(self) -> Dict[str, Any]:
        return {"ok": False, "error": "telnyx_not_configured"}

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST запрос к Telnyx API. Возвращает {"ok": True/False, ...}."""
        url = f"{self._api_base}{path}"
        try:
            resp = self._get_session().post(url, json=payload, timeout=10.0)
            return self._handle_response(resp)
        except requests.exceptions.RequestException as exc:
            logger.error("Telnyx POST %s network error: %s", path, exc)
            return {"ok": False, "error": "network_error", "message": str(exc)}

    def _get(self, path: str) -> Dict[str, Any]:
        """GET запрос к Telnyx API."""
        url = f"{self._api_base}{path}"
        try:
            resp = self._get_session().get(url, timeout=10.0)
            return self._handle_response(resp)
        except requests.exceptions.RequestException as exc:
            logger.error("Telnyx GET %s network error: %s", path, exc)
            return {"ok": False, "error": "network_error", "message": str(exc)}

    def _delete(self, path: str) -> Dict[str, Any]:
        """DELETE запрос к Telnyx API."""
        url = f"{self._api_base}{path}"
        try:
            resp = self._get_session().delete(url, timeout=10.0)
            return self._handle_response(resp)
        except requests.exceptions.RequestException as exc:
            logger.error("Telnyx DELETE %s network error: %s", path, exc)
            return {"ok": False, "error": "network_error", "message": str(exc)}

    def _handle_response(self, resp: requests.Response) -> Dict[str, Any]:
        """Разбирает HTTP-ответ Telnyx в унифицированный dict."""
        status = resp.status_code

        # Успешные ответы
        if status in (200, 201, 202):
            try:
                body = resp.json()
            except ValueError:
                body = {}
            data = body.get("data", body)
            return {"ok": True, "data": data, "status": status}

        # Особые коды ошибок
        if status == 402:
            return {
                "ok": False,
                "error": "insufficient_balance",
                "message": "Недостаточно средств на счёте Telnyx",
                "status": status,
            }
        if status == 422:
            return {
                "ok": False,
                "error": "unreachable_number",
                "message": "Номер недостижим или некорректен",
                "status": status,
            }
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
            logger.warning("Telnyx rate limit hit, waiting %.1fs", wait)
            time.sleep(wait)
            return {
                "ok": False,
                "error": "rate_limit",
                "message": "Превышен лимит запросов Telnyx",
                "status": status,
                "retry_after": wait,
            }
        if status == 401:
            return {
                "ok": False,
                "error": "unauthorized",
                "message": "Неверный или отозванный TELNYX_API_KEY",
                "status": status,
            }

        # Прочие ошибки
        try:
            body = resp.json()
            errors = body.get("errors", [])
            detail = errors[0].get("detail", str(errors)) if errors else resp.text
        except ValueError:
            detail = resp.text or f"HTTP {status}"

        logger.error("Telnyx API error %s: %s", status, detail)
        return {
            "ok": False,
            "error": f"http_{status}",
            "message": detail,
            "status": status,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dial(
        self,
        to_number: str,
        call_control_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Инициирует исходящий вызов через Telnyx Call Control API.

        Returns:
            {"ok": True, "call_id": str, "call_control_id": str} при успехе.
            {"ok": False, "error": str, "message": str} при ошибке.
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
            "to": to_number,
            "from": self._from_number,
            "connection_id": self._connection_id,
        }
        if call_control_id:
            payload["call_control_id"] = call_control_id
        if webhook_url:
            payload["webhook_url"] = webhook_url

        result = self._post("/calls", payload)
        if not result["ok"]:
            return result

        data = result.get("data", {})
        call_id = data.get("call_leg_id") or data.get("id", "")
        ctrl_id = data.get("call_control_id", "")

        logger.info("Telnyx call initiated: call_id=%s to=%s", call_id, to_number)
        return {
            "ok": True,
            "call_id": call_id,
            "call_control_id": ctrl_id,
            "to_number": to_number,
            "data": data,
        }

    def hangup(self, call_control_id: str) -> Dict[str, Any]:
        """Завершает звонок по call_control_id.

        Returns:
            {"ok": True} при успехе.
            {"ok": False, "error": str} при ошибке.
        """
        if not self._configured:
            return self._not_configured_result()

        if not call_control_id:
            return {"ok": False, "error": "missing_call_control_id"}

        result = self._post(
            f"/calls/{call_control_id}/actions/hangup",
            {},
        )
        if result["ok"]:
            logger.info("Telnyx hangup sent: call_control_id=%s", call_control_id)
        return result

    def get_call_status(self, call_control_id: str) -> Dict[str, Any]:
        """Возвращает статус звонка: ringing/answered/bridged/hangup/etc.

        Returns:
            {"ok": True, "status": str, "data": dict} при успехе.
            {"ok": False, "error": str} при ошибке.
        """
        if not self._configured:
            return self._not_configured_result()

        if not call_control_id:
            return {"ok": False, "error": "missing_call_control_id"}

        result = self._get(f"/calls/{call_control_id}")
        if not result["ok"]:
            return result

        data = result.get("data", {})
        status = data.get("status", "unknown")
        return {"ok": True, "status": status, "data": data}

    def list_active_calls(self) -> Dict[str, Any]:
        """Возвращает список активных звонков на connection_id.

        Returns:
            {"ok": True, "calls": [{"id": str, "to_number": str,
             "duration_sec": int, "status": str}, ...]}
        """
        if not self._configured:
            return self._not_configured_result()

        path = "/calls"
        if self._connection_id:
            path = f"/calls?filter[connection_id]={self._connection_id}"

        result = self._get(path)
        if not result["ok"]:
            return result

        raw_data = result.get("data", [])
        if not isinstance(raw_data, list):
            raw_data = [raw_data] if raw_data else []

        calls: List[Dict[str, Any]] = []
        for item in raw_data:
            start_time = item.get("start_time")
            duration_sec = 0
            if start_time:
                try:
                    # Telnyx возвращает ISO8601 timestamp
                    from datetime import datetime, timezone
                    started = datetime.fromisoformat(
                        start_time.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)
                    duration_sec = max(0, int((now - started).total_seconds()))
                except (ValueError, TypeError):
                    pass

            calls.append(
                {
                    "id": item.get("call_leg_id") or item.get("id", ""),
                    "call_control_id": item.get("call_control_id", ""),
                    "to_number": item.get("to", ""),
                    "from_number": item.get("from", ""),
                    "duration_sec": duration_sec,
                    "status": item.get("status", "unknown"),
                }
            )

        return {"ok": True, "calls": calls}
