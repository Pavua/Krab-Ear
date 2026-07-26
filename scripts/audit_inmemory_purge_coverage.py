#!/usr/bin/env python3
"""audit_inmemory_purge_coverage.py — in-RAM PII purge-coverage guard.

ROOT-CAUSE invariant for the BLIND SPOT left open by the two disk-backed purge
guards (``audit_purge_coverage.py`` for file-backed stores + the dead-module /
path-containment guards).  Those guards prove that every *file* a privacy purge
must wipe is wiped.  They say NOTHING about **in-RAM collaborators that hold user
PII**: recent transcripts, clipboard text, search indices, queued async jobs,
cached embeddings / purge-epoch barriers.  If ``handle_purge_all_data`` does not
also clear those in-memory holders, stale PII survives a privacy purge in RAM
(re-exposable through IPC: ``get_context_memory`` / ``get_clipboard_history`` /
``get_job_status`` / search) until the process is restarted.  There was NO guard
for this class — this script is that guard.

DESIGN: a CURATED REGISTRY, not auto-discovery
-----------------------------------------------
"Which in-RAM attribute holds PII" is a judgement call that cannot be inferred
statically — a ``list`` field could hold transcripts (PII) or device ids (not).
So this guard intentionally uses an explicit, human-curated REGISTRY (the
``REGISTRY`` constant below) of the in-RAM collaborators that MUST be cleared on
purge, each keyed by the receiver + attribute it is stored under in
``HistoryService`` and the exact clear-call expected
(e.g. ``self._context_memory.clear()`` or ``self.store.reset_search_caches()``).

The guard parses ``HistoryService.handle_purge_all_data``'s AST and asserts each
registry entry's clear-call is physically present in that method.  A MISSING
clear-call is a real purge gap and fails ``--fail-on-found``.

The VALUE of a curated registry (vs auto-discovery) is the forcing function:
when a new in-RAM PII collaborator is added to the backend, a human must ADD it
to this registry — and that addition immediately fails the guard until the
purge wiring is also added.  The registry is the single human-reviewed list of
"RAM holders that leak PII across a purge"; the guard mechanically enforces it.

What it checks (static AST analysis, no import of the target code):

  1. Locate ``handle_purge_all_data`` in ``KrabEar/backend/history_service.py``.
  2. Collect every clear-call inside it (and any single-hop helper it calls in
     the same module) as ``(receiver_attr, method)`` pairs — where
     ``self._foo.clear()`` yields ``("_foo", "clear")`` and
     ``self.store.reset_search_caches()`` yields ``("store", "reset_search_caches")``.
  3. For each REGISTRY entry, assert its ``(receiver, method)`` clear-call is in
     that set.  Report any entry whose clear-call is MISSING.

Usage:
    python3 scripts/audit_inmemory_purge_coverage.py                 # report, exit 0
    python3 scripts/audit_inmemory_purge_coverage.py --fail-on-found # exit 1 on gap
    python3 scripts/audit_inmemory_purge_coverage.py --json          # machine-readable
    python3 scripts/audit_inmemory_purge_coverage.py --selftest      # classifier self-test

Exit 0 → every registry entry is cleared by the purge (or report-only mode).
Exit 1 → ``--fail-on-found`` and at least one registry entry is NOT cleared.

REPORT-ONLY: this guard is NOT wired into CI or ``make audit-all``.  A human
enforces it (``make audit-inmemory-purge-coverage ARGS=--fail-on-found``) after
confirming there are zero findings.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
BACKEND_DIR = KRAB_EAR / "backend"
HISTORY_SERVICE = BACKEND_DIR / "history_service.py"
PURGE_METHOD = "handle_purge_all_data"


@dataclass(frozen=True)
class RegistryEntry:
    """An in-RAM PII collaborator that MUST be cleared by the privacy purge.

    receiver  — the attribute the collaborator is reached through inside
                ``handle_purge_all_data``.  ``"_foo"`` matches ``self._foo`` and
                ``"store"`` matches ``self.store`` (the leading ``self.`` is
                implicit; nested receivers are matched on their trailing attr).
    method    — the clear-call method name expected on that receiver
                (``clear`` / ``purge_all`` / ``reset_search_caches`` ...).
    pii       — one-line description of the PII the collaborator holds in RAM.
    """

    receiver: str
    method: str
    pii: str

    @property
    def call_repr(self) -> str:
        return f"self.{self.receiver}.{self.method}()"


# ---------------------------------------------------------------------------
# THE REGISTRY — curated list of in-RAM PII collaborators (human-maintained).
#
# Add an entry here whenever a new backend collaborator holds user PII in RAM
# that would survive a privacy purge without an explicit clear-call.  Adding an
# entry will FAIL this guard until the matching clear-call is wired into
# ``handle_purge_all_data`` — which is the whole point.
# ---------------------------------------------------------------------------
REGISTRY: tuple[RegistryEntry, ...] = (
    RegistryEntry(
        receiver="_context_memory",
        method="clear",
        pii="ContextMemory deque of last ~50 raw transcripts (full PII), "
        "re-exposable via get_context_memory IPC",
    ),
    RegistryEntry(
        receiver="_clipboard_history",
        method="clear",
        pii="in-memory last ~20 pasted transcripts (full PII), re-exposable via "
        "get_clipboard_history / repaste_item IPC",
    ),
    RegistryEntry(
        receiver="store",
        method="reset_search_caches",
        pii="StateStore in-RAM SearchIndex (_search_index: full text of ALL "
        "items) + _recent_search_index (~4000 cleartext haystacks)",
    ),
    RegistryEntry(
        receiver="_job_tracker",
        method="clear",
        pii="JobTracker._jobs registry — terminal async-job records holding "
        "transcript text in items[].text + error fragments (live up to 1h)",
    ),
    RegistryEntry(
        receiver="_recording_core",
        method="clear_terminal_cache",
        pii="terminal-ответы stop_recording в RAM: text/original_text/"
        "translated_text, recovery-пути и публичные поля ошибок (TTL 5 минут)",
    ),
    RegistryEntry(
        receiver="_semantic_searcher",
        method="purge_all",
        pii="SemanticSearcher embedding index + purge-epoch barrier "
        "(_purge_epoch bump stops in-flight re-persist of purged embeddings)",
    ),
)

# ``_recent_search_index`` is a distinct RAM PII holder named in the task, but it
# is cleared by the SAME ``store.reset_search_caches()`` call as ``_search_index``
# (see StateStore.reset_search_caches, which clears both).  It is therefore folded
# into the ``store/reset_search_caches`` registry entry above rather than given a
# separate entry whose clear-call would never appear distinctly in the purge AST.
#
# ``HotwordDetector._hotwords`` / ``._patterns`` (wave-26 MED) are intentionally
# NOT in this REGISTRY because HotwordDetector lives in BackendService, not in
# HistoryService.  The clear-call ``self._hotword_detector.clear()`` is wired in
# ``BackendService._handle_purge_all_data`` (KrabEar/backend/service.py) — after the
# HistoryService purge deletes hotwords.json from disk.  This guard only scans
# ``history_service.py::handle_purge_all_data``; a comment here documents the
# intentional out-of-band placement so future auditors do not re-add it to the
# registry (which would always show as a false gap).
#
# ``TranscriptionQueue._jobs`` (wave-30 MED) is intentionally NOT in this REGISTRY
# for the same reason: TranscriptionQueue lives in BackendService (self._transcription_queue),
# not in HistoryService.  The clear-call ``self._transcription_queue.clear()`` is wired
# in ``BackendService._handle_purge_all_data`` immediately after
# ``self._hotword_detector.clear()`` — same out-of-band pattern.  Adding it to this
# registry would permanently show as a false gap because this guard only scans
# ``history_service.py::handle_purge_all_data``.


@dataclass
class AuditResult:
    registry: tuple[RegistryEntry, ...]
    cleared_calls: set[tuple[str, str]]
    covered: list[RegistryEntry]
    gaps: list[RegistryEntry]


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


def _find_method(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _receiver_attr(node: ast.AST) -> str | None:
    """Return the trailing attribute that names the receiver of a method call.

    For ``self._foo.clear()`` the call's ``func.value`` is ``self._foo`` (an
    Attribute whose ``.attr`` is ``"_foo"``) → returns ``"_foo"``.
    For ``self.store.reset_search_caches()`` → ``"store"``.
    For a bare ``self.clear()`` (receiver is ``self``) → ``None`` (no attr
    receiver — never matches a registry entry, which always has an attr).
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _collect_clear_calls(func: ast.FunctionDef) -> set[tuple[str, str]]:
    """Collect every ``self.<receiver>.<method>(...)`` call inside ``func`` as a
    ``(receiver_attr, method)`` pair.

    Only calls whose receiver is ``self.<attr>`` (one hop off ``self``) are
    recorded — exactly the shape every registry clear-call takes.  Calls on
    deeper chains or on locals are ignored (they can never match a registry
    entry, whose receiver is always a direct ``self`` attribute).
    """
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        recv = node.func.value
        recv_attr = _receiver_attr(recv)
        if recv_attr is None:
            continue
        # recv must be ``self.<attr>``: recv is an Attribute, its .value is Name 'self'.
        if isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name):
            if recv.value.id == "self":
                pairs.add((recv_attr, method))
    return pairs


