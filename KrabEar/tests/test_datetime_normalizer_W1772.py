"""Wave 1772 — регрессионные тесты для исправления коллизии десятков в _parse_year_ru.

Подтверждённый баг (до W1772): `_parse_year_ru` использовал substring 'in'-matching
для поиска ключей _RU_YEAR_DECADES в строке `rest`.  Ключ 'десятого' (=10) является
подстрокой всех ordinal-форм десятков 50-80:

    'десятого' in 'пятидесятого'   → True  (ожидалось 50, получали 10)
    'десятого' in 'шестидесятого'  → True  (ожидалось 60, получали 10)
    'десятого' in 'семидесятого'   → True  (ожидалось 70, получали 10)
    'десятого' in 'восьмидесятого' → True  (ожидалось 80, получали 10)

Corruption:
    «тысяча девятьсот пятидесятого года»   → год = 1910  (должно быть 1950)
    «тысяча девятьсот шестидесятого года»  → год = 1910  (должно быть 1960)
    «тысяча девятьсот семидесятого года»   → год = 1910  (должно быть 1970)
    «тысяча девятьсот восьмидесятого года» → год = 1910  (должно быть 1980)
    Аналогично для 2050-2080.

Исправление (W1772): replace substring 'in' matching on `rest` with token-boundary
matching — split `rest` on whitespace, look up each whole token as exact key in
_RU_YEAR_DECADES (and _RU_YEAR_ONES_ORDINAL).  Теперь 'десятого' совпадает ТОЛЬКО
с самим собой, а 'пятидесятого' совпадает только с ключом 'пятидесятого'.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \\
        KrabEar/tests/test_datetime_normalizer_W1772.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.datetime_normalizer import DateTimeNormalizer, _RU_YEAR_DECADES


# ---------------------------------------------------------------------------
# Вспомогательная утилита: явная демонстрация коллизии на старом коде
# ---------------------------------------------------------------------------

def _old_code_decade_lookup(rest: str) -> int:
    """Эмулирует ошибочный substring-matching из кода до W1772.

    Используется в тестах-доказательствах (fail-before), чтобы показать,
    что старый алгоритм действительно давал неправильный результат.
    """
    for dk, dv in _RU_YEAR_DECADES.items():
        if dk in rest:          # <-- substring, не токен
            return dv
    return 0


# ---------------------------------------------------------------------------
# TestW1772OldCodeCollision — демонстрирует, ЧТО было сломано
# ---------------------------------------------------------------------------

class TestW1772OldCodeCollision(unittest.TestCase):
    """Демонстрация: старый код с substring 'in' давал неверный decade для 50-80.

    Эти тесты проверяют ЧТО старый алгоритм возвращал (т.е. фиксируют баг
    в виде «это было неправильно»).  Все assertion'ы показывают ОЖИДАЕМОЕ
    поведение (правильное) vs то, что возвращал старый код.
    """

    def test_old_collision_pyatidesyatogo(self):
        """Старый код: 'пятидесятого' → decade=10 (неверно, ожидается 50)."""
        # Доказываем коллизию явно через эмулятор старого кода.
        old_result = _old_code_decade_lookup("пятидесятого")
        # Старый код возвращал 10, потому что 'десятого' ∈ 'пятидесятого'.
        self.assertEqual(old_result, 10,
                         "Демонстрация: старый код davал 10 для пятидесятого (expected для bug-proof)")

    def test_old_collision_shestidesyatogo(self):
        """Старый код: 'шестидесятого' → decade=10 (неверно, ожидается 60)."""
        old_result = _old_code_decade_lookup("шестидесятого")
        self.assertEqual(old_result, 10)

    def test_old_collision_semidesyatogo(self):
        """Старый код: 'семидесятого' → decade=10 (неверно, ожидается 70)."""
        old_result = _old_code_decade_lookup("семидесятого")
        self.assertEqual(old_result, 10)

    def test_old_collision_vosmidesyatogo(self):
        """Старый код: 'восьмидесятого' → decade=10 (неверно, ожидается 80)."""
        old_result = _old_code_decade_lookup("восьмидесятого")
        self.assertEqual(old_result, 10)


# ---------------------------------------------------------------------------
# TestW1772FixedDecadesDirectParse — основные тесты исправленного _parse_year_ru
# ---------------------------------------------------------------------------

class TestW1772FixedDecadesDirectParse(unittest.TestCase):
    """_parse_year_ru теперь возвращает правильный год для 1950-1980 и 2050-2080."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _parse(self, text: str):
        return self.d._parse_year_ru(text)

    # --- 19xx ordinal forms (тысяча девятьсот ...) ---

    def test_1950_parsed_correctly(self):
        """«тысяча девятьсот пятидесятого года» → 1950 (было 1910)."""
        result = self._parse("тысяча девятьсот пятидесятого года")
        self.assertEqual(result, 1950, f"Получено: {result!r}, ожидалось 1950")

    def test_1960_parsed_correctly(self):
        """«тысяча девятьсот шестидесятого года» → 1960 (было 1910)."""
        result = self._parse("тысяча девятьсот шестидесятого года")
        self.assertEqual(result, 1960, f"Получено: {result!r}, ожидалось 1960")

    def test_1970_parsed_correctly(self):
        """«тысяча девятьсот семидесятого года» → 1970 (было 1910)."""
        result = self._parse("тысяча девятьсот семидесятого года")
        self.assertEqual(result, 1970, f"Получено: {result!r}, ожидалось 1970")

    def test_1980_parsed_correctly(self):
        """«тысяча девятьсот восьмидесятого года» → 1980 (было 1910)."""
        result = self._parse("тысяча девятьсот восьмидесятого года")
        self.assertEqual(result, 1980, f"Получено: {result!r}, ожидалось 1980")

    # --- 19xx с единицами (корректность не нарушалась, регрессионная проверка) ---

    def test_1955_parsed_correctly(self):
        """«тысяча девятьсот пятидесятого пятого года» — проверка ones+decade."""
        # После W1772 также исправлена цепочка: неверный decade больше не удаляет
        # фрагменты правильного ключа, не мешая поиску ones.
        result = self._parse("тысяча девятьсот пятидесятого пятого года")
        self.assertEqual(result, 1955, f"Получено: {result!r}, ожидалось 1955")

    def test_1963_parsed_correctly(self):
        """«тысяча девятьсот шестидесятого третьего года» → 1963."""
        result = self._parse("тысяча девятьсот шестидесятого третьего года")
        self.assertEqual(result, 1963, f"Получено: {result!r}, ожидалось 1963")

    def test_1978_parsed_correctly(self):
        """«тысяча девятьсот семидесятого восьмого года» → 1978."""
        result = self._parse("тысяча девятьсот семидесятого восьмого года")
        self.assertEqual(result, 1978, f"Получено: {result!r}, ожидалось 1978")

    # --- 20xx ordinal forms (две тысячи ...) ---

    def test_2050_parsed_correctly(self):
        """«две тысячи пятидесятого года» → 2050 (было 2010)."""
        result = self._parse("две тысячи пятидесятого года")
        self.assertEqual(result, 2050, f"Получено: {result!r}, ожидалось 2050")

    def test_2060_parsed_correctly(self):
        """«две тысячи шестидесятого года» → 2060 (было 2010)."""
        result = self._parse("две тысячи шестидесятого года")
        self.assertEqual(result, 2060, f"Получено: {result!r}, ожидалось 2060")

    def test_2070_parsed_correctly(self):
        """«две тысячи семидесятого года» → 2070 (было 2010)."""
        result = self._parse("две тысячи семидесятого года")
        self.assertEqual(result, 2070, f"Получено: {result!r}, ожидалось 2070")

    def test_2080_parsed_correctly(self):
        """«две тысячи восьмидесятого года» → 2080 (было 2010)."""
        result = self._parse("две тысячи восьмидесятого года")
        self.assertEqual(result, 2080, f"Получено: {result!r}, ожидалось 2080")

    def test_2057_parsed_correctly(self):
        """«две тысячи пятидесятого седьмого года» → 2057."""
        result = self._parse("две тысячи пятидесятого седьмого года")
        self.assertEqual(result, 2057, f"Получено: {result!r}, ожидалось 2057")


