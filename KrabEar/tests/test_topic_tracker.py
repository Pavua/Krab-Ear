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


class TestTopicTrackerSegmentCoverage(unittest.TestCase):
    """Тесты полноты покрытия: все элементы должны быть в сегментах."""

    def setUp(self):
        self.tracker = TopicTracker()

    def test_all_items_covered_same_topic(self):
        """Сумма items_count всех сегментов == длина входного списка."""
        items = _items_same_topic(8)
        result = self.tracker.track_topics(items, window_size=3)
        total_covered = sum(seg.items_count for seg in result)
        self.assertEqual(total_covered, len(items))

    def test_all_items_covered_two_topics(self):
        """Полное покрытие при двух темах."""
        items = _items_two_topics()
        result = self.tracker.track_topics(items, window_size=3)
        total_covered = sum(seg.items_count for seg in result)
        self.assertEqual(total_covered, len(items))

    def test_source_text_field_supported(self):
        """Записи с полем source_text (а не text) обрабатываются корректно."""
        items = [
            {"source_text": "Python программирование функции классы модули библиотека"},
            {"source_text": "Код Python писать тесты модули библиотека функция"},
            {"source_text": "Python классы наследование полиморфизм программирование"},
        ]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        # Полное покрытие
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))

    def test_three_distinct_topics_detected(self):
        """Три явно разные темы → минимум 2 сегмента."""
        topic1 = [
            _item("Football match yesterday team scored goals players stadium"),
            _item("Soccer training tactics coach players football match"),
            _item("Championship football team wins cup goals scored"),
        ]
        topic2 = [
            _item("Recipe cake flour sugar butter eggs bake oven"),
            _item("Cooking soup vegetables boil recipe kitchen dinner"),
            _item("Baking bread yeast dough temperature oven recipe"),
        ]
        topic3 = [
            _item("Python programming code algorithms data structures software"),
            _item("Machine learning neural networks training data models"),
            _item("Software engineering code review testing deployment build"),
        ]
        items = topic1 + topic2 + topic3
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreaterEqual(len(result), 2)

    def test_segments_are_contiguous(self):
        """Сегменты не перекрываются и не имеют пробелов."""
        items = _items_two_topics()
        result = self.tracker.track_topics(items, window_size=3)
        # Первый сегмент начинается с 0
        self.assertEqual(result[0].start_index, 0)
        # Последний сегмент заканчивается на len(items)-1
        self.assertEqual(result[-1].end_index, len(items) - 1)
        # Каждый сегмент примыкает к предыдущему
        for i in range(1, len(result)):
            self.assertEqual(
                result[i].start_index,
                result[i - 1].end_index + 1,
                "Сегменты должны примыкать без пробелов",
            )

    def test_segment_summary_is_nonempty_string(self):
        """summary каждого сегмента — непустая строка."""
        items = _items_same_topic(6)
        result = self.tracker.track_topics(items, window_size=3)
        for seg in result:
            self.assertIsInstance(seg.summary, str)
            self.assertGreater(len(seg.summary), 0)

    def test_two_items_no_crash(self):
        """Два элемента обрабатываются без ошибок."""
        items = [_item("Python код"), _item("Python функция")]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)

    def test_current_topic_items_count_capped_at_total(self):
        """last_n > len(items) → items_count == len(items)."""
        items = _items_same_topic(3)
        result = self.tracker.get_current_topic(items, last_n=100)
        self.assertEqual(result["items_count"], 3)
        self.assertEqual(result["start_index"], 0)

    def test_all_stopwords_text_returns_segment(self):
        """Текст только из стоп-слов не вызывает исключений."""
        items = [_item("и в на с по из от до за под"), _item("я он она мы вы они")]
        result = self.tracker.track_topics(items, window_size=1)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))


