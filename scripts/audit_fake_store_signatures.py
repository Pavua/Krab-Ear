#!/usr/bin/env python3
"""audit_fake_store_signatures.py — гард класса «фейк разошёлся с зависимостью».

ROOT CAUSE (живой случай, PR #1916)
-----------------------------------
Тесты подделывают ``StateStore`` вручную — своим классом или MagicMock-фабрикой.
Сигнатура настоящего класса живёт своей жизнью: волна «Контенция общего лока»
(2f27c547) добавила ``load_settings(lock_timeout_sec=..., nowait=...)``, и
`SettingsService.cached_settings()` начал звать стор новыми именованными
аргументами. Ни один фейк об этом не узнал.

Коварство класса — в отложенности. Тест краснеет НЕ в момент дрейфа: пока путь
исполнения внутри теста не доходит до вызова с новым кваргом, всё зелено. В
PR #1916 так рвануло сразу 7 файлов, а ещё 22 фейка тихо ждали своей правки
вызывающего кода. Ручная сверка тут не работает: разошедшийся фейк выглядит как
совершенно обычный рабочий тест.

КРИТЕРИЙ (главное решение этого гарда)
---------------------------------------
Фейк обязан принимать НЕ всю сигнатуру реального класса, а ровно те
**именованные** аргументы, которыми его реально зовёт продовый код.

Наивный критерий «фейк должен повторять сигнатуру целиком» был отвергнут
замером: он дал 21 находку на ``save_settings``, где фейки называют параметр
``settings`` вместо ``new_settings``. Все они безвредны — прод зовёт этот метод
ПОЗИЦИОННО (``store.save_settings(payload)``), и имя параметра ни на что не
влияет. Гард с 21 ложной находкой из 43 не доживает до второго использования.

Поэтому:
  * ``store.load_settings(nowait=True)``  → фейк ОБЯЗАН принимать ``nowait``;
  * ``store.save_settings(payload)``      → к фейку требований нет;
  * фейк с ``**kwargs``                    → претензий нет по построению.

ЧТО СЧИТАЕТСЯ ФЕЙКОМ СТОРА
---------------------------
Только совпадения имени метода мало — тест вправе иметь свою функцию
``load_settings``, не имеющую отношения к ``StateStore``. Метод признаётся
фейком стора, если выполнено хотя бы одно:
  * он объявлен в классе, чьё имя содержит store/fake/stub/mock/persistence;
  * он объявлен в классе, где ≥2 метода совпадают с публичным API StateStore
    (структурная типизация: выглядит как стор — значит стор);
  * он объявлен внутри функции-фабрики, чьё имя содержит ``store``
    (паттерн ``_make_store()`` с ``MagicMock`` и ``side_effect``).

Сигнатуры реального класса читаются AST-разбором исходника, БЕЗ импорта
проекта: гард должен запускаться системным python3 в быстром CI-джобе, где
зависимости backend'а не установлены (та же причина, по которой чисто-AST
устроены audit_purge_coverage.py и соседи).
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
STATE_STORE = KRAB_EAR / "backend" / "state_store.py"
PROD_DIRS = ("backend", "core")
TESTS_DIR = KRAB_EAR / "tests"

# Имена, выдающие класс-подделку стора.
_FAKE_NAME_HINTS = ("store", "fake", "stub", "mock", "persistence")

# Сколько совпавших методов делает безымянный класс «структурным» стором.
_STRUCTURAL_METHOD_THRESHOLD = 2


@dataclass(frozen=True)
class Finding:
    """Одно расхождение фейка с реальным контрактом."""

    file: str
    line: int
    method: str
    missing: tuple[str, ...]

    def describe(self) -> str:
        args = ", ".join(sorted(self.missing))
        return f"{self.file}:{self.line}  {self.method}() не принимает: {args}"


# ---------------------------------------------------------------------------
# Реальные сигнатуры
# ---------------------------------------------------------------------------
def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = node.args
    return {p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)} - {"self", "cls"}


def _accepts_var_keyword(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.args.kwarg is not None


def extract_class_signatures(source: str, class_name: str) -> dict[str, set[str]]:
    """Публичные методы класса ``class_name`` → имена их параметров."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    result: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not item.name.startswith("_"):
                    result[item.name] = _param_names(item)
    return result


# ---------------------------------------------------------------------------
# Чем прод реально пользуется
# ---------------------------------------------------------------------------
def collect_required_kwargs(
    sources: Iterable[tuple[str, str]],
    known_methods: set[str],
) -> dict[str, set[str]]:
    """Именованные аргументы, которыми продовый код зовёт методы стора.

    Позиционные вызовы намеренно не создают требований — см. КРИТЕРИЙ в шапке.
    """
    required: dict[str, set[str]] = {}
    for _name, source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in known_methods:
                continue
            names = {kw.arg for kw in node.keywords if kw.arg}
            if names:
                required.setdefault(func.attr, set()).update(names)
    return required


