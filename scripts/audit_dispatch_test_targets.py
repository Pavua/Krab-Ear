#!/usr/bin/env python3
"""audit_dispatch_test_targets.py — root-cause guard for the "test validates
the dead in-class copy" bug class (telegram #44 / wave-20).

ROOT CAUSE
==========
``backend/service.py`` (``BackendService``) extracted ~17 services out of the
monolith.  The LIVE IPC router is ``self._dispatch_table``, built ONCE in
``BackendService._build_dispatch_table()`` (a single ``return {...}`` dict
literal, after every collaborator/service is constructed).  Many handlers were
MOVED to extracted services — e.g. ``"send_to_telegram"`` now routes to
``self._apple_integration_svc.handle_send_to_telegram`` — but a DEAD in-class
copy ``BackendService._handle_send_to_telegram`` was left behind, with ZERO
production callers (only its own ``def`` line + tests).

THE DANGER (real, found in wave-20)
-----------------------------------
The W1211 privacy-guard test (``test_telegram_bridge_w1211.py``) AST-inspects
the BODY of the DEAD in-class ``_handle_list_telegram_chats`` for a
``privacy_mode_enabled`` guard, and a dispatch-invariant test references the
in-class ``_handle_*`` name — but PRODUCTION routes ``list_telegram_chats`` to
the EXTRACTED ``apple_integration_service`` copy.  So a developer can delete the
privacy guard from the LIVE extracted copy and EVERY telegram test stays green
while transcript text silently flows to Telegram.  The test validates a copy
production never runs.

WHAT THIS SCRIPT FINDS
======================
1. **dead_duplicate** — an in-class ``BackendService._handle_<X>`` method that is
   a dead shadow: (a) the live dispatch entry for the corresponding IPC method
   ``<X>`` routes to an EXTRACTED service (``self._<svc>.handle_<X>`` /
   ``self._<svc>.<fn>``), NOT to this in-class method, AND (b) the in-class
   method has no non-test production caller (``self._handle_<X>`` anywhere under
   ``KrabEar/`` excluding ``tests/`` and the method's own ``def`` line).  Such a
   method exists only so tests can validate it instead of the live path.

2. **test sites** — for every dead-duplicate ``<X>``, the tests under
   ``KrabEar/tests/**`` that reference the dead in-class ``_handle_<X>`` name
   (string literal ``"_handle_<X>"``, attribute ``._handle_<X>``,
   ``getattr(..., "_handle_<X>")``, ``inspect.getsource(... _handle_<X>)``, or a
   bare identifier ``_handle_<X>``).  These tests validate the dead copy and must
   be repointed at the extracted service (and ideally exercised THROUGH the
   dispatch table so they bind to the live target).

AST-based for service.py (the dispatch dict is parsed, not regex'd).  The
test-reference scan is grep-style (literal + attribute + getattr + getsource).

Usage::

    python3 scripts/audit_dispatch_test_targets.py                 # report, exit 0
    python3 scripts/audit_dispatch_test_targets.py --json          # machine output
    python3 scripts/audit_dispatch_test_targets.py --fail-on-found  # exit 1 if any
    python3 scripts/audit_dispatch_test_targets.py --selftest       # known-bad/good

Exit 0 → no non-allowlisted finding (or report-only mode).
Exit 1 → ``--fail-on-found`` and at least one non-allowlisted finding.

Allowlist: ``scripts/dispatch_test_targets_allowlist.txt`` (one id per line,
``# reason`` comments allowed).  Two id forms::

    method:send_to_telegram                 # whole method pair intentional
    test:_handle_send_to_telegram@tests/test_x.py   # one method@testfile site

House style mirrors ``scripts/audit_dead_extracted_modules.py`` +
``scripts/audit_purge_coverage.py`` (argparse, --fail-on-found, --json,
allowlist file, report-only exit 0 by default).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
SERVICE_PY = KRAB_EAR / "backend" / "service.py"
TESTS_DIR = KRAB_EAR / "tests"
ALLOWLIST_FILE = REPO_ROOT / "scripts" / "dispatch_test_targets_allowlist.txt"

# The monolith class whose in-class ``_handle_*`` copies can shadow an extracted
# service handler.  (BackendService is the only known one; kept as a constant
# so the intent is explicit.)
MONOLITH_CLASS = "BackendService"

# Directories to skip when counting non-test PRODUCTION callers of an in-class
# handler.  Tests are excluded on purpose: a handler reachable only from tests is
# exactly the dead copy we are hunting.
_PROD_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".venv_krab_ear",
        ".venv",
        "venv",
        ".venv_gigaam",
        ".venv_vl",
        "worktrees",
        ".claude",
        ".git",
        "__pycache__",
        "dist",
        "build",
        ".eggs",
        "node_modules",
        "tests",
    }
)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------
@dataclass
class TestSite:
    """A test that references a dead in-class ``_handle_<X>`` name."""

    test_file: str          # "tests/test_x.py" (repo-relative)
    line: int
    kind: str               # literal | attribute | getattr | getsource | identifier
    snippet: str            # trimmed source line


@dataclass
class DeadDuplicate:
    """An in-class ``_handle_<X>`` that is a dead shadow of an extracted handler."""

    method: str                     # IPC method name, e.g. "send_to_telegram"
    handler: str                    # in-class name, e.g. "_handle_send_to_telegram"
    inclass_location: str           # "backend/service.py:LINE"
    inclass_line: int
    live_target: str                # "self._apple_integration_svc.handle_send_to_telegram"
    live_service_attr: str          # "_apple_integration_svc"
    test_sites: List[TestSite] = field(default_factory=list)


@dataclass
class AuditResult:
    dead_duplicates: List[DeadDuplicate] = field(default_factory=list)
    allowlisted_methods: Set[str] = field(default_factory=set)
    allowlisted_sites: Set[str] = field(default_factory=set)
    # findings after allowlist filtering
    flagged: List[DeadDuplicate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# ...`` comment from a source line.

    Best-effort (string-literal aware enough for caller detection): a ``#`` that
    is not inside a quote starts the comment.  Used by the production-caller scan
    so a handler name mentioned in a comment is never miscounted as a live call.
    """
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _classify_target(value: ast.AST) -> Tuple[str, Optional[str], Optional[str]]:
    """Classify a dispatch-table RHS expression.

    Returns ``(kind, service_attr, fn_attr)`` where ``kind`` is one of:
      - ``"inclass"``   : ``self._handle_x``       → service_attr=None, fn_attr="_handle_x"
      - ``"extracted"`` : ``self._svc.handle_x``   → service_attr="_svc", fn_attr="handle_x"
      - ``"lambda"``    : a lambda expression
      - ``"other"``     : anything else (call, name, dict, ...)
    """
    if isinstance(value, ast.Lambda):
        return ("lambda", None, None)
    # self._svc.handle_x  →  Attribute(value=Attribute(value=Name('self')))
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Attribute)
        and isinstance(value.value.value, ast.Name)
        and value.value.value.id == "self"
    ):
        return ("extracted", value.value.attr, value.attr)
    # self._handle_x  →  Attribute(value=Name('self'))
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
    ):
        return ("inclass", None, value.attr)
    return ("other", None, None)


