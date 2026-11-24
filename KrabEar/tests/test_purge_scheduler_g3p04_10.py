"""G3-P04-10: поток авто-очистки не умирает от настроек, stop() не теряет живой поток."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.purge_scheduler import PurgeScheduler  # noqa: E402

FAST = 1 / 3600  # 1 секунда — минимальный интервал


class _Settings:
    """Настройки, которые первые ``fail_times`` чтений ключа ``fail_key`` бросают."""

    def __init__(self, values, fail_key=None, fail_times=0, exc=RuntimeError("settings store down")):
        self.values = dict(values)
        self.fail_key = fail_key
        self.fail_left = fail_times
        self.exc = exc
        self.lock = threading.Lock()

    def get(self, key, default=None):
        with self.lock:
            if key == self.fail_key and self.fail_left > 0:
                self.fail_left -= 1
                raise self.exc
        return self.values.get(key, default)


def _wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class SettingsFailuresDoNotKillThread(unittest.TestCase):
    def _run_until_purge(self, settings):
        purged = threading.Event()
        sched = PurgeScheduler(settings_get=settings.get, purge_fn=lambda d: purged.set() or 0)
        sched.start()
        self.addCleanup(sched.stop)
        return sched, purged

    def test_interval_read_exception_is_survived(self):
        s = _Settings({"auto_purge_enabled": True, "auto_purge_check_interval_hours": FAST},
                      fail_key="auto_purge_check_interval_hours", fail_times=1)
        sched, _purged = self._run_until_purge(s)
        self.assertTrue(_wait_for(lambda: s.fail_left == 0), "интервал так и не прочитан")
        time.sleep(0.2)
        # Сбой чтения → дефолтные 24 ч ожидания, поток жив.
        self.assertTrue(sched.get_status()["running"], "поток умер на исключении чтения интервала")

    def test_enabled_read_exception_is_survived(self):
        s = _Settings({"auto_purge_enabled": True, "auto_purge_check_interval_hours": FAST},
                      fail_key="auto_purge_enabled", fail_times=1)
        sched, purged = self._run_until_purge(s)
        self.assertTrue(purged.wait(6.0), "поток умер на исключении чтения auto_purge_enabled")

    def test_retention_read_exception_skips_purge_and_survives(self):
        calls = []
        s = _Settings({"auto_purge_enabled": True, "auto_purge_check_interval_hours": FAST,
                       "auto_purge_retention_days": 30},
                      fail_key="auto_purge_retention_days", fail_times=1, exc=OSError("eio"))
        sched = PurgeScheduler(settings_get=s.get, purge_fn=lambda d: calls.append(d) or 0)
        sched.start()
        self.addCleanup(sched.stop)
        self.assertTrue(_wait_for(lambda: calls), "поток умер на чтении срока хранения")
        # Тик со сбоем чтения срока пропущен, а не выполнен с выдуманным дефолтом.
        self.assertEqual(calls[0], 30)
        self.assertEqual(s.fail_left, 0)

    def test_infinite_interval_does_not_kill_thread(self):
        s = _Settings({"auto_purge_enabled": False, "auto_purge_check_interval_hours": float("inf")})
        sched = PurgeScheduler(settings_get=s.get, purge_fn=lambda d: 0)
        sched.start()
        self.addCleanup(sched.stop)
        time.sleep(0.3)
        self.assertTrue(sched.get_status()["running"], "Event.wait(inf) → OverflowError убил поток")

    def test_nan_interval_falls_back_to_default_not_one_second(self):
        calls = []
        s = _Settings({"auto_purge_enabled": True, "auto_purge_check_interval_hours": float("nan")})
        sched = PurgeScheduler(settings_get=s.get, purge_fn=lambda d: calls.append(d) or 0)
        sched.start()
        self.addCleanup(sched.stop)
        time.sleep(2.5)
        # max(1.0, nan) == 1.0: без проверки конечности очистка шла бы раз в секунду.
        self.assertEqual(calls, [])

    def test_non_bool_enabled_does_not_enable_purge(self):
        calls = []
        s = _Settings({"auto_purge_enabled": "false", "auto_purge_check_interval_hours": FAST})
        sched = PurgeScheduler(settings_get=s.get, purge_fn=lambda d: calls.append(d) or 0)
        sched.start()
        self.addCleanup(sched.stop)
        time.sleep(2.5)
        self.assertEqual(calls, [], "строка 'false' не должна включать удаление истории")
        self.assertFalse(sched.get_status()["enabled"])


class StopKeepsLiveThread(unittest.TestCase):
    def test_stop_timeout_keeps_reference_and_blocks_second_thread(self):
        entered = threading.Event()
        release = threading.Event()
        concurrent = []
        active = [0]
        lock = threading.Lock()

        def slow_purge(days):
            with lock:
                active[0] += 1
                concurrent.append(active[0])
            entered.set()
            release.wait(10)
            with lock:
                active[0] -= 1
            return 0

        s = _Settings({"auto_purge_enabled": True, "auto_purge_check_interval_hours": FAST})
        sched = PurgeScheduler(settings_get=s.get, purge_fn=slow_purge)
        sched._STOP_JOIN_TIMEOUT_SEC = 0.2
        sched.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(6.0))

        self.assertFalse(sched.stop(), "stop() обязан сообщить, что поток ещё жив")
        self.assertTrue(sched.get_status()["running"], "живой поток потерян из учёта")
        sched.start()  # не должен породить второй поток рядом с работающей очисткой
        alive = [t for t in threading.enumerate() if t.name == "PurgeScheduler" and t.is_alive()]
        self.assertEqual(len(alive), 1)

        release.set()
        time.sleep(0.5)
        self.assertTrue(sched.get_status()["running"], "start() после неудачного stop() обязан отменить остановку")
        self.assertTrue(_wait_for(lambda: sched.stop()), "после завершения очистки stop() → True")
        self.assertFalse(sched.get_status()["running"])
        self.assertEqual(max(concurrent), 1)

    def test_stop_returns_true_for_idle_and_never_started(self):
        s = _Settings({"auto_purge_check_interval_hours": 24})
        sched = PurgeScheduler(settings_get=s.get, purge_fn=lambda d: 0)
        self.assertTrue(sched.stop())
        sched.start()
        self.assertTrue(sched.stop())


class ServiceCloseReportsLivePurgeThread(unittest.TestCase):
    def test_close_returns_false_when_purge_thread_survives(self):
        import tempfile
        from unittest import mock

        from backend.service import BackendService
        from backend.state_store import StateStore

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        service = BackendService(store=StateStore(Path(tmp.name) / "data"))
        self.addCleanup(service.close)
        real_stop = service._purge_scheduler.stop
        with mock.patch.object(service._purge_scheduler, "stop", side_effect=lambda: real_stop() and False):
            self.assertFalse(service.close(), "живой поток очистки не даёт close() рапортовать успех")


if __name__ == "__main__":
    unittest.main()
