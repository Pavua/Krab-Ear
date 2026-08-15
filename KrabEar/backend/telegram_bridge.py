"""Telegram Bridge — мост Krab Ear → main Krab userbot.

Отправляет сообщения через HTTP API main Krab web-панели (`POST /api/notify`).

Endpoint discovery:
  Основной Krab запускает FastAPI web-панель на порту WEB_PORT (default 8080).
  Endpoint `POST /api/notify` принимает {text, chat_id} и делегирует отправку
  pyrogram-клиенту userbot. Вызов localhost-only, без авторизации.

TODO (integration):
  Если main Krab добавит поле `reply_to_message_id` в /api/notify — обновить
  _build_payload() и убрать TODO-комментарий.
  Текущая реализация передаёт reply_to в поле запроса, но серверная сторона
  его игнорирует до момента явной поддержки.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _panel_auth_headers() -> dict[str, str]:
    """W-r22: панель Krab гейтит /api/* ключом WEB_API_KEY.

    Ключ приходит через env ``KRAB_WEB_KEY`` (plist EnvironmentVariables
    ai.krab.ear.backend). Пустой env → пустые headers → поведение как раньше
    (безвредно и для негейтнутых эндпоинтов). Значение НЕ логируем.
    """
    key = (os.environ.get("KRAB_WEB_KEY") or "").strip()
    return {"X-Krab-Web-Key": key} if key else {}


class CircuitBreakerOpen(Exception):
    """Выбрасывается, когда circuit breaker разомкнут (слишком много ошибок)."""


class TelegramBridge:
    """HTTP-клиент для отправки сообщений через main Krab userbot.

    Параметры
    ----------
    base_url:
        Базовый URL web-панели Krab (default: ``http://localhost:8080``).
    timeout_sec:
        Таймаут HTTP-запроса в секундах.
    circuit_fail_threshold:
        Число последовательных ошибок перед размыканием circuit breaker.
    circuit_reset_sec:
        Время (секунды), после которого circuit breaker снова закрывается.
    """

    NOTIFY_PATH = "/api/notify"
    CHATS_PATH = "/api/chats"

    # Allowlist of hostnames that the bridge may connect to.
    # Prevents SSRF via KRAB_EAR_TELEGRAM_BRIDGE_URL env-var override.
    _ALLOWED_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
    # IPv4-mapped IPv6 forms also allowed (e.g. ::ffff:127.0.0.1) — checked via
    # ipaddress.is_loopback in _is_loopback_host() to avoid string-comparison gaps.

    @staticmethod
    def _is_loopback_host(hostname: str) -> bool:
        """Return True if *hostname* resolves to a loopback address.

        Covers: 'localhost', '127.x.x.x', '::1', '::ffff:127.x.x.x'
        (IPv4-mapped IPv6 loopback).  String comparison alone misses the last
        form and would allow SSRF via crafted IPv4-mapped addresses.
        """
        import ipaddress as _ip
        if hostname == "localhost":
            return True
        try:
            return _ip.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        timeout_sec: float = 5.0,
        circuit_fail_threshold: int = 3,
        circuit_reset_sec: float = 60.0,
    ) -> None:
        # Hostname allowlist guard — reject non-localhost targets at construction
        # time so that a bad KRAB_EAR_TELEGRAM_BRIDGE_URL cannot be used for SSRF
        # (e.g. 0.0.0.0, 169.254.x.x link-local, private LAN IPs).
        _parsed = urlparse(base_url)
        if not self._is_loopback_host(_parsed.hostname or ""):
            raise ValueError(
                f"telegram_bridge: refusing non-localhost base_url {base_url!r}; "
                f"hostname {_parsed.hostname!r} is not a loopback address"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._circuit_fail_threshold = circuit_fail_threshold
        self._circuit_reset_sec = circuit_reset_sec

        self._lock = threading.Lock()
        self._fail_count: int = 0
        self._open_at: float | None = None  # monotonic ts когда CB открылся

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(
        self,
        text: str,
        chat_id: int | str,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        """Отправить сообщение через main Krab userbot.

        Параметры
        ----------
        text:
            Текст сообщения. Не может быть пустым.
        chat_id:
            ID или username чата Telegram (``int`` или ``str``).
        reply_to:
            ID сообщения для цитирования (``reply_to_message_id``).
            Передаётся серверной стороне; поддержка на стороне Krab — TODO.

        Возвращает
        ----------
        dict с ключами ``message_id``, ``sent_at``, ``chat_title``.

        Исключения
        ----------
        ValueError
            Если ``text`` пустой.
        CircuitBreakerOpen
            Если circuit breaker разомкнут.
        requests.ConnectionError / requests.Timeout
            Если main Krab недоступен.
        RuntimeError
            Если Krab ответил с HTTP-ошибкой (userbot_not_ready, etc.).
        """
        if not text or not text.strip():
            raise ValueError("text не может быть пустым")

        _TELEGRAM_MAX_CHARS = 4096
        if len(text) > _TELEGRAM_MAX_CHARS:
            text = text[: _TELEGRAM_MAX_CHARS - 3] + "..."
            logger.warning(
                "TelegramBridge.send_message: текст обрезан до %d символов",
                _TELEGRAM_MAX_CHARS,
            )

        self._check_circuit()

        payload = self._build_payload(text=text, chat_id=chat_id, reply_to=reply_to)
        url = self._base_url + self.NOTIFY_PATH

        try:
            # allow_redirects=False: запрещаем следование 3xx-редиректам — allowlist
            # проверяется только на base_url при конструировании, а Location-заголовок
            # редиректа может указывать на любой хост (169.254.169.254 и т.д.).
            resp = requests.post(
                url,
                json=payload,
                headers=_panel_auth_headers(),
                timeout=self._timeout_sec,
                allow_redirects=False,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._record_failure()
            logger.warning("TelegramBridge: Krab недоступен: %s", exc)
            raise

        if not resp.ok:
            self._record_failure()
            detail = self._extract_detail(resp)
            code = "krab_unavailable" if resp.status_code == 503 else "krab_error"
            raise RuntimeError(f"{code}: {detail}")

        self._record_success()
        data = resp.json()
        # Fix 4: Main Krab's /api/notify currently returns only {"ok": True, "chat_id": ...}.
        # The fields below are best-effort / forward-compat for when Main Krab adds them.
        # message_id=None and chat_title=str(chat_id) are graceful fallbacks; sent_at
        # falls back to time.time() so callers always receive a numeric timestamp.
        return {
            "message_id": data.get("message_id"),
            "sent_at": data.get("sent_at") or time.time(),
            "chat_title": data.get("chat_title") or str(chat_id),
        }

    def get_chats(self) -> list[dict[str, Any]]:
        """Получить список доступных чатов через main Krab userbot.

        Возвращает
        ----------
        Список словарей с ключами ``id``, ``title``, ``type``.

        Исключения
        ----------
        CircuitBreakerOpen
            Если circuit breaker разомкнут.
        requests.ConnectionError / requests.Timeout
            Если main Krab недоступен.
        RuntimeError
            Если Krab ответил с HTTP-ошибкой.
        """
        self._check_circuit()

        url = self._base_url + self.CHATS_PATH

        try:
            # allow_redirects=False: аналогично send_message — Location редиректа
            # не проходит через _ALLOWED_HOSTS allowlist.
            resp = requests.get(
                url,
                headers=_panel_auth_headers(),
                timeout=self._timeout_sec,
                allow_redirects=False,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._record_failure()
            logger.warning("TelegramBridge.get_chats: Krab недоступен: %s", exc)
            raise

        if not resp.ok:
            self._record_failure()
            detail = self._extract_detail(resp)
            code = "krab_unavailable" if resp.status_code == 503 else "krab_error"
            raise RuntimeError(f"{code}: {detail}")

        self._record_success()
        data = resp.json()
        chats = data.get("chats") or []
        result: list[dict[str, Any]] = []
        for chat in chats:
            result.append(
                {
                    "id": chat.get("id"),
                    "title": chat.get("title") or str(chat.get("id", "")),
                    "type": chat.get("type") or "unknown",
                }
            )
        return result

    @property
    def is_circuit_open(self) -> bool:
        """True если circuit breaker разомкнут."""
        with self._lock:
            return self._is_open()

    def reset_circuit(self) -> None:
        """Принудительно закрыть circuit breaker (для тестов/диагностики)."""
        with self._lock:
            self._fail_count = 0
            self._open_at = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        text: str,
        chat_id: int | str,
        reply_to: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "chat_id": str(chat_id)}
        if reply_to is not None:
            # TODO: поддержать reply_to_message_id на стороне /api/notify в main Krab
            payload["reply_to_message_id"] = reply_to
        return payload

    def _check_circuit(self) -> None:
        with self._lock:
            if self._is_open():
                elapsed = time.monotonic() - self._open_at  # type: ignore[operator]
                raise CircuitBreakerOpen(
                    f"Circuit breaker разомкнут. Повторить через "
                    f"{max(0.0, self._circuit_reset_sec - elapsed):.0f}s"
                )

    def _is_open(self) -> bool:
        """Вызывать под self._lock."""
        if self._open_at is None:
            return False
        elapsed = time.monotonic() - self._open_at
        if elapsed >= self._circuit_reset_sec:
            # Half-open: сбрасываем и разрешаем один пробный запрос
            self._fail_count = 0
            self._open_at = None
            return False
        return True

    def _record_failure(self) -> None:
        with self._lock:
            self._fail_count += 1
            if self._fail_count >= self._circuit_fail_threshold:
                if self._open_at is None:
                    self._open_at = time.monotonic()
                    logger.warning(
                        "TelegramBridge: circuit breaker открылся после %d ошибок",
                        self._fail_count,
                    )

    def _record_success(self) -> None:
        with self._lock:
            self._fail_count = 0
            self._open_at = None

    @staticmethod
    def _extract_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
            return str(body.get("detail") or body)
        except Exception:
            return resp.text[:200] if resp.text else str(resp.status_code)
