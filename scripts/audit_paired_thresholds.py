#!/usr/bin/env python3
"""Гард парных порогов: один и тот же предел, выраженный по-разному в соседних местах.

ЗАЧЕМ
-----
За 29 августа 2026 один и тот же класс ошибки сработал ТРИЖДЫ, и каждый раз
чинился вручную по красному CI:

  1. ``mlx_lock``: сиблинг ``MLXWatchdog.run_with_timeout`` держал замок до
     смерти рабочего потока (W1358), а ``stt_gigaam_mlx._run_with_timeout``
     написал свой вариант мимо готового паттерна — и отпускал замок под живым
     потоком (#1958);
  2. ``_TIMING_BUDGET_SEC = 2.0`` жил в одном классе с комментарием «поднят
     ради загруженных CI-раннеров», а соседний класс того же файла держал
     захардкоженные ``0.3`` — и падал на 0.350 с и 0.463 с;
  3. пороги ReDoS-защит разъехались по репозиторию от 0.05 с до 5.0 с, хотя
     защищают все от одного и того же (#1963).

Общий признак: **правку применили к одному месту, соседнее осталось**. Пока
расхождение не видно инструменту, его находит красный CI — то есть дороже
всего и в самый неудобный момент.

КРИТЕРИЙ (главное решение этого гарда)
---------------------------------------
Находкой считается пара, где В ОДНОМ ФАЙЛЕ **одна и та же проверка одной и той
же измеряемой величины** выражена и через ИМЕНОВАННЫЙ порог, и нетривиальным
литералом.

Совпадать обязаны все три вещи: имя assert-метода, имя измеряемой переменной и
файл. Иначе гард ловит не асимметрию, а совпадение чисел.

ПОЧЕМУ НЕ ПРОЩЕ
----------------
Два более широких критерия отвергнуты замером на этом репозитории:

  * «в файле есть константа-порог и где-то рядом литерал» → **44 файла**, почти
    всё ложь: ``_POLL_BUDGET_SEC = 90`` (бюджет опроса) и ``urlopen(timeout=30)``
    (таймаут HTTP) — разные величины, а не расхождение;
  * «та же проверка той же величины, порог = любое имя» → **21 находка**, шум от
    сравнения двух измеренных величин (``assertGreater(end_sec, start_sec)``) и
    от проверок знака (``assertGreater(ratio, 0.0)``).

Отсюда два сужения, каждое режет свой класс шума:
  * порог обязан быть UPPER_SNAKE-именем (``BUDGET``/``_TIMEOUT_SEC``/…), а не
    произвольной переменной — так отсеиваются сравнения величин между собой;
  * литерал не может быть тривиальным (0, ±1) — это проверка знака или наличия,
    а не предел.

На вычищенном репозитории критерий даёт НОЛЬ находок, а на восстановленном
дефекте из пункта 2 — ровно одну, без шума (проверено мутацией, см. --selftest).

ГРАНИЦЫ
--------
Гард ловит ЧИСЛОВУЮ асимметрию порогов. Пункт 1 из списка выше (расхождение
поведения при таймауте) он НЕ ловит и не претендует: там расходятся не числа, а
алгоритмы, и надёжного автоматического критерия для этого пока нет. Честнее
покрыть один класс точно, чем три приблизительно.
"""
from __future__ import annotations

import argparse
import ast
import collections
import pathlib
import re
import sys

# Порог — UPPER_SNAKE, в том числе с ведущим подчёркиванием и как self._X.
_THRESHOLD_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")

# Границы, не являющиеся порогом: проверка знака / наличия.
_TRIVIAL_BOUNDS = frozenset({0, 0.0, 1, 1.0, -1, -1.0})

# Только односторонние сравнения с пределом. assertEqual/assertAlmostEqual сюда
# не входят: там число — ожидаемое значение, а не граница.
_BOUND_ASSERTS = frozenset({"assertLess", "assertLessEqual"})

_SKIP_PARTS = ("/.venv", "site-packages", "_legacy_", "/node_modules/")


class Finding:
    __slots__ = ("path", "assert_name", "measured", "named", "literals")

    def __init__(self, path, assert_name, measured, named, literals):
        self.path = path
        self.assert_name = assert_name
        self.measured = measured
        self.named = named
        self.literals = literals

    def render(self) -> str:
        named = ", ".join(f"{n}:{ln}" for ln, n in self.named)
        lits = ", ".join(f"{v}:{ln}" for ln, v in self.literals)
        return (
            f"{self.path}\n"
            f"    {self.assert_name}({self.measured}, …) — один предел выражен двумя способами\n"
            f"    именованный порог: {named}\n"
            f"    литерал:           {lits}\n"
            f"    → сведите литерал к тому же имени: правка одного места оставит второе позади"
        )


