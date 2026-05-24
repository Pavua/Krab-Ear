"""
Tests for start_agent.command caller audit (Phase C.6.2 followup).

These tests verify:
1. The audit document exists and is non-empty.
2. No PRODUCTION source files reference 'native/runtime/KrabEarAgent' outside
   the expected locations (SingleInstanceGuard, main.swift, binary-drift checks).

Wave 545 (3rd attempt — Wave 527 + Wave 540 failed due to session compact/reboot):
Scope changed from repo-wide walk to production-only directories:
  - KrabEar/backend/**/*.py
  - KrabEar/core/**/*.py
  - native/KrabEarAgent/Sources/**/*.swift

Exempt (not scanned):
  - docs/, specs/, .claude/, scripts/, Makefile, tests/**
  - Any non-production path

Rationale: docs/specs/tests legitimately describe the legacy runtime/ path for
diagnostic/instructional purposes. Only PRODUCTION code that spawns a process is
dangerous; that's what this canary checks. See Wave 470 precedent (same pattern
applied to another cross-cutting audit).
"""

import os
import unittest

# ── Project root resolution ──────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))

_AUDIT_DOC = os.path.join(
    _PROJECT_ROOT, "docs", "audit", "start-agent-callers-2026-05-05.md"
)

# ── Production source roots (Wave 545: scoped, not repo-wide) ────────────────
# Only these directories are scanned for stray runtime/ references.
# Everything else (tests, docs, scripts, specs, .claude) is exempt.
_PRODUCTION_ROOTS = [
    os.path.join(_PROJECT_ROOT, "KrabEar", "backend"),
    os.path.join(_PROJECT_ROOT, "KrabEar", "core"),
    os.path.join(_PROJECT_ROOT, "native", "KrabEarAgent", "Sources"),
]

# Files within the production roots that are explicitly allowed to reference
# native/runtime/KrabEarAgent because they legitimately handle the legacy path.
_ALLOWED_RUNTIME_REFS = {
    # C.6.2 orphan killer — its whole job is to find and kill the runtime binary
    os.path.join(
        _PROJECT_ROOT,
        "native", "KrabEarAgent", "Sources", "KrabEarAgent",
        "SingleInstanceGuard.swift",
    ),
    # main.swift: C.6.2 killOrphanRuntimeProcesses + startup comment
    os.path.join(
        _PROJECT_ROOT,
        "native", "KrabEarAgent", "Sources", "KrabEarAgent",
        "main.swift",
    ),
    # Wave 50: error_codes.py — `agent.binary_drift` entry's `user_msg_ru` text
    # mentions the runtime path so user understands what 'drift' means in toast.
    os.path.join(_PROJECT_ROOT, "KrabEar", "backend", "error_codes.py"),
    # Wave 55 A1: service.py — `_check_binary_drift_on_startup()` dwarfdumps both
    # Krab Ear.app bundle binary AND native/runtime/KrabEarAgent to detect drift,
    # then pushes `agent.binary_drift` error code via error_bus.
    os.path.join(_PROJECT_ROOT, "KrabEar", "backend", "service.py"),
}

# Extensions scanned within production roots.
_SOURCE_EXTENSIONS = {".py", ".swift"}


