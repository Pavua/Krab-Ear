"""Unit-тесты для RecordingMerger."""

from __future__ import annotations
from backend.recording_merger import RecordingMerger

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Настройка пути для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class TestMergeEdgeCases(unittest.TestCase):
    """Edge case тесты для RecordingMerger."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 23. Слияние с пропуском в событиях (временной разрыв)
    def test_merge_with_time_gap(self) -> None:
        self._add("gap1", "Morning note", ts="2026-04-12T09:00:00")
        self._add("gap2", "Evening note", ts="2026-04-12T18:00:00")
        result = self.merger.merge_items(["gap1", "gap2"], self.store)
        self.assertIn("Morning note", result["text"])
        self.assertIn("Evening note", result["text"])
        self.assertIn("09:00", result["text"])
        self.assertIn("18:00", result["text"])

    # 24. Слияние 3+ элементов
    def test_merge_multiple_items_three_plus(self) -> None:
        self._add("multi1", "First", ts="2026-04-12T10:00:00")
        self._add("multi2", "Second", ts="2026-04-12T10:01:00")
        self._add("multi3", "Third", ts="2026-04-12T10:02:00")
        self._add("multi4", "Fourth", ts="2026-04-12T10:03:00")
        result = self.merger.merge_items(["multi1", "multi2", "multi3", "multi4"], self.store)
        for text in ["First", "Second", "Third", "Fourth"]:
            self.assertIn(text, result["text"])

    # 25. Уверенность с None значениями
    def test_merge_confidence_with_none_values(self) -> None:
        self._add("conf1", "Has confidence", ts="2026-04-12T10:00:00", confidence=0.9)
        self._add("conf2", "No confidence", ts="2026-04-12T10:01:00", confidence=None)
        result = self.merger.merge_items(["conf1", "conf2"], self.store)
        self.assertAlmostEqual(result["confidence"], 0.9, places=4)

    # 26. Длительность только у одного элемента
    def test_merge_duration_single_item_has_duration(self) -> None:
        self._add("dur1", "Has duration", ts="2026-04-12T10:00:00", audio_duration_sec=30.0)
        self._add("dur2", "No duration", ts="2026-04-12T10:01:00", audio_duration_sec=None)
        result = self.merger.merge_items(["dur1", "dur2"], self.store)
        self.assertAlmostEqual(result["audio_duration_sec"], 30.0, places=2)

    # 27. Пустые теги (пропускаются)
    def test_merge_tags_empty_strings_skipped(self) -> None:
        self._add("tag1", "A", ts="2026-04-12T10:00:00", tags=["valid", ""])
        self._add("tag2", "B", ts="2026-04-12T10:01:00", tags=["other", "  "])
        result = self.merger.merge_items(["tag1", "tag2"], self.store)
        merged_tags = result["tags"]
        self.assertIn("valid", merged_tags)
        self.assertIn("other", merged_tags)
        self.assertNotIn("", merged_tags)

    # 28. Пользовательский разделитель текста
    def test_merge_custom_separator(self) -> None:
        self._add("sep1", "Part one", ts="2026-04-12T10:00:00")
        self._add("sep2", "Part two", ts="2026-04-12T10:01:00")
        result = self.merger.merge_items(
            ["sep1", "sep2"],
            self.store,
            separator=" ||| ",
        )
        self.assertIn(" ||| ", result["text"])

    # 29. Одиночный элемент выбрасывает ValueError
    def test_merge_single_item_error(self) -> None:
        self._add("single", "Only one")
        with self.assertRaises(ValueError) as ctx:
            self.merger.merge_items(["single"], self.store)
        self.assertIn("минимум 2", str(ctx.exception))

    # 30. IPC без item_ids параметра
    def test_handle_merge_recordings_missing_item_ids_raises(self) -> None:
        with self.assertRaises((ValueError, KeyError)):
            self.merger.handle_merge_recordings({}, self.store)

    # 31. merge_items с пустым списком (прямой вызов)
    def test_merge_items_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.merger.merge_items([], self.store)
        self.assertIn("минимум 2", str(ctx.exception))

    # 32. preview_merge с одним элементом тоже выбрасывает ValueError
    def test_preview_merge_single_item_raises(self) -> None:
        self._add("solo", "Единственный")
        with self.assertRaises(ValueError):
            self.merger.preview_merge(["solo"], self.store)

    # 33. preview_merge с пустым списком выбрасывает ValueError
    def test_preview_merge_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.merger.preview_merge([], self.store)


class TestMergeDiarizationExtended(unittest.TestCase):
    """Расширенные тесты для _merge_diarization."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 34. Только один из двух имеет diarization: merged — False, возврат первого
    def test_merge_diarization_one_none_one_valid(self) -> None:
        d2 = {"speaker_segments": [{"speaker": "X", "start": 0.0, "end": 2.0}]}
        self._add("md1", "No diag", ts="2026-04-12T10:00:00")
        self._add("md2", "Has diag", ts="2026-04-12T10:01:00", diarization=d2)
        result = self.merger.merge_items(["md1", "md2"], self.store)
        merged_diag = result.get("diarization")
        self.assertIsNotNone(merged_diag)
        # должен вернуть объединённую структуру с merged=True (1 сегмент)
        self.assertTrue(merged_diag.get("merged"))
        self.assertEqual(len(merged_diag["speaker_segments"]), 1)

    # 35. diarization без ключа speaker_segments (используется segments)
    def test_merge_diarization_uses_segments_fallback_key(self) -> None:
        d1 = {"segments": [{"speaker": "A", "start": 0.0, "end": 1.0}]}
        d2 = {"segments": [{"speaker": "B", "start": 0.0, "end": 1.0}]}
        self._add("seg1", "Text A", ts="2026-04-12T10:00:00", diarization=d1)
        self._add("seg2", "Text B", ts="2026-04-12T10:01:00", diarization=d2)
        result = self.merger.merge_items(["seg1", "seg2"], self.store)
        merged_diag = result.get("diarization")
        self.assertIsNotNone(merged_diag)
        self.assertEqual(len(merged_diag["speaker_segments"]), 2)