def _measured_name(call: ast.Call):
    """Что именно меряет проверка: assertLess(elapsed, X) → 'elapsed'."""
    first = call.args[0]
    return getattr(first, "id", None) or getattr(first, "attr", None)


def _threshold_name(node):
    """Имя, похожее на именованный порог, иначе None."""
    if isinstance(node, ast.Name) and _THRESHOLD_NAME.match(node.id):
        return node.id
    if isinstance(node, ast.Attribute) and _THRESHOLD_NAME.match(node.attr):
        return node.attr
    return None


def scan_source(source: str, path: str) -> list:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    buckets = collections.defaultdict(lambda: {"named": [], "literals": []})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None) or ""
        if name not in _BOUND_ASSERTS:
            continue
        measured = _measured_name(node)
        if measured is None:
            continue

        bound = node.args[1]
        key = (name, measured)
        threshold = _threshold_name(bound)
        if threshold is not None:
            buckets[key]["named"].append((node.lineno, threshold))
        elif isinstance(bound, ast.Constant) and isinstance(bound.value, (int, float)):
            if bound.value not in _TRIVIAL_BOUNDS:
                buckets[key]["literals"].append((node.lineno, bound.value))

    out = []
    for (name, measured), got in sorted(buckets.items()):
        if got["named"] and got["literals"]:
            out.append(Finding(path, name, measured, sorted(got["named"]), sorted(got["literals"])))
    return out


def scan_tree(root: pathlib.Path) -> list:
    findings = []
    for path in sorted(root.rglob("*.py")):
        text = str(path)
        if any(part in text for part in _SKIP_PARTS):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_source(source, text))
    return findings


_BAD_SAMPLE = '''
class Timing(unittest.TestCase):
    _TIMING_BUDGET_SEC = 2.0

    def test_a(self):
        self.assertLess(elapsed, self._TIMING_BUDGET_SEC, "ок")


class Neighbour(unittest.TestCase):
    def test_b(self):
        self.assertLess(elapsed, 0.3, "сосед остался с прежним порогом")
'''

_GOOD_SAMPLES = {
    "оба через константу": '''
class T(unittest.TestCase):
    BUDGET_SEC = 2.0
    def test_a(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
    def test_b(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
''',
    "разные величины": '''
class T(unittest.TestCase):
    BUDGET_SEC = 2.0
    def test_a(self):
        self.assertLess(elapsed, self.BUDGET_SEC)
    def test_b(self):
        self.assertLess(payload_size, 4096)
''',
    "сравнение двух величин": '''
class T(unittest.TestCase):
    def test_a(self):
        self.assertLess(start_sec, end_sec)
    def test_b(self):
        self.assertLess(start_sec, 5.0)
''',
    "тривиальная граница": '''
class T(unittest.TestCase):
    RATIO_CAP = 0.9
    def test_a(self):
        self.assertLess(ratio, self.RATIO_CAP)
    def test_b(self):
        self.assertLess(ratio, 1)
''',
}


def selftest() -> int:
    """Классификатор обязан ловить известно-плохое и молчать на известно-хорошем.

    Гард, тихо переставший различать, отчитывался бы CLEAN вечно — то же самое
    правило, что у соседних аудитов репозитория.
    """
    failures = []

    bad = scan_source(_BAD_SAMPLE, "<bad>")
    if len(bad) != 1:
        failures.append(f"известно-плохой образец: ожидалась 1 находка, получено {len(bad)}")
    elif bad[0].measured != "elapsed":
        failures.append(f"известно-плохой образец: не та величина ({bad[0].measured})")

    for label, sample in _GOOD_SAMPLES.items():
        got = scan_source(sample, f"<good:{label}>")
        if got:
            failures.append(f"ложное срабатывание на «{label}»: {len(got)}")

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"SELFTEST OK: 1 плохой образец пойман, {len(_GOOD_SAMPLES)} хороших — чисто")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default="KrabEar", help="каталог для скана")
    parser.add_argument("--fail-on-found", action="store_true", help="ненулевой код при находках")
    parser.add_argument("--selftest", action="store_true", help="проверить классификатор")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    root = pathlib.Path(args.root)
    if not root.exists():
        print(f"каталог не найден: {root}", file=sys.stderr)
        return 2

    findings = scan_tree(root)
    if not findings:
        print("CLEAN — парных порогов с расхождением не найдено.")
        return 0

    print(f"НАЙДЕНО {len(findings)} расхождений парных порогов:\n")
    for f in findings:
        print(f.render())
        print()
    print(
        "Каждая находка — предел, выраженный двумя способами в одном файле.\n"
        "Правка одного места оставит второе позади: ровно так 29.08.2026 упал\n"
        "test_metadata_enricher_W1765 после того, как бюджет подняли только в\n"
        "соседнем классе."
    )
    return 1 if args.fail_on_found else 0


if __name__ == "__main__":
    sys.exit(main())
