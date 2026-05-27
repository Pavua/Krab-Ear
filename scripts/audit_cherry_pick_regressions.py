#!/usr/bin/env python3
"""
W1525: audit_cherry_pick_regressions.py

Scan all KrabEar/**/*.py test files for symbols imported from production
modules that no longer exist — indicating a cherry-pick --theirs reversion.

Usage:
    python scripts/audit_cherry_pick_regressions.py
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
KRAB_EAR = PROJECT_ROOT / "KrabEar"
TESTS_DIR = KRAB_EAR / "tests"

# Module alias map: how test imports map to actual source files
# "backend.foo" -> KrabEar/backend/foo.py
# "core.foo"    -> KrabEar/core/foo.py
# "foo"         -> KrabEar/backend/foo.py  OR  KrabEar/core/foo.py

MODULE_SEARCH_DIRS = [
    KRAB_EAR / "backend",
    KRAB_EAR / "core",
    KRAB_EAR,
]


def module_path(module_name: str) -> Optional[Path]:
    """Resolve a module name to its source file, or None if not found."""
    # dotted: backend.foo or core.foo
    parts = module_name.split(".")
    for search_dir in MODULE_SEARCH_DIRS:
        candidate = search_dir.joinpath(*parts[1:] if len(parts) > 1 else parts).with_suffix(".py")
        if candidate.exists():
            return candidate
        # Also try without prefix (bare name)
        if len(parts) > 1:
            candidate2 = search_dir.joinpath(*parts).with_suffix(".py")
            if candidate2.exists():
                return candidate2
    # bare name search
    for search_dir in MODULE_SEARCH_DIRS:
        candidate = (search_dir / parts[-1]).with_suffix(".py")
        if candidate.exists():
            return candidate
    return None


def get_module_symbols(filepath: Path) -> set:
    """Extract all top-level names defined in a Python source file."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return set()

    names = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # re-exports via 'from x import y as y' — count them
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def extract_imports_from_test(filepath: Path) -> list:
    """
    Return list of (module_name, [symbol, ...]) for all
    'from X import Y, Z' statements in a test file.
    Only includes imports that reference KrabEar production modules.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Only production modules (backend.*, core.*, or bare names that exist)
            if not (mod.startswith("backend") or mod.startswith("core")):
                # Check if it's a bare module name that lives in backend/ or core/
                candidate = module_path(mod)
                if candidate is None:
                    continue
            symbols = [alias.name for alias in node.names if alias.name != "*"]
            if symbols:
                results.append((mod, symbols))
    return results


def check_file(test_file: Path, module_symbol_cache: dict) -> list:
    """
    Returns list of dicts:
      {test_file, module, symbol, module_file, severity}
    for each missing symbol.
    """
    findings = []
    for mod_name, symbols in extract_imports_from_test(test_file):
        mod_file = module_path(mod_name)
        if mod_file is None:
            continue  # Can't resolve — skip

        cache_key = str(mod_file)
        if cache_key not in module_symbol_cache:
            module_symbol_cache[cache_key] = get_module_symbols(mod_file)
        defined = module_symbol_cache[cache_key]

        for sym in symbols:
            # Skip dunder and very common names that might be re-exported
            if sym.startswith("__"):
                continue
            if sym not in defined:
                findings.append(
                    {
                        "test_file": str(test_file.relative_to(PROJECT_ROOT)),
                        "module": mod_name,
                        "module_file": str(mod_file.relative_to(PROJECT_ROOT)),
                        "symbol": sym,
                    }
                )
    return findings


def severity(finding: dict) -> str:
    """Classify severity based on symbol type heuristic."""
    sym = finding["symbol"]
    # Constants (ALL_CAPS) — configuration/threshold values, HIGH severity if missing
    if sym.isupper():
        return "HIGH"
    # Private helpers (_name) — MED
    if sym.startswith("_"):
        return "MED"
    # Public classes/functions — HIGH
    return "HIGH"


def main():
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    print(f"Scanning {len(test_files)} test files in {TESTS_DIR}...")

    module_symbol_cache = {}
    all_findings = []

    for tf in test_files:
        findings = check_file(tf, module_symbol_cache)
        all_findings.extend(findings)

    # Deduplicate by (module, symbol)
    seen = set()
    unique = []
    for f in all_findings:
        key = (f["module"], f["symbol"])
        if key not in seen:
            seen.add(key)
            f["severity"] = severity(f)
            unique.append(f)

    # Sort: HIGH first, then by module
    unique.sort(key=lambda f: (0 if f["severity"] == "HIGH" else 1, f["module"], f["symbol"]))

    if not unique:
        print("No regressions found.")
        return unique

    print(f"\nFound {len(unique)} unique missing symbols:\n")
    for i, f in enumerate(unique[:10], 1):
        print(
            f"  [{i}] [{f['severity']}] {f['module']}.{f['symbol']}"
            f"\n       missing from: {f['module_file']}"
            f"\n       detected in:  {f['test_file']}\n"
        )

    return unique


if __name__ == "__main__":
    findings = main()
    sys.exit(0 if not findings else 1)
