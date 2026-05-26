"""Тесты IPC-диспетчеризации профилей нормализации (W1055).

Проверяем три новых метода: add_normalization_profile, remove_normalization_profile,
apply_normalization_profile — добавленных в W1055.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.normalization_profiles import NormalizationProfileRegistry


class TestNormalizationProfileHandlers(unittest.TestCase):
    """Тестируем три новых IPC-хендлера через прямой вызов методов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = NormalizationProfileRegistry(data_dir=Path(self.tmp.name))

    # Вспомогательные методы, имитирующие хендлеры из service.py

    def _handle_add_normalization_profile(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        rules = list(params.get("rules", []))
        description = str(params.get("description", ""))
        overwrite = bool(params.get("overwrite", False))
        profile = self.registry.add_profile(name, rules, description, overwrite=overwrite)
        return {"profile": profile.to_dict()}

    def _handle_remove_normalization_profile(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        removed = self.registry.remove_profile(name)
        return {"removed": removed, "name": name}

    def _handle_apply_normalization_profile(self, params: dict) -> dict:
        text = str(params.get("text", ""))
        profile_name = str(params.get("profile_name", "")).strip()
        if not profile_name:
            raise ValueError("Параметр 'profile_name' обязателен")
        normalized = self.registry.apply_profile(text, profile_name)
        return {"text": normalized, "profile_name": profile_name}

    # ── F1 tests ──────────────────────────────────────────────────────────────

    def test_add_profile_dispatched(self):
        """add_normalization_profile возвращает dict с profile."""
        result = self._handle_add_normalization_profile({
            "name": "my_custom",
            "rules": ["cleanup_soft"],
            "description": "мой профиль",
        })
        self.assertIn("profile", result)
        self.assertEqual(result["profile"]["name"], "my_custom")
        self.assertEqual(result["profile"]["rules"], ["cleanup_soft"])
        self.assertFalse(result["profile"]["builtin"])

    def test_add_profile_empty_name_raises(self):
        """add_normalization_profile без name должен поднять ValueError."""
        with self.assertRaises(ValueError):
            self._handle_add_normalization_profile({"name": "", "rules": []})

    def test_remove_profile_dispatched(self):
        """remove_normalization_profile удаляет ранее добавленный профиль."""
        self.registry.add_profile("to_remove", ["cleanup_soft"])
        result = self._handle_remove_normalization_profile({"name": "to_remove"})
        self.assertTrue(result["removed"])
        self.assertEqual(result["name"], "to_remove")

    def test_remove_profile_nonexistent_returns_false(self):
        """remove_normalization_profile для несуществующего профиля возвращает removed=False."""
        result = self._handle_remove_normalization_profile({"name": "does_not_exist"})
        self.assertFalse(result["removed"])

    def test_remove_builtin_profile_raises(self):
        """Нельзя удалить встроенный профиль."""
        with self.assertRaises(ValueError):
            self._handle_remove_normalization_profile({"name": "clean"})

    def test_apply_profile_dispatched(self):
        """apply_normalization_profile применяет профиль к тексту."""
        result = self._handle_apply_normalization_profile({
            "text": "  привет   мир  ",
            "profile_name": "verbatim",
        })
        self.assertIn("text", result)
        self.assertEqual(result["profile_name"], "verbatim")
        self.assertIsInstance(result["text"], str)

    def test_apply_profile_empty_profile_name_raises(self):
        """apply_normalization_profile без profile_name поднимает ValueError."""
        with self.assertRaises(ValueError):
            self._handle_apply_normalization_profile({"text": "hello", "profile_name": ""})

    def test_apply_profile_unknown_raises(self):
        """apply_normalization_profile для несуществующего профиля поднимает ValueError."""
        with self.assertRaises(ValueError):
            self._handle_apply_normalization_profile({
                "text": "hello",
                "profile_name": "nonexistent_profile_xyz",
            })

    # ── F2 tests ──────────────────────────────────────────────────────────────

    def test_atomic_save_no_partial_file(self):
        """После add_profile .tmp файл не должен оставаться, финальный файл должен существовать."""
        self.registry.add_profile("atomictest", ["cleanup_soft"], "проверка атомарности")
        data_dir = Path(self.tmp.name)
        final_path = data_dir / "normalization_profiles.json"
        tmp_path = data_dir / "normalization_profiles.tmp"
        self.assertTrue(final_path.exists(), "Финальный файл профилей не создан")
        self.assertFalse(tmp_path.exists(), ".tmp файл остался после записи (не атомарно)")

    def test_atomic_save_valid_json_after_write(self):
        """Сохранённый файл должен быть валидным JSON со всеми кастомными профилями."""
        import json
        self.registry.add_profile("p1", ["cleanup_soft"])
        self.registry.add_profile("p2", ["strip_hallucinations"])
        data_dir = Path(self.tmp.name)
        data = json.loads((data_dir / "normalization_profiles.json").read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        names = [p["name"] for p in data]
        self.assertIn("p1", names)
        self.assertIn("p2", names)

    def test_remove_profile_updates_file_atomically(self):
        """remove_profile тоже обновляет файл через атомарную запись."""
        import json
        self.registry.add_profile("removeme", ["cleanup_soft"])
        self.registry.remove_profile("removeme")
        data_dir = Path(self.tmp.name)
        data = json.loads((data_dir / "normalization_profiles.json").read_text(encoding="utf-8"))
        names = [p["name"] for p in data]
        self.assertNotIn("removeme", names)

    def test_builtin_profiles_not_saved_to_custom_file(self):
        """Встроенные профили не должны попасть в кастомный JSON-файл."""
        import json
        # Добавляем хотя бы один кастомный профиль чтобы инициировать запись
        self.registry.add_profile("my_profile", ["cleanup_soft"])
        data_dir = Path(self.tmp.name)
        data = json.loads((data_dir / "normalization_profiles.json").read_text(encoding="utf-8"))
        builtin_names = {"verbatim", "clean", "formal", "telegram", "subtitles"}
        saved_names = {p["name"] for p in data}
        self.assertTrue(saved_names.isdisjoint(builtin_names),
                        f"Встроенные профили попали в кастомный файл: {saved_names & builtin_names}")


if __name__ == "__main__":
    unittest.main()
