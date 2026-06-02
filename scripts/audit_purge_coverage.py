#!/usr/bin/env python3
"""audit_purge_coverage.py — W1768 privacy-purge coverage guard.

ROOT-CAUSE invariant for the recurring "X store survives privacy purge" bug
class (W1730 / W1734 / W1749 / W1765 / W1766 / W1767 + wave-12/13).  There has
been NO single check ensuring that every persisted PII/data store the backend
writes under the data dir is wiped by
``backend.history_service.handle_purge_all_data``.  This script is that check.

What it does (static analysis, no imports of the target code):

  1. **Discover** every persisted artifact the backend writes under the data
     dir.  Scans ``KrabEar/backend/*.py`` + ``KrabEar/core/*.py`` for:
       - ``<base_dir> / "name"``      (literal filename / subdir)
       - ``<base_dir> / _CONST``      (module- or class-level string constant)
       - ``<dir>.glob("*.ext")``      (glob-managed file families, e.g. audit_*.ndjson)
       - ``<dir>.mkdir(...)``         (subdirectories created under the data dir)
     ...where ``<base_dir>`` is a data-dir-rooted path (``data_dir`` /
     ``self.data_dir`` / ``self._data_dir`` / ``store.data_dir`` / a ``*_dir``
     attribute that is itself rooted at one of those).  Produces a set of
     canonical store identifiers (filename or ``subdir/`` name) with the
     owning module and the file:line where each store is persisted.

  2. **Extract coverage** — parse ``handle_purge_all_data`` (and the
     collaborator purge methods it calls + ``state_store`` compaction) for every
     name it physically removes / clears: ``unlink`` / ``rmtree`` / ``glob(..)``
     targets, empty-file rewrites, and collaborator ``.clear_all()`` /
     ``.purge_all()`` / ``.delete_all()`` calls.  Each collaborator call is
     resolved to its owning module + method via ``service.py`` wiring
     (``self._X = ClassName(...)``) + imports, and that method body is parsed in
     turn — so the coverage set is fully static and self-maintaining.

  3. **Report the gap** — stores from (1) NOT covered by (2) and NOT in the
     allowlist, grouped by module, each with its file:line.

  4. **Allowlist** — ``scripts/purge_coverage_allowlist.txt`` lists INTENTIONAL
     survivors (one store id per line, ``# reason`` comments allowed): the
     compliance audit trail, app config that is not user PII, model caches,
     logs, lock files, and ID-only resurrection registries.

Usage:
    python3 scripts/audit_purge_coverage.py                 # print report, exit 0
    python3 scripts/audit_purge_coverage.py --fail-on-found # exit 1 if any gap
    python3 scripts/audit_purge_coverage.py --json          # machine-readable

Exit 0 → no non-allowlisted gap (or report-only mode).
Exit 1 → ``--fail-on-found`` and at least one non-allowlisted gap exists.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
BACKEND_DIR = KRAB_EAR / "backend"
CORE_DIR = KRAB_EAR / "core"
HISTORY_SERVICE = BACKEND_DIR / "history_service.py"
SERVICE_PY = BACKEND_DIR / "service.py"
STATE_STORE = BACKEND_DIR / "state_store.py"
ALLOWLIST_FILE = REPO_ROOT / "scripts" / "purge_coverage_allowlist.txt"

# Attribute names that denote a *data-dir-rooted* base path.  A BinOp whose
# left operand is one of these (or a *_dir attribute itself rooted at one) is
# treated as persisting an artifact under the data dir.
DATA_DIR_BASE_NAMES: frozenset[str] = frozenset(
    {
        "data_dir",
        "_data_dir",
    }
)

# Method names on a collaborator that constitute a "purge" (clears/removes a
# store).  Used when walking ``handle_purge_all_data`` for collaborator calls.
PURGE_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "clear",
        "clear_all",
        "purge_all",
        "purge_all_synced_files",
        "delete_all",
        "delete_all_chains",
        "cleanup_for_ids",
        "compact_with_stats",
    }
)

# File extensions that mark a persisted data artifact.
# W1771 GAP-1b: html + srt added — export handlers write report_*.html / srt_*.srt
# under transcripts/, the most PII-dense artefacts.  No plain-literal data-dir file
# uses these extensions, so they only ever surface via the per-extension (f-string /
# glob) sibling-extension detection — adding them here lets that path recognise them.
PERSIST_EXTENSIONS: frozenset[str] = frozenset(
    {"json", "ndjson", "txt", "npy", "key", "csv", "html", "srt"}
)

# Filenames that the discovery scanner must never treat as a real store even if
# they syntactically match (transient probes etc.).
_NEVER_A_STORE: frozenset[str] = frozenset(
    {
        ".startup_write_test",
    }
)


@dataclass
class StoreRef:
    """A persisted artifact discovered under the data dir."""

    store_id: str  # canonical id: "name.ext", "subdir/", or "prefix_*.ext"
    module: str  # owning module stem, e.g. "webhook_manager"
    location: str  # "relative/path.py:LINE"


@dataclass
class AuditResult:
    discovered: dict[str, list[StoreRef]] = field(default_factory=dict)
    covered: set[str] = field(default_factory=set)
    allowlisted: set[str] = field(default_factory=set)
    gaps: list[StoreRef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic AST helpers
# ---------------------------------------------------------------------------
def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse(path: Path) -> ast.Module:
    return ast.parse(_read_source(path), filename=str(path))


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _const_str(node: ast.AST) -> str | None:
    """Return the string value of a string-literal node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def collect_string_constants(tree: ast.Module) -> dict[str, str]:
    """Build ``{NAME: "literal"}`` for module- and class-level simple string
    assignments (``_FOO = "bar.json"`` / ``_FOO: str = "bar"``).

    Only plain ``Name = <str-literal>`` / ``Name: ann = <str-literal>``
    targets are captured; anything computed (BinOp, call, f-string) is skipped
    on purpose — those are resolved at the use site if at all.
    """
    consts: dict[str, str] = {}

    def _visit(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                value = _const_str(stmt.value)
                if value is not None:
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            consts.setdefault(tgt.id, value)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                value = _const_str(stmt.value)
                if value is not None and isinstance(stmt.target, ast.Name):
                    consts.setdefault(stmt.target.id, value)
            # Recurse into class bodies (class-level constants like _FILENAME).
            if isinstance(stmt, ast.ClassDef):
                _visit(stmt.body)

    _visit(tree.body)
    return consts


def _binop_div_chain_root(node: ast.AST) -> ast.AST | None:
    """Walk down a chain of ``a / b / c`` Div-BinOps and return the left-most
    operand (the base of the path)."""
    cur = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        cur = cur.left
    return cur


def _name_of(node: ast.AST) -> str | None:
    """Return the trailing identifier of a Name or Attribute node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ---------------------------------------------------------------------------
# Discovery (step 1)
# ---------------------------------------------------------------------------
def _canonicalize(name: str) -> str:
    """Strip atomic-write temp suffixes so ``x.ndjson.tmp`` and ``x.tmp`` map
    onto the real store id ``x.ndjson``."""
    n = name
    while n.endswith(".tmp"):
        n = n[: -len(".tmp")]
    return n


def _looks_like_store_filename(name: str) -> bool:
    if name in _NEVER_A_STORE:
        return False
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in PERSIST_EXTENSIONS:
        return True
    return False


class _DataDirBaseResolver:
    """Resolves whether a path-base expression is rooted at the data dir.

    Heuristics handled per-module:
      - ``data_dir`` / ``self.data_dir`` / ``self._data_dir`` / ``store.data_dir``
      - any ``self.<x>_dir`` / ``self._<x>_dir`` attribute (or property /
        local) that is assigned a value rooted at the data dir somewhere in the
        module (transitive root).
    """

    def __init__(self, tree: ast.Module, consts: dict[str, str]) -> None:
        self.consts = consts
        self._dir_roots: set[str] = set(DATA_DIR_BASE_NAMES)
        # attr/local name -> relative subdir path under data_dir ("" = data_dir
        # itself, "shares" = data_dir/shares, "backups" = data_dir/backups, ...)
        self._dir_subpath: dict[str, str] = {b: "" for b in DATA_DIR_BASE_NAMES}
        self._discover_derived_dirs(tree)

    def _expr_base_name(self, node: ast.AST) -> str | None:
        """For a path expression, return the identifier that names its base."""
        root = _binop_div_chain_root(node)
        if root is None:
            return None
        root = self._unwrap(root)
        return _name_of(root)

    @staticmethod
    def _unwrap(node: ast.AST) -> ast.AST:
        # Unwrap Path(x) -> x ; x.resolve()/.expanduser()/.absolute() -> x
        cur = node
        changed = True
        while changed:
            changed = False
            if isinstance(cur, ast.Call):
                func = cur.func
                if isinstance(func, ast.Name) and func.id == "Path" and cur.args:
                    cur = cur.args[0]
                    changed = True
                elif isinstance(func, ast.Attribute) and func.attr in {
                    "resolve",
                    "expanduser",
                    "absolute",
                }:
                    cur = func.value
                    changed = True
        return cur

    def _subpath_join(self, parent: str, seg: str | None) -> str:
        seg = (seg or "").strip("/")
        if not parent:
            return seg
        if not seg:
            return parent
        return f"{parent}/{seg}"

    def _register_dir(self, name: str, value: ast.AST) -> bool:
        """If ``value`` is rooted at a known dir, register ``name`` as a derived
        dir and compute its subpath.  Returns True if newly registered."""
        if name in self._dir_roots:
            return False
        base = self._expr_base_name(value)
        if base is None or base not in self._dir_roots:
            return False
        # Subpath = parent subpath + trailing segment of this expression.
        seg = None
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            seg = _resolve_rhs_name(value.right, self.consts)
        parent_sub = self._dir_subpath.get(base, "")
        self._dir_roots.add(name)
        self._dir_subpath[name] = self._subpath_join(parent_sub, seg)
        return True

    def _discover_derived_dirs(self, tree: ast.Module) -> None:
        """Find ``self._x_dir = <data-dir-rooted>`` assignments (and the
        property form ``return self.data_dir / "sub"``) so later uses of
        ``self._x_dir / "file"`` are recognised, recording each dir's subpath
        under data_dir.  Fixpoint over a few passes for forward references."""
        for _ in range(5):
            added = False
            for node in ast.walk(tree):
                target_name: str | None = None
                value: ast.AST | None = None
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    tgt = node.targets[0]
                    if isinstance(tgt, ast.Attribute):
                        target_name = tgt.attr
                    elif isinstance(tgt, ast.Name):
                        target_name = tgt.id
                    value = node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    if isinstance(node.target, ast.Attribute):
                        target_name = node.target.attr
                    elif isinstance(node.target, ast.Name):
                        target_name = node.target.id
                    value = node.value
                if target_name is None or value is None:
                    continue
                if self._register_dir(target_name, value):
                    added = True
            # Property/method-returned dirs: ``return self.data_dir / "exports"``
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Return) and sub.value is not None:
                            if self._register_dir(node.name, sub.value):
                                added = True
            if not added:
                break

    def is_data_dir_rooted(self, node: ast.AST) -> bool:
        base = self._expr_base_name(node)
        return base is not None and base in self._dir_roots

    def base_attr_is_dir_root(self, attr_name: str | None) -> bool:
        return attr_name is not None and attr_name in self._dir_roots

    def base_subpath(self, node: ast.AST) -> str:
        """Relative subdir of the base of ``node`` under data_dir ("" = root)."""
        base = self._expr_base_name(node)
        if base is None:
            return ""
        return self._dir_subpath.get(base, "")

    def attr_subpath(self, attr_name: str | None) -> str:
        if attr_name is None:
            return ""
        return self._dir_subpath.get(attr_name, "")


def _resolve_rhs_name(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Resolve the right operand of ``base / X`` to a filename string.

    Handles string literals, module/class constants by name, and
    ``self._CONST`` attribute references whose attr is a known constant.
    """
    lit = _const_str(node)
    if lit is not None:
        return lit
    name = _name_of(node)
    if name is not None and name in consts:
        return consts[name]
    return None


def _trailing_extension(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Return the persisted-file extension of a filename expression that is NOT
    a plain literal/constant — i.e. an **f-string** such as
    ``f"report_{ts}.html"`` or a concat ``"srt_" + id + ".srt"``.

    W1771 GAP-1b (sibling-extension detection): export handlers build their
    filenames dynamically (``transcripts_dir / f"report_{ts}.html"``), so the
    literal-only resolver never sees them and the containing directory was
    wrongly credited as fully covered off a single ``*.md`` sweep.  This helper
    extracts just the trailing ``.ext`` from the dynamic name so the discovery
    can record a per-extension store (``transcripts/*.html``) that the purge
    must independently clear.

    Returns the bare extension (``"html"``) when the expression ends in a string
    literal carrying a ``.<persist-ext>`` suffix, else None.  A plain literal /
    constant returns None on purpose (handled by ``_resolve_rhs_name``).
    """
    if _resolve_rhs_name(node, consts) is not None:
        return None  # plain literal/const — not our job

    tail: str | None = None
    if isinstance(node, ast.JoinedStr):
        # f-string: inspect the last formatted/literal part for a ".ext" tail.
        for part in reversed(node.values):
            lit = _const_str(part)
            if lit:
                tail = lit
                break
            # A FormattedValue at the very end (``f"{x}"``) has no static suffix.
            break
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # "prefix_" + var + ".srt"  → walk down to the right-most literal.
        right = node.right
        tail = _const_str(right)

    if not tail:
        return None
    ext_match = re.search(r"\.([a-z0-9]+)$", tail)
    if ext_match is None:
        return None
    ext = ext_match.group(1)
    if ext not in PERSIST_EXTENSIONS and ext not in {"md", "html"}:
        return None
    return ext


def collect_fstring_ext_vars(scope: ast.AST, consts: dict[str, str]) -> dict[str, str]:
    """Map local variable names to the persisted extension of the f-string /
    concat they are assigned within ``scope``: ``filename = f"report_{ts}.html"``
    → ``{"filename": "html"}``.

    Export handlers split the write across two statements::

        filename = f"report_{ts}.html"     # dynamic name → captured here
        file_path = transcripts_dir / filename   # use site sees only ``filename``

    so the inline ``_trailing_extension`` at the ``/`` use site misses it.  This
    captures the extension by variable name.  **Scope matters**: the SAME local
    name (``filename``) is reused across handlers for different extensions
    (``.md`` / ``.srt`` / ``.html`` / ``.json``), so this MUST be called
    per-function (not module-wide) or the first assignment would mask the rest.
    """
    ext_vars: dict[str, str] = {}
    for node in ast.walk(scope):
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
        ext = _trailing_extension(value, consts)
        if ext is not None:
            ext_vars.setdefault(target.id, ext)
    return ext_vars


def _record_glob(
    found: dict[str, StoreRef],
    module: str,
    rel: str,
    pattern: str,
    lineno: int,
) -> None:
    """A prefixed ``glob("prefix_*.ext")`` family persists a distinct store
    (e.g. ``audit_*.ndjson``).  Record it so the guard can demand the purge
    clears the whole family.  Bare ``*.ext`` globs are the containing subdir
    (tracked via mkdir/dir refs) — skipped here to avoid noise."""
    basename = pattern.rsplit("/", 1)[-1]
    ext_match = re.search(r"\.([a-z0-9]+)$", basename)
    if ext_match is None:
        return
    ext = ext_match.group(1)
    if ext not in PERSIST_EXTENSIONS and ext != "md":
        return
    if basename.startswith("*"):
        return
    found.setdefault(pattern, StoreRef(pattern, module, f"{rel}:{lineno}"))


def discover_stores_in_module(path: Path) -> list[StoreRef]:
    """Scan a single backend/core module for persisted data-dir stores."""
    tree = _parse(path)
    consts = collect_string_constants(tree)
    resolver = _DataDirBaseResolver(tree, consts)
    module = path.stem
    rel = _rel(path)
    found: dict[str, StoreRef] = {}

    def _qualify(subpath: str, name: str) -> str:
        subpath = subpath.strip("/")
        return f"{subpath}/{name}" if subpath else name

    def _record(name: str, lineno: int, *, is_dir: bool, subpath: str = "") -> None:
        if not name:
            return
        if is_dir:
            store_id = _qualify(subpath, name.rstrip("/")) + "/"
        else:
            name = _canonicalize(name)
            if not _looks_like_store_filename(name):
                return
            store_id = _qualify(subpath, name)
        found.setdefault(store_id, StoreRef(store_id, module, f"{rel}:{lineno}"))

    def _record_ext_family(subpath: str, ext: str, lineno: int) -> None:
        """Record a per-extension store ``<subdir>/*.ext`` for a directory that
        receives dynamically-named files of extension ``ext`` (W1771 GAP-1b).

        Modelled as a glob-family store id so coverage demands the purge sweep
        that extension explicitly (or wipe the whole dir).  Dir-rooted files
        (subpath == "") would collide with real top-level stores, so a bare
        ``*.ext`` at data-dir root is skipped (no such PII pattern in this repo)."""
        subpath = subpath.strip("/")
        if not subpath:
            return
        store_id = f"{subpath}/*.{ext}"
        found.setdefault(store_id, StoreRef(store_id, module, f"{rel}:{lineno}"))

    # (a'') Per-function pre-pass for the two-statement dynamic-export pattern:
    #   filename = f"report_{ts}.html"           (local, function-scoped)
    #   file_path = transcripts_dir / filename   (dir-rooted use site)
    # Resolved per-function because the SAME local name (``filename``) is reused
    # across handlers for different extensions — a module-wide map would collapse
    # them onto the first one seen.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fn_ext_vars = collect_fstring_ext_vars(fn, consts)
        if not fn_ext_vars:
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            if not resolver.is_data_dir_rooted(node):
                continue
            if _resolve_rhs_name(node.right, consts) is not None:
                continue  # plain literal/const handled in main loop below
            rname = _name_of(node.right)
            ext = fn_ext_vars.get(rname) if rname else None
            if ext is not None:
                _record_ext_family(resolver.base_subpath(node), ext, node.lineno)

    for node in ast.walk(tree):
        # (a) base_dir / "name"  OR  base_dir / _CONST  (base may be a subdir)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if resolver.is_data_dir_rooted(node):
                rhs = _resolve_rhs_name(node.right, consts)
                if rhs is not None:
                    _record(
                        rhs,
                        node.lineno,
                        is_dir="." not in rhs,
                        subpath=resolver.base_subpath(node),
                    )
                else:
                    # (a') base_dir / f"prefix_{x}.ext"  (inline dynamic name).
                    ext = _trailing_extension(node.right, consts)
                    if ext is not None:
                        _record_ext_family(
                            resolver.base_subpath(node), ext, node.lineno
                        )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            recv = node.func.value
            # (b) <dir>.glob("prefix_*.ext")
            if attr == "glob" and node.args:
                pattern = _const_str(node.args[0])
                if pattern is not None and resolver.base_attr_is_dir_root(
                    _name_of(recv)
                ):
                    _record_glob(
                        found,
                        module,
                        rel,
                        _qualify(resolver.attr_subpath(_name_of(recv)), pattern),
                        node.lineno,
                    )
            # (c) (base_dir / "sub").mkdir(...)
            if attr == "mkdir" and isinstance(recv, ast.BinOp) and isinstance(
                recv.op, ast.Div
            ):
                if resolver.is_data_dir_rooted(recv):
                    rhs = _resolve_rhs_name(recv.right, consts)
                    if rhs is not None and "." not in rhs:
                        _record(
                            rhs,
                            node.lineno,
                            is_dir=True,
                            subpath=resolver.base_subpath(recv),
                        )

    return list(found.values())


def discover_all_stores() -> dict[str, list[StoreRef]]:
    """Discover every persisted store across backend + core, keyed by store id."""
    out: dict[str, list[StoreRef]] = {}
    for directory in (BACKEND_DIR, CORE_DIR):
        for path in sorted(directory.rglob("*.py")):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            for ref in discover_stores_in_module(path):
                out.setdefault(ref.store_id, []).append(ref)
    return out


# ---------------------------------------------------------------------------
# Coverage extraction (step 2)
# ---------------------------------------------------------------------------
def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _module_attr_filenames(scope: ast.AST, consts: dict[str, str]) -> dict[str, str]:
    """Map ``self._x_path`` attribute names to the filename they are assigned,
    by scanning ``scope`` (a module tree).  Resolution walks one hop:
    ``self._x_path = <base> / _CONST`` -> filename from ``consts``."""
    mapping: dict[str, str] = {}
    for node in ast.walk(scope):
        target_attr: str | None = None
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Attribute):
                target_attr = tgt.attr
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Attribute):
                target_attr = node.target.attr
            value = node.value
        if target_attr is None or value is None:
            continue
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            rhs = _resolve_rhs_name(value.right, consts)
            if rhs is not None and _looks_like_store_filename(_canonicalize(rhs)):
                mapping.setdefault(target_attr, rhs)
    return mapping


def _collect_removed_names_in_function(
    func: ast.FunctionDef, consts: dict[str, str], module_attrs: dict[str, str]
) -> set[str]:
    """Collect every filename/dir id physically removed or emptied inside a
    function body: ``base / "name"`` literals, ``X.glob(pat)`` deletion loops,
    and ``self._path.unlink()`` / ``rmtree(self._dir)`` whose attribute resolves
    to a known store filename.

    Permissive by design: the purge methods only *reference* a store when they
    delete/empty it, so any addressed store id counts as covered.
    """
    removed: set[str] = set()

    for node in ast.walk(func):
        # base / "name"  (literal or constant)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            rhs = _resolve_rhs_name(node.right, consts)
            if rhs is not None:
                canon = _canonicalize(rhs)
                if _looks_like_store_filename(canon):
                    removed.add(canon)
                elif "." not in rhs:
                    removed.add(rhs.rstrip("/") + "/")

        # X.glob("pat") -> family / subdir enumerated for deletion
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "glob" and node.args:
                pattern = _const_str(node.args[0])
                if pattern is not None and not pattern.startswith("*"):
                    removed.add(pattern)

    # self._path / self._x_path attributes that are unlinked/replaced/rmtree'd.
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"unlink", "rmtree", "replace"}:
                targets: list[ast.AST] = []
                if node.func.attr == "rmtree":
                    targets.extend(node.args)
                else:
                    targets.append(node.func.value)
                    targets.extend(node.args)
                for tgt in targets:
                    attr = _name_of(tgt)
                    if attr in module_attrs:
                        fname = _canonicalize(module_attrs[attr])
                        if _looks_like_store_filename(fname):
                            removed.add(fname)
    return removed


def _dir_extension_coverage(
    func: ast.FunctionDef, resolver: "_DataDirBaseResolver", consts: dict[str, str]
) -> set[str]:
    """W1771 GAP-1b: per-extension + wipe-all coverage for data-dir subdirectories.

    A directory store (``transcripts/``) holds dynamically-named export files of
    several extensions (``*.md``, ``*.html``, ``*.srt`` ...).  Merely *naming* the
    directory inside the purge (``transcripts_dir = data_dir / "transcripts"``)
    must NOT credit every extension as cleared — only the extensions the purge
    actually sweeps are covered.  This returns, for each data-dir-rooted dir the
    purge touches:

      - ``<subdir>/*.ext``  for each explicit ``<dir>.glob("*.ext")`` sweep, and
      - ``<subdir>/*``       (a wipe-all marker) when the purge removes the dir
        wholesale — ``shutil.rmtree(<dir>)`` or a full ``<dir>.iterdir()`` /
        ``<dir>.glob("*")`` enumeration that unlinks every entry.

    ``_is_covered`` then credits a discovered ``<subdir>/*.ext`` store iff that
    exact extension is swept, or the dir carries the ``<subdir>/*`` wipe-all mark.
    """
    covered: set[str] = set()

    def _subpath_of(node: ast.AST) -> str | None:
        """Subpath under data_dir for a dir expression / dir local-var name."""
        name = _name_of(node)
        if name is not None and name in resolver._dir_roots:
            return resolver.attr_subpath(name)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if resolver.is_data_dir_rooted(node):
                rhs = _resolve_rhs_name(node.right, consts)
                base_sub = resolver.base_subpath(node)
                if rhs is not None and "." not in rhs:
                    seg = rhs.rstrip("/")
                    return f"{base_sub}/{seg}".strip("/") if base_sub else seg
                return base_sub
        return None

    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        recv = node.func.value

        # <dir>.glob("...") — per-extension sweep OR wipe-all ("*").
        if attr == "glob" and node.args:
            pattern = _const_str(node.args[0])
            if pattern is None:
                continue
            sub = _subpath_of(recv)
            if not sub:
                continue
            if pattern == "*":
                covered.add(f"{sub}/*")
            else:
                m = re.search(r"\*\.([a-z0-9]+)$", pattern)
                if m:
                    covered.add(f"{sub}/*.{m.group(1)}")

        # <dir>.iterdir() — full enumeration ⇒ every entry removed (wipe-all).
        elif attr == "iterdir":
            sub = _subpath_of(recv)
            if sub:
                covered.add(f"{sub}/*")

        # shutil.rmtree(<dir>) — directory removed wholesale (wipe-all).
        elif attr == "rmtree":
            for arg in node.args:
                sub = _subpath_of(arg)
                if sub:
                    covered.add(f"{sub}/*")

    return covered


# --- collaborator resolution: attr -> module_stem --------------------------
def _build_service_collaborator_map() -> dict[str, str]:
    """Parse ``service.py`` and return ``{collaborator_attr: module_stem}``.

    Two resolution passes, because a collaborator is instantiated under one
    attribute and then *aliased* onto the history-service attribute that
    ``handle_purge_all_data`` actually calls::

        self.vocabulary = VocabularyStore(...)          # pass 1: instantiation
        self._history._vocabulary_store = self.vocabulary  # pass 2: alias chain

    Pass 1 records ``attr -> module`` for every ``<obj>.attr = ClassName(...)``
    whose class is imported via ``from backend.<mod> import <ClassName>``.
    Pass 2 (fixpoint) propagates the module across ``<obj>.dst = <obj>.src``
    aliasing assignments, so the history-service-side attribute name resolves to
    the same module.  Keys are the trailing attribute names (collisions across
    objects are not a concern here — the persist collaborators have unique
    attr names).
    """
    tree = _parse(SERVICE_PY)
    class_to_module: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("backend."):
                mod_stem = node.module.split(".", 1)[1]
                for alias in node.names:
                    class_to_module[alias.asname or alias.name] = mod_stem

    attr_to_module: dict[str, str] = {}
    aliases: list[tuple[str, str]] = []  # (dst_attr, src_attr)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Attribute):
            continue
        val = node.value
        # Pass 1: direct instantiation  self.X = ClassName(...)
        if isinstance(val, ast.Call):
            cls_name = val.func.id if isinstance(val.func, ast.Name) else None
            if cls_name and cls_name in class_to_module:
                attr_to_module[tgt.attr] = class_to_module[cls_name]
        # Collect alias candidates  self._history._X = self.vocabulary
        src_attr = _name_of(val)
        if src_attr is not None and isinstance(val, ast.Attribute):
            aliases.append((tgt.attr, src_attr))

    # Pass 2: propagate module across alias chains until fixpoint.
    for _ in range(6):
        changed = False
        for dst_attr, src_attr in aliases:
            if dst_attr in attr_to_module:
                continue
            if src_attr in attr_to_module:
                attr_to_module[dst_attr] = attr_to_module[src_attr]
                changed = True
        if not changed:
            break
    return attr_to_module


def _collaborator_purge_calls(func: ast.FunctionDef) -> set[str]:
    """Return collaborator attribute names ``_X`` for ``self._X.<purge>(...)``
    calls inside ``func``."""
    attrs: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in PURGE_METHOD_NAMES:
                attr = _name_of(node.func.value)
                if attr is not None:
                    attrs.add(attr)
    return attrs


def _save_method_targets(
    tree: ast.Module, module_attrs: dict[str, str]
) -> dict[str, str]:
    """Map ``_save`` / ``_persist`` / ``_write_*`` method names to the store
    filename they (re)write.

    A "clear-then-save" purge (e.g. ``recording_chain.delete_all_chains`` resets
    ``self._data = {...}`` then calls ``self._save()``) empties the on-disk store
    without ever naming the file inside the purge method itself.  We detect the
    file by parsing the save method for the path attribute it writes via:
      - ``tmp.replace(self._path)``        (Path.replace — dest is ``args[0]``)
      - ``os.replace(tmp, self._path)``    (os.replace — dest is ``args[1]``;
        W1771 GAP-3: ``transcript_versioning._rewrite_all`` uses exactly this, so
        ``clear_all`` left ``transcript_versions.ndjson`` looking uncovered)
      - ``open(self._path, "w")`` / ``self._path.write_text(...)``
    To stay robust to either ``replace`` form, ALL of {receiver, args} are
    considered and whichever resolves to a known store attr is credited.
    """
    targets: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not re.match(r"_(save|persist|write|flush|rewrite|dump)", node.name):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            cands: list[ast.AST] = []
            if sub.func.attr == "replace":
                # Path.replace(dest): receiver=tmp, dest=args[0].
                # os.replace(src, dest): dest=args[1].  Consider every operand and
                # credit whichever names a known store path attribute.
                cands.extend(sub.args)
                cands.append(sub.func.value)
            elif sub.func.attr in {"write_text", "write_bytes"}:
                cands.append(sub.func.value)  # self._path.write_text(...)
            elif sub.func.attr == "open" and sub.args:
                cands.append(sub.func.value)  # self._path.open("w")
            for cand in cands:
                attr = _name_of(cand)
                if attr in module_attrs:
                    targets.setdefault(node.name, _canonicalize(module_attrs[attr]))
                    break
    return targets


def _filenames_cleared_by_module(module_stem: str) -> set[str]:
    """Parse ``backend/<module_stem>.py`` and collect filenames cleared by any
    purge method (across the module's classes), including the clear-then-save
    pattern (purge resets in-memory state then calls ``self._save()``)."""
    path = BACKEND_DIR / f"{module_stem}.py"
    if not path.exists():
        return set()
    tree = _parse(path)
    consts = collect_string_constants(tree)
    module_attrs = _module_attr_filenames(tree, consts)
    save_targets = _save_method_targets(tree, module_attrs)
    cleared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in PURGE_METHOD_NAMES:
            cleared |= _collect_removed_names_in_function(node, consts, module_attrs)
            # Clear-then-save: credit the file written by any save method the
            # purge method invokes (e.g. delete_all_chains -> _save -> chains).
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr in save_targets:
                        fname = save_targets[sub.func.attr]
                        if _looks_like_store_filename(fname):
                            cleared.add(fname)
    return cleared


def _state_store_compaction_coverage() -> set[str]:
    """Files emptied by ``state_store._compact_unlocked`` (history.ndjson +
    sidecar journals).  ``handle_purge_all_data`` tombstones all items then
    calls ``compact_with_stats``, so these are physically cleared."""
    tree = _parse(STATE_STORE)
    consts = collect_string_constants(tree)
    attr_filenames = _module_attr_filenames(tree, consts)
    cleared: set[str] = set()
    for fn_name in ("_compact_unlocked", "compact_with_stats"):
        fn = _find_function(tree, fn_name)
        if fn is None:
            continue
        for node in ast.walk(fn):
            attr = node.attr if isinstance(node, ast.Attribute) else None
            if attr in attr_filenames:
                fname = _canonicalize(attr_filenames[attr])
                if _looks_like_store_filename(fname):
                    cleared.add(fname)
    return cleared


def extract_purge_coverage() -> set[str]:
    """Return the full set of store ids cleared by ``handle_purge_all_data``."""
    hs_tree = _parse(HISTORY_SERVICE)
    hs_consts = collect_string_constants(hs_tree)
    hs_attrs = _module_attr_filenames(hs_tree, hs_consts)
    purge_fn = _find_function(hs_tree, "handle_purge_all_data")
    if purge_fn is None:
        raise SystemExit(
            "audit_purge_coverage: handle_purge_all_data not found in "
            f"{_rel(HISTORY_SERVICE)} — purge guard cannot run."
        )

    covered: set[str] = set()

    # (a) direct file/dir operations inside handle_purge_all_data itself.
    covered |= _collect_removed_names_in_function(purge_fn, hs_consts, hs_attrs)

    # (a') W1771 GAP-1b: per-extension + wipe-all coverage for data-dir subdirs
    # (e.g. transcripts/*.html swept explicitly, or shares/ rmtree'd wholesale).
    hs_resolver = _DataDirBaseResolver(hs_tree, hs_consts)
    covered |= _dir_extension_coverage(purge_fn, hs_resolver, hs_consts)

    # (b) collaborator purge calls -> parse each collaborator's purge methods.
    attr_to_module = _build_service_collaborator_map()
    for attr in _collaborator_purge_calls(purge_fn):
        module_stem = attr_to_module.get(attr)
        if module_stem is None:
            # Unmapped collaborator -> cannot credit its coverage (safe: leaves
            # the store visible as a gap rather than silently passing).
            continue
        covered |= _filenames_cleared_by_module(module_stem)

    # (c) state_store compaction (history.ndjson + sidecar journals).
    covered |= _state_store_compaction_coverage()

    # Canonicalise filename ids (strip .tmp) but preserve the ``*.ext`` / ``*``
    # extension-family markers verbatim (they carry no temp suffix).
    return {c if c.endswith("*") or "/*." in c else _canonicalize(c) for c in covered}


# ---------------------------------------------------------------------------
# Allowlist (step 4)
# ---------------------------------------------------------------------------
def load_allowlist() -> set[str]:
    if not ALLOWLIST_FILE.exists():
        return set()
    out: set[str] = set()
    for raw in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _basename(store_id: str) -> str:
    return store_id.rstrip("/").rsplit("/", 1)[-1] + ("/" if store_id.endswith("/") else "")


def _is_covered(
    store_id: str,
    covered: set[str],
    allowlisted: set[str],
    discovered_ids: set[str] | None = None,
) -> bool:
    """A discovered store is covered if any of the following hold:

      0. **W1771 sibling-extension rule (checked first for ``<subdir>/*.ext``):**
         a per-extension family store is covered ONLY by an explicit extension
         sweep (``<subdir>/*.ext`` in pool) or a confirmed whole-dir wipe
         (``<subdir>/*`` in pool — rmtree / full iterdir).  It is deliberately
         NOT credited by the generic directory-prefix rule (3): merely *naming*
         ``transcripts/`` while sweeping only ``*.md`` must leave ``*.html`` etc.
         visible as a gap.  Allowlisting a ``<subdir>/*.ext`` id still works.
      1. Exact id match in covered/allowlist (``archive.ndjson``).
      2. Basename match — a collaborator that clears ``archive.ndjson`` clears it
         regardless of the subdir it lives in, so ``archive/archive.ndjson`` is
         covered by ``archive.ndjson`` (basenames are unique in this codebase).
      3. Under a covered/allowlisted *directory* prefix (``shares/...`` ⊂
         ``shares/``; ``backups/auto_backup_meta.json`` ⊂ ``backups/``).
      4. A *directory* whose every discovered child file is itself covered (an
         empty shell once its contents are wiped — e.g. ``archive/`` whose only
         content ``archive/archive.ndjson`` is cleared by ``clear_all``).
    """
    pool = covered | allowlisted
    # (0) sibling-extension family store ``<subdir>/*.ext``.
    if "/*." in store_id:
        if store_id in pool:
            return True
        subdir = store_id.rsplit("/", 1)[0]
        if f"{subdir}/*" in pool:  # whole-dir wipe covers every extension
            return True
        return False
    if store_id in pool:
        return True
    bn = _basename(store_id)
    if bn in pool:
        return True
    for d in (c for c in pool if c.endswith("/")):
        if store_id.startswith(d):
            return True
    # (4) directory whose discovered children are all covered.
    if store_id.endswith("/") and discovered_ids:
        children = [
            cid
            for cid in discovered_ids
            if cid != store_id and cid.startswith(store_id) and not cid.endswith("/")
        ]
        if children and all(
            _is_covered(child, covered, allowlisted) for child in children
        ):
            return True
    return False


def run_audit() -> AuditResult:
    discovered = discover_all_stores()
    covered = extract_purge_coverage()
    allowlisted = load_allowlist()

    discovered_ids = set(discovered.keys())
    gaps: list[StoreRef] = []
    for store_id, refs in sorted(discovered.items()):
        if _is_covered(store_id, covered, allowlisted, discovered_ids):
            continue
        gaps.append(sorted(refs, key=lambda r: r.location)[0])

    return AuditResult(
        discovered=discovered,
        covered=covered,
        allowlisted=allowlisted,
        gaps=sorted(gaps, key=lambda r: (r.module, r.store_id)),
    )


def format_report(result: AuditResult) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("PRIVACY-PURGE COVERAGE AUDIT (W1768)")
    lines.append("=" * 78)
    lines.append(f"discovered stores : {len(result.discovered)}")
    lines.append(f"covered by purge  : {len(result.covered)}")
    lines.append(f"allowlisted       : {len(result.allowlisted)}")
    lines.append(f"UNCOVERED GAPS    : {len(result.gaps)}")
    lines.append("")

    if not result.gaps:
        lines.append("OK — every persisted store is wiped by purge or allowlisted.")
        return "\n".join(lines)

    lines.append("Stores persisted under the data dir but NOT wiped by")
    lines.append("history_service.handle_purge_all_data (and not allowlisted):")
    lines.append("")
    by_module: dict[str, list[StoreRef]] = {}
    for ref in result.gaps:
        by_module.setdefault(ref.module, []).append(ref)
    for module in sorted(by_module):
        lines.append(f"  [{module}]")
        for ref in sorted(by_module[module], key=lambda r: r.store_id):
            lines.append(f"    - {ref.store_id:<34} {ref.location}")
        lines.append("")
    lines.append("Each line is a store that survives a privacy purge.  Either wire")
    lines.append("its deletion into handle_purge_all_data, or add it to")
    lines.append(
        f"  {_rel(ALLOWLIST_FILE)}  with a # reason if it is an intentional survivor."
    )
    return "\n".join(lines)


def format_json(result: AuditResult) -> str:
    payload = {
        "discovered_count": len(result.discovered),
        "covered_count": len(result.covered),
        "allowlisted_count": len(result.allowlisted),
        "gap_count": len(result.gaps),
        "covered": sorted(result.covered),
        "allowlisted": sorted(result.allowlisted),
        "gaps": [
            {
                "store_id": ref.store_id,
                "module": ref.module,
                "location": ref.location,
            }
            for ref in result.gaps
        ],
        "discovered": {
            store_id: [
                {"module": r.module, "location": r.location} for r in refs
            ]
            for store_id, refs in sorted(result.discovered.items())
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="exit non-zero if any non-allowlisted gap exists",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the text report",
    )
    args = parser.parse_args(argv)

    result = run_audit()

    if args.json:
        print(format_json(result))
    else:
        print(format_report(result))

    if args.fail_on_found and result.gaps:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
