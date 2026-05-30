#!/usr/bin/env python3
"""
audit_cherry_pick_regressions.py — W1525 regression scanner.

For each KrabEar/tests/test_*.py file, parse `from X import Y` statements
using AST and check whether Y exists in the resolved KrabEar/X.py module.
Reports (test_file, expected_symbol, missing_from_module) triples.

Usage:
    python scripts/audit_cherry_pick_regressions.py [--json]
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
TESTS_DIR = KRAB_EAR / "tests"


def _module_path(module_name: str) -> Path | None:
    """Resolve a dotted module name to a .py path inside KrabEar/."""
    parts = module_name.split(".")
    # Try as package file
    candidate = KRAB_EAR / Path(*parts).with_suffix(".py")
    if candidate.exists():
        return candidate
    # Try as package __init__
    candidate2 = KRAB_EAR / Path(*parts) / "__init__.py"
    if candidate2.exists():
        return candidate2
    return None


def _collect_top_level_names(path: Path) -> set[str]:
    """Return set of names defined at module top-level in a .py file.

    Only names that are actually reachable at module scope are collected.
    Definitions nested inside a class or function body are intentionally
    excluded — they are attributes/locals, not module-level symbols.

    Control-flow containers (if/try/with/for/while) DO leak names to module
    scope in Python, so we recurse into their bodies.  ClassDef and
    FunctionDef/AsyncFunctionDef bodies are opaque walls — we record the
    class/function *name* itself but do NOT descend into their contents.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set()

    names: set[str] = set()

    def _scan_stmts(stmts: list) -> None:
        """Collect module-level names from a flat list of AST statements."""
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Record the name of the definition itself, but do NOT
                # recurse into the body — inner defs are NOT module-level.
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, ast.Tuple):
                        for elt in ast.walk(t):
                            if isinstance(elt, ast.Name):
                                names.add(elt.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # re-exports: `from x import Y` at module level counts as defined
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        exported = alias.asname if alias.asname else alias.name
                        if exported != "*":
                            names.add(exported)
                else:
                    for alias in node.names:
                        exported = alias.asname if alias.asname else alias.name.split(".")[0]
                        names.add(exported)
            # Control-flow: these containers leak names into the enclosing
            # (module) scope, so recurse into their sub-statement lists.
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With,
                                   ast.AsyncFor, ast.AsyncWith)):
                _scan_stmts(node.body)
                _scan_stmts(node.orelse)
            elif isinstance(node, ast.Try):
                _scan_stmts(node.body)
                _scan_stmts(node.orelse)
                _scan_stmts(node.finalbody)
                for handler in node.handlers:
                    _scan_stmts(handler.body)
            elif hasattr(ast, "TryStar") and isinstance(node, ast.TryStar):
                # Python 3.11+ except* syntax
                _scan_stmts(node.body)
                _scan_stmts(node.finalbody)
                for handler in node.handlers:
                    _scan_stmts(handler.body)

    _scan_stmts(tree.body)
    return names


def _parse_from_imports(test_path: Path) -> list[tuple[str, str]]:
    """Return list of (module_name, symbol) from `from X import Y` in a test file."""
    try:
        source = test_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(test_path))
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        # Only care about project-internal imports (backend.*, core.*)
        if not (module.startswith("backend.") or module.startswith("core.")):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            results.append((module, alias.name))
    return results


def main(as_json: bool = False, fail_on_found: bool = False) -> list[dict]:
    missing: list[dict] = []
    checked = 0
    skipped_modules: set[str] = set()

    test_files = sorted(TESTS_DIR.glob("test_*.py"))

    for test_path in test_files:
        imports = _parse_from_imports(test_path)
        for module_name, symbol in imports:
            checked += 1
            mod_path = _module_path(module_name)
            if mod_path is None:
                skipped_modules.add(module_name)
                continue
            defined = _collect_top_level_names(mod_path)
            if symbol not in defined:
                missing.append(
                    {
                        "test_file": str(test_path.relative_to(REPO_ROOT)),
                        "module": module_name,
                        "symbol": symbol,
                        "source_file": str(mod_path.relative_to(REPO_ROOT)),
                    }
                )

    if as_json:
        print(json.dumps({"missing": missing, "checked": checked}, indent=2))
        return missing

    print(f"Checked {checked} from-imports across {len(test_files)} test files.")
    print(f"Skipped {len(skipped_modules)} unresolvable modules.")
    if missing:
        print(f"\nMISSING SYMBOLS ({len(missing)}):")
        for m in missing:
            print(
                f"  {m['test_file']}"
                f"\n    from {m['module']} import {m['symbol']}"
                f"\n    (source: {m['source_file']})\n"
            )
        if fail_on_found:
            sys.exit(1)
    else:
        print("\nNo missing symbols found — all imports resolve.")

    return missing


if __name__ == "__main__":
    fail_on_found = "--fail-on-found" in sys.argv
    as_json = "--json" in sys.argv
    missing = main(as_json=as_json, fail_on_found=fail_on_found)
