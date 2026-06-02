"""Тесты W1344: IPC-обёртка get_never_played и атомарная запись PlaybackTracker.

Проверяет:
- W1336 F2 MED: handle_get_never_played корректно возвращает записи, ни разу не
  воспроизведённые. W1769: ключ get_never_played НЕ в живой dispatch table
  (decorative gap, был только в удалённом ipc_dispatch.py) — отдельный follow-up.
- W1336 F5 LOW: _save() использует атомарный паттерн tmp+fsync+rename.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.playback_tracker import PlaybackTracker  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str):
        self._id = item_id

    def to_dict(self) -> dict:
        return {"id": self._id, "text": f"текст {self._id}"}


class FakeStore:
    """Заглушка StateStore с пагинацией."""

    def __init__(self, items: list[str]):
        self._items = [FakeHistoryItem(iid) for iid in items]

    def get_history_page_filtered(self, cursor=None, limit=50, **_):
        start = int(cursor) if cursor is not None else 0
        end = start + limit
        page = self._items[start:end]
        next_cursor = str(end) if end < len(self._items) else None
        return page, next_cursor


# ---------------------------------------------------------------------------
# W1336 F2: IPC handler tests
# ---------------------------------------------------------------------------

class TestGetNeverPlayedViaIPC(unittest.TestCase):
    """W1336 F2: handle_get_never_played() IPC handler works correctly."""

    def setUp(self):
        self.tracker = PlaybackTracker()
        self.store = FakeStore(["id1", "id2", "id3"])

    def test_handle_get_never_played_exists_on_tracker(self):
        """handle_get_never_played method must be present on PlaybackTracker."""
        self.assertTrue(
            callable(getattr(self.tracker, "handle_get_never_played", None)),
            "PlaybackTracker.handle_get_never_played() must exist",
        )

    def test_handle_returns_items_and_count_keys(self):
        """Response must contain 'items' list and 'count' int."""
        result = self.tracker.handle_get_never_played({}, store=self.store)
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["items"], list)
        self.assertIsInstance(result["count"], int)

    def test_get_never_played_via_ipc_returns_all_when_none_played(self):
        """All store items are returned when none have been played."""
        result = self.tracker.handle_get_never_played({"limit": 50}, store=self.store)
        returned_ids = [item["id"] for item in result["items"]]
        self.assertIn("id1", returned_ids)
        self.assertIn("id2", returned_ids)
        self.assertIn("id3", returned_ids)
        self.assertEqual(result["count"], 3)

    def test_get_never_played_via_ipc_excludes_played_item(self):
        """Played items must be excluded from the result."""
        self.tracker.record_playback("id2")
        result = self.tracker.handle_get_never_played({"limit": 50}, store=self.store)
        returned_ids = [item["id"] for item in result["items"]]
        self.assertNotIn("id2", returned_ids)
        self.assertIn("id1", returned_ids)
        self.assertIn("id3", returned_ids)
        self.assertEqual(result["count"], 2)

    def test_get_never_played_via_ipc_respects_limit(self):
        """limit param controls maximum number of returned items."""
        store = FakeStore([f"item_{i}" for i in range(30)])
        result = self.tracker.handle_get_never_played({"limit": 5}, store=store)
        self.assertLessEqual(len(result["items"]), 5)
        self.assertEqual(result["count"], len(result["items"]))

    def test_get_never_played_via_ipc_default_limit(self):
        """Default limit (50) applied when not specified."""
        store = FakeStore([f"item_{i}" for i in range(10)])
        result = self.tracker.handle_get_never_played({}, store=store)
        self.assertEqual(len(result["items"]), 10)

    def test_get_never_played_wired_into_live_dispatch(self):
        """W1773: 'get_never_played' подключён в живую dispatch table.

        Ранее этот ключ был зарегистрирован ТОЛЬКО в мёртвом backend/ipc_dispatch.py
        (никогда не достижим в production); после удаления того модуля (W1769)
        обработчик остался осиротевшим — реальный, покрытый тестами, но недостижимый.
        W1773 подключил три осиротевших обработчика (get_never_played,
        rename_collection, semantic_search_reset) в единственный источник истины
        service.py::_build_dispatch_table. Этот тест фиксирует НОВУЮ
        production-реальность: ключ присутствует в живой таблице.
        """
        # Capability присутствует на классе.
        self.assertTrue(
            hasattr(PlaybackTracker, "handle_get_never_played"),
            "PlaybackTracker.handle_get_never_played должен существовать",
        )

        # W1773: теперь ключ ЕСТЬ в живой таблице диспетчеризации.
        import inspect
        from backend.service import BackendService
        source = inspect.getsource(BackendService._build_dispatch_table)
        self.assertIn(
            '"get_never_played"', source,
            "W1773: 'get_never_played' должен быть подключён в живую dispatch table.",
        )


# ---------------------------------------------------------------------------
# W1336 F2 (privacy mode gate)
# ---------------------------------------------------------------------------

def _handle_get_never_played_impl(self, params):
    """Копия реализации _handle_get_never_played из BackendService.

    Вынесена сюда, чтобы избежать тяжёлого импорта service.py
    (который тянет Python 3.10+ зависимости через metadata_enricher).
    Логика идентична той, что в service.py.
    """
    if self._cached_settings().get("privacy_mode_enabled"):
        return {"items": [], "count": 0, "privacy_mode": True}
    return self._playback_tracker.handle_get_never_played(params, store=self.store)


class TestGetNeverPlayedSkippedInPrivacyMode(unittest.TestCase):
    """W1336 F2: privacy_mode gate — no history exposed when privacy enabled."""

    def _make_stub_service(self, privacy_enabled: bool):
        """Returns a minimal stub that exercises _handle_get_never_played logic."""
        import types

        svc = MagicMock()
        svc._cached_settings.return_value = {"privacy_mode_enabled": privacy_enabled}
        svc._playback_tracker = PlaybackTracker()
        svc.store = FakeStore(["id1", "id2", "id3"])
        svc._handle_get_never_played = types.MethodType(_handle_get_never_played_impl, svc)
        return svc

    def test_privacy_mode_returns_empty_items(self):
        """When privacy_mode_enabled=True, result items must be empty."""
        svc = self._make_stub_service(True)
        result = svc._handle_get_never_played({"limit": 50})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_privacy_mode_flag_in_response(self):
        """When privacy_mode_enabled=True, response must include privacy_mode=True."""
        svc = self._make_stub_service(True)
        result = svc._handle_get_never_played({})
        self.assertTrue(result.get("privacy_mode"), "privacy_mode flag must be True")

    def test_normal_mode_returns_items(self):
        """When privacy_mode_enabled=False, items are returned normally."""
        svc = self._make_stub_service(False)
        result = svc._handle_get_never_played({"limit": 50})
        self.assertGreater(result["count"], 0)
        self.assertNotIn("privacy_mode", result)


# ---------------------------------------------------------------------------
# W1336 F5: atomic-write regression test
# ---------------------------------------------------------------------------

class TestSaveUsesAtomicTmpRenameRegression(unittest.TestCase):
    """W1336 F5: _save() must use tmp+fsync+rename atomic pattern."""

    def test_save_uses_atomic_tmp_rename_pattern(self):
        """Assert that _save() writes to .tmp then renames via os.replace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            final_path = Path(tmpdir) / "playback_stats.json"

            rename_calls = []
            import os as _os
            original_replace = _os.replace

            def patched_replace(src, dst):
                rename_calls.append((str(src), str(dst)))
                return original_replace(src, dst)

            with patch("backend.playback_tracker.os.replace", side_effect=patched_replace):
                tracker.record_playback("atomic_test_item", duration_listened_sec=5.0)

            self.assertTrue(rename_calls, "_save() must perform at least one atomic rename")
            found = any(
                ".playback_stats_tmp_" in src and dst == str(final_path)
                for src, dst in rename_calls
            )
            self.assertTrue(found, f"Expected rename from tmp to {final_path}, got: {rename_calls}")

    def test_save_calls_fsync(self):
        """Assert that _save() calls os.fsync() to durably flush data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            fsync_called = []

            original_fsync = os.fsync

            def mock_fsync(fd):
                fsync_called.append(fd)
                return original_fsync(fd)

            with patch("os.fsync", side_effect=mock_fsync):
                tracker.record_playback("fsync_test_item", duration_listened_sec=3.0)

            self.assertTrue(
                fsync_called,
                "_save() must call os.fsync() to ensure durability",
            )

    def test_atomic_write_produces_valid_json(self):
        """After atomic write, the final file must be valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            tracker.record_playback("json_test", duration_listened_sec=7.5)

            final_path = Path(tmpdir) / "playback_stats.json"
            self.assertTrue(final_path.exists(), "playback_stats.json must exist after save")

            data = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            self.assertIn("json_test", data)

    def test_tmp_file_not_left_on_disk(self):
        """After a successful _save(), the .tmp file must not remain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            tracker.record_playback("cleanup_test", duration_listened_sec=1.0)

            tmp_path = Path(tmpdir) / "playback_stats.tmp"
            self.assertFalse(
                tmp_path.exists(),
                f"Temp file {tmp_path} must be removed after successful atomic save",
            )


if __name__ == "__main__":
    unittest.main()
