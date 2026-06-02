"""Тесты для W1767 hardening: 3 MED-уязвимости в SharingManager.

#15 revoke_share оставляет content в индексе → очищаем чувствительные поля.
#16 Файлы пакетов и индекс создаются с 0o644/0o755 → принудительно 0o600/0o700.
#17 TOCTOU: _unique_share_id + _persist_package не атомарны → резервируем ID под локом.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sharing_manager import SharingManager  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательные заглушки
# ---------------------------------------------------------------------------

class _FakeItem:
    """Минимальная заглушка HistoryItem."""

    def __init__(self, item_id: str, text: str, translated_text: str = "") -> None:
        self.id = item_id
        self.text = text
        self.ts = "2024-01-01T10:00:00+00:00"
        self.translated_text = translated_text
        self.source_lang = "ru"
        self.target_lang = "es"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "ts": self.ts,
            "translated_text": self.translated_text,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
        }


class _FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, _FakeItem] = {}

    def add(self, item_id: str, text: str, **kw: Any) -> _FakeItem:
        item = _FakeItem(item_id, text, **kw)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> _FakeItem | None:
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# #15 — revoke_share удаляет content из индекса
# ---------------------------------------------------------------------------

class RevokeShareScrubsContentW1767TestCase(unittest.TestCase):
    """W1767 #15: revoke_share должен удалять поля транскрипции из индекса."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = _FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store, share_no_default_ttl=True)
        self._store.add("i1", "секретный текст транскрипции", translated_text="texto secreto")

    def test_revoke_removes_content_from_index(self) -> None:
        """После revoke_share в индексе не должно быть поля 'content'."""
        pkg = self._mgr.prepare_share(["i1"], format="text")
        share_id = pkg.share_id

        revoked = self._mgr.revoke_share(share_id)
        self.assertTrue(revoked, "revoke_share должен вернуть True для существующего токена")

        # Прямой доступ к индексу — content должно быть удалено
        entry = self._mgr._index.get(share_id)
        self.assertIsNotNone(entry, "tombstone-запись должна остаться в индексе")
        self.assertNotIn(
            "content", entry,
            "поле 'content' не должно присутствовать в индексе после отзыва",
        )
        # Чувствительные поля перевода тоже должны быть удалены
        self.assertNotIn("translated_text", entry, "translated_text должен быть удалён при отзыве")

    def test_revoke_tombstone_preserves_metadata(self) -> None:
        """Tombstone должен сохранять is_revoked=True и метаданные (share_id, filename)."""
        pkg = self._mgr.prepare_share(["i1"], format="markdown")
        self._mgr.revoke_share(pkg.share_id)

        entry = self._mgr._index.get(pkg.share_id)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.get("is_revoked"), "is_revoked должен быть True")
        self.assertIn("share_id", entry, "share_id метаданных должен сохраниться")
        # get_shared должен возвращать None для отозванного пакета
        self.assertIsNone(self._mgr.get_shared(pkg.share_id))

    def test_revoke_nonexistent_returns_false(self) -> None:
        """revoke_share для несуществующего ID должен вернуть False без ошибки."""
        result = self._mgr.revoke_share("NOSUCHID")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# #16 — права доступа к файлам 0o600, директория 0o700
# ---------------------------------------------------------------------------

class FileModeW1767TestCase(unittest.TestCase):
    """W1767 #16: shares/ создаётся 0o700; файлы пакетов и индекс — 0o600."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = _FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store, share_no_default_ttl=True)
        self._store.add("m1", "текст для проверки прав доступа")

    def _get_mode(self, path: Path) -> int:
        """Возвращает нижние 9 бит прав (rwxrwxrwx)."""
        return stat.S_IMODE(path.stat().st_mode)

    def test_shares_dir_is_0o700(self) -> None:
        """Директория shares/ должна иметь права 0o700."""
        shares_dir = Path(self._tmpdir) / "shares"
        self.assertTrue(shares_dir.exists(), "shares/ должна существовать")
        mode = self._get_mode(shares_dir)
        self.assertEqual(
            mode, 0o700,
            f"shares/ должна иметь права 0o700, получено {oct(mode)}",
        )

    def test_share_file_is_0o600(self) -> None:
        """Файл пакета должен быть создан с правами 0o600."""
        pkg = self._mgr.prepare_share(["m1"], format="text")
        file_path = Path(self._tmpdir) / "shares" / pkg.filename
        self.assertTrue(file_path.exists(), "файл пакета должен существовать")
        mode = self._get_mode(file_path)
        self.assertEqual(
            mode, 0o600,
            f"файл пакета должен иметь права 0o600, получено {oct(mode)}",
        )

    def test_shares_index_is_0o600(self) -> None:
        """shares_index.json должен быть создан с правами 0o600."""
        self._mgr.prepare_share(["m1"], format="text")
        index_path = Path(self._tmpdir) / "shares" / "shares_index.json"
        self.assertTrue(index_path.exists(), "shares_index.json должен существовать")
        mode = self._get_mode(index_path)
        self.assertEqual(
            mode, 0o600,
            f"shares_index.json должен иметь права 0o600, получено {oct(mode)}",
        )


# ---------------------------------------------------------------------------
# #17 — TOCTOU: параллельные prepare_share дают различные ID
# ---------------------------------------------------------------------------

class ToctouUniqueIdW1767TestCase(unittest.TestCase):
    """W1767 #17: параллельные вызовы prepare_share не должны коллизировать share_id."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = _FakeStore(data_dir=self._tmpdir)
        self._mgr = SharingManager(store=self._store, share_no_default_ttl=True)
        for i in range(20):
            self._store.add(f"p{i}", f"текст {i}")

    def test_parallel_prepare_share_distinct_ids(self) -> None:
        """20 параллельных prepare_share должны дать 20 различных share_id.

        Тест покрывает W1767 #17 (TOCTOU): без атомарной резервации ID два потока
        могут выбрать один и тот же ID, и один пакет перезапишет другой.
        """
        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(20)

        def worker(item_id: str) -> None:
            try:
                barrier.wait(timeout=5)  # Стартуем все потоки одновременно
                pkg = self._mgr.prepare_share([item_id], format="text")
                results.append(pkg.share_id)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"p{i}",), daemon=True)
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")
        self.assertEqual(len(results), 20, f"Ожидалось 20 результатов, получено {len(results)}")
        # Все ID должны быть уникальны — без коллизий
        unique_ids = set(results)
        self.assertEqual(
            len(unique_ids), 20,
            f"Дублирующиеся share_id обнаружены: {sorted(results)}",
        )

    def test_no_index_entry_after_persist_failure(self) -> None:
        """При ошибке записи файла зарезервированный ID должен быть удалён из индекса."""
        from unittest.mock import patch

        self._store.add("fail1", "текст для теста сбоя")
        initial_count = len(self._mgr._index)

        with patch("backend.sharing_manager.os.open", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                self._mgr.prepare_share(["fail1"])

        # После сбоя reserved-запись должна быть убрана
        self.assertEqual(
            len(self._mgr._index),
            initial_count,
            "зарезервированный ID должен быть удалён из индекса при ошибке записи",
        )


if __name__ == "__main__":
    unittest.main()