def _same_module_helper_calls(func: ast.FunctionDef) -> set[str]:
    """Return names of same-object helper methods invoked as ``self.<name>(...)``
    inside ``func`` (single-hop), so their bodies can be scanned for clear-calls
    too (the purge may delegate a clear into a private helper)."""
    helpers: set[str] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        recv = node.func.value
        if isinstance(recv, ast.Name) and recv.id == "self":
            helpers.add(node.func.attr)
    return helpers


def extract_cleared_calls() -> set[tuple[str, str]]:
    """Parse ``handle_purge_all_data`` (+ one hop of same-module helpers it
    calls) and return the set of ``(receiver_attr, method)`` clear-calls it
    physically performs."""
    tree = _parse(HISTORY_SERVICE)
    purge_fn = _find_method(tree, PURGE_METHOD)
    if purge_fn is None:
        raise SystemExit(
            "audit_inmemory_purge_coverage: "
            f"{PURGE_METHOD} not found in {_rel(HISTORY_SERVICE)} — guard cannot run."
        )

    cleared = _collect_clear_calls(purge_fn)

    # One-hop helper expansion: scan the bodies of same-class methods the purge
    # calls as ``self.<helper>(...)`` for additional clear-calls.
    helper_names = _same_module_helper_calls(purge_fn)
    if helper_names:
        method_defs = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        for hname in helper_names:
            hfn = method_defs.get(hname)
            if hfn is not None and hfn is not purge_fn:
                cleared |= _collect_clear_calls(hfn)

    return cleared


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_audit() -> AuditResult:
    cleared = extract_cleared_calls()
    covered: list[RegistryEntry] = []
    gaps: list[RegistryEntry] = []
    for entry in REGISTRY:
        if (entry.receiver, entry.method) in cleared:
            covered.append(entry)
        else:
            gaps.append(entry)
    return AuditResult(
        registry=REGISTRY,
        cleared_calls=cleared,
        covered=covered,
        gaps=gaps,
    )


