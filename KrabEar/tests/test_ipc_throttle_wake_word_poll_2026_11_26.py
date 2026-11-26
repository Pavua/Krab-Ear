"""Опрос wake_word_status агентом каждые 0,25 с (4/с) не должен упираться в лимит IPC.

Живая приёмка 26.11: «light» 120/мин на метод (≈2/с) отбрасывал половину опросов —
~29 000 строк «IPC rate limit exceeded: method=wake_word_status» за 4 часа.
Метод только читает счётчики и последнее срабатывание, лимит ему не нужен.
"""
import unittest

from backend.ipc_throttle import EXCLUDED_METHODS, IPCThrottle


class WakeWordPollThrottleTests(unittest.TestCase):
    def test_wake_word_status_is_excluded_from_throttle(self):
        self.assertIn("wake_word_status", EXCLUDED_METHODS)

    def test_four_polls_per_second_for_a_minute_are_all_allowed(self):
        throttle = IPCThrottle()
        # 240 опросов подряд (минута при 4/с) — все разрешены, без учёта в статистике.
        self.assertTrue(all(throttle.check_rate("wake_word_status") for _ in range(240)))
        self.assertEqual(throttle.get_wait_time("wake_word_status"), 0.0)


if __name__ == "__main__":
    unittest.main()
