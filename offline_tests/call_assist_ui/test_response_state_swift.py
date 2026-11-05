"""Standalone Swift harness invokes the actual Call Assist state method."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+CallAssist.swift"


def _actual_method():
    source = SOURCE.read_text(encoding="utf-8")
    begin = source.index("    func applyCallAssistState(")
    opening = source.index("{", begin)
    depth = 0
    for position in range(opening, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[begin:position + 1]
    raise AssertionError("Unterminated Swift state method")


def test_refusal_notifies_without_replacing_state_and_success_still_applies(tmp_path):
    harness = r'''
import Foundation
final class TextField { var stringValue = "previous" }
final class Button { var isEnabled = false }
final class Controller {
    var callAssistStatusLabel = TextField()
    var callAssistStartButton = Button()
    var callAssistStopButton = Button()
    var callAssistActive = true
    var alerts: [(String, String)] = []
    func showInfoAlert(title: String, body: String) { alerts.append((title, body)) }
__ACTUAL_METHOD__
}
func check(_ condition: Bool, _ name: String) {
    if !condition { print("FAIL: " + name); exit(1) }
}
let controller = Controller()
controller.callAssistStopButton.isEnabled = true
controller.applyCallAssistState(["ok": false, "error": "maintenance_active"])
check(controller.alerts.count == 1, "refusal must notify")
check(!controller.alerts[0].1.isEmpty, "refusal explanation")
check(controller.callAssistStatusLabel.stringValue == "previous", "status preserved")
check(controller.callAssistActive, "active flag preserved")
check(!controller.callAssistStartButton.isEnabled && controller.callAssistStopButton.isEnabled, "buttons preserved")
controller.applyCallAssistState(["ok": false, "error": "maintenance_unknown"])
check(controller.alerts.count == 2 && !controller.alerts[1].1.isEmpty, "unknown must notify")
controller.applyCallAssistState(["ok": false, "error": "private-synthetic-detail"])
check(controller.alerts.count == 3 && !controller.alerts[2].1.contains("private-synthetic-detail"), "unknown error redacted")
controller.applyCallAssistState(["active": false, "status": "idle"])
check(controller.callAssistStatusLabel.stringValue == "Call Assist: idle", "legacy state applied")
check(!controller.callAssistActive && controller.callAssistStartButton.isEnabled, "idle buttons")
controller.applyCallAssistState(["ok": true, "active": true, "status": "running"])
check(controller.callAssistActive && controller.callAssistStopButton.isEnabled, "active state applied")
print("PASS")
'''.replace("__ACTUAL_METHOD__", _actual_method())
    swift_source = tmp_path / "CallAssistHarness.swift"
    binary = tmp_path / "call-assist-harness"
    swift_source.write_text(harness, encoding="utf-8")
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    compile_result = subprocess.run(
        ["/usr/bin/swiftc", "-module-cache-path", str(tmp_path / "module-cache"), str(swift_source), "-o", str(binary)],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, env=env)
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert run_result.stdout.strip() == "PASS"
