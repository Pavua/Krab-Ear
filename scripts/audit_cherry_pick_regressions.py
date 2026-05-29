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
    """Return set of names defined at module top-level in a .py file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
            # re-exports: `from x import Y` at module level counts as a definition
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    exported = alias.asname if alias.asname else alias.name
                    if exported != "*":
                        names.add(exported)
            else:
                for alias in node.names:
                    exported = alias.asname if alias.asname else alias.name.split(".")[0]
                    names.add(exported)

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
