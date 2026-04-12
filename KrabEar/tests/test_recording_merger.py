"""Unit-тесты для RecordingMerger."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_merger import RecordingMerger


# ---------------------------------------------------------------------------
# Вспомогательные фейки
# ---------------------------------------------------------------------------


@dataclass
class FakeHistoryItem:
    id: str
    ts: str
    text: str
    paste_status: str = "success"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    audio_duration_sec: float | None = None
    confidence: float | None = None
    diarization: dict | None = None
    tags: list = field(default_factory=list)
    favorite: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


class FakeStore:
    """Минимальный фейк StateStore для тестов RecordingMerger."""

    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}
        self._deleted: list[str] = []
        self._added: list[FakeHistoryItem] = []

    def add_fake_item(
        self,
        item_id: str,
        text: str,
        ts: str = "2026-04-12T10:00:00",
        **kwargs: Any,
    ) -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, ts=ts, text=text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.append(item_id)
            return True
        return False

    def add_history_item(
        self,
        text: str,
        paste_status: str = "merged",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        diarization: dict | None = None,
        audio_duration_sec: float | None = None,
        confidence: float | None = None,
        tags: list | None = None,
        **kwargs: Any,
    ) -> FakeHistoryItem:
        import uuid
        item = FakeHistoryItem(
            id=str(uuid.uuid4()),
            ts="2026-04-12T12:00:00",
            text=text,
            paste_status=paste_status,
            source_text=source_text,
            translated_text=translated_text,
            translation_mode=translation_mode,
            source_lang=source_lang,
            target_lang=target_lang,
            translation_status=translation_status,
            diarization=diarization,
            audio_duration_sec=audio_duration_sec,
            confidence=confidence,
            tags=list(tags) if tags else [],
        )
        self._items[item.id] = item
        self._added.append(item)
        return item


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


class TestMergeItemsBasic(unittest.TestCase):
    """Базовые тесты объединения записей."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 1. Базовое объединение двух записей
    def test_merge_two_items_returns_new_item(self) -> None:
        self._add("a1", "Привет мир", ts="2026-04-12T09:00:00")
        self._add("a2", "Как дела", ts="2026-04-12T09:05:00")
        result = self.merger.merge_items(["a1", "a2"], self.store)
        self.assertIn("text", result)
        self.assertIn("Привет мир", result["text"])
        self.assertIn("Как дела", result["text"])

    # 2. merged_from содержит оба ID
    def test_merge_returns_merged_from_list(self) -> None:
        self._add("b1", "Alpha", ts="2026-04-12T09:00:00")
        self._add("b2", "Beta", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(["b1", "b2"], self.store)
        self.assertIn("merged_from", result)
        self.assertCountEqual(result["merged_from"], ["b1", "b2"])

    # 3. Сумма длительностей
    def test_merge_sums_audio_duration(self) -> None:
        self._add("c1", "A", ts="2026-04-12T09:00:00", audio_duration_sec=10.0)
        self._add("c2", "B", ts="2026-04-12T09:01:00", audio_duration_sec=20.5)
        result = self.merger.merge_items(["c1", "c2"], self.store)
        self.assertAlmostEqual(result["audio_duration_sec"], 30.5, places=2)

    # 4. Среднее значение уверенности
    def test_merge_averages_confidence(self) -> None:
        self._add("d1", "X", ts="2026-04-12T09:00:00", confidence=0.8)
        self._add("d2", "Y", ts="2026-04-12T09:01:00", confidence=0.6)
        result = self.merger.merge_items(["d1", "d2"], self.store)
        self.assertAlmostEqual(result["confidence"], 0.7, places=4)

    # 5. Теги объединяются, дубликаты убираются
    def test_merge_combines_tags_deduplicates(self) -> None:
        self._add("e1", "A", ts="2026-04-12T09:00:00", tags=["work", "important"])
        self._add("e2", "B", ts="2026-04-12T09:01:00", tags=["important", "personal"])
        result = self.merger.merge_items(["e1", "e2"], self.store)
        merged_tags = result["tags"]
        self.assertIn("work", merged_tags)
        self.assertIn("important", merged_tags)
        self.assertIn("personal", merged_tags)
        self.assertEqual(len(merged_tags), 3)

    # 6. С delete_originals=True исходные записи удаляются
    def test_merge_deletes_originals_when_requested(self) -> None:
        self._add("f1", "First", ts="2026-04-12T09:00:00")
        self._add("f2", "Second", ts="2026-04-12T09:01:00")
        self.merger.merge_items(["f1", "f2"], self.store, delete_originals=True)
        self.assertIn("f1", self.store._deleted)
        self.assertIn("f2", self.store._deleted)

    # 7. Без delete_originals оригиналы не удаляются
    def test_merge_keeps_originals_by_default(self) -> None:
        self._add("g1", "One", ts="2026-04-12T09:00:00")
        self._add("g2", "Two", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(["g1", "g2"], self.store)
        self.assertFalse(result["deleted_originals"])
        self.assertEqual(len(self.store._deleted), 0)

    # 8. Менее двух ID вызывает ValueError
    def test_merge_requires_at_least_two_items(self) -> None:
        self._add("h1", "Solo")
        with self.assertRaises(ValueError):
            self.merger.merge_items(["h1"], self.store)

    # 9. Несуществующий ID вызывает ValueError
    def test_merge_raises_on_missing_id(self) -> None:
        self._add("i1", "Exists")
        with self.assertRaises(ValueError) as ctx:
            self.merger.merge_items(["i1", "nonexistent"], self.store)
        self.assertIn("nonexistent", str(ctx.exception))

    # 10. Хронологическая сортировка текста
    def test_merge_orders_text_chronologically(self) -> None:
        # Передаём в обратном порядке — ожидаем сортировку по ts
        self._add("j1", "Первый", ts="2026-04-12T09:00:00")
        self._add("j2", "Второй", ts="2026-04-12T09:10:00")
        result = self.merger.merge_items(["j2", "j1"], self.store)
        idx_first = result["text"].index("Первый")
        idx_second = result["text"].index("Второй")
        self.assertLess(idx_first, idx_second)


class TestPreviewMerge(unittest.TestCase):
    """Тесты предварительного просмотра объединения."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 11. preview_merge возвращает preview=True
    def test_preview_returns_preview_flag(self) -> None:
        self._add("p1", "Alpha", ts="2026-04-12T10:00:00")
        self._add("p2", "Beta", ts="2026-04-12T10:01:00")
        result = self.merger.preview_merge(["p1", "p2"], self.store)
        self.assertTrue(result["preview"])

    # 12. preview_merge не создаёт запись в store
    def test_preview_does_not_save_to_store(self) -> None:
        self._add("q1", "Alpha", ts="2026-04-12T10:00:00")
        self._add("q2", "Beta", ts="2026-04-12T10:01:00")
        self.merger.preview_merge(["q1", "q2"], self.store)
        self.assertEqual(len(self.store._added), 0)

    # 13. preview_merge содержит merged_from и item_count
    def test_preview_has_merged_from_and_count(self) -> None:
        self._add("r1", "A", ts="2026-04-12T10:00:00")
        self._add("r2", "B", ts="2026-04-12T10:01:00")
        self._add("r3", "C", ts="2026-04-12T10:02:00")
        result = self.merger.preview_merge(["r1", "r2", "r3"], self.store)
        self.assertEqual(result["item_count"], 3)
        self.assertCountEqual(result["merged_from"], ["r1", "r2", "r3"])

    # 14. IPC handle_preview_merge через params
    def test_handle_preview_merge_via_params(self) -> None:
        self._add("s1", "Hello", ts="2026-04-12T10:00:00")
        self._add("s2", "World", ts="2026-04-12T10:01:00")
        result = self.merger.handle_preview_merge(
            {"item_ids": ["s1", "s2"]}, self.store
        )
        self.assertTrue(result["preview"])
        self.assertIn("text", result)


class TestMergeDiarization(unittest.TestCase):
    """Тесты объединения дiarization."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 15. Сегменты дiarization объединяются
    def test_merge_combines_speaker_segments(self) -> None:
        d1 = {"speaker_segments": [{"speaker": "A", "start": 0.0, "end": 5.0}]}
        d2 = {"speaker_segments": [{"speaker": "B", "start": 0.0, "end": 3.0}]}
        self._add("t1", "Speaker A talking", ts="2026-04-12T10:00:00", diarization=d1)
        self._add("t2", "Speaker B talking", ts="2026-04-12T10:01:00", diarization=d2)
        result = self.merger.merge_items(["t1", "t2"], self.store)
        merged_diag = result.get("diarization")
        self.assertIsNotNone(merged_diag)
        segments = merged_diag.get("speaker_segments", [])
        self.assertEqual(len(segments), 2)
        self.assertTrue(merged_diag.get("merged"))

    # 16. Нет diarization → None в результате
    def test_merge_no_diarization_returns_none(self) -> None:
        self._add("u1", "No diag 1", ts="2026-04-12T10:00:00")
        self._add("u2", "No diag 2", ts="2026-04-12T10:01:00")
        result = self.merger.merge_items(["u1", "u2"], self.store)
        self.assertIsNone(result["diarization"])


class TestMergeIPCHandlers(unittest.TestCase):
    """Тесты IPC-обёрток handle_merge_recordings / handle_preview_merge."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 17. handle_merge_recordings сохраняет запись
    def test_handle_merge_recordings_saves_item(self) -> None:
        self._add("v1", "Текст один", ts="2026-04-12T10:00:00")
        self._add("v2", "Текст два", ts="2026-04-12T10:01:00")
        result = self.merger.handle_merge_recordings(
            {"item_ids": ["v1", "v2"]}, self.store
        )
        self.assertEqual(len(self.store._added), 1)
        self.assertIn("id", result)

    # 18. handle_merge_recordings с delete_originals=True удаляет оригиналы
    def test_handle_merge_recordings_delete_flag(self) -> None:
        self._add("w1", "AAA", ts="2026-04-12T10:00:00")
        self._add("w2", "BBB", ts="2026-04-12T10:01:00")
        self.merger.handle_merge_recordings(
            {"item_ids": ["w1", "w2"], "delete_originals": True}, self.store
        )
        self.assertIn("w1", self.store._deleted)
        self.assertIn("w2", self.store._deleted)

    # 19. Пустой item_ids вызывает ValueError
    def test_handle_merge_recordings_empty_ids_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.merger.handle_merge_recordings({"item_ids": []}, self.store)

    # 20. item_ids не список → ValueError
    def test_handle_merge_recordings_non_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.merger.handle_merge_recordings({"item_ids": "not_a_list"}, self.store)


class TestMergeTranslation(unittest.TestCase):
    """Тесты объединения полей перевода."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 21. translated_text объединяется
    def test_merge_combines_translated_text(self) -> None:
        self._add("x1", "Hola", ts="2026-04-12T10:00:00", translated_text="Привет", translation_mode="es_ru")
        self._add("x2", "Mundo", ts="2026-04-12T10:01:00", translated_text="Мир", translation_mode="es_ru")
        result = self.merger.merge_items(["x1", "x2"], self.store)
        self.assertIn("Привет", result["translated_text"])
        self.assertIn("Мир", result["translated_text"])

    # 22. source_lang/target_lang берётся у первой непустой записи
    def test_merge_picks_language_from_first_non_empty(self) -> None:
        self._add("y1", "A", ts="2026-04-12T10:00:00")  # без языков
        self._add("y2", "B", ts="2026-04-12T10:01:00", source_lang="es", target_lang="ru")
        result = self.merger.merge_items(["y1", "y2"], self.store)
        self.assertEqual(result["source_lang"], "es")
        self.assertEqual(result["target_lang"], "ru")


if __name__ == "__main__":
    unittest.main()
