"""Контракт recovery-сообщений private Ear CI runner-а."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "krab_ear_runner_health_check.py"


def load_module():
    spec = importlib.util.spec_from_file_location("krab_ear_runner_health_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PrivateRunnerRecoveryMessageTest(unittest.TestCase):
    """Alert ведёт к private controller, а не к восстановлению public runner-а."""

    def test_actionable_message_targets_private_controller_only(self) -> None:
        mod = load_module()

        message = mod.runner_offline_message("krab-ear-m4max-private", "offline", 3)

        self.assertIn("Pavua/Krab-CI-Control", message)
        self.assertIn("actions-runner-krab-ci-control", message)
        self.assertNotIn("Pavua/Krab-Ear", message)
        self.assertNotIn("actions-runner-krab-ear", message)