# ---------------------------------------------------------------------------
# TestW1772CurrentlyCorrectCasesUnchanged — регрессия: правильное остаётся правильным
# ---------------------------------------------------------------------------

class TestW1772CurrentlyCorrectCasesUnchanged(unittest.TestCase):
    """Ранее работавшие случаи должны продолжать работать после W1772."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _parse(self, text: str):
        return self.d._parse_year_ru(text)

    # --- Числовые годы ---

    def test_numeric_year_2026(self):
        """«2026 года» → 2026."""
        self.assertEqual(self._parse("2026 года"), 2026)

    def test_numeric_year_1990(self):
        """«1990» → 1990."""
        self.assertEqual(self._parse("1990"), 1990)

    def test_numeric_year_1955(self):
        """«1955» (явное число) → 1955."""
        self.assertEqual(self._parse("1955"), 1955)

    # --- Явно-правильные словесные годы (не были задеты коллизией) ---

    def test_2023_spoken_correct(self):
        """«две тысячи двадцать третьего года» → 2023."""
        self.assertEqual(self._parse("две тысячи двадцать третьего года"), 2023)

    def test_2026_spoken_correct(self):
        """«две тысячи двадцать шестого года» → 2026."""
        self.assertEqual(self._parse("две тысячи двадцать шестого года"), 2026)

    def test_2010_spoken_correct(self):
        """«две тысячи десятого года» → 2010."""
        self.assertEqual(self._parse("две тысячи десятого года"), 2010)

    def test_2020_spoken_correct(self):
        """«две тысячи двадцатого года» → 2020."""
        self.assertEqual(self._parse("две тысячи двадцатого года"), 2020)

    def test_2000_spoken_correct(self):
        """«две тысячи» → 2000."""
        self.assertEqual(self._parse("две тысячи"), 2000)

    def test_1990_spoken_correct(self):
        """«тысяча девятьсот девяностого года» → 1990."""
        self.assertEqual(self._parse("тысяча девятьсот девяностого года"), 1990)

    def test_1930_spoken_correct(self):
        """«тысяча девятьсот тридцатого года» → 1930."""
        self.assertEqual(self._parse("тысяча девятьсот тридцатого года"), 1930)

    def test_1910_spoken_correct(self):
        """«тысяча девятьсот десятого года» → 1910 (не должно сломаться)."""
        # 'десятого' — самостоятельный токен, должен совпадать точно.
        self.assertEqual(self._parse("тысяча девятьсот десятого года"), 1910)

    def test_unrecognized_returns_none(self):
        """Нераспознанный текст → None."""
        self.assertIsNone(self._parse("бесконечно давно"))

    def test_empty_returns_none(self):
        """Пустая строка → None."""
        self.assertIsNone(self._parse(""))


# ---------------------------------------------------------------------------
# TestW1772FullNormalizeDateIntegration — интеграционные тесты через normalize()
# ---------------------------------------------------------------------------

class TestW1772FullNormalizeDateIntegration(unittest.TestCase):
    """Полная нормализация дат с годами 1950-1980 и 2050-2080 через DateTimeNormalizer."""

    def setUp(self):
        self.d_eu = DateTimeNormalizer(output_format="european")

    def _ru(self, text: str) -> str:
        return self.d_eu.normalize(text, "ru")

    def test_1st_march_1950_eu(self):
        """«первого марта тысяча девятьсот пятидесятого года» → «01.03.1950»."""
        result = self._ru("первого марта тысяча девятьсот пятидесятого года")
        self.assertIn("01.03.1950", result, f"Получено: {result!r}")

    def test_1st_march_1960_eu(self):
        """«первого марта тысяча девятьсот шестидесятого года» → «01.03.1960»."""
        result = self._ru("первого марта тысяча девятьсот шестидесятого года")
        self.assertIn("01.03.1960", result, f"Получено: {result!r}")

    def test_1st_march_1970_eu(self):
        """«первого марта тысяча девятьсот семидесятого года» → «01.03.1970»."""
        result = self._ru("первого марта тысяча девятьсот семидесятого года")
        self.assertIn("01.03.1970", result, f"Получено: {result!r}")

    def test_1st_march_1980_eu(self):
        """«первого марта тысяча девятьсот восьмидесятого года» → «01.03.1980»."""
        result = self._ru("первого марта тысяча девятьсот восьмидесятого года")
        self.assertIn("01.03.1980", result, f"Получено: {result!r}")

    def test_15th_january_2026_still_correct(self):
        """«пятнадцатого января две тысячи двадцать шестого года» → «15.01.2026» (W1764 регрессия)."""
        result = self._ru("пятнадцатого января две тысячи двадцать шестого года")
        self.assertIn("15.01.2026", result, f"Получено: {result!r}")


if __name__ == "__main__":
    unittest.main()
