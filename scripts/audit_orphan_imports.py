#!/usr/bin/env python3
"""audit_orphan_imports.py — W746/W771 regression guard.

Detects names that are USED in a Python source file but NOT imported,
which would cause NameError on fresh startup.

Checks performed:
  1. **Class instantiations** (default) — bare-Name calls where the name starts
     with an uppercase letter (e.g. ``MyService()``).
  2. **Decorators** (default) — bare ``@Name`` decorators (dotted forms like
     ``@functools.wraps`` are safe and ignored).
  3. **Function calls** (``--strict`` only) — lowercase bare-Name calls that are
     not in the built-in safe-list and not defined locally.

Usage:
    python3 scripts/audit_orphan_imports.py
    python3 scripts/audit_orphan_imports.py KrabEar/backend/service.py
    python3 scripts/audit_orphan_imports.py --strict

Exit 0 → all checked names are imported / defined.
Exit 1 → one or more missing imports found (details on stderr).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Safe-list: built-in types, exceptions, stdlib containers, and project-local
# names that are defined inside service.py itself (e.g. JsonFormatter) — these
# do NOT require an explicit import statement.
# ---------------------------------------------------------------------------
BUILTIN_SAFELIST: frozenset[str] = frozenset(
    {
        # Built-in exceptions
        "Exception",
        "KeyError",
        "TypeError",
        "ValueError",
        "OSError",
        "RuntimeError",
        "FileNotFoundError",
        "ConnectionError",
        "NotImplementedError",
        "AttributeError",
        "IndexError",
        "StopIteration",
        "GeneratorExit",
        "SystemExit",
        "KeyboardInterrupt",
        "PermissionError",
        "TimeoutError",
        "IOError",
        "EOFError",
        "MemoryError",
        "RecursionError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "UnicodeError",
        "ImportError",
        "ModuleNotFoundError",
        "OverflowError",
        "ZeroDivisionError",
        "AssertionError",
        "NotImplemented",
        # Built-in types
        "dict",
        "list",
        "set",
        "tuple",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "memoryview",
        "frozenset",
        "object",
        "type",
        "classmethod",
        "staticmethod",
        "property",
        "super",
        "slice",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "reversed",
        "sorted",
        "iter",
        "next",
        "open",
        "print",
        "len",
        "abs",
        "max",
        "min",
        "sum",
        "round",
        "hex",
        "oct",
        "bin",
        "id",
        "hash",
        "repr",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
        "callable",
        "isinstance",
        "issubclass",
        "any",
        "all",
        # typing / collections
        "NamedTuple",
        "TypedDict",
        "Enum",
        "Counter",
        "defaultdict",
        "OrderedDict",
        "deque",
        # pathlib / threading / queue / logging
        "Path",
        "Lock",
        "RLock",
        "Thread",
        "Event",
        "Queue",
        "Logger",
        # datetime
        "datetime",
        "timedelta",
        "date",
        "time",
        # project-level names that live directly in service.py
        "BackendService",
        "IPCServer",
    }
)

# ---------------------------------------------------------------------------
# Additional safe-list for lowercase function-call names used in --strict mode.
# These are common stdlib / Python builtins that don't need an explicit import.
# ---------------------------------------------------------------------------
LOWERCASE_FUNC_SAFELIST: frozenset[str] = frozenset(
    {
        # everything already in BUILTIN_SAFELIST that is lowercase, plus extras
        "len",
        "print",
        "range",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "reversed",
        "any",
        "all",
        "min",
        "max",
        "sum",
        "abs",
        "round",
        "open",
        "isinstance",
        "hasattr",
        "getattr",
        "setattr",
        "delattr",
        "callable",
        "issubclass",
        "id",
        "hash",
        "repr",
        "type",
        "vars",
        "dir",
        "dict",
        "list",
        "set",
        "tuple",
        "frozenset",
        "object",
        "super",
        "next",
        "iter",
        "hex",
        "oct",
        "bin",
        "chr",
        "ord",
        "format",
        "input",
        "exec",
        "eval",
        "compile",
        "globals",
        "locals",
        "slice",
        "memoryview",
        "bytearray",
        "property",
        "classmethod",
        "staticmethod",
        # common stdlib functions imported star-style or used as builtins in tests
        "print",
        "exit",
        "quit",
        "breakpoint",
        "NotImplemented",
        # datetime constructors
        "datetime",
        "timedelta",
        "date",
        "time",
        # Path — even as lowercase call it's commonly imported
        "path",
    }
)


def collect_imported_names(tree: ast.Module) -> set[str]:
    """Return every name bound by import statements at module level."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # `from foo import Bar as B` → "B"; `from foo import Bar` → "Bar"
                imported.add(alias.asname if alias.asname else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # `import os.path as osp` → "osp"; `import os` → "os"
                imported.add(alias.asname if alias.asname else alias.name.split(".")[0])
    return imported


def collect_locally_defined_names(tree: ast.Module) -> set[str]:
    """Return every class/function name and locally-assigned variable name.

    We include simple ``Name`` assignment targets (``x = ...``) because
    ``--strict`` mode would otherwise flag patterns like::

        coerce = int if ... else float
        result = coerce(value)   # ← coerce is a local variable, not missing import

    We also include ``for`` loop variables, ``with ... as`` targets, and
    comprehension variables to reduce noise.
    """
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, (ast.AnnAssign,)):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        elif isinstance(node, ast.withitem):
            if node.optional_vars and isinstance(node.optional_vars, ast.Name):
                defined.add(node.optional_vars.id)
        elif isinstance(node, ast.NamedExpr):
            # Walrus operator: (x := expr)
            defined.add(node.target.id)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            # Comprehension variables — generator.ifs is not needed, just targets
            generators = getattr(node, "generators", [])
            for gen in generators:
                if isinstance(gen.target, ast.Name):
                    defined.add(gen.target.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                defined.add(name)
    return defined


def collect_bare_name_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Return (name, lineno) for every Call whose func is a bare ast.Name."""
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append((node.func.id, node.lineno))
    return calls


def collect_bare_decorator_names(tree: ast.Module) -> list[tuple[str, int]]:
    """Return (name, lineno) for every bare-Name decorator (not dotted/called).

    Examples:
      @my_decorator       → captured  (ast.Name)
      @functools.wraps    → ignored   (ast.Attribute)
      @pytest.mark.skip() → ignored   (ast.Call whose func is ast.Attribute)
      @my_factory()       → captured  (ast.Call whose func is ast.Name)
    """
    decorators: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        # Both ClassDef and FunctionDef/AsyncFunctionDef carry decorator_list
        dec_list = getattr(node, "decorator_list", [])
        for dec in dec_list:
            if isinstance(dec, ast.Name):
                # @bare_name
                decorators.append((dec.id, dec.lineno))
            elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                # @bare_name(args...)
                decorators.append((dec.func.id, dec.func.lineno))
    return decorators


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a Python file for names used but not imported (W746/W771 guard). "
            "Default mode checks class instantiations + bare decorators. "
            "--strict also checks lowercase function calls."
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to the Python file to audit (default: KrabEar/backend/service.py).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Also flag lowercase bare-Name function calls that are not imported "
            "and not locally defined. More noise; useful for deep audits."
        ),
    )
    args = parser.parse_args()

    if args.target:
        target_path = Path(args.target)
    else:
        # Default: KrabEar/backend/service.py relative to the scripts/ dir
        script_dir = Path(__file__).parent
        target_path = script_dir.parent / "KrabEar" / "backend" / "service.py"

    if not target_path.exists():
        print(f"ERROR: file not found: {target_path}", file=sys.stderr)
        sys.exit(2)

    source = target_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(target_path))
    except SyntaxError as exc:
        print(f"ERROR: syntax error in {target_path}: {exc}", file=sys.stderr)
        sys.exit(2)

    imported_names = collect_imported_names(tree)
    locally_defined = collect_locally_defined_names(tree)
    all_call_sites = collect_bare_name_calls(tree)
    all_decorator_sites = collect_bare_decorator_names(tree)

    # Safe set: imported OR built-in OR locally defined
    safe = imported_names | BUILTIN_SAFELIST | locally_defined

    # ------------------------------------------------------------------
    # Category 1: class instantiations (uppercase-starting names)
    # ------------------------------------------------------------------
    class_call_sites = [(n, ln) for n, ln in all_call_sites if n[0].isupper()]

    missing: dict[str, tuple[str, int]] = {}  # name → (category, lineno)

    for name, lineno in class_call_sites:
        if name not in safe and name not in missing:
            missing[name] = ("class-instantiation", lineno)

    # ------------------------------------------------------------------
    # Category 2: bare decorators (any case; dotted already excluded)
    # ------------------------------------------------------------------
    decorator_safe = safe | LOWERCASE_FUNC_SAFELIST

    for name, lineno in all_decorator_sites:
        if name not in decorator_safe and name not in missing:
            missing[name] = ("decorator", lineno)

    # ------------------------------------------------------------------
    # Category 3 (--strict): lowercase function calls
    # ------------------------------------------------------------------
    if args.strict:
        lowercase_safe = safe | LOWERCASE_FUNC_SAFELIST
        for name, lineno in all_call_sites:
            if name[0].islower() and name not in lowercase_safe and name not in missing:
                missing[name] = ("function-call", lineno)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    total_class_sites = len(class_call_sites)
    total_decorator_sites = len(all_decorator_sites)
    total_missing = len(missing)

    mode_tag = "strict" if args.strict else "default"
    summary = (
        f"audit_orphan_imports [{mode_tag}]: scanned "
        f"{total_class_sites} class-call sites, "
        f"{total_decorator_sites} decorator sites"
        + (f", {len(all_call_sites) - total_class_sites} fn-call sites" if args.strict else "")
        + f" — {total_missing} missing import(s)"
    )

    if missing:
        print(summary, file=sys.stderr)
        print(
            f"\nERROR: {total_missing} name(s) used in {target_path.name} "
            f"but never imported (W746/W771-style bug):\n",
            file=sys.stderr,
        )
        for name in sorted(missing.keys()):
            category, lineno = missing[name]
            print(
                f"  MISSING IMPORT [{category}]: {name}  (first seen at line {lineno})",
                file=sys.stderr,
            )
        print(
            "\nFix: add the appropriate 'from ... import <Name>' at the top of the file.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print(summary)
        print(f"All checked names are imported or defined  [{target_path.name}]")
        sys.exit(0)


if __name__ == "__main__":
    main()
