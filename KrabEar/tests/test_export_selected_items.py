"""Тесты для handle_export_selected_items — экспорт ВЫБРАННЫХ записей истории.

Покрывает три случая:
1. Фильтрация по item_ids — в результат попадают только указанные записи.
2. Privacy-гейт — при privacy_mode_enabled=True возвращает ok=False.
3. Пустой item_ids — возвращает ok=False с понятной ошибкой.

Запуск:
    PYTHONPATH=KrabEar python3 -m pytest KrabEar/tests/test_export_selected_items.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Корень проекта: два уровня вверх от этого файла (tests/ → KrabEar/ → repo/)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class ExportSelectedItemsFilterTest(unittest.TestCase):
    """Фильтрация по item_ids — в результат попадают только запрошенные записи."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        # Privacy-mode выключен — используем явный флаг
        self._privacy: dict[str, bool] = {"privacy_mode_enabled": False}
        self.svc = HistoryService(
            store=self.store,
            cached_settings=lambda: dict(self._privacy),
        )
        # Засеиваем три записи с разным текстом
        item_a = self.store.add_history_item(text="запись Альфа", paste_status="ok")
        item_b = self.store.add_history_item(text="запись Бета", paste_status="ok")
        item_c = self.store.add_history_item(text="запись Гамма", paste_status="ok")
        self.id_a = item_a.id
        self.id_b = item_b.id
        self.id_c = item_c.id  # не будем запрашивать этот

    # ------------------------------------------------------------------
    # Основной сценарий: markdown — попадают только выбранные id
    # ------------------------------------------------------------------

    def test_markdown_contains_only_selected_ids(self) -> None:
        """Экспорт md: текст содержит только записи из item_ids."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.id_a, self.id_b],
            "format": "markdown",
        })
        self.assertTrue(res.get("ok"), f"Ожидали ok=True, получили: {res}")
        content = res.get("content", "")
        # Тексты выбранных записей должны присутствовать
        self.assertIn("запись Альфа", content)
        self.assertIn("запись Бета", content)
        # Текст невыбранной записи НЕ должен быть в контенте
        self.assertNotIn("запись Гамма", content)
        # Количество экспортированных записей
        self.assertEqual(res.get("entries"), 2)

    def test_srt_contains_only_selected_ids(self) -> None:
        """Экспорт srt: возвращает только выбранные записи."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.id_c],
            "format": "srt",
        })
        self.assertTrue(res.get("ok"), f"Ожидали ok=True, получили: {res}")
        content = res.get("content", "")
        self.assertIn("запись Гамма", content)
        self.assertNotIn("запись Альфа", content)
        self.assertNotIn("запись Бета", content)
        self.assertEqual(res.get("entries"), 1)

    def test_save_to_file_writes_under_data_dir(self) -> None:
        """save_to_file=True записывает файл в data_dir/transcripts/."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.id_a],
            "format": "markdown",
            "save_to_file": True,
        })
        self.assertTrue(res.get("ok"), f"Ожидали ok=True, получили: {res}")
        path_str = res.get("path")
        self.assertIsNotNone(path_str, "path должен быть возвращён при save_to_file=True")
        saved_path = Path(path_str)
        self.assertTrue(saved_path.exists(), f"Файл не создан: {saved_path}")
        # Path containment: файл должен лежать внутри data_dir
        data_dir = Path(self.store.data_dir).resolve()
        self.assertTrue(
            saved_path.resolve().is_relative_to(data_dir),
            f"Файл вне data_dir: {saved_path} не внутри {data_dir}",
        )
        content = saved_path.read_text(encoding="utf-8")
        self.assertIn("запись Альфа", content)

    def test_all_selected_ids_present_in_large_batch(self) -> None:
        """Все запрошенные IDs должны попасть в экспорт."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.id_a, self.id_b, self.id_c],
            "format": "markdown",
        })
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("entries"), 3)
        content = res.get("content", "")
        self.assertIn("запись Альфа", content)
        self.assertIn("запись Бета", content)
        self.assertIn("запись Гамма", content)

    def test_unknown_ids_are_silently_skipped(self) -> None:
        """Несуществующие ID молча пропускаются; остальные экспортируются."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.id_a, "non-existent-id-xyz"],
            "format": "markdown",
        })
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("entries"), 1)
        content = res.get("content", "")
        self.assertIn("запись Альфа", content)


class ExportSelectedItemsPrivacyGateTest(unittest.TestCase):
    """Privacy-гейт: при privacy_mode_enabled=True экспорт заблокирован."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self._privacy: dict[str, bool] = {"privacy_mode_enabled": True}
        self.svc = HistoryService(
            store=self.store,
            cached_settings=lambda: dict(self._privacy),
        )
        item = self.store.add_history_item(text="секретная запись", paste_status="ok")
        self.item_id = item.id

    def test_privacy_mode_blocks_markdown_export(self) -> None:
        """Privacy gate: markdown-экспорт возвращает ok=False и reason."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.item_id],
            "format": "markdown",
        })
        self.assertFalse(res.get("ok"), f"Ожидали ok=False, получили: {res}")
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertEqual(res.get("entries"), 0)
        # Контент должен быть пустым при заблокированном экспорте
        self.assertEqual(res.get("content", ""), "")

    def test_privacy_mode_blocks_srt_export(self) -> None:
        """Privacy gate: srt-экспорт возвращает ok=False и reason."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.item_id],
            "format": "srt",
        })
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        self.assertEqual(res.get("entries"), 0)

    def test_privacy_mode_no_file_written(self) -> None:
        """Privacy gate: save_to_file=True не создаёт файл при privacy_mode."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [self.item_id],
            "format": "markdown",
            "save_to_file": True,
        })
        self.assertFalse(res.get("ok"))
        self.assertIsNone(res.get("path"))
        # Файлы в transcripts/ не должны появиться
        transcripts_dir = Path(self.store.data_dir) / "transcripts"
        if transcripts_dir.exists():
            files = list(transcripts_dir.glob("selected_*.md"))
            self.assertEqual(files, [], f"Файлы утекли при privacy_mode: {files}")


class ExportSelectedItemsEmptyIdsTest(unittest.TestCase):
    """Пустой item_ids — возвращает ok=False с понятной ошибкой."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_empty_list_returns_error(self) -> None:
        """Пустой список item_ids возвращает ok=False."""
        res = self.svc.handle_export_selected_items({
            "item_ids": [],
            "format": "markdown",
        })
        self.assertFalse(res.get("ok"), f"Ожидали ok=False для пустого списка, получили: {res}")
        self.assertIn("item_ids", res.get("reason", ""))

    def test_missing_item_ids_key_returns_error(self) -> None:
        """Отсутствие ключа item_ids в params — возвращает ok=False."""
        res = self.svc.handle_export_selected_items({
            "format": "markdown",
        })
        self.assertFalse(res.get("ok"))
        # Причина должна указывать на отсутствующий/пустой item_ids
        self.assertIn("item_ids", res.get("reason", ""))

    def test_invalid_format_uses_markdown_fallback(self) -> None:
        """Неизвестный format — используется markdown как fallback."""
        item = self.store.add_history_item(text="запись", paste_status="ok")
        res = self.svc.handle_export_selected_items({
            "item_ids": [item.id],
            "format": "unknown_format_xyz",
        })
        self.assertTrue(res.get("ok"), f"Ожидали ok=True с fallback, получили: {res}")
        content = res.get("content", "")
        self.assertIn("запись", content)


if __name__ == "__main__":
    unittest.main()
