#!/usr/bin/env python3
"""Verify file paths mentioned in CLAUDE.md actually exist in repo.

Usage: python scripts/verify_claude_md.py [--strict] [--report-extras]

Resolution strategy:
  1. Check repo_root / candidate directly.
  2. If not found and candidate has no leading directory, search repo for a
     file with that basename (Swift sources, top-level scripts, etc.).
  3. If candidate looks like a bare module path (e.g. ``backend/service.py``),
     also try known prefixes: KrabEar/, native/KrabEarAgent/Sources/KrabEarAgent/.

Exits 1 if any mentioned file is missing.
Exits 2 if CLAUDE.md itself is not found.
"""
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Pattern for plausible file paths wrapped in backticks or asterisks.
# Captures the path group only (group 1).
PATH_PATTERN = re.compile(
    r"[`*]([A-Za-z][\w/\-\.+]+\.(py|swift|json|md|command|sh|txt|yml|yaml|toml|plist|m|h|c|cpp|js|ts|tsx))[`*]"
)

# Extensions worth tracking
KNOWN_EXTS = {
    ".py", ".swift", ".json", ".md", ".command", ".sh",
    ".plist", ".yml", ".yaml", ".toml", ".txt",
}

# Prefixes that identify non-repo external paths / URLs — skip these.
SKIP_PREFIXES = (
    "http://", "https://", "/Users/", "/opt/", "/System/",
    "/Library/", "/tmp/", "/var/",
)

# Candidate repo-relative prefixes to try when the bare path is not found.
# Ordered from most-specific to most-general.
SEARCH_PREFIXES = [
    "KrabEar",
    "native/KrabEarAgent/Sources/KrabEarAgent",
    "native/KrabEarAgent",
    "scripts",
    "docs",
    ".github/workflows",
]

# Directories to skip when doing a fallback glob search (venvs, caches, etc.)
_SKIP_DIRS = {".venv", ".venv_krab_ear", "venv", "__pycache__", ".build", ".git"}


def _repo_file_index() -> Dict[str, List[Path]]:
    """Build a basename → [Path] index of all repo files (lazy, cached)."""
    idx: Dict[str, List[Path]] = {}
    for p in REPO_ROOT.rglob("*"):
        if p.is_file():
            # Skip anything inside skip directories
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            idx.setdefault(p.name, []).append(p)
    return idx


_INDEX: Optional[Dict[str, List[Path]]] = None


def _get_index() -> Dict[str, List[Path]]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _repo_file_index()
    return _INDEX


def file_exists_in_repo(candidate: str) -> bool:
    """Return True if ``candidate`` resolves to an existing repo file."""
    # 1. Direct path from repo root.
    if (REPO_ROOT / candidate).exists():
        return True

    # 2. Try known repo-relative prefixes.
    for prefix in SEARCH_PREFIXES:
        if (REPO_ROOT / prefix / candidate).exists():
            return True

    # 3. Fallback: basename search across entire repo.
    #    Only do this for bare filenames (no directory component) to avoid
    #    false positives on ambiguous short paths.
    p = Path(candidate)
    if p.parent == Path("."):
        idx = _get_index()
        if p.name in idx:
            return True

    return False


def extract_paths(content: str) -> set[str]:
    """Extract plausible relative file paths from CLAUDE.md content."""
    paths: Set[str] = set()
    for match in PATH_PATTERN.finditer(content):
        candidate = match.group(1)
        # Skip URLs and absolute paths to external dirs
        if any(candidate.startswith(prefix) for prefix in SKIP_PREFIXES):
            continue
        # Skip absolute paths (start with /)
        if candidate.startswith("/"):
            continue
        # Must have a path separator OR a known extension to be worth checking
        if "/" not in candidate and Path(candidate).suffix not in KNOWN_EXTS:
            continue
        paths.add(candidate)
    return paths


def main() -> int:
    if not CLAUDE_MD.exists():
        print(f"CLAUDE.md not found at {CLAUDE_MD}", file=sys.stderr)
        return 2

    content = CLAUDE_MD.read_text(encoding="utf-8")
    paths = extract_paths(content)

    missing: List[str] = []
    for p in sorted(paths):
        if not file_exists_in_repo(p):
            missing.append(p)

    if missing:
        print(
            f"DRIFT DETECTED — {len(missing)} file(s) mentioned in CLAUDE.md but missing:",
            file=sys.stderr,
        )
        for p in missing:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"OK — {len(paths)} file references checked, all exist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
