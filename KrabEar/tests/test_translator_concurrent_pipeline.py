"""W926 F2 HIGH regression test: concurrent first-translate must build pipeline exactly once.

Воспроизводит race condition из аудита W926: N потоков одновременно вызывают translate()
с одной языковой парой до заполнения кэша — _build_pipeline должен быть вызван ровно 1 раз.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translator import Translator  # noqa: E402


class ConcurrentPipelineBuildTest(unittest.TestCase):
    """Проверяет что double-checked locking предотвращает двойную загрузку модели."""

    def test_concurrent_first_translate_builds_once(self) -> None:
        """N потоков с одной языковой парой — _build_pipeline вызывается ровно 1 раз."""
        N = 8  # достаточно для выявления race без замедления CI
        build_call_count = 0
        build_count_lock = threading.Lock()

        original_build = Translator._build_pipeline

        def slow_fake_builder(model_name: str, allow_network: bool):
            nonlocal build_call_count
            # Искусственная задержка обнажает race window.
            time.sleep(0.05)
            with build_count_lock:
                build_call_count += 1

            def fake_pipeline(text: str):
                return [{"translation_text": f"TRANSLATED:{text}"}]

            return fake_pipeline

        translator = Translator()
        Translator._build_pipeline = staticmethod(slow_fake_builder)
        errors: list[Exception] = []

        def worker():
            try:
                result = translator.translate(
                    "тест конкуренции",
                    mode="ru_to_es",
                    network_mode="offline_default",
                )
                # Каждый поток должен получить успешный результат.
                assert result.status == "ok", f"Unexpected status: {result.status}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        finally:
            Translator._build_pipeline = original_build

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertEqual(
            build_call_count,
            1,
            f"_build_pipeline called {build_call_count} times instead of 1 "
            "(W926 F2: double-init race not fixed)",
        )

    def test_different_pairs_build_independently(self) -> None:
        """Разные языковые пары строятся независимо и не блокируют друг друга."""
        modes = ["ru_to_es", "en_to_ru", "es_to_ru"]
        build_counts: dict[str, int] = {m: 0 for m in modes}
        counts_lock = threading.Lock()

        original_build = Translator._build_pipeline

        def counting_builder(model_name: str, allow_network: bool):
            time.sleep(0.02)
            # Определяем режим по имени модели для счётчика.
            for m, mname in Translator._MODEL_BY_MODE.items():
                if mname == model_name:
                    with counts_lock:
                        if m in build_counts:
                            build_counts[m] += 1
                    break

            def fake_pipeline(text: str):
                return [{"translation_text": f"OK:{text}"}]

            return fake_pipeline

        translator = Translator()
        Translator._build_pipeline = staticmethod(counting_builder)
        errors: list[Exception] = []

        def worker(mode: str):
            try:
                for _ in range(4):
                    result = translator.translate("test", mode=mode, network_mode="offline_default")
                    assert result.status == "ok", f"mode={mode} status={result.status}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(m,)) for m in modes for _ in range(3)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            Translator._build_pipeline = original_build

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        # Каждая пара должна быть построена ровно 1 раз.
        for mode in modes:
            self.assertEqual(
                build_counts[mode],
                1,
                f"mode={mode} built {build_counts[mode]} times instead of 1",
            )

    def test_pipeline_lock_helper_creates_distinct_locks(self) -> None:
        """_get_pipeline_lock возвращает разные объекты Lock для разных ключей."""
        translator = Translator()
        key_a = ("model-A", True)
        key_b = ("model-B", False)

        lock_a1 = translator._get_pipeline_lock(key_a)
        lock_a2 = translator._get_pipeline_lock(key_a)  # повторный вызов
        lock_b = translator._get_pipeline_lock(key_b)

        # Один и тот же ключ → один и тот же Lock.
        self.assertIs(lock_a1, lock_a2)
        # Разные ключи → разные Lock.
        self.assertIsNot(lock_a1, lock_b)

    def test_pipeline_lock_is_acquirable(self) -> None:
        """Lock, возвращённый _get_pipeline_lock, реально работает как threading.Lock."""
        translator = Translator()
        key = ("test-model", False)
        lock = translator._get_pipeline_lock(key)

        acquired = lock.acquire(blocking=False)
        self.assertTrue(acquired, "Lock должен быть свободен при первом acquire")
        # Второй acquire в том же потоке должен провалиться (это обычный Lock, не RLock).
        acquired2 = lock.acquire(blocking=False)
        self.assertFalse(acquired2, "Lock должен быть занят после первого acquire")
        lock.release()


if __name__ == "__main__":
    unittest.main()