def _find_class(tree: ast.Module, name: str) -> Optional[ast.ClassDef]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _extract_dispatch_table(
    cls: ast.ClassDef,
) -> Dict[str, Tuple[str, Optional[str], Optional[str]]]:
    """Return ``{method: (kind, service_attr, fn_attr)}`` for the live dispatch
    table built in ``_build_dispatch_table`` (its single ``return {...}`` dict)
    PLUS any ``self._dispatch_table = {...}`` / ``self._dispatch_table[...] = ...``
    inline assignment anywhere in the class (defence-in-depth; none exist today).

    The dict literal is parsed via AST — never regex'd — so an entry only counts
    if its key is a string constant.  A later assignment for the same method
    overrides an earlier one (matches Python dict-build order).
    """
    table: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}

    def _absorb_dict(dnode: ast.Dict) -> None:
        for k, v in zip(dnode.keys, dnode.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                table[k.value] = _classify_target(v)

    # 1) the canonical builder method's `return {...}`.
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "_build_dispatch_table"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    _absorb_dict(sub.value)

    # 2) any inline `self._dispatch_table = {...}` / subscript assignment.
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                # self._dispatch_table = {...}
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "_dispatch_table"
                    and isinstance(node.value, ast.Dict)
                ):
                    _absorb_dict(node.value)
                # self._dispatch_table["method"] = <target>
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr == "_dispatch_table"
                ):
                    key = tgt.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        table[key.value] = _classify_target(node.value)
    return table


