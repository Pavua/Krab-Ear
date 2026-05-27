#!/usr/bin/env python3
"""
audit_duplicate_defs.py — W1441 meta-audit
Finds all duplicate function/method definitions within the same scope
(module-level or class-level) across KrabEar/**/*.py.

Root cause: W1425 (translator.py) + W1438 (audio_lang_id.py) revealed a pattern
where second definitions silently shadow first ones, masking shipped bug fixes.

Usage:
    python scripts/audit_duplicate_defs.py
    python scripts/audit_duplicate_defs.py --fail-on-found   # CI mode
"""

import ast
import os
import sys
import argparse
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple, Dict


SKIP_DIRS = {"tests", ".venv_krab_ear", ".venv", "venv", "worktrees", ".claude",
             ".git", "__pycache__", "dist", "build", ".eggs", ".venv_gigaam",
             ".venv_vl", "node_modules"}


def find_python_files(root: Path) -> List[Path]:
    """Walk root and yield .py files, skipping excluded dirs."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                results.append(Path(dirpath) / fname)
    return sorted(results)


def is_property_getter_setter_pair(filepath: Path, nodes, name: str, lines: List[int]) -> bool:
    """
    Return True if the duplicate definitions form a valid @property getter+setter pair.

    A pair is considered valid when:
    - Exactly 2 occurrences
    - One node has a @property decorator
    - The other node has a @<name>.setter decorator
    """
    if len(lines) != 2:
        return False

    # Collect the actual AST nodes for this function name
    fn_nodes = [
        n for n in nodes
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]
    if len(fn_nodes) != 2:
        return False

    def has_decorator(fn_node, decorator_name: str) -> bool:
        for dec in fn_node.decorator_list:
            # @property → ast.Name(id='property')
            if isinstance(dec, ast.Name) and dec.id == decorator_name:
                return True
            # @<name>.setter → ast.Attribute(value=ast.Name(id=name), attr='setter')
            if (isinstance(dec, ast.Attribute)
                    and dec.attr in ("setter", "deleter", "getter")
                    and isinstance(dec.value, ast.Name)
                    and dec.value.id == decorator_name):
                return True
        return False

    has_prop = any(has_decorator(n, "property") for n in fn_nodes)
    has_setter = any(
        has_decorator(n, name) for n in fn_nodes
    )
    return has_prop and has_setter


def extract_duplicates(filepath: Path) -> List[Dict]:
    """
    Parse filepath with ast and find duplicate function/method names
    within the same scope (module or class body).

    Skips valid @property getter+setter pairs.

    Returns list of dicts:
        {scope, name, first_line, second_line, all_lines, is_property_pair}
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] Cannot read {filepath}: {e}", file=sys.stderr)
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        print(f"[WARN] Syntax error in {filepath}: {e}", file=sys.stderr)
        return []

    findings = []

    def _check_scope(nodes, scope_name: str):
        """Given a list of AST nodes, find duplicate FunctionDef names."""
        seen: Dict[str, List[int]] = defaultdict(list)
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen[node.name].append(node.lineno)
        for name, lines in seen.items():
            if len(lines) > 1:
                prop_pair = is_property_getter_setter_pair(filepath, nodes, name, lines)
                findings.append({
                    "scope": scope_name,
                    "name": name,
                    "first_line": lines[0],
                    "second_line": lines[1],
                    "all_lines": lines,
                    "count": len(lines),
                    "is_property_pair": prop_pair,
                })

    # Module scope
    _check_scope(tree.body, scope_name="<module>")

    # Class scopes
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _check_scope(node.body, scope_name=node.name)

    return findings


def get_snippet(filepath: Path, lineno: int, context: int = 3) -> str:
    """Return a few lines around lineno from the file."""
    try:
        lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, lineno - 1)
        end = min(len(lines), lineno - 1 + context)
        snippet_lines = []
        for i in range(start, end):
            snippet_lines.append(f"  {i+1:>5}: {lines[i]}")
        return "\n".join(snippet_lines)
    except Exception:
        return "  (could not read snippet)"


