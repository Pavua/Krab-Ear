#!/usr/bin/env python3
"""audit_path_containment.py — root-cause guard for the path-containment
``startswith`` regression class (timeline #1660 / recording_core / history_import).

ROOT CAUSE (recurring REAL vulnerability): a path-containment / "is this resolved
path inside an allowed root?" check written with a **string prefix match**
instead of a **path-component containment** check::

    # BUG — classic prefix-escape: ``/Users/pablito_evil/x`` passes the check
    if any(str(resolved).startswith(str(root)) for root in allowed_roots):
        ...

    # CORRECT — component containment (raises ValueError when outside):
    try:
        resolved.relative_to(root)        # or: resolved.is_relative_to(root)
    except ValueError:
        ...  # outside

The prefix form lets ``output_dir=/home/user_evil/sub`` pass a check that meant
to confine paths under ``/home/user`` — a directory-traversal / sandbox-escape
bug.  It keeps regressing (PR #1660 timeline export, plus sibling fixes in
``recording_core_service.transcribe_paths`` and the still-live
``history_service.handle_import_history_ndjson``) because nothing flags it at
commit time.  This AST scanner is that flag.

WHAT IT FLAGS (and, deliberately, what it does NOT)
---------------------------------------------------
For every ``X.startswith(Y)`` call in ``KrabEar/**/*.py`` (skipping tests/,
.venv*, _legacy) the scanner asks: *is this a filesystem path-containment check?*
It answers with data-lineage heuristics over ``X`` (receiver) and ``Y`` (arg):

  * **A "path expression"** is one derived from a filesystem operation:
    ``X.resolve()`` / ``.expanduser()`` / ``.absolute()`` / ``.parent``,
    ``Path(...)``, ``os.path.*(...)``, ``tempfile.gettempdir()`` /
    ``Path.home()``, ``str(<path-expr>)``, or a bare reference to a variable
    that was assigned a path expression, or to a **filesystem-root-named**
    variable (``allowed_roots``, ``data_dir``, ``resolved``, ``vault``,
    ``cache_root``, ``tmp_dir`` ...).

  * **FLAG (dangerous)** only when the call is a genuine *containment* test —
    i.e. BOTH a path-expression receiver AND a path-expression argument
    (``str(resolved).startswith(str(root))``), OR a path-expression receiver
    compared against an explicitly **root-named** argument
    (``resolved.startswith(root)`` where the arg comes from / iterates
    ``allowed_roots`` / a ``*_root`` / ``data_dir`` name).  This is exactly the
    shape that should be ``relative_to`` / ``is_relative_to``.

  * **DO NOT FLAG** benign string-prefix checks, which are the overwhelming
    majority:
      - short literal markers / schemes:  ``text.startswith("http")``,
        ``auth.startswith("Bearer ")``, ``name.startswith("_")``,
        ``entry.name.startswith("models--")``, ``d.name.startswith("auto_backup_")``.
        (A string-literal argument is NEVER a containment root → never flagged.)
      - URL-route / non-filesystem prefixes:  ``req.path.startswith(prefix)``
        in API version routing — ``path``/``prefix`` are URL tokens, neither
        side has filesystem lineage, so not flagged.
      - timestamp / token / command prefixes:  ``item.ts.startswith(today_iso)``,
        ``c.startswith(text)`` over REPL commands, error-code prefixes.

The guiding principle: **a string-literal argument is benign by definition** (a
filesystem root is never a short literal in this codebase — it is always a
resolved ``Path``), and a non-literal argument is dangerous only when it carries
filesystem lineage.  This keeps false positives at zero on the current tree
while catching the exact bug class.

Usage::

    python3 scripts/audit_path_containment.py                 # report, exit 0
    python3 scripts/audit_path_containment.py --fail-on-found # exit 1 if any
    python3 scripts/audit_path_containment.py --json          # machine-readable
    python3 scripts/audit_path_containment.py --selftest      # inline asserts

Allowlist: ``scripts/path_containment_allowlist.txt`` — one ``file:line`` or
``file:symbol`` entry per line (``#`` comments) for reviewed-safe exceptions.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
ALLOWLIST_FILE = REPO_ROOT / "scripts" / "path_containment_allowlist.txt"

# Directories never scanned (vendored / generated / archived / tests).  Tests are
# excluded because a string-prefix check in a test fixture is not a production
# vulnerability — the guard protects shipped code paths.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".venv_krab_ear",
        ".venv",
        "venv",
        ".venv_gigaam",
        ".venv_vl",
        "tests",
        "test",
        "__pycache__",
        ".git",
        "dist",
        "build",
        "node_modules",
        "_legacy_tkinter_archive_2026-02-11",
    }
)

# ---------------------------------------------------------------------------
# Lineage vocabulary
# ---------------------------------------------------------------------------
# Method calls that PRODUCE a filesystem path object (``x.resolve()`` etc.).
# The receiver of such a call is a path → the whole expression is path-derived.
_PATH_PRODUCING_METHODS: frozenset[str] = frozenset(
    {
        "resolve",
        "expanduser",
        "absolute",
        "joinpath",
        "with_suffix",
        "with_name",
        "relative_to",
        "home",            # Path.home()
        "cwd",             # Path.cwd()
        "gettempdir",      # tempfile.gettempdir()
        "realpath",        # os.path.realpath()
        "abspath",         # os.path.abspath()
        "normpath",        # os.path.normpath()
        "expandvars",      # os.path.expandvars()
    }
)

# Attribute accesses that, applied to a path, yield ANOTHER path component used
# for containment (``p.parent``).  Note ``.name`` / ``.stem`` are *excluded* on
# purpose: ``entry.name`` is a bare basename string, and
# ``entry.name.startswith("models--")`` is a benign filename-marker filter, not a
# containment check.
_PATH_COMPONENT_ATTRS: frozenset[str] = frozenset({"parent", "parents"})

# Callables whose result is a path object when called as ``Name(...)``.
_PATH_CONSTRUCTORS: frozenset[str] = frozenset({"Path", "PurePath", "PosixPath"})

# os.path.<fn>(...) helpers that build/normalise filesystem paths.
_OSPATH_FUNCS: frozenset[str] = frozenset(
    {
        "join",
        "realpath",
        "abspath",
        "normpath",
        "expanduser",
        "expandvars",
        "dirname",
        "commonpath",
    }
)

# Variable/attribute *names* that strongly denote a FILESYSTEM ROOT (a directory
# a path is confined within).  Used to recognise a path-expression argument even
# when it is a bare name (``resolved.startswith(root)``).  Kept tight on purpose:
# generic ``path`` / ``dir`` / ``prefix`` are NOT here, because URL-routing code
# reuses those names (``req.path.startswith(prefix)``), and admitting them would
# create false positives.  Membership match is on the *trailing identifier* and
# also matched as a token-component (so ``data_dir`` and ``allowed_roots`` and
# ``cache_root`` all hit) — see ``_name_is_root_like``.
_ROOT_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "root",
        "roots",
        "allowed_roots",
        "data_dir",
        "datadir",
        "resolved",
        "home",
        "homedir",
        "vault",
        "tmpdir",
        "tempdir",
        "basedir",
        "rootdir",
    }
)

# Filesystem-root name suffixes matched on a full snake_case identifier
# (``cache_root`` / ``vault_roots``).  ``_dir`` alone is deliberately NOT a
# suffix here — too weak (URL/category code uses ``export_dir`` etc.); a
# ``*_dir`` name is only treated as root-like when its component set hits
# ``_ROOT_NAME_TOKENS`` (e.g. ``data_dir`` via the explicit token entry).
_ROOT_NAME_SUFFIXES: tuple[str, ...] = ("_root", "_roots")


@dataclass
class Finding:
    """A flagged ``startswith`` path-containment site."""

    location: str       # "relative/path.py:LINE"
    code: str           # the source line, stripped
    reason: str         # why it was flagged
    symbol: str         # enclosing function/class qualname (for allowlisting)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _name_tokens(name: str) -> set[str]:
    """snake_case components of an identifier, lowercased."""
    return {tok for tok in name.lower().split("_") if tok}


def _name_is_root_like(name: str | None) -> bool:
    """True when ``name`` denotes a filesystem ROOT (containment boundary).

    Hits on:
      * exact match in ``_ROOT_NAME_TOKENS`` (``data_dir``, ``allowed_roots``),
      * any ``_``-split component in ``_ROOT_NAME_TOKENS`` (``allowed_roots`` →
        component ``roots``; ``cache_roots`` → ``roots``),
      * names ending in ``_root`` / ``_roots`` (``cache_root``, ``vault_roots``).

    Deliberately does NOT hit bare ``path`` / ``dir`` / ``prefix`` — those appear
    in URL-routing code and would cause false positives.
    """
    if not name:
        return False
    low = name.lower()
    if low in _ROOT_NAME_TOKENS:
        return True
    if any(low.endswith(suf) for suf in _ROOT_NAME_SUFFIXES):
        return True
    toks = _name_tokens(name)
    return bool(toks & _ROOT_NAME_TOKENS)


def _unwrap_str(node: ast.AST) -> ast.AST:
    """Peel ``str(<x>)`` wrappers so name-based lineage checks see the inner
    expression.  ``str(resolved)`` → ``resolved`` (a Name); a non-``str`` node is
    returned unchanged.  Path-containment checks almost always stringify both
    operands (``str(resolved).startswith(str(root))``), so without this the
    *name* of the path/root is hidden behind the ``str(...)`` Call node."""
    cur = node
    while (
        isinstance(cur, ast.Call)
        and isinstance(cur.func, ast.Name)
        and cur.func.id == "str"
        and cur.args
    ):
        cur = cur.args[0]
    return cur


def _trailing_name(node: ast.AST) -> str | None:
    """Trailing identifier of a (possibly ``str()``-wrapped) Name / Attribute
    node (``self.data_dir`` → ``data_dir``; ``str(root)`` → ``root``)."""
    node = _unwrap_str(node)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Path-lineage classifier
# ---------------------------------------------------------------------------
class _Lineage:
    """Classifies whether an expression node is a FILESYSTEM PATH expression.

    Tracks which local variables were *assigned* a path expression (so a later
    bare ``resolved`` / ``out_dir`` reference is known to be path-derived even
    though the name itself is not in the root vocabulary).  Module-flat for
    simplicity: a path-typed name staying path-typed is the conservative call,
    and the dangerous-shape gate (both/root-named) keeps this from over-flagging.
    """

    def __init__(self) -> None:
        self._path_vars: set[str] = set()

    # --- public API --------------------------------------------------------
    def learn_assignments(self, tree: ast.AST) -> None:
        """Pre-pass: record ``name = <path-expr>`` assignments + path-typed loop
        targets module-wide."""
        for node in ast.walk(tree):
            target: ast.AST | None = None
            value: ast.AST | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target = node.target
                value = node.value
            if not isinstance(target, ast.Name) or value is None:
                continue
            if self.is_path_expr(value):
                self._path_vars.add(target.id)

        # ``for root in allowed_roots:`` / comprehension generators → loop var is
        # a path when the iterable is root-named or path-typed.
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                self._learn_iter_target(node.target, node.iter)
            for comp in getattr(node, "generators", []) or []:
                if isinstance(comp, ast.comprehension):
                    self._learn_iter_target(comp.target, comp.iter)

    def _learn_iter_target(self, target: ast.AST, iterable: ast.AST) -> None:
        """``for <target> in <iterable>``: if the iterable is root-named or
        path-typed, the loop variable is a path expression."""
        if not isinstance(target, ast.Name):
            return
        it_name = _trailing_name(iterable)
        if _name_is_root_like(it_name) or (
            it_name is not None and it_name in self._path_vars
        ):
            self._path_vars.add(target.id)

    def is_path_expr(self, node: ast.AST) -> bool:
        """True when ``node`` evaluates to / wraps a filesystem path object."""
        # str(<path-expr>) — unwrap and recurse.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and node.args
        ):
            return self.is_path_expr(node.args[0])

        # Path(...) / PurePath(...) constructors.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _PATH_CONSTRUCTORS:
                return True

        # <recv>.resolve()/.expanduser()/... and os.path.*(...) / tempfile.*().
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _PATH_PRODUCING_METHODS:
                return True
            # os.path.<fn>(...) — only the path-building helpers.
            if attr in _OSPATH_FUNCS and self._is_ospath_or_path_recv(node.func.value):
                return True

        # <path>.parent / <path>.parents  (path component that stays a path).
        if isinstance(node, ast.Attribute) and node.attr in _PATH_COMPONENT_ATTRS:
            return self.is_path_expr(node.value)

        # a / b  (pathlib Path division builds a path) — left operand path-typed.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self.is_path_expr(node.left)

        # Bare name previously assigned a path expression.
        name = _trailing_name(node)
        if name is not None and name in self._path_vars:
            return True
        return False

    @staticmethod
    def _is_ospath_or_path_recv(node: ast.AST) -> bool:
        """True when ``node`` is ``os.path`` / ``os`` / ``Path`` / ``tempfile``
        (the receiver namespace for path-building helper functions)."""
        # os.path
        if isinstance(node, ast.Attribute) and node.attr == "path":
            inner = node.value
            return isinstance(inner, ast.Name) and inner.id == "os"
        if isinstance(node, ast.Name):
            return node.id in {"os", "tempfile", "Path"}
        return False

    # --- root-likeness of an *argument* expression -------------------------
    def arg_is_root_like(self, node: ast.AST) -> bool:
        """True when the ``startswith`` argument denotes a filesystem ROOT.

        Either it is a path expression (``str(root)`` / ``root.resolve()``) OR a
        bare name from the root vocabulary (``root``, ``data_dir``,
        ``allowed_roots`` member, ``cache_root``).  A string literal is NEVER
        root-like (returns False) — that is the primary false-positive guard.
        """
        if isinstance(node, ast.Constant):
            return False  # literal marker/scheme — benign by construction
        if self.is_path_expr(node):
            return True
        return _name_is_root_like(_trailing_name(node))


# ---------------------------------------------------------------------------
# Per-file scan
# ---------------------------------------------------------------------------
def _enclosing_symbol(stack: list[ast.AST]) -> str:
    """Build a ``Class.method`` style qualname from the visitor's parent stack."""
    parts: list[str] = []
    for node in stack:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(node.name)
    return ".".join(parts) if parts else "<module>"


