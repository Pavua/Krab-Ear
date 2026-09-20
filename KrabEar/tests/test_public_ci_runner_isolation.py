"""Контракт fail-closed аудита public GitHub Actions workflow."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "audit_public_ci_runner_isolation.py"


def load_module():
    spec = importlib.util.spec_from_file_location("audit_public_ci_runner_isolation", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WorkflowPolicyFixturesTest(unittest.TestCase):
    """Небольшие реальные workflow pin-ят границу доверия runner-а."""

    def setUp(self) -> None:
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".github" / "workflows").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.root / ".github" / "workflows" / name).write_text(text, encoding="utf-8")

    def reasons(self) -> list[str]:
        return [finding.reason for finding in self.mod.audit_tree(self.root)]

    def test_accepts_hosted_pull_request_workflow(self) -> None:
        self.write("ci.yml", "on:\n  pull_request:\njobs:\n  test:\n    runs-on: macos-latest\n")

        self.assertEqual(self.reasons(), [])

    def test_rejects_self_hosted_string_and_list(self) -> None:
        self.write("a.yml", "on: push\njobs:\n  a:\n    runs-on: self-hosted\n")
        self.write("b.yaml", "on: [push]\njobs:\n  b:\n    runs-on: [self-hosted, macOS, ARM64]\n")

        self.assertEqual(sum("self_hosted_runner" in value for value in self.reasons()), 2)

    def test_rejects_dynamic_runs_on(self) -> None:
        self.write("ci.yml", "on: pull_request\njobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n")

        self.assertIn("dynamic_runs_on", self.reasons())

    def test_rejects_pull_request_target(self) -> None:
        self.write("ci.yml", "on:\n  pull_request_target:\njobs:\n  test:\n    runs-on: ubuntu-latest\n")

        self.assertIn("pull_request_target_forbidden", self.reasons())

    def test_invalid_yaml_fails_closed(self) -> None:
        self.write("ci.yml", "on: [pull_request\njobs: {}\n")

        self.assertTrue(any(value.startswith("yaml_parse_error:") for value in self.reasons()))
