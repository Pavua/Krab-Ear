"""wave1775 — webhook fire wiring tests.

Регрессия: зарегистрированные webhook-и НИКОГДА не срабатывали — fire_webhook не
имел ни одного production-вызова (register/list/unregister были подключены, а сама
доставка — мертва). Эти тесты фиксируют, что:

  1. событие из allowlist (stt.final), эмитнутое на EventBus, ДОСТАВЛЯЕТСЯ
     зарегистрированному webhook-у (через реальный fire_webhook → _post_once);
  2. set_privacy_mode(True) ПОДАВЛЯЕТ доставку (privacy gate);
  3. webhook, зарегистрированный на ДРУГОЕ событие, НЕ срабатывает на stt.final;
  4. событие НЕ из allowlist (например, recording.audio_level @ 30 Гц) НЕ форвардится;
  5. payload PII-безопасен — текст транскрипта НЕ покидает устройство.

Сеть полностью замокана (_post_once / _deliver_with_retry) — реального HTTP нет.

Тестируется НАСТОЯЩИЙ production-код: реальный EventBus.add_listener/emit, реальный
BackendService._forward_event_to_webhooks (привязан к минимальному shim через
types.MethodType, т.к. метод трогает только self._webhook_manager + статический
self._webhook_safe_payload), реальный WebhookManager.fire_webhook.
"""

from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus  # noqa: E402
from backend.service import (  # noqa: E402
    BackendService,
    _WEBHOOK_FORWARDED_EVENTS,
    _WEBHOOK_PII_KEYS,
)
from backend.webhook_manager import WebhookManager  # noqa: E402


class _ForwarderShim:
    """Минимальный носитель _webhook_manager — _forward_event_to_webhooks трогает
    только self._webhook_manager и статический self._webhook_safe_payload."""

    # _webhook_safe_payload — @staticmethod на BackendService; присваиваем сам
    # production-объект функции как атрибут класса shim, чтобы self._webhook_safe_payload
    # внутри _forward_event_to_webhooks резолвился в НАСТОЯЩИЙ код (без привязки self).
    _webhook_safe_payload = staticmethod(BackendService._webhook_safe_payload)

    def __init__(self, webhook_manager: WebhookManager) -> None:
        self._webhook_manager = webhook_manager
        # Привязываем НАСТОЯЩИЙ production-метод _forward_event_to_webhooks к shim.
        self._forward_event_to_webhooks = types.MethodType(
            BackendService._forward_event_to_webhooks, self
        )


