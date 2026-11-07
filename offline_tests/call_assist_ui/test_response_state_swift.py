"""Standalone Swift harness invokes the actual Call Assist state method."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+CallAssist.swift"


def _actual_method(marker="    func applyCallAssistState("):
    source_path = Path(os.environ.get("CALL_ASSIST_UI_TEST_SOURCE", str(SOURCE)))
    source = source_path.read_text(encoding="utf-8")
    begin = source.index(marker)
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
controller.applyCallAssistState(["ok": false, "error": "maintenance_active"], showErrors: false)
check(controller.alerts.count == 1, "silent refresh must not alert")
check(controller.callAssistActive && controller.callAssistStopButton.isEnabled, "silent refusal preserves state")
controller.applyCallAssistState(["ok": false, "error": "maintenance_unknown"])
check(controller.alerts.count == 2 && !controller.alerts[1].1.isEmpty, "unknown must notify")
controller.applyCallAssistState(["ok": false, "error": "private-synthetic-detail"])
check(controller.alerts.count == 3 && !controller.alerts[2].1.contains("private-synthetic-detail"), "unknown error redacted")
controller.applyCallAssistState(["active": false, "status": "idle"])
check(controller.callAssistStatusLabel.stringValue == "Call Assist: idle", "legacy state applied")
check(!controller.callAssistActive && controller.callAssistStartButton.isEnabled, "idle buttons")
controller.applyCallAssistState(["status": "running"])
check(controller.callAssistActive && controller.callAssistStopButton.isEnabled, "legacy running allows stop")
controller.applyCallAssistState(["status": "starting"])
check(!controller.callAssistActive && !controller.callAssistStopButton.isEnabled, "legacy starting does not allow stop")
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


def test_refresh_passes_silent_preference_to_state_handler(tmp_path):
    harness = r'''
import Foundation
final class Label { var stringValue = "" }
final class IPC {
    func call(method: String, params: [String: Any]) throws -> [String: Any] {
        return ["result": ["ok": false, "error": "synthetic"]]
    }
}
final class Controller {
    let ipcClient = IPC()
    let callAssistStatusLabel = Label()
    var values: [Bool] = []
    func applyCallAssistState(_ result: [String: Any], showErrors: Bool = true) { values.append(showErrors) }
__ACTUAL_METHOD__
}
func check(_ condition: Bool, _ name: String) {
    if !condition { print("FAIL: " + name); exit(1) }
}
func waitFor(_ controller: Controller, _ count: Int) {
    let deadline = Date().addingTimeInterval(3)
    while controller.values.count < count && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    check(controller.values.count == count, "handler completed")
}
let controller = Controller()
controller.refreshCallAssistState()
waitFor(controller, 1)
check(controller.values == [false], "default refresh is silent")
controller.refreshCallAssistState(silentOnError: false)
waitFor(controller, 2)
check(controller.values == [false, true], "explicit refresh shows errors")
print("PASS")
'''.replace("__ACTUAL_METHOD__", _actual_method("    func refreshCallAssistState("))
    swift_source = tmp_path / "CallAssistRefreshHarness.swift"
    binary = tmp_path / "call-assist-refresh-harness"
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


def test_start_only_saves_settings_after_an_accepted_response(tmp_path):
    harness = r'''
import Foundation
enum State { case on, off }
final class Button { var state: State = .on }
struct Settings { let translationMode = "off" }
// Зеркало файлового глобала HistoryPanelController+CallAssist.swift (R3).
var pendingCallTimelineSaveRequestId: String? = "stale-from-previous-call"
final class IPC {
    var result: [String: Any] = [:]
    var methods: [String] = []
    func call(method: String, params: [String: Any]) throws -> [String: Any] {
        methods.append(method)
        return ["result": result]
    }
}
final class Controller: NSObject {
    let ipcClient = IPC()
    let callNotifyButton = Button()
    let callAutoSummaryButton = Button()
    var patchCount = 0
    var received = false
    var lastResult: [String: Any] = [:]
    var alertCount = 0
    func settingsProvider() -> Settings { Settings() }
    func selectedCaptureSourceMode() -> String { "microphone" }
    func applySettingsPatch(_ patch: [String: Any]) { patchCount += 1 }
    func applyCallAssistState(_ result: [String: Any]) { lastResult = result; received = true }
    func showInfoAlert(title: String, body: String) { alertCount += 1; received = true }
__ACTUAL_METHOD__
}
func check(_ condition: Bool, _ name: String) {
    if !condition { print("FAIL: " + name); exit(1) }
}
func waitForReply(_ controller: Controller) {
    let deadline = Date().addingTimeInterval(3)
    while !controller.received && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    check(controller.received, "handler completed")
}
let controller = Controller()
controller.ipcClient.result = ["ok": false, "error": "maintenance_active"]
controller.onStartCallAssist()
waitForReply(controller)
check(controller.patchCount == 0, "refusal must not save settings")
for status in ["running", "starting", ""] {
    controller.received = false
    controller.ipcClient.result = ["ok": false, "error": "already_active", "status": status]
    controller.onStartCallAssist()
    waitForReply(controller)
    check(controller.patchCount == 0, "already active must not save settings")
    check(controller.lastResult["ok"] == nil, "already active uses legacy state projection")
    check((controller.lastResult["status"] as? String) == status, "status preserved")
}
check(controller.alertCount == 3, "already active is explained")
controller.received = false
controller.ipcClient.result = ["ok": true, "active": true]
controller.onStartCallAssist()
waitForReply(controller)
check(controller.patchCount == 1, "accepted start saves settings")
controller.received = false
controller.ipcClient.result = ["active": true]
controller.onStartCallAssist()
waitForReply(controller)
check(controller.patchCount == 2, "legacy start saves settings")
check(controller.ipcClient.methods.allSatisfy { $0 == "start_call_assist" }, "no stop or refresh is issued")
print("PASS")
'''.replace("__ACTUAL_METHOD__", _actual_method("    @objc func onStartCallAssist("))
    swift_source = tmp_path / "CallAssistStartHarness.swift"
    binary = tmp_path / "call-assist-start-harness"
    swift_source.write_text(harness, encoding="utf-8")
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path)
    compile_result = subprocess.run(
        ["/usr/bin/swiftc", "-module-cache-path", str(tmp_path / "module-cache"), str(swift_source), "-o", str(binary)],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15, env=env)
    assert run_result.returncode == 0, run_result.stdout + run_result.stderr
    assert run_result.stdout.strip() == "PASS"


if __name__ == "__main__":
    if sys.flags.optimize:
        raise SystemExit("optimized Python disables required assertions")
    if "CALL_ASSIST_UI_TEST_SOURCE" in os.environ:
        raise SystemExit("source override is forbidden for the CI entrypoint")
    for label, test in (
        ("state", test_refusal_notifies_without_replacing_state_and_success_still_applies),
        ("refresh", test_refresh_passes_silent_preference_to_state_handler),
        ("start", test_start_only_saves_settings_after_an_accepted_response),
    ):
        with tempfile.TemporaryDirectory(prefix="call-assist-swift-") as temp_dir:
            test(Path(temp_dir))
        print(f"PASS {label}")