def format_report(result: AuditResult) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("IN-MEMORY PRIVACY-PURGE COVERAGE AUDIT (curated registry)")
    lines.append("=" * 78)
    lines.append(f"registry entries : {len(result.registry)}")
    lines.append(f"covered by purge : {len(result.covered)}")
    lines.append(f"UNCOVERED GAPS   : {len(result.gaps)}")
    lines.append("")

    if not result.gaps:
        lines.append(
            "OK — every registered in-RAM PII collaborator is cleared by "
            f"{PURGE_METHOD}."
        )
        lines.append("")
        lines.append("Covered:")
        for entry in result.covered:
            lines.append(f"  - {entry.call_repr:<42} {entry.pii}")
        return "\n".join(lines)

    lines.append(
        f"In-RAM PII collaborators NOT cleared by {PURGE_METHOD} "
        "(stale PII survives a"
    )
    lines.append("privacy purge in memory until process restart):")
    lines.append("")
    for entry in result.gaps:
        lines.append(f"  - {entry.call_repr}")
        lines.append(f"      PII: {entry.pii}")
        lines.append("")
    lines.append(
        f"Wire each missing clear-call into {_rel(HISTORY_SERVICE)}::{PURGE_METHOD}, "
        "or — if the"
    )
    lines.append(
        "collaborator no longer holds PII — remove its entry from REGISTRY "
        "in this script."
    )
    return "\n".join(lines)


