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
import threading
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
        wid = self._mgr.register_webhook("https://persist.test/hook", events=["stt.final"], allow_local=True)
        # Создаём новый экземпляр из той же директории
        mgr2 = _make_manager(self._tmpdir)
        ids = [h["webhook_id"] for h in mgr2.list_webhooks()]
        self.assertIn(wid, ids)

    # 9 — секрет не раскрывается в list_webhooks
    def test_secret_not_exposed_in_list(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=[], secret="my-very-long-secret")
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

        def capture(webhook_id, url, secret, body, event_type, allow_local=False):
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
        import unittest.mock as mock_mod
        sent_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n=-1): return b""

        def fake_open(req, timeout=None):
            sent_headers.update(req.headers)
            return FakeResponse()

        fake_opener = mock_mod.MagicMock()
        fake_opener.open.side_effect = fake_open

        with patch("backend.webhook_manager.urllib.request.build_opener", return_value=fake_opener):
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
        import unittest.mock as mock_mod
        sent_headers: dict[str, str] = {}

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n=-1): return b""

        def fake_open(req, timeout=None):
            sent_headers.update(req.headers)
            return FakeResponse()

        fake_opener = mock_mod.MagicMock()
        fake_opener.open.side_effect = fake_open

        with patch("backend.webhook_manager.urllib.request.build_opener", return_value=fake_opener):
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

        def failing_post(url, body, secret, allow_local=False):
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

        def client_error_post(url, body, secret, allow_local=False):
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

        def success_post(url, body, secret, allow_local=False):
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

    # 23 — handle_register_webhook возвращает webhook_id (публичный URL)
    def test_ipc_register(self) -> None:
        # Используем публичный домен — SSRF guard блокирует localhost/private.
        # webhook_allow_local=True намеренно передаётся, но игнорируется (wave1763 fix).
        result = self._mgr.handle_register_webhook({
            "url": "https://example.com/hook",
            "events": ["stt.final"],
            "secret": "s3cr3t-is-long-enough-now",
            "webhook_allow_local": True,  # игнорируется IPC-обработчиком
        })
        self.assertIn("webhook_id", result)
        self.assertIsInstance(result["webhook_id"], str)

    # 24 — handle_unregister_webhook возвращает {"removed": true}
    def test_ipc_unregister(self) -> None:
        wid = self._mgr.register_webhook("https://ipc.test/hook", events=[], allow_local=True)
        result = self._mgr.handle_unregister_webhook({"webhook_id": wid})
        self.assertTrue(result["removed"])

    # 25 — handle_list_webhooks возвращает список
    def test_ipc_list(self) -> None:
        self._mgr.register_webhook("https://ipc.test/hook", events=[], allow_local=True)
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


class WebhookManagerSSRFBypassFixTestCase(unittest.TestCase):
    """Регрессионные тесты wave1763 MED SSRF-bypass: webhook_allow_local из IPC игнорируется.

    Злоумышленник не может обойти SSRF-защиту передав webhook_allow_local=True
    в теле IPC-запроса — handle_register_webhook всегда использует allow_local=False.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 28a — localhost URL + webhook_allow_local=True → SSRF guard не обходится, ValueError
    def test_ipc_allow_local_true_does_not_bypass_ssrf_for_localhost(self) -> None:
        """Передача webhook_allow_local=True НЕ снимает SSRF-защиту для localhost.

        До исправления allow_local передавался вербально из params, и этот вызов
        регистрировал webhook на 127.0.0.1, обходя всю защиту (MED SSRF-bypass).
        После исправления — ValueError от SSRF guard.
        """
        with self.assertRaises(ValueError):
            self._mgr.handle_register_webhook({
                "url": "http://127.0.0.1:8080/admin",
                "events": [],
                "webhook_allow_local": True,  # должен быть проигнорирован
            })

    # 28b — RFC1918 URL + webhook_allow_local=True → SSRF guard не обходится
    def test_ipc_allow_local_true_does_not_bypass_ssrf_for_private_ip(self) -> None:
        """Передача webhook_allow_local=True не снимает защиту для RFC1918."""
        with self.assertRaises(ValueError):
            self._mgr.handle_register_webhook({
                "url": "http://192.168.1.1/hook",
                "events": ["stt.final"],
                "webhook_allow_local": True,
            })

    # 28c — cloud metadata IP + webhook_allow_local=True → SSRF guard не обходится
    def test_ipc_allow_local_true_does_not_bypass_ssrf_for_metadata_ip(self) -> None:
        """Передача webhook_allow_local=True не снимает защиту для cloud metadata."""
        with self.assertRaises(ValueError):
            self._mgr.handle_register_webhook({
                "url": "http://169.254.169.254/latest/meta-data/",
                "events": [],
                "webhook_allow_local": True,
            })

    # 28d — нормальный публичный HTTPS webhook регистрируется успешно (не регрессия)
    def test_ipc_public_https_webhook_still_registers(self) -> None:
        """Легитимный публичный webhook регистрируется без проблем."""
        result = self._mgr.handle_register_webhook({
            "url": "https://example.com/webhook",
            "events": ["stt.final"],
            "secret": "abcdefghijklmnop",
        })
        self.assertIn("webhook_id", result)
        self.assertIsInstance(result["webhook_id"], str)
        self.assertTrue(len(result["webhook_id"]) > 0)

    # 28e — webhook_allow_local=False явно тоже работает как ожидается
    def test_ipc_allow_local_false_explicit_still_blocks_localhost(self) -> None:
        """Явное allow_local=False (и дефолтный False) также блокирует localhost."""
        with self.assertRaises(ValueError):
            self._mgr.handle_register_webhook({
                "url": "http://localhost/hook",
                "events": [],
                "webhook_allow_local": False,
            })


class WebhookManagerFireOnEventTypeTestCase(unittest.TestCase):
    """test_fire_on_event_type — fire_webhook вызывает доставку для нужного типа."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 28 — fire_webhook доставляет совпадающий тип события
    def test_fire_on_event_type_matching(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        delivered: list[Any] = []

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a[1])):
            self._mgr.fire_webhook("stt.final", {"text": "hello"})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0], "https://example.com/hook")

    # 29 — fire_webhook пропускает несовпадающий тип события
    def test_fire_on_event_type_not_matching(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=["stt.final"])
        delivered: list[Any] = []

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("translation.done", {"text": "hello"})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 0)

    # 30 — fire_webhook с events=[] получает все типы событий
    def test_fire_on_event_type_wildcard(self) -> None:
        self._mgr.register_webhook("https://example.com/hook", events=[])
        delivered: list[Any] = []

        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("any.event", {})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 1)


