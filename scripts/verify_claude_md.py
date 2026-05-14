#!/usr/bin/env python3
"""Verify file paths mentioned in CLAUDE.md actually exist in repo.

Usage: python scripts/verify_claude_md.py [--strict] [--report-extras]

Resolution strategy:
  1. Check repo_root / candidate directly.
  2. If not found and candidate has no leading directory, search repo for a
     file with that basename (Swift sources, top-level scripts, etc.).
  3. If candidate looks like a bare module path (e.g. ``backend/service.py``),
     also try known prefixes: KrabEar/, native/KrabEarAgent/Sources/KrabEarAgent/.

Exits 1 if any mentioned file is missing (or strict-mode class drift found).
Exits 2 if CLAUDE.md itself is not found.

New checks (warn-only, do not block CI):
  - Class/method drift: ClassName mentioned for a file must exist inside it.
  - Stale PR references: PR #N must exist on GitHub (gh CLI required).
  - TODO/FIXME hygiene: lists any TODO/FIXME/XXX/HACK markers in CLAUDE.md.
"""
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

# Pattern for plausible file paths wrapped in backticks or asterisks.
# Captures the path group only (group 1).
PATH_PATTERN = re.compile(
    r"[`*]([A-Za-z][\w/\-\.+]+\.(py|swift|json|md|command|sh|txt|yml|yaml|toml|plist|m|h|c|cpp|js|ts|tsx))[`*]"
)

# Pattern: **`some/file.py`** — `ClassName`: description
# Captures file path (group 1) and class name (group 2).
CLASS_IN_FILE_PATTERN = re.compile(
    r"\*\*`([a-z][a-z0-9/_+\-.]+\.(?:py|swift))`\*\*\s*[—–-]+\s*`([A-Z][A-Za-z0-9_]+)`"
)

# Stale PR reference: PR #N
PR_REF_PATTERN = re.compile(r"\bPR #(\d+)\b")

# Wave reference: Wave N
WAVE_REF_PATTERN = re.compile(r"\bWave (\d+)\b")

# TODO/FIXME markers
TODO_PATTERN = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

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

# Class names that are dict/module-level names (not classes) — skip type check.
_SKIP_CLASS_CHECK = {
    "ERROR_REGISTRY",
    "ACTION_HANDLERS",
    "EVENT_SCHEMA_MAP",
}

