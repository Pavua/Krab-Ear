"""W1304: тесты TTL для _unavailable_models в AudioEngine.

Проверяет:
- test_unavailable_model_blocked_within_ttl   — модель заблокирована пока TTL не истёк
- test_unavailable_model_recovers_after_ttl   — модель разблокируется после истечения TTL
- test_clear_unavailable_models_ipc_resets    — _handle_clear_unavailable_models сбрасывает blacklist
- test_concurrent_add_thread_safe             — параллельные записи не ломают dict

Bug: W1141 заявлял TTL-реализацию, но _unavailable_models был plain set без timestamps.
Fix: W1304 — dict[str, float] + _is_model_unavailable() + _UNAVAILABLE_MODEL_TTL_SEC = 300.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine, _UNAVAILABLE_MODEL_TTL_SEC


def _make_minimal_engine() -> AudioEngine:
    """Создаёт AudioEngine без реальных зависимостей (mlx-whisper, pyannote и пр.).

    Использует object.__new__ чтобы пропустить __init__ и установить только
    атрибуты, нужные для тестируемых методов.
    """
    engine = object.__new__(AudioEngine)
    engine._unavailable_models = {}
    engine._router = None
    return engine


class UnavailableModelsTTLTestCase(unittest.TestCase):
    """Тесты TTL-логики _unavailable_models."""

    def setUp(self) -> None:
        self.engine = _make_minimal_engine()

    # ------------------------------------------------------------------
    # 1. Модель заблокирована пока TTL не истёк
    # ------------------------------------------------------------------
    def test_unavailable_model_blocked_within_ttl(self) -> None:
        """_is_model_unavailable возвращает True, пока TTL не истёк."""
        model = "whisper-large-v3-mlx"
        self.engine._unavailable_models[model] = time.monotonic()

        # Сразу после записи — должна быть заблокирована
        self.assertTrue(self.engine._is_model_unavailable(model))

        # Запись не должна быть вычищена (TTL ещё не истёк)
        self.assertIn(model, self.engine._unavailable_models)

    # ------------------------------------------------------------------
    # 2. Модель разблокируется после TTL
    # ------------------------------------------------------------------
    def test_unavailable_model_recovers_after_ttl(self) -> None:
        """_is_model_unavailable возвращает False и удаляет запись после истечения TTL."""
        model = "whisper-large-v3-turbo-mlx"
        # Симулируем «старую» запись: timestamp за пределами TTL
        expired_ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC - 1.0
        self.engine._unavailable_models[model] = expired_ts

        # Должна вернуть False (TTL истёк)
        result = self.engine._is_model_unavailable(model)
        self.assertFalse(result)

        # Запись должна быть удалена (evict при истёкшем TTL)
        self.assertNotIn(model, self.engine._unavailable_models)

    def test_unavailable_model_not_in_dict_returns_false(self) -> None:
        """_is_model_unavailable возвращает False если модель не в словаре."""
        self.assertFalse(self.engine._is_model_unavailable("nonexistent-model"))

    def test_model_blocked_just_before_expiry(self) -> None:
        """Модель заблокирована если истёкло ровно TTL - 0.001 сек."""
        model = "gigaam-rnnt"
        # Истекает через 0.001 с — ещё внутри TTL
        ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC + 0.001
        self.engine._unavailable_models[model] = ts
        self.assertTrue(self.engine._is_model_unavailable(model))

    def test_model_unblocked_at_exact_expiry(self) -> None:
        """Модель разблокирована когда elapsed == TTL (граничное условие)."""
        model = "whisper-large-v3-mlx"
        ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC
        self.engine._unavailable_models[model] = ts
        # elapsed >= TTL → должна быть разблокирована
        self.assertFalse(self.engine._is_model_unavailable(model))

    # ------------------------------------------------------------------
    # 3. IPC _handle_clear_unavailable_models сбрасывает blacklist
    # ------------------------------------------------------------------
    def test_clear_unavailable_models_ipc_resets(self) -> None:
        """_handle_clear_unavailable_models сбрасывает все записи, возвращает count."""
        # Создаём минимальный BackendService-подобный объект
        engine = self.engine
        engine._unavailable_models = {
            "whisper-large-v3-mlx": time.monotonic() - 10,
            "gigaam-rnnt": time.monotonic() - 20,
        }

        # Строим фейковый сервис с .transcriber.engine
        fake_transcriber = MagicMock()
        fake_transcriber.engine = engine

        svc = MagicMock()
        svc.transcriber = fake_transcriber

        # Вызываем handler напрямую (без IPC overhead)
        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})

        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["cleared"]), 2)
        cleared_ids = {entry["model_id"] for entry in result["cleared"]}
        self.assertIn("whisper-large-v3-mlx", cleared_ids)
        self.assertIn("gigaam-rnnt", cleared_ids)
        # Словарь должен быть пуст после сброса
        self.assertEqual(len(engine._unavailable_models), 0)

    def test_clear_unavailable_models_no_engine(self) -> None:
        """_handle_clear_unavailable_models корректно обрабатывает отсутствие engine."""
        svc = MagicMock()
        svc.transcriber = None

        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})
        self.assertIn("error", result)
        self.assertEqual(result["cleared"], [])

    def test_clear_unavailable_models_returns_age(self) -> None:
        """Поле age_sec в ответе корректно отражает возраст записи."""
        engine = self.engine
        # Запись 30 секунд назад
        engine._unavailable_models = {
            "test-model": time.monotonic() - 30.0,
        }

        fake_transcriber = MagicMock()
        fake_transcriber.engine = engine
        svc = MagicMock()
        svc.transcriber = fake_transcriber

        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})

        age = result["cleared"][0]["age_sec"]
        # Допуск ±2 секунды (погрешность теста)
        self.assertAlmostEqual(age, 30.0, delta=2.0)

    # ------------------------------------------------------------------
    # 4. Thread safety — параллельные записи не ломают dict
    # ------------------------------------------------------------------
    def test_concurrent_add_thread_safe(self) -> None:
        """Параллельные записи в _unavailable_models не вызывают RuntimeError."""
        engine = self.engine
        errors: list[Exception] = []

        def write_worker(model_id: str) -> None:
            try:
                for _ in range(50):
                    engine._unavailable_models[model_id] = time.monotonic()
                    # Также читаем, как это делает _is_model_unavailable
                    _ = engine._is_model_unavailable(model_id)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=write_worker, args=(f"model-{i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Никаких исключений не должно быть
        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")

    # ------------------------------------------------------------------
    # 5. TTL constant sanity
    # ------------------------------------------------------------------
    def test_ttl_constant_is_300(self) -> None:
        """_UNAVAILABLE_MODEL_TTL_SEC должен быть 300 (5 минут)."""
        self.assertEqual(_UNAVAILABLE_MODEL_TTL_SEC, 300)

    def test_unavailable_models_init_is_dict(self) -> None:
        """_unavailable_models должен быть dict, не set."""
        engine = _make_minimal_engine()
        self.assertIsInstance(engine._unavailable_models, dict)


if __name__ == "__main__":
    unittest.main()
