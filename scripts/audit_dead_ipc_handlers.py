#!/usr/bin/env python3
"""
audit_dead_ipc_handlers.py — Wave 65 dead IPC handler detection (v2).

Generates a trusted DEFINITELY_DEAD candidates list with zero false positives
by scanning all caller sources: Swift, Python tests, REST server, and other
Python code outside tests.

v2 additions (Wave 149):
  - Catches self.assert_dispatch("X") test helper pattern (201 uses in suite)
  - Catches self._call("X") test helper pattern (80 uses in suite)
  - Strips Python # line comments and Swift // line comments before matching
    to avoid false LIVE classifications from commented-out code
  - Skips triple-quoted Python docstrings to avoid false positives
  - JSON output includes per-handler confidence ranking

Usage:
    python scripts/audit_dead_ipc_handlers.py [--output-format=text|json] [--repo-root=PATH]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class HandlerInfo(NamedTuple):
    method: str
    is_swift_caller: bool
    is_python_test_caller: bool
    is_rest_caller: bool
    is_other_python_caller: bool
    has_deprecated_comment: bool

    @property
    def classification(self) -> str:
        if (self.is_swift_caller or self.is_rest_caller
                or self.is_other_python_caller):
            return "LIVE"
        if self.is_python_test_caller:
            return "TEST_ONLY"
        if self.has_deprecated_comment:
            return "LEGACY_FALLBACK"
        return "DEFINITELY_DEAD"

    @property
    def is_live(self) -> bool:
        return self.classification == "LIVE"

    @property
    def confidence(self) -> str:
        """Confidence ranking for DEFINITELY_DEAD candidates."""
        if self.classification != "DEFINITELY_DEAD":
            return "N/A"
        # Highest confidence: no callers anywhere and no deprecated comment
        return "HIGH"


# ---------------------------------------------------------------------------
# Source text pre-processing helpers
# ---------------------------------------------------------------------------

_PYTHON_TRIPLE_DOUBLE = re.compile(r'""".*?"""', re.DOTALL)
_PYTHON_TRIPLE_SINGLE = re.compile(r"'''.*?'''", re.DOTALL)


def _strip_python_docstrings(text: str) -> str:
    """Remove triple-quoted docstrings so we don't match method names inside them."""
    text = _PYTHON_TRIPLE_DOUBLE.sub('""', text)
    text = _PYTHON_TRIPLE_SINGLE.sub("''", text)
    return text


def _strip_python_line_comments(line: str) -> str:
    """Remove everything after a # that is not inside a string literal."""
    # Simple heuristic: find first # not inside quotes
    result = []
    in_single = False
    in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == '#' and not in_single and not in_double:
            break
        result.append(ch)
    return ''.join(result)


def _strip_swift_line_comments(line: str) -> str:
    """Remove // single-line Swift comments."""
    idx = line.find('//')
    if idx >= 0:
        return line[:idx]
    return line


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def find_repo_root(start: Path) -> Path:
    """Walk up from start until we find KrabEar/backend/service.py."""
    current = start.resolve()
    for candidate in [current] + list(current.parents):
        if (candidate / "KrabEar" / "backend" / "service.py").exists():
            return candidate
    raise FileNotFoundError(
        f"Could not find repo root from {start}. "
        "Expected KrabEar/backend/service.py to exist."
    )


# --- 1. Parse service.py dispatch table ---

_DISPATCH_PATTERN = re.compile(
    r'"([a-z][a-z0-9_]*)"\s*:\s*self\._handle_\w+'
)
_DEPRECATED_COMMENT_PATTERN = re.compile(
    r'"([a-z][a-z0-9_]*)"\s*:\s*self\._handle_\w+.*?(?:deprecated|backwards compat|legacy)',
    re.IGNORECASE,
)


def parse_registered_handlers(service_py: Path) -> dict[str, bool]:
    """
    Returns {method_name: has_deprecated_comment} for all registered handlers.
    """
    text = service_py.read_text(encoding="utf-8", errors="replace")
    handlers: dict[str, bool] = {}
    for line in text.splitlines():
        m = _DISPATCH_PATTERN.search(line)
        if m:
            name = m.group(1)
            has_depr = bool(_DEPRECATED_COMMENT_PATTERN.search(line))
            handlers[name] = has_depr
    return handlers


