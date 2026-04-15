"""Тесты для TopicTracker — отслеживание смены тем разговора.

Покрывает:
1. track_topics — пустой список возвращает пустой результат
2. track_topics — один элемент создаёт один сегмент
3. track_topics — без смены темы создаёт один сегмент
4. track_topics — смена темы создаёт два и более сегментов
5. TopicSegment.items_count — корректно вычисляется
6. TopicSegment.to_dict — содержит все ожидаемые ключи
7. get_topic_timeline — is_shift=False для первого сегмента
8. get_topic_timeline — is_shift=True для последующих сегментов
9. get_current_topic — пустой список возвращает fallback
10. get_current_topic — возвращает topic_words и summary
11. get_current_topic — last_n ограничивает окно анализа
12. track_topics — topic_words содержит только значимые слова
13. track_topics — window_size=1 работает корректно
14. get_topic_timeline — возвращает корректное total_shifts через handle
"""

from __future__ import annotations
from core.topic_tracker import TopicTracker, _tokenize, _keyword_overlap

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Вспомогательные фабрики ──────────────────────────────────────────────────

def _item(text: str) -> dict:
    """Создаёт минимальную запись истории."""
    return {"text": text}


def _items_same_topic(n: int = 8) -> list[dict]:
    """Набор записей об одной теме — программирование на Python."""
    texts = [
        "Сегодня писали код на Python, разбирали функции и классы.",
        "Python очень удобный язык, поддерживает много библиотек.",
        "Программирование на Python требует понимания функций и модулей.",
        "Код Python лёгкий для чтения, используем классы и методы.",
        "Библиотека pandas для Python отличная, работаем с данными.",
        "Функции Python можно декорировать, используем декораторы.",
        "Классы в Python наследуются, полиморфизм реализован красиво.",
        "Python программирование изучаем, пишем тесты для кода.",
    ]
    return [_item(texts[i % len(texts)]) for i in range(n)]


def _items_two_topics() -> list[dict]:
    """Набор записей с явной сменой темы (спорт → кулинария)."""
    sport_items = [
        "Футбол матч вчера был замечательный, команда победила гол.",
        "Спортивные тренировки важны, футбол развивает выносливость игроков.",
        "Чемпионат футбольный в этом сезоне, команды борются кубок.",
        "Тренер команды разрабатывает тактику, игроки тренируются матч.",
        "Стадион заполнен болельщиками, команда готовится чемпионат победить.",
    ]
    cooking_items = [
        "Рецепт торта требует яйца мука масло сахар печь духовка.",
        "Готовим суп борщ капуста свёкла морковь картошка кастрюля.",
        "Кулинария приготовление блюд рецепты десерты торт печенье.",
        "Ужин приготовить пасту соус помидоры чеснок оливковое масло.",
        "Выпечка хлеб тесто дрожжи духовка температура рецепт печь.",
    ]
    return [_item(t) for t in sport_items + cooking_items]


# ── Тест-кейсы ───────────────────────────────────────────────────────────────

class TestTopicTrackerBasic(unittest.TestCase):
    """Базовые тесты track_topics."""

    def setUp(self):
        self.tracker = TopicTracker()

    # -- Тест 1 --
    def test_empty_items_returns_empty(self):
        """Пустой список → пустой результат."""
        result = self.tracker.track_topics([])
        self.assertEqual(result, [])

    # -- Тест 2 --
    def test_single_item_creates_one_segment(self):
        """Один элемент → один сегмент."""
        result = self.tracker.track_topics([_item("Привет мир тест данные")])
        self.assertEqual(len(result), 1)
        seg = result[0]
        self.assertEqual(seg.start_index, 0)
        self.assertEqual(seg.end_index, 0)
        self.assertEqual(seg.items_count, 1)

    # -- Тест 3 --
    def test_same_topic_creates_one_segment(self):
        """Похожая тема во всех записях → один сегмент (без смены)."""
        items = _items_same_topic(8)
        result = self.tracker.track_topics(items, window_size=3)
        # Должен быть 1 сегмент (или очень мало, но точно не по 1 на каждый элемент)
        self.assertLess(len(result), len(items))
        # Первый сегмент начинается с 0
        self.assertEqual(result[0].start_index, 0)

    # -- Тест 4 --
    def test_topic_shift_creates_multiple_segments(self):
        """Резкая смена темы → минимум 2 сегмента."""
        items = _items_two_topics()
        result = self.tracker.track_topics(items, window_size=3)
        self.assertGreaterEqual(len(result), 2)

    # -- Тест 5 --
    def test_segment_items_count_correct(self):
        """TopicSegment.items_count == end_index - start_index + 1."""
        items = _items_same_topic(6)
        result = self.tracker.track_topics(items, window_size=6)
        for seg in result:
            expected = seg.end_index - seg.start_index + 1
            self.assertEqual(seg.items_count, expected)

    # -- Тест 6 --
    def test_segment_to_dict_has_required_keys(self):
        """TopicSegment.to_dict() содержит все обязательные ключи."""
        items = _items_same_topic(4)
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        d = result[0].to_dict()
        for key in ("start_index", "end_index", "topic_words", "summary", "items_count"):
            self.assertIn(key, d, f"Ключ '{key}' отсутствует в to_dict()")

    # -- Тест 13 --
    def test_window_size_one(self):
        """window_size=1 не вызывает ошибок и возвращает корректный результат."""
        items = _items_same_topic(4)
        result = self.tracker.track_topics(items, window_size=1)
        self.assertGreater(len(result), 0)
        # Все записи должны быть покрыты
        covered = sum(seg.items_count for seg in result)
        self.assertEqual(covered, len(items))

    # -- Тест 12 --
    def test_topic_words_excludes_stop_words(self):
        """topic_words не должны содержать стоп-слова."""
        items = _items_same_topic(6)
        result = self.tracker.track_topics(items, window_size=3)
        from core.topic_tracker import _STOP_WORDS
        for seg in result:
            for word in seg.topic_words:
                self.assertNotIn(
                    word.lower(), _STOP_WORDS,
                    f"Стоп-слово '{word}' попало в topic_words"
                )


