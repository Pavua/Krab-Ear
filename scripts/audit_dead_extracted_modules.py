#!/usr/bin/env python3
"""audit_dead_extracted_modules.py — W1768 decorative-extraction guard.

Корневая причина (root cause, W746-класс): "extraction" модуля, которая
ничего не меняет, потому что монолит (``service.py``) продолжает использовать
свою СОБСТВЕННУЮ инлайн-копию. Подтверждённые примеры (W797/W813/W828):

  * ``backend/ipc_dispatch.py`` — ``build_dispatch_table`` НЕ импортируется
    нигде в production; живая диспетчеризация — это инлайн-словарь
    ``handlers`` в ``service.py`` (~321 запись). Мёртвый модуль.
  * ``backend/ipc_server.py`` — ``IPCServer`` повторно определён инлайн в
    ``service.py`` (class IPCServer), и ``main()`` инстанцирует ИНЛАЙН-копию.
    Извлечённая копия достижима только через ленивый ре-экспорт в
    ``backend/__init__.py``, который никто не вызывает.
  * ``backend/service_logging.py`` — ``configure_logging``/``JsonFormatter``
    повторены инлайн в ``service.py``; ``main()`` зовёт инлайн-версию.

Этот скрипт детектирует два класса находок по ``KrabEar/backend/`` +
``KrabEar/core/``:

1. **Мёртвые модули** — top-level публичные символы модуля (классы и
   функции ``build_*`` / ``configure_*`` / ``make_*`` / ``create_*`` /
   ``get_*_factory`` / ``*_factory``) не импортируются НИГДЕ в production
   (любой non-test ``.py``: ``from <mod> import``, ``import <mod>``,
   ``<mod>.<symbol>``). Модуль, "используемый" только через ре-экспорт в
   ``backend/__init__.py``, который сам нигде не потребляется, тоже мёртв.

2. **Кросс-файловые дубликаты, где извлечённая копия мертва** — класс или
   функция определены И в извлечённом модуле, И инлайн в монолите
   (``service.py``), причём монолит использует СВОЮ инлайн-копию (не
   импортирует извлечённый символ). Извлечённая копия — тень/мёртвый код.

Использование::

    python3 scripts/audit_dead_extracted_modules.py
    python3 scripts/audit_dead_extracted_modules.py --json
    python3 scripts/audit_dead_extracted_modules.py --fail-on-found   # CI

Exit 0 → нет находок (или все в allowlist).
Exit 1 → найдены мёртвые модули / мёртвые дубликаты (только с --fail-on-found).

Allowlist: ``scripts/audit_dead_extracted_modules_allowlist.txt`` (по одной
записи на строку, ``#`` — комментарий). Форматы записей:

    module:backend/some_module.py          # модуль целиком разрешён как dead
    dup:IPCServer@backend/ipc_server.py    # символ@извлечённый-модуль разрешён
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Конфигурация скана
# ---------------------------------------------------------------------------

# Каталоги, которые НЕ сканируем как источник production-кода и не считаем
# импортёрами. Тесты исключаются из "production import" анализа намеренно:
# мёртвый в production модуль, который трогают только тесты, всё равно мёртв
# (как ipc_dispatch.build_dispatch_table — тесты есть, production нет).
SKIP_DIRS: frozenset[str] = frozenset({
    ".venv_krab_ear", ".venv", "venv", ".venv_gigaam", ".venv_vl",
    "worktrees", ".claude", ".git", "__pycache__", "dist", "build",
    ".eggs", "node_modules", "_legacy_tkinter_archive_2026-02-11",
})

# Внутри какого пакета ищем кандидатов на "извлечённый модуль".
CANDIDATE_PKG_DIRS: Tuple[str, ...] = ("backend", "core")

# Монолиты, чьи ИНЛАЙН-копии считаются "живыми" при кросс-файловом дублировании.
# (service.py — единственный известный монолит, но список расширяем.)
MONOLITH_RELPATHS: Tuple[str, ...] = (
    "backend/service.py",
)

# Префиксы/суффиксы имён функций, считающихся "публичной точкой входа" модуля
# (фабрики/конфигураторы). Классы с Uppercase-именем всегда публичны.
FACTORY_PREFIXES: Tuple[str, ...] = ("build_", "configure_", "make_", "create_", "init_")
FACTORY_SUFFIXES: Tuple[str, ...] = ("_factory",)

# Имя файла-реэкспортёра внутри пакета (его импорт символа НЕ считается
# реальным использованием, если сам реэкспортированный атрибут нигде не зовут).
PKG_INIT_NAME = "__init__.py"

# Generic-имена точек входа: законно дублируются в каждом runnable-скрипте,
# не являются «извлечённой копией». Исключаем из детектора дубликатов.
ENTRYPOINT_DUP_NAMES: frozenset[str] = frozenset({"main"})


# ---------------------------------------------------------------------------
# Обход файлов
# ---------------------------------------------------------------------------

def find_python_files(root: Path) -> List[Path]:
    """Walk root, yield .py files, prune SKIP_DIRS."""
    results: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                results.append(Path(dirpath) / fname)
    return sorted(results)


def is_test_file(path: Path) -> bool:
    """True если файл — тест (каталог tests/ или имя test_*.py / *_test.py)."""
    parts = set(path.parts)
    if "tests" in parts or "test" in parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


# ---------------------------------------------------------------------------
# Извлечение публичных символов модуля
# ---------------------------------------------------------------------------

def module_dotted_names(pkg_root: Path, path: Path) -> List[str]:
    """Возвращает возможные dotted-имена модуля для grep импортов.

    Например ``KrabEar/backend/ipc_dispatch.py`` → ['backend.ipc_dispatch',
    'ipc_dispatch'] (последнее — для относительных ``from .ipc_dispatch``).
    """
    rel = path.relative_to(pkg_root).with_suffix("")
    dotted_full = ".".join(rel.parts)            # backend.ipc_dispatch
    leaf = rel.parts[-1]                          # ipc_dispatch
    names = [dotted_full]
    if leaf != dotted_full:
        names.append(leaf)
    return names


def is_entrypoint_module(path: Path) -> bool:
    """True если модуль — runnable entry point (есть top-level
    ``if __name__ == "__main__":``).

    Такие модули по дизайну запускаются как скрипт (``python mod.py`` или
    через WSGI ``mod:create_app()``), а не импортируются — их «неимпортируемые»
    публичные символы не означают мёртвый модуль.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except (OSError, SyntaxError):  # pragma: no cover - defensive
        return False
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            # if __name__ == "__main__":
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                    and any(isinstance(c, ast.Constant) and c.value == "__main__"
                            for c in test.comparators)):
                return True
    return False