class WebhookFireWiringTestCase(unittest.TestCase):
    """EventBus → _forward_event_to_webhooks → WebhookManager.fire_webhook."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = WebhookManager(data_dir=self._tmpdir)
        self._bus = EventBus()
        self._shim = _ForwarderShim(self._mgr)
        # Точно так же, как BackendService.__init__ подключает форвардер.
        self._bus.add_listener(self._shim._forward_event_to_webhooks)

    def _emit_and_capture(self, event_type: str, payload: dict[str, Any]) -> list[Any]:
        """Эмитит событие, мокая _deliver_with_retry, возвращает захваченные вызовы.

        Возвращает список tuple-аргументов (webhook_id, url, secret, body, event_type, ...).
        """
        captured: list[Any] = []

        def _capture(*args: Any, **kwargs: Any) -> None:
            captured.append(args)

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=_capture):
            self._bus.emit(event_type, payload)
            # fire_webhook отправляет в ThreadPoolExecutor — даём воркерам отработать.
            time.sleep(0.1)
        return captured

    # 1 — событие из allowlist доставляется зарегистрированному webhook-у
    def test_stt_final_fires_registered_webhook(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        captured = self._emit_and_capture("stt.final", {"history_id": "h1", "text": "secret"})
        self.assertEqual(len(captured), 1, "stt.final должен доставиться webhook-у")

    # 2 — privacy mode подавляет доставку
    def test_privacy_mode_suppresses_fire(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        self._mgr.set_privacy_mode(True)
        captured = self._emit_and_capture("stt.final", {"history_id": "h1", "text": "x"})
        self.assertEqual(len(captured), 0, "при privacy mode доставка должна быть подавлена")
        # И обратно: отключение privacy mode возвращает доставку.
        self._mgr.set_privacy_mode(False)
        captured2 = self._emit_and_capture("stt.final", {"history_id": "h2"})
        self.assertEqual(len(captured2), 1, "после выключения privacy mode доставка возобновляется")

    # 3 — webhook на другое событие НЕ срабатывает на stt.final (per-webhook filter)
    def test_webhook_for_different_event_not_fired(self) -> None:
        self._mgr.register_webhook("https://example.com/other", events=["translation.completed"])
        captured = self._emit_and_capture("stt.final", {"history_id": "h1"})
        self.assertEqual(len(captured), 0, "webhook на translation.completed не должен ловить stt.final")

    # 3b — два webhook-а с разными фильтрами: каждый ловит только своё
    def test_two_webhooks_independent_filters(self) -> None:
        self._mgr.register_webhook("https://one.com/hook", events=["stt.final"])
        self._mgr.register_webhook("https://two.com/hook", events=["translation.completed"])
        urls: list[str] = []

        def _capture(webhook_id: str, url: str, *args: Any, **kwargs: Any) -> None:
            urls.append(url)

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=_capture):
            self._bus.emit("stt.final", {"history_id": "h1"})
            time.sleep(0.1)

        self.assertIn("https://one.com/hook", urls)
        self.assertNotIn("https://two.com/hook", urls)

    # 4 — событие НЕ из allowlist НЕ форвардится (даже если webhook принимает всё)
    def test_non_allowlisted_event_not_forwarded(self) -> None:
        # Пустой events = «все события» на уровне webhook-а, но allowlist форвардера
        # должен отсечь высокочастотное recording.audio_level ДО fire_webhook.
        self._mgr.register_webhook("https://example.com/hook", events=[])
        captured = self._emit_and_capture("recording.audio_level", {"rms": 0.1})
        self.assertEqual(len(captured), 0, "не-allowlist событие не должно форвардиться")
        # Контроль: allowlist-событие при том же пустом фильтре — доставляется.
        captured2 = self._emit_and_capture("translation.completed", {"history_id": "h3"})
        self.assertEqual(len(captured2), 1)

    # 5 — payload PII-безопасен: текст транскрипта НЕ уходит во внешний webhook
    def test_payload_strips_transcript_text(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        bodies: list[bytes] = []

        def _capture_post(url: str, body: bytes, secret: str, allow_local: bool = False) -> int:
            bodies.append(body)
            return 200

        with patch.object(self._mgr, "_post_once", side_effect=_capture_post):
            self._bus.emit(
                "stt.final",
                {
                    "history_id": "h1",
                    "text": "СУПЕР СЕКРЕТНЫЙ ТРАНСКРИПТ",
                    "translated_text": "super secret",
                    "segments": [{"text": "leak"}],
                    "duration_sec": 4.2,
                    "language": "ru",
                    "confidence": 0.9,
                },
            )
            time.sleep(0.1)

        self.assertEqual(len(bodies), 1)
        body_str = bodies[0].decode("utf-8")
        # PII-ключи вырезаны
        self.assertNotIn("СУПЕР СЕКРЕТНЫЙ ТРАНСКРИПТ", body_str)
        self.assertNotIn("super secret", body_str)
        self.assertNotIn("leak", body_str)
        for pii_key in ("text", "translated_text", "segments"):
            self.assertIn(pii_key, _WEBHOOK_PII_KEYS)
        # Метаданные сохранены
        self.assertIn("h1", body_str)
        self.assertIn("duration_sec", body_str)
        self.assertIn("ru", body_str)

    # 6 — статический хелпер _webhook_safe_payload вычищает только PII-ключи
    def test_webhook_safe_payload_helper(self) -> None:
        raw = {"history_id": "x", "text": "t", "duration_sec": 1.0, "language": "ru"}
        safe = BackendService._webhook_safe_payload(raw)
        self.assertNotIn("text", safe)
        self.assertEqual(safe["history_id"], "x")
        self.assertEqual(safe["duration_sec"], 1.0)
        self.assertEqual(safe["language"], "ru")

    # 7 — allowlist содержит ожидаемые lifecycle-события и НЕ содержит высокочастотных
    def test_allowlist_contents(self) -> None:
        self.assertIn("stt.final", _WEBHOOK_FORWARDED_EVENTS)
        self.assertIn("translation.completed", _WEBHOOK_FORWARDED_EVENTS)
        self.assertNotIn("recording.audio_level", _WEBHOOK_FORWARDED_EVENTS)
        self.assertNotIn("stt.partial", _WEBHOOK_FORWARDED_EVENTS)


class EventBusListenerTestCase(unittest.TestCase):
    """add_listener / emit fan-out — изоляция от webhook-специфики."""

    def test_listener_called_with_event_and_payload(self) -> None:
        bus = EventBus()
        received: list[tuple[str, dict[str, Any]]] = []
        bus.add_listener(lambda et, pl: received.append((et, pl)))
        bus.emit("stt.final", {"history_id": "h1"})
        self.assertEqual(received, [("stt.final", {"history_id": "h1"})])

    def test_raising_listener_does_not_break_emit(self) -> None:
        bus = EventBus()
        ok: list[str] = []

        def _boom(event_type: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("listener boom")

        bus.add_listener(_boom)
        bus.add_listener(lambda et, pl: ok.append(et))
        # Не должно поднять исключение наружу — emit обязан изолировать сбойный листенер.
        bus.emit("stt.final", {"history_id": "h1"})
        self.assertEqual(ok, ["stt.final"], "второй листенер должен отработать несмотря на сбой первого")

    def test_listener_does_not_break_sse_subscribers(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        bus.add_listener(lambda et, pl: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.emit("stt.final", {"history_id": "h1"})
        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.final")


if __name__ == "__main__":
    unittest.main(verbosity=2)