def scan_file(path: Path) -> list[Finding]:
    """Scan one module; return dangerous path-containment ``startswith`` sites."""
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except (OSError, SyntaxError) as exc:  # pragma: no cover - defensive
        print(f"[WARN] cannot parse {path}: {exc}", file=sys.stderr)
        return []

    src_lines = src.splitlines()
    lineage = _Lineage()
    lineage.learn_assignments(tree)
    rel = _rel(path)
    findings: list[Finding] = []

    # Walk with a parent stack so each finding records its enclosing symbol.
    def _visit(node: ast.AST, stack: list[ast.AST]) -> None:
        child_stack = stack
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            child_stack = stack + [node]

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and len(node.args) >= 1
        ):
            finding = _classify_call(node, lineage, src_lines, rel, stack)
            if finding is not None:
                findings.append(finding)

        for child in ast.iter_child_nodes(node):
            _visit(child, child_stack)

    _visit(tree, [])
    return findings


def _classify_call(
    node: ast.Call,
    lineage: _Lineage,
    src_lines: list[str],
    rel: str,
    stack: list[ast.AST],
) -> Finding | None:
    """Decide whether a single ``X.startswith(Y)`` is a dangerous containment
    check.  Returns a ``Finding`` to flag, else ``None``.

    Two dangerous shapes (and ONLY these):

      (1) BOTH sides path-derived:  ``str(resolved).startswith(str(root))``.
          The canonical bug — both operands are filesystem paths.

      (2) Path receiver vs ROOT-named argument:  ``resolved.startswith(root)``
          / ``str(p).startswith(allowed_root)`` — receiver is path-derived and
          the argument names a filesystem root.

    A string-literal argument short-circuits to benign (handled below), which
    rejects every marker/scheme/URL-prefix case.
    """
    assert isinstance(node.func, ast.Attribute)
    receiver = node.func.value
    arg = node.args[0]

    # Literal argument → never a containment root → benign, regardless of recv.
    if isinstance(arg, ast.Constant):
        return None

    # A side counts as "path-ish" when it is a filesystem path expression
    # (``Path(x).resolve()`` / ``str(<path>)``) OR its (str-unwrapped) name is in
    # the filesystem-root vocabulary (``resolved`` / ``data_dir`` / a ``*_root``).
    # The name signal matters because the receiver of the canonical bug,
    # ``str(resolved).startswith(str(root))``, is a free var named ``resolved``
    # with no in-scope assignment — only its NAME betrays it as a path.
    recv_is_path = lineage.is_path_expr(receiver)
    recv_root_name = _name_is_root_like(_trailing_name(receiver))
    arg_is_path = lineage.is_path_expr(arg)
    arg_root_like = lineage.arg_is_root_like(arg)

    recv_pathish = recv_is_path or recv_root_name
    arg_pathish = arg_is_path or arg_root_like

    # Flag a containment check only when BOTH sides are path-ish (a genuine
    # "is this path under that root?" test).  This is the exact shape that must
    # be ``relative_to`` / ``is_relative_to``.  Requiring BOTH sides (not just
    # one) is what keeps benign single-sided cases — ``req.path.startswith(prefix)``
    # (neither side path-ish), ``entry.name.startswith("models--")`` (literal
    # arg already short-circuited) — from being flagged.
    if not (recv_pathish and arg_pathish):
        return None

    if recv_is_path and arg_is_path:
        reason = (
            "path-containment via string prefix: BOTH receiver and argument are "
            "filesystem path expressions — use Path.relative_to / is_relative_to"
        )
    else:
        reason = (
            "path-containment via string prefix: a filesystem path/root is "
            "compared by string prefix (receiver + argument both path-like) — "
            "use Path.relative_to / is_relative_to"
        )

    line_idx = node.lineno - 1
    code = src_lines[line_idx].strip() if 0 <= line_idx < len(src_lines) else "<?>"
    return Finding(
        location=f"{rel}:{node.lineno}",
        code=code,
        reason=reason,
        symbol=_enclosing_symbol(stack),
    )


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def iter_python_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            if fname.startswith("test_") or fname.endswith("_test.py"):
                continue
            out.append(Path(dirpath) / fname)
    return sorted(out)


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def load_allowlist() -> set[str]:
    """Entries are ``file:line`` or ``file:symbol`` (``#`` comments allowed)."""
    if not ALLOWLIST_FILE.exists():
        return set()
    out: set[str] = set()
    for raw in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _finding_keys(f: Finding) -> set[str]:
    """Allowlist match keys for a finding: ``file:line`` and ``file:symbol``."""
    file_part = f.location.rsplit(":", 1)[0]
    keys = {f.location}  # file:line
    if f.symbol and f.symbol != "<module>":
        keys.add(f"{file_part}:{f.symbol}")
    return keys


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_audit() -> tuple[list[Finding], list[Finding]]:
    """Return (flagged, allowlisted) findings across the KrabEar package."""
    allow = load_allowlist()
    flagged: list[Finding] = []
    allowlisted: list[Finding] = []
    for path in iter_python_files(KRAB_EAR):
        for finding in scan_file(path):
            if _finding_keys(finding) & allow:
                allowlisted.append(finding)
            else:
                flagged.append(finding)
    flagged.sort(key=lambda f: f.location)
    allowlisted.sort(key=lambda f: f.location)
    return flagged, allowlisted


