#!/usr/bin/env python3
"""audit_ipc_contract_drift.py — Static guard for Swift↔Python IPC / SSE contract drift.

Detects the class of bug that produced 37 silent feature-breakages:
  - Part A (HIGH, fail-on-found): Swift calls a method that is NOT in the Python
    dispatch table → unknown_method at runtime, feature silently no-ops.
  - Part B (REPORT-ONLY): Swift sends param keys the Python handler never reads →
    feature no-ops with wrong defaults.
  - Part C (REPORT-ONLY): Backend emits an SSE event type string that differs from
    the string used in the Swift filter/consumer (dot vs underscore mismatch class).

Allowlist file: scripts/ipc_drift_allowlist.txt
Format (one entry per line, # comments ignored):
    method:clear_privacy_audit_log    # intentionally removed from IPC dispatch (W957)
    param:apply_profile_preset:name   # false positive — param routed through helper
    emit:disk.warn                    # emitted but not consumed on Swift side (backend-only)

Usage:
    python3 scripts/audit_ipc_contract_drift.py
    python3 scripts/audit_ipc_contract_drift.py --json
    python3 scripts/audit_ipc_contract_drift.py --fail-on-found        # fails on Part A
    python3 scripts/audit_ipc_contract_drift.py --strict               # fails on A+B+C
    python3 scripts/audit_ipc_contract_drift.py --help

Exit codes:
    0 — no drift (or all drift is allowlisted)
    1 — drift found that triggers failure (Part A by default; A+B+C with --strict)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent  # scripts/ is one level under repo root
_SWIFT_SRC = _REPO_ROOT / "native" / "KrabEarAgent" / "Sources" / "KrabEarAgent"
_SERVICE_PY = _REPO_ROOT / "KrabEar" / "backend" / "service.py"
_BACKEND_DIR = _REPO_ROOT / "KrabEar" / "backend"
_CORE_DIR = _REPO_ROOT / "KrabEar" / "core"
_ALLOWLIST_FILE = _SCRIPT_DIR / "ipc_drift_allowlist.txt"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SwiftCallSite:
    method: str
    file: str
    line: int
    call_form: str  # "call", "callAsync", "executeIPC", "callWithRecovery"


@dataclass
class SwiftParamSite:
    method: str
    keys: list[str]
    file: str
    line: int


@dataclass
class SseEmit:
    event_type: str
    file: str
    line: int
    is_typed: bool  # emit_typed vs emit(string, ...)


@dataclass
class SseConsumer:
    event_type: str
    file: str
    line: int
    source: str  # "filter" or "case"


@dataclass
class Finding:
    check: str           # "A", "B", "C"
    severity: str        # "high", "medium", "low"
    description: str
    swift_loc: Optional[str] = None
    python_loc: Optional[str] = None
    allowlist_key: Optional[str] = None


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def _load_allowlist(path: Path) -> set[str]:
    """Load allowlist entries (stripped lines, # comments removed)."""
    if not path.exists():
        return set()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


# ---------------------------------------------------------------------------
# Part A — IPC method-name check
# ---------------------------------------------------------------------------

_SWIFT_IPC_CALL_PATTERNS = [
    # Matches all direct IPC call forms (with or without receiver prefix like self.):
    #   call(method: "M"        callAsync(method: "M"    executeIPC(method: "M"
    #   callWithRecovery(method: "M"   ipc.call(method: "M"  ipcClient.call(method: "M"
    #   appendPageAsync(method: "M"   self.appendPageAsync(method: "M"  (local wrapper)
    re.compile(
        r'(?:^|[^a-zA-Z_])'  # not preceded by identifier char (word boundary)
        r'(?:call|callAsync|executeIPC|callWithRecovery|appendPageAsync)'
        r'\s*\(\s*method\s*:\s*"([a-z][a-z0-9_]*)"',
        re.MULTILINE,
    ),
    # ipc.call / ipcClient.call with explicit receiver
    re.compile(
        r'(?:ipc|ipcClient|client)\s*\.\s*call(?:Async)?\s*\(\s*method\s*:\s*"([a-z][a-z0-9_]*)"',
        re.MULTILINE,
    ),
    # Catch multi-line call forms where method: label is on its own indented line.
    # E.g.: appendPageAsync(\n    method: "M",\n    params: ...
    # Only match lines that look like a named argument label (indented, followed by comma or closing paren).
    re.compile(
        r'^\s{2,}method\s*:\s*"([a-z][a-z0-9_]*)"\s*[,)]?\s*(?://.*)?$',
        re.MULTILINE,
    ),
]


def _enumerate_swift_call_sites(src_dir: Path) -> list[SwiftCallSite]:
    """Extract all string-literal IPC call sites from Swift sources."""
    sites: list[SwiftCallSite] = []
    if not src_dir.exists():
        return sites
    for swift_file in sorted(src_dir.rglob("*.swift")):
        try:
            text = swift_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for lineno, line in enumerate(lines, 1):
            for pat in _SWIFT_IPC_CALL_PATTERNS:
                for m in pat.finditer(line):
                    method = m.group(1)
                    # Determine call form
                    call_form = "call"
                    if "callAsync" in m.group(0):
                        call_form = "callAsync"
                    elif "executeIPC" in m.group(0):
                        call_form = "executeIPC"
                    elif "callWithRecovery" in m.group(0):
                        call_form = "callWithRecovery"
                    sites.append(SwiftCallSite(
                        method=method,
                        file=str(swift_file.relative_to(_REPO_ROOT)),
                        line=lineno,
                        call_form=call_form,
                    ))
    return sites


def _enumerate_python_dispatch_methods(service_py: Path) -> dict[str, int]:
    """Extract literal keys from ``BackendService._build_dispatch_table`` only.

    This includes lambda handlers and excludes every other dictionary in
    ``service.py`` so the audit reflects the live runtime dispatch contract.
    """
    if not service_py.exists():
        return {}
    try:
        text = service_py.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=str(service_py))
    except (OSError, SyntaxError):
        return {}
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "BackendService":
            continue
        for member in class_node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if member.name != "_build_dispatch_table":
                continue
            for statement in member.body:
                if not isinstance(statement, ast.Return) or not isinstance(statement.value, ast.Dict):
                    continue
                methods: dict[str, int] = {}
                for key in statement.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        if re.fullmatch(r"[a-z][a-z0-9_]*", key.value):
                            methods[key.value] = key.lineno
                return methods
    return {}


