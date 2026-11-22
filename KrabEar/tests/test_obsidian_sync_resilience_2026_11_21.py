"""KE05D-18 / KE05D-14 / KE05D-05: устойчивость ObsidianSyncManager.

* Сбой статус-события (``event_bus.emit``) не должен прерывать синхронизацию: события только
  информируют UI, а раньше исключение в конце итерации обрывало цикл после записи файла.
* Отказ записи obsidian_sync.json не должен прятаться: sync() сообщает об ошибке состояния.
* purge при небезопасном ``folder`` не возвращает «0 удалено» как успех: неподтверждённая
  очистка обязана быть видна вызывающему (он пишет «obsidian» в secondary_errors).
"""

from __future__ import annotations

from _plaintext_export_test_helpers import off_authorizer

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.obsidian_sync import ObsidianSyncManager  # noqa: E402


def _items(n: int = 3) -> list[dict]:
    return [{"id": f"id{i:06d}", "ts": f"2026-01-0{i + 1}T10:00:00+00:00", "text": f"запись {i}"} for i in range(n)]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.data = self.root / "data"

    def _mgr(self, bus=None) -> ObsidianSyncManager:
        mgr = ObsidianSyncManager(data_dir=self.data, event_bus=bus, plaintext_export_authorizer=off_authorizer())
        mgr.configure(str(self.vault), folder="Notes")
        return mgr


class EventBusFailureDoesNotAbortSync(_Base):
    def test_every_emit_raising_still_syncs_everything(self):
        bus = MagicMock()
        bus.emit.side_effect = RuntimeError("event bus down")
        mgr = self._mgr(bus)
        result = mgr.sync(_items(3), force=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.synced_count, 3)
        self.assertEqual(len(list((self.vault / "Notes").glob("transcript_*.md"))), 3)
        self.assertIsNotNone(mgr.get_sync_status()["last_sync_ts"])
        self.assertGreater(bus.emit.call_count, 0)

    def test_emit_failure_only_on_progress_event(self):
        bus = MagicMock()
        calls = {"n": 0}

        def flaky(event, payload):
            calls["n"] += 1
            if payload.get("stage") == "syncing":
                raise OSError("pipe closed")

        bus.emit.side_effect = flaky
        mgr = self._mgr(bus)
        result = mgr.sync(_items(2), force=True)
        self.assertEqual(result.synced_count, 2)
        self.assertEqual(result.errors, [])


class StateSaveFailureIsReported(_Base):
    def test_state_write_failure_is_an_error_in_result(self):
        mgr = self._mgr()
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            result = mgr.sync(_items(1), force=True)
        self.assertEqual(result.synced_count, 1)
        self.assertTrue(any("state" in e.lower() for e in result.errors), result.errors)


class PurgeUnsafeFolderIsNotSilentSuccess(_Base):
    def test_tampered_folder_raises_and_deletes_nothing_outside(self):
        mgr = self._mgr()
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "important.md"
        victim.write_text("keep me", encoding="utf-8")
        with mgr._lock:
            mgr._folder = "../../" + outside.name
        with self.assertRaises(OSError):
            mgr.purge_all_synced_files()
        self.assertTrue(victim.exists())

    def test_normal_purge_still_returns_count(self):
        mgr = self._mgr()
        mgr.sync(_items(2), force=True)
        self.assertEqual(mgr.purge_all_synced_files(), 2)


if __name__ == "__main__":
    unittest.main()