def format_report(flagged: list[Finding], allowlisted: list[Finding]) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("PATH-CONTAINMENT startswith AUDIT (root-cause guard, #1660 class)")
    lines.append("=" * 78)
    lines.append(f"flagged (dangerous) : {len(flagged)}")
    lines.append(f"allowlisted         : {len(allowlisted)}")
    lines.append("")

    if not flagged:
        lines.append(
            "OK — no dangerous startswith-based path-containment checks found."
        )
    else:
        lines.append(
            "DANGEROUS path-containment checks (string prefix instead of "
            "Path.relative_to):"
        )
        lines.append("")
        for f in flagged:
            lines.append(f"  {f.location}  [{f.symbol}]")
            lines.append(f"      code   : {f.code}")
            lines.append(f"      reason : {f.reason}")
            lines.append("")
        lines.append("Fix each by replacing the prefix check with a try/except")
        lines.append("``resolved.relative_to(root)`` (or ``is_relative_to``), or add")
        lines.append(
            f"  {_rel(ALLOWLIST_FILE)}  with a # reason if reviewed safe."
        )

    if allowlisted:
        lines.append("")
        lines.append(f"--- Allowlisted (reviewed safe): {len(allowlisted)} ---")
        for f in allowlisted:
            lines.append(f"  [allow] {f.location}  [{f.symbol}]")

    return "\n".join(lines)


