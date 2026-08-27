"""Волна «передача mlx_lock брошенному потоку» (2026-08-27).

Живой инцидент: две потерянные диктовки владельца подряд, три
`handle_request завис дольше 180с` и рабочее состояние, восстановимое только
перезапуском backend. `sample` зависшего процесса: четыре брошенных потока
стоят на `rlock_acquire` (mlx_lock), при этом ВСЕ потоки libmlx спят — GPU не
занят, замок просто не отпущен.

Корень: `_run_with_timeout` при таймауте делает
`executor.shutdown(wait=False, cancel_futures=True)` и сразу `raise`. Вызывающий
выходит из `with mlx_inter_process_lock(), mlx_lock()`, отпуская замок, пока
брошенный поток ещё внутри Metal-вызова. Следующая транскрипция берёт замок и
запускает ВТОРОЙ параллельный MLX-инференс — ровно тот конкурентный доступ,
ради запрета которого mlx_lock и существует.

Сиблинг `core/mlx_subprocess.py::MLXWatchdog.run_with_timeout` этот же баг уже
чинил (W1358 F1 MED): bounded join, удерживающий замок, ДО raise.

🔴 Критерий здесь СТРУКТУРНЫЙ, а не по wallclock: проверяем не «сколько
секунд прошло», а «завершился ли рабочий поток к моменту броска исключения».
Замер времени в таком тесте дал бы флейк на загруженной машине.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.mlx_subprocess import MLXTimeoutError  # noqa: E402
from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter  # noqa: E402


class LockHeldUntilWorkerFinishesTests(unittest.TestCase):
    """Замок нельзя отпускать, пока брошенный поток ещё в Metal-вызове."""

    def test_worker_is_finished_when_timeout_is_raised(self):
        """Поток, успевший умереть в hard-kill окно, обязан быть мёртв к моменту raise."""
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        finished = threading.Event()
        release = threading.Event()

        def slow():
            release.wait(timeout=5.0)
            finished.set()
            return "готово"

        # Отпускаем работу заведомо ПОСЛЕ срабатывания watchdog, но задолго до
        # конца hard-kill окна — то есть поток успевает завершиться штатно.
        threading.Timer(0.3, release.set).start()

        with self.assertRaises(MLXTimeoutError):
            adapter._run_with_timeout(slow)

        self.assertTrue(
            finished.is_set(),
            "MLXTimeoutError брошен, пока рабочий поток ещё внутри Metal-вызова — "
            "вызывающий выйдет из with mlx_lock() и отпустит замок под живым потоком",
        )

    def test_executor_not_replaced_while_worker_alive(self):
        """Executor нельзя пересоздавать под ЖИВЫМ потоком — иначе следующий
        вызов пойдёт в новый поток параллельно старому.

        Сценарий именно «поток не умер»: если он успел завершиться, Metal
        свободен и пересоздание executor'а безопасно (проверяется отдельно
        в test_worker_is_finished_when_timeout_is_raised).
        """
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        forever = threading.Event()
        self.addCleanup(forever.set)

        import core.pipeline.stt_gigaam_mlx as mod
        original = getattr(mod, "MLX_HANG_HARD_KILL_SEC", None)
        mod.MLX_HANG_HARD_KILL_SEC = 0.2
        self.addCleanup(lambda: setattr(mod, "MLX_HANG_HARD_KILL_SEC", original))

        old_executor = None
        try:
            adapter._run_with_timeout(lambda: forever.wait(timeout=30.0))
        except MLXTimeoutError:
            old_executor = adapter._executor

        self.assertIsNotNone(
            old_executor,
            "executor обнулён при живом рабочем потоке — старый поток остался "
            "без владельца, а новый вызов создаст второй параллельный инференс",
        )

    def test_hung_worker_still_raises_and_does_not_stall_forever(self):
        """Поток, не умерший за hard-kill окно, не должен вешать вызывающего навсегда."""
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        forever = threading.Event()  # никогда не ставится
        self.addCleanup(forever.set)  # отпускаем поток при выходе из теста

        import core.pipeline.stt_gigaam_mlx as mod

        original = getattr(mod, "MLX_HANG_HARD_KILL_SEC", None)
        mod.MLX_HANG_HARD_KILL_SEC = 0.2
        self.addCleanup(
            lambda: setattr(mod, "MLX_HANG_HARD_KILL_SEC", original)
            if original is not None
            else None
        )

        with self.assertRaises(MLXTimeoutError):
            adapter._run_with_timeout(lambda: forever.wait(timeout=30.0))

    def test_poisoned_after_unrecoverable_hang(self):
        """Не умерший за hard-kill поток обязан пометить адаптер отравленным.

        Иначе следующая диктовка снова стартует инференс параллельно живому
        потоку — именно повторяемость превращает узкое SIGSEGV-окно в
        гарантированную поломку до перезапуска процесса.
        """
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        forever = threading.Event()
        self.addCleanup(forever.set)

        import core.pipeline.stt_gigaam_mlx as mod

        original = getattr(mod, "MLX_HANG_HARD_KILL_SEC", None)
        mod.MLX_HANG_HARD_KILL_SEC = 0.2
        self.addCleanup(
            lambda: setattr(mod, "MLX_HANG_HARD_KILL_SEC", original)
            if original is not None
            else None
        )

        with self.assertRaises(MLXTimeoutError):
            adapter._run_with_timeout(lambda: forever.wait(timeout=30.0))

        self.assertTrue(
            adapter.is_poisoned(),
            "адаптер не помечен отравленным — следующий вызов запустит второй "
            "параллельный MLX-инференс поверх живого зависшего потока",
        )

    def test_poisoned_adapter_fails_fast_instead_of_waiting(self):
        """Отравленный адаптер отвечает отказом сразу, чтобы каскад STT успел
        уйти на резервный движок, а не упёрся в 180-секундный IPC-backstop."""
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        adapter._mark_poisoned("тест")

        with self.assertRaises(MLXTimeoutError):
            adapter._run_with_timeout(lambda: "не должно исполниться")

    def test_poison_clears_when_stuck_worker_finally_dies(self):
        """Выход из отравленного состояния: как только зависший поток умер,
        Metal свободен и движок обязан снова стать доступен — без перезапуска
        backend. Липкое состояние без выхода — известный класс проекта."""
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=0.05)
        release = threading.Event()
        self.addCleanup(release.set)

        import core.pipeline.stt_gigaam_mlx as mod
        original = getattr(mod, "MLX_HANG_HARD_KILL_SEC", None)
        mod.MLX_HANG_HARD_KILL_SEC = 0.2
        self.addCleanup(lambda: setattr(mod, "MLX_HANG_HARD_KILL_SEC", original))

        with self.assertRaises(MLXTimeoutError):
            adapter._run_with_timeout(lambda: release.wait(timeout=30.0))
        self.assertTrue(adapter.is_poisoned(), "должен быть отравлен, пока поток жив")

        release.set()
        for _ in range(100):
            if not adapter.is_poisoned():
                break
            time.sleep(0.02)
        self.assertFalse(
            adapter.is_poisoned(),
            "поток умер, Metal свободен, а адаптер остался отравлен навсегда",
        )

    def test_successful_run_leaves_adapter_clean(self):
        """Штатный путь не должен ничего отравлять."""
        adapter = GigaAMMLXAdapter(watchdog_timeout_sec=5.0)
        self.assertEqual(adapter._run_with_timeout(lambda: "ок"), "ок")
        self.assertFalse(adapter.is_poisoned())


if __name__ == "__main__":
    unittest.main()
