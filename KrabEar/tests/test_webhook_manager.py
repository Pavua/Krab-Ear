"""Unit-тесты для WebhookManager.

Покрывает: CRUD операции, IPC-обработчики, фильтрацию событий,
HMAC-подпись, retry-логику, статистику доставки.
"""

from __future__ import annotations
from backend.webhook_manager import WebhookManager, _MAX_RETRIES

import hashlib
import hmac
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(tmpdir: str) -> WebhookManager:
    return WebhookManager(data_dir=tmpdir)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class WebhookManagerCRUDTestCase(unittest.TestCase):
    """Тесты регистрации, удаления и списка webhook-ов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 1 — базовая регистрация
    def test_register_returns_webhook_id(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        self.assertIsInstance(wid, str)
        self.assertTrue(len(wid) > 0)

    # 2 — пустой список после создания нового менеджера
    def test_list_empty_initially(self) -> None:
        mgr = _make_manager(tempfile.mkdtemp())
        result = mgr.list_webhooks()
        self.assertEqual(result, [])

    # 3 — зарегистрированный webhook появляется в list_webhooks
    def test_list_shows_registered(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=["stt.failed"])
        hooks = self._mgr.list_webhooks()
        ids = [h["webhook_id"] for h in hooks]
        self.assertIn(wid, ids)

    # 4 — unregister возвращает True и удаляет запись
    def test_unregister_removes_webhook(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])
        removed = self._mgr.unregister_webhook(wid)
        self.assertTrue(removed)
        ids = [h["webhook_id"] for h in self._mgr.list_webhooks()]
        self.assertNotIn(wid, ids)

    # 5 — unregister несуществующего возвращает False
    def test_unregister_nonexistent_returns_false(self) -> None:
        result = self._mgr.unregister_webhook("no-such-id")
        self.assertFalse(result)

    # 6 — некорректный URL вызывает ValueError
    def test_register_invalid_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("ftp://not-http.com", events=[])

    # 7 — пустой URL вызывает ValueError
    def test_register_empty_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("   ", events=[])

    # 8 — реестр персистируется на диск и загружается заново
    def test_persistence_survives_reload(self) -> None:
        wid = self._mgr.register_webhook("https://persist.test/hook", events=["stt.final"])
        # Создаём новый экземпляр из той же директории
        mgr2 = _make_manager(self._tmpdir)
        ids = [h["webhook_id"] for h in mgr2.list_webhooks()]
        self.assertIn(wid, ids)

    # 9 — секрет не раскрывается в list_webhooks
    def test_secret_not_exposed_in_list(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=[], secret="my-secret")
        hooks = self._mgr.list_webhooks()
        for hook in hooks:
            self.assertNotIn("secret", hook)
            self.assertTrue(hook.get("has_secret"))

    # 10 — has_secret=False если секрет не указан
    def test_has_secret_false_when_no_secret(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=[])
        hooks = self._mgr.list_webhooks()
        self.assertEqual(hooks[0]["has_secret"], False)


class WebhookManagerStatsTestCase(unittest.TestCase):
    """Тесты статистики доставки."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 11 — get_webhook_stats для нового webhook возвращает нули
    def test_stats_initial_zeros(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])
        stats = self._mgr.get_webhook_stats(wid)
        self.assertEqual(stats["deliveries"], 0)
        self.assertEqual(stats["failures"], 0)
        self.assertIsNone(stats["last_status"])

    # 12 — get_webhook_stats для несуществующего поднимает KeyError
    def test_stats_unknown_id_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.get_webhook_stats("no-such-id")


class WebhookManagerFilterTestCase(unittest.TestCase):
    """Тесты фильтрации событий."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    def _register_and_capture_deliveries(
        self, events_filter: list[str], fire_event: str
    ) -> list[Any]:
        """Вспомогательный метод: регистрирует webhook, стреляет событием, возвращает вызовы POST."""
        self._mgr.register_webhook("https://example.com/hook", events=events_filter)
        delivered: list[Any] = []

        self._mgr._deliver_with_retry

        def capture(*args, **kwargs):
            delivered.append(args)
            # Не делаем реальный HTTP

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=capture):
            self._mgr.fire_webhook(fire_event, {"text": "hello"})
            # Даём потокам завершиться (они daemon=True)
            time.sleep(0.05)

        return delivered

    # 13 — пустой список events = принимает все события
    def test_empty_events_receives_all(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=[])
        delivered: list[Any] = []

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("stt.final", {"text": "test"})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 1)

    # 14 — фильтр по событию: webhook не вызывается для другого типа
    def test_event_filter_blocks_unmatched(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        delivered: list[Any] = []

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("stt.failed", {"text": "error"})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 0)

    # 15 — два webhook-а с разными фильтрами: каждый получает только своё
    def test_two_webhooks_independent_filters(self) -> None:
        _wid1 = self._mgr.register_webhook("https://one.com/hook", events=["stt.final"])  # noqa: F841
        _wid2 = self._mgr.register_webhook("https://two.com/hook", events=["stt.failed"])  # noqa: F841
        calls_log: list[str] = []

        def capture(webhook_id, url, secret, body, event_type):
            calls_log.append(url)

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=capture):
            self._mgr.fire_webhook("stt.final", {"text": "ok"})
            time.sleep(0.05)

        self.assertIn("https://one.com/hook", calls_log)
        self.assertNotIn("https://two.com/hook", calls_log)


class WebhookManagerHmacTestCase(unittest.TestCase):
    """Тесты HMAC-SHA256 подписи."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 16 — заголовок X-KrabEar-Signature присутствует при наличии секрета
    def test_signature_header_present_with_secret(self) -> None:
        sent_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            sent_headers.update(req.headers)
            return FakeResponse()

        with patch("backend.webhook_manager.urlopen", side_effect=fake_urlopen):
            self._mgr._post_once(
                url="https://example.com/hook",
                body=b'{"type":"stt.final"}',
                secret="mysecret",
            )

        # urllib capitalizes header keys
        sig_key = next((k for k in sent_headers if "signature" in k.lower()), None)
        self.assertIsNotNone(sig_key, "Заголовок X-KrabEar-Signature не найден")
        sig_value: str = sent_headers[sig_key]
        self.assertTrue(sig_value.startswith("sha256="))

        # Верифицируем HMAC
        body = b'{"type":"stt.final"}'
        expected = hmac.new(b"mysecret", body, hashlib.sha256).hexdigest()
        self.assertEqual(sig_value, f"sha256={expected}")

    # 17 — без секрета заголовок подписи не добавляется
    def test_no_signature_header_without_secret(self) -> None:
        sent_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=None):
            sent_headers.update(req.headers)
            return FakeResponse()

        with patch("backend.webhook_manager.urlopen", side_effect=fake_urlopen):
            self._mgr._post_once(
                url="https://example.com/hook",
                body=b'{"type":"stt.final"}',
                secret="",
            )

        sig_key = next((k for k in sent_headers if "signature" in k.lower()), None)
        self.assertIsNone(sig_key, "Заголовок подписи не должен быть без секрета")