class TestMergeMetadataEdgeCases(unittest.TestCase):
    """Edge cases для метаданных объединённой записи."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    # 36. Все элементы без confidence → confidence=None в результате
    def test_merge_all_none_confidence_returns_none(self) -> None:
        self._add("nc1", "A", ts="2026-04-12T10:00:00", confidence=None)
        self._add("nc2", "B", ts="2026-04-12T10:01:00", confidence=None)
        result = self.merger.merge_items(["nc1", "nc2"], self.store)
        self.assertIsNone(result["confidence"])

    # 37. Все элементы без duration → audio_duration_sec=None в результате
    def test_merge_all_none_duration_returns_none(self) -> None:
        self._add("nd1", "A", ts="2026-04-12T10:00:00", audio_duration_sec=None)
        self._add("nd2", "B", ts="2026-04-12T10:01:00", audio_duration_sec=None)
        result = self.merger.merge_items(["nd1", "nd2"], self.store)
        self.assertIsNone(result["audio_duration_sec"])

    # 38. Нет переведённых текстов → translated_text пустая строка
    def test_merge_no_translated_text_is_empty(self) -> None:
        self._add("nt1", "Hello", ts="2026-04-12T10:00:00")
        self._add("nt2", "World", ts="2026-04-12T10:01:00")
        result = self.merger.merge_items(["nt1", "nt2"], self.store)
        self.assertEqual(result["translated_text"], "")

    # 39. translation_mode='off' у всех → 'off' в результате
    def test_merge_translation_mode_off_when_all_off(self) -> None:
        self._add("tm1", "A", ts="2026-04-12T10:00:00", translation_mode="off")
        self._add("tm2", "B", ts="2026-04-12T10:01:00", translation_mode="off")
        result = self.merger.merge_items(["tm1", "tm2"], self.store)
        self.assertEqual(result["translation_mode"], "off")

    # 40. Результат содержит поле deleted_originals=False по умолчанию
    def test_merge_result_has_deleted_originals_false(self) -> None:
        self._add("del1", "First", ts="2026-04-12T10:00:00")
        self._add("del2", "Second", ts="2026-04-12T10:01:00")
        result = self.merger.merge_items(["del1", "del2"], self.store)
        self.assertIn("deleted_originals", result)
        self.assertFalse(result["deleted_originals"])


class TestMergerRequiredNames(unittest.TestCase):
    """Тесты с именами, заданными в Wave 139 task spec."""

    def setUp(self) -> None:
        self.store = FakeStore()
        self.merger = RecordingMerger()

    def _add(self, item_id: str, text: str, ts: str = "2026-04-12T10:00:00", **kw: Any) -> FakeHistoryItem:
        return self.store.add_fake_item(item_id, text, ts=ts, **kw)

    def test_merge_two_items_concatenates(self) -> None:
        """Объединение двух записей конкатенирует тексты."""
        self._add("cat1", "First fragment", ts="2026-04-12T09:00:00")
        self._add("cat2", "Second fragment", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(["cat1", "cat2"], self.store)
        self.assertIn("First fragment", result["text"])
        self.assertIn("Second fragment", result["text"])

    def test_metadata_preserved(self) -> None:
        """Метаданные (source_lang, target_lang, tags) сохраняются в результате."""
        self._add(
            "mp1", "Hola mundo",
            ts="2026-04-12T09:00:00",
            source_lang="es",
            target_lang="ru",
            tags=["call", "important"],
        )
        self._add("mp2", "Buenos días", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(["mp1", "mp2"], self.store)
        self.assertEqual(result["source_lang"], "es")
        self.assertEqual(result["target_lang"], "ru")
        self.assertIn("call", result["tags"])
        self.assertIn("important", result["tags"])

    def test_duration_summed(self) -> None:
        """Длительности суммируются в итоговой записи."""
        self._add("ds1", "A", ts="2026-04-12T09:00:00", audio_duration_sec=12.3)
        self._add("ds2", "B", ts="2026-04-12T09:01:00", audio_duration_sec=7.7)
        result = self.merger.merge_items(["ds1", "ds2"], self.store)
        self.assertAlmostEqual(result["audio_duration_sec"], 20.0, places=2)

    def test_original_items_marked_merged(self) -> None:
        """При delete_originals=True оба оригинальных ID отмечаются как удалённые."""
        self._add("om1", "Original one", ts="2026-04-12T09:00:00")
        self._add("om2", "Original two", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(
            ["om1", "om2"], self.store, delete_originals=True
        )
        self.assertTrue(result["deleted_originals"])
        self.assertIn("om1", self.store._deleted)
        self.assertIn("om2", self.store._deleted)
        self.assertCountEqual(result["merged_from"], ["om1", "om2"])

    def test_unicode_preserved(self) -> None:
        """Кириллица и спецсимволы сохраняются в объединённом тексте."""
        self._add("up1", "Привет мир — первая запись!", ts="2026-04-12T09:00:00")
        self._add("up2", "Hasta la vista — вторая запись!", ts="2026-04-12T09:01:00")
        result = self.merger.merge_items(["up1", "up2"], self.store)
        self.assertIn("Привет мир", result["text"])
        self.assertIn("Hasta la vista", result["text"])
        self.assertIn("вторая запись", result["text"])

    def test_handles_single_item_no_merge(self) -> None:
        """Один элемент — merge_items выбрасывает ValueError (нельзя слить одно)."""
        self._add("si1", "Solo item")
        with self.assertRaises(ValueError):
            self.merger.merge_items(["si1"], self.store)

    def test_concurrent_merge_safe(self) -> None:
        """Параллельные вызовы merge_items с разными парами ID не конфликтуют."""
        import threading

        pairs = [
            (f"th{idx}a", f"th{idx}b", f"Pair {idx} alpha", f"Pair {idx} beta")
            for idx in range(6)
        ]
        for idx, (a_id, b_id, a_text, b_text) in enumerate(pairs):
            self._add(a_id, a_text, ts=f"2026-04-12T0{idx}:00:00")
            self._add(b_id, b_text, ts=f"2026-04-12T0{idx}:01:00")

        errors: list[Exception] = []
        results: list[dict] = []
        lock = threading.Lock()

        def run(a_id: str, b_id: str) -> None:
            try:
                r = self.merger.merge_items([a_id, b_id], self.store)
                with lock:
                    results.append(r)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=run, args=(a_id, b_id))
            for a_id, b_id, *_ in pairs
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], msg=str(errors))
        self.assertEqual(len(results), 6)
        for r in results:
            self.assertIn("text", r)
            self.assertIn("merged_from", r)


if __name__ == "__main__":
    unittest.main()
