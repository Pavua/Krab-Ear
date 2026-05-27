"""Tests for W1426 F2 HIGH fix: HistoryService semantic_searcher wiring.

Verifies:
- HistoryService accepts semantic_searcher= kwarg (W1431).
- handle_delete_history_item calls semantic_searcher.remove_item() on success.
- Works safely when semantic_searcher=None (default).
- Works safely when remove_item() raises.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService
from backend.state_store import StateStore


class TestHistoryServiceSemanticWiring(unittest.TestCase):
    """HistoryService получает semantic_searcher и удаляет эмбеддинги при delete."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    # ------------------------------------------------------------------
    # 1. Constructor accepts semantic_searcher kwarg
    # ------------------------------------------------------------------

    def test_history_service_constructed_with_semantic_searcher(self) -> None:
        """HistoryService.__init__ принимает semantic_searcher= без ошибок."""
        mock_searcher = MagicMock()
        svc = HistoryService(store=self.store, semantic_searcher=mock_searcher)
        self.assertIs(svc._semantic_searcher, mock_searcher)

    def test_history_service_default_semantic_searcher_is_none(self) -> None:
        """По умолчанию _semantic_searcher=None (обратная совместимость)."""
        svc = HistoryService(store=self.store)
        self.assertIsNone(svc._semantic_searcher)

    # ------------------------------------------------------------------
    # 2. handle_delete_history_item calls remove_item on searcher
    # ------------------------------------------------------------------

    def test_delete_history_item_removes_from_semantic_index(self) -> None:
        """При удалении записи semantic_searcher.remove_item() вызывается с item_id."""
        mock_searcher = MagicMock()
        svc = HistoryService(store=self.store, semantic_searcher=mock_searcher)

        item = self.store.add_history_item(text="тест семантики", paste_status="ok")

        svc.handle_delete_history_item({"id": item.id})

        mock_searcher.remove_item.assert_called_once_with(item.id)

    def test_delete_history_item_returns_deleted_true(self) -> None:
        """handle_delete_history_item возвращает {deleted: True} как обычно."""
        mock_searcher = MagicMock()
        svc = HistoryService(store=self.store, semantic_searcher=mock_searcher)

        item = self.store.add_history_item(text="удаляемое сообщение", paste_status="ok")
        result = svc.handle_delete_history_item({"id": item.id})

        self.assertEqual(result, {"deleted": True})

    # ------------------------------------------------------------------
    # 3. Safe when semantic_searcher=None (default path)
    # ------------------------------------------------------------------

    def test_delete_safe_when_semantic_searcher_none(self) -> None:
        """Удаление работает без ошибок если semantic_searcher не передан."""
        svc = HistoryService(store=self.store)

        item = self.store.add_history_item(text="безопасный тест", paste_status="ok")
        result = svc.handle_delete_history_item({"id": item.id})

        self.assertEqual(result, {"deleted": True})

    # ------------------------------------------------------------------
    # 4. remove_item() exception is caught — store delete still succeeds
    # ------------------------------------------------------------------

    def test_delete_survives_semantic_searcher_remove_exception(self) -> None:
        """Если remove_item() бросает исключение — оно поглощается, delete не падает."""
        mock_searcher = MagicMock()
        mock_searcher.remove_item.side_effect = RuntimeError("index unavailable")

        svc = HistoryService(store=self.store, semantic_searcher=mock_searcher)

        item = self.store.add_history_item(text="краш тест", paste_status="ok")

        # Should not raise, warning is logged internally
        result = svc.handle_delete_history_item({"id": item.id})
        self.assertEqual(result, {"deleted": True})
        mock_searcher.remove_item.assert_called_once_with(item.id)

    # ------------------------------------------------------------------
    # 5. Empty ID raises ValueError before store or searcher is touched
    # ------------------------------------------------------------------

    def test_delete_raises_for_empty_id_no_searcher_call(self) -> None:
        """ValueError поднимается если id пустой; remove_item не вызывается."""
        mock_searcher = MagicMock()
        svc = HistoryService(store=self.store, semantic_searcher=mock_searcher)

        with self.assertRaises(ValueError):
            svc.handle_delete_history_item({"id": ""})

        mock_searcher.remove_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
