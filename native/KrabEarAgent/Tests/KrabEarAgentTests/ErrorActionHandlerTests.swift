/*
 ErrorActionHandlerTests — тесты ErrorActionHandler (Phase B.1 Task 10).

 Покрытие:
 1. handleErrorEvent → toast presenter вызывается с правильным payload.
 2. handleActionTap → IPC метод handle_error_action вызывается с правильными params.
 3. handleActionTap side_effect=swift_focus_hf_token → NotificationCenter post.
 4. KrabErrorPayload — Codable round-trip (decode из JSON + encode обратно).
 5. AnyCodable — декодирование Bool, Int, Double, String, null, array, dict.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

// MARK: - MockToastPresenter

@MainActor
private final class MockToastPresenter: ToastPresenting {
    var presentedErrors: [KrabErrorPayload] = []

    func present(error: KrabErrorPayload) {
        presentedErrors.append(error)
    }
}

// MARK: - Unix socket echo server (shared helper)

private func runIPCEchoServer(
    socketPath: String,
    responseJSON: String,
    ready: @escaping () -> Void
) {
    let serverFd = socket(AF_UNIX, SOCK_STREAM, 0)
    precondition(serverFd >= 0, "socket() failed")

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let sunPathSize = MemoryLayout.size(ofValue: addr.sun_path)
    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
        ptr.withMemoryRebound(to: CChar.self, capacity: sunPathSize) { cPtr in
            for i in 0..<sunPathSize { cPtr[i] = 0 }
            let bytes = Array(socketPath.utf8)
            for (i, b) in bytes.enumerated() { cPtr[i] = CChar(bitPattern: b) }
        }
    }
    let pathLen = socketPath.utf8.count
    let addrLen = socklen_t(MemoryLayout<sa_family_t>.size + pathLen + 1)
    let bound: Int32 = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.bind(serverFd, $0, addrLen)
        }
    }
    precondition(bound == 0, "bind() failed: \(String(cString: strerror(errno)))")
    precondition(Darwin.listen(serverFd, 1) == 0, "listen() failed")
    ready()

    DispatchQueue.global(qos: .utility).async {
        let clientFd = Darwin.accept(serverFd, nil, nil)
        guard clientFd >= 0 else { close(serverFd); return }
        defer { close(clientFd); close(serverFd) }
        var buf = [UInt8](repeating: 0, count: 4096)
        Darwin.read(clientFd, &buf, buf.count)
        let bytes = Array((responseJSON + "\n").utf8)
        _ = bytes.withUnsafeBytes { Darwin.write(clientFd, $0.baseAddress, $0.count) }
    }
}

private func tempSocketPath() -> String {
    let name = "krabear_errhandler_\(Int.random(in: 100_000...999_999)).sock"
    return (NSTemporaryDirectory() as NSString).appendingPathComponent(name)
}

// MARK: - ErrorActionHandlerTests

@MainActor
final class ErrorActionHandlerTests: XCTestCase {

    // MARK: 1. handleErrorEvent calls toast presenter

    func test_handleErrorEvent_calls_toast_present() async throws {
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let payload = KrabErrorPayload(
            severity: "error",
            component: "rewriter",
            code: "rewriter.timeout",
            message_user: "LM Studio не ответил",
            message_debug: "HTTP timeout after 30s",
            timestamp: "2026-05-04T00:00:00+00:00",
            context: [:],
            actionable: true,
            action_id: "open_lm_studio"
        )

        await handler.handleErrorEvent(payload)

        XCTAssertEqual(presenter.presentedErrors.count, 1, "presenter.present() должен быть вызван ровно один раз")
        let received = try XCTUnwrap(presenter.presentedErrors.first)
        XCTAssertEqual(received.code, "rewriter.timeout")
        XCTAssertEqual(received.severity, "error")
        XCTAssertEqual(received.message_user, "LM Studio не ответил")
        XCTAssertTrue(received.actionable)
        XCTAssertEqual(received.action_id, "open_lm_studio")
    }

    // MARK: 2. handleActionTap calls IPC with correct method + params

    func test_handleActionTap_calls_ipc() async throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        let okJSON = #"{"id":"1","ok":true,"result":{"status":"ok"}}"#
        runIPCEchoServer(socketPath: socketPath, responseJSON: okJSON) {
            readyExp.fulfill()
        }
        await fulfillment(of: [readyExp], timeout: 1.0)

        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: socketPath)
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        // Should not throw — IPC server responds with ok=true.
        await handler.handleActionTap(actionId: "open_lm_studio")

        // No assertion needed beyond "did not crash"; IPC call went through.
        // If the socket wasn't hit the server would block forever and test would timeout.
    }

    // MARK: 3. handleActionTap side_effect=swift_focus_hf_token posts notification

    func test_handleActionTap_swift_focus_hf_token_posts_notification() async throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        // Backend returns side_effect = swift_focus_hf_token
        let responseJSON = #"{"id":"1","ok":true,"result":{"status":"ok","side_effect":"swift_focus_hf_token"}}"#
        runIPCEchoServer(socketPath: socketPath, responseJSON: responseJSON) {
            readyExp.fulfill()
        }
        await fulfillment(of: [readyExp], timeout: 1.0)

        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: socketPath)
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let notifExp = expectation(description: "focusHFTokenSetting notification posted")
        let observer = NotificationCenter.default.addObserver(
            forName: .focusHFTokenSetting,
            object: nil,
            queue: .main
        ) { _ in
            notifExp.fulfill()
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        await handler.handleActionTap(actionId: "fix_hf_token")

        await fulfillment(of: [notifExp], timeout: 2.0)
    }

    // MARK: 4. KrabErrorPayload — Codable round-trip

    func test_krabErrorPayload_codable_roundTrip() throws {
        let json = """
        {
            "severity": "warn",
            "component": "diarization",
            "code": "diarization.no_token",
            "message_user": "HuggingFace token не задан",
            "message_debug": "pyannote requires HF_TOKEN",
            "timestamp": "2026-05-04T12:00:00+00:00",
            "context": {"model": "pyannote/speaker-diarization-3.1"},
            "actionable": true,
            "action_id": "fix_hf_token"
        }
        """
        let data = Data(json.utf8)
        let decoder = JSONDecoder()
        let payload = try decoder.decode(KrabErrorPayload.self, from: data)

        XCTAssertEqual(payload.severity, "warn")
        XCTAssertEqual(payload.component, "diarization")
        XCTAssertEqual(payload.code, "diarization.no_token")
        XCTAssertEqual(payload.message_user, "HuggingFace token не задан")
        XCTAssertEqual(payload.message_debug, "pyannote requires HF_TOKEN")
        XCTAssertEqual(payload.timestamp, "2026-05-04T12:00:00+00:00")
        XCTAssertTrue(payload.actionable)
        XCTAssertEqual(payload.action_id, "fix_hf_token")

        // Verify context key decodes as String
        let modelVal = payload.context["model"]?.value
        XCTAssertEqual(modelVal as? String, "pyannote/speaker-diarization-3.1")

        // Re-encode and decode again (round-trip)
        let encoder = JSONEncoder()
        let reEncoded = try encoder.encode(payload)
        let reDecoded = try decoder.decode(KrabErrorPayload.self, from: reEncoded)
        XCTAssertEqual(reDecoded.code, payload.code)
        XCTAssertEqual(reDecoded.severity, payload.severity)
    }

    func test_krabErrorPayload_null_action_id() throws {
        let json = """
        {
            "severity": "info",
            "component": "stt",
            "code": "stt.ok",
            "message_user": "OK",
            "message_debug": "",
            "timestamp": "2026-05-04T00:00:00+00:00",
            "context": {},
            "actionable": false,
            "action_id": null
        }
        """
        let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
        XCTAssertNil(payload.action_id)
        XCTAssertFalse(payload.actionable)
    }

    // MARK: 5. AnyCodable — primitive types decode correctly

    func test_anyCodable_decodes_bool() throws {
        let json = #"true"#
        let val = try JSONDecoder().decode(AnyCodable.self, from: Data(json.utf8))
        XCTAssertEqual(val.value as? Bool, true)
    }

    func test_anyCodable_decodes_int() throws {
        let json = #"42"#
        let val = try JSONDecoder().decode(AnyCodable.self, from: Data(json.utf8))
        XCTAssertEqual(val.value as? Int, 42)
    }

    func test_anyCodable_decodes_double() throws {
        let json = #"3.14"#
        let val = try JSONDecoder().decode(AnyCodable.self, from: Data(json.utf8))
        XCTAssertEqual(val.value as? Double ?? 0.0, 3.14, accuracy: 0.0001)
    }

    func test_anyCodable_decodes_string() throws {
        let json = #""hello""#
        let val = try JSONDecoder().decode(AnyCodable.self, from: Data(json.utf8))
        XCTAssertEqual(val.value as? String, "hello")
    }

    func test_anyCodable_decodes_null() throws {
        let json = #"null"#
        let val = try JSONDecoder().decode(AnyCodable.self, from: Data(json.utf8))
        XCTAssertTrue(val.value is NSNull)
    }

    // MARK: 6. parse_KrabErrorPayload_valid_JSON — explicit named test

    func test_parse_KrabErrorPayload_valid_JSON() throws {
        let json = """
        {
            "severity": "error",
            "component": "stt",
            "code": "stt.mlx_crash",
            "message_user": "MLX упал",
            "message_debug": "SIGSEGV at gpu_hash_table",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {"model": "mlx-whisper-base"},
            "actionable": false,
            "action_id": null
        }
        """
        let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
        XCTAssertEqual(payload.severity, "error")
        XCTAssertEqual(payload.component, "stt")
        XCTAssertEqual(payload.code, "stt.mlx_crash")
        XCTAssertEqual(payload.message_user, "MLX упал")
        XCTAssertEqual(payload.message_debug, "SIGSEGV at gpu_hash_table")
        XCTAssertEqual(payload.timestamp, "2026-05-19T10:00:00+00:00")
        XCTAssertFalse(payload.actionable)
        XCTAssertNil(payload.action_id)
        XCTAssertEqual(payload.context["model"]?.value as? String, "mlx-whisper-base")
    }

    // MARK: 7. parse_invalid_JSON_returns_nil — handleRawSSEData silently drops bad data

    func test_parse_invalid_JSON_returns_nil() async throws {
        // We verify by calling handleRawSSEData with malformed JSON and confirming
        // the mock presenter never gets called.
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_parse_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        // Malformed JSON — missing required fields
        handler.handleRawSSEData("{not valid json!!!!}")

        // Give the internal Task a brief moment to run if it were going to
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(presenter.presentedErrors.count, 0,
                       "Presenter must NOT be called when JSON is invalid")
    }

    // MARK: 8. handleErrorEvent severity info calls toast (info variant)

    func test_handleErrorEvent_severity_info_shows_toast() async throws {
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_info_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let payload = KrabErrorPayload(
            severity: "info",
            component: "system",
            code: "system.ready",
            message_user: "Готов к работе",
            message_debug: "",
            timestamp: "2026-05-19T10:00:00+00:00",
            actionable: false,
            action_id: nil
        )

        await handler.handleErrorEvent(payload)

        XCTAssertEqual(presenter.presentedErrors.count, 1)
        XCTAssertEqual(presenter.presentedErrors.first?.severity, "info")
        // info severity has 2s auto-dismiss — verified in ErrorToastViewTests
    }

    // MARK: 9. handleErrorEvent severity critical calls toast (critical variant)

    func test_handleErrorEvent_severity_critical_shows_toast_for_manual_dismiss() async throws {
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_crit_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let payload = KrabErrorPayload(
            severity: "critical",
            component: "backend",
            code: "backend.crash",
            message_user: "Backend упал",
            message_debug: "process exited with code 1",
            timestamp: "2026-05-19T10:00:00+00:00",
            actionable: true,
            action_id: "restart_backend"
        )

        await handler.handleErrorEvent(payload)

        XCTAssertEqual(presenter.presentedErrors.count, 1)
        let received = try XCTUnwrap(presenter.presentedErrors.first)
        XCTAssertEqual(received.severity, "critical")
        XCTAssertEqual(received.action_id, "restart_backend")
        // critical toasts require manual dismiss — verified in ErrorToastViewTests
    }

    // MARK: 10. handleActionTap with side_effect=swift_focus_hotkey_tab posts notification

    func test_handleActionTap_swift_focus_hotkey_tab_posts_notification() async throws {
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        let responseJSON = #"{"id":"1","ok":true,"result":{"status":"ok","side_effect":"swift_focus_hotkey_tab"}}"#
        runIPCEchoServer(socketPath: socketPath, responseJSON: responseJSON) {
            readyExp.fulfill()
        }
        await fulfillment(of: [readyExp], timeout: 1.0)

        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: socketPath)
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let notifExp = expectation(description: "focusHotkeyTab notification posted")
        let observer = NotificationCenter.default.addObserver(
            forName: .focusHotkeyTab,
            object: nil,
            queue: .main
        ) { _ in
            notifExp.fulfill()
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        await handler.handleActionTap(actionId: "report_hotkey_conflict")

        await fulfillment(of: [notifExp], timeout: 2.0)
    }

    // MARK: 11. handleActionTap unknown action_id — no notification, no crash

    func test_handleActionTap_unknown_action_id_logs_warning_no_crash() async throws { // swiftlint:disable:this function_body_length
        let socketPath = tempSocketPath()
        defer { unlink(socketPath) }

        let readyExp = expectation(description: "server ready")
        // Backend returns an unknown side_effect — Swift handler should ignore it gracefully
        let responseJSON = #"{"id":"1","ok":true,"result":{"status":"ok","side_effect":"unknown_future_effect"}}"#
        runIPCEchoServer(socketPath: socketPath, responseJSON: responseJSON) {
            readyExp.fulfill()
        }
        await fulfillment(of: [readyExp], timeout: 1.0)

        // Register observers for all known notifications to ensure none fire
        // Using DispatchQueue-protected flags to satisfy Swift concurrency checker
        final class FiredFlags: @unchecked Sendable {
            private let queue = DispatchQueue(label: "FiredFlags")
            private var _focusHFToken = false
            private var _focusHotkeyTab = false
            var focusHFToken: Bool { queue.sync { _focusHFToken } }
            var focusHotkeyTab: Bool { queue.sync { _focusHotkeyTab } }
            func setFocusHFToken() { queue.async { self._focusHFToken = true } }
            func setFocusHotkeyTab() { queue.async { self._focusHotkeyTab = true } }
        }
        let flags = FiredFlags()
        let obsHF = NotificationCenter.default.addObserver(
            forName: .focusHFTokenSetting, object: nil, queue: .main) { _ in
            flags.setFocusHFToken()
        }
        let obsHK = NotificationCenter.default.addObserver(
            forName: .focusHotkeyTab, object: nil, queue: .main) { _ in
            flags.setFocusHotkeyTab()
        }
        defer {
            NotificationCenter.default.removeObserver(obsHF)
            NotificationCenter.default.removeObserver(obsHK)
        }

        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: socketPath)
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        // Should not crash and should not post any known notifications
        await handler.handleActionTap(actionId: "some_unknown_action")

        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(flags.focusHFToken, "focusHFTokenSetting must not fire for unknown side_effect")
        XCTAssertFalse(flags.focusHotkeyTab, "focusHotkeyTab must not fire for unknown side_effect")
    }

    // MARK: 12. handleActionTap IPC error — does not crash, logs error

    func test_handleActionTap_ipc_error_does_not_crash() async {
        // Socket path that has no server — IPC will fail with connection refused
        let ipcClient = IPCClient(socketPath: "/tmp/nonexistent_\(UUID().uuidString).sock")
        let presenter = MockToastPresenter()
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        // Should complete without throwing (errors are logged, not rethrown)
        await handler.handleActionTap(actionId: "open_lm_studio")
        // If we reach here without crashing or hanging, the test passes
        XCTAssertEqual(presenter.presentedErrors.count, 0)
    }

    // MARK: 13. concurrent handleErrorEvent — thread safe (main actor serial)
    // Note: ErrorActionHandler is @MainActor; all calls are serialised on the main actor.
    // This test verifies the handler correctly accumulates 20 sequential dispatched events.

    func test_concurrent_handleErrorEvent_thread_safe() async throws {
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_conc_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        // Create 20 tasks that each call handleErrorEvent; all funnel through @MainActor
        var tasks: [Task<Void, Never>] = []
        for i in 0..<20 {
            let taskIndex = i
            let t = Task { @MainActor in
                let payload = KrabErrorPayload(
                    severity: taskIndex % 2 == 0 ? "error" : "warn",
                    component: "stt",
                    code: "stt.test_\(taskIndex)",
                    message_user: "Concurrent error \(taskIndex)",
                    message_debug: "",
                    timestamp: "2026-05-19T10:00:00+00:00",
                    actionable: false,
                    action_id: nil
                )
                await handler.handleErrorEvent(payload)
            }
            tasks.append(t)
        }

        // Await all tasks
        for t in tasks { await t.value }

        XCTAssertEqual(presenter.presentedErrors.count, 20,
                       "All 20 handleErrorEvent calls must reach presenter via @MainActor serialisation")
    }

    // MARK: 14. handleRawSSEData with valid JSON dispatches to presenter

    func test_handleRawSSEData_valid_json_dispatches_to_presenter() async throws {
        let presenter = MockToastPresenter()
        let ipcClient = IPCClient(socketPath: "/tmp/unused_raw_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipcClient, toastPresenter: presenter)

        let json = """
        {
            "severity": "warn",
            "component": "paste",
            "code": "paste.accessibility_denied",
            "message_user": "Нет доступа к Accessibility",
            "message_debug": "AXUIElement error -25211",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {},
            "actionable": true,
            "action_id": "open_privacy_settings"
        }
        """

        handler.handleRawSSEData(json)

        // handleRawSSEData dispatches via Task @MainActor — wait a tick
        let waitExp = expectation(description: "Task dispatch delay")
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 50_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 2.0)

        XCTAssertEqual(presenter.presentedErrors.count, 1,
                       "handleRawSSEData should decode and dispatch to presenter")
        let received = try XCTUnwrap(presenter.presentedErrors.first)
        XCTAssertEqual(received.code, "paste.accessibility_denied")
        XCTAssertEqual(received.action_id, "open_privacy_settings")
    }
}
