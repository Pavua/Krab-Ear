"""Реальное тело Swift force restart в стенде без lifecycle системы."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift"


def force_restart_method():
    source = SOURCE.read_text()
    start = source.index("    func forceRestartBackend() -> Bool {")
    end = source.index("\n    func stopBackend()", start)
    return source[start:end]


class SwiftForceRestartGuardTests(unittest.TestCase):
    def test_force_restart_is_fail_closed_on_all_platforms(self):
        method = force_restart_method()
        body = method[method.index("{") + 1:method.rindex("}")]
        body = re.sub(r"//[^\n]*", "", body)
        self.assertEqual(body.strip(), "false")

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/swift").is_file(),
        "executable Swift harness requires the macOS toolchain",
    )
    def test_force_restart_refuses_in_both_modes_without_lifecycle(self):
        method = force_restart_method()
        program = r'''
import Foundation
import Darwin

enum SupervisionMode { case active, passive }
final class Process {
    static var runs = 0
    var executableURL: URL?
    var arguments: [String]?
    var terminationStatus: Int32 = 0
    func run() throws { Self.runs += 1 }
    func waitUntilExit() {}
}
final class FakeSupervisor {
    var supervisionMode: SupervisionMode
    var stops = 0
    var starts = 0
    init(_ mode: SupervisionMode) { supervisionMode = mode }
    static func kickstartArguments(uid: uid_t) -> [String] { ["fake"] }
    func stopBackend() { stops += 1 }
    func ensureBackendRunning() throws { starts += 1 }
METHOD
}
for mode in [SupervisionMode.active, .passive] {
    let supervisor = FakeSupervisor(mode)
    let result = supervisor.forceRestartBackend()
    if result || supervisor.stops != 0 || supervisor.starts != 0 || Process.runs != 0 {
        fputs("unsafe lifecycle in force restart\n", stderr)
        exit(1)
    }
}
'''.replace("METHOD", method)
        with tempfile.TemporaryDirectory(prefix="ear-swift-guard-") as directory:
            harness = Path(directory) / "guard.swift"
            harness.write_text(program)
            result = subprocess.run(["/usr/bin/swift", str(harness)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
