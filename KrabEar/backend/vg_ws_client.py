"""WebSocket-клиент для Voice Gateway.

Подключается к VG session stream, пробрасывает события
в Krab Ear EventBus для Swift-агента (через SSE /v1/events).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl

import websockets

from backend.event_bus import bus
from contracts.registry import EVENT_SCHEMA_MAP

logger = logging.getLogger("KrabEar.VGClient")

_RECONNECT_BASE_SEC = 1.0
_RECONNECT_MAX_SEC = 10.0

# W1204 F1: explicit handshake timeout constant
_VG_WS_DEFAULT_TIMEOUT_SEC = 30

# W1204 F2: allowed characters in session_id (alphanumeric, underscore, dash; 1-128 chars)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# W1204 F4: cap incoming WS message size to prevent rogue-gateway DoS
_VG_WS_MAX_SIZE = 2 * 1024 * 1024  # 2 MiB


class VGWebSocketClient:
    """Клиент к Voice Gateway WebSocket stream."""

    def __init__(self, gateway_url: str, session_id: str, api_key: str = ""):
        # W1204 F2: validate session_id before embedding in URL path
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"Invalid session_id {session_id!r}: must match ^[A-Za-z0-9_-]{{1,128}}$"
            )
        ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{ws_base.rstrip('/')}/v1/sessions/{session_id}/stream"
        self.api_key = api_key
        self.session_id = session_id
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Основной цикл: подключение + проброс событий в EventBus."""
        backoff = _RECONNECT_BASE_SEC
        # W1204 F1: build explicit SSL context — certificate verification required
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = True
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED
        # Use ssl=None for plain ws:// URLs so we don't force TLS on non-wss
        _ssl_arg = ssl_ctx if self.ws_url.startswith("wss://") else None
        while not self._stop.is_set():
            try:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with websockets.connect(
                    self.ws_url,
                    extra_headers=headers,
                    ssl=_ssl_arg,
                    open_timeout=_VG_WS_DEFAULT_TIMEOUT_SEC,
                    max_size=_VG_WS_MAX_SIZE,
                ) as ws:
                    logger.info("VG WS connected: %s", self.ws_url)
                    backoff = _RECONNECT_BASE_SEC
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            event = json.loads(raw)
                            event_type = event.get("type", "unknown")
                            event_data = event.get("data", {})
                            schema_cls = EVENT_SCHEMA_MAP.get(event_type)
                            if schema_cls:
                                try:
                                    schema_cls.model_validate(event_data)
                                except Exception as e:
                                    logger.warning("VG event %s failed contract validation: %s", event_type, e)
                            bus.emit(event_type, event_data)
                        except (json.JSONDecodeError, TypeError) as parse_err:
                            logger.warning("VG WS bad message: %s", parse_err)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("VG WS disconnected (%s), reconnect in %.0fs", exc, backoff)
                self._push_error(
                    "vgw.reconnect",
                    f"{type(exc).__name__}: {exc} (reconnect in {backoff:.0f}s)",
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_SEC)

        logger.info("VG WS client stopped for session %s", self.session_id)

    def stop(self) -> None:
        """Сигнал остановки. Безопасно вызывать из другого потока."""
        self._stop.set()

    def _push_error(self, code: str, message_debug: str) -> None:
        """Push KrabError to late-injected ErrorBus. Never raises."""
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code, {})
            err = KrabError(
                severity=entry.get("severity", "warn"),
                component="vgw",
                code=code,
                message_user=entry.get("user_msg_ru", "VGW ошибка"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"session_id": self.session_id, "ws_url": self.ws_url},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception as e:  # noqa: BLE001
            # Wave 222: surface push failures to Sentry instead of silent swallow
            try:
                from backend.observability import capture_exception
                capture_exception(e, "_push_error_internal")
            except Exception:
                pass  # Sentry itself failing — stay silent
            logger.exception("error_bus.push failed for code=%s", code)