def check_part_a(
    swift_sites: list[SwiftCallSite],
    py_methods: dict[str, int],
    allowlist: set[str],
) -> list[Finding]:
    """Part A: Find Swift-called methods not in the Python dispatch table."""
    findings: list[Finding] = []
    seen: set[tuple[str, str, int]] = set()

    for site in swift_sites:
        key = (site.method, site.file, site.line)
        if key in seen:
            continue
        seen.add(key)

        ak = f"method:{site.method}"
        if ak in allowlist:
            continue

        if site.method not in py_methods:
            findings.append(Finding(
                check="A",
                severity="high",
                description=(
                    f"Swift calls \"{site.method}\" (via {site.call_form}) "
                    f"but method is NOT in Python dispatch table"
                ),
                swift_loc=f"{site.file}:{site.line}",
                python_loc=f"KrabEar/backend/service.py (dispatch table)",
                allowlist_key=ak,
            ))
    return findings


# ---------------------------------------------------------------------------
# Part B — IPC param-key check (best-effort, report-only)
# ---------------------------------------------------------------------------

def _extract_swift_params(src_dir: Path) -> list[SwiftParamSite]:
    """Find Swift call sites with DICTIONARY LITERAL params and extract keys."""
    sites: list[SwiftParamSite] = []
    if not src_dir.exists():
        return sites

    # Match: (method: "M", params: ["k1": ..., "k2": ...])
    # We look for the params dict literal on the same line as the method call.
    # Key pattern: "key": (value) where key is a lowercase snake_case word.
    method_pat = re.compile(
        r'(?:call|callAsync|executeIPC|callWithRecovery|ipc\.call|ipcClient\.call)'
        r'\s*\(\s*method\s*:\s*"([a-z][a-z0-9_]*)"'
        r'[^)]*params\s*:\s*\[([^\]]*)\]',
        re.DOTALL,
    )
    key_pat = re.compile(r'"([a-z][a-z0-9_]*)"\s*:')

    for swift_file in sorted(src_dir.rglob("*.swift")):
        try:
            text = swift_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()

        # Work line by line; for multi-line calls, join with next few lines
        for lineno, line in enumerate(lines, 1):
            # Quick filter: must contain method: "
            if 'method:' not in line and 'method :' not in line:
                continue
            # Try to capture up to 5 lines for multi-line dict literals
            chunk = "\n".join(lines[lineno - 1: lineno + 5])
            for m in method_pat.finditer(chunk):
                method = m.group(1)
                params_body = m.group(2)
                keys = key_pat.findall(params_body)
                if keys:
                    sites.append(SwiftParamSite(
                        method=method,
                        keys=keys,
                        file=str(swift_file.relative_to(_REPO_ROOT)),
                        line=lineno,
                    ))
    return sites


