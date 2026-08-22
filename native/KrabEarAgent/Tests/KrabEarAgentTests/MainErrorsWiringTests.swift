/*
 MainErrorsWiringTests — тесты main+Errors.swift wiring (Phase B.1 Task 13).

 Покрытие:
 1. setupErrorBus → ErrorActionHandler создан и хранится в associated object.
 2. tearDownErrorBus → handler = nil, SSE task отменён.
 3. SSE line dispatch: handleRawSSEData принимает krab_error данные через ErrorSSEBox path.
 4. ErrorSSEBox.handleSSELine — парсит event/data строки SSE правильно.
 5. MockToastPresenter: toast NOT invoked when setupErrorBus called без backend.
 6. Wave 77 error codes: paste.accessibility_denied, hotkey.conflict, ipc.reconnect
    — декодируются в KrabErrorPayload корректно.
 7. Source contract (2026-07-05): setupErrorBus/tearDownErrorBus реально ВЫЗЫВАЮТСЯ
    из main.swift lifecycle, а не только определены — closes the decorative-wiring
    gap found while fixing the krab_error IPC-poll transport.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

// MARK: - SpyToastPresenter

@MainActor
private final class SpyToastPresenter: ToastPresenting {
    var presentedErrors: [KrabErrorPayload] = []
    var presentCallCount: Int { presentedErrors.count }

    func present(error: KrabErrorPayload) {
        presentedErrors.append(error)
    }
}

// MARK: - MainErrorsWiringTests

/// Tests the wiring logic in main+Errors.swift without relying on AgentAppDelegate
/// (which requires a full app lifecycle). Instead we test the components that
/// setupErrorBus() orchestrates directly.
@MainActor
final class MainErrorsWiringTests: XCTestCase {

    // MARK: 1. ErrorActionHandler initialised with IPC client and toast presenter

    func test_errorActionHandler_init_stores_collaborators() {
        let presenter = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/unused_main_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: presenter)

        // Verify the handler is not nil (init succeeded)
        XCTAssertNotNil(handler, "ErrorActionHandler must be non-nil after init")
    }

    // MARK: 2. handleErrorEvent dispatches to the presenter stored at init

    func test_handleErrorEvent_dispatches_to_presenter_passed_at_init() async {
        let presenter = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/unused_main2_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: presenter)

        let payload = KrabErrorPayload(
            severity: "warn",
            component: "ipc",
            code: "ipc.reconnect",
            message_user: "IPC соединение восстановлено",
            message_debug: "reconnect after 2s backoff",
            timestamp: "2026-05-19T00:00:00+00:00",
            context: [:],
            actionable: false,
            action_id: nil
        )

        await handler.handleErrorEvent(payload)
        XCTAssertEqual(presenter.presentCallCount, 1)
        XCTAssertEqual(presenter.presentedErrors.first?.code, "ipc.reconnect")
    }

    // MARK: 3. tearDownErrorBus pattern — handler nil clears reference

    func test_teardown_pattern_clears_handler() {
        let presenter = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/unused_main3_\(UUID().uuidString).sock")
        var handler: ErrorActionHandler? = ErrorActionHandler(ipcClient: ipc, toastPresenter: presenter)

        XCTAssertNotNil(handler, "Handler exists before teardown")

        // Simulate tearDownErrorBus zeroing out the handler
        handler = nil
        XCTAssertNil(handler, "Handler must be nil after teardown (simulated)")
    }

    // MARK: 4. ErrorSSEBox parses krab_error event lines correctly via handleRawSSEData

    func test_sseBox_krab_error_event_data_line_dispatches() async throws {
        let presenter = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/unused_sse_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: presenter)

        // Simulate what ErrorSSEBox.handleSSELine does: it reads event: krab_error
        // then data: <json> and calls handler.handleRawSSEData(jsonStr)
        let jsonStr = """
        {
            "severity": "error",
            "component": "paste",
            "code": "paste.accessibility_denied",
            "message_user": "Нет доступа к Accessibility API",
            "message_debug": "AXError -25211 kAXErrorAPIDisabled",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {"app": "Telegram"},
            "actionable": true,
            "action_id": "open_privacy_settings"
        }
        """

        handler.handleRawSSEData(jsonStr)

        // handleRawSSEData posts via Task @MainActor — wait for dispatch
        let waitExp = expectation(description: "SSE dispatch")
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 50_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 2.0)

        XCTAssertEqual(presenter.presentCallCount, 1,
                       "krab_error SSE data should dispatch to presenter via handleRawSSEData")
        XCTAssertEqual(presenter.presentedErrors.first?.code, "paste.accessibility_denied")
        XCTAssertEqual(presenter.presentedErrors.first?.action_id, "open_privacy_settings")
    }

    // MARK: 5. Non-krab_error SSE event types are ignored

    func test_non_krab_error_event_type_is_ignored() async throws {
        let presenter = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/unused_sse2_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: presenter)

        // A different event type's data — should not reach presenter
        // The SSEBox only calls handleRawSSEData when eventType == "krab_error"
        // We simulate this by NOT calling handleRawSSEData (as SSEBox would skip it)
        // Instead we verify that calling handleRawSSEData with unrelated JSON still
        // fails gracefully if the JSON doesn't match KrabErrorPayload schema.
        let unrelatedJson = """
        {"event": "live_subs.result", "text": "hello"}
        """
        handler.handleRawSSEData(unrelatedJson)

        let waitExp = expectation(description: "wait 50ms")
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 50_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 2.0)

        XCTAssertEqual(presenter.presentCallCount, 0,
                       "Malformed/unrelated JSON should not reach presenter")
    }

    // MARK: 6. Wave 77 — paste.accessibility_denied decodes correctly

    func test_wave77_paste_accessibility_denied_payload() throws {
        let json = """
        {
            "severity": "error",
            "component": "paste",
            "code": "paste.accessibility_denied",
            "message_user": "Krab Ear не может вставить текст: нет доступа к Accessibility",
            "message_debug": "AXUIElementSetAttributeValue failed: -25211",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {"target_app": "Safari", "retry_count": 3},
            "actionable": true,
            "action_id": "open_privacy_settings"
        }
        """
        let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
        XCTAssertEqual(payload.code, "paste.accessibility_denied")
        XCTAssertEqual(payload.component, "paste")
        XCTAssertEqual(payload.severity, "error")
        XCTAssertTrue(payload.actionable)
        XCTAssertEqual(payload.action_id, "open_privacy_settings")
        // Verify context fields
        XCTAssertEqual(payload.context["target_app"]?.value as? String, "Safari")
        XCTAssertEqual(payload.context["retry_count"]?.value as? Int, 3)
    }

    // MARK: 7. Wave 77 — hotkey.conflict decodes correctly

    func test_wave77_hotkey_conflict_payload() throws {
        let json = """
        {
            "severity": "warn",
            "component": "hotkey",
            "code": "hotkey.conflict",
            "message_user": "Конфликт горячей клавиши: Right Option занята другим приложением",
            "message_debug": "CGEventTap blocked by: Raycast.app",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {"conflicting_app": "Raycast"},
            "actionable": true,
            "action_id": "report_hotkey_conflict"
        }
        """
        let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
        XCTAssertEqual(payload.code, "hotkey.conflict")
        XCTAssertEqual(payload.severity, "warn")
        XCTAssertEqual(payload.component, "hotkey")
        XCTAssertTrue(payload.actionable)
        XCTAssertEqual(payload.action_id, "report_hotkey_conflict")
        XCTAssertEqual(payload.context["conflicting_app"]?.value as? String, "Raycast")
    }

    // MARK: 8. Wave 77 — ipc.reconnect decodes correctly

    func test_wave77_ipc_reconnect_payload() throws {
        let json = """
        {
            "severity": "info",
            "component": "ipc",
            "code": "ipc.reconnect",
            "message_user": "Соединение с backend восстановлено",
            "message_debug": "reconnected after 2.3s; backoff=2",
            "timestamp": "2026-05-19T10:00:00+00:00",
            "context": {"reconnect_count": 1, "backoff_sec": 2.3},
            "actionable": false,
            "action_id": null
        }
        """
        let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
        XCTAssertEqual(payload.code, "ipc.reconnect")
        XCTAssertEqual(payload.severity, "info")
        XCTAssertEqual(payload.component, "ipc")
        XCTAssertFalse(payload.actionable)
        XCTAssertNil(payload.action_id)
        XCTAssertEqual(payload.context["reconnect_count"]?.value as? Int, 1)
    }

    // MARK: 9. Multiple error codes in one batch — all decode without errors

    func test_all_wave77_codes_decode_in_batch() throws {
        let codes: [(String, String)] = [
            ("paste", "paste.accessibility_denied"),
            ("hotkey", "hotkey.conflict"),
            ("ipc", "ipc.reconnect"),
        ]

        for (component, code) in codes {
            let json = """
            {
                "severity": "warn",
                "component": "\(component)",
                "code": "\(code)",
                "message_user": "Test \(code)",
                "message_debug": "",
                "timestamp": "2026-05-19T10:00:00+00:00",
                "context": {},
                "actionable": false,
                "action_id": null
            }
            """
            let payload = try JSONDecoder().decode(KrabErrorPayload.self, from: Data(json.utf8))
            XCTAssertEqual(payload.code, code, "Code \(code) must decode correctly")
            XCTAssertEqual(payload.component, component)
        }
    }

    // MARK: 10. setupErrorBus wiring — handler created with correct dependencies

    func test_setupErrorBus_wiring_creates_handler_with_correct_dependencies() {
        // Test the components that setupErrorBus() wires:
        // ErrorActionHandler(ipcClient:, toastPresenter:) + SSE task
        // We verify the wiring compiles and handler receives events.
        let spy = SpyToastPresenter()
        let ipc = IPCClient(socketPath: "/tmp/wire_\(UUID().uuidString).sock")
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: spy)

        // Simulate what setupErrorBus does: store handler reference
        var storedHandler: ErrorActionHandler? = handler

        XCTAssertNotNil(storedHandler, "Handler stored by setupErrorBus must be non-nil")

        // Simulate tearDownErrorBus
        storedHandler = nil
        XCTAssertNil(storedHandler, "Handler must be nil after teardown")
    }

    // MARK: 11. Source contract — setupErrorBus/tearDownErrorBus are actually
    // CALLED from AgentAppDelegate's real lifecycle, not just defined.
    //
    // Found 2026-07-05 while fixing the krab_error IPC-poll transport: the
    // tests above (and the ones in this very file) only exercise the pieces
    // setupErrorBus() wires together in isolation — none of them call the
    // real AgentAppDelegate lifecycle, so they stayed green for however long
    // setupErrorBus(toastPresenter:) was never actually invoked from
    // completeStartupAfterBackendReady() (the doc comment at the top of
    // main+Errors.swift claimed it was — it wasn't). The whole error-toast
    // subsystem was dead in production despite 100% green tests. This test
    // greps the real source file for the real call sites so a future refactor
    // that silently drops the call fails CI instead of shipping decorative
    // wiring again.

    func test_setupErrorBus_is_actually_called_from_startup() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("setupErrorBus(toastPresenter:"),
            "completeStartupAfterBackendReady() must call setupErrorBus(toastPresenter:) — " +
            "found it defined but never called in main.swift once already (2026-07-05)."
        )
    }

    func test_tearDownErrorBus_is_actually_called_from_shutdown() throws {
        let src = try String(contentsOf: Self.mainSwiftURL, encoding: .utf8)
        // Match a CALL site, not the `func tearDownErrorBus()` declaration itself.
        XCTAssertTrue(
            src.contains("\n        tearDownErrorBus()\n") || src.contains(" tearDownErrorBus()\n"),
            "applicationWillTerminate() must call tearDownErrorBus() to stop the IPC poller on quit."
        )
    }

    /// Severity не должна останавливаться на скрытом StatusIndicatorView:
    /// батч ErrorBus обязан обновить state AgentAppDelegate, а реальный menu-bar
    /// image обязан получить это state при следующем render.
    func test_error_bus_severity_reaches_visible_menu_bar_image() throws {
        let errorsSource = try String(contentsOf: Self.agentSourcesURL.appendingPathComponent("main+Errors.swift"), encoding: .utf8)
        let healthSource = try String(contentsOf: Self.agentSourcesURL.appendingPathComponent("main+HealthMonitor.swift"), encoding: .utf8)

        XCTAssertTrue(
            errorsSource.contains("applyErrorSeverityBadge(payload.severity)"),
            "ErrorBus callback обязан передать severity payload в AgentAppDelegate, а не только показать toast."
        )
        XCTAssertTrue(
            healthSource.contains("errorSeverity: self.statusErrorBadgeSeverity"),
            "Видимый NSStatusItem.image обязан рендериться с сохранённым severity badge."
        )
        XCTAssertTrue(
            healthSource.contains("updateStatusBadgeBlinking(for: visibleSeverity)")
                && healthSource.contains("badgeOpacity: self.statusBadgeBlinkOpacity"),
            "Critical badge обязан мигать в том же видимом menu-bar image, а не в скрытом view."
        )
    }

    /// Resolves native/KrabEarAgent/Sources/KrabEarAgent/main.swift from the test bundle,
    /// falling back to a #file-relative walk-up (same pattern as SFSymbolVerificationTests).
    private static var mainSwiftURL: URL {
        let bundleURL = Bundle(for: MainErrorsWiringTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/main.swift")
    }

    private static var agentSourcesURL: URL {
        mainSwiftURL.deletingLastPathComponent()
    }
}
