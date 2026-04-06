"""WebSocket-клиент для Voice Gateway.

Подключается к VG session stream, пробрасывает события
в Krab Ear EventBus для Swift-агента (через SSE /v1/events).
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from backend.event_bus import bus

logger = logging.getLogger("KrabEar.VGClient")

_RECONNECT_BASE_SEC = 1.0
_RECONNECT_MAX_SEC = 10.0


class VGWebSocketClient:
    """Клиент к Voice Gateway WebSocket stream."""

    def __init__(self, gateway_url: str, session_id: str, api_key: str = ""):
        ws_base = gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{ws_base.rstrip('/')}/v1/sessions/{session_id}/stream"
        self.api_key = api_key
        self.session_id = session_id
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Основной цикл: подключение + проброс событий в EventBus."""
        backoff = _RECONNECT_BASE_SEC
        while not self._stop.is_set():
            try:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    logger.info("VG WS connected: %s", self.ws_url)
                    backoff = _RECONNECT_BASE_SEC
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            event = json.loads(raw)
                            event_type = event.get("type", "unknown")
                            event_data = event.get("data", {})
                            bus.emit(event_type, event_data)
                        except (json.JSONDecodeError, TypeError) as parse_err:
                            logger.warning("VG WS bad message: %s", parse_err)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("VG WS disconnected (%s), reconnect in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_SEC)

        logger.info("VG WS client stopped for session %s", self.session_id)

    def stop(self) -> None:
        """Сигнал остановки. Безопасно вызывать из другого потока."""
        self._stop.set()
