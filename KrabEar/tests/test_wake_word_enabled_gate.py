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

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