class WebhookManagerSkipDisabledTestCase(unittest.TestCase):
    """test_skip_disabled_webhook — отключённый webhook не получает события."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 31 — disabled webhook не вызывается при fire_webhook
    def test_skip_disabled_webhook(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])
        # Отключаем webhook напрямую
        with self._mgr._lock:
            self._mgr._webhooks[wid]["enabled"] = False

        delivered: list[Any] = []
        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("stt.final", {})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 0)

    # 32 — включённый webhook после disabled=True получает события
    def test_re_enabled_webhook_receives_events(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])
        with self._mgr._lock:
            self._mgr._webhooks[wid]["enabled"] = False

        # Включаем обратно
        with self._mgr._lock:
            self._mgr._webhooks[wid]["enabled"] = True

        delivered: list[Any] = []
        with patch.object(self._mgr, "_deliver_with_retry", side_effect=lambda *a, **k: delivered.append(a)):
            self._mgr.fire_webhook("stt.final", {})
            time.sleep(0.05)

        self.assertEqual(len(delivered), 1)


class WebhookManagerTimeoutTestCase(unittest.TestCase):
    """test_timeout_handled_gracefully — timeout при доставке не роняет процесс."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 33 — сетевой timeout обрабатывается как ошибка, не исключение
    def test_timeout_handled_gracefully(self) -> None:
        from urllib.error import URLError

        wid = self._mgr.register_webhook("https://example.com/hook", events=[])

        def timeout_post(url, body, secret):
            raise URLError("timed out")

        with patch.object(self._mgr, "_post_once", side_effect=timeout_post):
            with patch("backend.webhook_manager.time.sleep"):
                # Не должно бросить исключение наружу
                self._mgr._deliver_with_retry(wid, "https://example.com/hook", "", b"{}", "stt.final")

        stats = self._mgr.get_webhook_stats(wid)
        self.assertGreater(stats["failures"], 0)

    # 34 — timeout не блокирует caller (доставка async в Thread)
    def test_timeout_does_not_block_fire_webhook(self) -> None:
        from urllib.error import URLError

        self._mgr.register_webhook("https://example.com/hook", events=[])

        slow_called = []

        def slow_post(*args, **kwargs):
            slow_called.append(True)
            raise URLError("very slow")

        with patch.object(self._mgr, "_post_once", side_effect=slow_post):
            with patch("backend.webhook_manager.time.sleep"):
                start = time.monotonic()
                self._mgr.fire_webhook("stt.final", {})
                elapsed = time.monotonic() - start

        # fire_webhook должен вернуться немедленно (< 0.2s)
        self.assertLess(elapsed, 0.2)