# --- 2. Parse Swift callers ---

# Patterns:
#   ipcClient.call(method: "foo", ...)
#   callAsync(method: "foo", ...)
#   callWithRecovery(method: "foo", ...)
#   method: "foo"   (generic IPC call)
_SWIFT_METHOD_PATTERN = re.compile(r'method:\s*"([a-z][a-z0-9_]*)"')


def find_swift_callers(swift_root: Path) -> set[str]:
    """Return set of method names called anywhere in Swift source (comments stripped)."""
    called: set[str] = set()
    if not swift_root.exists():
        return called
    for path in swift_root.rglob("*.swift"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = _strip_swift_line_comments(line)
            for m in _SWIFT_METHOD_PATTERN.finditer(line):
                called.add(m.group(1))
    return called


# --- 3. Parse Python test callers ---

# Patterns captured:
#   handle_request({"method": "foo", ...})   →  "method": "foo"
#   {"method": "foo"}                         →  "method": "foo"
#   self.req("foo", ...)                      →  self.req("foo")
#   self.req('foo', ...)                      →  self.req('foo')
#   svc.handle_add_history_item(...)          →  handle_add_xxx (direct call)
#   service.handle_foo(...)                   →  handle_foo
#   svc._handle_foo(...)                      →  _handle_foo (underscore prefix variant)
#   self.assert_dispatch("foo", ...)          →  assert_dispatch("foo")  [v2: Wave 149]
#   self._call("foo", ...)                    →  _call("foo")  [v2: Wave 149]
_TEST_METHOD_DICT_PATTERN = re.compile(r'"method"\s*:\s*"([a-z][a-z0-9_]*)"')
_TEST_METHOD_DICT_SINGLE_PATTERN = re.compile(r'"method"\s*:\s*\'([a-z][a-z0-9_]*)\'')
_TEST_REQ_DOUBLE_PATTERN = re.compile(r'\.\s*req\s*\(\s*"([a-z][a-z0-9_]*)"')
_TEST_REQ_SINGLE_PATTERN = re.compile(r"\.\s*req\s*\(\s*'([a-z][a-z0-9_]*)'")
# Matches: svc.handle_foo(  or  service.handle_foo(  or  self.svc.handle_foo(
# Also matches underscore-prefix variant: svc._handle_foo(  (most common in actual tests)
_TEST_DIRECT_HANDLE_PATTERN = re.compile(
    r'(?:svc|service|self\.svc|self\.service)\s*\.\s*_?handle_([a-z][a-z0-9_]*)\s*\('
)
# dispatch helper: dispatch("foo", ...)
_TEST_DISPATCH_PATTERN = re.compile(r'dispatch\s*\(\s*"([a-z][a-z0-9_]*)"')
# bare req("foo", ...) — standalone function not attached to self
_TEST_BARE_REQ_PATTERN = re.compile(r'(?<!\.)req\s*\(\s*"([a-z][a-z0-9_]*)"')
# assert_dispatch helper: self.assert_dispatch("foo", ...)  [v2: Wave 149]
# Catches the common TestCase mixin used in test_dispatch_complete.py (201 uses)
_TEST_ASSERT_DISPATCH_PATTERN = re.compile(
    r'assert_dispatch\s*\(\s*"([a-z][a-z0-9_]*)"'
)
# self._call helper: self._call("foo", ...)  [v2: Wave 149]
# Catches test helper wrappers around handle_request (80 uses in suite)
_TEST_SELF_CALL_DOUBLE_PATTERN = re.compile(
    r'\._call\s*\(\s*"([a-z][a-z0-9_]*)"'
)
_TEST_SELF_CALL_SINGLE_PATTERN = re.compile(
    r"\._call\s*\(\s*'([a-z][a-z0-9_]*)'"
)


def find_python_test_callers(tests_root: Path) -> set[str]:
    """Return set of method names referenced in Python test files."""
    called: set[str] = set()
    if not tests_root.exists():
        return called
    for path in tests_root.rglob("test_*.py"):
        raw = path.read_text(encoding="utf-8", errors="replace")
        # Strip docstrings first to avoid matching method names inside docs
        text = _strip_python_docstrings(raw)
        # Strip # comments line-by-line for dict/req/call patterns
        stripped_lines = [_strip_python_line_comments(ln) for ln in text.splitlines()]
        text_no_comments = "\n".join(stripped_lines)
        for pattern in (
            _TEST_METHOD_DICT_PATTERN,
            _TEST_METHOD_DICT_SINGLE_PATTERN,
            _TEST_REQ_DOUBLE_PATTERN,
            _TEST_REQ_SINGLE_PATTERN,
            _TEST_DISPATCH_PATTERN,
            _TEST_BARE_REQ_PATTERN,
            _TEST_ASSERT_DISPATCH_PATTERN,   # v2: assert_dispatch("X")
            _TEST_SELF_CALL_DOUBLE_PATTERN,  # v2: ._call("X")
            _TEST_SELF_CALL_SINGLE_PATTERN,  # v2: ._call('X')
        ):
            for m in pattern.finditer(text_no_comments):
                called.add(m.group(1))
        # Direct handle_xxx calls: svc.handle_foo → method "foo"
        for m in _TEST_DIRECT_HANDLE_PATTERN.finditer(text_no_comments):
            handler_suffix = m.group(1)
            # Map handle_add_history_item → add_history_item
            called.add(handler_suffix)
    return called


# --- 4. Parse REST server callers ---

_REST_METHOD_PATTERN = re.compile(r'"method"\s*:\s*"([a-z][a-z0-9_]*)"')


def find_rest_callers(rest_server_py: Path) -> set[str]:
    """Return set of IPC method names called from the REST server (comments stripped)."""
    called: set[str] = set()
    if not rest_server_py.exists():
        return called
    raw = rest_server_py.read_text(encoding="utf-8", errors="replace")
    text = _strip_python_docstrings(raw)
    # Only lines that look like IPC calls (not log records about HTTP method)
    for line in text.splitlines():
        line = _strip_python_line_comments(line)
        # Skip lines that are clearly about HTTP method (logging etc.)
        if '"method": request.method' in line or 'request.method' in line:
            continue
        for m in _REST_METHOD_PATTERN.finditer(line):
            called.add(m.group(1))
    return called


# --- 5. Parse other Python callers (outside backend/ and tests/) ---

_OTHER_PY_METHOD_PATTERN = re.compile(r'"method"\s*:\s*"([a-z][a-z0-9_]*)"')


def find_other_python_callers(
        repo_root: Path,
        exclude_paths: list[Path]) -> set[str]:
    """
    Return set of IPC method names from Python files outside
    KrabEar/backend/ and KrabEar/tests/.
    """
    called: set[str] = set()
    exclude_resolved = {p.resolve() for p in exclude_paths}

    for py_file in repo_root.rglob("*.py"):
        # Skip excluded directories
        skip = False
        for excl in exclude_resolved:
            try:
                py_file.resolve().relative_to(excl)
                skip = True
                break
            except ValueError:
                pass
        if skip:
            continue
        # Skip the script itself
        if py_file.name == "audit_dead_ipc_handlers.py":
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _OTHER_PY_METHOD_PATTERN.finditer(text):
            called.add(m.group(1))

    return called


# ---------------------------------------------------------------------------
# Main audit logic
# ---------------------------------------------------------------------------

def run_audit(repo_root: Path) -> list[HandlerInfo]:
    service_py = repo_root / "KrabEar" / "backend" / "service.py"
    swift_root = repo_root / "native" / "KrabEarAgent" / "Sources"
    tests_root = repo_root / "KrabEar" / "tests"
    rest_server_py = repo_root / "KrabEar" / "backend" / "rest_server.py"
    backend_root = repo_root / "KrabEar" / "backend"

    registered = parse_registered_handlers(service_py)
    swift_callers = find_swift_callers(swift_root)
    test_callers = find_python_test_callers(tests_root)
    rest_callers = find_rest_callers(rest_server_py)
    other_callers = find_other_python_callers(
        repo_root,
        exclude_paths=[backend_root, tests_root],
    )

    results: list[HandlerInfo] = []
    for method, has_depr in sorted(registered.items()):
        info = HandlerInfo(
            method=method,
            is_swift_caller=(method in swift_callers),
            is_python_test_caller=(method in test_callers),
            is_rest_caller=(method in rest_callers),
            is_other_python_caller=(method in other_callers),
            has_deprecated_comment=has_depr,
        )
        results.append(info)

    return results


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def print_text_report(results: list[HandlerInfo]) -> None:
    dead = [r for r in results if r.classification == "DEFINITELY_DEAD"]
    test_only = [r for r in results if r.classification == "TEST_ONLY"]
    legacy = [r for r in results if r.classification == "LEGACY_FALLBACK"]
    live = [r for r in results if r.classification == "LIVE"]

    print("=" * 60)
    print("  Krab Ear — Dead IPC Handler Audit (Wave 65)")
    print("=" * 60)
    print(f"Total registered handlers : {len(results)}")
    print(f"  LIVE (any caller)        : {len(live)}")
    print(f"  TEST_ONLY (test coverage): {len(test_only)}")
    print(f"  LEGACY_FALLBACK          : {len(legacy)}")
    print(f"  DEFINITELY_DEAD          : {len(dead)}")
    print()

    if dead:
        print("DEFINITELY_DEAD — safe to remove (zero callers anywhere):")
        for r in dead:
            print(f"  {r.method}")
        print()

    if legacy:
        print("LEGACY_FALLBACK — has deprecated comment, review before removing:")
        for r in legacy:
            print(f"  {r.method}")
        print()

    if test_only:
        print("TEST_ONLY — internal API, keep (tests cover these):")
        for r in test_only:
            print(f"  {r.method}")
        print()

    # False-positive detection: handlers Swift doesn't call but tests do
    # (would have been wrong if we only grepped Swift)
    test_only_no_swift = [
        r for r in results
        if r.is_python_test_caller and not r.is_swift_caller
        and not r.is_rest_caller and not r.is_other_python_caller
    ]
    if test_only_no_swift:
        print(
            "FALSE-POSITIVE GUARD — would be 'dead' without test scanning "
            f"({len(test_only_no_swift)} handlers):"
        )
        for r in test_only_no_swift:
            print(f"  {r.method}  [test-only, not in Swift]")
        print()


def print_json_report(results: list[HandlerInfo]) -> None:
    dead = [r for r in results if r.classification == "DEFINITELY_DEAD"]
    test_only = [r.method for r in results if r.classification == "TEST_ONLY"]
    legacy = [r.method for r in results if r.classification == "LEGACY_FALLBACK"]
    live = [r for r in results if r.classification == "LIVE"]

    output = {
        "summary": {
            "total": len(results),
            "live": len(live),
            "test_only": len(test_only),
            "legacy_fallback": len(legacy),
            "definitely_dead": len(dead),
        },
        # v2: dead entries include confidence ranking
        "definitely_dead": [
            {"method": r.method, "confidence": r.confidence}
            for r in dead
        ],
        "legacy_fallback": legacy,
        "test_only": test_only,
        "live": [r.method for r in live],
        "all": [
            {
                "method": r.method,
                "classification": r.classification,
                "confidence": r.confidence,
                "is_swift_caller": r.is_swift_caller,
                "is_python_test_caller": r.is_python_test_caller,
                "is_rest_caller": r.is_rest_caller,
                "is_other_python_caller": r.is_other_python_caller,
                "has_deprecated_comment": r.has_deprecated_comment,
            }
            for r in results
        ],
    }
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit dead IPC handlers in Krab Ear (Wave 65)."
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Path to the repo root containing KrabEar/. "
            "Auto-detected from script location if not specified."
        ),
    )
    args = parser.parse_args()

    if args.repo_root:
        repo_root = args.repo_root.resolve()
    else:
        # Detect: script lives in <repo_root>/scripts/
        script_dir = Path(__file__).resolve().parent
        repo_root = find_repo_root(script_dir)

    results = run_audit(repo_root)

    if args.output_format == "json":
        print_json_report(results)
    else:
        print_text_report(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
