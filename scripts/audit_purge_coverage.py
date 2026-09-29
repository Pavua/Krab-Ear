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
    {"json", "ndjson", "txt", "npy", "key", "csv", "html", "srt", "bak"}
)

# A5.2c1: a ``.bak``-copy is a DERIVED copy of the store whose name precedes it
# (``history.ndjson`` → ``history.ndjson.bak`` / ``.bak-<ts>``).  It holds the
# same cleartext the base store holds, so a privacy purge that leaves it behind is
# not a purge.  The guard used to be structurally blind to this family:
# ``_record_glob`` dropped any pattern whose trailing token is not a known
# extension, and ``history.ndjson.bak*`` ends in ``bak*`` — so deleting the
# purge's ``*.bak*`` sweep was invisible here, exactly the "wiring removed but
# 0 gaps stayed green" class the A5.2b2 review closed for f-string families.
# Recognised by SHAPE, so a future producer cannot fall through silently.
BACKUP_COPY_SUFFIX_RE = re.compile(r"^\.(?P<ext>[a-z0-9]+)\.bak(?:[-._0-9A-Za-z*]*)$")
_BACKUP_GLOB_RE = re.compile(r"^(?P<base>.+?)\.bak[-._0-9A-Za-z*]*$")

# A5.2c1 (M1): atomic-write temp copies.  ``x.ndjson.tmp`` / ``x.ndjson.migration_tmp``
# are the temp file of an atomic write that DIED before its rename — i.e. a full
# copy of the store.  ``history.ndjson.migration_tmp`` was the reviewer's finding:
# a complete plaintext copy of history, and ``settings.json.tmp`` a settings copy
# with secrets, both surviving a purge that reported ``complete: true``.
#
# The guard was blind to them TWICE OVER, and both holes mattered:
#   * ``migration_tmp`` is not a known extension, so it was never a store;
#   * ``_canonicalize`` strips ``.tmp`` — so ``settings.json.tmp`` collapsed ONTO
#     ``settings.json``, which is ALLOWLISTED.  A temp file was therefore "covered"
#     by the allowlist entry of the very file it is a copy of.
# The family is therefore recorded under its OWN canonical id (``*.tmp`` / ``*_tmp``)
# and coverage demands an explicit sweep of that family in the purge.  Both endings
# are listed separately because ``*.tmp`` does NOT match ``migration_tmp`` — the
# underscore is the whole reason the first leak slipped through.
# ``.irregular`` добавлен по находке N2: pre-flight уводит нерегулярную запись
# (fifo/сокет) в ``<name>.irregular``, и такая запись не должна выпадать из зоны
# purge МОЛЧА.
#
# ЧЕСТНО О РАЗДЕЛЕНИИ (N2′ — прежний комментарий здесь врал): семейство нужно
# ТОЛЬКО ГЕЙТУ, то есть для ВИДИМОСТИ. СВИПА `.irregular` в purge НЕТ и
# добавлять его нельзя:
#   * `.irregular` — отложенная НЕРЕГУЛЯРНАЯ запись (fifo/сокет), а не временная
#     копия данных. Каталоги с этой ветки уже снимаются `rmtree`, так что в
#     отложенной ветке остаются только fifo/сокет;
#   * снос пользовательских `*.irregular` был бы over-delete: проверено пробой
#     ревьюера, файл `моё_заметка.irregular` переживает purge — и это правильное
#     поведение, а не пробел (тест в test_a52c1_purge_integrity.py фиксирует его
#     как намеренное решение, чтобы следующий волнёц не «допилил» свип).
# Реальный свип покрывает `.bak*` / `*.tmp` / `*_tmp` — это производные копии.
TEMP_FAMILY_SUFFIXES: tuple[str, ...] = (".tmp", "_tmp", ".irregular")

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


def _temp_family(suffix: str) -> str | None:
    """Канонический id семейства temp-копий по окончанию (``*.tmp`` / ``*_tmp``).

    ``suffix`` — либо имя файла (``settings.json.tmp``), либо литерал суффикса
    (``".ndjson.migration_tmp"``). None — это не temp-копия. Возвращается
    СЕМЕЙСТВО, а не точное имя: у ``journal_path.with_suffix(".tmp")`` база
    неизвестна статически (это переменная цикла), и требовать от purge
    «удали history_status.tmp» вместо «удали всё, что кончается на .tmp» было бы
    и неточно, и непроверяемо.
    """
    name = suffix.strip()
    for ending in TEMP_FAMILY_SUFFIXES:
        if name.endswith(ending) and len(name) > len(ending):
            return f"*{ending}"
    return None


def _looks_like_backup_copy(name: str) -> bool:
    """``history.ndjson.bak`` / ``settings.json.bak1`` / ``…bak-20260926``?

    A5.2c1: backup copies are data stores in their own right — the base name must
    itself look like a store, so ``bak`` alone is never a store but
    ``history.ndjson.bak`` is one.  The canonical family id is
    ``<base>.bak*`` so all timestamped/numbered variants collapse into ONE
    store the purge must sweep explicitly.
    """
    match = _BACKUP_GLOB_RE.match(name)
    if match is None:
        return False
    base = match.group("base")
    if base == ".secrets" or base.endswith("/.secrets"):
        return True
    parts = base.rsplit(".", 1)
    return len(parts) == 2 and parts[1] in PERSIST_EXTENSIONS