def format_json(flagged: list[Finding], allowlisted: list[Finding]) -> str:
    def _ser(f: Finding) -> dict[str, str]:
        return {
            "location": f.location,
            "code": f.code,
            "reason": f.reason,
            "symbol": f.symbol,
        }

    payload = {
        "flagged_count": len(flagged),
        "allowlisted_count": len(allowlisted),
        "flagged": [_ser(f) for f in flagged],
        "allowlisted": [_ser(f) for f in allowlisted],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Self-test (feeds known-bad / known-good snippets through the classifier)
# ---------------------------------------------------------------------------
def _scan_source(src: str, name: str = "<snippet>") -> list[Finding]:
    """Scan an in-memory source string (used by --selftest)."""
    tree = ast.parse(src, filename=name)
    lineage = _Lineage()
    lineage.learn_assignments(tree)
    src_lines = src.splitlines()
    findings: list[Finding] = []

    def _visit(node: ast.AST, stack: list[ast.AST]) -> None:
        child_stack = stack
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child_stack = stack + [node]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and len(node.args) >= 1
        ):
            f = _classify_call(node, lineage, src_lines, name, stack)
            if f is not None:
                findings.append(f)
        for child in ast.iter_child_nodes(node):
            _visit(child, child_stack)

    _visit(tree, [])
    return findings


