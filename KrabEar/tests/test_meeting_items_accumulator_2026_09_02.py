"""Окно транскрипта + накопление пунктов встречи (2026-09-02).

ЧТО БЫЛО СЛОМАНО
----------------
`_job_items_llm` отправлял в LLM-экстрактор ВЕСЬ транскрипт с начала встречи:

    full_text = "".join(s.chunks)
    result = self._extractor.extract(full_text, ...)

Обрезки нет ни здесь, ни в самом экстракторе (в модуле ноль упоминаний
truncate). На многочасовой встрече payload перерастает контекст локальной
модели, `extract()` начинает возвращать `ok=False`, а курсор
`last_extract_len` двигается ТОЛЬКО в ветке `if result.ok`. Значит пункты
перестают обновляться до конца встречи — фича ломалась ровно на том
сценарии, ради которого делалась.

🔴 Ресурсный шторм при этом гасил уже существующий `CircuitBreaker` внутри
экстрактора (`allow_request`/`record_failure`, экспоненциальный откат до
600с) — второй предохранитель строить не нужно было. Здесь чинится потеря
обновлений, а не расход ресурсов.

ПОЧЕМУ ОКНА МАЛО БЕЗ НАКОПИТЕЛЯ
-------------------------------
Раньше списки перезаписывались целиком (`s.items = [...]`). С переходом на
окно пункты, названные в начале встречи, исчезали бы при каждом сдвиге —
окно починило бы одно и сломало другое. Поэтому окно и накопитель
обязаны идти вместе.
"""
from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.meeting_session_service import (  # noqa: E402
    _ITEMS_WINDOW_CHARS,
    _merge_items,
    _merge_texts,
    _norm_key,
)


class WindowConstantTests(unittest.TestCase):
    def test_window_is_bounded_and_sane(self) -> None:
        """Окно должно быть конечным и заметно меньше многочасового транскрипта."""
        self.assertIsInstance(_ITEMS_WINDOW_CHARS, int)
        self.assertGreater(_ITEMS_WINDOW_CHARS, 1000, "слишком узкое окно потеряет контекст")
        self.assertLess(_ITEMS_WINDOW_CHARS, 100_000, "окно должно ограничивать payload")


class MergeTextsTests(unittest.TestCase):
    def test_new_items_appended_old_kept(self) -> None:
        """Главное свойство накопителя: ранние пункты переживают сдвиг окна."""
        got = _merge_texts(["решение A"], ["решение B"])
        self.assertEqual(got, ["решение A", "решение B"])

    def test_duplicates_are_not_repeated(self) -> None:
        """LLM повторяет тот же пункт на соседних окнах — дублей быть не должно."""
        got = _merge_texts(["решение A"], ["решение A", "решение B"])
        self.assertEqual(got, ["решение A", "решение B"])

    def test_dedup_ignores_case_and_spacing(self) -> None:
        """🔴 Без нормализации список рос бы дублями: формулировка модели
        колеблется регистром и пробелами между окнами."""
        got = _merge_texts(["Решение  A"], ["решение a", "новое"])
        self.assertEqual(got, ["Решение  A", "новое"])

    def test_empty_and_none_are_skipped(self) -> None:
        self.assertEqual(_merge_texts(["A"], ["", "   ", None]), ["A"])
        self.assertEqual(_merge_texts(["A"], None), ["A"])

    def test_order_of_appearance_preserved(self) -> None:
        """Порядок появления — единственный осмысленный здесь: он отражает ход встречи."""
        got = _merge_texts([], ["первое", "второе", "третье"])
        self.assertEqual(got, ["первое", "второе", "третье"])


class MergeItemsTests(unittest.TestCase):
    def test_new_task_appended(self) -> None:
        got = _merge_items([{"text": "задача A"}], [{"text": "задача B"}])
        self.assertEqual([d["text"] for d in got], ["задача A", "задача B"])

    def test_same_task_refined_replaces_not_duplicates(self) -> None:
        """🔴 Уточнение исполнителя/срока на следующем окне ЗАМЕЩАЕТ запись.

        Иначе одна задача расплодилась бы в несколько почти одинаковых.
        """
        got = _merge_items(
            [{"text": "созвон с клиентом", "assignee": "", "due": ""}],
            [{"text": "созвон с клиентом", "assignee": "Паша", "due": "пятница"}],
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["assignee"], "Паша")
        self.assertEqual(got[0]["due"], "пятница")

    def test_items_without_text_are_skipped(self) -> None:
        """Пустой ключ не должен схлопывать разные задачи в одну."""
        got = _merge_items([{"text": "A"}], [{"text": ""}, {"assignee": "кто-то"}])
        self.assertEqual([d["text"] for d in got], ["A"])

    def test_existing_items_survive_empty_fresh(self) -> None:
        """Неудачное окно не стирает накопленное."""
        got = _merge_items([{"text": "A"}], [])
        self.assertEqual([d["text"] for d in got], ["A"])


class NormKeyTests(unittest.TestCase):
    def test_normalisation_collapses_whitespace_and_case(self) -> None:
        self.assertEqual(_norm_key("  Привет   Мир "), _norm_key("привет мир"))

    def test_none_and_empty_give_empty_key(self) -> None:
        self.assertEqual(_norm_key(None), "")
        self.assertEqual(_norm_key("   "), "")


if __name__ == "__main__":
    unittest.main()
