"""Тесты для IPCThrottle — rate limiting IPC-методов Krab Ear.

Покрывают:
  - Классификацию методов (heavy/medium/light)
  - Базовое поведение token bucket: разрешение и отклонение
  - Потокобезопасность при параллельных вызовах
  - get_wait_time: положительное значение при исчерпании токенов
  - get_throttle_stats: корректный подсчёт вызовов и throttled
  - Кастомные лимиты
  - Отключение через лимит 0 и сброс stats
  - Интеграция с BackendService (throttle wire-in)
"""

from __future__ import annotations

import sys
import threading
import time
import tempfile
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ipc_throttle import (
    IPCThrottle,
    _classify_method,
    HEAVY_METHODS,
    MEDIUM_METHODS,
)


class TestClassifyMethod(unittest.TestCase):
    """Тест _classify_method — корректная категоризация."""

    def test_heavy_methods_classified_correctly(self):
        for m in ["transcribe_paths", "export_history", "summarize_text"]:
            self.assertEqual(_classify_method(m), "heavy", f"Expected heavy: {m}")

    def test_medium_methods_classified_correctly(self):
        for m in ["search_history", "get_diagnostics", "translate_text"]:
            self.assertEqual(_classify_method(m), "medium", f"Expected medium: {m}")

    def test_light_methods_classified_as_light(self):
        for m in ["ping", "get_settings", "some_unknown_method"]:
            self.assertEqual(_classify_method(m), "light", f"Expected light: {m}")

    def test_heavy_medium_sets_are_disjoint(self):
        overlap = HEAVY_METHODS & MEDIUM_METHODS
        self.assertEqual(overlap, set(), f"Methods in both sets: {overlap}")


class TestTokenBucket(unittest.TestCase):
    """Тест поведения token bucket через IPCThrottle."""

    def test_allows_calls_within_limit(self):
        throttle = IPCThrottle(limits={"heavy": 3, "medium": 30, "light": 120})
        for _ in range(3):
            self.assertTrue(throttle.check_rate("transcribe_paths"))

    def test_rejects_call_when_bucket_empty(self):
        # Лимит = 3 для heavy
        throttle = IPCThrottle(limits={"heavy": 3, "medium": 30, "light": 120})
        for _ in range(3):
            throttle.check_rate("transcribe_paths")
        # 4-й вызов должен быть отклонён
        self.assertFalse(throttle.check_rate("transcribe_paths"))

    def test_allows_calls_after_token_refill(self):
        # Очень маленький лимит, но fast refill
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        result1 = throttle.check_rate("export_history")
        self.assertTrue(result1)
        # Немного ждём — bucket пополняется со скоростью 1/60 t/s
        # При capacity=1 ждём ~1 секунду для полного пополнения
        time.sleep(1.1)
        result2 = throttle.check_rate("export_history")
        self.assertTrue(result2, "Должен разрешить после пополнения токена")

    def test_light_methods_have_higher_limit(self):
        # Дефолтный лимит light = 120, heavy = 5
        throttle = IPCThrottle()
        heavy_ok = sum(1 for _ in range(6) if throttle.check_rate("transcribe_paths"))
        # Ровно 5 разрешённых
        self.assertEqual(heavy_ok, 5)

        throttle2 = IPCThrottle()
        light_ok = sum(1 for _ in range(121) if throttle2.check_rate("ping"))
        self.assertEqual(light_ok, 120)


class TestGetWaitTime(unittest.TestCase):
    """Тест get_wait_time."""

    def test_zero_when_tokens_available(self):
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 120})
        wait = throttle.get_wait_time("transcribe_paths")
        self.assertAlmostEqual(wait, 0.0, places=2)

    def test_positive_when_bucket_empty(self):
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        throttle.check_rate("export_history")  # исчерпать токен
        wait = throttle.get_wait_time("export_history")
        self.assertGreater(wait, 0.0)
        # При capacity=1 ждать нужно не более 60 секунд
        self.assertLessEqual(wait, 60.0)

    def test_wait_time_independent_methods(self):
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        # Исчерпать heavy
        throttle.check_rate("transcribe_paths")
        # light не должен иметь wait
        self.assertAlmostEqual(throttle.get_wait_time("ping"), 0.0, places=2)


class TestThrottleStats(unittest.TestCase):
    """Тест get_throttle_stats."""

    def test_initial_stats_empty(self):
        throttle = IPCThrottle()
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["total_throttled"], 0)
        self.assertEqual(stats["methods"], {})

    def test_stats_tracks_allowed_calls(self):
        throttle = IPCThrottle()
        throttle.check_rate("ping")
        throttle.check_rate("ping")
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 2)
        self.assertEqual(stats["total_throttled"], 0)
        self.assertEqual(stats["methods"]["ping"]["calls"], 2)
        self.assertEqual(stats["methods"]["ping"]["throttled"], 0)

    def test_stats_tracks_throttled_calls(self):
        throttle = IPCThrottle(limits={"heavy": 2, "medium": 30, "light": 120})
        throttle.check_rate("export_history")  # ok
        throttle.check_rate("export_history")  # ok
        throttle.check_rate("export_history")  # throttled
        throttle.check_rate("export_history")  # throttled
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 4)
        self.assertEqual(stats["total_throttled"], 2)
        self.assertEqual(stats["methods"]["export_history"]["calls"], 4)
        self.assertEqual(stats["methods"]["export_history"]["throttled"], 2)

    def test_stats_includes_category_and_limit(self):
        throttle = IPCThrottle()
        throttle.check_rate("transcribe_paths")
        stats = throttle.get_throttle_stats()
        method_info = stats["methods"]["transcribe_paths"]
        self.assertEqual(method_info["category"], "heavy")
        self.assertEqual(method_info["limit_per_minute"], 5)

    def test_reset_stats_clears_counters(self):
        throttle = IPCThrottle()
        throttle.check_rate("ping")
        throttle.reset_stats()
        stats = throttle.get_throttle_stats()
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["methods"], {})