# ---------------------------------------------------------------------------
# Фейки в тестах
# ---------------------------------------------------------------------------
def _looks_like_fake_store(
    ancestors: list[ast.AST],
    known_methods: set[str],
) -> bool:
    for parent in ancestors:
        if isinstance(parent, ast.ClassDef):
            if any(hint in parent.name.lower() for hint in _FAKE_NAME_HINTS):
                return True
            own = {
                item.name for item in parent.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if len(own & known_methods) >= _STRUCTURAL_METHOD_THRESHOLD:
                return True
        elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "store" in parent.name.lower():
                return True
    return False


def find_fake_drift(
    source: str,
    filename: str,
    required: dict[str, set[str]],
    known_methods: set[str],
) -> list[Finding]:
    """Фейки в одном тестовом файле, не принимающие требуемых kwargs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    findings: list[Finding] = []

    def visit(node: ast.AST, ancestors: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                needed = required.get(child.name)
                if needed and _looks_like_fake_store(ancestors, known_methods):
                    if not _accepts_var_keyword(child):
                        missing = needed - _param_names(child)
                        if missing:
                            findings.append(Finding(
                                file=filename,
                                line=child.lineno,
                                method=child.name,
                                missing=tuple(sorted(missing)),
                            ))
            visit(child, ancestors + [child])

    visit(tree, [])
    return findings


# ---------------------------------------------------------------------------
# Прогон по репозиторию
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def run_audit(repo_root: Path | None = None) -> tuple[list[Finding], dict[str, set[str]]]:
    root = repo_root or REPO_ROOT
    state_store = root / "KrabEar" / "backend" / "state_store.py"
    real = extract_class_signatures(_read(state_store), "StateStore")
    known = set(real)

    prod_sources: list[tuple[str, str]] = []
    for sub in PROD_DIRS:
        for path in sorted((root / "KrabEar" / sub).rglob("*.py")):
            if path.name == "state_store.py":
                continue
            prod_sources.append((str(path), _read(path)))
    required = collect_required_kwargs(prod_sources, known)

    # Требовать можно только то, что реальный класс действительно принимает:
    # иначе опечатка в продовом вызове превратилась бы в требование к фейкам.
    for method in list(required):
        required[method] &= real.get(method, set())
        if not required[method]:
            del required[method]

    findings: list[Finding] = []
    for path in sorted((root / "KrabEar" / "tests").glob("test_*.py")):
        rel = str(path.relative_to(root))
        findings.extend(find_fake_drift(_read(path), rel, required, known))
    return findings, required


# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------
def format_report(findings: list[Finding], required: dict[str, set[str]]) -> str:
    lines = [
        "=" * 72,
        "FAKE StateStore SIGNATURE DRIFT AUDIT",
        "=" * 72,
        "Критерий: фейк обязан принимать kwargs, которыми его зовёт прод.",
        "",
        f"Методов стора, вызываемых прод-кодом по имени: {len(required)}",
    ]
    for method in sorted(required):
        lines.append(f"  {method}: {sorted(required[method])}")
    lines.append("")

    if not findings:
        lines.append("CLEAN — расхождений не найдено.")
    else:
        lines.append(f"--- {len(findings)} расхождение(й) ---")
        for finding in findings:
            lines.append("  " + finding.describe())
        lines.append("")
        lines.append("Почини сигнатуру фейка (или дай ему **kwargs).")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_json(findings: list[Finding], required: dict[str, set[str]]) -> str:
    return json.dumps({
        "required_kwargs": {k: sorted(v) for k, v in sorted(required.items())},
        "findings": [
            {
                "file": f.file,
                "line": f.line,
                "method": f.method,
                "missing": list(f.missing),
            }
            for f in findings
        ],
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def selftest() -> int:
    """Известно-плохие и известно-хорошие образцы классификатора."""
    known = {"load_settings", "save_settings", "count_active_items"}
    required = {"load_settings": {"lock_timeout_sec", "nowait"}}

    bad_cases = [
        ("класс с Store в имени", '''
class FakeStore:
    def load_settings(self):
        return {}
'''),
        ("фабрика _make_store", '''
def _make_store():
    def load_settings():
        return {}
    return load_settings
'''),
        ("структурный стор без Store в имени", '''
class _Persist:
    def load_settings(self):
        return {}
    def save_settings(self, s):
        return s
'''),
        ("принимает лишь часть kwargs", '''
class FakeStore:
    def load_settings(self, nowait=False):
        return {}
'''),
    ]

    good_cases = [
        ("полная сигнатура", '''
class FakeStore:
    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return {}
'''),
        ("**kwargs", '''
class FakeStore:
    def load_settings(self, **kwargs):
        return {}
'''),
        ("посторонняя одноимённая функция", '''
def load_settings():
    return {}
'''),
        ("keyword-only параметры", '''
class FakeStore:
    def load_settings(self, *, lock_timeout_sec=None, nowait=False):
        return {}
'''),
    ]

    failures: list[str] = []
    for label, source in bad_cases:
        if not find_fake_drift(source, "t.py", required, known):
            failures.append(f"НЕ отловлен известно-плохой случай: {label}")
    for label, source in good_cases:
        found = find_fake_drift(source, "t.py", required, known)
        if found:
            failures.append(f"ложная тревога на известно-хорошем: {label} -> {found}")

    # Край критерия: позиционный вызов не создаёт требований.
    positional = collect_required_kwargs(
        [("svc.py", "def f(store):\n    store.save_settings({'a': 1})\n")], known
    )
    if "save_settings" in positional:
        failures.append("позиционный вызов ошибочно превращён в требование")

    if failures:
        for line in failures:
            print("SELFTEST FAIL:", line)
        return 1
    print(
        f"SELFTEST OK: {len(bad_cases)} известно-плохих отловлены, "
        f"{len(good_cases)} известно-хороших чисты."
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="ненулевой код возврата, если найдено расхождение",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="машиночитаемый JSON вместо текстового отчёта",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="прогнать известно-плохие/известно-хорошие образцы и выйти",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    findings, required = run_audit()
    print(format_json(findings, required) if args.json else format_report(findings, required))

    if args.fail_on_found and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
