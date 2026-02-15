"""Тесты хранилища состояния Krab Ear.

Проверяем критичный контракт:
1) сохраняются только последние 5 транскрибаций;
2) дефолтные настройки всегда доступны;
3) битый JSON не ломает приложение.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.storage import AppState, AppStorage


class AppStorageTestCase(unittest.TestCase):
    """Проверки сериализации и нормализации состояния."""

    def test_push_history_keeps_only_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = AppStorage(state_file=Path(tmp) / "state.json", max_history=5)
            state = AppState()

            for idx in range(9):
                state = storage.push_history(state, f"строка-{idx}")
            storage.save(state)

            loaded = storage.load()
            self.assertEqual(len(loaded.history), 5)
            self.assertEqual(loaded.history[0].text, "строка-4")
            self.assertEqual(loaded.history[-1].text, "строка-8")

    def test_load_returns_defaults_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text("{not-valid-json", encoding="utf-8")
            storage = AppStorage(state_file=state_file, max_history=5)

            loaded = storage.load()
            self.assertEqual(loaded.history, [])
            self.assertIn("auto_paste", loaded.settings)
            self.assertIn("toggle_mode", loaded.settings)

    def test_save_and_load_preserves_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            storage = AppStorage(state_file=state_file, max_history=5)
            state = AppState(
                settings={
                    "always_on_top": True,
                    "auto_paste": False,
                    "toggle_mode": False,
                    "play_start_sound": True,
                },
                history=[],
            )
            storage.save(state)

            loaded = storage.load()
            self.assertTrue(loaded.settings["always_on_top"])
            self.assertFalse(loaded.settings["auto_paste"])
            self.assertFalse(loaded.settings["toggle_mode"])
            self.assertTrue(loaded.settings["play_start_sound"])


if __name__ == "__main__":
    unittest.main()