def _find_python_handler_file(method: str, dispatch: dict[str, int]) -> Optional[Path]:
    """Best-effort: find the Python file containing the handler for `method`."""
    if method not in dispatch:
        return None

    # Read service.py to find the handler reference
    try:
        text = _SERVICE_PY.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Find the dispatch line and extract the handler reference
    line_no = dispatch[method]
    lines = text.splitlines()
    if line_no <= 0 or line_no > len(lines):
        return None
    dispatch_line = lines[line_no - 1]

    # Extract handler method name: self._svc.handle_X or self._handle_X
    handler_m = re.search(r'self\.(\w+)\.(\w+)', dispatch_line)
    if handler_m:
        svc_attr = handler_m.group(1)
        handler_name = handler_m.group(2)
    else:
        handler_m2 = re.search(r'self\.(_handle_\w+)', dispatch_line)
        if handler_m2:
            # In-class handler — check service.py itself
            return _SERVICE_PY
        return None

    # Scan backend/ for the class that has this handler
    for py_file in sorted(_BACKEND_DIR.glob("*.py")):
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"def {handler_name}(" in content:
            return py_file
    # Fallback to service.py
    return _SERVICE_PY


def _extract_python_param_keys(py_file: Path, handler_name: str) -> list[str]:
    """Extract param keys read by a handler: params.get("k") / params["k"]."""
    try:
        text = py_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Find the handler method block (from def handler_name to next def at same indent)
    # Simplified: find the def and grab up to 80 lines
    pat = re.compile(rf'def {re.escape(handler_name)}\s*\(')
    m = pat.search(text)
    if not m:
        return []

    start = text.rfind('\n', 0, m.start()) + 1
    snippet_lines = text[start:].splitlines()[:80]
    snippet = '\n'.join(snippet_lines)

    # Extract keys from params.get("k") or params["k"] or params.get('k')
    keys = re.findall(r'params\s*[\[\.get\(]+["\']([a-z][a-z0-9_]*)["\']\]?', snippet)
    return list(dict.fromkeys(keys))  # deduplicate, preserve order