def extract_public_symbols(path: Path) -> Tuple[List[str], Dict[str, int]]:
    """Top-level публичные символы модуля + их номера строк.

    Публичными считаем:
      - все ``class Name`` верхнего уровня (любой регистр, но не _private);
      - функции верхнего уровня с фабричным префиксом/суффиксом
        (build_/configure_/make_/create_/init_/*_factory).

    Возвращает (список_имён, {имя: lineno}).
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (OSError, SyntaxError) as exc:  # pragma: no cover - defensive
        print(f"[WARN] cannot parse {path}: {exc}", file=sys.stderr)
        return [], {}

    symbols: List[str] = []
    lines: Dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            symbols.append(node.name)
            lines[node.name] = node.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            is_factory = (
                any(node.name.startswith(p) for p in FACTORY_PREFIXES)
                or any(node.name.endswith(s) for s in FACTORY_SUFFIXES)
            )
            if is_factory:
                symbols.append(node.name)
                lines[node.name] = node.lineno
    return symbols, lines


# ---------------------------------------------------------------------------
# Индекс импортов / использований по production-файлам
# ---------------------------------------------------------------------------

class UsageIndex:
    """Кэш «какой production-файл что импортирует и какие имена использует»."""

    def __init__(self, prod_files: List[Path]) -> None:
        self._prod_files = prod_files
        # module_dotted -> set(files импортирующих этот модуль через import/from)
        self._module_importers: Dict[str, Set[Path]] = defaultdict(set)
        # symbol_name -> set(files, где есть `from <mod> import <symbol>`)
        self._symbol_importers: Dict[str, Set[Path]] = defaultdict(set)
        # быстрый кэш текста файла
        self._text: Dict[Path, str] = {}
        self._build()

    def _read(self, path: Path) -> str:
        if path not in self._text:
            try:
                self._text[path] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover
                self._text[path] = ""
        return self._text[path]

    def _build(self) -> None:
        for path in self._prod_files:
            src = self._read(path)
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self._module_importers[alias.name].add(path)
                        # leaf тоже (для `import backend.ipc_server as x`)
                        self._module_importers[alias.name.split(".")[-1]].add(path)
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    # относительный (from .ipc_server import) → leaf
                    leaf = mod.split(".")[-1] if mod else ""
                    self._module_importers[mod].add(path)
                    if leaf:
                        self._module_importers[leaf].add(path)
                    for alias in node.names:
                        self._symbol_importers[alias.name].add(path)

    def files_importing_module(self, dotted_names: List[str]) -> Set[Path]:
        out: Set[Path] = set()
        for dn in dotted_names:
            out |= self._module_importers.get(dn, set())
        return out

    def files_importing_symbol(self, symbol: str) -> Set[Path]:
        return set(self._symbol_importers.get(symbol, set()))

    def attribute_use_count(self, symbol: str, exclude: Set[Path]) -> int:
        """Сколько production-файлов (кроме exclude) упоминают ``symbol`` как
        идентификатор (грубая текстовая проверка `\\bsymbol\\b`).

        Используется как fallback для модулей, которые могут зваться через
        ``mod.symbol`` без прямого ``from mod import symbol``.
        """
        pat = re.compile(rf"\b{re.escape(symbol)}\b")
        count = 0
        for path in self._prod_files:
            if path in exclude:
                continue
            if pat.search(self._read(path)):
                count += 1
        return count


# ---------------------------------------------------------------------------
# Детектор 1: мёртвые модули
# ---------------------------------------------------------------------------

def detect_dead_modules(
    pkg_root: Path,
    candidate_files: List[Path],
    usage: UsageIndex,
    reexport_consumed: Dict[str, bool],
) -> List[Dict]:
    """Находит модули, чьи публичные символы не импортируются ни одним
    production-файлом (кроме самого модуля и неиспользуемого ре-экспорта).

    ``reexport_consumed[symbol]`` — был ли символ, реэкспортированный из
    ``__init__.py``, реально потреблён где-либо (``pkg.symbol`` /
    ``from pkg import symbol``). Если нет — импорт внутри ``__init__.py`` не
    спасает модуль от статуса dead.
    """
    findings: List[Dict] = []

    for path in candidate_files:
        if is_entrypoint_module(path):
            # Runnable entry point (rest_server, *_worker): по дизайну не
            # импортируется — не считаем мёртвым модулем.
            continue
        symbols, sym_lines = extract_public_symbols(path)
        if not symbols:
            # Модуль без публичных классов/фабрик — не наша зона (utils, consts).
            continue

        dotted = module_dotted_names(pkg_root, path)
        importers = usage.files_importing_module(dotted)
        # исключаем сам модуль
        importers = {p for p in importers if p != path}

        # Разбираем импортёров: __init__.py (реэкспорт) vs реальные потребители.
        real_importers: Set[Path] = set()
        reexport_only: Set[Path] = set()
        for imp in importers:
            if imp.name == PKG_INIT_NAME:
                reexport_only.add(imp)
            else:
                real_importers.add(imp)

        # Если есть хотя бы один НЕ-__init__ импортёр модуля — живой.
        if real_importers:
            continue

        # Проверяем посимвольно: импортирует ли кто-то символ напрямую,
        # либо реэкспорт символа реально потреблён.
        symbol_alive = False
        live_evidence: List[str] = []
        for sym in symbols:
            sym_importers = {
                p for p in usage.files_importing_symbol(sym)
                if p != path and p.name != PKG_INIT_NAME
            }
            if sym_importers:
                symbol_alive = True
                live_evidence.append(
                    f"{sym} imported by {sorted(str(p) for p in sym_importers)[:2]}"
                )
                break
            # реэкспорт через __init__ + реальное потребление символа?
            if reexport_only and reexport_consumed.get(sym, False):
                symbol_alive = True
                live_evidence.append(f"{sym} re-exported via __init__ and consumed")
                break

        if symbol_alive:
            continue

        # Модуль мёртв в production.
        findings.append({
            "type": "dead_module",
            "module": str(path),
            "symbols": symbols,
            "symbol_lines": sym_lines,
            "reexport_only_importers": sorted(str(p) for p in reexport_only),
        })

    return findings


# ---------------------------------------------------------------------------
# Детектор 2: кросс-файловые дубликаты, где извлечённая копия мертва
# ---------------------------------------------------------------------------

def collect_toplevel_defs(path: Path) -> Dict[str, int]:
    """{имя_класса_или_функции верхнего уровня: lineno}."""
    out: Dict[str, int] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except (OSError, SyntaxError):  # pragma: no cover
        return out
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node.lineno)
    return out


def detect_dead_duplicates(
    pkg_root: Path,
    candidate_files: List[Path],
    monolith_files: List[Path],
    usage: UsageIndex,
) -> List[Dict]:
    """Находит символы, определённые И в извлечённом модуле, И инлайн в
    монолите, где монолит НЕ импортирует извлечённый символ (значит, использует
    свою инлайн-копию → извлечённая копия мертва/затенена).
    """
    findings: List[Dict] = []

    # Карта инлайн-определений каждого монолита.
    monolith_defs: Dict[Path, Dict[str, int]] = {
        m: collect_toplevel_defs(m) for m in monolith_files
    }

    for path in candidate_files:
        if path in monolith_files:
            continue
        ext_defs = collect_toplevel_defs(path)
        dotted = module_dotted_names(pkg_root, path)

        for mono_path, defs in monolith_defs.items():
            # Импортирует ли монолит сам модуль или его символы?
            mono_imports_module = mono_path in usage.files_importing_module(dotted)
            for name, ext_line in ext_defs.items():
                if name in ENTRYPOINT_DUP_NAMES:
                    # `main` и подобные законно дублируются в каждом
                    # runnable-скрипте — это не извлечённая копия.
                    continue
                if name not in defs:
                    continue  # нет инлайн-дубликата в монолите
                inline_line = defs[name]
                mono_imports_symbol = mono_path in usage.files_importing_symbol(name)
                if mono_imports_module or mono_imports_symbol:
                    # Монолит реально подтягивает извлечённую копию — не dead.
                    continue
                findings.append({
                    "type": "dead_duplicate",
                    "symbol": name,
                    "extracted_module": str(path),
                    "extracted_line": ext_line,
                    "monolith": str(mono_path),
                    "monolith_inline_line": inline_line,
                    "production_uses": "monolith inline copy "
                                       f"({mono_path.name}:{inline_line})",
                })

    return findings


# ---------------------------------------------------------------------------
# Реэкспорт-потребление: какие символы из __init__.py реально используются
# ---------------------------------------------------------------------------

def build_reexport_consumption(
    pkg_root: Path,
    prod_files: List[Path],
) -> Dict[str, bool]:
    """Для каждого символа, реэкспортированного из любого ``__init__.py``,
    определяет, потребляется ли он где-либо в production вне __init__.

    Потребление = ``from <pkg> import <symbol>`` ИЛИ текстовое ``pkg.symbol``
    (грубо, но достаточно: нам нужно лишь «есть/нет потребитель»).
    """
    consumed: Dict[str, bool] = {}
    init_files = [p for p in prod_files if p.name == PKG_INIT_NAME]
    if not init_files:
        return consumed

    # Имена символов, упомянутых в __all__ / __getattr__ каждого пакета,
    # + множество модулей, ОПРЕДЕЛЯЮЩИХ символ (из `from .X import Sym`
    # внутри __init__). Defining-модуль исключаем из проверки потребления —
    # иначе собственное определение символа ложно «подтверждает» использование.
    reexported: Set[str] = set()
    defining_leaves: Dict[str, Set[str]] = defaultdict(set)
    for init in init_files:
        try:
            tree = ast.parse(init.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            # __all__ = [...]
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    reexported.add(elt.value)
            # строковые сравнения в __getattr__: if name == "IPCServer"
            if isinstance(node, ast.Compare):
                for comp in node.comparators:
                    if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                        reexported.add(comp.value)
            # from .ipc_server import IPCServer → IPCServer определён в ipc_server.py
            if isinstance(node, ast.ImportFrom) and node.module:
                leaf = node.module.split(".")[-1]
                for alias in node.names:
                    defining_leaves[alias.name].add(leaf)

    for sym in reexported:
        importers = {
            p for p in prod_files
            if p.name != PKG_INIT_NAME and sym in _symbols_imported_in(p)
        }
        if importers:
            consumed[sym] = True
            continue
        # текстовый fallback: pkg.sym где-либо вне __init__ И вне defining-модуля.
        leaves = defining_leaves.get(sym, set())
        pat = re.compile(rf"\b{re.escape(sym)}\b")
        hit = any(
            pat.search(p.read_text(encoding="utf-8", errors="replace"))
            for p in prod_files
            if p.name != PKG_INIT_NAME and p.stem not in leaves
        )
        consumed[sym] = hit

    return consumed


def _symbols_imported_in(path: Path) -> Set[str]:
    out: Set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):  # pragma: no cover
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.add(alias.name)
    return out


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def load_allowlist(path: Path) -> Tuple[Set[str], Set[str]]:
    """Возвращает (allowed_modules, allowed_dups).

    allowed_modules: множество relpath модулей (``backend/foo.py``).
    allowed_dups:    множество ``symbol@relpath`` (``IPCServer@backend/ipc_server.py``).
    """
    modules: Set[str] = set()
    dups: Set[str] = set()
    if not path.exists():
        return modules, dups
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("module:"):
            modules.add(line[len("module:"):].strip())
        elif line.startswith("dup:"):
            dups.add(line[len("dup:"):].strip())
    return modules, dups


def _relpath(repo_root: Path, p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_audit(repo_root: Path) -> Tuple[List[Dict], List[Dict]]:
    """Возвращает (dead_modules, dead_duplicates) для данного repo_root.

    Вынесено отдельно от ``main`` для удобства юнит-тестов.
    """
    pkg_root = repo_root / "KrabEar"
    all_py = find_python_files(pkg_root)
    prod_files = [p for p in all_py if not is_test_file(p)]

    monolith_files = [
        repo_root / "KrabEar" / rel for rel in MONOLITH_RELPATHS
        if (repo_root / "KrabEar" / rel).exists()
    ]
    monolith_set = {p.resolve() for p in monolith_files}

    candidate_files = [
        p for p in prod_files
        if p.parent.name in CANDIDATE_PKG_DIRS or (
            len(p.relative_to(pkg_root).parts) >= 2
            and p.relative_to(pkg_root).parts[0] in CANDIDATE_PKG_DIRS
        )
    ]
    # Исключаем: сами __init__.py и монолиты (service.py) — монолит это
    # ПОТРЕБИТЕЛЬ/хаб, а не «извлечённый модуль»; его «неимпортируемость»
    # нерелевантна (его инстанцируют main.py/тесты как точку входа).
    candidate_files = [
        p for p in candidate_files
        if p.name != PKG_INIT_NAME and p.resolve() not in monolith_set
    ]

    usage = UsageIndex(prod_files)
    reexport_consumed = build_reexport_consumption(pkg_root, prod_files)

    dead_modules = detect_dead_modules(
        pkg_root, candidate_files, usage, reexport_consumed
    )
    dead_dups = detect_dead_duplicates(
        pkg_root, candidate_files, monolith_files, usage
    )
    return dead_modules, dead_dups


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit dead extracted modules & dead cross-file duplicates (W1768)."
    )
    parser.add_argument("--fail-on-found", action="store_true",
                        help="Exit 1 if any non-allowlisted finding (CI mode).")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")
    parser.add_argument("--root", default=None,
                        help="Repo root (default: parent of scripts/).")
    parser.add_argument("--allowlist", default=None,
                        help="Path to allowlist file "
                             "(default: scripts/audit_dead_extracted_modules_allowlist.txt).")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = Path(args.root).resolve() if args.root else script_dir.parent
    allow_path = (
        Path(args.allowlist) if args.allowlist
        else script_dir / "audit_dead_extracted_modules_allowlist.txt"
    )

    pkg_root = repo_root / "KrabEar"
    if not pkg_root.exists():
        print(f"[ERROR] KrabEar package not found under {repo_root}", file=sys.stderr)
        sys.exit(2)

    allowed_modules, allowed_dups = load_allowlist(allow_path)
    dead_modules, dead_dups = run_audit(repo_root)

    # Применяем allowlist (по relpath относительно repo_root).
    def mod_allowed(f: Dict) -> bool:
        return _relpath(repo_root, f["module"]) in allowed_modules

    def dup_allowed(f: Dict) -> bool:
        key = f"{f['symbol']}@{_relpath(repo_root, f['extracted_module'])}"
        return key in allowed_dups

    flagged_modules = [f for f in dead_modules if not mod_allowed(f)]
    flagged_dups = [f for f in dead_dups if not dup_allowed(f)]
    allowed_mod_hits = [f for f in dead_modules if mod_allowed(f)]
    allowed_dup_hits = [f for f in dead_dups if dup_allowed(f)]

    if args.json:
        payload = {
            "dead_modules": flagged_modules,
            "dead_duplicates": flagged_dups,
            "allowlisted_modules": [_relpath(repo_root, f["module"]) for f in allowed_mod_hits],
            "allowlisted_duplicates": [
                f"{f['symbol']}@{_relpath(repo_root, f['extracted_module'])}"
                for f in allowed_dup_hits
            ],
            "summary": {
                "dead_modules": len(flagged_modules),
                "dead_duplicates": len(flagged_dups),
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(repo_root, flagged_modules, flagged_dups,
                     allowed_mod_hits, allowed_dup_hits)

    total = len(flagged_modules) + len(flagged_dups)
    if args.fail_on_found and total:
        print(f"\n[FAIL] {total} dead-extraction finding(s) (--fail-on-found set).",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def _print_human(
    repo_root: Path,
    dead_modules: List[Dict],
    dead_dups: List[Dict],
    allowed_modules: List[Dict],
    allowed_dups: List[Dict],
) -> None:
    print("=" * 72)
    print("DEAD EXTRACTED MODULES & DEAD CROSS-FILE DUPLICATES (W1768)")
    print("=" * 72)

    print(f"\n--- Dead modules (no production importer): {len(dead_modules)} ---")
    if not dead_modules:
        print("  (none)")
    for f in dead_modules:
        rel = _relpath(repo_root, f["module"])
        syms = ", ".join(f["symbols"][:8])
        print(f"  DEAD MODULE: {rel}")
        print(f"      public symbols: {syms}")
        if f["reexport_only_importers"]:
            ro = ", ".join(_relpath(repo_root, p) for p in f["reexport_only_importers"])
            print(f"      only re-exported (unconsumed) via: {ro}")

    print(f"\n--- Dead cross-file duplicates (extracted copy shadowed): {len(dead_dups)} ---")
    if not dead_dups:
        print("  (none)")
    for f in dead_dups:
        ext = _relpath(repo_root, f["extracted_module"])
        mono = _relpath(repo_root, f["monolith"])
        print(f"  DEAD DUP: {f['symbol']}")
        print(f"      extracted copy : {ext}:{f['extracted_line']}  (DEAD)")
        print(f"      inline copy    : {mono}:{f['monolith_inline_line']}  (LIVE — production uses this)")

    if allowed_modules or allowed_dups:
        print(f"\n--- Allowlisted (intentional): "
              f"{len(allowed_modules)} module(s), {len(allowed_dups)} dup(s) ---")
        for f in allowed_modules:
            print(f"  [allow] module {_relpath(repo_root, f['module'])}")
        for f in allowed_dups:
            print(f"  [allow] dup {f['symbol']}@{_relpath(repo_root, f['extracted_module'])}")

    total = len(dead_modules) + len(dead_dups)
    if total == 0:
        print("\n[OK] No dead extracted modules or dead duplicates found.")
    else:
        print(f"\n[FOUND] {len(dead_modules)} dead module(s), "
              f"{len(dead_dups)} dead duplicate(s).")


if __name__ == "__main__":
    main()
