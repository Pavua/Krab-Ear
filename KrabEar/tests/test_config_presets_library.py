"""Unit-тесты для ConfigPresetsLibrary."""

from __future__ import annotations
from backend.config_presets_library import ConfigPresetsLibrary, _BUILTIN_PRESETS

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ConfigPresetsLibraryBuiltinsTestCase(unittest.TestCase):
    """Тесты встроенных пресетов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._lib = ConfigPresetsLibrary(data_dir=self._tmpdir)

    def test_list_presets_includes_all_builtins(self) -> None:
        presets = self._lib.list_presets()
        names = {p["name"] for p in presets}
        for builtin_name in _BUILTIN_PRESETS:
            self.assertIn(builtin_name, names)

    def test_list_presets_returns_five_builtins(self) -> None:
        presets = self._lib.list_presets()
        builtin_presets = [p for p in presets if p.get("builtin") is True]
        self.assertEqual(len(builtin_presets), 5)

    def test_builtin_preset_has_required_fields(self) -> None:
        presets = self._lib.list_presets()
        for preset in presets:
            if not preset.get("builtin"):
                continue
            self.assertIn("name", preset)
            self.assertIn("description", preset)
            self.assertIn("settings_patch", preset)
            self.assertIsInstance(preset["settings_patch"], dict)

    def test_apply_interview_preset_returns_patch(self) -> None:
        patch = self._lib.apply_preset("interview")
        self.assertEqual(patch["quality_profile"], "max")
        self.assertTrue(patch["diarization_enabled"])
        self.assertTrue(patch["auto_title_enabled"])

    def test_apply_meeting_preset_returns_patch(self) -> None:
        patch = self._lib.apply_preset("meeting")
        self.assertEqual(patch["quality_profile"], "balanced")
        self.assertTrue(patch["diarization_enabled"])

    def test_apply_voice_memo_preset_no_diarization(self) -> None:
        patch = self._lib.apply_preset("voice_memo")
        self.assertFalse(patch["diarization_enabled"])
        self.assertEqual(patch["quality_profile"], "max")

    def test_apply_language_practice_translation_on(self) -> None:
        patch = self._lib.apply_preset("language_practice")
        self.assertNotEqual(patch.get("translation_mode"), "off")
        self.assertTrue(patch.get("language_learning_mode"))

    def test_apply_podcast_preset_obsidian_export(self) -> None:
        patch = self._lib.apply_preset("podcast")
        self.assertTrue(patch.get("obsidian_export_enabled"))
        self.assertTrue(patch.get("diarization_enabled"))

    def test_apply_unknown_preset_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self._lib.apply_preset("nonexistent_preset_xyz")


class ConfigPresetsLibraryCustomTestCase(unittest.TestCase):
    """Тесты создания и управления кастомными пресетами."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._lib = ConfigPresetsLibrary(data_dir=self._tmpdir)

    def test_create_custom_preset_returns_preset(self) -> None:
        result = self._lib.create_preset(
            name="my_preset",
            description="Мой тестовый пресет",
            settings_patch={"quality_profile": "max", "auto_paste": False},
        )
        self.assertEqual(result["name"], "my_preset")
        self.assertEqual(result["description"], "Мой тестовый пресет")
        self.assertFalse(result.get("builtin"))
        self.assertIn("created_at", result)

    def test_create_preset_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.create_preset(name="", description="x", settings_patch={})

    def test_create_preset_builtin_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.create_preset(
                name="meeting",
                description="override",
                settings_patch={"quality_profile": "balanced"},
            )

    def test_create_preset_invalid_patch_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.create_preset(
                name="bad_preset",
                description="x",
                settings_patch="not_a_dict",  # type: ignore[arg-type]
            )

    def test_custom_preset_appears_in_list(self) -> None:
        self._lib.create_preset(
            name="my_custom",
            description="Custom preset",
            settings_patch={"quality_profile": "balanced"},
        )
        names = {p["name"] for p in self._lib.list_presets()}
        self.assertIn("my_custom", names)

    def test_custom_preset_apply_returns_patch(self) -> None:
        self._lib.create_preset(
            name="silent_mode",
            description="No auto paste",
            settings_patch={"auto_paste": False, "realtime_preview_enabled": False},
        )
        patch = self._lib.apply_preset("silent_mode")
        self.assertFalse(patch["auto_paste"])
        self.assertFalse(patch["realtime_preview_enabled"])

    def test_delete_custom_preset(self) -> None:
        self._lib.create_preset(
            name="to_delete",
            description="Will be deleted",
            settings_patch={"quality_profile": "max"},
        )
        deleted = self._lib.delete_preset("to_delete")
        self.assertTrue(deleted)
        names = {p["name"] for p in self._lib.list_presets()}
        self.assertNotIn("to_delete", names)

    def test_delete_nonexistent_preset_returns_false(self) -> None:
        result = self._lib.delete_preset("does_not_exist")
        self.assertFalse(result)

    def test_delete_builtin_preset_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.delete_preset("interview")