def main():
    parser = argparse.ArgumentParser(description="Audit duplicate function/method defs in KrabEar")
    parser.add_argument("--fail-on-found", action="store_true",
                        help="Exit with code 1 if any duplicates are found (CI mode)")
    parser.add_argument("--root", default=None,
                        help="Root directory to scan (default: KrabEar/ relative to script)")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    scan_root = Path(args.root) if args.root else repo_root / "KrabEar"

    if not scan_root.exists():
        print(f"[ERROR] Scan root does not exist: {scan_root}", file=sys.stderr)
        sys.exit(2)

    print(f"Scanning: {scan_root}")
    py_files = find_python_files(scan_root)
    print(f"Found {len(py_files)} Python files to check")

    all_results: List[Tuple[Path, Dict]] = []
    for filepath in py_files:
        dups = extract_duplicates(filepath)
        for dup in dups:
            all_results.append((filepath, dup))

    if not all_results:
        print("\n[OK] No duplicate definitions found.")
        sys.exit(0)

    # Sort by file then scope then name
    all_results.sort(key=lambda x: (str(x[0]), x[1]["scope"], x[1]["name"]))

    # Separate genuine bugs from property pairs
    real_dups = [(f, d) for f, d in all_results if not d["is_property_pair"]]
    prop_pairs = [(f, d) for f, d in all_results if d["is_property_pair"]]

    print(f"\n{'='*70}")
    print(f"DUPLICATE DEFINITIONS FOUND: {len(all_results)} total")
    print(f"  Genuine shadowing bugs:      {len(real_dups)}")
    print(f"  @property getter/setter:     {len(prop_pairs)} (false positives)")
    print(f"{'='*70}\n")

    if real_dups:
        print("=== GENUINE SHADOWING BUGS (need fixing) ===\n")
        by_file_real: Dict[Path, List[Dict]] = defaultdict(list)
        for filepath, dup in real_dups:
            by_file_real[filepath].append(dup)

        for filepath, dups in sorted(by_file_real.items(), key=lambda x: str(x[0])):
            rel = filepath.relative_to(repo_root)
            print(f"FILE: {rel}  ({len(dups)} genuine duplicate(s))")
            print("-" * 60)
            for dup in dups:
                print(f"  scope={dup['scope']}  name={dup['name']}")
                print(f"  first def at line {dup['first_line']}, shadow at line {dup['second_line']}")
                if dup['count'] > 2:
                    print(f"  (appears {dup['count']} times total: {dup['all_lines']})")
                print(f"  -- first definition (line {dup['first_line']}):")
                print(get_snippet(filepath, dup['first_line']))
                print(f"  -- shadowing definition (line {dup['second_line']}):")
                print(get_snippet(filepath, dup['second_line']))
                print()
            print()

    if prop_pairs:
        print("=== @PROPERTY GETTER/SETTER PAIRS (false positives — OK) ===\n")
        by_file_prop: Dict[Path, List[Dict]] = defaultdict(list)
        for filepath, dup in prop_pairs:
            by_file_prop[filepath].append(dup)
        for filepath, dups in sorted(by_file_prop.items(), key=lambda x: str(x[0])):
            rel = filepath.relative_to(repo_root)
            names = ", ".join(d["name"] for d in dups)
            print(f"  {rel}: {names}")
        print()

    print(f"Total files with genuine duplicates: {len(set(str(f) for f,_ in real_dups))}")
    print(f"Total genuine shadowing bugs:        {len(real_dups)}")

    if args.fail_on_found and real_dups:
        print("\n[FAIL] Genuine duplicate definitions found (--fail-on-found is set)", file=sys.stderr)
        sys.exit(1)
    elif not real_dups:
        print("\n[OK] No genuine duplicate definitions found (property pairs are expected).")
        sys.exit(0)


if __name__ == "__main__":
    main()
