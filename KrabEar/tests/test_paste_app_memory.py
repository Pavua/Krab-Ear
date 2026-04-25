"""Unit tests для PasteAppMemory.

Тесты:
1. record + get — запись и чтение профиля
2. persistence — данные сохраняются на диск и восстанавливаются
3. cleanup_stale — устаревшие записи удаляются
4. unknown_app — get_profile_for неизвестного bundle_id → None
5. disabled — при enabled=False record/get/list ничего не делают
6. IPC handle_* методы (get, record, list)
7. multi_bundle — разные bundle_id → разные профили
8. invalid_profile — некорректный профиль → ValueError
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.paste_app_memory import PasteAppMemory


def _make_mem(tmp_path: Path, enabled: bool = True) -> PasteAppMemory:
    return PasteAppMemory(data_dir=tmp_path, enabled=enabled)


class TestPasteAppMemoryRecordGet(unittest.TestCase):
    """1. record + get_profile_for."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mem = _make_mem(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_record_and_get(self) -> None:
        self.mem.record("com.tinyspeck.slackmacgap", "markdown")
        result = self.mem.get_profile_for("com.tinyspeck.slackmacgap")
        self.assertEqual(result, "markdown")

    def test_overwrite_profile(self) -> None:
        self.mem.record("com.apple.mail", "plain")
        self.mem.record("com.apple.mail", "markdown")
        self.assertEqual(self.mem.get_profile_for("com.apple.mail"), "markdown")


class TestPasteAppMemoryPersistence(unittest.TestCase):
    """2. Persistence — данные сохраняются и восстанавливаются."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_data_survives_reload(self) -> None:
        mem1 = _make_mem(self.tmp_path)
        mem1.record("com.tinyspeck.slackmacgap", "markdown")
        mem1.record("com.apple.mail", "plain")

        mem2 = _make_mem(self.tmp_path)
        self.assertEqual(mem2.get_profile_for("com.tinyspeck.slackmacgap"), "markdown")
        self.assertEqual(mem2.get_profile_for("com.apple.mail"), "plain")

    def test_file_written_as_json(self) -> None:
        mem = _make_mem(self.tmp_path)
        mem.record("com.apple.Notes", "notes")
        data_file = self.tmp_path / "paste_app_memory.json"
        self.assertTrue(data_file.exists())
        raw = json.loads(data_file.read_text())
        self.assertIn("com.apple.Notes", raw)


class TestPasteAppMemoryCleanup(unittest.TestCase):
    """3. cleanup_stale — устаревшие записи удаляются."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_cleanup_removes_old_entries(self) -> None:
        mem = PasteAppMemory(data_dir=self.tmp_path, enabled=True, stale_days=30)
        mem.record("com.old.app", "plain")
        mem.record("com.new.app", "markdown")

        # Искусственно состарим запись
        stale_date = (datetime.now(tz=timezone.utc) - timedelta(days=31)).isoformat()
        with mem._lock:
            mem._data["com.old.app"]["last_used"] = stale_date
            mem._save()

        removed = mem.cleanup_stale()
        self.assertEqual(removed, 1)
        self.assertIsNone(mem.get_profile_for("com.old.app"))
        self.assertEqual(mem.get_profile_for("com.new.app"), "markdown")

    def test_cleanup_no_stale(self) -> None:
        mem = PasteAppMemory(data_dir=self.tmp_path, enabled=True, stale_days=30)
        mem.record("com.fresh.app", "html")
        removed = mem.cleanup_stale()
        self.assertEqual(removed, 0)


