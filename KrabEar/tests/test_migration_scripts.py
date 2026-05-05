"""
Tests for migration scripts.
Verifies structural requirements without executing state-modifying scripts.
"""
import os
import stat
import unittest

# Resolve repo root relative to this test file
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")

START_AGENT_CMD = os.path.join(_SCRIPTS_DIR, "start_agent.command")
MIGRATE_CMD = os.path.join(_SCRIPTS_DIR, "migrate_to_canonical_launchagent.command")


class TestStartAgentDeprecated(unittest.TestCase):

    def test_start_agent_command_exists(self):
        self.assertTrue(
            os.path.isfile(START_AGENT_CMD),
            f"start_agent.command not found at {START_AGENT_CMD}",
        )

    def test_start_agent_command_has_deprecated_header(self):
        """DEPRECATED marker must appear within the first 30 lines."""
        with open(START_AGENT_CMD, "r", encoding="utf-8") as fh:
            first_30 = [next(fh, "") for _ in range(30)]
        content = "".join(first_30)
        self.assertIn(
            "DEPRECATED",
            content,
            "start_agent.command must contain DEPRECATED in its first 30 lines",
        )

    def test_start_agent_command_mentions_migration_script(self):
        """Deprecated header should point users to the migration script."""
        with open(START_AGENT_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "migrate_to_canonical_launchagent.command",
            content,
            "start_agent.command must reference migrate_to_canonical_launchagent.command",
        )


class TestMigrateScript(unittest.TestCase):

    def test_migrate_script_exists(self):
        self.assertTrue(
            os.path.isfile(MIGRATE_CMD),
            f"migrate_to_canonical_launchagent.command not found at {MIGRATE_CMD}",
        )

    def test_migrate_script_is_executable(self):
        mode = os.stat(MIGRATE_CMD).st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        self.assertTrue(
            is_exec,
            f"migrate_to_canonical_launchagent.command must be executable (mode={oct(mode)})",
        )

    def test_migrate_script_references_canonical_label(self):
        """Must install the canonical plist label com.antigravity.krab-ear."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "com.antigravity.krab-ear",
            content,
            "Migration script must reference com.antigravity.krab-ear label",
        )

    def test_migrate_script_removes_legacy_plist(self):
        """Must reference legacy plist com.krabear.agent.plist for removal."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "com.krabear.agent.plist",
            content,
            "Migration script must reference legacy com.krabear.agent.plist",
        )

    def test_migrate_script_uses_open_for_bundle(self):
        """/usr/bin/open must be used so launchd launches the .app bundle."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(
            "/usr/bin/open",
            content,
            "Migration script must use /usr/bin/open to launch .app bundle",
        )

    def test_migrate_script_does_not_reference_runtime_binary(self):
        """Must NOT directly invoke native/runtime/KrabEarAgent (that's the legacy path)."""
        with open(MIGRATE_CMD, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn(
            "native/runtime/KrabEarAgent",
            content,
            "Migration script must not spawn the legacy runtime binary",
        )


if __name__ == "__main__":
    unittest.main()