def _inclass_handler_defs(cls: ast.ClassDef) -> Dict[str, int]:
    """``{_handle_x: lineno}`` for every in-class ``_handle_*`` method def."""
    out: Dict[str, int] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_handle_"):
                out[node.name] = node.lineno
    return out


# ---------------------------------------------------------------------------
# Production-caller check (step 2b)
# ---------------------------------------------------------------------------
def _iter_prod_py_files() -> List[Path]:
    """All ``.py`` under ``KrabEar/`` excluding tests + venvs + caches."""
    out: List[Path] = []
    for path in KRAB_EAR.rglob("*.py"):
        parts = set(path.relative_to(KRAB_EAR).parts)
        if parts & _PROD_SKIP_DIRS:
            continue
        out.append(path)
    return out


def _build_prod_caller_index(handlers: Set[str]) -> Dict[str, int]:
    """Count non-test production references of ``self.<handler>`` for each
    handler, EXCLUDING the method's own ``def`` line in ``service.py``.

    A reference is any textual ``self._handle_x`` occurrence (outside a comment)
    that is NOT the ``def _handle_x`` line.  (The dispatch table routes these
    methods ELSEWHERE, so a surviving ``self._handle_x`` call means an internal
    in-process caller — proof the method is still live and therefore NOT a dead
    duplicate.)  The trailing ``# ...`` comment is stripped before matching so a
    mention of the handler name inside a comment never counts as a live call.
    """
    counts: Dict[str, int] = {h: 0 for h in handlers}
    pats = {h: re.compile(r"self\.%s\b" % re.escape(h)) for h in handlers}
    defpats = {h: re.compile(r"\bdef\s+%s\b" % re.escape(h)) for h in handlers}
    for path in _iter_prod_py_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - defensive
            continue
        for line in text.splitlines():
            code = _strip_comment(line)
            for h, pat in pats.items():
                if pat.search(code) and not defpats[h].search(code):
                    counts[h] += 1
    return counts