class StartAgentAuditTestCase(unittest.TestCase):

    def test_audit_doc_exists(self):
        """Audit document must be present at the expected path."""
        self.assertTrue(
            os.path.exists(_AUDIT_DOC),
            f"Audit doc not found: {_AUDIT_DOC}",
        )

    def test_audit_doc_not_empty(self):
        """Audit document must have substantive content (>500 bytes)."""
        self.assertTrue(
            os.path.exists(_AUDIT_DOC),
            f"Audit doc not found: {_AUDIT_DOC}",
        )
        size = os.path.getsize(_AUDIT_DOC)
        self.assertGreater(
            size,
            500,
            f"Audit doc is suspiciously small ({size} bytes): {_AUDIT_DOC}",
        )

    def test_audit_doc_contains_hypothesis(self):
        """Audit document must contain a hypothesis / recommendation section."""
        with open(_AUDIT_DOC, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "Hypothesis",
            content,
            "Audit doc must contain a 'Hypothesis' section",
        )
        self.assertIn(
            "Recommended action",
            content,
            "Audit doc must contain a 'Recommended action' section",
        )

    def test_no_stray_callers_for_runtime_path_in_repo(self):
        """
        No production .py / .swift file (KrabEar/backend, KrabEar/core,
        native/KrabEarAgent/Sources) outside the known-allowed set should
        reference 'native/runtime/KrabEarAgent'.

        Wave 545: scope changed from repo-wide to production-only to stop false
        positives from docs/specs/tests that legitimately describe the path for
        instructional or diagnostic purposes (Wave 527 + Wave 540 precedent).

        This remains a meaningful canary: any new production code that accidentally
        introduces the legacy spawn path will be caught before it ships.
        """
        stray_hits = []

        for root in _PRODUCTION_ROOTS:
            if not os.path.isdir(root):
                continue  # root doesn't exist yet — skip gracefully

            for dirpath, dirnames, filenames in os.walk(root):
                # Skip hidden dirs and __pycache__
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d != "__pycache__"
                ]

                for filename in filenames:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in _SOURCE_EXTENSIONS:
                        continue

                    filepath = os.path.join(dirpath, filename)

                    # Skip explicitly allowed files
                    if filepath in _ALLOWED_RUNTIME_REFS:
                        continue

                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                            for lineno, line in enumerate(fh, start=1):
                                if "native/runtime/KrabEarAgent" in line:
                                    stray_hits.append(
                                        f"{os.path.relpath(filepath, _PROJECT_ROOT)}:{lineno}: {line.rstrip()}"
                                    )
                    except OSError:
                        pass  # Unreadable file — skip

        if stray_hits:
            self.fail(
                "Unexpected references to native/runtime/KrabEarAgent found in "
                "production source (add file to _ALLOWED_RUNTIME_REFS if intentional):\n"
                + "\n".join(stray_hits)
            )

    def test_doc_file_with_runtime_path_does_not_trigger_failure(self):
        """
        Wave 545 regression guard: a doc/spec/test file containing the string
        'native/runtime/KrabEarAgent' must NOT be flagged by the production scan.

        This verifies the scope-narrowing fix is actually effective and prevents
        the false-positive failures that blocked ~71 open PRs (Wave 527 + 540).
        """
        import tempfile

        # Create a temporary .md file outside production roots with the banned string
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", dir=_PROJECT_ROOT, delete=False
        ) as tf:
            tf.write("# Doc\nSee native/runtime/KrabEarAgent for details.\n")
            tmppath = tf.name

        try:
            # Re-run the scan logic — the temp doc must NOT appear in stray_hits
            stray_hits = []
            for root in _PRODUCTION_ROOTS:
                if not os.path.isdir(root):
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [
                        d for d in dirnames
                        if not d.startswith(".") and d != "__pycache__"
                    ]
                    for filename in filenames:
                        ext = os.path.splitext(filename)[1].lower()
                        if ext not in _SOURCE_EXTENSIONS:
                            continue
                        filepath = os.path.join(dirpath, filename)
                        if filepath in _ALLOWED_RUNTIME_REFS:
                            continue
                        try:
                            with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                                for lineno, line in enumerate(fh, start=1):
                                    if "native/runtime/KrabEarAgent" in line:
                                        stray_hits.append(filepath)
                        except OSError:
                            pass

            # The temp doc is outside production roots → must not appear
            self.assertNotIn(
                tmppath,
                stray_hits,
                "Doc file outside production roots incorrectly flagged — scope fix broken",
            )
            # Sanity: the scan produced no hits at all (clean tree)
            self.assertEqual(
                stray_hits,
                [],
                f"Unexpected stray hits in production scan: {stray_hits}",
            )
        finally:
            os.unlink(tmppath)


if __name__ == "__main__":
    unittest.main()
