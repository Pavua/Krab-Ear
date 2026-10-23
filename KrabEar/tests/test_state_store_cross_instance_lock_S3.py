"""Два StateStore на одном пути: конфликт flock ограничен тайм-аутом.

Реентерабельность остаётся per-instance: второй экземпляр не может войти
в эксклюзивный flock первого, даже из того же треда. Исторический тест
ожидал вечный дедлок и оставлял daemon-поток; после появления 30s timeout
тот падал уже во время соседних тестов. Проверяем действующий контракт
с реальным flock и нулевым дедлайном, без фоновых потоков и sleep.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.state_store import StateStore, StateStoreLockTimeout  # noqa: E402


class TestCrossInstanceLockTimeout(unittest.TestCase):
    def test_same_thread_second_instance_times_out_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_a = StateStore(Path(tmp))
            store_b = StateStore(Path(tmp))
            with store_a._lock():
                for _ in range(2):
                    with self.assertRaises(StateStoreLockTimeout):
                        with store_b._lock(timeout_sec=0):
                            self.fail("Второй экземпляр обошёл удерживаемый flock")

            # Ошибка захвата не оставляет ложную reentry depth или занятый fd.
            with store_b._lock(timeout_sec=0):
                with self.assertRaises(StateStoreLockTimeout):
                    with store_a._lock(timeout_sec=0):
                        self.fail("Первый экземпляр обошёл flock второго")
            with store_a._lock(timeout_sec=0):
                pass


if __name__ == "__main__":
    unittest.main()