def selftest() -> int:
    """Inline asserts: known-bad must flag, known-good must not.  Mirrors the
    real-world shapes seen in this codebase."""
    bad_cases = [
        # the canonical bug (history_service.handle_import_history_ndjson)
        "if any(str(resolved).startswith(str(root)) for root in allowed_roots):\n    pass",
        # receiver path-derived, root-named bare argument
        "p = Path(x).resolve()\nif p.startswith(allowed_root):\n    pass",
        # both sides Path(...) wrapped
        "if str(Path(a)).startswith(str(Path(b))):\n    pass",
        # data_dir as the containment root
        "r = Path(out).expanduser().resolve()\nif str(r).startswith(str(self.data_dir)):\n    pass",
    ]
    good_cases = [
        # short literal markers / schemes
        'if text.startswith("http"):\n    pass',
        'if auth_header.startswith("Bearer "):\n    pass',
        'if name.startswith("_"):\n    pass',
        # basename-marker filter on a Path's .name (benign)
        'for d in base.iterdir():\n    if d.name.startswith("auto_backup_"):\n        pass',
        'if entry.name.startswith("models--"):\n    pass',
        # URL-route prefix: path/prefix names but no filesystem lineage
        'path = req.path or ""\nprefix = "/v1/"\nif path.startswith(prefix):\n    pass',
        # timestamp / command / origin prefixes
        "if item.ts.startswith(today_iso):\n    pass",
        "if origin.startswith(p):\n    pass",
        # the CORRECT fixed form has no startswith at all -> nothing to flag
        "try:\n    resolved.relative_to(root)\nexcept ValueError:\n    pass",
    ]

    failures: list[str] = []
    for i, src in enumerate(bad_cases):
        hits = _scan_source(src, f"bad[{i}]")
        if not hits:
            failures.append(f"BAD case {i} NOT flagged (false negative):\n{src}")
    for i, src in enumerate(good_cases):
        hits = _scan_source(src, f"good[{i}]")
        if hits:
            failures.append(
                f"GOOD case {i} WAS flagged (false positive): "
                f"{[h.code for h in hits]}\n{src}"
            )

    if failures:
        print("SELFTEST FAILED:\n")
        for msg in failures:
            print(f"  - {msg}\n")
        return 1
    print(
        f"SELFTEST OK: {len(bad_cases)} known-bad flagged, "
        f"{len(good_cases)} known-good clean."
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="exit non-zero if any non-allowlisted dangerous site remains",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the text report",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run inline known-bad / known-good classifier asserts and exit",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    flagged, allowlisted = run_audit()

    if args.json:
        print(format_json(flagged, allowlisted))
    else:
        print(format_report(flagged, allowlisted))

    if args.fail_on_found and flagged:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