class WebhookManagerRetryTestCase(unittest.TestCase):
    """Тесты логики retry при сбоях доставки."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 18 — retry делается до 3 попыток при серверных ошибках (5xx)
    def test_retries_on_5xx(self) -> None:
        attempt_count = [0]

        def failing_post(url, body, secret):
            attempt_count[0] += 1
            return 503  # Service Unavailable

        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        with patch.object(self._mgr, "_post_once", side_effect=failing_post):
            with patch("backend.webhook_manager.time.sleep"):  # убираем задержки
                self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "test")

        self.assertEqual(attempt_count[0], _MAX_RETRIES)

    # 19 — 4xx не ретраится (один запрос)
    def test_no_retry_on_4xx(self) -> None:
        attempt_count = [0]

        def client_error_post(url, body, secret):
            attempt_count[0] += 1
            return 400  # Bad Request

        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        with patch.object(self._mgr, "_post_once", side_effect=client_error_post):
            with patch("backend.webhook_manager.time.sleep"):
                self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "test")

        self.assertEqual(attempt_count[0], 1)

    # 20 — успешная доставка с первой попытки не делает retry
    def test_success_on_first_attempt_no_retry(self) -> None:
        attempt_count = [0]

        def success_post(url, body, secret):
            attempt_count[0] += 1
            return 200

        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        with patch.object(self._mgr, "_post_once", side_effect=success_post):
            self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "test")

        self.assertEqual(attempt_count[0], 1)


class WebhookManagerDeliveryStatsUpdateTestCase(unittest.TestCase):
    """Тесты обновления статистики по результатам доставки."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 21 — успешная доставка увеличивает счётчик deliveries
    def test_success_increments_deliveries(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        with patch.object(self._mgr, "_post_once", return_value=200):
            self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "stt.final")

        stats = self._mgr.get_webhook_stats(wid)
        self.assertEqual(stats["deliveries"], 1)
        self.assertEqual(stats["failures"], 0)
        self.assertEqual(stats["last_status"], 200)

    # 22 — провальная доставка увеличивает счётчик failures
    def test_failure_increments_failures(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        with patch.object(self._mgr, "_post_once", return_value=503):
            with patch("backend.webhook_manager.time.sleep"):
                self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "stt.final")

        stats = self._mgr.get_webhook_stats(wid)
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["deliveries"], 0)


class WebhookManagerIPCTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 23 — handle_register_webhook возвращает webhook_id
    def test_ipc_register(self) -> None:
        result = self._mgr.handle_register_webhook({
            "url": "https://ipc.test/hook",
            "events": ["stt.final"],
            "secret": "s3cr3t",
        })
        self.assertIn("webhook_id", result)
        self.assertIsInstance(result["webhook_id"], str)

    # 24 — handle_unregister_webhook возвращает {"removed": true}
    def test_ipc_unregister(self) -> None:
        wid = self._mgr.register_webhook("https://ipc.test/hook", events=[])
        result = self._mgr.handle_unregister_webhook({"webhook_id": wid})
        self.assertTrue(result["removed"])

    # 25 — handle_list_webhooks возвращает список
    def test_ipc_list(self) -> None:
        self._mgr.register_webhook("https://ipc.test/hook", events=[])
        result = self._mgr.handle_list_webhooks({})
        self.assertIn("webhooks", result)
        self.assertEqual(len(result["webhooks"]), 1)

    # 26 — handle_register_webhook с пустым URL поднимает RuntimeError/ValueError
    def test_ipc_register_invalid_url_raises(self) -> None:
        with self.assertRaises((ValueError, RuntimeError)):
            self._mgr.handle_register_webhook({"url": "", "events": []})

    # 27 — handle_unregister_webhook без webhook_id поднимает RuntimeError
    def test_ipc_unregister_missing_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_unregister_webhook({})


if __name__ == "__main__":
    unittest.main()