class TestPasteAppMemoryUnknownApp(unittest.TestCase):
    """4. unknown_app — None для незарегистрированного bundle_id."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mem = _make_mem(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_unknown_returns_none(self) -> None:
        result = self.mem.get_profile_for("com.unknown.app")
        self.assertIsNone(result)

    def test_empty_bundle_id_returns_none(self) -> None:
        result = self.mem.get_profile_for("")
        self.assertIsNone(result)


class TestPasteAppMemoryDisabled(unittest.TestCase):
    """5. disabled — record и get_profile_for возвращают None / не пишут."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_record_ignored_when_disabled(self) -> None:
        mem = _make_mem(self.tmp_path, enabled=False)
        mem.record("com.tinyspeck.slackmacgap", "markdown")  # должно молча игнорироваться
        self.assertIsNone(mem.get_profile_for("com.tinyspeck.slackmacgap"))

    def test_list_profiles_returns_empty_when_disabled(self) -> None:
        mem = _make_mem(self.tmp_path, enabled=False)
        mem.record("com.apple.mail", "plain")
        profiles = mem.list_profiles()
        # При disabled record ничего не записывает
        self.assertEqual(profiles, [])

    def test_get_returns_none_when_disabled(self) -> None:
        # Сначала записываем (enabled), потом отключаем
        mem = _make_mem(self.tmp_path, enabled=True)
        mem.record("com.apple.Notes", "notes")
        mem.enabled = False
        self.assertIsNone(mem.get_profile_for("com.apple.Notes"))


class TestPasteAppMemoryIPC(unittest.TestCase):
    """6. IPC handle_* методы."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mem = _make_mem(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_handle_record_and_get(self) -> None:
        rec = self.mem.handle_record_paste_app_profile(
            {"bundle_id": "com.tinyspeck.slackmacgap", "profile": "markdown"}
        )
        self.assertTrue(rec["ok"])
        get = self.mem.handle_get_paste_profile_for_app(
            {"bundle_id": "com.tinyspeck.slackmacgap"}
        )
        self.assertEqual(get["profile"], "markdown")

    def test_handle_list_app_profiles(self) -> None:
        self.mem.record("com.tinyspeck.slackmacgap", "markdown")
        self.mem.record("com.apple.mail", "plain")
        resp = self.mem.handle_list_app_profiles({})
        bundles = [e["bundle_id"] for e in resp["profiles"]]
        self.assertIn("com.tinyspeck.slackmacgap", bundles)
        self.assertIn("com.apple.mail", bundles)

    def test_handle_record_missing_bundle_id(self) -> None:
        resp = self.mem.handle_record_paste_app_profile({"profile": "plain"})
        self.assertFalse(resp["ok"])

    def test_handle_record_invalid_profile(self) -> None:
        resp = self.mem.handle_record_paste_app_profile(
            {"bundle_id": "com.example.app", "profile": "invalid_format"}
        )
        self.assertFalse(resp["ok"])
        self.assertIn("error", resp)


class TestPasteAppMemoryMultiBundle(unittest.TestCase):
    """7. multi_bundle — разные bundle_id → разные профили."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mem = _make_mem(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_independent_profiles(self) -> None:
        self.mem.record("com.tinyspeck.slackmacgap", "markdown")
        self.mem.record("com.apple.mail", "plain")
        self.mem.record("com.apple.Notes", "notes")
        self.mem.record("com.microsoft.teams", "html")

        self.assertEqual(self.mem.get_profile_for("com.tinyspeck.slackmacgap"), "markdown")
        self.assertEqual(self.mem.get_profile_for("com.apple.mail"), "plain")
        self.assertEqual(self.mem.get_profile_for("com.apple.Notes"), "notes")
        self.assertEqual(self.mem.get_profile_for("com.microsoft.teams"), "html")

    def test_list_all_bundles(self) -> None:
        bundles = ["com.tinyspeck.slackmacgap", "com.apple.mail", "com.apple.Notes"]
        for b in bundles:
            self.mem.record(b, "plain")
        profiles = self.mem.list_profiles()
        listed = [p["bundle_id"] for p in profiles]
        for b in bundles:
            self.assertIn(b, listed)


class TestPasteAppMemoryInvalidProfile(unittest.TestCase):
    """8. invalid_profile — ValueError на неизвестный профиль."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.mem = _make_mem(Path(self._tmpdir.name))

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_invalid_profile_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.mem.record("com.example.app", "richtext")

    def test_valid_profiles_accepted(self) -> None:
        for profile in ("plain", "markdown", "html", "telegram", "email", "notes"):
            self.mem.record(f"com.example.{profile}", profile)
            self.assertEqual(self.mem.get_profile_for(f"com.example.{profile}"), profile)


if __name__ == "__main__":
    unittest.main()
