"""Тесты SpeakerStatisticsAnalyzer — per-speaker статистика диаризации Krab Ear."""

from __future__ import annotations
from backend.speaker_statistics import SpeakerStatisticsAnalyzer

import sys
import unittest
from pathlib import Path

# Настройка путей для standalone-запуска
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_diar_item(turns: list[dict], confidence: float | None = None, source_lang: str = "") -> dict:
    """Создаёт fake-словарь истории с данными диаризации."""
    return {
        "diarization": {
            "enabled": True,
            "speaker_turns": turns,
        },
        "confidence": confidence,
        "source_lang": source_lang,
    }


def _turn(speaker: str, start: float, end: float, text: str = "hello world") -> dict:
    """Создаёт один turn диаризации."""
    return {"speaker": speaker, "start": start, "end": end, "text": text}


class FakeSpeakerManager:
    """Минимальная заглушка SpeakerManager для тестов."""

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}

    def get_alias(self, speaker_id: str) -> str | None:
        return self._aliases.get(speaker_id)


# ---------------------------------------------------------------------------
# Тест 1: пустой список → нет спикеров
# ---------------------------------------------------------------------------

class TestEmptyItems(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_empty_returns_no_speakers(self):
        """Пустой список → speakers пустой, total_speakers=0."""
        result = self.analyzer.analyze_speakers([])
        self.assertEqual(result["total_speakers"], 0)
        self.assertEqual(result["speakers"], {})
        self.assertIsNone(result["most_active_speaker"])

    def test_empty_balance_is_1(self):
        """Пустой список → balance=1.0 (нет данных = баланс по умолчанию)."""
        result = self.analyzer.analyze_speakers([])
        self.assertEqual(result["speaker_balance"], 1.0)


# ---------------------------------------------------------------------------
# Тест 2: элементы без диаризации игнорируются
# ---------------------------------------------------------------------------

class TestItemsWithoutDiarization(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_items_without_diarization_ignored(self):
        """Записи без diarization не попадают в статистику."""
        items = [
            {"text": "hello", "confidence": 0.9, "source_lang": "ru"},
            {"diarization": None, "confidence": 0.8, "source_lang": "en"},
            {"diarization": {"enabled": False, "speaker_turns": []}, "confidence": 0.7},
        ]
        result = self.analyzer.analyze_speakers(items)
        self.assertEqual(result["total_speakers"], 0)

    def test_disabled_diarization_ignored(self):
        """Записи с enabled=False игнорируются."""
        item = {
            "diarization": {
                "enabled": False,
                "speaker_turns": [_turn("SPEAKER_00", 0, 10, "test")],
            },
            "confidence": 0.95,
        }
        result = self.analyzer.analyze_speakers([item])
        self.assertEqual(result["total_speakers"], 0)


# ---------------------------------------------------------------------------
# Тест 3: один спикер — базовые поля
# ---------------------------------------------------------------------------

class TestSingleSpeaker(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_single_speaker_basic_fields(self):
        """Один спикер → корректно заполняются все базовые поля."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 60.0, "один два три четыре пять"),
                _turn("SPEAKER_00", 65.0, 125.0, "шесть семь восемь девять десять"),
            ],
            confidence=0.9,
            source_lang="ru",
        )
        result = self.analyzer.analyze_speakers([item])

        self.assertEqual(result["total_speakers"], 1)
        self.assertIn("SPEAKER_00", result["speakers"])
        s = result["speakers"]["SPEAKER_00"]
        # Два turn → 2 appearances
        self.assertEqual(s["appearances"], 2)
        # Суммарное время = 60 + 60 = 120 сек
        self.assertAlmostEqual(s["total_speaking_time_sec"], 120.0, places=2)
        # Слова: 5 + 5 = 10
        self.assertEqual(s["total_words"], 10)
        # WPM: 10 / 120 * 60 = 5.0
        self.assertAlmostEqual(s["avg_words_per_minute"], 5.0, places=1)
        # Confidence = 0.9
        self.assertAlmostEqual(s["avg_confidence"], 0.9, places=4)
        # Язык: ru — 2 turn'а
        self.assertEqual(s["languages"].get("ru"), 2)
        # Самый активный
        self.assertEqual(result["most_active_speaker"], "SPEAKER_00")

    def test_single_speaker_balance_is_zero(self):
        """Один спикер → balance=0.0 (нет равенства)."""
        item = _make_diar_item(turns=[_turn("SPEAKER_00", 0, 60)])
        result = self.analyzer.analyze_speakers([item])
        self.assertEqual(result["speaker_balance"], 0.0)


# ---------------------------------------------------------------------------
# Тест 4: два спикера, баланс и most_active
# ---------------------------------------------------------------------------

class TestTwoSpeakers(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_most_active_speaker(self):
        """SPEAKER_00 говорит дольше → он most_active."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 100.0, "long speech"),
                _turn("SPEAKER_01", 100.0, 110.0, "short"),
            ]
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertEqual(result["most_active_speaker"], "SPEAKER_00")

    def test_two_equal_speakers_balance_near_1(self):
        """Два спикера с равным временем → balance близок к 1.0."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 60.0),
                _turn("SPEAKER_01", 60.0, 120.0),
            ]
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertAlmostEqual(result["speaker_balance"], 1.0, places=3)

    def test_two_unequal_speakers_balance_less_than_1(self):
        """Два спикера с разным временем → balance < 1.0."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 90.0),
                _turn("SPEAKER_01", 90.0, 100.0),
            ]
        )
        result = self.analyzer.analyze_speakers([item])
        bal = result["speaker_balance"]
        self.assertGreater(bal, 0.0)
        self.assertLess(bal, 1.0)


