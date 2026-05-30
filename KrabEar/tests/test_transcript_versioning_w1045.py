"""Тесты для W1040 HIGH findings в TranscriptVersionManager и HistoryService.

Покрывает:
- F1 HIGH: per-item version cap (MAX_VERSIONS_PER_ITEM), dropping oldest on overflow
- F2 HIGH: cascade delete from transcript_versions.ndjson on delete_history_item
           and on cleanup_old_history
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcript_versioning import TranscriptVersionManager, MAX_VERSIONS_PER_ITEM
from backend.history_service import HistoryService


# ---------------------------------------------------------------------------
# F1: per-item version cap
# ---------------------------------------------------------------------------

class TestVersionCapDropsOldest(unittest.TestCase):
    """F1 HIGH: per-item version cap enforced, oldest versions dropped."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_version_cap_drops_oldest(self) -> None:
        """После добавления MAX+1 версий старейшая удаляется."""
        cap = MAX_VERSIONS_PER_ITEM
        item_id = "item_cap_test"
        # Добавляем ровно cap версий — все должны сохраниться
        for i in range(1, cap + 1):
            self.manager.save_version(item_id, f"Text v{i}", "manual")

        versions = self.manager.get_versions(item_id)
        self.assertEqual(len(versions), cap, f"Ожидалось ровно {cap} версий после заполнения cap")

        # Добавляем (cap+1)-ю версию — старейшая (version_num=1) должна быть удалена
        self.manager.save_version(item_id, f"Text v{cap + 1}", "manual")
        versions_after = self.manager.get_versions(item_id)
        self.assertEqual(
            len(versions_after), cap,
            f"После превышения cap должно остаться {cap} версий",
        )
        version_nums = sorted(v["version_num"] for v in versions_after)
        # Версия 1 (старейшая) должна быть удалена; минимум теперь 2
        self.assertEqual(
            version_nums[0], 2,
            "Старейшая версия (version_num=1) должна быть удалена при превышении cap",
        )
        # Новейшая версия должна присутствовать
        self.assertEqual(version_nums[-1], cap + 1, "Новейшая версия должна присутствовать")

    def test_version_cap_applies_per_item(self) -> None:
        """Cap применяется независимо для каждого item_id."""
        cap = MAX_VERSIONS_PER_ITEM
        item_a = "item_a_cap"
        item_b = "item_b_cap"

        for i in range(cap + 2):
            self.manager.save_version(item_a, f"A v{i}", "manual")
            self.manager.save_version(item_b, f"B v{i}", "manual")

        versions_a = self.manager.get_versions(item_a)
        versions_b = self.manager.get_versions(item_b)
        self.assertLessEqual(len(versions_a), cap)
        self.assertLessEqual(len(versions_b), cap)

    def test_version_cap_not_exceeded(self) -> None:
        """get_versions никогда не возвращает больше MAX_VERSIONS_PER_ITEM версий."""
        cap = MAX_VERSIONS_PER_ITEM
        item_id = "item_overflow"
        # Добавляем в 2× раз больше версий чем cap
        for i in range(cap * 2):
            self.manager.save_version(item_id, f"Text {i}", "manual")

        versions = self.manager.get_versions(item_id)
        self.assertLessEqual(
            len(versions), cap,
            f"Количество версий не должно превышать cap={cap}",
        )

    def test_within_cap_no_data_loss(self) -> None:
        """Пока cap не превышен — данные не удаляются."""
        item_id = "item_under_cap"
        count = min(5, MAX_VERSIONS_PER_ITEM)
        for i in range(1, count + 1):
            self.manager.save_version(item_id, f"Text {i}", "manual")

        versions = self.manager.get_versions(item_id)
        self.assertEqual(len(versions), count, "Версии до cap не должны удаляться")


# ---------------------------------------------------------------------------
# F2: cascade delete — delete_history_item
# ---------------------------------------------------------------------------

def _make_fake_store(item_id: str, ts: str | None = None):
    """Создаёт минимальный stub StateStore для тестов HistoryService."""
    item = SimpleNamespace(id=item_id, ts=ts or datetime.now(timezone.utc).isoformat())
    store = MagicMock()
    store.delete_history_item.return_value = True
    store.data_dir = Path("/tmp")
    return store, item