# ---------------------------------------------------------------------------
# Test-reference scan (step 3)
# ---------------------------------------------------------------------------
def _scan_test_references(handlers: Set[str]) -> Dict[str, List[TestSite]]:
    """For each dead handler name, find every test site that references it.

    Reference kinds detected per source line:
      - ``getsource``  : ``inspect.getsource(... _handle_x ...)`` / the line both
        names ``getsource`` and the handler (AST-body inspection of the dead copy)
      - ``getattr``    : ``getattr(obj, "_handle_x")`` (string arg form)
      - ``literal``    : the bare string literal ``"_handle_x"`` / ``'_handle_x'``
      - ``attribute``  : ``.{_handle_x}`` attribute access (``BackendService._handle_x``)
      - ``identifier`` : a bare ``_handle_x`` token not covered above

    One TestSite per (file, line, handler) — the first matching kind in the
    precedence order above is recorded (most-specific first).
    """
    out: Dict[str, List[TestSite]] = {h: [] for h in handlers}
    if not TESTS_DIR.exists():
        return out

    # Pre-compile per-handler matchers.
    lit_pats = {h: re.compile(r"""["']%s["']""" % re.escape(h)) for h in handlers}
    attr_pats = {h: re.compile(r"\.%s\b" % re.escape(h)) for h in handlers}
    ident_pats = {h: re.compile(r"(?<![\w'\"])%s\b" % re.escape(h)) for h in handlers}

    for path in sorted(TESTS_DIR.rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - defensive
            continue
        rel = _rel(path)
        for idx, line in enumerate(lines, start=1):
            for h in handlers:
                if h not in line:
                    continue
                has_lit = bool(lit_pats[h].search(line))
                has_attr = bool(attr_pats[h].search(line))
                has_ident = bool(ident_pats[h].search(line))
                if not (has_lit or has_attr or has_ident):
                    continue
                if "getsource" in line:
                    kind = "getsource"
                elif "getattr" in line and has_lit:
                    kind = "getattr"
                elif has_lit:
                    kind = "literal"
                elif has_attr:
                    kind = "attribute"
                else:
                    kind = "identifier"
                out[h].append(
                    TestSite(
                        test_file=rel,
                        line=idx,
                        kind=kind,
                        snippet=line.strip()[:160],
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def load_allowlist(path: Path = ALLOWLIST_FILE) -> Tuple[Set[str], Set[str]]:
    """Return ``(allowed_methods, allowed_sites)``.

    ``allowed_methods``: IPC method names from ``method:<name>`` lines.
    ``allowed_sites``:   ``<handler>@<testfile>`` from ``test:<id>`` lines.
    """
    methods: Set[str] = set()
    sites: Set[str] = set()
    if not path.exists():
        return methods, sites
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("method:"):
            methods.add(line[len("method:"):].strip())
        elif line.startswith("test:"):
            sites.add(line[len("test:"):].strip())
    return methods, sites


# ---------------------------------------------------------------------------
# Audit orchestration
# ---------------------------------------------------------------------------
def _audit_source(
    service_path: Path,
    test_refs: Dict[str, List[TestSite]],
    prod_caller_counts: Dict[str, int],
) -> List[DeadDuplicate]:
    """Core detector — pure over its inputs so ``--selftest`` can reuse it.

    A dead-duplicate is emitted iff BOTH:
      (a) the live dispatch entry for ``<method>`` routes to an extracted service
          (kind == "extracted"), AND the in-class ``_handle_<method>`` def exists;
      (b) ``prod_caller_counts[_handle_<method>] == 0`` (no non-test caller).
    """
    tree = _parse(service_path)
    cls = _find_class(tree, MONOLITH_CLASS)
    if cls is None:
        raise SystemExit(
            f"audit_dispatch_test_targets: class {MONOLITH_CLASS} not found in "
            f"{_rel(service_path)} — guard cannot run."
        )

    table = _extract_dispatch_table(cls)
    inclass = _inclass_handler_defs(cls)

    findings: List[DeadDuplicate] = []
    for method, (kind, svc_attr, fn_attr) in sorted(table.items()):
        if kind != "extracted":
            continue
        handler = "_handle_" + method
        if handler not in inclass:
            continue  # no in-class shadow → nothing dead
        if prod_caller_counts.get(handler, 0) != 0:
            continue  # still has a non-test production caller → live, not dead
        live_target = "self.%s.%s" % (svc_attr, fn_attr)
        findings.append(
            DeadDuplicate(
                method=method,
                handler=handler,
                inclass_location="%s:%d" % (_rel(service_path), inclass[handler]),
                inclass_line=inclass[handler],
                live_target=live_target,
                live_service_attr=svc_attr or "",
                test_sites=sorted(
                    test_refs.get(handler, []), key=lambda s: (s.test_file, s.line)
                ),
            )
        )
    return findings


def run_audit() -> AuditResult:
    # First parse to learn the in-class handler set we must probe for callers.
    tree = _parse(SERVICE_PY)
    cls = _find_class(tree, MONOLITH_CLASS)
    if cls is None:
        raise SystemExit(
            f"audit_dispatch_test_targets: class {MONOLITH_CLASS} not found in "
            f"{_rel(SERVICE_PY)} — guard cannot run."
        )
    inclass = _inclass_handler_defs(cls)
    handler_names = set(inclass.keys())

    prod_caller_counts = _build_prod_caller_index(handler_names)
    test_refs = _scan_test_references(handler_names)

    dead = _audit_source(SERVICE_PY, test_refs, prod_caller_counts)

    allowed_methods, allowed_sites = load_allowlist()

    # Apply allowlist: drop whole methods; drop individual test sites.
    def _site_allowlisted(handler: str, test_file: str) -> bool:
        # Accept the full repo-relative form (``KrabEar/tests/x.py``) and the
        # ``tests/x.py`` suffix form so allowlist entries can be written either way.
        candidates = {test_file}
        if test_file.startswith("KrabEar/"):
            candidates.add(test_file[len("KrabEar/"):])
        return any("%s@%s" % (handler, c) in allowed_sites for c in candidates)

    flagged: List[DeadDuplicate] = []
    for dd in dead:
        if dd.method in allowed_methods:
            continue
        kept_sites = [
            s for s in dd.test_sites if not _site_allowlisted(dd.handler, s.test_file)
        ]
        flagged.append(
            DeadDuplicate(
                method=dd.method,
                handler=dd.handler,
                inclass_location=dd.inclass_location,
                inclass_line=dd.inclass_line,
                live_target=dd.live_target,
                live_service_attr=dd.live_service_attr,
                test_sites=kept_sites,
            )
        )

    return AuditResult(
        dead_duplicates=dead,
        allowlisted_methods=allowed_methods,
        allowlisted_sites=allowed_sites,
        flagged=sorted(flagged, key=lambda d: d.method),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_report(result: AuditResult) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("DISPATCH-TEST-TARGET AUDIT (test-validates-the-dead-copy guard)")
    lines.append("=" * 78)
    n_dead = len(result.dead_duplicates)
    n_flagged = len(result.flagged)
    n_with_tests = sum(1 for d in result.flagged if d.test_sites)
    lines.append(f"dead in-class duplicates : {n_dead}")
    lines.append(f"  allowlisted methods    : {len(result.allowlisted_methods)}")
    lines.append(f"  flagged (after allow)  : {n_flagged}")
    lines.append(f"  of which validated by tests : {n_with_tests}")
    lines.append("")

    if not n_flagged:
        lines.append("OK — no dead in-class handler shadows a live extracted target")
        lines.append("(or all are allowlisted).")
        return "\n".join(lines)

    lines.append("Each in-class BackendService._handle_<X> below is DEAD (no non-test")
    lines.append("production caller) while the live dispatch table routes <X> to an")
    lines.append("EXTRACTED service.  Tests that reference the in-class name validate a")
    lines.append("copy production never runs — repoint them at the live target and")
    lines.append("exercise the method THROUGH the dispatch table.")
    lines.append("")
    for dd in result.flagged:
        lines.append(f"  [{dd.method}]")
        lines.append(f"      dead in-class : {dd.inclass_location}  ({dd.handler})")
        lines.append(f"      LIVE target   : {dd.live_target}")
        if not dd.test_sites:
            lines.append("      tests         : (none reference the dead copy)")
        else:
            lines.append(
                f"      tests validating the dead copy ({len(dd.test_sites)}):"
            )
            for s in dd.test_sites:
                lines.append(f"        - {s.test_file}:{s.line}  [{s.kind}]  {s.snippet}")
        lines.append("")

    lines.append("Fix: delete the dead in-class method, repoint the listed tests at the")
    lines.append("extracted handler (ideally via BackendService.handle_request through")
    lines.append("the dispatch table), or allowlist an intentional pair/site in")
    lines.append(f"  {_rel(ALLOWLIST_FILE)}  with a # reason.")
    return "\n".join(lines)


def format_json(result: AuditResult) -> str:
    def _dd(dd: DeadDuplicate) -> dict:
        return {
            "method": dd.method,
            "handler": dd.handler,
            "inclass_location": dd.inclass_location,
            "live_target": dd.live_target,
            "live_service_attr": dd.live_service_attr,
            "test_sites": [
                {
                    "test_file": s.test_file,
                    "line": s.line,
                    "kind": s.kind,
                    "snippet": s.snippet,
                }
                for s in dd.test_sites
            ],
        }

    payload = {
        "summary": {
            "dead_duplicates": len(result.dead_duplicates),
            "flagged": len(result.flagged),
            "flagged_with_tests": sum(1 for d in result.flagged if d.test_sites),
            "allowlisted_methods": len(result.allowlisted_methods),
            "allowlisted_sites": len(result.allowlisted_sites),
        },
        "flagged": [_dd(d) for d in result.flagged],
        "all_dead_duplicates": [_dd(d) for d in result.dead_duplicates],
        "allowlisted_methods": sorted(result.allowlisted_methods),
        "allowlisted_sites": sorted(result.allowlisted_sites),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Self-test (known-bad + known-good in-memory snippets)
# ---------------------------------------------------------------------------
_SELFTEST_BAD = '''
class BackendService:
    def _build_dispatch_table(self):
        return {
            "send_to_telegram": self._apple_svc.handle_send_to_telegram,
            "ping": self._handle_ping,
        }

    def _handle_send_to_telegram(self, params):
        # DEAD shadow: dispatch routes send_to_telegram to _apple_svc above,
        # and nothing calls self._handle_send_to_telegram.
        return {}

    def _handle_ping(self, params):
        return {"ok": True}
'''

_SELFTEST_GOOD = '''
class BackendService:
    def _build_dispatch_table(self):
        return {
            "ping": self._handle_ping,
            "warmup": self._handle_warmup,
        }

    def _handle_ping(self, params):
        return {"ok": True}

    def _handle_warmup(self, params):
        # In-class AND dispatched in-class → not a dead duplicate.
        return self._handle_ping(params)
'''


def _run_selftest() -> int:
    """Parse two in-memory snippets and assert detection.

    BAD: ``send_to_telegram`` routes to an extracted service while a dead
    in-class ``_handle_send_to_telegram`` exists with no caller → must flag 1.
    GOOD: every in-class handler is dispatched in-class (or called) → flag 0.
    """
    import tempfile

    failures: List[str] = []
    cases = [("known-bad", _SELFTEST_BAD, 1), ("known-good", _SELFTEST_GOOD, 0)]
    for name, snippet, expected in cases:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(snippet)
            tmp = Path(fh.name)
        try:
            tree = _parse(tmp)
            cls = _find_class(tree, MONOLITH_CLASS)
            assert cls is not None
            inclass = _inclass_handler_defs(cls)
            # Production-caller counts: scan only the snippet itself (self-contained).
            text = snippet
            counts: Dict[str, int] = {}
            for h in inclass:
                pat = re.compile(r"self\.%s\b" % re.escape(h))
                defpat = re.compile(r"\bdef\s+%s\b" % re.escape(h))
                counts[h] = sum(
                    1
                    for ln in (_strip_comment(x) for x in text.splitlines())
                    if pat.search(ln) and not defpat.search(ln)
                )
            dead = _audit_source(tmp, {h: [] for h in inclass}, counts)
            got = len(dead)
            status = "OK" if got == expected else "FAIL"
            print(f"[selftest] {name:11s} expected={expected} got={got}  {status}")
            if got != expected:
                failures.append(name)
        finally:
            tmp.unlink(missing_ok=True)

    if failures:
        print(f"[selftest] FAILED: {failures}", file=sys.stderr)
        return 1
    print("[selftest] all cases passed.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit dead in-class BackendService._handle_* copies that shadow a "
            "live extracted dispatch target, and tests that validate the dead copy."
        )
    )
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="exit non-zero if any non-allowlisted dead-duplicate is found (CI mode).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON output instead of the text report.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the built-in known-bad / known-good detection snippets and exit.",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return _run_selftest()

    if not SERVICE_PY.exists():
        print(f"[ERROR] {_rel(SERVICE_PY)} not found.", file=sys.stderr)
        return 2

    result = run_audit()

    if args.json:
        print(format_json(result))
    else:
        print(format_report(result))

    if args.fail_on_found and result.flagged:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