def format_json(result: AuditResult) -> str:
    payload = {
        "registry_count": len(result.registry),
        "covered_count": len(result.covered),
        "gap_count": len(result.gaps),
        "cleared_calls": sorted(
            f"self.{r}.{m}()" for r, m in result.cleared_calls
        ),
        "covered": [
            {"call": e.call_repr, "receiver": e.receiver, "method": e.method}
            for e in result.covered
        ],
        "gaps": [
            {
                "call": e.call_repr,
                "receiver": e.receiver,
                "method": e.method,
                "pii": e.pii,
            }
            for e in result.gaps
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Self-test — known-good vs known-bad snippet, asserts the classifier flags bad.
# ---------------------------------------------------------------------------
_GOOD_SNIPPET = '''
class HistoryService:
    def handle_purge_all_data(self, params):
        if self._widget is not None:
            self._widget.clear()
        self.store.reset_caches()
'''

_BAD_SNIPPET = '''
class HistoryService:
    def handle_purge_all_data(self, params):
        # _widget.clear() is MISSING — stale PII survives the purge in RAM
        self.store.reset_caches()
'''


def _cleared_in_source(source: str) -> set[tuple[str, str]]:
    tree = ast.parse(source)
    fn = _find_method(tree, PURGE_METHOD)
    assert fn is not None, "selftest snippet missing handle_purge_all_data"
    cleared = _collect_clear_calls(fn)
    for hname in _same_module_helper_calls(fn):
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == hname and n is not fn:
                cleared |= _collect_clear_calls(n)
    return cleared


def run_selftest() -> int:
    probe = RegistryEntry(receiver="_widget", method="clear", pii="probe")

    good = _cleared_in_source(_GOOD_SNIPPET)
    assert (probe.receiver, probe.method) in good, (
        "SELFTEST FAIL: classifier did not detect the present "
        f"{probe.call_repr} in the known-GOOD snippet"
    )

    bad = _cleared_in_source(_BAD_SNIPPET)
    assert (probe.receiver, probe.method) not in bad, (
        "SELFTEST FAIL: classifier wrongly reported "
        f"{probe.call_repr} as present in the known-BAD snippet"
    )

    # Sanity: the present store.reset_caches() must register under receiver 'store'.
    assert ("store", "reset_caches") in good, (
        "SELFTEST FAIL: classifier missed self.store.reset_caches() in GOOD snippet"
    )

    print("SELFTEST OK: classifier flags the missing clear-call in the bad snippet")
    print(f"  GOOD snippet cleared calls: {sorted(good)}")
    print(f"  BAD  snippet cleared calls: {sorted(bad)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="exit non-zero if any registered in-RAM PII collaborator is uncleared",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the text report",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the known-good/known-bad classifier self-test and exit",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

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
