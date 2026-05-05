"""
Tests for start_agent.command caller audit (Phase C.6.2 followup).

These tests verify:
1. The audit document exists and is non-empty.
2. No Python/Swift/command files in the repo reference native/runtime/KrabEarAgent
   (except expected locations: SingleInstanceGuard, this audit doc, scripts that
   explicitly document the legacy path for diagnostic/removal purposes).
"""

import os
import sys
import unittest

# ── Project root resolution ──────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))

_AUDIT_DOC = os.path.join(
    _PROJECT_ROOT, "docs", "audit", "start-agent-callers-2026-05-05.md"
)

# Files that are explicitly allowed to reference native/runtime/KrabEarAgent
# because they document or handle the legacy path intentionally.
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
    # Swift tests for SingleInstanceGuard (test the orphan-killing logic)
    os.path.join(
        _PROJECT_ROOT,
        "native", "KrabEarAgent", "Tests", "KrabEarAgentTests",
        "SingleInstanceGuardTests.swift",
    ),
    # Smoke-release: tests backward compat path explicitly
    os.path.join(_PROJECT_ROOT, "scripts", "run_smoke_release.command"),
    # Scripts that document / remove / verify the legacy path
    os.path.join(_PROJECT_ROOT, "scripts", "repair_permissions.command"),
    os.path.join(_PROJECT_ROOT, "scripts", "remove_agent.command"),
    os.path.join(_PROJECT_ROOT, "scripts", "verify_binaries.command"),
    os.path.join(_PROJECT_ROOT, "scripts", "install_agent.command"),
    os.path.join(_PROJECT_ROOT, "scripts", "update_agent.command"),
    os.path.join(_PROJECT_ROOT, "scripts", "start_agent.command"),
    # Migration script diagnostic mentions the pattern in a grep
    os.path.join(
        _PROJECT_ROOT, "scripts", "migrate_to_canonical_launchagent.command"
    ),
    # This audit document
    _AUDIT_DOC,
    # This test file itself (contains the string in comments/docstrings)
    os.path.join(_PROJECT_ROOT, "KrabEar", "tests", "test_start_agent_audit.py"),
    # Migration script tests — checks that start_agent.command does NOT call runtime directly
    os.path.join(_PROJECT_ROOT, "KrabEar", "tests", "test_migration_scripts.py"),
}

# Extensions we care about for the "no stray runtime refs" check.
# Docs (.md) intentionally excluded — release checklists and runbooks
# legitimately mention the runtime/ path for build instructions.
# Only check live source files that could actually spawn a process.
_SOURCE_EXTENSIONS = {".py", ".swift", ".command", ".sh"}


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
        No .py / .swift / .command / .sh / .md file outside the known-allowed
        set should reference 'native/runtime/KrabEarAgent'.

        This is a canary: new code that accidentally introduces the legacy spawn
        path will be caught here before it ships.
        """
        stray_hits = []

        for dirpath, dirnames, filenames in os.walk(_PROJECT_ROOT):
            # Skip .git and hidden dirs
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d != "__pycache__"
            ]

            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in _SOURCE_EXTENSIONS:
                    continue

                filepath = os.path.join(dirpath, filename)

                # Skip allowed files
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
                "Unexpected references to native/runtime/KrabEarAgent found "
                "(add file to _ALLOWED_RUNTIME_REFS if intentional):\n"
                + "\n".join(stray_hits)
            )


if __name__ == "__main__":
    unittest.main()
