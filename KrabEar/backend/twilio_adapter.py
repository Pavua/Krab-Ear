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

# F3 (W1203): SSRF guard — reuse WebhookManager's validated URL checker.
from backend.webhook_manager import _is_safe_webhook_url  # noqa: PLC2701

logger = logging.getLogger("KrabEar.Backend.TwilioAdapter")

# Twilio REST API base — включает Account SID как часть пути
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"

# Retry config: 3 попытки, экспоненциальная задержка, retry на 5xx only.
# 429 исключён из urllib3 retry list, чтобы избежать двойного sleep с нашим
# Retry-After обработчиком в _handle_response (F1 fix — W1203).
_RETRY_TOTAL = 3
_RETRY_BACKOFF = 1.0
_RETRY_STATUS = frozenset([500, 502, 503, 504])

# Maximum seconds we will sleep on a Twilio 429 Retry-After header.
# Fix 2 (LOW): потолок снижен с 60s до 5s, чтобы блокировка IPC-потока не превышала ~5с.
_RETRY_AFTER_MAX_SEC = 5.0

# Минимальная пауза при 429 если Retry-After не задан
_RATE_LIMIT_SLEEP_SEC = 2.0

# Maximum length for error detail forwarded to IPC / logs (F5 — W1203).
_ERROR_DETAIL_MAX_CHARS = 512

# Regex E.164: +<1-15 digits>
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

# F3-TW (W1203): Twilio Account SID — "AC" + 32 hex chars.
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")

# F2 (W1203): Twilio Call SID — "CA" + 32 hex chars.
_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")


def _is_valid_phone(number: str) -> bool:
    """Проверяет формат E.164."""
    return bool(_E164_RE.match(number or ""))


def _is_valid_account_sid(value: str) -> bool:
    """F3-TW (W1203): проверяет формат Twilio Account SID (AC + 32 hex)."""
    return bool(_ACCOUNT_SID_RE.match(value or ""))


def _is_valid_call_sid(value: str) -> bool:
    """F2 (W1203): проверяет формат Twilio Call SID (CA + 32 hex)."""
    return bool(_CALL_SID_RE.match(value or ""))


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
        sid = (account_sid or "").strip()
        # F3-TW (W1203): reject malformed Account SIDs at init to prevent path
        # traversal via _base_url() which embeds the SID in the URL path.
        # Empty string is allowed (stub mode); any non-empty value must match AC + 32 hex.
        if sid and not _is_valid_account_sid(sid):
            raise ValueError(
                f"account_sid не соответствует формату Twilio (AC + 32 hex): {sid!r}"
            )
        self._account_sid = sid
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
                url, data=payload, auth=self._auth(), timeout=10.0,
                allow_redirects=False,
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
                url, params=params or {}, auth=self._auth(), timeout=10.0,
                allow_redirects=False,
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
                # F5 (W1203): prefer errors[*].detail, truncate to 512 chars.
                errors = body.get("errors", [])
                if errors and isinstance(errors, list):
                    detail = errors[0].get("detail", str(errors[0]))
                else:
                    detail = body.get("message", resp.text or "Bad request")
                detail = str(detail)[:_ERROR_DETAIL_MAX_CHARS]
                code = body.get("code", "")
            except ValueError:
                detail = (resp.text or "Bad request")[:_ERROR_DETAIL_MAX_CHARS]
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
            # F1 (W1203): cap wait to _RETRY_AFTER_MAX_SEC to prevent an
            # unbounded sleep caused by a huge or malicious Retry-After value.
            try:
                raw_wait = float(retry_after) if retry_after else _RATE_LIMIT_SLEEP_SEC
            except (ValueError, TypeError):
                raw_wait = _RATE_LIMIT_SLEEP_SEC
            wait = max(0.0, min(raw_wait, _RETRY_AFTER_MAX_SEC))
            logger.warning(
                "Twilio rate limit hit, waiting %.1fs (capped at %.0fs)",
                wait, _RETRY_AFTER_MAX_SEC,
            )
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
            # F5 (W1203): prefer errors[*].detail; truncate to 512 chars before
            # forwarding to IPC / logs to prevent verbatim Twilio body leakage.
            errors = body.get("errors", [])
            if errors and isinstance(errors, list):
                detail = errors[0].get("detail", str(errors[0]))
            else:
                detail = body.get("message", str(body))
            detail = str(detail)[:_ERROR_DETAIL_MAX_CHARS]
            twilio_code = body.get("code", "")
        except ValueError:
            detail = (resp.text or f"HTTP {status}")[:_ERROR_DETAIL_MAX_CHARS]
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

        # `From` берётся из настроек (TWILIO_FROM_NUMBER через set_settings) и
        # раньше форвардился в payload без проверки — зеркалим guard для to_number,
        # иначе мусорный/адверсарный _from_number уходит в REST-запрос.
        if not _is_valid_phone(self._from_number):
            return {
                "ok": False,
                "error": "invalid_from_number",
                "message": "TWILIO_FROM_NUMBER не соответствует формату E.164",
            }

        payload: Dict[str, Any] = {
            "To": to_number,
            "From": self._from_number,
            # Twilio требует Url или Twiml для описания звонка; минимальный TwiML
            "Twiml": "<Response><Say>Connected</Say></Response>",
        }
        if webhook_url:
            # F3 (W1203): validate webhook_url against SSRF check before forwarding
            # to Twilio, preventing internal service exposure via callback URL.
            safe, reject_reason = _is_safe_webhook_url(webhook_url)
            if not safe:
                logger.warning(
                    "Twilio dial rejected webhook_url: %s (%s)", webhook_url, reject_reason
                )
                return {
                    "ok": False,
                    "error": "unsafe_webhook_url",
                    "message": f"webhook_url отклонён защитой SSRF: {reject_reason}",
                }
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

        # F2 (W1203): reject call_control_id values that are not valid Twilio Call SIDs
        # (CA + 32 hex) to prevent path traversal in URL interpolation.
        if not _is_valid_call_sid(call_control_id):
            logger.warning("Twilio hangup rejected: invalid call SID %r", call_control_id)
            return {
                "ok": False,
                "error": "invalid_call_control_id",
                "message": "call_control_id не соответствует формату Twilio Call SID (CA + 32 hex)",
            }

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

        # F2 (W1203): reject call_control_id values that are not valid Twilio Call SIDs.
        if not _is_valid_call_sid(call_control_id):
            logger.warning(
                "Twilio get_call_status rejected: invalid call SID %r", call_control_id
            )
            return {
                "ok": False,
                "error": "invalid_call_control_id",
                "message": "call_control_id не соответствует формату Twilio Call SID (CA + 32 hex)",
            }

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
