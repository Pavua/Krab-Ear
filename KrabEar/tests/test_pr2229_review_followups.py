"""Регрессии по замечаниям ревью PR #2229 (Codex): контракт бэкенда ↔ потребители.

Корень четырёх замечаний: Swift-правки G3-MEM-06/07 требовали поле `ok == true`, а
бэкенд на УСПЕХЕ его не отдаёт (только на отказе: `ok: false`). Потребителей исправили
(Swift: отказ = явный `ok == false`, успех = квитанция), а тут закреплена ПРОИЗВОДИТЕЛЬСКАЯ
сторона: форма успешного ответа. Если кто-то снова потребует `ok` от этих методов —
этот файл покажет, что бэкенд его не обещает.

Дополнительно:
  * get_chain в обычном режиме несёт `privacy_mode: False` — Swift fail-closed на отсутствие;
  * пустой NPZ-снимок индекса возвращается в «неинициализированное» состояние и индекс
    снова принимает векторы;
  * верификация purge видит оставшийся embeddings_snapshot.npz.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_chain import RecordingChainManager  # noqa: E402
from backend.semantic_search import SemanticSearcher  # noqa: E402
from test_purge_all_data_w1730 import FakeStore, _make_svc  # noqa: E402


def _fake_vec(dim: int = 4):
    import numpy as np
    return np.ones(dim, dtype=np.float32) / float(dim) ** 0.5


def _searcher(tmpdir: str) -> SemanticSearcher:
    s = SemanticSearcher(data_dir=Path(tmpdir), model_name="test-model", enabled=True, max_items=0)
    s._model = object()
    s._model_loaded = True
    s._encode = lambda model, text, prefix="query: ": _fake_vec()
    return s


class ChainContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.store = FakeStore(data_dir=self._td.name)

    def test_start_chain_success_has_chain_id_and_no_ok_flag(self) -> None:
        """Успех = {"chain_id"} БЕЗ `ok`: потребитель не вправе требовать ok==true."""
        mgr = RecordingChainManager(store=self.store)
        result = mgr.handle_start_chain({"name": "Встреча"})
        self.assertTrue(result["chain_id"])
        self.assertNotIn("ok", result)

    def test_get_chain_normal_response_is_explicitly_not_private(self) -> None:
        mgr = RecordingChainManager(store=self.store, settings_fn=lambda: {"privacy_mode_enabled": False})
        cid = mgr.start_chain("Открытая")
        self.assertIs(mgr.get_chain(cid)["privacy_mode"], False)

    def test_get_chain_private_response_is_explicitly_private(self) -> None:
        mgr = RecordingChainManager(store=self.store, settings_fn=lambda: {"privacy_mode_enabled": True})
        cid = mgr.start_chain("Закрытая")
        priv = mgr.get_chain(cid)
        self.assertIs(priv["privacy_mode"], True)
        self.assertEqual(priv["items"], [])


class SttHotwordContractTests(unittest.TestCase):
    def test_add_hotword_success_returns_list_containing_word_without_ok(self) -> None:
        from backend.stt_management_service import STTManagementService

        svc = STTManagementService.__new__(STTManagementService)
        settings = MagicMock()
        settings.cached_settings.return_value = {"stt_hotwords": ["один"]}
        svc._settings_svc = settings
        result = svc.handle_add_stt_hotword({"word": "  Краб  "})
        self.assertIn("Краб", result["hotwords"])  # слово обрезано и присутствует
        self.assertNotIn("ok", result)


class EmptySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = self._td.name

    def test_removing_last_item_then_restart_keeps_index_usable(self) -> None:
        first = _searcher(self.root)
        self.assertTrue(first.index_item("only", "единственный"))
        self.assertTrue(first.remove_item("only"))
        self.assertTrue(first._snapshot_path.exists())  # пустой снимок на диске

        # «перезапуск backend»: новый экземпляр загружает пустой снимок
        second = _searcher(self.root)
        second._load_from_disk()
        self.assertIsNone(second._embeddings)  # а не матрица (0, 0)
        self.assertEqual(second._index, [])

        # главное: индексация после перезапуска работает (раньше падало на stack 0-колоночной)
        self.assertTrue(second.index_item("next", "следующий"))
        with second._index_lock:
            self.assertEqual(second._index, ["next"])
            self.assertEqual(second._embeddings.shape[0], 1)


class PurgeVerifySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def test_leftover_snapshot_is_flagged_by_purge_verification(self) -> None:
        svc = _make_svc(str(self.root), item_ids=["a"])
        (self.root / "embeddings_snapshot.npz").write_bytes(b"leftover")
        errors: list[str] = []
        svc._purge_verify_cleanup(errors, self.root)
        self.assertIn("purge_verification", errors)

    def test_clean_directory_passes_purge_verification(self) -> None:
        svc = _make_svc(str(self.root), item_ids=["a"])
        errors: list[str] = []
        svc._purge_verify_cleanup(errors, self.root)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
