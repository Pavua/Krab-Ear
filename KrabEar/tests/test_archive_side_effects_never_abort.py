"""archive_items: сбой вторичного каскада не обрывает архивирование.

До правки ``ArchiveManager._archive_side_effects`` звал
``remove_item_from_all_chains`` без try (вопреки собственному docstring «всё в
try/except»). Ошибка цепочек вылетала из archive_items ПОСЛЕ tombstone первой
записи: она уже в архиве, остальные — нет, а IPC-вызывающий получал ошибку, т.е.
частично выполненная операция выдавалась за полный отказ.

Теперь любой упавший шаг (версии, семантика, цепочки) логируется, архивирование
идёт дальше, а ответ помечается ``complete: false`` + ``incomplete_ids``;
``reason`` — ``semantic_index_cleanup_failed``, если среди сбоев эмбеддинг, иначе
``cascade_cleanup_failed`` (как у delete_history_item / merge_recordings).
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
from backend.models import HistoryItem  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _BrokenChains:
    def __init__(self, fail_ids=None) -> None:
        self.fail_ids = fail_ids  # None — падает всегда
        self.removed: list[str] = []

    def remove_item_from_all_chains(self, item_id: str) -> None:
        if self.fail_ids is None or item_id in self.fail_ids:
            raise OSError("recording_chains.json недоступен")
        self.removed.append(item_id)


class _BrokenVersions:
    def purge_versions_for_item(self, item_id: str) -> None:
        raise OSError("versions недоступны")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self.tmp.name))
        self._n = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, text: str) -> HistoryItem:
        self._n += 1
        item = HistoryItem.create(text=text, paste_status="success")
        item.ts = (datetime.now(timezone.utc) + timedelta(seconds=self._n)).isoformat(timespec="seconds")
        with self.store._lock():
            self.store._append_ndjson(self.store.history_path, item.to_dict())
        return item

    def _active_ids(self) -> set[str]:
        with self.store._lock():
            return {i.id for i in self.store._load_active_items_unlocked()}


class TestChainFailureDoesNotAbortArchive(_Base):
    def test_chain_failure_archives_all_and_reports_incomplete(self) -> None:
        a = self._add("первая")
        b = self._add("вторая")
        mgr = ArchiveManager(store=self.store)
        mgr._recording_chain_mgr = _BrokenChains()
        result = mgr.handle_archive_items({"item_ids": [a.id, b.id]})
        self.assertEqual(result["archived_count"], 2, "вторая запись тоже заархивирована")
        self.assertEqual(self._active_ids(), set(), "обе ушли из активной истории")
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("reason"), "cascade_cleanup_failed")
        self.assertEqual(result.get("incomplete_ids"), [a.id, b.id])

    def test_partial_chain_failure_marks_only_failed_id(self) -> None:
        a = self._add("первая")
        b = self._add("вторая")
        mgr = ArchiveManager(store=self.store)
        chains = _BrokenChains(fail_ids={b.id})
        mgr._recording_chain_mgr = chains
        result = mgr.handle_archive_items({"item_ids": [a.id, b.id]})
        self.assertEqual(result["archived_count"], 2)
        self.assertEqual(chains.removed, [a.id])
        self.assertEqual(result.get("incomplete_ids"), [b.id])

    def test_versions_failure_reports_incomplete(self) -> None:
        a = self._add("первая")
        mgr = ArchiveManager(store=self.store, transcript_versioner=_BrokenVersions())
        result = mgr.handle_archive_items({"item_ids": [a.id]})
        self.assertEqual(result["archived_count"], 1)
        self.assertIs(result.get("complete"), False)
        self.assertEqual(result.get("reason"), "cascade_cleanup_failed")
        self.assertEqual(result.get("incomplete_ids"), [a.id])

    def test_healthy_chains_keep_contract_unchanged(self) -> None:
        a = self._add("первая")
        mgr = ArchiveManager(store=self.store)
        mgr._recording_chain_mgr = _BrokenChains(fail_ids=set())
        result = mgr.handle_archive_items({"item_ids": [a.id]})
        self.assertEqual(set(result), {"archived_count", "archive_path", "size_mb"})


if __name__ == "__main__":
    unittest.main()
