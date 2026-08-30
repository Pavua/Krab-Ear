"""Тесты гарда парных порогов (scripts/audit_paired_thresholds.py).

Гард ловит предел, выраженный в одном файле двумя способами — где-то именованной
константой, где-то литералом. 29.08.2026 ровно так упал
`test_metadata_enricher_W1765`: бюджет подняли с 0.3 с до 2.0 с в одном классе
(#1782), соседний класс того же файла остался с прежним литералом и свалился на
0.350 с и 0.463 с при загруженном раннере.

🔴 Здесь проверяется КЛАССИФИКАТОР, а не факт запуска скрипта. Гард, тихо
переставший различать плохое и хорошее, отчитывался бы CLEAN вечно — тот же
класс, что «всегда красный монитор = слепота», только наоборот.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PROJECT_ROOT)
SCRIPT = os.path.join(REPO_ROOT, "scripts", "audit_paired_thresholds.py")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import audit_paired_thresholds as guard  # noqa: E402


class ClassifierTests(unittest.TestCase):
    """Что гард обязан считать находкой, а что — нет."""

    def test_catches_named_threshold_beside_literal(self):
        """Ядро: тот же assert той же величины — константа и литерал рядом."""
        src = """
class A(unittest.TestCase):
    _BUDGET_SEC = 2.0
    def test_a(self):
        self.assertLess(elapsed, self._BUDGET_SEC)

class B(unittest.TestCase):
    def test_b(self):
        self.assertLess(elapsed, 0.3)
"""
        found = guard.scan_source(src, "<t>")
        self.assertEqual(len(found), 1, "расхождение порогов не поймано")
        self.assertEqual(found[0].measured, "elapsed")

    def test_ignores_different_measured_values(self):
        """Разные величины — не пара, даже если обе с порогами.

        Иначе гард ловит совпадение чисел, а не асимметрию.
        """
        src = """
class A(unittest.TestCase):
    BUDGET_SEC = 2.0
    def test_a(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
    def test_b(self):
        self.assertLess(payload_size, 4096)
"""
        self.assertEqual(guard.scan_source(src, "<t>"), [])

    def test_ignores_comparison_of_two_measured_values(self):
        """`assertLess(start, end)` — сравнение величин, а не порог.

        Этот шум завалил первую редакцию критерия: 21 находка на репозитории.
        """
        src = """
class A(unittest.TestCase):
    def test_a(self):
        self.assertLess(start_sec, end_sec)
    def test_b(self):
        self.assertLess(start_sec, 5.0)
"""
        self.assertEqual(guard.scan_source(src, "<t>"), [])

    def test_ignores_trivial_bounds(self):
        """`assertLess(ratio, 1)` — проверка знака/доли, а не предел."""
        src = """
class A(unittest.TestCase):
    RATIO_CAP = 0.9
    def test_a(self):
        self.assertLess(ratio, self.RATIO_CAP)
    def test_b(self):
        self.assertLess(ratio, 1)
"""
        self.assertEqual(guard.scan_source(src, "<t>"), [])

    def test_ignores_consistent_usage(self):
        """Оба места через одну константу — расхождения нет."""
        src = """
class A(unittest.TestCase):
    BUDGET_SEC = 2.0
    def test_a(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
    def test_b(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
"""
        self.assertEqual(guard.scan_source(src, "<t>"), [])

    def test_equality_asserts_are_not_thresholds(self):
        """assertEqual сравнивает с ожидаемым значением, а не с пределом."""
        src = """
class A(unittest.TestCase):
    COUNT = 3
    def test_a(self):
        self.assertEqual(items, self.COUNT)
    def test_b(self):
        self.assertEqual(items, 5)
"""
        self.assertEqual(guard.scan_source(src, "<t>"), [])

    def test_module_level_constant_counts(self):
        """Порог на уровне модуля — такой же именованный порог, как классовый."""
        src = """
REDOS_BUDGET_SEC = 5.0

class A(unittest.TestCase):
    def test_a(self):
        self.assertLess(elapsed, REDOS_BUDGET_SEC)
    def test_b(self):
        self.assertLess(elapsed, 1.5)
"""
        self.assertEqual(len(guard.scan_source(src, "<t>")), 1)


class CliTests(unittest.TestCase):
    def test_selftest_passes(self):
        """Встроенная самопроверка классификатора зелёная."""
        r = subprocess.run(
            [sys.executable, SCRIPT, "--selftest"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(r.returncode, 0, f"selftest упал: {r.stdout}{r.stderr}")

    def test_repository_is_clean(self):
        """🔴 Живой гейт: репозиторий не должен содержать расхождений.

        Если тест покраснел — не правьте порог, а сведите литерал к той же
        константе: иначе следующая правка снова оставит соседа позади.
        """
        r = subprocess.run(
            [sys.executable, SCRIPT, "--root", PROJECT_ROOT, "--fail-on-found"],
            capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(r.returncode, 0, f"найдены расхождения порогов:\n{r.stdout}")


if __name__ == "__main__":
    unittest.main()