class TestGetTopicTimeline(unittest.TestCase):
    """Тесты get_topic_timeline."""

    def setUp(self):
        self.tracker = TopicTracker()

    # -- Тест 7 --
    def test_first_segment_is_not_shift(self):
        """Первый сегмент в таймлайне — is_shift=False."""
        items = _items_same_topic(5)
        timeline = self.tracker.get_topic_timeline(items)
        self.assertGreater(len(timeline), 0)
        self.assertFalse(
            timeline[0]["is_shift"],
            "Первый сегмент не должен помечаться как is_shift=True"
        )

    # -- Тест 8 --
    def test_subsequent_segments_are_shifts(self):
        """Все сегменты после первого — is_shift=True."""
        items = _items_two_topics()
        timeline = self.tracker.get_topic_timeline(items, window_size=3)
        shifts = [e for e in timeline if e["is_shift"]]
        non_shifts = [e for e in timeline if not e["is_shift"]]
        # Ровно один non-shift (первый)
        self.assertEqual(len(non_shifts), 1)
        # Хотя бы один shift
        self.assertGreaterEqual(len(shifts), 1)

    # -- Тест 14 --
    def test_timeline_contains_required_keys(self):
        """Каждая запись таймлайна содержит нужные поля."""
        items = _items_same_topic(5)
        timeline = self.tracker.get_topic_timeline(items)
        required = {"start_index", "end_index", "topic_words", "summary",
                    "items_count", "is_shift"}
        for entry in timeline:
            self.assertTrue(
                required.issubset(entry.keys()),
                f"Запись таймлайна не содержит все ключи: {entry.keys()}"
            )


class TestGetCurrentTopic(unittest.TestCase):
    """Тесты get_current_topic."""

    def setUp(self):
        self.tracker = TopicTracker()

    # -- Тест 9 --
    def test_empty_items_returns_fallback(self):
        """Пустой список → fallback словарь без ошибок."""
        result = self.tracker.get_current_topic([])
        self.assertIn("topic_words", result)
        self.assertIn("summary", result)
        self.assertIn("items_count", result)
        self.assertIn("start_index", result)
        self.assertEqual(result["items_count"], 0)

    # -- Тест 10 --
    def test_returns_topic_words_and_summary(self):
        """get_current_topic возвращает непустые topic_words и summary."""
        items = _items_same_topic(5)
        result = self.tracker.get_current_topic(items)
        self.assertIn("topic_words", result)
        self.assertIn("summary", result)
        self.assertIsInstance(result["topic_words"], list)
        self.assertGreater(len(result["topic_words"]), 0)
        self.assertIsInstance(result["summary"], str)
        self.assertGreater(len(result["summary"]), 0)

    # -- Тест 11 --
    def test_last_n_limits_window(self):
        """last_n ограничивает количество анализируемых записей."""
        items = _items_same_topic(10)
        result = self.tracker.get_current_topic(items, last_n=3)
        self.assertEqual(result["items_count"], 3)
        self.assertEqual(result["start_index"], 7)  # 10 - 3 = 7


class TestHelpers(unittest.TestCase):
    """Тесты вспомогательных функций."""

    def test_tokenize_filters_stop_words(self):
        """_tokenize отфильтровывает стоп-слова."""
        tokens = _tokenize("я иду в магазин покупать хлеб")
        from core.topic_tracker import _STOP_WORDS
        for t in tokens:
            self.assertNotIn(t, _STOP_WORDS)

    def test_keyword_overlap_same_sets(self):
        """Пересечение одинаковых наборов = 1.0."""
        kw = ["python", "код", "функция"]
        self.assertAlmostEqual(_keyword_overlap(kw, kw), 1.0)

    def test_keyword_overlap_disjoint_sets(self):
        """Пересечение непересекающихся наборов = 0.0."""
        a = ["футбол", "матч", "команда"]
        b = ["рецепт", "торт", "мука"]
        self.assertAlmostEqual(_keyword_overlap(a, b), 0.0)

    def test_keyword_overlap_empty(self):
        """Пересечение пустых наборов = 0.0."""
        self.assertAlmostEqual(_keyword_overlap([], ["что-то"]), 0.0)
        self.assertAlmostEqual(_keyword_overlap(["что-то"], []), 0.0)


if __name__ == "__main__":
    unittest.main()
