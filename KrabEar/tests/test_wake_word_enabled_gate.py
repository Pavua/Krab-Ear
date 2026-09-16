"""Гейт `wake_word_enabled` в OpenWakeWordAdapter.

Живой инцидент 2026-07-28/29: настройка `wake_word_enabled` в settings.json
НЕ читалась backend'ом вообще (0 вхождений вне config.py) — единственным
источником правды был UserDefaults-ключ Swift-агента. Из-за этого settings.json
показывал `False`, пока адаптер держал микрофон и слушал (`running: true`,
`hey_jarvis`), а попытка выключить фичу через set_settings/правку JSON не
давала НИЧЕГО.

Backend владеет микрофоном, поэтому гейт обязан стоять здесь — симметрично
уже существующему privacy-гейту (F2, test_openwakeword_security_W1210):
устаревший или сломанный агент не должен иметь возможности открыть тап
вопреки настройке.

Отдельно закреплено: гейт fail-OPEN по УМОЛЧАНИЮ (отсутствие ключа = разрешено).
Это намеренно — иначе первый же запуск с чистым settings.json тихо сломал бы
работающий у пользователя wake word, а миграцию значения делает агент.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import (  # noqa: E402
    OpenWakeWordAdapter,
    WakeWordDisabledError,
)
from backend.audio_reinit import AudioReinitCoordinator  # noqa: E402


def _make_adapter(tmp_dir: str | Path, settings: dict | None = None) -> OpenWakeWordAdapter:
    settings = settings or {}
    adapter = OpenWakeWordAdapter(
        data_dir=tmp_dir,
        settings_get=lambda k, d: settings.get(k, d),
    )
    adapter._oww_available = False
    return adapter


class TestWakeWordEnabledGate(unittest.TestCase):
    """`wake_word_enabled=False` обязан блокировать открытие микрофона."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_disabled_blocks_wake_word_start(self) -> None:
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": False})
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), "wake word disabled in settings")

    def test_enabled_proceeds_past_the_gate(self) -> None:
        """С enabled=True гейт не срабатывает — отказ приходит уже от движка."""
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": True})
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), "wake word disabled in settings")

    def test_missing_key_defaults_to_allowed(self) -> None:
        """Отсутствие ключа НЕ выключает фичу: миграцию значения делает агент.

        Fail-closed здесь означал бы тихую поломку работающего wake word у
        всех, у кого ключ ещё не синхронизирован из UserDefaults.
        """
        adapter = _make_adapter(self._tmp, settings={})
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), "wake word disabled in settings")

    def test_privacy_gate_still_wins_over_enabled(self) -> None:
        """privacy_mode всегда побеждает — даже при wake_word_enabled=True."""
        adapter = _make_adapter(
            self._tmp,
            settings={"wake_word_enabled": True, "privacy_mode_enabled": True},
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), "cannot activate wake-word in privacy mode")


def _make_coordinator(adapter) -> AudioReinitCoordinator:
    return AudioReinitCoordinator(
        reinit_audio_backend=lambda: None,
        is_recording=lambda: False,
        wake_word_adapter=adapter,
    )


class TestWakeWordStartGate(unittest.TestCase):
    """Прямые вызовы start() подчиняются тому же гейту, что IPC (F5b)."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_start_raises_when_disabled(self) -> None:
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": False})
        with self.assertRaises(WakeWordDisabledError):
            adapter.start("hey_jarvis", lambda *a: None)

    def test_start_gate_missing_key_defaults_to_allowed(self) -> None:
        """Нет ключа — не DisabledError (дальше штатный отказ движка)."""
        adapter = _make_adapter(self._tmp, settings={})
        with self.assertRaises(RuntimeError) as ctx:
            adapter.start("hey_jarvis", lambda *a: None)
        self.assertNotIsInstance(ctx.exception, WakeWordDisabledError)

    def test_restore_skips_when_disabled(self) -> None:
        """Restore при выключенной фиче = нечего восстанавливать (True)."""
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": False})
        coordinator = _make_coordinator(adapter)
        self.assertTrue(
            coordinator._restore_listener(adapter, True, "hey_jarvis", None, None)
        )
        self.assertFalse(adapter.is_running())


class _FakeListener:
    def __init__(self, running=True, model="hey_jarvis"):
        self._running = running
        self._model = model
        self.stopped = 0

    def is_running(self):
        return self._running

    def active_model(self):
        return self._model

    def heartbeat(self):
        return {}

    def set_wedged(self, value):
        pass

    def stop(self, timeout=3.0):
        self.stopped += 1
        self._running = False
        self._model = None
        return True


def _make_watchdog(listener, settings, clock=None):
    from unittest.mock import MagicMock

    from backend.wake_word_watchdog import WakeWordWatchdog

    kw = dict(adapter=listener, reinit_coordinator=MagicMock(),
              settings_get=lambda k, d: settings.get(k, d))
    if clock is not None:
        kw["clock"] = clock
    return WakeWordWatchdog(**kw)


class TestWatchdogDisabledFeature(unittest.TestCase):
    def test_stops_live_listener_when_disabled(self) -> None:
        wd = _make_watchdog(_FakeListener(running=True),
                            {"wake_word_enabled": False})
        self.assertEqual(wd.check_once(), "stopped_disabled")
        self.assertFalse(wd._adapter.is_running())
        self.assertEqual(wd._adapter.stopped, 1)

    def test_no_resurrect_when_disabled(self) -> None:
        now = [1000.0]
        wd = _make_watchdog(_FakeListener(running=False),
                            {"wake_word_enabled": False},
                            clock=lambda: now[0])
        self.assertIsNone(wd.check_once())
        now[0] += 10000.0
        self.assertIsNone(wd.check_once())
        self.assertFalse(wd._escalated_this_episode)

    def test_ignores_healthy_when_enabled(self) -> None:
        listener = _FakeListener(running=True)
        wd = _make_watchdog(listener, {})
        self.assertIsNone(wd.check_once())
        self.assertEqual(listener.stopped, 0)


if __name__ == "__main__":
    unittest.main()
