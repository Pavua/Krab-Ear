"""Production Swift callbacks без AppKit, сети, истории и запуска приложения."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


# Compile150 + run30 и общий autouse backend-import укладываются в общий предел.
@pytest.mark.timeout(300)
def test_history_migration_observation_callbacks(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("Исполняемый Swift gate запускается на macOS")
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("Swift toolchain отсутствует")
    root = Path(__file__).resolve().parents[2]
    native = root / "native/KrabEarAgent"
    binary = tmp_path / "migration-observation-checks"
    compilation = subprocess.run(
        [swiftc, "-swift-version", "5", "-parse-as-library",
         "-DHISTORY_MIGRATION_OBSERVATION_STANDALONE",
         str(native / "Sources/KrabEarAgent/HistoryEncryptionOutcomes.swift"),
         str(native / "Sources/KrabEarAgent/HistoryMigrationObservationCoordinator.swift"),
         str(native / "Tests/KrabEarAgentTests/HistoryMigrationObservationCoordinatorTests.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=150,
    )
    assert compilation.returncode == 0, compilation.stdout + compilation.stderr
    execution = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert "48/48 migration observation checks passed" in execution.stdout