def check_part_b(
    swift_sites: list[SwiftCallSite],
    swift_params: list[SwiftParamSite],
    py_methods: dict[str, int],
    allowlist: set[str],
) -> list[Finding]:
    """Part B: Find Swift param keys not read by the Python handler (best-effort)."""
    findings: list[Finding] = []

    # Build index of param sites by method
    param_index: dict[str, list[SwiftParamSite]] = {}
    for ps in swift_params:
        param_index.setdefault(ps.method, []).append(ps)

    # Only check methods that ARE in the dispatch table (avoid Part A noise)
    for method, sites_list in param_index.items():
        if method not in py_methods:
            continue  # Part A handles this

        py_file = _find_python_handler_file(method, py_methods)
        if py_file is None:
            continue

        # Find handler method name
        try:
            svc_line = _SERVICE_PY.read_text(encoding="utf-8", errors="replace").splitlines()[py_methods[method] - 1]
        except (OSError, IndexError):
            continue

        handler_m = re.search(r'self\.\w+\.(\w+)', svc_line)
        if handler_m:
            handler_name = handler_m.group(1)
        else:
            handler_m2 = re.search(r'self\.(_handle_\w+)', svc_line)
            if handler_m2:
                handler_name = handler_m2.group(1)
            else:
                continue

        py_keys = _extract_python_param_keys(py_file, handler_name)
        if not py_keys:
            continue  # couldn't extract — skip rather than false positive

        for ps in sites_list:
            for swift_key in ps.keys:
                ak = f"param:{method}:{swift_key}"
                if ak in allowlist:
                    continue
                if swift_key not in py_keys:
                    findings.append(Finding(
                        check="B",
                        severity="medium",
                        description=(
                            f"Swift sends params[\"{swift_key}\"] to \"{method}\" "
                            f"but handler \"{handler_name}\" in "
                            f"{py_file.relative_to(_REPO_ROOT)} never reads that key"
                        ),
                        swift_loc=f"{ps.file}:{ps.line}",
                        python_loc=f"{py_file.relative_to(_REPO_ROOT)} ({handler_name})",
                        allowlist_key=ak,
                    ))
    return findings


# ---------------------------------------------------------------------------
# Part C — SSE type-string check (best-effort, report-only)
# ---------------------------------------------------------------------------

