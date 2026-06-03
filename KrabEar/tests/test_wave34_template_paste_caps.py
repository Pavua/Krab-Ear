"""Wave-34: caps + validation tests for TemplateManager and PasteAppMemory.

C1 (MED) — template_manager: MAX_TEMPLATES=200, MAX_TEMPLATE_NAME_LEN=200,
                               MAX_TEMPLATE_TEXT_LEN=10000
C2 (MED) — paste_app_memory: MAX_BUNDLE_IDS=500, bundle_id format validation
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.template_manager import (
    MAX_TEMPLATES,
    MAX_TEMPLATE_NAME_LEN,
    MAX_TEMPLATE_TEXT_LEN,
    TemplateManager,
)
from backend.paste_app_memory import MAX_BUNDLE_IDS, PasteAppMemory


# ---------------------------------------------------------------------------
# C1 — TemplateManager caps
# ---------------------------------------------------------------------------


class TestTemplateCaps(unittest.TestCase):
    """Проверяем лимиты шаблонов: count, name length, text length."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tm = TemplateManager(data_dir=self.tmpdir)

    def test_constants_have_expected_values(self):
        self.assertEqual(MAX_TEMPLATES, 200)
        self.assertEqual(MAX_TEMPLATE_NAME_LEN, 200)
        self.assertEqual(MAX_TEMPLATE_TEXT_LEN, 10_000)

    def test_add_up_to_max_templates_allowed(self):
        """Добавление ровно MAX_TEMPLATES пользовательских шаблонов должно работать."""
        for i in range(MAX_TEMPLATES):
            self.tm.add_template(name=f"tpl_{i}", text="some text")
        # последний должен был добавиться без исключения
        names = {t["name"] for t in self.tm.get_templates() if not t.get("builtin")}
        self.assertEqual(len(names), MAX_TEMPLATES)

    def test_add_201st_template_raises(self):
        """201-й уникальный шаблон должен вернуть ValueError."""
        for i in range(MAX_TEMPLATES):
            self.tm.add_template(name=f"tpl_{i}", text="text")
        with self.assertRaises(ValueError):
            self.tm.add_template(name="tpl_over_limit", text="text")

    def test_update_existing_template_does_not_count_toward_limit(self):
        """Обновление существующего шаблона не должно считаться как новый."""
        for i in range(MAX_TEMPLATES):
            self.tm.add_template(name=f"tpl_{i}", text="text")
        # Обновление tpl_0 — должно пройти без ошибки
        result = self.tm.add_template(name="tpl_0", text="updated text")
        self.assertEqual(result["text"], "updated text")

    def test_name_too_long_raises(self):
        long_name = "a" * (MAX_TEMPLATE_NAME_LEN + 1)
        # name regex [\w\-]+ will also pass for long pure alpha names
        with self.assertRaises(ValueError):
            self.tm.add_template(name=long_name, text="text")

    def test_name_at_limit_allowed(self):
        name_at_limit = "a" * MAX_TEMPLATE_NAME_LEN
        result = self.tm.add_template(name=name_at_limit, text="text")
        self.assertEqual(result["name"], name_at_limit)

    def test_text_too_long_raises(self):
        long_text = "x" * (MAX_TEMPLATE_TEXT_LEN + 1)
        with self.assertRaises(ValueError):
            self.tm.add_template(name="tpl_long", text=long_text)

    def test_text_at_limit_allowed(self):
        text_at_limit = "x" * MAX_TEMPLATE_TEXT_LEN
        result = self.tm.add_template(name="tpl_exact", text=text_at_limit)
        self.assertEqual(len(result["text"]), MAX_TEMPLATE_TEXT_LEN)

    def test_add_after_remove_allowed(self):
        """После удаления шаблона из полного хранилища можно добавить новый."""
        for i in range(MAX_TEMPLATES):
            self.tm.add_template(name=f"tpl_{i}", text="text")
        self.tm.remove_template("tpl_0")
        # Теперь должно пройти
        result = self.tm.add_template(name="tpl_new", text="new")
        self.assertEqual(result["name"], "tpl_new")


# ---------------------------------------------------------------------------
# C2 — PasteAppMemory caps + bundle_id validation
# ---------------------------------------------------------------------------