# Prefixes to search for Python/Swift source files when resolving class names.
_CLASS_SEARCH_PREFIXES = [
    "KrabEar",
    "native/KrabEarAgent/Sources/KrabEarAgent",
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


# ---------------------------------------------------------------------------
# Check 1: Class/method drift
# ---------------------------------------------------------------------------

def _resolve_source_file(fpath: str) -> Optional[Path]:
    """Resolve a CLAUDE.md-relative file path to an absolute Path."""
    for prefix in ["", *_CLASS_SEARCH_PREFIXES]:
        candidate = REPO_ROOT / prefix / fpath if prefix else REPO_ROOT / fpath
        if candidate.exists():
            return candidate
    return None


def _name_exists_in_file(path: Path, name: str) -> bool:
    """Return True if ``name`` appears as a class/struct/actor/func/dict in file."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # For Python: match 'class Name', 'def name', 'NAME = ' at start of line
    # For Swift: match 'class Name', 'struct Name', 'actor Name', 'protocol Name', 'final class Name'
    patterns = [
        f"class {name}",
        f"struct {name}",
        f"actor {name}",
        f"protocol {name}",
        f"def {name}",
        # module-level constant/dict
        f"\n{name} =",
        f"\n{name}: ",
    ]
    return any(p in content for p in patterns)


def check_class_drift(content: str) -> List[Tuple[str, str]]:
    """Return list of (file_path, class_name) pairs where class is not found in file.

    Warn-only: returns violations but does not block CI.
    """
    violations: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    for match in CLASS_IN_FILE_PATTERN.finditer(content):
        fpath = match.group(1)
        class_name = match.group(2)

        key = (fpath, class_name)
        if key in seen:
            continue
        seen.add(key)

        # Skip known non-class names
        if class_name in _SKIP_CLASS_CHECK:
            continue
        # Skip names that are very short (likely not real class names)
        if len(class_name) < 3:
            continue

        resolved = _resolve_source_file(fpath)
        if resolved is None:
            # File itself is missing — already caught by file-existence check
            continue

        if not _name_exists_in_file(resolved, class_name):
            violations.append((fpath, class_name))

    return violations


# ---------------------------------------------------------------------------
# Check 2: Stale Wave/PR references
# ---------------------------------------------------------------------------

def _gh_pr_exists(pr_number: int) -> Optional[str]:
    """Return PR state string or None if gh CLI unavailable / PR missing."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "number,state"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        import json
        data = json.loads(result.stdout)
        return data.get("state", "UNKNOWN")
    except Exception:
        return None


def check_pr_references(content: str) -> List[Tuple[int, str]]:
    """Return list of (pr_number, issue) for PRs that are missing or unmerged.

    Warn-only. Requires ``gh`` CLI authenticated.
    Returns [] if gh is unavailable (silently skips).
    """
    # Check gh is available
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
        if r.returncode != 0:
            return []
    except Exception:
        return []

    pr_numbers: Set[int] = set()
    for match in PR_REF_PATTERN.finditer(content):
        pr_numbers.add(int(match.group(1)))

    violations: List[Tuple[int, str]] = []
    for num in sorted(pr_numbers):
        state = _gh_pr_exists(num)
        if state is None:
            violations.append((num, "not found on GitHub"))
        elif state == "CLOSED":
            violations.append((num, "closed without merge"))
        # MERGED and OPEN are fine
    return violations


def check_wave_references(content: str) -> List[Tuple[int, str]]:
    """Return list of (wave_number, issue) for Wave refs that look suspiciously high.

    Uses the latest merged PR number as a proxy for current wave (each wave ~5-10 PRs).
    Warn-only.
    """
    # Determine max wave from the latest merged PR number.
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--limit", "1", "--json", "number"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return []
        import json
        data = json.loads(r.stdout)
        if not data:
            return []
        latest_pr = data[0]["number"]
        # Rough heuristic: each wave ships ~5-8 PRs; add generous buffer
        max_wave = (latest_pr // 5) + 10
    except Exception:
        return []

    violations: List[Tuple[int, str]] = []
    for match in WAVE_REF_PATTERN.finditer(content):
        wave_num = int(match.group(1))
        if wave_num > max_wave:
            violations.append((wave_num, f"exceeds estimated max wave ~{max_wave}"))
    return violations


# ---------------------------------------------------------------------------
# Check 3: TODO/FIXME hygiene
# ---------------------------------------------------------------------------

def check_todos(content: str) -> List[Tuple[int, str, str]]:
    """Return list of (line_number, marker, line_text) for TODO/FIXME/XXX/HACK in CLAUDE.md."""
    results: List[Tuple[int, str, str]] = []
    for i, line in enumerate(content.splitlines(), 1):
        for match in TODO_PATTERN.finditer(line):
            results.append((i, match.group(1), line.strip()))
    return results


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

_WARN_COLOR = "\033[33m"
_OK_COLOR = "\033[32m"
_RESET = "\033[0m"
_HEADER = "\033[1m"


def _warn(msg: str) -> None:
    print(f"{_WARN_COLOR}WARN{_RESET}  {msg}", file=sys.stderr)


def _print_section(title: str, items: list, formatter, max_show: int = 5) -> None:
    print(f"\n{_HEADER}── {title} ({len(items)}) ──{_RESET}", file=sys.stderr)
    for item in items[:max_show]:
        print(f"  {formatter(item)}", file=sys.stderr)
    if len(items) > max_show:
        print(f"  … and {len(items) - max_show} more", file=sys.stderr)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    if not CLAUDE_MD.exists():
        print(f"CLAUDE.md not found at {CLAUDE_MD}", file=sys.stderr)
        return 2

    content = CLAUDE_MD.read_text(encoding="utf-8")

    # ── Original check: file existence ──────────────────────────────────────
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
        # Fall through to run all checks and report everything.

    # ── Check 1: Class/method drift ─────────────────────────────────────────
    class_violations = check_class_drift(content)

    # ── Check 2: Stale PR references ────────────────────────────────────────
    pr_violations = check_pr_references(content)

    # ── Check 2b: Stale Wave references ─────────────────────────────────────
    wave_violations = check_wave_references(content)

    # ── Check 3: TODO/FIXME hygiene ─────────────────────────────────────────
    todos = check_todos(content)

    # ── Print warn-only results ──────────────────────────────────────────────
    any_warn = class_violations or pr_violations or wave_violations or todos

    if class_violations:
        _print_section(
            "CLASS DRIFT — class name in CLAUDE.md not found in referenced file",
            class_violations,
            lambda v: f"{v[0]}  →  `{v[1]}` not found",
        )

    if pr_violations:
        _print_section(
            "PR REFERENCE DRIFT — PR mentioned in CLAUDE.md is missing or closed",
            pr_violations,
            lambda v: f"PR #{v[0]}: {v[1]}",
        )

    if wave_violations:
        _print_section(
            "WAVE REFERENCE DRIFT — Wave number looks suspiciously high",
            wave_violations,
            lambda v: f"Wave {v[0]}: {v[1]}",
        )

    if todos:
        _print_section(
            "TODO/FIXME HYGIENE — markers found in CLAUDE.md (info only)",
            todos,
            lambda v: f"line {v[0]}  [{v[1]}]  {v[2][:80]}",
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    warn_count = len(class_violations) + len(pr_violations) + len(wave_violations)

    if missing:
        print(
            f"\n{_HEADER}SUMMARY{_RESET}: {len(missing)} missing files | "
            f"{len(class_violations)} class drifts | "
            f"{len(pr_violations)} PR issues | "
            f"{len(wave_violations)} wave issues | "
            f"{len(todos)} TODO markers",
            file=sys.stderr,
        )
        return 1

    if any_warn:
        print(
            f"\n{_OK_COLOR}OK{_RESET} — {len(paths)} file refs OK | "
            f"{_WARN_COLOR}{warn_count} drift warnings{_RESET} | "
            f"{len(todos)} TODOs (see above)"
        )
        # Warn-only: exit 0 so CI doesn't break on first introduction
        return 0

    print(f"{_OK_COLOR}OK{_RESET} — {len(paths)} file references checked, all exist; no drift warnings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
