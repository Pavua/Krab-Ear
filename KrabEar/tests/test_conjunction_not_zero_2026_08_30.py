"""Союз «и»/«y» не должен превращаться в цифру 0 (2026-08-30).

Нашла сессия Voice Gateway на живых звонках владельца, подтверждено замером
здесь: ошибка систематическая, не редкая.

    эталон:  «до которого часа вы сегодня работаете И есть ли свободные места?»
    было:    «до которого часа вы сегодня работаете 0 есть ли свободные места?»

Испанский страдает так же: «abiertos hoy Y si tienen» → «abiertos hoy 0 si tienen».

МАСШТАБ (замер по истории владельца на 31.08.2026): **178 записей — 1.4%**.
🔴 Первый замер дал 767 (6.0%) и был НЕВЕРЕН: 595 записей из них оказались
зацикленной галлюцинацией STT («задания 0 ответы на вопросы, задания 0
ответы…» по 26 повторов), где один и тот же ноль лишь размножен петлёй.
Подозрительно массовая находка почти всегда прячет один повторяющийся
источник — считать записи, а не вхождения.

В живых звонках скрининга спама баг выглядит так:
«Звоню 0 предложите вам выгодный кредит».

ПРИЧИНА (наша, не whisper). `number_normalizer.py` добавляет союз прямо в
паттерн числительных:

    word_pat = "|".join(... for w in num_words + ["и"])

Союз там нужен по делу — составные числительные «двадцать и пять» → «25»
обязаны продолжать работать. Но паттерн матчит и группу, состоящую ИЗ ОДНОГО
союза, а парсер такой группы возвращает 0.

🔴 Поэтому фикс не в том, чтобы убрать союз из паттерна (сломает составные
числительные), а в том, чтобы группа без единого настоящего числительного не
считалась числом вовсе.
"""
from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.number_normalizer import NumberNormalizer  # noqa: E402


class ConjunctionSurvivesTests(unittest.TestCase):
    def setUp(self):
        self.n = NumberNormalizer()

    def _norm(self, text: str, lang: str) -> str:
        return self.n.normalize(text, lang)

    def test_russian_conjunction_between_words(self):
        """Живой случай из звонка владельца."""
        out = self._norm("до которого часа вы сегодня работаете и есть ли свободные места", "ru")
        self.assertIn(" и ", out, f"союз съеден: {out}")
        self.assertNotIn(" 0 ", out)

    def test_russian_conjunction_screening_call(self):
        """Реальная запись 12.07: «звоню и предложу» → «звоню 0 предложите»."""
        out = self._norm("звоню и предложу вам выгодный кредит", "ru")
        self.assertIn(" и ", out, f"союз съеден: {out}")

    def test_spanish_conjunction_between_words(self):
        out = self._norm("abiertos hoy y si tienen mesas disponibles", "es")
        self.assertIn(" y ", out, f"союз съеден: {out}")
        self.assertNotIn(" 0 ", out)

    def test_conjunction_at_sentence_start(self):
        out = self._norm("и потом я перезвоню", "ru")
        self.assertTrue(out.startswith("и "), f"союз в начале съеден: {out}")

    def test_conjunction_repeated(self):
        out = self._norm("хлеб и молоко и сыр", "ru")
        self.assertEqual(out.count(" и "), 2, f"союзы съедены: {out}")


class CompoundNumeralsStillWorkTests(unittest.TestCase):
    """🔴 Обратная сторона: союз внутри числительного обязан работать как раньше."""

    def setUp(self):
        self.n = NumberNormalizer()

    def test_russian_compound_with_conjunction(self):
        out = self.n.normalize("у меня двадцать и пять яблок", "ru")
        self.assertIn("25", out, f"составное числительное сломано: {out}")

    def test_plain_numeral_still_normalized(self):
        out = self.n.normalize("осталось пять минут", "ru")
        self.assertIn("5", out, f"обычное числительное сломано: {out}")

    def test_zero_word_still_normalized(self):
        """Слово «ноль» обязано оставаться нулём — фикс не трогает настоящий ноль."""
        out = self.n.normalize("температура ноль градусов", "ru")
        self.assertIn("0", out, f"настоящий ноль потерян: {out}")

    def test_spanish_numeral_still_normalized(self):
        out = self.n.normalize("añadir una silla para niños", "es")
        self.assertIn("1", out, f"испанское числительное сломано: {out}")


if __name__ == "__main__":
    unittest.main()
