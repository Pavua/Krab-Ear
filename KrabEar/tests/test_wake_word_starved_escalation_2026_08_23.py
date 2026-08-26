"""Лестница эскалации при голодании стрима (T3 спеки PortAudio).

🔴 Находка ре-ревью (CRITICAL-2): guarded read делает тред прерываемым, но
сам по себе РВЁТ путь к эскалации. Проверено по коду: выход цикла зовёт
`_cleanup_session_after_loop_exit()`, тот зануляет `_active_model`, и watchdog
в `check_once` попадает в ветку «чистая пауза» → `_reset_episode()`. Плюс
каждая новая сессия получает grace-окно (`staleness < stale_sec`), до которого
умирающий за 3 с тред не доживает. Итог без этой волны: тихий вечный цикл
respawn → голодание → cooldown, в котором `wedged` недостижим — то есть вместо
«рестарт раз в день» подсистема молча не работала бы вообще.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.wake_word_watchdog import WakeWordWatchdog  # noqa: E402


class _FakeAdapter:
    """Адаптер после starve-выхода: сессии нет, но причина известна."""

    def __init__(self, *, starving: bool, streak: int):
        self._hb = {
            "last_chunk_ts": None,
            "listen_started_ts": None,
            "starvation_active": starving,
            "consecutive_starve_exits": streak,
        }
        self.wedged = False

    def is_running(self) -> bool:
        return False              # тред вышел — сессии нет

    def active_model(self):
        return None               # cleanup сессии обнулил модель

    def heartbeat(self) -> dict:
        return dict(self._hb)

    def set_wedged(self, value: bool) -> None:
        self.wedged = bool(value)

    def is_wedged(self) -> bool:
        return self.wedged


def _watchdog(adapter, *, recording: bool = False, clock_value: float = 1000.0):
    wd = WakeWordWatchdog(
        adapter=adapter,
        reinit_coordinator=MagicMock(),
        error_bus=MagicMock(),
        is_recording=lambda: recording,
        settings_get=lambda k, d: d,
        clock=lambda: clock_value,
    )
    return wd


class StarvedStreamEscalationTest(unittest.TestCase):
    def test_starvation_below_threshold_does_not_reset_episode(self):
        """Эпизод обязан ЖИТЬ между попытками — иначе лестница не наберётся."""
        adapter = _FakeAdapter(starving=True, streak=1)
        wd = _watchdog(adapter)
        wd.check_once()
        self.assertFalse(adapter.wedged)
        self.assertIsNotNone(
            wd._anomaly_since,
            "голодание — не «чистая пауза»: аномалия должна вестись",
        )

    def test_streak_reaches_threshold_escalates_to_wedged(self):
        adapter = _FakeAdapter(starving=True, streak=3)
        wd = _watchdog(adapter)
        result = wd.check_once()
        self.assertEqual(result, "escalated")
        self.assertTrue(adapter.wedged, "путь к wedged обязан быть живым")
        self.assertTrue(wd._error_bus.push.called, "ErrorBus обязан узнать о клине")

    def test_no_double_escalation_in_one_episode(self):
        adapter = _FakeAdapter(starving=True, streak=5)
        wd = _watchdog(adapter)
        wd.check_once()
        wd._error_bus.push.reset_mock()
        wd.check_once()
        self.assertFalse(wd._error_bus.push.called, "второй пуш той же ошибки не нужен")

    def test_recording_suppresses_starvation_escalation(self):
        """🔴 Встреча не снимает слушатель — голодание тогда легитимно."""
        adapter = _FakeAdapter(starving=True, streak=9)
        wd = _watchdog(adapter, recording=True)
        wd.check_once()
        self.assertFalse(adapter.wedged, "под записью эскалировать нельзя")

    def test_clean_pause_still_resets_episode(self):
        """Анти-регресс: обычная пауза (без голодания) ведёт себя как раньше."""
        adapter = _FakeAdapter(starving=False, streak=0)
        wd = _watchdog(adapter)
        wd._anomaly_since = 500.0
        wd.check_once()
        self.assertIsNone(wd._anomaly_since)
        self.assertFalse(adapter.wedged)


if __name__ == "__main__":
    unittest.main()