class TestDeleteHistoryItemCascadesVersions(unittest.TestCase):
    """F2 HIGH: delete_history_item вызывает cascade delete версий."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.versions_mgr = TranscriptVersionManager(self.temp_dir.name)
        self.store = MagicMock()
        self.store.delete_history_item.return_value = True
        self.store.data_dir = Path(self.temp_dir.name)
        self.history_svc = HistoryService(
            store=self.store,
            transcript_versions=self.versions_mgr,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_delete_history_item_cascades_versions(self) -> None:
        """delete_history_item удаляет все версии транскрипции из хранилища."""
        item_id = "item_cascade_delete"
        # Добавляем несколько версий
        self.versions_mgr.save_version(item_id, "Version 1", "stt_raw")
        self.versions_mgr.save_version(item_id, "Version 2", "manual")
        self.versions_mgr.save_version(item_id, "Version 3", "llm_rewrite")

        # Убедились, что версии есть
        self.assertEqual(len(self.versions_mgr.get_versions(item_id)), 3)

        # Удаляем запись истории
        self.history_svc.handle_delete_history_item({"id": item_id})

        # Версии должны быть удалены (cascade)
        remaining = self.versions_mgr.get_versions(item_id)
        self.assertEqual(
            remaining, [],
            f"После delete_history_item версии для {item_id!r} должны быть удалены, "
            f"осталось: {remaining}",
        )

    def test_delete_does_not_affect_other_items_versions(self) -> None:
        """Каскадное удаление не затрагивает версии других item_id."""
        item_a = "item_del_a"
        item_b = "item_del_b"
        self.versions_mgr.save_version(item_a, "A v1", "manual")
        self.versions_mgr.save_version(item_b, "B v1", "manual")
        self.versions_mgr.save_version(item_b, "B v2", "manual")

        # Удаляем только item_a
        self.history_svc.handle_delete_history_item({"id": item_a})

        self.assertEqual(len(self.versions_mgr.get_versions(item_a)), 0)
        self.assertEqual(len(self.versions_mgr.get_versions(item_b)), 2)

    def test_delete_without_versions_ok(self) -> None:
        """delete_history_item работает корректно, если версий для item не существует."""
        item_id = "item_no_versions"
        result = self.history_svc.handle_delete_history_item({"id": item_id})
        self.assertTrue(result.get("deleted"))

    def test_delete_without_versions_manager_skips_cascade(self) -> None:
        """Если _transcript_versions is None — cascade пропускается без ошибки."""
        store = MagicMock()
        store.delete_history_item.return_value = True
        store.data_dir = Path(self.temp_dir.name)
        svc = HistoryService(store=store, transcript_versions=None)
        result = svc.handle_delete_history_item({"id": "some_item"})
        self.assertTrue(result.get("deleted"))


# ---------------------------------------------------------------------------
# F2: cascade delete — cleanup_old_history
# ---------------------------------------------------------------------------

class FakeLock:
    """Контекстный менеджер — stub для store._lock()."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class TestCleanupOldHistoryCascadesVersions(unittest.TestCase):
    """F2 HIGH: cleanup_old_history вызывает bulk cascade delete версий."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.versions_mgr = TranscriptVersionManager(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_store_with_items(self, old_ids: list[str], recent_ids: list[str]):
        """Собирает mock store с нужными items."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=91)
        old_ts = (cutoff - timedelta(days=1)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        old_items = [SimpleNamespace(id=i, ts=old_ts) for i in old_ids]
        recent_items = [SimpleNamespace(id=i, ts=new_ts) for i in recent_ids]
        all_items = old_items + recent_items

        store = MagicMock()
        store.data_dir = Path(self.temp_dir.name)
        store._lock = MagicMock(return_value=FakeLock())
        store._load_active_items_unlocked.return_value = all_items
        store._append_ndjson = MagicMock()
        store.tombstones_path = Path(self.temp_dir.name) / "tombstones.ndjson"
        return store

    def test_cleanup_old_history_cascades_versions(self) -> None:
        """cleanup_old_history удаляет версии для старых удалённых записей."""
        old_id = "old_item_cleanup"
        recent_id = "recent_item_cleanup"

        # Добавляем версии для обоих items
        self.versions_mgr.save_version(old_id, "Old v1", "stt_raw")
        self.versions_mgr.save_version(old_id, "Old v2", "manual")
        self.versions_mgr.save_version(recent_id, "Recent v1", "stt_raw")

        store = self._make_store_with_items([old_id], [recent_id])
        svc = HistoryService(store=store, transcript_versions=self.versions_mgr)

        result = svc.handle_cleanup_old_history({"older_than_days": 90})

        self.assertEqual(result["deleted_count"], 1)
        # Версии старого item должны быть удалены
        self.assertEqual(
            self.versions_mgr.get_versions(old_id), [],
            "Версии старого item должны быть удалены после cleanup_old_history",
        )
        # Версии нового item должны остаться
        self.assertEqual(
            len(self.versions_mgr.get_versions(recent_id)), 1,
            "Версии нового item не должны пострадать",
        )

    def test_cleanup_no_old_items_no_cascade(self) -> None:
        """Если старых items нет — cascade не вызывается (нет ошибок)."""
        recent_id = "recent_only"
        self.versions_mgr.save_version(recent_id, "v1", "manual")
        store = self._make_store_with_items([], [recent_id])
        svc = HistoryService(store=store, transcript_versions=self.versions_mgr)
        result = svc.handle_cleanup_old_history({"older_than_days": 90})
        self.assertEqual(result["deleted_count"], 0)
        # Версии recent item не тронуты
        self.assertEqual(len(self.versions_mgr.get_versions(recent_id)), 1)

    def test_cleanup_without_versions_manager_skips_cascade(self) -> None:
        """Если _transcript_versions is None — cascade пропускается без ошибки."""
        old_id = "old_no_mgr"
        store = self._make_store_with_items([old_id], [])
        svc = HistoryService(store=store, transcript_versions=None)
        result = svc.handle_cleanup_old_history({"older_than_days": 90})
        self.assertEqual(result["deleted_count"], 1)

    def test_cleanup_multiple_old_items_all_cascaded(self) -> None:
        """Все старые items удаляются каскадно при bulk cleanup."""
        old_ids = [f"old_{i}" for i in range(5)]
        for oid in old_ids:
            self.versions_mgr.save_version(oid, f"Text for {oid}", "manual")

        store = self._make_store_with_items(old_ids, [])
        svc = HistoryService(store=store, transcript_versions=self.versions_mgr)
        result = svc.handle_cleanup_old_history({"older_than_days": 90})

        self.assertEqual(result["deleted_count"], 5)
        for oid in old_ids:
            self.assertEqual(
                self.versions_mgr.get_versions(oid), [],
                f"Версии для {oid!r} должны быть удалены",
            )


if __name__ == "__main__":
    unittest.main()