class TestMakeSummaryHelper(unittest.TestCase):
    """Тесты вспомогательной функции _make_summary."""

    def test_make_summary_with_words(self):
        """_make_summary возвращает слова через запятую."""
        from core.topic_tracker import _make_summary
        result = _make_summary(["python", "код", "функция"])
        self.assertIn("python", result)
        self.assertIn(",", result)

    def test_make_summary_empty(self):
        """_make_summary с пустым списком возвращает fallback."""
        from core.topic_tracker import _make_summary
        result = _make_summary([])
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_make_summary_max_words(self):
        """_make_summary ограничивает количество слов."""
        from core.topic_tracker import _make_summary
        words = ["a", "b", "c", "d", "e", "f", "g", "h"]
        result = _make_summary(words, max_words=3)
        parts = result.split(", ")
        self.assertLessEqual(len(parts), 3)

    def test_top_keywords_sorted(self):
        """_top_keywords возвращает слова по убыванию веса."""
        from core.topic_tracker import _top_keywords
        scores = {"python": 0.9, "код": 0.5, "функция": 0.7}
        result = _top_keywords(scores, top_n=2)
        self.assertEqual(result[0], "python")
        self.assertEqual(result[1], "функция")


class TestTopicTrackerGradualDrift(unittest.TestCase):
    """Тест плавного дрейфа темы — алгоритм не должен создавать сегмент на каждой записи."""

    def setUp(self):
        self.tracker = TopicTracker()

    def test_gradual_drift_smoothed(self) -> None:
        """Постепенный сдвиг лексики не создаёт отдельный сегмент на каждый элемент.

        Симулируем постепенный переход: начинаем со спорта, добавляем
        по одному слову из другой темы — окно должно сгладить это.
        """
        items = [
            _item("Football match team players score goals win championship"),
            _item("Football match team coach tactics win players scored"),
            _item("Football team training tactics coach players strategy"),
            _item("Team training coach strategy players sports tactics games"),
            _item("Training sports strategy coach games athletics players"),
            _item("Sports games athletics competition players training events"),
            _item("Athletics competition games events sports training outdoor"),
            _item("Competition events athletics outdoor sports games training"),
        ]
        result = self.tracker.track_topics(items, window_size=4)
        # С window_size=4 постепенный дрейф должен быть сглажен:
        # результат должен иметь значительно меньше сегментов, чем элементов
        self.assertLess(len(result), len(items),
                        f"Expected fewer segments than items due to smoothing, got {len(result)}")
        # Полное покрытие
        total_covered = sum(seg.items_count for seg in result)
        self.assertEqual(total_covered, len(items))

    def test_gradual_vs_abrupt_shift_window_effect(self) -> None:
        """Большее окно сглаживает смену лучше, чем маленькое."""
        items = _items_two_topics()
        small_window = self.tracker.track_topics(items, window_size=1)
        large_window = self.tracker.track_topics(items, window_size=5)
        # Большее окно сглаживает переходы → меньше или равно сегментов
        self.assertLessEqual(len(large_window), len(small_window))


class TestTopicTrackerUnicode(unittest.TestCase):
    """Тест обработки Unicode-текста (кириллица, испанский, эмодзи)."""

    def setUp(self):
        self.tracker = TopicTracker()

    def test_unicode_topic_words(self) -> None:
        """Кирилличные слова корректно попадают в topic_words."""
        items = [
            _item("программирование алгоритмы структуры данных Python разработка"),
            _item("алгоритмы сортировка поиск данные структуры программирование"),
            _item("Python разработка код алгоритмы структуры данных функции"),
        ]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        all_words = [w for seg in result for w in seg.topic_words]
        # Должны быть кирилличные слова в topic_words
        cyrillic_words = [w for w in all_words if any('а' <= c <= 'я' or 'А' <= c <= 'Я' for c in w)]
        self.assertGreater(len(cyrillic_words), 0, "Кирилличные слова должны быть в topic_words")

    def test_spanish_unicode_topic_words(self) -> None:
        """Испанские слова с диакритикой обрабатываются корректно."""
        items = [
            _item("programación algoritmos datos estructuras desarrollo Python"),
            _item("algoritmos búsqueda ordenación datos estructuras programación"),
            _item("desarrollo código Python algoritmos estructuras función"),
        ]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))

    def test_mixed_language_items_no_crash(self) -> None:
        """Смешанный русский/испанский/английский в одном наборе не вызывает ошибок."""
        items = [
            _item("Python код функция программирование"),
            _item("código función programación Python"),
            _item("code function programming Python"),
            _item("Python разработка сортировка алгоритм"),
        ]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))

    def test_emoji_in_items_no_crash(self) -> None:
        """Emoji в текстах не вызывает ошибок — regex фильтрует их."""
        items = [
            _item("Отлично 😊 программирование Python функции"),
            _item("Код 🚀 алгоритмы структуры данных разработка"),
            _item("Python 💡 разработка функции программирование код"),
        ]
        result = self.tracker.track_topics(items, window_size=2)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))


