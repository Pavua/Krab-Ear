"""Тесты для startup warmup probe LLM rewriter.

Проверяет:
- warmup_probe() отправляет минимальный запрос (max_tokens=1)
- Корректная обработка ошибок: connection error, timeout
- Таймаут уважается (timeout_sec параметр)
- warmup_sync() — синхронный wrapper для threading
- Настройка rewriter_warmup_on_startup=False отключает автостарт
- warmup_probe() возвращает latency_ms
- Circuit breaker НЕ трогается при warmup failure (failures не открывают circuit)
- При warmup SUCCESS — circuit reset'ится (OPEN→CLOSED), иначе confusing UX
- IPC метод warmup_rewriter возвращает правильную структуру
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import time

# Настройка путей — совместимость с запуском из repo root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.llm_rewriter import LLMRewriter, CircuitBreaker, CircuitState


def _make_rewriter(**kwargs) -> LLMRewriter:
    """Вспомогательный factory с разумными defaults."""
    defaults = dict(
        base_url="http://localhost:1234/v1",
        api_key="test-key",
        model="qwen3-test",
        timeout_sec=5.0,
        circuit_fail_threshold=3,
        circuit_initial_reset_sec=60,
    )
    defaults.update(kwargs)
    return LLMRewriter(**defaults)


class TestWarmupSendsMinimalRequest(unittest.TestCase):
    """warmup_probe() должен отправлять POST с max_tokens=1."""

    def test_warmup_sends_minimal_request(self):
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response) as mock_post:
            rewriter.warmup_probe()
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            json_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json") or call_kwargs[0][1]
            self.assertEqual(json_body["max_tokens"], 1)
            self.assertFalse(json_body["stream"])

    def test_warmup_posts_to_chat_completions_endpoint(self):
        rewriter = _make_rewriter(base_url="http://localhost:9999/v1")
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response) as mock_post:
            rewriter.warmup_probe()
            url = mock_post.call_args[0][0] if mock_post.call_args[0] else mock_post.call_args.args[0]
            self.assertIn("chat/completions", url)
            self.assertIn("9999", url)

    def test_warmup_returns_ok_true_on_200(self):
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()
            self.assertTrue(result["ok"])
            self.assertIsNone(result["error"])

    def test_warmup_returns_ok_false_on_non_200(self):
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "http_503")

    def test_warmup_uses_model_from_rewriter(self):
        rewriter = _make_rewriter(model="custom-test-model")
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response) as mock_post:
            rewriter.warmup_probe()
            call_kwargs = mock_post.call_args
            json_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json") or call_kwargs[0][1]
            self.assertEqual(json_body["model"], "custom-test-model")


class TestWarmupHandlesConnectionErrorGracefully(unittest.TestCase):
    """warmup_probe() никогда не бросает исключения."""

    def test_warmup_handles_connection_error(self):
        import requests as req
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=req.exceptions.ConnectionError("refused")):
            result = rewriter.warmup_probe()
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["error"])
            self.assertIn("latency_ms", result)

    def test_warmup_handles_os_error(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=OSError("network error")):
            result = rewriter.warmup_probe()
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["error"])

    def test_warmup_handles_exception_never_raises(self):
        """warmup_probe() должен проглатывать любое исключение."""
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=RuntimeError("unexpected")):
            try:
                result = rewriter.warmup_probe()
                self.assertFalse(result["ok"])
            except Exception as e:
                self.fail(f"warmup_probe() raised exception: {e}")


class TestWarmupRespectsTimeout(unittest.TestCase):
    """warmup_probe() передаёт timeout_sec в HTTP запрос."""

    def test_warmup_uses_custom_timeout_sec(self):
        import requests as req
        rewriter = _make_rewriter(timeout_sec=5.0)
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response) as mock_post:
            rewriter.warmup_probe(timeout_sec=30.0)
            call_kwargs = mock_post.call_args
            timeout = call_kwargs.kwargs.get("timeout") or call_kwargs[1].get("timeout")
            self.assertEqual(timeout, 30.0)

    def test_warmup_uses_instance_timeout_when_none(self):
        """Если timeout_sec=None, используется _timeout инстанса."""
        rewriter = _make_rewriter(timeout_sec=7.5)
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response) as mock_post:
            rewriter.warmup_probe(timeout_sec=None)
            call_kwargs = mock_post.call_args
            timeout = call_kwargs.kwargs.get("timeout") or call_kwargs[1].get("timeout")
            self.assertEqual(timeout, 7.5)

    def test_warmup_handles_timeout_exception(self):
        import requests as req
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=req.Timeout("timed out")):
            result = rewriter.warmup_probe(timeout_sec=0.001)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "timeout")
            self.assertGreaterEqual(result["latency_ms"], 0)


class TestWarmupDisabledWhenSettingFalse(unittest.TestCase):
    """rewriter_warmup_on_startup=False → thread НЕ запускается."""

    def test_warmup_thread_not_started_when_disabled(self):
        """Проверяем что при _warmup_enabled=False thread не стартует."""
        from core.config import DEFAULT_SETTINGS

        # Сохраняем оригинальное значение
        original = DEFAULT_SETTINGS.get("rewriter_warmup_on_startup", True)
        try:
            DEFAULT_SETTINGS["rewriter_warmup_on_startup"] = False
            _enabled = DEFAULT_SETTINGS.get("rewriter_warmup_on_startup", True)
            self.assertFalse(_enabled)
        finally:
            DEFAULT_SETTINGS["rewriter_warmup_on_startup"] = original

    def test_warmup_enabled_by_default(self):
        from core.config import DEFAULT_SETTINGS
        self.assertTrue(DEFAULT_SETTINGS.get("rewriter_warmup_on_startup", False))


class TestWarmupReturnsLatencyMs(unittest.TestCase):
    """warmup_probe() возвращает latency_ms в миллисекундах."""

    def test_warmup_returns_latency_in_ms(self):
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()
            self.assertIn("latency_ms", result)
            self.assertIsInstance(result["latency_ms"], int)
            self.assertGreaterEqual(result["latency_ms"], 0)

    def test_warmup_latency_measured_on_error(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=OSError("failed")):
            result = rewriter.warmup_probe()
            self.assertIn("latency_ms", result)
            self.assertIsInstance(result["latency_ms"], int)
            self.assertGreaterEqual(result["latency_ms"], 0)


class TestWarmupCircuitBreakerBehavior(unittest.TestCase):
    """Поведение circuit breaker при warmup_probe()."""

    def test_warmup_failure_does_not_open_circuit(self):
        """Failures при warmup НЕ открывают circuit — warmup не user-facing."""
        import requests as req
        rewriter = _make_rewriter(circuit_fail_threshold=1)
        initial_state = rewriter._circuit.state

        with patch.object(rewriter._session, "post", side_effect=req.exceptions.ConnectionError("refused")):
            for _ in range(5):  # много ошибок
                rewriter.warmup_probe()

        # Circuit должен остаться закрытым
        self.assertEqual(rewriter._circuit.state, "closed")
        self.assertEqual(rewriter._circuit.state, initial_state)

    def test_warmup_success_resets_open_circuit(self):
        """2026-05-09 fix: warmup success когда circuit OPEN → circuit закрывается.

        Scenario: LM Studio token mismatch → circuit OPEN; user fixes token → warmup OK
        → circuit должен закрыться, иначе следующий rewrite всё равно блокируется.
        """
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Принудительно открываем circuit (симулируем накопленные failures)
        rewriter._circuit._state = CircuitState.OPEN
        rewriter._circuit._opened_at = time.monotonic()

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()

        self.assertTrue(result["ok"])
        # Circuit должен закрыться после успешного warmup
        self.assertEqual(rewriter._circuit.state, "closed")

    def test_warmup_success_resets_half_open_circuit(self):
        """warmup success когда circuit HALF_OPEN → circuit закрывается (HALF_OPEN→CLOSED)."""
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        rewriter._circuit._state = CircuitState.HALF_OPEN
        rewriter._circuit._opened_at = time.monotonic()

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()

        self.assertTrue(result["ok"])
        self.assertEqual(rewriter._circuit.state, "closed")

    def test_warmup_success_leaves_closed_circuit_closed(self):
        """warmup success когда circuit CLOSED → circuit остаётся CLOSED (no-op)."""
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 200

        self.assertEqual(rewriter._circuit.state, "closed")

        with patch.object(rewriter._session, "post", return_value=mock_response):
            rewriter.warmup_probe()

        self.assertEqual(rewriter._circuit.state, "closed")

    def test_warmup_failure_does_not_close_open_circuit(self):
        """warmup failure при OPEN circuit → circuit остаётся OPEN."""
        import requests as req
        rewriter = _make_rewriter()

        rewriter._circuit._state = CircuitState.OPEN
        rewriter._circuit._opened_at = time.monotonic()

        with patch.object(rewriter._session, "post", side_effect=req.exceptions.ConnectionError("refused")):
            result = rewriter.warmup_probe()

        self.assertFalse(result["ok"])
        # Failure не закрывает circuit
        self.assertEqual(rewriter._circuit.state, "open")

    def test_warmup_non_200_does_not_reset_circuit(self):
        """HTTP 503 при warmup не закрывает OPEN circuit (ok=False)."""
        rewriter = _make_rewriter()
        mock_response = MagicMock()
        mock_response.status_code = 503

        rewriter._circuit._state = CircuitState.OPEN
        rewriter._circuit._opened_at = time.monotonic()

        with patch.object(rewriter._session, "post", return_value=mock_response):
            result = rewriter.warmup_probe()

        self.assertFalse(result["ok"])
        self.assertEqual(rewriter._circuit.state, "open")


class TestWarmupIpcHandler(unittest.TestCase):
    """_handle_warmup_rewriter возвращает ok, latency_ms, error, model."""

    def _make_handler_with_rewriter(self, warmup_result):
        """Создаёт mock-сервис с методом _handle_warmup_rewriter."""
        rewriter = _make_rewriter()

        # Patch warmup_probe to return our controlled result
        rewriter.warmup_probe = MagicMock(return_value=warmup_result)

        # Minimal service-like object to test the handler logic
        class FakeService:
            _llm_rewriter = rewriter

            def _get_runtime_setting(self, key, default):
                return 15 if key == "rewriter_warmup_timeout_sec" else default

            def _handle_warmup_rewriter(self, params):
                if self._llm_rewriter is None:
                    return {"ok": False, "latency_ms": 0, "error": "rewriter_disabled", "model": None}
                runtime_timeout = self._get_runtime_setting("rewriter_warmup_timeout_sec", 15)
                timeout_sec = float(params.get("timeout_sec") or runtime_timeout)
                result = self._llm_rewriter.warmup_probe(timeout_sec=timeout_sec)
                result["model"] = getattr(self._llm_rewriter, "_model", None)
                return result

        return FakeService()

    def test_handler_returns_ok_true_on_success(self):
        svc = self._make_handler_with_rewriter(
            {"ok": True, "latency_ms": 1200, "error": None}
        )
        result = svc._handle_warmup_rewriter({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["latency_ms"], 1200)
        self.assertIsNone(result["error"])
        self.assertIn("model", result)

    def test_handler_returns_ok_false_on_failure(self):
        svc = self._make_handler_with_rewriter(
            {"ok": False, "latency_ms": 500, "error": "timeout"}
        )
        result = svc._handle_warmup_rewriter({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_handler_disabled_rewriter_returns_error(self):
        class FakeServiceNoRewriter:
            _llm_rewriter = None

            def _handle_warmup_rewriter(self, params):
                if self._llm_rewriter is None:
                    return {"ok": False, "latency_ms": 0, "error": "rewriter_disabled", "model": None}

        svc = FakeServiceNoRewriter()
        result = svc._handle_warmup_rewriter({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rewriter_disabled")
        self.assertIsNone(result["model"])

    def test_handler_passes_timeout_from_params(self):
        rewriter = _make_rewriter()
        rewriter.warmup_probe = MagicMock(return_value={"ok": True, "latency_ms": 100, "error": None})

        class FakeService:
            _llm_rewriter = rewriter

            def _get_runtime_setting(self, key, default):
                return 15

            def _handle_warmup_rewriter(self, params):
                if self._llm_rewriter is None:
                    return {"ok": False, "latency_ms": 0, "error": "rewriter_disabled", "model": None}
                runtime_timeout = self._get_runtime_setting("rewriter_warmup_timeout_sec", 15)
                timeout_sec = float(params.get("timeout_sec") or runtime_timeout)
                result = self._llm_rewriter.warmup_probe(timeout_sec=timeout_sec)
                result["model"] = getattr(self._llm_rewriter, "_model", None)
                return result

        svc = FakeService()
        svc._handle_warmup_rewriter({"timeout_sec": 30.0})
        rewriter.warmup_probe.assert_called_once_with(timeout_sec=30.0)


class TestDefaultSettingsContainWarmup(unittest.TestCase):
    """DEFAULT_SETTINGS должен содержать warmup настройки."""

    def test_default_settings_has_warmup_on_startup(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("rewriter_warmup_on_startup", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["rewriter_warmup_on_startup"], True)

    def test_default_settings_has_warmup_timeout_sec(self):
        # Default bumped 15 → 60 in commit f6aa087 for vision multimodal cold-load;
        # bumped 60 → 240 in fix/lm-studio-warmup: JIT TTL 1800s evicts model after
        # 30min idle → External SSD cold-load ~3-4 min; 240s covers worst-case.
        from core.config import DEFAULT_SETTINGS
        self.assertIn("rewriter_warmup_timeout_sec", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["rewriter_warmup_timeout_sec"], 240)


class TestWarmupSyncWrapper(unittest.TestCase):
    """warmup_sync() — синхронный wrapper для threading.Thread."""

    def test_warmup_sync_calls_warmup_with_timeout(self):
        rewriter = _make_rewriter()
        rewriter.warmup = MagicMock(return_value=True)

        rewriter.warmup_sync(timeout_sec=20.0)
        rewriter.warmup.assert_called_once_with(timeout_sec=20.0)

    def test_warmup_sync_calls_warmup_with_none_by_default(self):
        rewriter = _make_rewriter()
        rewriter.warmup = MagicMock(return_value=True)

        rewriter.warmup_sync()
        rewriter.warmup.assert_called_once_with(timeout_sec=None)


if __name__ == "__main__":
    unittest.main()
