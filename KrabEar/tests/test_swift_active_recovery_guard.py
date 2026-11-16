"""Безопасный стенд для active-mode восстановления Swift supervisor."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift"


def recovery_method():
    source = SOURCE.read_text()
    start = source.index("    func restartIfDeadDetailed() -> BackendRestartOutcome {")
    end = source.index("\n    /// launchd-label", start)
    return source[start:end]


class SwiftActiveRecoveryGuardTests(unittest.TestCase):
    def test_active_branch_defers_without_lifecycle(self):
        method = recovery_method()
        active = method[method.index("        case .active:"):]
        active = re.sub(r"//[^\n]*", "", active)
        self.assertRegex(active, r"case \.active:\s*return \.failed\s*}\s*}")

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/swift").is_file(),
        "executable Swift harness requires the macOS toolchain",
    )
    def test_recovery_contract_without_real_lifecycle(self):
        program = r'''
import Foundation

enum SupervisionMode { case active, passive }
enum BackendRestartOutcome { case alreadyAlive, recovered, failed }
final class FakeSupervisor {
    var supervisionMode: SupervisionMode
    var alive: Bool
    let stateLock = NSLock()
    var consecutiveRestarts = 0
    var lastRestartAttemptAt: Date?
    let restartCooldownSec: TimeInterval = 300
    static let maxConsecutiveRestarts = 3
    var stops = 0
    var starts = 0
    init(_ mode: SupervisionMode, alive: Bool) { self.supervisionMode = mode; self.alive = alive }
    func isBackendAlive() -> Bool { alive }
    func stopBackend() { stops += 1 }
    func ensureBackendRunning() throws { starts += 1 }
METHOD
}
let active = FakeSupervisor(.active, alive: false)
if active.restartIfDeadDetailed() != .failed || active.stops != 0 || active.starts != 0 { exit(1) }
let alive = FakeSupervisor(.active, alive: true)
if alive.restartIfDeadDetailed() != .alreadyAlive || alive.stops != 0 || alive.starts != 0 { exit(2) }
let passive = FakeSupervisor(.passive, alive: false)
if passive.restartIfDeadDetailed() != .recovered || passive.stops != 0 || passive.starts != 1 { exit(3) }
'''.replace("METHOD", recovery_method())
        with tempfile.TemporaryDirectory(prefix="ear-active-guard-") as directory:
            harness = Path(directory) / "guard.swift"
            harness.write_text(program)
            result = subprocess.run(["/usr/bin/swift", str(harness)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
