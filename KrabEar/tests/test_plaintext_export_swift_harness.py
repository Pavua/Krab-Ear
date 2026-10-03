"""Исполняемый Swift coordinator с fake socket; без AppKit/app/runtime/dependencies."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_plaintext_export_coordinator_behavior(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("Swift IPC client uses Darwin; executable gate runs on macOS")
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("Swift toolchain unavailable")
    root = Path(__file__).resolve().parents[2]
    native = root / "native/KrabEarAgent"
    binary = tmp_path / "plaintext-export-checks"
    compilation = subprocess.run(
        [swiftc, "-swift-version", "5", "-parse-as-library", "-DPLAINTEXT_EXPORT_STANDALONE",
         str(native / "Sources/KrabEarAgent/IPCClient.swift"),
         str(native / "Sources/KrabEarAgent/PlaintextExportCoordinator.swift"),
         str(native / "Tests/KrabEarAgentTests/PlaintextExportCoordinatorTests.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=150,
    )
    assert compilation.returncode == 0, compilation.stderr
    execution = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert "passed" in execution.stdout
    assert "SECRET_SENTINEL" not in execution.stdout + execution.stderr
