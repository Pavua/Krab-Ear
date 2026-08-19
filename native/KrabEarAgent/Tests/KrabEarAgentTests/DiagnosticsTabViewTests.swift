/*
 DiagnosticsTabViewTests — тесты DiagnosticsTabViewController (Phase B.2 F6).

 Покрытие:
 1. test_filter_only_critical_shows_only_critical_rows
    — activeSeverities={"critical"} → filteredErrors содержит только critical.
 2. test_component_filter_dropdown
    — activeComponent="rewriter" → filteredErrors содержат только rewriter.
 3. test_empty_state_shown_when_no_errors
    — allErrors=[] → emptyStateLabel.isHidden == false.
 4. test_actionable_button_calls_handle_error_action
    — handleActionButtonTap(at:) с actionable payload → IPC вызов confirm.
 5. test_action_button_label_from_context_overrides_default
    — context["action_label"] переопределяет дефолтный лейбл "Исправить"
    (симметрично ErrorToastViewTests.test_action_label_from_context_overrides_default).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Helpers

private func makePayload(
    severity: String,
    component: String,
    actionable: Bool = false,
    actionId: String? = nil,
    context: [String: AnyCodable] = [:]
) -> KrabErrorPayload {
    KrabErrorPayload(
        severity: severity,
        component: component,
        code: "TEST_\(severity.uppercased())",
        message_user: "Test message [\(severity)/\(component)]",
        message_debug: "debug",
        timestamp: "2026-05-04T12:00:00.000Z",
        context: context,
        actionable: actionable,
        action_id: actionId
    )
}

private func tempSocketPath() -> String {
    let name = "krabear_diagtab_\(Int.random(in: 100_000...999_999)).sock"
    return (NSTemporaryDirectory() as NSString).appendingPathComponent(name)
}

// MARK: - DiagnosticsTabViewTests

@MainActor
final class DiagnosticsTabViewTests: XCTestCase {

    // MARK: 1. Severity filter — only critical

    func test_filter_only_critical_shows_only_critical_rows() {
        let socketPath = "/tmp/unused_diag_\(UUID().uuidString).sock"
        let ipcClient = IPCClient(socketPath: socketPath)
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        // Populate with mixed severities
        let errors: [KrabErrorPayload] = [
            makePayload(severity: "info",     component: "stt"),
            makePayload(severity: "warn",     component: "mlx"),
            makePayload(severity: "error",    component: "rewriter"),
            makePayload(severity: "critical", component: "paste"),
            makePayload(severity: "critical", component: "stt"),
        ]

        // Inject via internal state (white-box test)
        vc.allErrors = errors

        // Set only critical active
        vc.activeSeverities = ["critical"]
        vc.activeComponent = nil

        vc.applyFilter()

        XCTAssertEqual(vc.filteredErrors.count, 2)
        XCTAssertTrue(vc.filteredErrors.allSatisfy { $0.severity == "critical" })
    }

    // MARK: 2. Component filter

    func test_component_filter_dropdown() {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_comp_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        let errors: [KrabErrorPayload] = [
            makePayload(severity: "error", component: "rewriter"),
            makePayload(severity: "warn",  component: "stt"),
            makePayload(severity: "error", component: "rewriter"),
            makePayload(severity: "info",  component: "history"),
        ]
        vc.allErrors = errors
        vc.activeSeverities = ["info", "warn", "error", "critical"]
        vc.activeComponent = "rewriter"

        vc.applyFilter()

        XCTAssertEqual(vc.filteredErrors.count, 2)
        XCTAssertTrue(vc.filteredErrors.allSatisfy { $0.component == "rewriter" })
    }

    // MARK: 3. Empty state visible when no errors

    func test_empty_state_shown_when_no_errors() {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_empty_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        // Load the view hierarchy so emptyStateLabel is set up
        _ = vc.view

        vc.allErrors = []
        vc.activeSeverities = ["info", "warn", "error", "critical"]
        vc.activeComponent = nil

        vc.applyFilter()

        XCTAssertEqual(vc.filteredErrors.count, 0)
        // emptyStateLabel should be visible (isHidden = false) when no errors
        XCTAssertFalse(vc.emptyStateLabel.isHidden, "emptyStateLabel should be visible when filteredErrors is empty")
    }

    // MARK: 4. emptyStateLabel hidden when errors exist

    func test_empty_state_hidden_when_errors_exist() {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_notempty_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)
        _ = vc.view

        vc.allErrors = [makePayload(severity: "error", component: "stt")]
        vc.activeSeverities = ["error"]
        vc.activeComponent = nil

        vc.applyFilter()

        XCTAssertEqual(vc.filteredErrors.count, 1)
        XCTAssertTrue(vc.emptyStateLabel.isHidden, "emptyStateLabel should be hidden when filteredErrors is non-empty")
    }

    // MARK: 5. Actionable button calls handle_error_action via IPC

    func test_actionable_button_calls_handle_error_action() {
        // Spin up a Unix socket echo server that records the received request
        let socketPath = tempSocketPath()
        defer { try? FileManager.default.removeItem(atPath: socketPath) }

        let capturedMethod = NSMutableArray()
        let capturedParams = NSMutableArray()
        let expectation = XCTestExpectation(description: "IPC handle_error_action called")

        let responseJSON = #"{"id":"1","ok":true,"result":{"acknowledged":true}}"#

        // Start echo server using the same pattern as ErrorActionHandlerTests
        let serverFd = socket(AF_UNIX, SOCK_STREAM, 0)
        precondition(serverFd >= 0)
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let socketPathLocal = socketPath
        withUnsafeMutableBytes(of: &addr.sun_path) { ptr in
            for (i, b) in socketPathLocal.utf8.enumerated() {
                ptr[i] = b
            }
        }
        let pathLen = socketPath.utf8.count
        let addrLen = socklen_t(MemoryLayout<sa_family_t>.size + pathLen + 1)
        let bound: Int32 = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(serverFd, $0, addrLen)
            }
        }
        guard bound == 0 else {
            XCTFail("bind() failed: \(String(cString: strerror(errno)))")
            close(serverFd)
            return
        }
        precondition(Darwin.listen(serverFd, 1) == 0)

        DispatchQueue.global(qos: .utility).async {
            let clientFd = Darwin.accept(serverFd, nil, nil)
            guard clientFd >= 0 else { close(serverFd); return }
            defer { close(clientFd); close(serverFd) }
            var buf = [UInt8](repeating: 0, count: 4096)
            let bytesRead = Darwin.read(clientFd, &buf, buf.count)
            if bytesRead > 0 {
                let requestStr = String(bytes: buf.prefix(bytesRead), encoding: .utf8) ?? ""
                if let data = requestStr.data(using: .utf8),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let method = json["method"] as? String {
                        capturedMethod.add(method)
                    }
                    if let params = json["params"] as? [String: Any],
                       let actionId = params["action_id"] as? String {
                        capturedParams.add(actionId)
                    }
                }
            }
            let responseBytes = Array((responseJSON + "\n").utf8)
            _ = responseBytes.withUnsafeBytes { Darwin.write(clientFd, $0.baseAddress, $0.count) }
            expectation.fulfill()
        }

        let ipcClient = IPCClient(socketPath: socketPath)
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        // Inject actionable payload
        let actionId = "restart_rewriter"
        let errors = [makePayload(severity: "error", component: "rewriter", actionable: true, actionId: actionId)]
        vc.allErrors = errors
        vc.activeSeverities = ["error"]
        vc.activeComponent = nil
        vc.applyFilter()

        XCTAssertEqual(vc.filteredErrors.count, 1)
        XCTAssertTrue(vc.filteredErrors[0].actionable)

        // Trigger action
        vc.handleActionButtonTap(at: 0)

        wait(for: [expectation], timeout: 5.0)

        XCTAssertEqual(capturedMethod.firstObject as? String, "handle_error_action",
                       "Should call handle_error_action IPC method")
        XCTAssertEqual(capturedParams.firstObject as? String, actionId,
                       "Should send correct action_id")
    }

    // MARK: 6. Non-actionable row: handleActionButtonTap is a no-op

    func test_non_actionable_row_is_noop() {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_noop_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        let errors = [makePayload(severity: "info", component: "stt", actionable: false, actionId: nil)]
        vc.allErrors = errors
        vc.activeSeverities = ["info"]
        vc.activeComponent = nil
        vc.applyFilter()

        // Should not throw or crash
        vc.handleActionButtonTap(at: 0)

        // Just verify it doesn't crash and filtered count is still 1
        XCTAssertEqual(vc.filteredErrors.count, 1)
    }

    // MARK: 7. Filter with both severity AND component

    func test_combined_severity_and_component_filter() {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_combined_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        let errors: [KrabErrorPayload] = [
            makePayload(severity: "error", component: "stt"),
            makePayload(severity: "error", component: "mlx"),
            makePayload(severity: "warn",  component: "stt"),
            makePayload(severity: "critical", component: "stt"),
        ]
        vc.allErrors = errors
        vc.activeSeverities = ["error", "warn"]  // no critical
        vc.activeComponent = "stt"

        vc.applyFilter()

        // Should match: error+stt, warn+stt (2 rows). Not mlx, not critical+stt.
        XCTAssertEqual(vc.filteredErrors.count, 2)
        XCTAssertTrue(vc.filteredErrors.allSatisfy { $0.component == "stt" })
        XCTAssertTrue(vc.filteredErrors.allSatisfy { ["error", "warn"].contains($0.severity) })
    }

    // MARK: 8. Action button label — context["action_label"] overrides default

    func test_action_button_label_from_context_overrides_default() throws {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_actionlabel_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        let payload = makePayload(
            severity: "error",
            component: "mlx",
            actionable: true,
            actionId: "unload_model",
            context: ["action_label": AnyCodable(value: "Выгрузить модель")]
        )
        vc.allErrors = [payload]
        vc.activeSeverities = ["error"]
        vc.activeComponent = nil
        vc.applyFilter()

        let actionColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("action"))
        let container = try XCTUnwrap(
            vc.tableView(NSTableView(), viewFor: actionColumn, row: 0),
            "Action column должна вернуть контейнер для actionable payload"
        )
        let button = try XCTUnwrap(
            container.subviews.compactMap { $0 as? NSButton }.first,
            "Контейнер должен содержать NSButton"
        )

        XCTAssertEqual(
            button.title,
            "Выгрузить модель",
            "Подпись кнопки должна браться из context['action_label'], а не быть литералом 'Исправить'"
        )
    }

    // MARK: 9. Action button label — default "Исправить" when no context

    func test_action_button_label_default_when_no_context() throws {
        let ipcClient = IPCClient(socketPath: "/tmp/unused_actionlabel_default_\(UUID().uuidString).sock")
        let vc = DiagnosticsTabViewController(ipcClient: ipcClient)

        let payload = makePayload(
            severity: "error",
            component: "mlx",
            actionable: true,
            actionId: "unload_model"
        )
        vc.allErrors = [payload]
        vc.activeSeverities = ["error"]
        vc.activeComponent = nil
        vc.applyFilter()

        let actionColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("action"))
        let container = try XCTUnwrap(
            vc.tableView(NSTableView(), viewFor: actionColumn, row: 0)
        )
        let button = try XCTUnwrap(container.subviews.compactMap { $0 as? NSButton }.first)

        XCTAssertEqual(
            button.title,
            "Исправить",
            "Дефолтная подпись при отсутствии context['action_label'] должна остаться прежней"
        )
    }
}