class WebhookManagerConcurrentFireTestCase(unittest.TestCase):
    """test_concurrent_fire_does_not_block — параллельная доставка не блокирует."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 35 — несколько fire_webhook из разных потоков не падают
    def test_concurrent_fire_does_not_block(self) -> None:
        for i in range(3):
            self._mgr.register_webhook(f"https://example{i}.com/hook", events=[], allow_local=True)

        call_count = [0]
        lock = threading.Lock()

        def instant_post(*args, **kwargs):
            with lock:
                call_count[0] += 1
            return 200

        with patch.object(self._mgr, "_post_once", side_effect=instant_post):
            threads = [
                threading.Thread(target=self._mgr.fire_webhook, args=("stt.final", {}))
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2.0)
            # fire_webhook() spawns internal delivery threads; wait for them
            # INSIDE the patch so _post_once stays mocked until they complete.
            # Without this, delivery threads call the real _post_once after the
            # patch context exits → DNS failure on ubuntu CI (no network).
            time.sleep(0.5)
            # 5 fire_webhook calls × 3 webhooks = 15 deliveries
            self.assertEqual(call_count[0], 15)

    # 36 — concurrent register + fire не вызывает RuntimeError
    def test_concurrent_register_and_fire_safe(self) -> None:
        errors = []

        def register_loop():
            for i in range(5):
                try:
                    self._mgr.register_webhook(f"https://reg{i}.com/hook", events=[], allow_local=True)
                except Exception as e:
                    errors.append(e)

        with patch.object(self._mgr, "_post_once", return_value=200):
            reg_thread = threading.Thread(target=register_loop)
            fire_thread = threading.Thread(
                target=lambda: [self._mgr.fire_webhook("stt.final", {}) for _ in range(5)]
            )
            reg_thread.start()
            fire_thread.start()
            reg_thread.join(timeout=2.0)
            fire_thread.join(timeout=2.0)

        self.assertEqual(errors, [], f"Errors in threads: {errors}")


class WebhookManagerURLValidationTestCase(unittest.TestCase):
    """test_url_validation — SSRF guard: localhost, file://, invalid schemes rejected."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    # 37 — file:// URL вызывает ValueError
    def test_file_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("file:///etc/passwd", events=[])

    # 38 — ftp:// URL вызывает ValueError
    def test_ftp_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("ftp://example.com/hook", events=[])

    # 39 — пустой URL вызывает ValueError
    def test_empty_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("", events=[])

    # 40 — пробельный URL вызывает ValueError
    def test_whitespace_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("   ", events=[])

    # 41 — http:// принимается
    def test_http_url_accepted(self) -> None:
        wid = self._mgr.register_webhook("http://example.com/hook", events=[])
        self.assertIsNotNone(wid)

    # 42 — https:// принимается (must use a resolvable domain; gap 3 fix)
    def test_https_url_accepted(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/hook", events=[])
        self.assertIsNotNone(wid)

    # 43 — javascript: URL вызывает ValueError (SSRF / injection guard)
    def test_javascript_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("javascript:alert(1)", events=[])

    # 44 — data: URL вызывает ValueError
    def test_data_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("data:text/plain,hello", events=[])

    # NOTE: WebhookManager blocks localhost URLs via SSRF guard (Wave 157+).
    # W1355 updated this test from "currently accepted" to correctly asserting ValueError.
    def test_localhost_http_blocked_by_ssrf_guard(self) -> None:
        """localhost http:// отклоняется SSRF guard при регистрации (Wave 157+ behaviour).

        Использовать allow_local=True для dev/self-hosted окружений.
        """
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("http://localhost:9999/hook", events=[])


class WebhookManagerSecretLengthW1770TestCase(unittest.TestCase):
    """wave-1770 LOW: HMAC secret minimum length enforcement (≥ 16 chars).

    Short non-empty secrets (e.g. "abc") are trivially brutable — enforce
    minimum 16-char length at register_webhook() time, while allowing empty
    string (meaning "no HMAC signature") for endpoints that don't support it.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = _make_manager(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_empty_secret_allowed(self) -> None:
        """Пустой secret допустим — означает 'без подписи'."""
        wid = self._mgr.register_webhook("https://example.com/hook", events=[], secret="")
        self.assertIsNotNone(wid)

    def test_exactly_16_chars_secret_allowed(self) -> None:
        """Secret ровно 16 символов — допустимый минимум."""
        wid = self._mgr.register_webhook(
            "https://example.com/hook", events=[], secret="1234567890123456"
        )
        self.assertIsNotNone(wid)

    def test_longer_secret_allowed(self) -> None:
        """Secret длиннее 16 символов — без ограничения сверху."""
        wid = self._mgr.register_webhook(
            "https://example.com/hook", events=[], secret="very-long-secret-that-is-safe"
        )
        self.assertIsNotNone(wid)

    def test_15_chars_raises(self) -> None:
        """Secret из 15 символов вызывает ValueError (одним меньше минимума)."""
        with self.assertRaises(ValueError):
            self._mgr.register_webhook(
                "https://example.com/hook", events=[], secret="123456789012345"
            )

    def test_1_char_raises(self) -> None:
        """Однобуквенный secret вызывает ValueError."""
        with self.assertRaises(ValueError):
            self._mgr.register_webhook(
                "https://example.com/hook", events=[], secret="x"
            )

    def test_short_secret_via_ipc_raises(self) -> None:
        """handle_register_webhook также отклоняет слабый secret."""
        with self.assertRaises((ValueError, RuntimeError)):
            self._mgr.handle_register_webhook({
                "url": "https://example.com/hook",
                "events": [],
                "secret": "weak",
            })


if __name__ == "__main__":
    unittest.main()