class TestPasteAppMemoryCaps(unittest.TestCase):
    """Проверяем MAX_BUNDLE_IDS и валидацию формата bundle_id."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pam = PasteAppMemory(data_dir=Path(self.tmpdir))

    def test_constant_has_expected_value(self):
        self.assertEqual(MAX_BUNDLE_IDS, 500)

    def test_add_up_to_max_bundle_ids_allowed(self):
        for i in range(MAX_BUNDLE_IDS):
            self.pam.record(bundle_id=f"com.test.app{i}", profile="plain")
        self.assertEqual(len(self.pam.list_profiles()), MAX_BUNDLE_IDS)

    def test_501st_bundle_id_evicts_oldest(self):
        """При достижении лимита запись для 501-го ID принимается через LRU-вытеснение."""
        for i in range(MAX_BUNDLE_IDS):
            self.pam.record(bundle_id=f"com.test.app{i}", profile="plain")
        # Добавляем 501-й — должно пройти без ошибки (LRU eviction)
        self.pam.record(bundle_id="com.test.new-app", profile="markdown")
        profiles = self.pam.list_profiles()
        # Всё ещё не больше лимита
        self.assertLessEqual(len(profiles), MAX_BUNDLE_IDS)
        # Новая запись должна присутствовать
        ids = {p["bundle_id"] for p in profiles}
        self.assertIn("com.test.new-app", ids)

    def test_update_existing_does_not_evict(self):
        """Обновление существующего bundle_id не должно вытеснять другие."""
        for i in range(MAX_BUNDLE_IDS):
            self.pam.record(bundle_id=f"com.test.app{i}", profile="plain")
        # Обновляем первый — не должно триггерить eviction
        self.pam.record(bundle_id="com.test.app0", profile="markdown")
        self.assertEqual(len(self.pam.list_profiles()), MAX_BUNDLE_IDS)

    # -- bundle_id format validation --

    def test_valid_bundle_id_accepted(self):
        """Стандартные macOS bundle ID должны приниматься."""
        for bid in [
            "com.apple.Notes",
            "com.microsoft.Word",
            "org.chromium.Chromium",
            "net.app-name.SomeApp",
            "app",
        ]:
            self.pam.record(bundle_id=bid, profile="plain")
            self.assertIsNotNone(self.pam.get_profile_for(bid), f"ожидался профиль для {bid!r}")

    def test_bundle_id_with_spaces_rejected(self):
        """bundle_id с пробелами должен игнорироваться."""
        self.pam.record(bundle_id="com.bad app", profile="plain")
        self.assertIsNone(self.pam.get_profile_for("com.bad app"))

    def test_bundle_id_with_special_chars_rejected(self):
        """bundle_id со спецсимволами (слеш, @, скобки) должен игнорироваться."""
        for bad_id in ["com/bad", "com@bad", "com[bad]", "com;bad", "com<bad>"]:
            self.pam.record(bundle_id=bad_id, profile="plain")
            self.assertIsNone(self.pam.get_profile_for(bad_id), f"ожидался None для {bad_id!r}")

    def test_bundle_id_with_underscore_rejected(self):
        """bundle_id с подчёркиванием технически не стандарт macOS — должен игнорироваться."""
        self.pam.record(bundle_id="com.bad_app", profile="plain")
        self.assertIsNone(self.pam.get_profile_for("com.bad_app"))

    def test_empty_bundle_id_rejected(self):
        """Пустой bundle_id должен молча игнорироваться."""
        self.pam.record(bundle_id="", profile="plain")
        self.assertEqual(len(self.pam.list_profiles()), 0)

    def test_whitespace_only_bundle_id_rejected(self):
        """bundle_id из пробелов должен молча игнорироваться."""
        self.pam.record(bundle_id="   ", profile="plain")
        self.assertEqual(len(self.pam.list_profiles()), 0)

    def test_very_long_bundle_id_rejected(self):
        """bundle_id длиннее _MAX_BUNDLE_ID_LEN должен игнорироваться."""
        long_id = "com." + "a" * 600
        self.pam.record(bundle_id=long_id, profile="plain")
        self.assertIsNone(self.pam.get_profile_for(long_id))


if __name__ == "__main__":
    unittest.main()