# ---------------------------------------------------------------------------
# Тест 5: longest_turn и avg_turn
# ---------------------------------------------------------------------------

class TestTurnDurations(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_longest_and_avg_turn(self):
        """longest_turn и avg_turn вычисляются корректно."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 10.0),   # 10 сек
                _turn("SPEAKER_00", 10.0, 40.0),  # 30 сек
                _turn("SPEAKER_00", 40.0, 60.0),  # 20 сек
            ]
        )
        result = self.analyzer.analyze_speakers([item])
        s = result["speakers"]["SPEAKER_00"]
        self.assertAlmostEqual(s["longest_turn_sec"], 30.0, places=2)
        self.assertAlmostEqual(s["avg_turn_sec"], 20.0, places=2)


# ---------------------------------------------------------------------------
# Тест 6: псевдонимы через SpeakerManager
# ---------------------------------------------------------------------------

class TestSpeakerManagerAlias(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_alias_from_speaker_manager(self):
        """Псевдоним из SpeakerManager попадает в поле alias."""
        mgr = FakeSpeakerManager({"SPEAKER_00": "Паша", "SPEAKER_01": "Маша"})
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 30.0),
                _turn("SPEAKER_01", 30.0, 60.0),
            ]
        )
        result = self.analyzer.analyze_speakers([item], speaker_manager=mgr)
        self.assertEqual(result["speakers"]["SPEAKER_00"]["alias"], "Паша")
        self.assertEqual(result["speakers"]["SPEAKER_01"]["alias"], "Маша")

    def test_no_alias_when_manager_is_none(self):
        """Без speaker_manager alias=None для всех спикеров."""
        item = _make_diar_item(turns=[_turn("SPEAKER_00", 0.0, 30.0)])
        result = self.analyzer.analyze_speakers([item])
        self.assertIsNone(result["speakers"]["SPEAKER_00"]["alias"])

    def test_unknown_speaker_alias_is_none(self):
        """Спикер без псевдонима → alias=None даже при переданном manager."""
        mgr = FakeSpeakerManager({})  # нет псевдонимов
        item = _make_diar_item(turns=[_turn("SPEAKER_00", 0.0, 30.0)])
        result = self.analyzer.analyze_speakers([item], speaker_manager=mgr)
        self.assertIsNone(result["speakers"]["SPEAKER_00"]["alias"])


# ---------------------------------------------------------------------------
# Тест 7: несколько записей — агрегирование по спикерам
# ---------------------------------------------------------------------------

class TestMultipleItems(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_aggregates_across_items(self):
        """Статистика суммируется по нескольким записям истории."""
        items = [
            _make_diar_item([_turn("SPEAKER_00", 0, 30)], confidence=0.8, source_lang="ru"),
            _make_diar_item([_turn("SPEAKER_00", 0, 60)], confidence=0.9, source_lang="ru"),
            _make_diar_item([_turn("SPEAKER_01", 0, 20)], confidence=0.7, source_lang="es"),
        ]
        result = self.analyzer.analyze_speakers(items)
        # SPEAKER_00: суммарное время = 30 + 60 = 90 сек
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["total_speaking_time_sec"], 90.0, places=2
        )
        # avg_confidence для SPEAKER_00 = (0.8 + 0.9) / 2 = 0.85
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["avg_confidence"], 0.85, places=4
        )
        # SPEAKER_01: 20 сек
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_01"]["total_speaking_time_sec"], 20.0, places=2
        )

    def test_language_counts_per_speaker(self):
        """Язык source_lang накапливается per-speaker per-turn."""
        items = [
            _make_diar_item(
                [_turn("SPEAKER_00", 0, 10), _turn("SPEAKER_00", 10, 20)],
                source_lang="ru",
            ),
            _make_diar_item(
                [_turn("SPEAKER_00", 0, 10)],
                source_lang="es",
            ),
        ]
        result = self.analyzer.analyze_speakers(items)
        langs = result["speakers"]["SPEAKER_00"]["languages"]
        # Из первой записи — 2 turn'а на "ru", из второй — 1 turn на "es"
        self.assertEqual(langs.get("ru"), 2)
        self.assertEqual(langs.get("es"), 1)


# ---------------------------------------------------------------------------
# Тест 8: IPC handle_get_speaker_statistics с fake store
# ---------------------------------------------------------------------------

class FakeStore:
    """Минимальная заглушка StateStore для IPC-тестов."""

    class _FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def __init__(self, items: list) -> None:
        self._items = items

    def _lock(self):
        return self._FakeLock()

    def _load_active_items_unlocked(self) -> list:
        return list(self._items)


class TestIPCHandler(unittest.TestCase):
    def setUp(self):
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_handle_with_empty_store(self):
        """IPC handler с пустым store → корректный пустой ответ."""
        store = FakeStore([])
        result = self.analyzer.handle_get_speaker_statistics({}, store=store)
        self.assertEqual(result["total_speakers"], 0)
        self.assertEqual(result["speakers"], {})

    def test_handle_with_items(self):
        """IPC handler агрегирует данные из store."""
        item = _make_diar_item(
            turns=[_turn("SPEAKER_00", 0.0, 45.0, "тест тест тест")],
            confidence=0.88,
        )
        store = FakeStore([item])
        result = self.analyzer.handle_get_speaker_statistics({}, store=store)
        self.assertEqual(result["total_speakers"], 1)
        self.assertIn("SPEAKER_00", result["speakers"])
        s = result["speakers"]["SPEAKER_00"]
        self.assertAlmostEqual(s["total_speaking_time_sec"], 45.0, places=2)
        self.assertAlmostEqual(s["avg_confidence"], 0.88, places=4)

    def test_handle_with_speaker_manager(self):
        """IPC handler передаёт speaker_manager для разрешения псевдонимов."""
        item = _make_diar_item(turns=[_turn("SPEAKER_00", 0.0, 10.0)])
        store = FakeStore([item])
        mgr = FakeSpeakerManager({"SPEAKER_00": "Тест"})
        result = self.analyzer.handle_get_speaker_statistics({}, store=store, speaker_manager=mgr)
        self.assertEqual(result["speakers"]["SPEAKER_00"]["alias"], "Тест")

    def test_handle_store_exception_returns_empty(self):
        """Если store бросает исключение, возвращается пустой результат."""

        class BrokenStore:
            def _lock(self):
                raise RuntimeError("disk error")

        result = self.analyzer.handle_get_speaker_statistics({}, store=BrokenStore())
        self.assertEqual(result["total_speakers"], 0)


# ---------------------------------------------------------------------------
# Тест 9: явная проверка имён полей и базовых сценариев из задания
# ---------------------------------------------------------------------------

class TestFieldNamesAndScenarios(unittest.TestCase):
    """Явная проверка имён полей result dict и сценариев без диаризации."""

    def setUp(self) -> None:
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_no_diarization_returns_empty_dict(self) -> None:
        """Записи без поля diarization → speakers пустой dict, total_speakers=0."""
        items = [
            {"text": "hello world", "confidence": 0.9, "source_lang": "en"},
            {"text": "another item", "confidence": 0.8},
        ]
        result = self.analyzer.analyze_speakers(items)
        self.assertEqual(result["speakers"], {})
        self.assertEqual(result["total_speakers"], 0)
        self.assertIsNone(result["most_active_speaker"])

    def test_single_speaker_result_field_names(self) -> None:
        """Один спикер → все ожидаемые поля присутствуют с правильными именами."""
        item = _make_diar_item(
            turns=[_turn("SPEAKER_00", 0.0, 30.0, "слово раз два")],
            confidence=0.91,
            source_lang="ru",
        )
        result = self.analyzer.analyze_speakers([item])

        self.assertIn("SPEAKER_00", result["speakers"])
        s = result["speakers"]["SPEAKER_00"]

        # Проверяем ключевые имена полей из задания
        self.assertIn("total_words", s)
        self.assertIn("total_speaking_time_sec", s)
        self.assertIn("avg_confidence", s)

        # Значения корректны
        self.assertEqual(s["total_words"], 3)
        self.assertAlmostEqual(s["total_speaking_time_sec"], 30.0, places=2)
        self.assertAlmostEqual(s["avg_confidence"], 0.91, places=4)

    def test_multi_speaker_all_represented(self) -> None:
        """Два разных спикера → оба присутствуют в result['speakers']."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_A", 0.0, 40.0, "alpha beta gamma"),
                _turn("SPEAKER_B", 40.0, 60.0, "delta"),
            ],
            confidence=0.85,
        )
        result = self.analyzer.analyze_speakers([item])

        self.assertEqual(result["total_speakers"], 2)
        self.assertIn("SPEAKER_A", result["speakers"])
        self.assertIn("SPEAKER_B", result["speakers"])

    def test_multi_speaker_word_count_per_speaker(self) -> None:
        """word_count распределяется по спикерам из текста turn."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 30.0, "один два три"),      # 3 слова
                _turn("SPEAKER_01", 30.0, 60.0, "four five six seven"),  # 4 слова
            ],
        )
        result = self.analyzer.analyze_speakers([item])

        self.assertEqual(result["speakers"]["SPEAKER_00"]["total_words"], 3)
        self.assertEqual(result["speakers"]["SPEAKER_01"]["total_words"], 4)

    def test_multi_speaker_duration_per_speaker(self) -> None:
        """duration распределяется по спикерам из временных меток turn."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 50.0),   # 50 сек
                _turn("SPEAKER_01", 50.0, 65.0),  # 15 сек
            ],
        )
        result = self.analyzer.analyze_speakers([item])

        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["total_speaking_time_sec"], 50.0, places=2
        )
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_01"]["total_speaking_time_sec"], 15.0, places=2
        )

    def test_confidence_avg_per_speaker_from_item_level(self) -> None:
        """avg_confidence берётся из поля confidence записи (item-level), не turn."""
        items = [
            _make_diar_item([_turn("SPEAKER_00", 0, 10)], confidence=0.80),
            _make_diar_item([_turn("SPEAKER_00", 0, 10)], confidence=0.90),
        ]
        result = self.analyzer.analyze_speakers(items)

        # avg = (0.80 + 0.90) / 2 = 0.85
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["avg_confidence"], 0.85, places=4
        )

    def test_no_confidence_data_avg_is_none(self) -> None:
        """Если confidence не задан → avg_confidence is None."""
        item = {
            "diarization": {
                "enabled": True,
                "speaker_turns": [_turn("SPEAKER_00", 0.0, 10.0)],
            }
        }
        result = self.analyzer.analyze_speakers([item])
        self.assertIsNone(result["speakers"]["SPEAKER_00"]["avg_confidence"])


