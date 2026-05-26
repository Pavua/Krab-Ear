"""Tests for W1339 F1+F2 — config_presets atomic apply + IPC wiring of
delete/export/import handlers.

W1339 F1 MED: apply_config_preset теперь атомарно мержит patch в настройки
    через settings_svc.handle_set_settings (единый вызов, аналог apply_profile_preset).
W1339 F2 MED: delete_config_preset, export_config_preset, import_config_preset
    добавлены в dispatch-таблицу service.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# ---------------------------------------------------------------------------
# sys.path setup (inline, no conftest)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR_ROOT = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, _KRAB_EAR_ROOT)

from backend.config_presets_library import ConfigPresetsLibrary  # noqa: E402


# ---------------------------------------------------------------------------
# Stub settings service
# ---------------------------------------------------------------------------

class _FakeSettingsSvc:
    """Minimal stub that records calls to handle_set_settings."""

    def __init__(self) -> None:
        self._settings: dict = {}
        self.calls: list[dict] = []

    def handle_set_settings(self, params: dict) -> dict:
        self.calls.append(dict(params))
        self._settings.update(params)
        return {"ok": True, "updated_keys": sorted(params.keys())}

    def cached_settings(self) -> dict:
        return dict(self._settings)


# ---------------------------------------------------------------------------
# Tests — atomic apply (W1339 F1)
# ---------------------------------------------------------------------------

class TestApplyConfigPresetAtomicWritesSettings(unittest.TestCase):
    """F1: handle_apply_config_preset должен атомарно записывать настройки."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.settings_svc = _FakeSettingsSvc()
        self.lib = ConfigPresetsLibrary(
            data_dir=self._tmpdir.name,
            settings_svc=self.settings_svc,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_apply_builtin_preset_calls_settings_svc(self) -> None:
        """apply_config_preset на встроенном пресете вызывает handle_set_settings."""
        result = self.lib.handle_apply_config_preset({"name": "meeting"})

        self.assertTrue(result["applied"], "applied должно быть True при наличии settings_svc")
        self.assertEqual(result["name"], "meeting")
        self.assertIn("settings_patch", result)
        self.assertIn("saved", result)

        # settings_svc.handle_set_settings должен быть вызван ровно один раз
        self.assertEqual(len(self.settings_svc.calls), 1)
        call = self.settings_svc.calls[0]
        # patch должен содержать ключи из встроенного пресета "meeting"
        self.assertIn("quality_profile", call)
        self.assertEqual(call["quality_profile"], "balanced")

    def test_apply_custom_preset_atomic(self) -> None:
        """apply_config_preset на кастомном пресете тоже атомарен."""
        self.lib.create_preset(
            name="my_custom",
            description="Test preset",
            settings_patch={"quality_profile": "max", "auto_paste": True},
        )
        result = self.lib.handle_apply_config_preset({"name": "my_custom"})

        self.assertTrue(result["applied"])
        self.assertEqual(len(self.settings_svc.calls), 1)
        self.assertEqual(self.settings_svc.calls[0]["quality_profile"], "max")
        self.assertTrue(self.settings_svc.calls[0]["auto_paste"])

    def test_apply_without_settings_svc_returns_patch_only(self) -> None:
        """Без settings_svc возвращает patch и applied=False (режим совместимости)."""
        lib_no_svc = ConfigPresetsLibrary(data_dir=self._tmpdir.name)
        result = lib_no_svc.handle_apply_config_preset({"name": "voice_memo"})

        self.assertFalse(result["applied"])
        self.assertIn("settings_patch", result)
        self.assertNotIn("saved", result)

    def test_apply_missing_name_raises(self) -> None:
        """Пустое имя → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_apply_config_preset({"name": ""})

    def test_apply_unknown_preset_raises(self) -> None:
        """Несуществующий пресет → KeyError."""
        with self.assertRaises(KeyError):
            self.lib.handle_apply_config_preset({"name": "nonexistent_xyz"})

    def test_single_call_not_two_step(self) -> None:
        """Ровно один вызов settings_svc, не два (атомарность)."""
        self.lib.handle_apply_config_preset({"name": "interview"})
        self.assertEqual(
            len(self.settings_svc.calls),
            1,
            "Ожидался ровно 1 вызов handle_set_settings (атомарный), не 2",
        )


# ---------------------------------------------------------------------------
# Tests — delete_config_preset IPC (W1339 F2)
# ---------------------------------------------------------------------------

class TestDeleteConfigPresetIPC(unittest.TestCase):
    """F2: handle_delete_config_preset должен удалять кастомный пресет."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.lib = ConfigPresetsLibrary(data_dir=self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_delete_existing_custom_preset(self) -> None:
        """Удаляет существующий кастомный пресет."""
        self.lib.create_preset(
            name="to_delete",
            description="будет удалён",
            settings_patch={"quality_profile": "max"},
        )
        result = self.lib.handle_delete_config_preset({"name": "to_delete"})
        self.assertEqual(result["name"], "to_delete")
        self.assertTrue(result["deleted"])

        # Убедимся, что пресет больше не в списке
        names = [p["name"] for p in self.lib.list_presets()]
        self.assertNotIn("to_delete", names)

    def test_delete_nonexistent_preset_returns_false(self) -> None:
        """Несуществующий → deleted=False."""
        result = self.lib.handle_delete_config_preset({"name": "ghost_preset"})
        self.assertFalse(result["deleted"])

    def test_delete_builtin_preset_raises(self) -> None:
        """Попытка удалить встроенный пресет → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_delete_config_preset({"name": "meeting"})

    def test_delete_missing_name_raises(self) -> None:
        """Пустое имя → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_delete_config_preset({"name": ""})


# ---------------------------------------------------------------------------
# Tests — export_config_preset IPC (W1339 F2)
# ---------------------------------------------------------------------------

class TestExportConfigPresetIPC(unittest.TestCase):
    """F2: handle_export_config_preset должен вернуть JSON-строку пресета."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.lib = ConfigPresetsLibrary(data_dir=self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_export_builtin_preset(self) -> None:
        """Встроенный пресет экспортируется корректно."""
        result = self.lib.handle_export_config_preset({"name": "podcast"})
        self.assertEqual(result["name"], "podcast")
        self.assertIn("json", result)

        envelope = json.loads(result["json"])
        self.assertIn("preset", envelope)
        self.assertEqual(envelope["preset"]["name"], "podcast")

    def test_export_custom_preset(self) -> None:
        """Кастомный пресет экспортируется с полной структурой."""
        self.lib.create_preset(
            name="export_me",
            description="экспортируемый пресет",
            settings_patch={"diarization_enabled": True},
        )
        result = self.lib.handle_export_config_preset({"name": "export_me"})
        envelope = json.loads(result["json"])
        self.assertEqual(envelope["preset"]["name"], "export_me")
        self.assertIn("format_version", envelope)

    def test_export_nonexistent_raises(self) -> None:
        """Несуществующий пресет → KeyError."""
        with self.assertRaises(KeyError):
            self.lib.handle_export_config_preset({"name": "no_such_preset"})

    def test_export_missing_name_raises(self) -> None:
        """Пустое имя → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_export_config_preset({"name": ""})


# ---------------------------------------------------------------------------
# Tests — import_config_preset IPC (W1339 F2)
# ---------------------------------------------------------------------------

class TestImportConfigPresetIPC(unittest.TestCase):
    """F2: handle_import_config_preset должен импортировать пресет из JSON."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.lib = ConfigPresetsLibrary(data_dir=self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _make_envelope(self, name: str, patch: dict, desc: str = "") -> str:
        """Создаёт валидный JSON envelope для импорта."""
        return json.dumps({
            "format_version": "1",
            "preset": {
                "name": name,
                "description": desc,
                "settings_patch": patch,
            },
        })

    def test_import_valid_preset(self) -> None:
        """Импорт валидного пресета создаёт кастомный пресет."""
        json_str = self._make_envelope(
            "imported_one",
            {"quality_profile": "max", "auto_paste": False},
            desc="импортированный пресет",
        )
        result = self.lib.handle_import_config_preset({"json": json_str})
        self.assertIn("preset", result)
        self.assertEqual(result["preset"]["name"], "imported_one")

        names = [p["name"] for p in self.lib.list_presets()]
        self.assertIn("imported_one", names)

    def test_import_round_trip_with_export(self) -> None:
        """Export → import round-trip сохраняет данные пресета."""
        self.lib.create_preset(
            name="round_trip",
            description="туда и обратно",
            settings_patch={"translation_mode": "bilingual_ru_es"},
        )
        export_result = self.lib.handle_export_config_preset({"name": "round_trip"})

        # Удаляем и импортируем заново
        self.lib.delete_preset("round_trip")
        import_result = self.lib.handle_import_config_preset({"json": export_result["json"]})
        self.assertEqual(import_result["preset"]["name"], "round_trip")
        self.assertEqual(
            import_result["preset"]["settings_patch"]["translation_mode"],
            "bilingual_ru_es",
        )

    def test_import_invalid_json_raises(self) -> None:
        """Невалидный JSON → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_import_config_preset({"json": "not_json{"})

    def test_import_missing_name_raises(self) -> None:
        """JSON без поля name → ValueError."""
        json_str = json.dumps({
            "preset": {"settings_patch": {"quality_profile": "balanced"}}
        })
        with self.assertRaises(ValueError):
            self.lib.handle_import_config_preset({"json": json_str})

    def test_import_missing_settings_patch_raises(self) -> None:
        """JSON без settings_patch → ValueError."""
        json_str = json.dumps({"preset": {"name": "bad_preset"}})
        with self.assertRaises(ValueError):
            self.lib.handle_import_config_preset({"json": json_str})

    def test_import_missing_json_param_raises(self) -> None:
        """Пустой параметр json → ValueError."""
        with self.assertRaises(ValueError):
            self.lib.handle_import_config_preset({"json": ""})


# ---------------------------------------------------------------------------
# Tests — dispatch table wiring in service.py (W1339 F2)
# ---------------------------------------------------------------------------

class TestServiceDispatchWiring(unittest.TestCase):
    """Убеждаемся, что 3 новых метода добавлены в таблицу dispatch service.py."""

    def test_dispatch_table_contains_delete_export_import(self) -> None:
        """Все три новых IPC метода присутствуют в dispatch table service.py."""
        service_path = os.path.join(
            os.path.dirname(__file__), "..", "backend", "service.py"
        )
        with open(service_path, encoding="utf-8") as fh:
            source = fh.read()

        for method in ("delete_config_preset", "export_config_preset", "import_config_preset"):
            self.assertIn(
                f'"{method}"',
                source,
                f"Метод {method!r} не найден в service.py",
            )

    def test_dispatch_table_apply_updated_comment(self) -> None:
        """apply_config_preset остаётся в таблице dispatch."""
        service_path = os.path.join(
            os.path.dirname(__file__), "..", "backend", "service.py"
        )
        with open(service_path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn(
            '"apply_config_preset"',
            source,
            "apply_config_preset должен оставаться в таблице dispatch",
        )

    def test_dispatch_handlers_are_callable_methods(self) -> None:
        """handle_delete/export/import_config_preset существуют в ConfigPresetsLibrary."""
        for method_name in (
            "handle_delete_config_preset",
            "handle_export_config_preset",
            "handle_import_config_preset",
        ):
            self.assertTrue(
                hasattr(ConfigPresetsLibrary, method_name),
                f"ConfigPresetsLibrary.{method_name} не найден",
            )
            self.assertTrue(
                callable(getattr(ConfigPresetsLibrary, method_name)),
                f"ConfigPresetsLibrary.{method_name} не является callable",
            )


if __name__ == "__main__":
    unittest.main()