class ConfigPresetsLibraryPersistenceTestCase(unittest.TestCase):
    """Тесты сохранения и загрузки кастомных пресетов из файла."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_custom_preset_persisted_to_file(self) -> None:
        lib = ConfigPresetsLibrary(data_dir=self._tmpdir)
        lib.create_preset(
            name="persistent_preset",
            description="Будет сохранён",
            settings_patch={"quality_profile": "max"},
        )
        presets_file = Path(self._tmpdir) / "config_presets.json"
        self.assertTrue(presets_file.exists())
        data = json.loads(presets_file.read_text(encoding="utf-8"))
        self.assertIn("persistent_preset", data["presets"])

    def test_custom_preset_loaded_on_init(self) -> None:
        lib1 = ConfigPresetsLibrary(data_dir=self._tmpdir)
        lib1.create_preset(
            name="reload_test",
            description="Test reload",
            settings_patch={"auto_paste": True},
        )
        # Создаём новый экземпляр — должен загрузить из файла
        lib2 = ConfigPresetsLibrary(data_dir=self._tmpdir)
        names = {p["name"] for p in lib2.list_presets()}
        self.assertIn("reload_test", names)

    def test_custom_preset_deleted_on_reload(self) -> None:
        lib1 = ConfigPresetsLibrary(data_dir=self._tmpdir)
        lib1.create_preset(
            name="temp_preset",
            description="Temporary",
            settings_patch={"quality_profile": "balanced"},
        )
        lib1.delete_preset("temp_preset")
        lib2 = ConfigPresetsLibrary(data_dir=self._tmpdir)
        names = {p["name"] for p in lib2.list_presets()}
        self.assertNotIn("temp_preset", names)


class ConfigPresetsLibraryExportImportTestCase(unittest.TestCase):
    """Тесты экспорта и импорта пресетов."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._lib = ConfigPresetsLibrary(data_dir=self._tmpdir)

    def test_export_builtin_preset_returns_json(self) -> None:
        json_str = self._lib.export_preset("interview")
        data = json.loads(json_str)
        self.assertIn("preset", data)
        self.assertEqual(data["preset"]["name"], "interview")

    def test_export_unknown_preset_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._lib.export_preset("no_such_preset")

    def test_import_preset_from_json(self) -> None:
        # Экспортируем встроенный пресет и импортируем под новым именем
        json_str = self._lib.export_preset("meeting")
        data = json.loads(json_str)
        data["preset"]["name"] = "imported_meeting"
        data["preset"].pop("builtin", None)
        imported = self._lib.import_preset(json.dumps(data))
        self.assertEqual(imported["name"], "imported_meeting")
        # Должен появиться в списке
        names = {p["name"] for p in self._lib.list_presets()}
        self.assertIn("imported_meeting", names)

    def test_import_custom_preset_roundtrip(self) -> None:
        self._lib.create_preset(
            name="roundtrip_preset",
            description="Roundtrip test",
            settings_patch={"quality_profile": "max", "diarization_enabled": True},
        )
        json_str = self._lib.export_preset("roundtrip_preset")

        # Создаём новую библиотеку без кастомных пресетов
        tmpdir2 = tempfile.mkdtemp()
        lib2 = ConfigPresetsLibrary(data_dir=tmpdir2)
        lib2.import_preset(json_str)

        patch = lib2.apply_preset("roundtrip_preset")
        self.assertEqual(patch["quality_profile"], "max")
        self.assertTrue(patch["diarization_enabled"])

    def test_import_invalid_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.import_preset("not json {{{")

    def test_import_missing_name_raises(self) -> None:
        payload = json.dumps({
            "preset": {"description": "no name here", "settings_patch": {}}
        })
        with self.assertRaises(ValueError):
            self._lib.import_preset(payload)

    def test_import_missing_settings_patch_raises(self) -> None:
        payload = json.dumps({
            "preset": {"name": "broken_preset", "description": "no patch"}
        })
        with self.assertRaises(ValueError):
            self._lib.import_preset(payload)


class ConfigPresetsLibraryIPCTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._lib = ConfigPresetsLibrary(data_dir=self._tmpdir)

    def test_handle_list_config_presets(self) -> None:
        result = self._lib.handle_list_config_presets({})
        self.assertIn("presets", result)
        self.assertIsInstance(result["presets"], list)
        self.assertGreater(len(result["presets"]), 0)

    def test_handle_apply_config_preset(self) -> None:
        result = self._lib.handle_apply_config_preset({"name": "podcast"})
        self.assertEqual(result["name"], "podcast")
        self.assertIn("settings_patch", result)
        self.assertIsInstance(result["settings_patch"], dict)

    def test_handle_apply_config_preset_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.handle_apply_config_preset({})

    def test_handle_apply_config_preset_unknown_name_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._lib.handle_apply_config_preset({"name": "no_such_preset_abc"})

    def test_handle_create_config_preset(self) -> None:
        result = self._lib.handle_create_config_preset({
            "name": "ipc_test_preset",
            "description": "Создан через IPC",
            "settings_patch": {"quality_profile": "balanced", "auto_paste": True},
        })
        self.assertIn("preset", result)
        self.assertEqual(result["preset"]["name"], "ipc_test_preset")

    def test_handle_create_config_preset_missing_patch_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.handle_create_config_preset({
                "name": "bad",
                "description": "no patch",
            })

    def test_handle_create_config_preset_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._lib.handle_create_config_preset({
                "description": "x",
                "settings_patch": {"quality_profile": "max"},
            })


class ConfigPresetsLibraryGetBuiltinTestCase(unittest.TestCase):
    """Тесты метода get_built_in_presets()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._lib = ConfigPresetsLibrary(data_dir=self._tmpdir)

    def test_get_built_in_presets_returns_dict(self) -> None:
        result = self._lib.get_built_in_presets()
        self.assertIsInstance(result, dict)

    def test_get_built_in_presets_contains_all_five(self) -> None:
        result = self._lib.get_built_in_presets()
        for name in ("interview", "meeting", "voice_memo", "language_practice", "podcast"):
            self.assertIn(name, result)

    def test_get_built_in_presets_count(self) -> None:
        result = self._lib.get_built_in_presets()
        self.assertEqual(len(result), 5)

    def test_get_built_in_presets_returns_copy(self) -> None:
        """Мутация результата не затрагивает оригинал."""
        result = self._lib.get_built_in_presets()
        result["interview"]["name"] = "mutated"
        fresh = self._lib.get_built_in_presets()
        self.assertEqual(fresh["interview"]["name"], "interview")

    def test_get_built_in_presets_each_has_settings_patch(self) -> None:
        result = self._lib.get_built_in_presets()
        for name, preset in result.items():
            self.assertIn("settings_patch", preset, f"Нет settings_patch в пресете '{name}'")
            self.assertIsInstance(preset["settings_patch"], dict)

    def test_get_built_in_presets_is_static(self) -> None:
        """get_built_in_presets доступен как статический метод."""
        result = ConfigPresetsLibrary.get_built_in_presets()
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 5)

    def test_get_built_in_presets_not_affected_by_custom(self) -> None:
        """Кастомные пресеты не попадают в get_built_in_presets."""
        self._lib.create_preset(
            name="my_custom_extra",
            description="Custom",
            settings_patch={"quality_profile": "balanced"},
        )
        builtins = self._lib.get_built_in_presets()
        self.assertNotIn("my_custom_extra", builtins)


if __name__ == "__main__":
    unittest.main()