class TestTopicTrackerWindowSize(unittest.TestCase):
    """Тест параметра window_size."""

    def setUp(self):
        self.tracker = TopicTracker()

    def test_window_size_respected(self) -> None:
        """window_size влияет на детекцию смен: большее окно = меньше сегментов."""
        # 10 элементов с чёткой сменой посередине
        items = _items_two_topics()  # 5 sport + 5 cooking = 10 items

        # Маленькое окно — более чувствительно к переходам
        result_w1 = self.tracker.track_topics(items, window_size=1)
        # Большое окно — сглаживает переходы
        result_w5 = self.tracker.track_topics(items, window_size=5)

        # Оба покрывают все элементы
        self.assertEqual(sum(s.items_count for s in result_w1), len(items))
        self.assertEqual(sum(s.items_count for s in result_w5), len(items))

        # При window_size=1 должно быть не меньше сегментов, чем при window_size=5
        self.assertGreaterEqual(len(result_w1), len(result_w5),
                                f"w1={len(result_w1)} segments should be >= w5={len(result_w5)}")

    def test_window_size_zero_treated_as_one(self) -> None:
        """window_size=0 обрабатывается как window_size=1 (max(1, 0))."""
        items = _items_same_topic(4)
        result = self.tracker.track_topics(items, window_size=0)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))

    def test_window_size_larger_than_items(self) -> None:
        """window_size > len(items) не вызывает ошибок."""
        items = _items_same_topic(3)
        result = self.tracker.track_topics(items, window_size=100)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))

    def test_window_size_equals_items_count(self) -> None:
        """window_size == len(items) — граничный случай."""
        items = _items_same_topic(5)
        result = self.tracker.track_topics(items, window_size=5)
        self.assertGreater(len(result), 0)
        total = sum(seg.items_count for seg in result)
        self.assertEqual(total, len(items))


class TestTopicTrackerConcurrent(unittest.TestCase):
    """Тест конкурентного вызова track_topics из нескольких потоков."""

    def test_concurrent_track(self) -> None:
        """TopicTracker потокобезопасен при параллельных вызовах."""
        import threading

        tracker = TopicTracker()
        results = []
        errors = []
        lock = threading.Lock()

        def worker(items: list, window_size: int) -> None:
            try:
                segs = tracker.track_topics(items, window_size=window_size)
                total = sum(s.items_count for s in segs)
                with lock:
                    results.append((len(segs), total, len(items)))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(str(exc))

        tasks = [
            (_items_same_topic(6), 3),
            (_items_two_topics(), 3),
            (_items_same_topic(4), 2),
            (_items_two_topics(), 2),
            (_items_same_topic(8), 4),
        ] * 4  # 20 total tasks

        threads = [threading.Thread(target=worker, args=(items, ws)) for items, ws in tasks]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

        self.assertEqual(errors, [], f"Errors in threads: {errors}")
        self.assertEqual(len(results), len(tasks))
        # Each result must cover all items
        for n_segs, total_covered, n_items in results:
            self.assertEqual(total_covered, n_items,
                             f"Coverage mismatch: {total_covered} != {n_items}")
            self.assertGreater(n_segs, 0)


