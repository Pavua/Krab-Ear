"""Follow-up ревью #2229 F6: неполное удаление эмбеддинга не выдаётся за полное.

``SemanticSearcher.remove_item`` бросает ``SnapshotNotPersistedError``, если запись
убрана из RAM, а снимок на диске не перезаписан (эмбеддинг вернётся после рестарта).
``handle_delete_history_item`` это уже учитывает (``complete: False``). Остальные пути
удаления только писали warning и рапортовали полный успех:

  * merge_recordings(delete_originals=True) — RecordingMerger._delete_one;
  * archive_items — ArchiveManager._archive_side_effects;
  * cleanup_old_history — HistoryService.handle_cleanup_old_history (и PurgeScheduler).

Контракт (минимально ломающий, как у delete_history_item): при полном удалении ответ
не меняется; при неполном добавляются ``complete: False``, ``reason`` и
``incomplete_ids`` (id, чьи вторичные индексы не очищены). Основное удаление остаётся
выполненным — счётчики/списки удалённых не меняются.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager  # noqa: E402
from backend.history_service import HistoryService  # noqa: E402
from backend.models import HistoryItem  # noqa: E402
from backend.recording_merger import RecordingMerger  # noqa: E402
from backend.semantic_search import SnapshotNotPersistedError  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FlakySearcher:
    """Снимок на диске не пишется для id из ``fail_ids``."""

    def __init__(self, fail_ids=()) -> None:
        self.fail_ids = set(fail_ids)
        self.removed: list[str] = []

    def remove_item(self, item_id: str) -> bool:
        if item_id in self.fail_ids:
            raise SnapshotNotPersistedError("диск отказал")
        self.removed.append(item_id)
        return True

    def index_item(self, item_id: str, text: str) -> None:
        pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.store = StateStore(data_dir=self.data_dir)
        self._n = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, text: str, *, old: bool = False) -> HistoryItem:
        self._n += 1
        base = datetime(2001, 1, 1, tzinfo=timezone.utc) if old else datetime.now(timezone.utc)
        item = HistoryItem.create(text=text, paste_status="success")
        item.ts = (base + timedelta(seconds=self._n)).isoformat(timespec="seconds")
        with self.store._lock():
            self.store._append_ndjson(self.store.history_path, item.to_dict())
        return item


class TestCleanupOldHistoryReportsIncomplete(_Base):
    def _svc(self, searcher) -> HistoryService:
        svc = HistoryService(store=self.store)
        svc._semantic_searcher = searcher
        return svc

    def test_snapshot_failure_marks_result_incomplete(self) -> None:
        a = self._add("старая А", old=True)
        b = self._add("старая Б", old=True)
        self._add("свежая")
        result = self._svc(_FlakySearcher(fail_ids={b.id})).handle_cleanup_old_history(
            {"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 2, "основное удаление выполнено")
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("reason"), "semantic_index_cleanup_failed")
        self.assertEqual(result.get("incomplete_ids"), [b.id])
        self.assertNotIn(a.id, result["incomplete_ids"])

    def test_full_success_keeps_contract_unchanged(self) -> None:
        self._add("старая", old=True)
        result = self._svc(_FlakySearcher()).handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(set(result), {"deleted_count", "remaining"})


class TestArchiveReportsIncomplete(_Base):
    def test_snapshot_failure_marks_ipc_result_incomplete(self) -> None:
        a = self._add("архив А")
        b = self._add("архив Б")
        mgr = ArchiveManager(store=self.store, semantic_searcher=_FlakySearcher(fail_ids={a.id}))
        result = mgr.handle_archive_items({"item_ids": [a.id, b.id]})
        self.assertEqual(result["archived_count"], 2, "архивирование выполнено")
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("reason"), "semantic_index_cleanup_failed")
        self.assertEqual(result.get("incomplete_ids"), [a.id])

    def test_full_success_keeps_contract_unchanged(self) -> None:
        a = self._add("архив")
        mgr = ArchiveManager(store=self.store, semantic_searcher=_FlakySearcher())
        result = mgr.handle_archive_items({"item_ids": [a.id]})
        self.assertNotIn("complete", result)
        self.assertNotIn("incomplete_ids", result)


class TestMergeReportsIncomplete(_Base):
    def _merger(self, searcher) -> RecordingMerger:
        history = HistoryService(store=self.store)
        history._semantic_searcher = searcher
        merger = RecordingMerger()
        # Production wiring (service.py): merger → HistoryService.cascade_delete_artifacts.
        merger.cascade_delete_fn = history.cascade_delete_artifacts
        return merger

    def test_cascade_snapshot_failure_marks_result_incomplete(self) -> None:
        a = self._add("первый")
        b = self._add("второй")
        merger = self._merger(_FlakySearcher(fail_ids={b.id}))
        result = merger.merge_items([a.id, b.id], self.store, delete_originals=True)
        self.assertCountEqual(result["deleted_ids"], [a.id, b.id], "оригиналы удалены")
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("reason"), "semantic_index_cleanup_failed")
        self.assertEqual(result.get("incomplete_ids"), [b.id])

    def test_fallback_without_cascade_fn_marks_incomplete(self) -> None:
        a = self._add("первый")
        b = self._add("второй")
        merger = RecordingMerger(semantic_searcher=_FlakySearcher(fail_ids={a.id}))
        result = merger.merge_items([a.id, b.id], self.store, delete_originals=True)
        self.assertCountEqual(result["deleted_ids"], [a.id, b.id])
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("incomplete_ids"), [a.id])

    def test_full_success_keeps_contract_unchanged(self) -> None:
        a = self._add("первый")
        b = self._add("второй")
        result = self._merger(_FlakySearcher()).merge_items(
            [a.id, b.id], self.store, delete_originals=True)
        self.assertNotIn("complete", result)
        self.assertNotIn("incomplete_ids", result)


if __name__ == "__main__":
    unittest.main()
