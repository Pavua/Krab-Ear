#!/usr/bin/env python3
"""audit_orphan_imports.py — W746 regression guard.

Detects class names that are USED (instantiated by bare-name call) in a Python
source file but NOT imported, which would cause NameError on fresh startup.

Usage:
    python3 scripts/audit_orphan_imports.py
    python3 scripts/audit_orphan_imports.py KrabEar/backend/service.py

Exit 0 → all used class names are imported.
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


def collect_locally_defined_classes(tree: ast.Module) -> set[str]:
    """Return every class name defined via ClassDef at any nesting level."""
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            defined.add(node.name)
    return defined


def collect_bare_name_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Return (name, lineno) for every Call whose func is a bare ast.Name."""
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append((node.func.id, node.lineno))
    return calls


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit service.py for class names used but not imported (W746 guard)."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Path to the Python file to audit (default: KrabEar/backend/service.py relative to this script).",
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
    locally_defined = collect_locally_defined_classes(tree)
    all_call_sites = collect_bare_name_calls(tree)

    # Filter to names that look like class instantiations (start with uppercase)
    class_call_sites = [(name, lineno) for name, lineno in all_call_sites if name[0].isupper()]

    # Unique class names used (for counting)
    used_class_names: set[str] = {name for name, _ in class_call_sites}

    # Safe: imported OR built-in OR locally defined
    safe = imported_names | BUILTIN_SAFELIST | locally_defined

    # Identify missing: used but not safe
    # Collect per-name the first line number where it appears
    missing: dict[str, int] = {}
    for name, lineno in class_call_sites:
        if name not in safe and name not in missing:
            missing[name] = lineno

    total_call_sites = len(class_call_sites)
    total_used = len(used_class_names)
    total_missing = len(missing)

    summary = (
        f"audit_orphan_imports: scanned {total_call_sites} call sites, "
        f"found {total_used} used classes, {total_missing} missing imports"
    )

    if missing:
        print(summary, file=sys.stderr)
        print(
            f"\nERROR: {total_missing} class name(s) used in {target_path.name} "
            f"but never imported (W746-style bug):\n",
            file=sys.stderr,
        )
        for name in sorted(missing.keys()):
            print(f"  MISSING IMPORT: {name}  (first used at line {missing[name]})", file=sys.stderr)
        print(
            "\nFix: add the appropriate 'from ... import <ClassName>' at the top of the file.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print(summary)
        print(f"✓ All used class names are imported  [{target_path.name}]")
        sys.exit(0)


if __name__ == "__main__":
    main()
