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
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# F3 (W1195): SSRF guard — reuse WebhookManager's validated URL checker.
from backend.webhook_manager import _is_safe_webhook_url  # noqa: PLC2701
# F8 (W1766): маскировка номера телефона в логах — предотвращает утечку PII.
from backend.observability import mask_phone

logger = logging.getLogger("KrabEar.Backend.TelnyxAdapter")

# Telnyx Call Control API v2
TELNYX_API_BASE = "https://api.telnyx.com/v2"

# Retry config: 3 attempts, exp backoff 1s/2s/4s, retry on 5xx only.
# 429 is excluded from urllib3 retry list to avoid double-sleep with our own
# Retry-After handling in _handle_response (F1 fix — W1195).
_RETRY_TOTAL = 3
_RETRY_BACKOFF = 1.0  # seconds; urllib3 uses backoff_factor × (2 ** (attempt - 1))
_RETRY_STATUS = frozenset([500, 502, 503, 504])

# Maximum seconds we will sleep on a Telnyx 429 Retry-After header.
# Prevents a MITM or misbehaving server from blocking the IPC thread forever.
_RETRY_AFTER_MAX_SEC = 60.0

# Минимальная пауза между попытками при 429 (если Retry-After не указан)
_RATE_LIMIT_SLEEP_SEC = 2.0

# Regex E.164: +<1-15 digits>
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

# F2 (W1195): call_control_id regex — alphanumeric + hyphen/underscore, 1–128 chars.
# Rejects path-traversal payloads like "../../other-resource".
_CALL_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# W1748: connection_id — Telnyx connection IDs are pure numeric strings.
# Validated before embedding in URL query parameters to prevent query injection.
# An empty/missing connection_id is also handled — caller skips the filter entirely.
_CONNECTION_ID_RE = re.compile(r"^\d{1,64}$")


def _is_valid_phone(number: str) -> bool:
    """Проверяет формат E.164."""
    return bool(_E164_RE.match(number or ""))


def _is_valid_call_control_id(value: str) -> bool:
    """Проверяет call_control_id на допустимые символы (предотвращает path traversal)."""
    return bool(_CALL_CONTROL_ID_RE.match(value or ""))


def _is_valid_connection_id(value: str) -> bool:
    """Проверяет connection_id — должен быть числовой строкой (Telnyx format).

    Пустая строка считается валидной (означает «фильтр не задан»).
    """
    if not value:
        return True  # empty = не применяем фильтр, а не ошибка
    return bool(_CONNECTION_ID_RE.match(value))


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
            # F1 (W1195): cap wait to _RETRY_AFTER_MAX_SEC to prevent an
            # unbounded sleep caused by a huge or malicious Retry-After value.
            try:
                raw_wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
            except (ValueError, TypeError):
                raw_wait = _RATE_LIMIT_SLEEP_SEC
            wait = max(0.0, min(raw_wait, _RETRY_AFTER_MAX_SEC))
            logger.warning("Telnyx rate limit hit, waiting %.1fs (capped at %.0fs)", wait, _RETRY_AFTER_MAX_SEC)
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
    # Public API — CallProvider interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Возвращает True если Telnyx API key задан."""
        return self._configured

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
                # F8 (W1766): маскируем номер в сообщении — значение видно в логах вызывающего кода.
                "message": f"Номер '{mask_phone(to_number)}' не соответствует формату E.164",
            }

        # W1748: validate from_number (E.164) before sending to Telnyx.
        if self._from_number and not _is_valid_phone(self._from_number):
            return {
                "ok": False,
                "error": "invalid_from_number",
                # F8 (W1766): маскируем номер отправителя в сообщении.
                "message": f"Номер отправителя '{mask_phone(self._from_number)}' не соответствует формату E.164",
            }

        payload: Dict[str, Any] = {
            "to": to_number,
            "from": self._from_number,
            "connection_id": self._connection_id,
        }
        if call_control_id:
            payload["call_control_id"] = call_control_id
        if webhook_url:
            # F3 (W1195): validate webhook_url against SSRF check before forwarding
            # to Telnyx, preventing internal service exposure via callback URL.
            safe, reject_reason = _is_safe_webhook_url(webhook_url)
            if not safe:
                logger.warning("Telnyx dial rejected webhook_url: %s (%s)", webhook_url, reject_reason)
                return {
                    "ok": False,
                    "error": "unsafe_webhook_url",
                    "message": f"webhook_url отклонён защитой SSRF: {reject_reason}",
                }
            payload["webhook_url"] = webhook_url

        result = self._post("/calls", payload)
        if not result["ok"]:
            return result

        data = result.get("data", {})
        call_id = data.get("call_leg_id") or data.get("id", "")
        ctrl_id = data.get("call_control_id", "")

        logger.info("Telnyx call initiated: call_id=%s to=%s", call_id, mask_phone(to_number))
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

        # F2 (W1195): reject call_control_id values that could enable path traversal.
        if not _is_valid_call_control_id(call_control_id):
            logger.warning("Telnyx hangup rejected: invalid call_control_id %r", call_control_id)
            return {"ok": False, "error": "invalid_call_control_id",
                    "message": "call_control_id содержит недопустимые символы"}

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

        # F2 (W1195): reject call_control_id values that could enable path traversal.
        if not _is_valid_call_control_id(call_control_id):
            logger.warning("Telnyx get_call_status rejected: invalid call_control_id %r", call_control_id)
            return {"ok": False, "error": "invalid_call_control_id",
                    "message": "call_control_id содержит недопустимые символы"}

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

        # W1748: validate connection_id before embedding in URL query string.
        # An invalid/malicious connection_id (e.g. "123&evil=1") could manipulate
        # the Telnyx API request. urlencode() would encode it, but we reject non-
        # numeric values early so the caller gets a clear error, not a confusing 4xx.
        if self._connection_id and not _is_valid_connection_id(self._connection_id):
            logger.warning(
                "Telnyx list_active_calls rejected: invalid connection_id %r",
                self._connection_id,
            )
            return {
                "ok": False,
                "error": "invalid_connection_id",
                "message": "connection_id должен быть числовой строкой (Telnyx format)",
            }

        path = "/calls"
        if self._connection_id:
            # Use urlencode for safe query-string construction even though the
            # value is already validated to be numeric-only above.
            qs = urlencode({"filter[connection_id]": self._connection_id})
            path = f"/calls?{qs}"

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