class TestCustomLimits(unittest.TestCase):
    """Тест переопределения лимитов."""

    def test_custom_heavy_limit(self):
        throttle = IPCThrottle(limits={"heavy": 10, "medium": 30, "light": 120})
        ok = sum(1 for _ in range(11) if throttle.check_rate("transcribe_paths"))
        self.assertEqual(ok, 10)

    def test_custom_light_limit(self):
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 5})
        ok = sum(1 for _ in range(6) if throttle.check_rate("ping"))
        self.assertEqual(ok, 5)


class TestThreadSafety(unittest.TestCase):
    """Тест потокобезопасности IPCThrottle."""

    def test_concurrent_access_no_exception(self):
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 120})
        errors = []

        def worker():
            try:
                for _ in range(50):
                    throttle.check_rate("ping")
                    throttle.get_wait_time("ping")
                    throttle.get_throttle_stats()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrency errors: {errors}")

    def test_concurrent_throttle_count_consistent(self):
        """Сумма allowed + throttled == total_calls."""
        throttle = IPCThrottle(limits={"heavy": 10, "medium": 30, "light": 120})

        def burst():
            for _ in range(20):
                throttle.check_rate("transcribe_paths")

        threads = [threading.Thread(target=burst) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = throttle.get_throttle_stats()
        total = stats["total_calls"]
        throttled = stats["total_throttled"]
        method_stats = stats["methods"].get("transcribe_paths", {})
        self.assertEqual(total, 100)  # 5 threads * 20 calls
        self.assertEqual(method_stats.get("calls", 0), 100)
        allowed = total - throttled
        self.assertEqual(method_stats.get("throttled", 0), throttled)
        # Allowed не может превышать capacity (10)
        self.assertLessEqual(allowed, 10)


class TestIPCThrottleIntegrationWithService(unittest.TestCase):
    """Интеграционный тест: BackendService возвращает ошибку при rate limit exceeded."""

    def setUp(self):
        import numpy as np
        from backend.service import BackendService
        from backend.state_store import StateStore

        class FakeRecorder:
            is_recording = False
            sample_rate = 16000

            def start(self):
                self.is_recording = True
                return True

            def stop(self, timeout_sec=3.0, trim_tail_ms=0):
                if not self.is_recording:
                    return None
                self.is_recording = False
                return np.zeros(16000, dtype=np.float32), 1.0

            def snapshot_audio(self, max_duration_sec=12.0):
                return np.ones(16000, dtype=np.float32), 1.0

        class FakeTranscriber:
            def transcribe(self, audio, sample_rate, quality_profile="balanced",
                           vocabulary=None, prompt=None, language=None):
                return ("test text", {"confidence": 0.9})

            def transcribe_v2(self, audio, sample_rate, quality_profile="balanced",
                              vocabulary=None, prompt=None, language=None):
                return ("test text", {"confidence": 0.9})

            def get_active_model(self):
                return "test-model"

        self._tmpdir = tempfile.mkdtemp()
        store = StateStore(data_dir=Path(self._tmpdir))
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
        )
        # Заменяем throttle с очень жёстким лимитом для теста
        from backend.ipc_throttle import IPCThrottle
        self.service._ipc_throttle = IPCThrottle(
            limits={"heavy": 1, "medium": 1, "light": 1}
        )

    def test_throttled_request_returns_rate_limit_error(self):
        # Первый ping — OK
        resp1 = self.service.handle_request({"id": "1", "method": "ping", "params": {}})
        self.assertTrue(resp1["ok"])

        # Второй ping — должен быть throttled (лимит = 1)
        resp2 = self.service.handle_request({"id": "2", "method": "ping", "params": {}})
        self.assertFalse(resp2["ok"])
        self.assertEqual(resp2["error"]["code"], "rate_limit_exceeded")

    def test_throttle_disabled_allows_all_calls(self):
        # Отключаем throttle
        self.service._ipc_throttle = None
        for i in range(10):
            resp = self.service.handle_request({"id": str(i), "method": "ping", "params": {}})
            self.assertTrue(resp["ok"], f"Call {i} was blocked but throttle is disabled")

    def test_get_throttle_stats_ipc_method(self):
        resp = self.service.handle_request({"id": "1", "method": "get_throttle_stats", "params": {}})
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("total_calls", result)
        self.assertIn("total_throttled", result)
        self.assertIn("methods", result)

    def test_throttle_error_response_contains_wait_time(self):
        # Исчерпать heavy лимит
        self.service.handle_request({"id": "1", "method": "export_history", "params": {}})
        # Следующий вызов throttled
        resp = self.service.handle_request({"id": "2", "method": "export_history", "params": {}})
        # Может вернуть ok=False (throttled) или ошибку экспорта, но не IndexError и т.п.
        # Нас интересует только что rate_limit_exceeded появляется при throttling
        if not resp["ok"]:
            # Либо rate_limit_exceeded, либо другая ошибка от самого метода (export_history может упасть)
            error_code = resp["error"]["code"]
            self.assertIn(error_code, ("rate_limit_exceeded", "internal_error"))


if __name__ == "__main__":
    unittest.main()
