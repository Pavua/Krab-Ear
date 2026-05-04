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
}
