"""Галлюцинация «Субтитры создавал <ник>» не отсекалась: покрыт был только глагол «сделал».

Живой инцидент 2026-07-24 (голосовой смок «Разговора с AI»): на ТИШИНЕ Whisper выдавал
``'Субтитры создавал DimaTorzok'``, и этот мусор уходил в Voice Gateway как реплика
владельца — мозг отвечал на несуществующий вопрос. Та же строка повторяется в логах
07-22 и 07-23, то есть жила давно.

Причина — sibling-asymmetry внутри одного списка: паттерн ``субтитры сделал [^.!?…]{1,40}``
покрывал ровно одну глагольную форму из семейства («сделал»), а реальная модель выдаёт
«создавал». Плюс список ДУБЛИРОВАН в двух файлах (``core/utils.py::_HALLUCINATION_PATTERNS``
и ``core/hallucination_manager.py::_BUILTIN_PATTERNS_RAW``) — правка одного оставила бы
второй расходиться дальше, поэтому тест проверяет ОБА источника.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import TextUtils  # noqa: E402


class SubtitleCreditHallucinationTest(unittest.TestCase):
    """Все ходовые глагольные формы «субтитры <глагол> <ник>» должны отсекаться."""

    def test_strips_sozdaval_form_from_live_incident(self):
        """RED до фикса: покрыт был только «сделал»."""
        self.assertEqual(TextUtils._strip_hallucinations("Субтитры создавал DimaTorzok"), "")

    def test_strips_all_common_verb_forms(self):
        for phrase in (
            "Субтитры сделал DimaTorzok",
            "Субтитры создавал DimaTorzok",
            "Субтитры создал Иван Петров",
            "Субтитры делал Дмитрий Торзок",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(TextUtils._strip_hallucinations(phrase), "")

    def test_known_gap_credit_name_with_inner_period(self):
        """Осознанная граница: имя с точкой внутри («А. Семкин») НЕ отсекается.

        Класс паттернов намеренно ограничен ``[^.!?…]`` — он останавливается на знаках
        конца предложения, чтобы не съесть соседнюю реальную фразу. Расширение на точки
        сделало бы фильтр опаснее самой галлюцинации. Живая строка из логов
        (``DimaTorzok``) точек не содержит, поэтому инцидент закрыт; тест фиксирует
        границу явно, чтобы будущая «доработка» паттерна была осознанным решением,
        а не случайностью.
        """
        phrase = "Субтитры делал А. Семкин"
        self.assertEqual(TextUtils._strip_hallucinations(phrase), phrase)

    def test_keeps_real_speech_before_the_credit(self):
        """Отсекаем хвост, а не всю реплику — реальный текст владельца обязан выжить."""
        result = TextUtils._strip_hallucinations(
            "Напомни купить молоко. Субтитры создавал DimaTorzok"
        )
        self.assertEqual(result, "Напомни купить молоко")

    def test_does_not_eat_legitimate_subtitle_talk(self):
        """Не ловим осмысленную речь про субтитры — иначе фильтр сам станет багом."""
        for phrase in (
            "Включи субтитры",
            "Субтитры не работают",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(TextUtils._strip_hallucinations(phrase), phrase)


class BuiltinPatternParityTest(unittest.TestCase):
    """Список продублирован в двух файлах — они обязаны не расходиться (W1894)."""

    def test_utils_and_manager_builtin_patterns_match(self):
        from core.hallucination_manager import _BUILTIN_PATTERNS_RAW
        from core.utils import _HALLUCINATION_PATTERNS

        utils_patterns = {p.pattern for p in _HALLUCINATION_PATTERNS}
        manager_patterns = {raw for raw, _category in _BUILTIN_PATTERNS_RAW}

        self.assertEqual(
            utils_patterns,
            manager_patterns,
            "Встроенные паттерны галлюцинаций разошлись между core/utils.py и "
            "core/hallucination_manager.py — правка одного файла не покрывает второй",
        )


if __name__ == "__main__":
    unittest.main()
