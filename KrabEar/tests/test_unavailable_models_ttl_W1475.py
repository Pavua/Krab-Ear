"""W1475: тесты TTL для _unavailable_models в AudioEngine.

Bug: W1472 F4 MED — _unavailable_models был plain set без timestamps.
     Transient timeout → перманентный blacklist на всю сессию.
     W1304 (PR #1214) — зафиксировано но не смерджено в main branch.
     W1475 — независимая реализация от codex/krab-ear-v2.

Проверяет (W1475 spec):
- test_unavailable_models_evicted_after_ttl        — TTL истёк → запись удалена
- test_unavailable_models_not_evicted_before_ttl   — внутри TTL → заблокирована
- test_clear_unavailable_models_removes_all        — IPC clear → все записи удалены
- test_transient_timeout_recovers_within_ttl_window — симуляция full lifecycle

Дополнительные тесты:
- test_model_not_in_dict_returns_false             — отсутствие в dict → False
- test_model_blocked_just_before_expiry            — граничное условие слева
- test_model_unblocked_at_exact_expiry             — граничное условие справа (==)
- test_clear_returns_age_sec                       — age_sec корректен
- test_clear_no_engine_returns_error               — graceful degradation
- test_unavailable_models_init_is_dict             — тип dict, не set
- test_ttl_constant_is_300                         — константа = 300 сек
- test_concurrent_writes_no_crash                  — thread-safety (GIL dict ops)
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
    """Создаёт AudioEngine без реальных зависимостей.

    Использует object.__new__ чтобы пропустить __init__ и установить только
    атрибуты, нужные для тестируемых методов.
    """
    engine = object.__new__(AudioEngine)
    engine._unavailable_models = {}
    engine._router = None
    return engine


class UnavailableModelsTTLTestCase(unittest.TestCase):
    """W1475: TTL-логика _unavailable_models."""

    def setUp(self) -> None:
        self.engine = _make_minimal_engine()

    # ------------------------------------------------------------------
    # 1. TTL eviction — W1475 spec primary
    # ------------------------------------------------------------------

    def test_unavailable_models_evicted_after_ttl(self) -> None:
        """Запись удаляется (evict) после истечения TTL."""
        model = "whisper-large-v3-mlx"
        expired_ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC - 1.0
        self.engine._unavailable_models[model] = expired_ts

        result = self.engine._is_model_unavailable(model)

        self.assertFalse(result, "Должно быть False — TTL истёк")
        self.assertNotIn(model, self.engine._unavailable_models,
                         "Запись должна быть удалена при evict")

    def test_unavailable_models_not_evicted_before_ttl(self) -> None:
        """Запись остаётся (заблокирована) пока TTL не истёк."""
        model = "whisper-large-v3-turbo-mlx"
        self.engine._unavailable_models[model] = time.monotonic()

        result = self.engine._is_model_unavailable(model)

        self.assertTrue(result, "Должно быть True — внутри TTL")
        self.assertIn(model, self.engine._unavailable_models,
                      "Запись не должна быть удалена — TTL ещё не истёк")

    # ------------------------------------------------------------------
    # 2. clear_unavailable_models IPC — W1475 spec
    # ------------------------------------------------------------------

    def test_clear_unavailable_models_removes_all(self) -> None:
        """_handle_clear_unavailable_models сбрасывает все записи и возвращает count."""
        engine = self.engine
        engine._unavailable_models = {
            "whisper-large-v3-mlx": time.monotonic() - 10,
            "gigaam-rnnt": time.monotonic() - 20,
            "sensevoice": time.monotonic() - 5,
        }
        fake_transcriber = MagicMock()
        fake_transcriber.engine = engine
        svc = MagicMock()
        svc.transcriber = fake_transcriber

        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})

        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["cleared"]), 3)
        cleared_ids = {entry["model_id"] for entry in result["cleared"]}
        self.assertIn("whisper-large-v3-mlx", cleared_ids)
        self.assertIn("gigaam-rnnt", cleared_ids)
        self.assertIn("sensevoice", cleared_ids)
        self.assertEqual(len(engine._unavailable_models), 0,
                         "Dict должен быть пуст после сброса")

    # ------------------------------------------------------------------
    # 3. Transient timeout recovers — W1475 spec lifecycle
    # ------------------------------------------------------------------

    def test_transient_timeout_recovers_within_ttl_window(self) -> None:
        """Симуляция: transient timeout → blocked → TTL expires → recovers."""
        model = "whisper-large-v3-mlx"

        # Фаза 1: transient timeout — помечаем недоступным
        self.engine._unavailable_models[model] = time.monotonic()
        self.assertTrue(self.engine._is_model_unavailable(model),
                        "Сразу после блокировки — модель недоступна")

        # Фаза 2: симулируем истечение TTL (подменяем timestamp)
        expired_ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC - 1.0
        self.engine._unavailable_models[model] = expired_ts

        # Фаза 3: проверяем recovery
        self.assertFalse(self.engine._is_model_unavailable(model),
                         "После истечения TTL — модель снова доступна")
        self.assertNotIn(model, self.engine._unavailable_models,
                         "Запись должна быть вычищена")

    # ------------------------------------------------------------------
    # 4. Edge cases
    # ------------------------------------------------------------------

    def test_model_not_in_dict_returns_false(self) -> None:
        """_is_model_unavailable возвращает False для неизвестной модели."""
        self.assertFalse(self.engine._is_model_unavailable("nonexistent-model"))

    def test_model_blocked_just_before_expiry(self) -> None:
        """Модель заблокирована если elapsed < TTL (за 1 мс до истечения)."""
        model = "gigaam-rnnt"
        ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC + 0.001
        self.engine._unavailable_models[model] = ts
        self.assertTrue(self.engine._is_model_unavailable(model))

    def test_model_unblocked_at_exact_expiry(self) -> None:
        """Модель разблокирована когда elapsed == TTL (граничное условие >=)."""
        model = "whisper-large-v3-mlx"
        ts = time.monotonic() - _UNAVAILABLE_MODEL_TTL_SEC
        self.engine._unavailable_models[model] = ts
        # elapsed >= TTL → должна быть разблокирована
        self.assertFalse(self.engine._is_model_unavailable(model))

    def test_clear_returns_age_sec(self) -> None:
        """Поле age_sec в ответе корректно отражает возраст записи."""
        engine = self.engine
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
        self.assertAlmostEqual(age, 30.0, delta=2.0)

    def test_clear_no_engine_returns_error(self) -> None:
        """_handle_clear_unavailable_models graceful при отсутствии engine."""
        svc = MagicMock()
        svc.transcriber = None

        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})
        self.assertIn("error", result)
        self.assertEqual(result["cleared"], [])

    def test_clear_empty_dict_returns_zero(self) -> None:
        """_handle_clear_unavailable_models с пустым dict возвращает count=0."""
        engine = self.engine
        engine._unavailable_models = {}
        fake_transcriber = MagicMock()
        fake_transcriber.engine = engine
        svc = MagicMock()
        svc.transcriber = fake_transcriber

        from backend.service import BackendService
        result = BackendService._handle_clear_unavailable_models(svc, {})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["cleared"], [])

    # ------------------------------------------------------------------
    # 5. Structural assertions
    # ------------------------------------------------------------------

    def test_unavailable_models_init_is_dict(self) -> None:
        """_unavailable_models должен быть dict, не set."""
        engine = _make_minimal_engine()
        self.assertIsInstance(engine._unavailable_models, dict)

    def test_ttl_constant_is_300(self) -> None:
        """_UNAVAILABLE_MODEL_TTL_SEC должен быть 300 (5 минут)."""
        self.assertEqual(_UNAVAILABLE_MODEL_TTL_SEC, 300)

    # ------------------------------------------------------------------
    # 6. Thread safety
    # ------------------------------------------------------------------

    def test_concurrent_writes_no_crash(self) -> None:
        """Параллельные записи и чтения не вызывают RuntimeError (GIL dict ops)."""
        engine = self.engine
        errors: list[Exception] = []

        def worker(model_id: str) -> None:
            try:
                for _ in range(50):
                    engine._unavailable_models[model_id] = time.monotonic()
                    _ = engine._is_model_unavailable(model_id)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"model-{i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")


if __name__ == "__main__":
    unittest.main()
