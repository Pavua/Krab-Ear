"""Тесты для export_settings / import_settings в SettingsService.

Проверяет:
- export_settings исключает чувствительные поля
- export_settings создаёт валидный JSON-файл
- export_settings возвращает корректное settings_count
- import_settings загружает настройки из файла
- import_settings не импортирует чувствительные поля (silent skip)
- import_settings выполняет merge (не replaces) существующих настроек
- import_settings возвращает корректные imported/skipped счётчики
- import_settings бросает ValueError при невалидном JSON
- import_settings бросает FileNotFoundError если файл не существует
- import_settings бросает ValueError если не передан 'file'
"""

from __future__ import annotations
from backend.settings_service import SettingsService

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_store(settings: dict | None = None) -> MagicMock:
    """Создаёт фиктивный store с сохранёнными настройками."""
    store = MagicMock()
    current: dict = dict(settings or {
        "quality_profile": "balanced",
        "cleanup_profile": "soft",
        "translation_mode": "off",
        "auto_paste": True,
        "realtime_preview_enabled": True,
        "voice_gateway_api_key": "secret_gw_key",
        "hf_token": "secret_hf",
        "rest_api_key": "secret_rest",
        "lm_studio_api_key": "secret_lm",
    })
    store.load_settings.return_value = dict(current)
    saved_holder: list[dict] = []

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        saved_holder.clear()
        saved_holder.append(dict(s))
        return dict(s)

    store.save_settings.side_effect = _save
    store._saved = saved_holder
    store._current = current
    return store


class TestExportSettings(unittest.TestCase):

    def _svc(self, **kw) -> SettingsService:
        return SettingsService(store=_make_store(**kw))

    def test_export_strips_sensitive_fields(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "export.json")
            _result = svc.handle_export_settings({"file": out})  # noqa: F841
            with open(out) as f:
                data = json.load(f)

        for field in ("voice_gateway_api_key", "hf_token", "rest_api_key", "lm_studio_api_key"):
            self.assertNotIn(field, data, f"Sensitive field '{field}' should not be exported")

    def test_export_creates_valid_json(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "export.json")
            svc.handle_export_settings({"file": out})
            with open(out) as f:
                data = json.load(f)
        self.assertIsInstance(data, dict)

    def test_export_returns_settings_count(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "export.json")
            result = svc.handle_export_settings({"file": out})
        self.assertIn("settings_count", result)
        self.assertIsInstance(result["settings_count"], int)
        self.assertGreater(result["settings_count"], 0)

    def test_export_returns_file_path(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "export.json")
            result = svc.handle_export_settings({"file": out})
        # resolve() handles /private symlink on macOS
        self.assertEqual(Path(result["file"]).resolve(), Path(out).resolve())

    def test_export_includes_non_sensitive_settings(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "export.json")
            svc.handle_export_settings({"file": out})
            with open(out) as f:
                data = json.load(f)
        self.assertIn("quality_profile", data)
        self.assertEqual(data["quality_profile"], "balanced")

    def test_export_default_path_created_if_no_file_param(self):
        """Без 'file' экспорт создаёт файл в домашнем каталоге."""
        svc = self._svc()
        result = svc.handle_export_settings({})
        path = Path(result["file"])
        try:
            self.assertTrue(path.exists())
            self.assertTrue(path.name.startswith("krabear_settings_"))
        finally:
            path.unlink(missing_ok=True)


class TestImportSettings(unittest.TestCase):

    def _svc(self) -> SettingsService:
        return SettingsService(store=_make_store())

    def _write_json(self, tmp: str, data: dict) -> str:
        p = str(Path(tmp) / "import.json")
        with open(p, "w") as f:
            json.dump(data, f)
        return p

    def test_import_merges_settings(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_json(tmp, {"quality_profile": "max"})
            result = svc.handle_import_settings({"file": p})
        self.assertGreater(result["imported"], 0)
        saved = svc.store._current
        self.assertEqual(saved.get("quality_profile"), "max")

    def test_import_does_not_overwrite_sensitive_fields(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_json(tmp, {
                "quality_profile": "max",
                "voice_gateway_api_key": "hacker_key",
                "hf_token": "hacker_hf",
            })
            result = svc.handle_import_settings({"file": p})

        # Sensitive keys should be in skipped count
        self.assertEqual(result["skipped"], 2)
        # They should not appear in saved settings (or remain with original empty value)
        saved = svc.store._current
        self.assertNotEqual(saved.get("voice_gateway_api_key"), "hacker_key")
        self.assertNotEqual(saved.get("hf_token"), "hacker_hf")

    def test_import_returns_correct_counts(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_json(tmp, {
                "quality_profile": "max",
                "hf_token": "x",          # skipped
                "rest_api_key": "y",       # skipped
            })
            result = svc.handle_import_settings({"file": p})
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 2)

    def test_import_raises_on_missing_file(self):
        svc = self._svc()
        with self.assertRaises(FileNotFoundError):
            svc.handle_import_settings({"file": "/nonexistent/path/settings.json"})

    def test_import_raises_on_invalid_json(self):
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            bad = str(Path(tmp) / "bad.json")
            with open(bad, "w") as f:
                f.write("NOT JSON{{")
            with self.assertRaises(ValueError):
                svc.handle_import_settings({"file": bad})

    def test_import_raises_without_file_param(self):
        svc = self._svc()
        with self.assertRaises(ValueError):
            svc.handle_import_settings({})

    def test_import_errors_list_in_result(self):
        """Результат всегда содержит ключ 'errors' (список)."""
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_json(tmp, {"quality_profile": "balanced"})
            result = svc.handle_import_settings({"file": p})
        self.assertIn("errors", result)
        self.assertIsInstance(result["errors"], list)

    def test_export_import_roundtrip_preserves_settings(self):
        """Export → import round-trip должен сохранить несекретные настройки."""
        svc = self._svc()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "roundtrip.json")
            svc.handle_export_settings({"file": out})

            # Reset store to different values
            svc.store._current["quality_profile"] = "max"
            svc.store.load_settings.return_value = dict(svc.store._current)
            svc.invalidate_cache()

            svc.handle_import_settings({"file": out})

        # After import, quality_profile should be restored to "balanced"
        self.assertEqual(svc.store._current.get("quality_profile"), "balanced")


if __name__ == "__main__":
    unittest.main()