def _enumerate_backend_emits(backend_dir: Path, core_dir: Path) -> list[SseEmit]:
    """Enumerate backend emit("TYPE", ...) and emit_typed(EventType.X, ...) calls."""
    emits: list[SseEmit] = []

    plain_pat = re.compile(r'\.emit\s*\(\s*["\']([a-zA-Z][a-zA-Z0-9_.]*)["\']')
    typed_pat = re.compile(r'emit_typed\s*\(\s*EventType\.([A-Z_]+)')
    # EventType string values from contracts/registry.py
    eventtype_values: dict[str, str] = {}

    # First, parse EventType enum from contracts/
    contracts_dir = _REPO_ROOT / "KrabEar" / "contracts"
    for py_file in sorted(contracts_dir.glob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'(\w+)\s*=\s*["\']([a-zA-Z][a-zA-Z0-9_.]*)["\']', text):
            eventtype_values[m.group(1)] = m.group(2)

    for search_dir in [backend_dir, core_dir]:
        if not search_dir.exists():
            continue
        for py_file in sorted(search_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                for m in plain_pat.finditer(line):
                    emits.append(SseEmit(
                        event_type=m.group(1),
                        file=str(py_file.relative_to(_REPO_ROOT)),
                        line=lineno,
                        is_typed=False,
                    ))
                for m in typed_pat.finditer(line):
                    enum_name = m.group(1)
                    resolved = eventtype_values.get(enum_name, enum_name.lower())
                    emits.append(SseEmit(
                        event_type=resolved,
                        file=str(py_file.relative_to(_REPO_ROOT)),
                        line=lineno,
                        is_typed=True,
                    ))
    return emits


def _enumerate_swift_sse_consumers(src_dir: Path) -> list[SseConsumer]:
    """Enumerate Swift SSE filter query params and case/== comparisons."""
    consumers: list[SseConsumer] = []
    if not src_dir.exists():
        return consumers

    # Pattern 1: ?filter=TYPE,TYPE2 in URL strings
    filter_pat = re.compile(r'filter=([a-zA-Z][a-zA-Z0-9_.,]*)')
    # Pattern 2: case "TYPE": or eventType == "TYPE"
    case_pat = re.compile(r'(?:case\s+|eventType\s*==\s*)"([a-zA-Z][a-zA-Z0-9_.]*)"')

    for swift_file in sorted(src_dir.rglob("*.swift")):
        try:
            text = swift_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for lineno, line in enumerate(lines, 1):
            for m in filter_pat.finditer(line):
                for etype in m.group(1).split(","):
                    etype = etype.strip()
                    if etype:
                        consumers.append(SseConsumer(
                            event_type=etype,
                            file=str(swift_file.relative_to(_REPO_ROOT)),
                            line=lineno,
                            source="filter",
                        ))
            for m in case_pat.finditer(line):
                etype = m.group(1)
                # Only consider event-like types (dot notation or snake_case with
                # known prefixes — filter out generic strings like "info", "error")
                if ("." in etype or "_" in etype) and len(etype) > 4:
                    consumers.append(SseConsumer(
                        event_type=etype,
                        file=str(swift_file.relative_to(_REPO_ROOT)),
                        line=lineno,
                        source="case",
                    ))
    return consumers


def _dot_underscore_variants(s: str) -> set[str]:
    """Return s plus its dot↔underscore swap variant."""
    return {s, s.replace(".", "_"), s.replace("_", ".")}


def check_part_c(
    emits: list[SseEmit],
    consumers: list[SseConsumer],
    allowlist: set[str],
) -> list[Finding]:
    """Part C: Find backend emit types that differ from Swift consumers by . vs _.

    Conservative: only flags cases where the emitted type differs from a consumed
    type ONLY by dot vs underscore — the exact class of the krab.error→krab_error bug.
    """
    findings: list[Finding] = []

    swift_types = {c.event_type for c in consumers}
    # Build index: consumer by event_type
    consumer_index: dict[str, list[SseConsumer]] = {}
    for c in consumers:
        consumer_index.setdefault(c.event_type, []).append(c)

    seen: set[tuple[str, str]] = set()

    for emit in emits:
        etype = emit.event_type
        ak = f"emit:{etype}"
        if ak in allowlist:
            continue

        # Skip if the exact type is consumed — no drift
        if etype in swift_types:
            continue

        # Check if a dot↔underscore variant IS consumed
        variants = _dot_underscore_variants(etype) - {etype}
        for variant in variants:
            if variant in swift_types:
                key = (etype, variant)
                if key in seen:
                    continue
                seen.add(key)
                # Find a consumer for the variant to report location
                consumers_for_variant = consumer_index.get(variant, [])
                swift_loc = (
                    f"{consumers_for_variant[0].file}:{consumers_for_variant[0].line}"
                    if consumers_for_variant else "Swift (multiple)"
                )
                findings.append(Finding(
                    check="C",
                    severity="medium",
                    description=(
                        f"Backend emits \"{etype}\" but Swift consumes \"{variant}\" "
                        f"(dot/underscore mismatch — event silently dropped)"
                    ),
                    swift_loc=swift_loc,
                    python_loc=f"{emit.file}:{emit.line}",
                    allowlist_key=ak,
                ))
    return findings


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _fmt_finding(f: Finding, idx: int) -> str:
    lines = [f"  [{idx}] [{f.check}] [{f.severity.upper()}] {f.description}"]
    if f.swift_loc:
        lines.append(f"        Swift: {f.swift_loc}")
    if f.python_loc:
        lines.append(f"        Python: {f.python_loc}")
    lines.append(f"        allowlist-key: {f.allowlist_key}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Swift↔Python IPC / SSE contract drift."
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--fail-on-found",
        action="store_true",
        help="Exit 1 on Part A (method-name) drift (default gate)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any drift (Part A + B + C)",
    )
    args = parser.parse_args()

    allowlist = _load_allowlist(_ALLOWLIST_FILE)

    # --- Enumerate sources ---
    swift_sites = _enumerate_swift_call_sites(_SWIFT_SRC)
    py_methods = _enumerate_python_dispatch_methods(_SERVICE_PY)
    swift_params = _extract_swift_params(_SWIFT_SRC)
    emits = _enumerate_backend_emits(_BACKEND_DIR, _CORE_DIR)
    consumers = _enumerate_swift_sse_consumers(_SWIFT_SRC)

    # --- Run checks ---
    findings_a = check_part_a(swift_sites, py_methods, allowlist)
    findings_b = check_part_b(swift_sites, swift_params, py_methods, allowlist)
    findings_c = check_part_c(emits, consumers, allowlist)

    all_findings = findings_a + findings_b + findings_c

    if args.json:
        out = {
            "summary": {
                "part_a_method_name": len(findings_a),
                "part_b_param_key": len(findings_b),
                "part_c_sse_type": len(findings_c),
                "total": len(all_findings),
                "swift_call_sites_scanned": len(swift_sites),
                "python_dispatch_methods": len(py_methods),
                "backend_emits_scanned": len(emits),
                "swift_sse_consumers_scanned": len(consumers),
                "allowlist_entries": len(allowlist),
            },
            "findings": [
                {
                    "check": f.check,
                    "severity": f.severity,
                    "description": f.description,
                    "swift_loc": f.swift_loc,
                    "python_loc": f.python_loc,
                    "allowlist_key": f.allowlist_key,
                }
                for f in all_findings
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("IPC / SSE CONTRACT DRIFT AUDIT")
        print("=" * 72)
        print(f"Swift sources : {_SWIFT_SRC}")
        print(f"Python service: {_SERVICE_PY}")
        print(f"Allowlist     : {_ALLOWLIST_FILE} ({len(allowlist)} entries)")
        print()

        print(f"Scanned: {len(swift_sites)} Swift IPC call sites, "
              f"{len(py_methods)} Python dispatch methods, "
              f"{len(emits)} backend emits, "
              f"{len(consumers)} Swift SSE consumers")
        print()

        # Part A
        print(f"--- Part A: IPC Method-Name Drift (HIGH, fail-on-found) "
              f"— {len(findings_a)} finding(s) ---")
        if findings_a:
            for i, f in enumerate(findings_a, 1):
                print(_fmt_finding(f, i))
        else:
            print("  CLEAN — no method-name drift found.")
        print()

        # Part B
        print(f"--- Part B: IPC Param-Key Drift (MEDIUM, report-only) "
              f"— {len(findings_b)} finding(s) ---")
        if findings_b:
            for i, f in enumerate(findings_b, 1):
                print(_fmt_finding(f, i))
        else:
            print("  CLEAN — no param-key drift found.")
        print()

        # Part C
        print(f"--- Part C: SSE Type-String Drift (MEDIUM, report-only) "
              f"— {len(findings_c)} finding(s) ---")
        if findings_c:
            for i, f in enumerate(findings_c, 1):
                print(_fmt_finding(f, i))
        else:
            print("  CLEAN — no SSE dot/underscore mismatch found.")
        print()

        total = len(all_findings)
        print("=" * 72)
        print(f"SUMMARY: {len(findings_a)} Part-A | {len(findings_b)} Part-B | "
              f"{len(findings_c)} Part-C | {total} total finding(s)")
        if total == 0:
            print("RESULT: CLEAN — no IPC/SSE contract drift detected.")
        else:
            print(f"RESULT: {total} finding(s) — see above.")
        print("=" * 72)

    # --- Exit code ---
    # --strict: fail on any drift (A + B + C)
    if args.strict and all_findings:
        return 1
    # --fail-on-found (default gate): fail on Part A method-name drift only.
    # Part A is always the hard gate; --fail-on-found makes it explicit.
    if findings_a:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
