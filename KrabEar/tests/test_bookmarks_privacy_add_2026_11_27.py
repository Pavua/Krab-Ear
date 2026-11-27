"""add_bookmark под privacy mode: закладка не создаётся.

Находки ревью R-Krab-Ear-fix-04 (пункт 12) и сверка с base 27.11.2026: гейты privacy стояли
только на list_bookmarks/list_all_bookmarks, а add_bookmark писал в bookmarks.ndjson
(session_id + offset + заметка пользователя) и при включённой приватности. Чтение настройки
fail-closed: если настройку прочитать нельзя — закладку не создаём (потеря закладки
обратима нажатием, тихая запись под privacy — нет).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.bookmarks import BookmarkManager  # noqa: E402


def _ndjson_text(data_dir: Path) -> str:
    path = data_dir / "bookmarks.ndjson"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class BookmarksPrivacyAddTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="krab_ear_bm_priv_")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def _manager(self, settings: dict | None = None, raises: bool = False) -> BookmarkManager:
        def settings_get(key, default=None):
            if raises:
                raise RuntimeError("settings недоступны")
            return (settings or {}).get(key, default)

        return BookmarkManager(data_dir=self.data_dir, settings_get=settings_get)

    def test_privacy_on_refuses_and_writes_nothing(self):
        mgr = self._manager({"privacy_mode_enabled": True})
        res = mgr.handle_add_bookmark(
            {"session_id": "sess-secret", "offset_sec": 12.5, "note": "приватная заметка"}
        )
        self.assertEqual(res, {"ok": False, "reason": "privacy_mode_active"})
        text = _ndjson_text(self.data_dir)
        self.assertNotIn("sess-secret", text)
        self.assertNotIn("приватная заметка", text)
        self.assertEqual(text.strip(), "")

    def test_privacy_off_still_creates_bookmark(self):
        mgr = self._manager({"privacy_mode_enabled": False})
        res = mgr.handle_add_bookmark({"session_id": "s1", "offset_sec": 3.0, "note": "ok"})
        self.assertIn("bookmark", res)
        self.assertIn("s1", _ndjson_text(self.data_dir))

    def test_settings_read_failure_is_fail_closed(self):
        mgr = self._manager(raises=True)
        res = mgr.handle_add_bookmark({"session_id": "s2", "offset_sec": 1.0})
        self.assertEqual(res, {"ok": False, "reason": "privacy_mode_active"})
        self.assertEqual(_ndjson_text(self.data_dir).strip(), "")

    def test_privacy_gate_runs_before_param_validation_leaks_nothing(self):
        """Под privacy невалидные параметры не раскрывают ничего и не пишут."""
        mgr = self._manager({"privacy_mode_enabled": True})
        res = mgr.handle_add_bookmark({"session_id": "s3"})  # offset_sec отсутствует
        self.assertEqual(res, {"ok": False, "reason": "privacy_mode_active"})


if __name__ == "__main__":
    unittest.main()