class TestRequiredScenarios(unittest.TestCase):
    """Явные тесты с именами из задания Wave 99."""

    def setUp(self) -> None:
        self.analyzer = SpeakerStatisticsAnalyzer()

    def test_compute_word_count_per_speaker(self) -> None:
        """Количество слов распределяется по спикерам из текста turn."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 30.0, "один два три"),       # 3 слова
                _turn("SPEAKER_01", 30.0, 60.0, "four five six seven"),  # 4 слова
            ],
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertEqual(result["speakers"]["SPEAKER_00"]["total_words"], 3)
        self.assertEqual(result["speakers"]["SPEAKER_01"]["total_words"], 4)

    def test_compute_total_duration_per_speaker(self) -> None:
        """Суммарная длительность вычисляется по временным меткам turn."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 0.0, 50.0),   # 50 сек
                _turn("SPEAKER_01", 50.0, 65.0),  # 15 сек
            ],
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["total_speaking_time_sec"], 50.0, places=2
        )
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_01"]["total_speaking_time_sec"], 15.0, places=2
        )

    def test_average_confidence_per_speaker(self) -> None:
        """avg_confidence усредняется по всем записям спикера."""
        items = [
            _make_diar_item([_turn("SPEAKER_00", 0, 10)], confidence=0.80),
            _make_diar_item([_turn("SPEAKER_00", 0, 10)], confidence=0.90),
        ]
        result = self.analyzer.analyze_speakers(items)
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["avg_confidence"], 0.85, places=4
        )

    def test_no_diarization_returns_empty(self) -> None:
        """Записи без диаризации → speakers пустой, total_speakers=0."""
        items = [
            {"text": "hello world", "confidence": 0.9, "source_lang": "en"},
        ]
        result = self.analyzer.analyze_speakers(items)
        self.assertEqual(result["speakers"], {})
        self.assertEqual(result["total_speakers"], 0)

    def test_unicode_speaker_name(self) -> None:
        """Имя спикера в Кириллице обрабатывается корректно."""
        item = _make_diar_item(
            turns=[
                _turn("ГОВОРЯЩИЙ_01", 0.0, 30.0, "тест"),
                _turn("Спикер-Два", 30.0, 60.0, "ещё тест"),
            ],
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertIn("ГОВОРЯЩИЙ_01", result["speakers"])
        self.assertIn("Спикер-Два", result["speakers"])
        self.assertEqual(result["total_speakers"], 2)

    def test_handles_single_speaker_history(self) -> None:
        """Один спикер — все поля заполнены корректно, balance=0."""
        item = _make_diar_item(
            turns=[_turn("SPEAKER_00", 0.0, 60.0, "один два три")],
            confidence=0.9,
        )
        result = self.analyzer.analyze_speakers([item])
        self.assertEqual(result["total_speakers"], 1)
        s = result["speakers"]["SPEAKER_00"]
        self.assertIn("total_words", s)
        self.assertIn("total_speaking_time_sec", s)
        self.assertEqual(result["speaker_balance"], 0.0)

    def test_skip_speaker_with_zero_segments(self) -> None:
        """Если все turns у спикера имеют нулевую длительность — спикер всё равно учитывается (words > 0)."""
        item = _make_diar_item(
            turns=[
                _turn("SPEAKER_00", 5.0, 5.0, "слово"),  # duration=0, но words=1
            ],
        )
        result = self.analyzer.analyze_speakers([item])
        # Спикер создаётся (turn существует), но speaking_time = 0
        self.assertIn("SPEAKER_00", result["speakers"])
        self.assertAlmostEqual(
            result["speakers"]["SPEAKER_00"]["total_speaking_time_sec"], 0.0, places=3
        )
        self.assertEqual(result["speakers"]["SPEAKER_00"]["total_words"], 1)


if __name__ == "__main__":
    unittest.main()
