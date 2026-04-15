"""Тесты SpeakerManager — псевдонимы спикеров диаризации."""

from __future__ import annotations
from backend.speaker_manager import SpeakerManager

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestSpeakerManagerCRUD(unittest.TestCase):
    """Базовые операции CRUD без персистентности."""

    def setUp(self):
        self.mgr = SpeakerManager()  # data_dir=None → in-memory

    def test_set_and_get_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_get_alias_missing_returns_none(self):
        self.assertIsNone(self.mgr.get_alias("SPEAKER_99"))

    def test_set_alias_overwrites(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "Павел")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Павел")

    def test_remove_alias_returns_true(self):
        self.mgr.set_alias("SPEAKER_01", "Маша")
        removed = self.mgr.remove_alias("SPEAKER_01")
        self.assertTrue(removed)
        self.assertIsNone(self.mgr.get_alias("SPEAKER_01"))

    def test_remove_missing_alias_returns_false(self):
        removed = self.mgr.remove_alias("SPEAKER_42")
        self.assertFalse(removed)

    def test_get_all_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        aliases = self.mgr.get_all_aliases()
        self.assertEqual(aliases, {"SPEAKER_00": "Паша", "SPEAKER_01": "Маша"})

    def test_get_all_aliases_returns_copy(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        aliases = self.mgr.get_all_aliases()
        aliases["SPEAKER_00"] = "Изменено"
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_set_empty_name_removes_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))


class TestSpeakerManagerApplyAliases(unittest.TestCase):
    """Применение псевдонимов к тексту транскрипции."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_apply_single_alias(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.apply_aliases("[SPEAKER_00] Привет мир")
        self.assertEqual(result, "[Паша] Привет мир")

    def test_apply_multiple_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_01", "Маша")
        text = "[SPEAKER_00] Привет\n[SPEAKER_01] Как дела?"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] Привет\n[Маша] Как дела?")

    def test_unknown_speaker_left_as_is(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "[SPEAKER_00] Привет [SPEAKER_01] Неизвестный"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] Привет [SPEAKER_01] Неизвестный")

    def test_apply_no_aliases_unchanged(self):
        text = "[SPEAKER_00] Текст без псевдонимов"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, text)

    def test_apply_empty_string(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.assertEqual(self.mgr.apply_aliases(""), "")

    def test_apply_text_without_tags(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "Обычный текст без тегов спикеров"
        self.assertEqual(self.mgr.apply_aliases(text), text)

    def test_apply_repeated_speaker(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        text = "[SPEAKER_00] раз [SPEAKER_00] два"
        result = self.mgr.apply_aliases(text)
        self.assertEqual(result, "[Паша] раз [Паша] два")


class TestSpeakerManagerPersistence(unittest.TestCase):
    """Персистентность псевдонимов в файл."""

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.set_alias("SPEAKER_00", "Паша")
            mgr1.set_alias("SPEAKER_01", "Маша")

            mgr2 = SpeakerManager(data_dir=tmpdir)
            self.assertEqual(mgr2.get_alias("SPEAKER_00"), "Паша")
            self.assertEqual(mgr2.get_alias("SPEAKER_01"), "Маша")

    def test_aliases_file_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SpeakerManager(data_dir=tmpdir)
            mgr.set_alias("SPEAKER_00", "Паша")
            aliases_path = Path(tmpdir) / "speaker_aliases.json"
            self.assertTrue(aliases_path.exists())
            data = json.loads(aliases_path.read_text(encoding="utf-8"))
            self.assertEqual(data["SPEAKER_00"], "Паша")

    def test_remove_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.set_alias("SPEAKER_00", "Паша")
            mgr1.remove_alias("SPEAKER_00")

            mgr2 = SpeakerManager(data_dir=tmpdir)
            self.assertIsNone(mgr2.get_alias("SPEAKER_00"))

    def test_empty_data_dir_no_crash(self):
        mgr = SpeakerManager(data_dir=None)
        mgr.set_alias("SPEAKER_00", "Паша")  # не должно упасть
        self.assertEqual(mgr.get_alias("SPEAKER_00"), "Паша")

    def test_missing_file_no_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SpeakerManager(data_dir=tmpdir)
            # Файл ещё не создан — get_alias должен вернуть None без исключения
            self.assertIsNone(mgr.get_alias("SPEAKER_00"))


class TestSpeakerManagerIPCHandlers(unittest.TestCase):
    """IPC-обработчики."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_handle_set_speaker_alias(self):
        result = self.mgr.handle_set_speaker_alias(
            {"speaker_id": "SPEAKER_00", "name": "Паша"}
        )
        self.assertEqual(result["speaker_id"], "SPEAKER_00")
        self.assertEqual(result["name"], "Паша")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")

    def test_handle_set_speaker_alias_missing_id(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({"name": "Паша"})

    def test_handle_set_speaker_alias_empty_name(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({"speaker_id": "SPEAKER_00", "name": ""})

    def test_handle_get_speaker_aliases(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.handle_get_speaker_aliases({})
        self.assertIn("aliases", result)
        self.assertEqual(result["aliases"]["SPEAKER_00"], "Паша")

    def test_handle_remove_speaker_alias_exists(self):
        self.mgr.set_alias("SPEAKER_00", "Паша")
        result = self.mgr.handle_remove_speaker_alias({"speaker_id": "SPEAKER_00"})
        self.assertTrue(result["removed"])
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_handle_remove_speaker_alias_missing(self):
        result = self.mgr.handle_remove_speaker_alias({"speaker_id": "SPEAKER_99"})
        self.assertFalse(result["removed"])

    def test_handle_remove_speaker_alias_no_id(self):
        with self.assertRaises(ValueError):
            self.mgr.handle_remove_speaker_alias({})


if __name__ == "__main__":
    unittest.main()