def _backup_family(name: str) -> str | None:
    """Канонический id семейства ``<base>.bak*`` (None — не backup-копия)."""
    match = _BACKUP_GLOB_RE.match(name)
    if match is None:
        return None
    base = match.group("base")
    if base == ".secrets" or base.endswith("/.secrets"):
        return f"{base}.bak*"
    parts = base.rsplit(".", 1)
    if len(parts) != 2 or parts[1] not in PERSIST_EXTENSIONS:
        return None
    return f"{base}.bak*"


def _looks_like_store_filename(name: str) -> bool:
    if name in _NEVER_A_STORE:
        return False
    if _looks_like_backup_copy(name):
        return True
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


def _fstring_family(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Glob-store id для f-string имени файла, иначе ``None``.

    A5.2b2 review (B3): сканер видел только literal / ``_CONST`` / ``glob``, а
    restore-артефакты построены как ``data_dir / f"{PREFIX}{txid}"`` и
    ``data_dir / f"{name}{SUFFIX}{txid}"``. Из-за этого «0 gaps» оставалось
    зелёным и после удаления purge-wiring — гейт был слепым.

    Правило (намеренно узкое, чтобы не превратить каждое f-string-имя в репо в
    «дыру»):

      * собираем СТАТИЧЕСКИЙ текст: литеральные части плюс ``{КОНСТАНТА}``;
        первый по-настоящему динамический ``{переменная}`` обрывает голову;
      * семейство = ``голова + "*" + хвост`` (хвост — статические части после
        первой динамической);
      * семейство считается хранилищем, только если в статическом тексте есть
        точка (``.a52b2-restore-`` / ``.ndjson.a52b2-restore-``) — то есть
        результат действительно похож на файл. Поэтому ``f"backup_{ts}"``
        (точки нет) остаётся невидимым, как раньше.
    """
    if not isinstance(node, ast.JoinedStr):
        return None
    if _resolve_rhs_name(node, consts) is not None:
        return None  # plain literal/const — обработано основным путём

    head_parts: list[str] = []
    tail_parts: list[str] = []
    dynamic_seen = False
    leading_dynamic = False
    trailing_dynamic = False
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            resolved = _const_str_value(part.value, consts)
            if resolved is not None:
                # Константа в f-string — это статический текст: до первой
                # динамической переменной она часть головы, после — часть хвоста.
                (tail_parts if dynamic_seen else head_parts).append(resolved)
                continue
            if not dynamic_seen and not head_parts and not tail_parts:
                leading_dynamic = True
            elif head_parts or tail_parts:
                # Динамика ПОСЛЕ статического текста: хвостовой ``*`` нужен
                # независимо от того, текст ли это в голове или в хвосте.
                trailing_dynamic = True
            dynamic_seen = True
            continue
        literal = _const_str(part)
        if literal is None:
            continue
        (tail_parts if dynamic_seen else head_parts).append(literal)

    if not dynamic_seen:
        return None  # f-string без динамики — тот же literal-случай
    head = "".join(head_parts)
    tail = "".join(tail_parts)
    if "." not in head + tail:
        return None
    # Точный glob-паттерн: динамика слева/справа от статического текста даёт
    # ведущий/хвостовой ``*`` (и разделитель, если статический текст разорван).
    # Именно этот паттерн потом использует purge (свой адресный glob) — поэтому
    # он должен совпадать символ в символ.
    segments: list[str] = []
    if leading_dynamic:
        segments.append("*")
    if head:
        segments.append(head)
    if tail:
        segments.append(tail)
    if trailing_dynamic:
        segments.append("*")
    if head and tail:
        segments.insert(len(segments) - 1, "*")
    family = "".join(segments)
    return family if _is_narrow_family(family) else None


# Минимальная длина значимого статического сегмента. Отделяет «узкое семейство»
# (``.a52b2-restore-tmp-*`` — метка нашего формата) от голого ``*.ndjson``.
_NARROW_FAMILY_MIN_SEGMENT = 6


def _is_narrow_family(pattern: str) -> bool:
    """Узкое glob-семейство: есть статический маркер, а не только ``*``.

    Именно узкие семейства образуют отдельное хранилище, которое purge обязан
    вычистить адресно. Голые ``*.ext`` / ``*`` остаются за каталогом-родителем
    (как и раньше) — иначе каждое «сними всё содержимое» стало бы доказательством
    покрытия чего угодно.
    """
    if "*" not in pattern:
        return False
    for segment in re.split(r"\*", pattern):
        if len(segment) >= _NARROW_FAMILY_MIN_SEGMENT and "." in segment:
            return True
    return False


def _const_str_value(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Строка-константа: литерал либо ссылка на константу (для f-string головы)."""
    literal = _const_str(node)
    if literal is not None:
        return literal
    name = _name_of(node)
    if name is not None:
        return consts.get(name)
    return None


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
    # A5.2c1 (M1): a ROOT-level ``*.tmp`` / ``*_tmp`` sweep is a real family the
    # purge must clear.  It is NOT the "containing subdir" case the wildcard
    # skip below is about: there is no subdir, the whole data dir is the family.
    if "/" not in pattern:
        family = _temp_family(basename)
        if family is not None and basename == family:
            found.setdefault(pattern, StoreRef(pattern, module, f"{rel}:{lineno}"))
            return
    # A5.2c1: a ``*.bak*`` sweep is a NARROW family (the base name is static),
    # so it is a real store the purge must cover — recorded under its canonical
    # id so all variants (``bak``, ``bak1``, ``bak-<ts>``) collapse together.
    family = _backup_family(basename)
    if family is not None:
        found.setdefault(pattern, StoreRef(pattern, module, f"{rel}:{lineno}"))
        return
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
                    else:
                        # (a'') base_dir / f"{PREFIX}{x}" — узкое семейство без
                        # разрешённого расширения (A5.2b2 review B3).
                        family = _fstring_family(node.right, consts)
                        if family is not None:
                            found.setdefault(
                                _qualify(resolver.base_subpath(node), family),
                                StoreRef(
                                    _qualify(resolver.base_subpath(node), family),
                                    module,
                                    f"{rel}:{node.lineno}",
                                ),
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
            # (b') A5.2c1: ``<data-dir-rooted path>.with_suffix(".X.bak…")`` —
            # ПРОИЗВОДИТЕЛЬ backup-копии. Без этой ветки гейт был слеп к месту,
            # где копия РЕАЛЬНО создаётся (``migrate_history_encryption``
            # делает ``history.ndjson.with_suffix(".ndjson.bak")``): снос
            # sweep'а в purge проходил бы как «0 gaps», хотя на диске лежит
            # открытая копия истории.  Регистрируем ПРОИЗВОДИТЕЛЯ, а не
            # потребителя — иначе гейт сам себя оправдывал бы.
            if attr == "with_suffix" and node.args:
                suffix = _const_str(node.args[0])
                if suffix is not None and BACKUP_COPY_SUFFIX_RE.match(suffix):
                    # ``subpath`` here is the BASE FILE's path under data_dir
                    # (``_register_dir`` treats any ``self.X = data_dir / "y"``
                    # as a root, file or not).  ``with_suffix`` REPLACES the last
                    # extension: ``history.ndjson`` + ``.ndjson.bak`` → stem
                    # ``history`` + suffix = ``history.ndjson.bak``.
                    base_path = resolver.base_subpath(recv)
                    if not base_path:
                        continue
                    parent = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
                    base_name = base_path.rsplit("/", 1)[-1]
                    parts = base_name.rsplit(".", 1)
                    if len(parts) != 2 or parts[1] not in PERSIST_EXTENSIONS:
                        continue
                    store_id = _qualify(parent, parts[0] + suffix)
                    found.setdefault(
                        store_id, StoreRef(store_id, module, f"{rel}:{node.lineno}")
                    )
            # (b'') A5.2c1 (M1): ПРОИЗВОДИТЕЛЬ temp-копии. Две формы, обе в бою:
            #   * ``path.with_suffix(".json.tmp")``    (state_store.save_settings)
            #   * ``path + ".tmp"`` / ``stem + suffix + ".tmp"``
            #     (translation_cache, export_scheduler, usage_tracker, event_replay)
            # Регистрируем СЕМЕЙСТВО (``*.tmp`` / ``*_tmp``), потому что у
            # ``journal_path.with_suffix(".tmp")`` в _compact_unlocked база —
            # переменная цикла, и требование «удали конкретное имя» было бы
            # невыполнимо.  Требование к purge: явный sweep этого семейства.
            if attr == "with_suffix" and node.args:
                family = _temp_family(_const_str(node.args[0]) or "")
                if family is not None:
                    found.setdefault(
                        family, StoreRef(family, module, f"{rel}:{node.lineno}")
                    )
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                for side in (node.left, node.right):
                    family = _temp_family(_const_str(side) or "")
                    if family is not None:
                        found.setdefault(
                            family, StoreRef(family, module, f"{rel}:{node.lineno}")
                        )
                        break
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
                    else:
                        # (c') (base_dir / f"{PREFIX}{x}").mkdir(...) — каталог,
                        # имя которого строится в рантайме (A5.2b2 staging).
                        family = _fstring_family(recv.right, consts)
                        if family is not None:
                            store_id = _qualify(
                                resolver.base_subpath(recv), family
                            ).rstrip("/") + "/"
                            found.setdefault(
                                store_id,
                                StoreRef(store_id, module, f"{rel}:{node.lineno}"),
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


def _glob_pattern_const(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Разрешает аргумент ``glob(...)``: литерал ИЛИ f-string из констант.

    A5.2b2 review (B3): purge не может писать ``glob(".a52b2-restore-*")``
    литералом — префикс живёт в константе модуля, иначе пришлось бы дублировать
    его в сканере и в коде. Такие glob'ы просто переставали засчитываться как
    покрытие, и гейт снова становился слепым.
    """
    literal = _const_str(node)
    if literal is not None:
        return literal
    if not isinstance(node, ast.JoinedStr):
        return None
    chunks: list[str] = []
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            resolved = _const_str_value(part.value, consts)
            if resolved is None:
                return None  # настоящая динамика — паттерн неизвестен
            chunks.append(resolved)
            continue
        piece = _const_str(part)
        if piece is None:
            return None
        chunks.append(piece)
    return "".join(chunks)


def _loop_sources(func: ast.FunctionDef) -> dict[str, list[ast.AST]]:
    """Имя переменной цикла → элементы контейнера, который он итерирует.

    Нужно для journal-зачисток вида ``for name in _delta_journals: path / name`` —
    там id хранилища приходит из КОНТЕЙНЕРА, а не из тела, и без такого разбора
    требование «id среди аргументов» отвергло бы правильный код (ровно как
    разбираются контейнеры циклов в ``_state_store_compaction_coverage``).
    Контейнер умеется и встроенный, и через переменную — обе формы встречаются
    в purge.
    """
    assignments = _assigned_values(func)
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(func):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        source = node.iter
        if isinstance(source, ast.Name):
            resolved = assignments.get(source.id)
            source = resolved if isinstance(resolved, (ast.List, ast.Tuple)) else None
        if not isinstance(source, (ast.List, ast.Tuple)):
            continue
        for stmt in node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]:
            name = _name_of(stmt)
            if name is not None:
                out.setdefault(name, []).extend(source.elts)
    return out


def _expr_names_store(
    expr: ast.AST,
    store_id: str,
    assignments: dict[str, ast.AST],
    loop_sources: dict[str, list[ast.AST]] | None = None,
) -> bool:
    """Упоминается ли id хранилища в выражении (с разыменкой переменных).

    Нужен разбор переменных, потому что реальный код честно пишет
    ``_wipe_journal_ciphertext(_tombstones_path)``, где путь задан присваиванием
    ``_tombstones_path = Path(data_dir) / "history_tombstones.ndjson"``. Требование
    «id обязан быть АРГУМЕНТОМ» без такого разбора отвергло бы правильный код, а
    «id где-то в функции» — пропустило бы мёртвый вызов (LOW3).
    """
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Constant) and sub.value == store_id:
            return True
        if isinstance(sub, ast.Name):
            if loop_sources:
                for element in loop_sources.get(sub.id, []):
                    if _expr_names_store(element, store_id, assignments, loop_sources):
                        return True
            target = assignments.get(sub.id)
            if target is not None and target is not sub:
                if _expr_names_store(target, store_id, assignments, loop_sources):
                    return True
    return False


def _assigned_values(func: ast.FunctionDef) -> dict[str, ast.AST]:
    """Имя переменной → выражение, которым она присвоена (последнее присваивание)."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                name = _name_of(target)
                if name is not None:
                    out[name] = value
    return out


def _reachable_helpers(func: ast.FunctionDef) -> list[ast.FunctionDef]:
    """Модульные хелперы, вызываемые из тела ``func`` (один уровень).

    A5.2c1 (N1′): доказательство уничтожения может лежать в хелпере, а не в
    теле purge — вынос сносов журналов в ``_purge_wipe_managed_journals`` сделал
    снос невидимым гейту, и все proof-gated журналы «выпали» из покрытия. Поиск
    только по телу purge означал бы: любая рефакторинг-выноска способна тихо
    обнулить требование доказательства.
    """
    tree_module = getattr(func, "_krab_module", None)
    if tree_module is None:
        return []
    called: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _name_of(node.func)
            if name is not None:
                called.add(name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                called.add(alias.asname or alias.name)
    out: list[ast.FunctionDef] = []
    for candidate in ast.walk(tree_module):
        if (
            isinstance(candidate, ast.FunctionDef)
            and candidate.name in called
            and candidate.name != func.name
        ):
            out.append(candidate)
    return out


def _has_proof_for(func: ast.FunctionDef, store_id: str, proofs: tuple[str, ...]) -> bool:
    """Доказательство уничтожения шифротекста ИМЕННО для этого хранилища.

    Требуется и сам вызов, и id хранилища среди его аргументов (с разыменкой
    переменных и разбором контейнеров циклов) — иначе мёртвый вызов
    доказательства в любой части функции засчитывал бы настоящее хранилище.
    Ищется в теле purge И в модульных хелперах, которые purge вызывает.
    """
    return _has_proof_in(func, store_id, proofs) or any(
        _has_proof_in(helper, store_id, proofs)
        for helper in _reachable_helpers(func)
    )


def _has_proof_in(func: ast.FunctionDef, store_id: str, proofs: tuple[str, ...]) -> bool:
    assignments = _assigned_values(func)
    loop_sources = _loop_sources(func)
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = (
            fn.id
            if isinstance(fn, ast.Name)
            else fn.attr
            if isinstance(fn, ast.Attribute)
            else None
        )
        if called in proofs and any(
            _expr_names_store(arg, store_id, assignments, loop_sources) for arg in node.args
        ):
            return True
    return False


def _has_call_to(func: ast.FunctionDef, names: tuple[str, ...]) -> bool:
    """Есть ли в теле функции вызов одной из перечисленных функций.

    Проверяется именно ВЫЗОВ (``Name(...)`` / ``attr(...)``), а не любое
    упоминание: иначе импорт или комментарий засчитывались бы как доказательство.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in names:
            return True
        if isinstance(fn, ast.Attribute) and fn.attr in names:
            return True
    return False


# A5.2c1: журналы, чья приватность держится на УНИЧТОЖЕНИИ ШИФРОТЕКСТА, а не на
# самом факте упоминания пути. Покрытием для них считается только явная проверка
# уничтожения — см. фильтр в конце ``_collect_removed_names_in_function``.
#
# B1‴ добавил ``history_tombstones.ndjson``: purge пишет собственные томбестоны
# открытыми, но компактирование чистит журнал ТОЛЬКО при успехе, а провал
# компактирования — банальный сценарий. Без требования явного доказательства
# сняли бы зачистку 1b-3 молча, и гейт снова стал бы зелёным на собственном
# главном изменении.
_CIPHERTEXT_DESTRUCTION_STORES: dict[str, tuple[str, ...]] = {
    "history_purged_ids.ndjson": (
        "_ledger_holds_no_ciphertext",
        "_rematerialize_ledger_plaintext",
    ),
    "history_tombstones.ndjson": ("_wipe_journal_ciphertext",),
    # N1: `history.ndjson` сносится БЕЗУСЛОВНО только при провале compact
    # (шаг 1b-1), но в список не попадал, и покрытие кредитовалось из
    # УСЛОВНОГО компактирования. NC-B ревьюера: отключить 1b-1 → «0 gaps».
    "history.ndjson": ("_wipe_journal_ciphertext",),
    # N1 (проверено, а не предположено): прод-писатель — `_append_ndjson`
    # (state_store.py:2399), тот же шифрующий путь, поэтому ENC1 возможен.
    "history_calendar_links.ndjson": ("_wipe_journal_ciphertext",),
    # N1 (собственная находка, измерена пробой): остальные delta-журналы тоже
    # шифруются и чистятся компактированием только при успехе. Шаг 1b-4 сносит
    # их безусловно, и гейт обязан это требовать.
    "history_status.ndjson": ("_wipe_journal_ciphertext",),
    "history_tags.ndjson": ("_wipe_journal_ciphertext",),
    "history_favorites.ndjson": ("_wipe_journal_ciphertext",),
    "history_text_updates.ndjson": ("_wipe_journal_ciphertext",),
    "history_action_items.ndjson": ("_wipe_journal_ciphertext",),
    "history_annotations.ndjson": ("_wipe_journal_ciphertext",),
}

# Все функции-доказательства уничтожения шифротекста. Нужны цикловому разбору
# ниже: переменная цикла засчитывается, только если участвует в вызове
# доказательства.
_ALL_WIPE_PROOFS: frozenset[str] = frozenset(
    _name for _proofs in _CIPHERTEXT_DESTRUCTION_STORES.values() for _name in _proofs
)


def _loop_carried_store_ids(func: ast.FunctionDef) -> set[str]:
    """id хранилищ, приходящие из контейнера цикла, который purge реально чистит.

    Условие признака: переменная цикла участвует либо в построении пути
    (``... / var``), либо в вызове функции-доказательства уничтожения. Простое
    перечисление имён в цикле НЕ засчитывается — иначе гейт начал бы верить
    любым константам рядом с циклом.
    """
    found: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        loop_vars = {
            _name_of(stmt)
            for stmt in (
                node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
            )
        }
        loop_vars.discard(None)
        if not loop_vars:
            continue
        body_nodes = [sub for stmt in node.body for sub in ast.walk(stmt)]
        uses_path = any(
            isinstance(sub, ast.BinOp)
            and isinstance(sub.op, ast.Div)
            and any(
                isinstance(leaf, ast.Name) and leaf.id in loop_vars
                for leaf in ast.walk(sub)
            )
            for sub in body_nodes
        )
        uses_proof = any(
            isinstance(sub, ast.Call)
            and _name_of(sub.func) in _ALL_WIPE_PROOFS
            and any(
                isinstance(leaf, ast.Name) and leaf.id in loop_vars
                for arg in sub.args
                for leaf in ast.walk(arg)
            )
            for sub in body_nodes
        )
        if not (uses_path or uses_proof):
            continue
        for var in loop_vars:
            for element in _loop_sources(func).get(var, []):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    canon = _canonicalize(element.value)
                    if _looks_like_store_filename(canon):
                        found.add(canon)
    return found


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
            else:
                # Узкое f-string семейство: purge перечисляет его адресным
                # glob'ом, и это РОВНО то самое доказательство покрытия, что
                # ищет discovery (A5.2b2 review B3).
                family = _fstring_family(node.right, consts)
                if family is not None:
                    removed.add(family)

        # X.glob("pat") -> family / subdir enumerated for deletion
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "glob" and node.args:
                pattern = _glob_pattern_const(node.args[0], consts)
                if pattern is None:
                    continue
                # Раньше ведущий ``*`` отбрасывался: голый ``*.ext`` означает
                # «всё содержимое каталога», и засчитывать его как покрытие
                # чего-либо нельзя. Узкое семейство (``.a52b2-restore-tmp-*``)
                # адресно — его вычистка и ЕСТЬ доказательство покрытия.
                if not pattern.startswith("*"):
                    removed.add(pattern)
                elif _is_narrow_family(pattern):
                    removed.add(pattern)
                elif _temp_family(pattern) == pattern:
                    # A5.2c1 (M1): a root-level ``*.tmp`` / ``*_tmp`` sweep is the
                    # proof of coverage for exactly that family — the same
                    # relationship ``history.ndjson.bak*`` has to
                    # ``history.ndjson.bak``.  The blanket "leading ``*`` means
                    # the whole directory, so it proves nothing" rule must yield
                    # here: the discovery side now has a canonical id for this
                    # family, and refusing to credit it would leave the family
                    # permanently uncovered (i.e. the guard would demand a sweep
                    # that can never be recognised, and every future purge
                    # would be reported as a gap).
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
    # N1: id, приходящие из КОНТЕЙНЕРА цикла (``for name in _delta_journals:
    # _wipe_journal_ciphertext(Path(data_dir) / name)``). Раньше такие id в
    # ``removed`` не попадали — правый операнд деления не литерал — и журнал
    # покрывался ТОЛЬКО кредитом условного компактирования. Требование
    # «literal id, иначе не видно» здесь само было дырой гейта: правильная
    # зачистка выглядела как её отсутствие. Добавляем ДО proof-фильтра ниже,
    # иначе фильтр их не увидит и снятие зачистки снова останется зелёным.
    removed |= _loop_carried_store_ids(func)

    # A5.2c1: упоминание пути ≠ уничтожение шифротекста. Pre-flight «ledger —
    # symlink вместо файла профиля» адресует `history_purged_ids.ndjson`, но лишь
    # снимает ссылку; ENC1-строки он не трогает. Пока такое упоминание
    # засчитывалось как покрытие, удаление 37a (перематериализация + проверка
    # «шифротекста не осталось») снова сделало бы гейт зелёным — то есть гейт
    # перестал бы ловить собственное главное изменение. Ровно тот класс
    # «проводка есть, а гейт зелёный», который закрывали в b2/b3/M2.
    for _store_id, _proofs in _CIPHERTEXT_DESTRUCTION_STORES.items():
        if _store_id in removed and not _has_proof_for(func, _store_id, _proofs):
            removed.discard(_store_id)

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
    calls ``compact_with_stats``, so these are physically cleared.

    A5.2c1 (M2): «упоминается в теле compact'а» больше НЕ равно «очищается».
    Прежняя версия засчитывала ЛЮБОЕ упоминание атрибута-журнала, включая
    ``purged_ids_path``, куда идёт ТОЛЬКО ``append`` + ``fsync``. Из-за этого
    удаление шага 37a из purge (то есть самое главное изменение волны) проходило
    как «0 gaps»: permanent ledger был «покрыт» правдоподобным, но неверным
    основанием — negative control NC2 ревьюера давал exit 0.

    Засчитываются только журналы, которые РЕАЛЬНО усекаются или заменяются:

    1. цель ``X.replace(...)`` / ``X.unlink()`` / ``X.write_text(...)`` /
       ``X.open("w")`` — прямое усечение журнала;
    2. переменная цикла, которая является целью ``replace``/``unlink`` в теле
       цикла, — а сам цикл итерируется по списку/кортежу атрибутов
       (``for journal_path in [tombstones, status, tags, ...]``) или по словарю
       (``for journal_path, prepared in surviving_lines.items()``). Именно так
       усекаются delta-журналы: база приходит из КОНТЕЙНЕРА цикла, а не из
       тела.
    """
    tree = _parse(STATE_STORE)
    consts = collect_string_constants(tree)
    attr_filenames = _module_attr_filenames(tree, consts)

    def _journal_attr(node: ast.AST) -> str | None:
        """Имя атрибута-журнала, если узел разрешается в известный файл."""
        attr = _name_of(node)
        if attr in attr_filenames:
            return attr
        return None

    def _clear_target_names(nodes: list[ast.AST]) -> set[str]:
        """Имена (атрибуты И переменные), стоящие ЦЕЛЬЮ очистки.

        Цель — это то, что ``replace``/``unlink``/``write_text`` ПИШЕТ:
        ``tmp.replace(journal_path)`` усекает ``journal_path``, а ``tmp`` —
        временный файл, а не журнал.
        """
        names: set[str] = set()
        for sub in nodes:
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            op = sub.func.attr
            if op == "unlink":
                cands: list[ast.AST] = [sub.func.value]
            elif op == "replace":
                # Path.replace(dest): dest=args[0]; os.replace(src, dest): dest=args[1]
                cands = list(sub.args)
            elif op in {"write_text", "write_bytes"}:
                cands = [sub.func.value]
            elif op == "open" and sub.args:
                # Только усекающий режим.  ``with purged_ids_path.open("r+b")``
                # в compact'е — это fsync, а не очистка: такой open не имеет
                # права засчитывать журнал (M2).
                mode = _const_str(sub.args[0]) if isinstance(sub.args[0], ast.Constant) else None
                if mode is None or not mode.startswith("w"):
                    continue
                cands = [sub.func.value]
            else:
                continue
            for cand in cands:
                name = _name_of(cand)
                if name is not None:
                    names.add(name)
        return names

    def _loop_target_names(node: ast.AST) -> set[str]:
        """Переменные, в которые цикл пишет (``for x`` / ``for x, y``)."""
        targets: set[str] = set()
        for stmt in (node.target,):
            for sub in ast.walk(stmt):
                name = _name_of(sub)
                if name is not None:
                    targets.add(name)
        return targets

    def _container_attrs(node: ast.AST) -> set[str]:
        """Атрибуты-журналы, перечисленные в ИСТОЧНИКЕ цикла (list/tuple/dict)."""
        attrs: set[str] = set()
        source = node.iter
        if isinstance(source, (ast.List, ast.Tuple)):
            for elt in getattr(source, "elts", []):
                attr = _journal_attr(elt)
                if attr is not None:
                    attrs.add(attr)
        elif isinstance(source, ast.Dict):
            for key in source.keys:
                if key is None:
                    continue
                attr = _journal_attr(key)
                if attr is not None:
                    attrs.add(attr)
        return attrs

    def _add(attr: str) -> None:
        fname = _canonicalize(attr_filenames[attr])
        if _looks_like_store_filename(fname):
            cleared.add(fname)

    cleared: set[str] = set()
    for fn_name in ("_compact_unlocked", "compact_with_stats"):
        fn = _find_function(tree, fn_name)
        if fn is None:
            continue

        all_nodes = list(ast.walk(fn))
        clear_targets = _clear_target_names(all_nodes)

        # (1) журнал стоит целью очистки прямо в теле (self.history_path в
        #     ``tmp_history.replace(self.history_path)``).
        for attr in clear_targets & set(attr_filenames):
            _add(attr)

        # ``D[var] = ...`` — какие словари наполняются внутри функции; нужно,
        # чтобы поймать ключи, приходящие из контейнера ДРУГОГО цикла.
        subscript_sources: dict[str, set[str]] = {}
        for sub in all_nodes:
            if (
                isinstance(sub, ast.Assign)
                and isinstance(sub.targets[0], ast.Subscript)
                and isinstance(sub.targets[0].value, ast.Name)
            ):
                idx = sub.targets[0].slice
                name = _name_of(idx)
                if name is not None:
                    subscript_sources.setdefault(
                        sub.targets[0].value.id, set()
                    ).add(name)

        for node in all_nodes:
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            loop_vars = _loop_target_names(node)
            if not (loop_vars & clear_targets):
                continue
            # (2a) журналы приходят прямо из контейнера этого цикла.
            for attr in _container_attrs(node):
                _add(attr)
            # (2b) ``for journal_path, prepared in surviving_lines.items():`` —
            #      ключи — из словаря, который наполняет ДРУГОЙ цикл; имя
            #      переменной связывает их.
            source = node.iter
            if (
                isinstance(source, ast.Call)
                and isinstance(source.func, ast.Attribute)
                and source.func.attr == "items"
                and isinstance(source.func.value, ast.Name)
            ):
                for key_var in subscript_sources.get(source.func.value.id, set()):
                    for other in all_nodes:
                        if (
                            isinstance(other, (ast.For, ast.AsyncFor))
                            and key_var in _loop_target_names(other)
                        ):
                            for attr in _container_attrs(other):
                                _add(attr)
    return cleared


def extract_purge_coverage() -> set[str]:
    """Return the full set of store ids cleared by ``handle_purge_all_data``."""
    hs_tree = _parse(HISTORY_SERVICE)
    hs_consts = collect_string_constants(hs_tree)
    hs_attrs = _module_attr_filenames(hs_tree, hs_consts)
    purge_fn = _find_function(hs_tree, "handle_purge_all_data")
    # A5.2c1 (N1′): доказательство может лежать в хелпере, вызываемом из purge,
    # поэтому функции нужна ссылка на модуль (см. `_reachable_helpers`).
    if purge_fn is not None:
        purge_fn._krab_module = hs_tree  # type: ignore[attr-defined]
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
    #
    # B1‴: для журналов из ``_CIPHERTEXT_DESTRUCTION_STORES`` кредит компактирования
    # НЕ засчитывается. Компактирование чистит их УСЛОВНО — «если не упало»,
    # и именно этот провал оставлял ENC1-строки, которые purge уже не мог
    # уничтожить (шаг shred'а ключа). Пока такое кредитование живёт, снятие
    # безусловной зачистки 1b-3 проходило как «0 gaps»: гейт закрывал
    # условный шаг и требовал доказательства от безусловного.
    covered |= _state_store_compaction_coverage() - set(_CIPHERTEXT_DESTRUCTION_STORES)

    # (d) module-level helpers, которые тело purge вызывает (в т.ч. через
    # локальный импорт) — A5.2b2 review B3.
    covered |= _local_helper_purge_coverage(purge_fn)

    # Canonicalise filename ids (strip a *filename* ``.tmp``) but preserve the
    # ``*.ext`` / ``*`` extension-family markers verbatim.
    # A5.2c1 (M1): temp-family markers (``*.tmp`` / ``*_tmp``) must ALSO be kept
    # verbatim — they are FAMILIES, not filenames, and canonicalising them
    # collapsed ``*.tmp`` into a bare ``*``.  That bare ``*`` then covered
    # nothing while looking like coverage, i.e. the pool and the discovered id
    # stopped matching on a family the purge really does sweep.  Note the
    # asymmetry with a real filename: for ``settings.json.tmp`` collapsing onto
    # ``settings.json`` is CORRECT (it is a copy of that file, and the reader
    # of the copy is the purge of the file); for the family marker it is not.
    # A5.2c1 (N1′): единый proof-фильтр по ИТОГОВОМУ покрытию — где бы ни
    # появилось покрытие (тело purge, его хелперы, коллабораторы), хранилище из
    # `_CIPHERTEXT_DESTRUCTION_STORES` без доказуемого уничтожения шифротекста не
    # считается покрытым. Раньше фильтр жил только внутри тела purge, из-за
    # чего вынос сноса в хелпер тихо обнулял требование.
    for _store_id, _proofs in _CIPHERTEXT_DESTRUCTION_STORES.items():
        if _store_id in covered and not _has_proof_for(purge_fn, _store_id, _proofs):
            covered.discard(_store_id)

    return {
        c
        if c.endswith("*") or "/*." in c or _temp_family(c) == c
        else _canonicalize(c)
        for c in covered
    }


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
      5. **A5.2c1 backup-copy family:** a ``.bak`` store is covered when the pool
         names the SAME family — a purge sweep of ``history.ndjson.bak*`` covers
         the producer-discovered ``history.ndjson.bak`` and vice versa.  The
         family is matched by canonical id, so ``bak`` / ``bak1`` / ``bak-<ts>``
         cannot each sneak past a sweep that only removed one variant.
         A5.2c1 (L1): the pool entry must ITSELF be a family — contain ``*`` —
         or be the exact ``store_id``.  Requiring only a shared family id let a
         non-wildcard sweep (``history.ndjson.bak``, NC3 of the review) close the
         whole family while every timestamped copy survived.  A single member is
         evidence about itself only (rule 1), never about its family.
    """
    pool = covered | allowlisted
    # (5) A5.2c1: backup-copy family — match on the canonical ``<base>.bak*`` id.
    #     L1: пустая запись пула (без wildcard) права закрыть семейство не имеет.
    own_family = _backup_family(store_id.rsplit("/", 1)[-1])
    if own_family is not None:
        for entry in pool:
            if entry.endswith("/"):
                continue
            entry_base = entry.rsplit("/", 1)[-1]
            if "*" not in entry_base and entry_base != store_id.rsplit("/", 1)[-1]:
                continue
            if _backup_family(entry_base) == own_family:
                return True
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


def _local_helper_purge_coverage(purge_fn: ast.FunctionDef) -> set[str]:
    """Coverage from module-level helpers CALLED (or locally imported) by the purge.

    A5.2b2 review (B3): the purge body delegates to a module-level helper
    (``purge_pending_restore_staging``), and the guard only ever looked at its
    OWN body plus ``self._X.purge_all()`` collaborators. A helper reached by a
    bare call was invisible in BOTH directions — which is exactly why "0 gaps"
    stayed green even with the wiring removed. Following one call level makes the
    gate test what it claims to test.
    """
    covered: set[str] = set()
    # A5.2c1 (N1′): хелперы ТОГО ЖЕ модуля. Раньше путь вел только к хелперам
    # ДРУГИХ модулей (через локальный импорт), поэтому вынос сносов журналов в
    # модульный `_purge_wipe_managed_journals` сделал их невидимыми гейту: все
    # proof-gated журналы выпали из покрытия. Рефакторинг-выноска не должна
    # обнулять требование доказательства.
    _module = getattr(purge_fn, "_krab_module", None)
    if _module is not None:
        _mconsts = collect_string_constants(_module)
        _mattrs = _module_attr_filenames(_module, _mconsts)
        for _helper in _reachable_helpers(purge_fn):
            covered |= _collect_removed_names_in_function(_helper, _mconsts, _mattrs)
    # Локальные импорты ВНУТРИ тела purge: ``from backend.X import name``.
    local_imports: dict[str, str] = {}
    for node in ast.walk(purge_fn):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("backend."):
            continue
        module_stem = node.module.rsplit(".", 1)[-1]
        for alias in node.names:
            local_imports[alias.asname or alias.name] = module_stem
    if not local_imports:
        return covered
    called = {
        node.func.id
        for node in ast.walk(purge_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for name, module_stem in local_imports.items():
        if name not in called:
            continue
        module_path = BACKEND_DIR / f"{module_stem}.py"
        if not module_path.exists():
            continue  # нет модуля — покрытие не засчитываем (fail-closed)
        tree = _parse(module_path)
        consts = collect_string_constants(tree)
        module_attrs = _module_attr_filenames(tree, consts)
        helper = _find_function(tree, name)
        if helper is None:
            continue
        covered |= _collect_removed_names_in_function(helper, consts, module_attrs)
    return covered


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