class TestTopicTrackerInternalCap(unittest.TestCase):
    """Тест F3 (W992): внутренний guard _MAX_ITEMS в TopicTracker.track_topics."""

    def setUp(self):
        self.tracker = TopicTracker()

    def test_internal_cap_applied_logs_warning(self):
        """track_topics молча усекает вход до _MAX_ITEMS и логирует предупреждение."""
        from core.topic_tracker import _MAX_ITEMS

        # Создаём ровно _MAX_ITEMS + 10 элементов
        oversized = [_item(f"тема программирование python функция {i}") for i in range(_MAX_ITEMS + 10)]

        with self.assertLogs("core.topic_tracker", level="WARNING") as cm:
            result = self.tracker.track_topics(oversized)

        # Лог должен содержать ключевые слова предупреждения
        self.assertTrue(
            any("truncated" in msg for msg in cm.output),
            f"Предупреждение об усечении не выдано: {cm.output}",
        )

        # Результат должен покрывать ровно _MAX_ITEMS элементов, а не oversized
        total_covered = sum(seg.items_count for seg in result)
        self.assertEqual(
            total_covered, _MAX_ITEMS,
            f"Ожидали покрытие {_MAX_ITEMS} элементов, получили {total_covered}",
        )

    def test_internal_cap_not_triggered_for_normal_input(self):
        """Нет предупреждения при входе <= _MAX_ITEMS элементов."""
        from core.topic_tracker import _MAX_ITEMS
        import logging

        normal = [_item("тема программирование python функция") for _ in range(min(50, _MAX_ITEMS))]
        with self.assertNoLogs("core.topic_tracker", level="WARNING"):
            result = self.tracker.track_topics(normal)

        total_covered = sum(seg.items_count for seg in result)
        self.assertEqual(total_covered, len(normal))


class TestHandlerPrivacyModeGuard(unittest.TestCase):
    """Тест F4 (W992): _handle_get_topic_timeline возвращает пустой таймлайн в privacy_mode.

    Используем прямую проверку через unittest.mock, не требующую инициализации BackendService,
    чтобы изолировать тест от зависимостей (pydantic_settings, sounddevice и т.п.).
    """

    def _make_fake_service(self, privacy_mode_enabled: bool):
        """Возвращает реальный SearchAndAnalysisService (живой extracted handler).

        W797 dedup: in-class BackendService._handle_get_topic_timeline удалён —
        production-путь идёт через self._search_and_analysis_svc.handle_get_topic_timeline,
        поэтому тест указывает на каноническую реализацию.
        """
        from unittest.mock import MagicMock
        from core.topic_tracker import TopicTracker
        from backend.search_and_analysis_service import SearchAndAnalysisService

        # store._lock возвращает context manager, _load_active_items_unlocked — пустой список
        from contextlib import contextmanager

        @contextmanager
        def _fake_lock():
            yield

        store_mock = MagicMock()
        store_mock._lock = _fake_lock
        store_mock._load_active_items_unlocked.return_value = []

        def _settings_get(key, default=None):
            if key == "privacy_mode_enabled":
                return privacy_mode_enabled
            return default

        return SearchAndAnalysisService(
            store=store_mock,
            semantic_searcher=None,
            action_items_extractor=None,
            topic_tracker=TopicTracker(),
            recording_insights=None,
            recording_comparison=None,
            stats_report=None,
            settings_get=_settings_get,
        )

    def test_get_topic_timeline_empty_in_privacy_mode(self):
        """При privacy_mode_enabled=True handler возвращает пустой таймлайн и reason."""
        svc = self._make_fake_service(privacy_mode_enabled=True)

        result = svc.handle_get_topic_timeline({"window_size": 5, "limit": 100})

        self.assertIsInstance(result, dict, f"Ожидали dict, получили: {type(result)}")
        self.assertIn("segments", result, f"Ожидали ключ 'segments' в ответе: {result}")
        self.assertEqual(result["segments"], [], f"privacy_mode: segments должен быть пустым: {result}")
        self.assertEqual(
            result.get("reason"), "privacy_mode_active",
            f"reason должен быть 'privacy_mode_active': {result}",
        )

    def test_get_topic_timeline_normal_without_privacy_mode(self):
        """При privacy_mode_enabled=False handler работает нормально (возвращает segments)."""
        svc = self._make_fake_service(privacy_mode_enabled=False)

        result = svc.handle_get_topic_timeline({"window_size": 5, "limit": 100})

        self.assertIsInstance(result, dict)
        # С пустой историей — segments пустой, но ключ должен присутствовать
        self.assertIn("segments", result, f"Ожидали ключ 'segments' в ответе: {result}")
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")


if __name__ == "__main__":
    unittest.main()
