"""Regression tests for MLX thread-safety fix.

Проверяет, что глобальный RLock из core.mlx_lock корректно сериализует
все конкурентные MLX inference вызовы, предотвращая SIGSEGV при
concurrent __hash_table<MTL::Resource*> access в libmlx.dylib.

Краш-репорт: ~/Library/Logs/DiagnosticReports/Python-2026-04-19-213636.ips
"""
import sys
import os
import threading
import time
import concurrent.futures
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.mlx_lock import mlx_lock, _mlx_lock


class TestMlxLockSerialization(unittest.TestCase):
    """Убеждаемся что lock действительно сериализует MLX-работу по потокам."""

    def test_concurrent_access_serialized(self):
        """10 потоков пытаются захватить MLX lock — каждый start немедленно предшествует своему end."""
        order = []
        lock_obj = mlx_lock()

        def worker(worker_id):
            with lock_obj:
                order.append(f"start_{worker_id}")
                time.sleep(0.005)  # имитируем GPU работу
                order.append(f"end_{worker_id}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(worker, range(10)))

        # Всего 20 событий (start + end для каждого из 10 потоков)
        self.assertEqual(len(order), 20)

        # Каждый start должен непосредственно предшествовать его end (нет чередования)
        for i in range(0, 20, 2):
            start_event = order[i]
            end_event = order[i + 1]
            self.assertTrue(start_event.startswith("start_"), f"Ожидался start на позиции {i}, получил: {start_event}")
            worker_id = start_event.split("_")[1]
            self.assertEqual(end_event, f"end_{worker_id}",
                             f"start_{worker_id} должен быть сразу за end_{worker_id}, но получил {end_event}")

    def test_reentrant_same_thread(self):
        """Тот же поток может повторно захватить RLock без deadlock (fallback chain паттерн)."""
        lock_obj = mlx_lock()
        acquired = []
        with lock_obj:
            acquired.append(1)
            with lock_obj:  # повторный захват — не должен deadlock
                acquired.append(2)
        self.assertEqual(acquired, [1, 2])

    def test_mlx_lock_returns_same_instance(self):
        """mlx_lock() всегда возвращает один и тот же объект блокировки."""
        lock_a = mlx_lock()
        lock_b = mlx_lock()
        self.assertIs(lock_a, lock_b)
        self.assertIs(lock_a, _mlx_lock)

    def test_lock_is_rlock(self):
        """Блокировка является threading.RLock (reentrant), а не простым Lock."""
        lock_obj = mlx_lock()
        # RLock можно захватить дважды из одного потока; Lock — нет
        acquired = lock_obj.acquire(blocking=False)
        self.assertTrue(acquired)
        # Второй acquire в том же потоке — должен вернуть True немедленно
        acquired2 = lock_obj.acquire(blocking=False)
        self.assertTrue(acquired2, "RLock должен разрешать повторный захват из одного потока")
        lock_obj.release()
        lock_obj.release()

    def test_lock_blocks_other_thread(self):
        """Пока один поток держит lock, другой поток заблокирован."""
        lock_obj = mlx_lock()
        barrier = threading.Barrier(2)
        results = {}

        def holder():
            with lock_obj:
                barrier.wait()  # сигнализируем что lock захвачен
                time.sleep(0.05)  # держим lock
                results["holder_released"] = time.monotonic()

        def waiter():
            barrier.wait()  # ждём пока holder захватит lock
            t_start = time.monotonic()
            with lock_obj:
                results["waiter_acquired"] = time.monotonic()
                results["wait_duration"] = results["waiter_acquired"] - t_start

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        # Waiter должен был ждать не менее 40ms (holder держал 50ms)
        self.assertIn("wait_duration", results, "Waiter так и не захватил lock")
        self.assertGreater(results["wait_duration"], 0.03,
                           "Waiter должен был подождать пока holder освободит lock")


class TestEngineTranscribeUsesLock(unittest.TestCase):
    """Integration: _transcribe_model вызывает mlx_whisper под MLX lock."""

    def _make_engine(self):
        """Создаём AudioEngine без тяжёлых зависимостей через __new__."""
        # Патчим mlx_whisper перед импортом модуля чтобы движок его не искал
        with patch.dict("sys.modules", {"mlx_whisper": MagicMock()}):
            from core import engine as engine_mod
            eng = engine_mod.AudioEngine.__new__(engine_mod.AudioEngine)
            eng._unavailable_models = {}
        return eng

    def test_transcribe_model_acquires_mlx_lock(self):
        """_transcribe_model должен захватывать mlx_lock до вызова mlx_whisper.transcribe."""
        lock_acquisitions = []
        real_lock = mlx_lock()

        fake_mlx = MagicMock()

        def fake_transcribe(audio_data, **kwargs):
            # Проверяем что lock захвачен (нельзя захватить снова без блокировки из ДРУГОГО потока)
            # В текущем потоке RLock разрешает повторный захват — используем другой поток для проверки
            acquired_from_other = threading.Event()
            blocked = threading.Event()

            def probe():
                # Пробуем захватить lock НЕблокирующе из другого потока
                got = real_lock.acquire(blocking=False)
                if got:
                    real_lock.release()
                    acquired_from_other.set()  # lock не был захвачен основным потоком — плохо
                else:
                    blocked.set()  # lock захвачен — ожидаемо

            t = threading.Thread(target=probe)
            t.start()
            t.join(timeout=0.5)
            lock_acquisitions.append(blocked.is_set())
            return {"text": "тест", "segments": []}

        fake_mlx.transcribe = fake_transcribe

        with patch("core.engine.mlx_whisper", fake_mlx):
            from core import engine as engine_mod
            eng = engine_mod.AudioEngine.__new__(engine_mod.AudioEngine)
            eng._unavailable_models = {}

            import numpy as np
            audio = np.zeros(16000, dtype=np.float32)
            eng._transcribe_model(audio, "mlx-community/whisper-tiny", "")

        self.assertTrue(
            any(lock_acquisitions),
            "_transcribe_model должен удерживать mlx_lock во время вызова mlx_whisper.transcribe"
        )

    def test_no_concurrent_mlx_calls(self):
        """Два одновременных _transcribe_model вызова НЕ пересекаются в критической секции."""
        call_log = []
        first_entered = threading.Event()

        fake_mlx = MagicMock()

        call_count = [0]

        def fake_transcribe(audio_data, **kwargs):
            call_count[0] += 1
            n = call_count[0]
            call_log.append(f"enter_{n}")
            if n == 1:
                first_entered.set()
                time.sleep(0.04)  # первый держит lock
            call_log.append(f"exit_{n}")
            return {"text": "тест", "segments": []}

        fake_mlx.transcribe = fake_transcribe

        import numpy as np

        with patch("core.engine.mlx_whisper", fake_mlx):
            from core import engine as engine_mod

            def run_transcribe():
                eng = engine_mod.AudioEngine.__new__(engine_mod.AudioEngine)
                eng._unavailable_models = {}
                audio = np.zeros(16000, dtype=np.float32)
                eng._transcribe_model(audio, "mlx-community/whisper-tiny", "")

            t1 = threading.Thread(target=run_transcribe)
            t2 = threading.Thread(target=run_transcribe)
            t1.start()
            first_entered.wait(timeout=1.0)
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        # Убеждаемся что enter/exit не перемешаны (нет interleaving)
        self.assertEqual(len(call_log), 4, f"Ожидали 4 события, получили: {call_log}")
        # Первые два события должны быть enter_N и exit_N одного потока
        self.assertTrue(
            call_log[0].startswith("enter_") and call_log[1].startswith("exit_"),
            f"Ожидали enter-exit пару без interleaving, получили: {call_log}"
        )
        # Индекс второй пары
        first_id = call_log[0].split("_")[1]
        self.assertEqual(call_log[1], f"exit_{first_id}",
                         f"exit должен следовать сразу за enter, лог: {call_log}")


if __name__ == "__main__":
    unittest.main()
