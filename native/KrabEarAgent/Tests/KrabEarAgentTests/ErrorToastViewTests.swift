/*
 ErrorToastViewTests — тесты ErrorToastPresenter (Phase B.1 Task 11).

 Покрытие:
 1. info severity — панель показывается, затем auto-dismiss через 2s.
 2. critical severity — панель НЕ скрывается автоматически через 1s.
 3. Queue drain — при 3 error payload'ах только 1 активен одновременно.
 4. Actionable button — click вызывает handleActionTap у ErrorActionHandler.
 5. action_label из context переопределяет дефолтный лейбл кнопки.

 Паттерны:
 - MockToastPanelFactory: injectable NSPanel factory, исключает экранные побочные эффекты.
 - SpyActionInvoker: protocol-based test double, записывает вызовы без реального IPC.
 - MainActor-synchronized XCTestCase expectations.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

// MARK: - TrackingPanel

/// NSPanel subclass that records whether orderOut(_:) was called.
private final class TrackingPanel: NSPanel {
    var orderOutCallCount = 0

    override func orderOut(_ sender: Any?) {
        orderOutCallCount += 1
        super.orderOut(sender)
    }
}

// MARK: - MockToastPanelFactory

/// Injects TrackingPanel instances so tests can inspect dismiss calls without touching screen.
@MainActor
private final class MockToastPanelFactory: ToastPanelFactory {
    private(set) var createdPanels: [TrackingPanel] = []

    func makePanel(frame: NSRect) -> NSPanel {
        let panel = TrackingPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: true          // defer=true avoids screen flicker in tests
        )
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        createdPanels.append(panel)
        return panel
    }
}

// MARK: - ActionInvoker protocol + spy

/// Protocol abstraction so ErrorToastPresenter can be tested without real IPC.
@MainActor
protocol ActionInvoker: AnyObject {
    func invokeAction(actionId: String) async
}

// MARK: - SpyActionInvoker

@MainActor
private final class SpyActionInvoker: ActionInvoker {
    var invokedActionIds: [String] = []

    func invokeAction(actionId: String) async {
        invokedActionIds.append(actionId)
    }
}

// MARK: - Testable ErrorToastPresenter subclass

/// Thin subclass that replaces the action dispatch with a spy invoker.
@MainActor
private final class TestableErrorToastPresenter: ErrorToastPresenter {
    let spy: SpyActionInvoker

    init(spy: SpyActionInvoker, factory: any ToastPanelFactory) {
        self.spy = spy
        // Build a real ErrorActionHandler backed by a no-op IPC + null toast presenter.
        // We override actionButtonTapped so it never calls the handler.
        let ipc = IPCClient(socketPath: "/tmp/testable_toast_\(UUID().uuidString).sock")
        let nullPresenter = NullToastPresenter()
        let handler = ErrorActionHandler(ipcClient: ipc, toastPresenter: nullPresenter)
        super.init(actionHandler: handler, panelFactory: factory)
    }

    override func invokeActionAsync(actionId: String) async {
        await spy.invokeAction(actionId: actionId)
    }
}

// MARK: - NullToastPresenter

@MainActor
private final class NullToastPresenter: ToastPresenting {
    func present(error: KrabErrorPayload) {}
}

// MARK: - Helpers

private func makePayload(
    severity: String = "error",
    code: String = "test.code",
    message: String = "Тестовое сообщение",
    actionable: Bool = false,
    actionId: String? = nil,
    context: [String: AnyCodable] = [:]
) -> KrabErrorPayload {
    KrabErrorPayload(
        severity: severity,
        component: "test",
        code: code,
        message_user: message,
        message_debug: "",
        timestamp: "2026-05-04T00:00:00+00:00",
        context: context,
        actionable: actionable,
        action_id: actionId
    )
}

// MARK: - ErrorToastViewTests

@MainActor
final class ErrorToastViewTests: XCTestCase {

    // MARK: 1. info severity — panel shows, then auto-dismisses after ~2s

    func test_present_shows_panel_for_info_then_dismisses() async throws {
        let spy = SpyActionInvoker()
        let factory = MockToastPanelFactory()
        let presenter = TestableErrorToastPresenter(spy: spy, factory: factory)

        let payload = makePayload(severity: "info", code: "stt.ready", message: "Модель загружена")

        presenter.present(error: payload)

        // Panel should be visible immediately
        XCTAssertNotNil(presenter.activePanel, "Panel should appear immediately after present()")
        XCTAssertEqual(factory.createdPanels.count, 1, "Exactly one panel should be created")

        // Wait 2.6s — auto-dismiss delay for info is 2s
        let dismissExp = expectation(description: "toast dismissed after info delay")
        Task {
            try await Task.sleep(nanoseconds: 2_600_000_000)
            dismissExp.fulfill()
        }
        await fulfillment(of: [dismissExp], timeout: 5.0)

        XCTAssertNil(presenter.activePanel, "Panel should be nil after info auto-dismiss (2s)")
        let panel = try XCTUnwrap(factory.createdPanels.first)
        XCTAssertGreaterThanOrEqual(
            panel.orderOutCallCount, 1,
            "orderOut should have been called at least once on the info panel"
        )
    }

    // MARK: 2. critical does NOT auto-dismiss after 1s

    func test_critical_does_not_auto_dismiss() async throws {
        let spy = SpyActionInvoker()
        let factory = MockToastPanelFactory()
        let presenter = TestableErrorToastPresenter(spy: spy, factory: factory)

        let payload = makePayload(severity: "critical", code: "backend.crash", message: "Backend упал")
        presenter.present(error: payload)

        XCTAssertNotNil(presenter.activePanel, "Panel should appear immediately")

        // Wait 1.5s — critical has no auto timer, so it must still be visible
        let waitExp = expectation(description: "wait 1.5s")
        Task {
            try await Task.sleep(nanoseconds: 1_500_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 3.0)

        XCTAssertNotNil(
            presenter.activePanel,
            "Critical toast MUST still be visible after 1s — no auto-dismiss"
        )
        let panel = try XCTUnwrap(factory.createdPanels.first)
        XCTAssertEqual(
            panel.orderOutCallCount, 0,
            "orderOut should NOT have been called for critical toast"
        )
    }

    // MARK: 3. Queue drain — only 1 active at a time

    func test_queue_drain_one_at_a_time() async throws {
        let spy = SpyActionInvoker()
        let factory = MockToastPanelFactory()
        let presenter = TestableErrorToastPresenter(spy: spy, factory: factory)

        let payloads = [
            makePayload(severity: "info",  code: "a", message: "A"),
            makePayload(severity: "info",  code: "b", message: "B"),
            makePayload(severity: "info",  code: "c", message: "C"),
        ]

        // Present all three
        payloads.forEach { presenter.present(error: $0) }

        // Only 1 panel should be shown at this moment
        XCTAssertEqual(factory.createdPanels.count, 1, "Only one panel created initially")
        XCTAssertNotNil(presenter.activePanel, "Active panel set")
        XCTAssertEqual(presenter.queue.count, 2, "Two payloads queued")

        // Dismiss the current toast manually
        presenter.dismissCurrentToast()

        // Second toast should now appear
        XCTAssertEqual(factory.createdPanels.count, 2, "Second panel created after first dismissed")
        XCTAssertNotNil(presenter.activePanel, "Second panel active")
        XCTAssertEqual(presenter.queue.count, 1, "One payload still queued")

        // Dismiss second
        presenter.dismissCurrentToast()

        // Third toast
        XCTAssertEqual(factory.createdPanels.count, 3, "Third panel created after second dismissed")
        XCTAssertNotNil(presenter.activePanel, "Third panel active")
        XCTAssertEqual(presenter.queue.count, 0, "Queue empty")

        // Dismiss third
        presenter.dismissCurrentToast()
        XCTAssertNil(presenter.activePanel, "No active panel after all dismissed")
    }

    // MARK: 4. Actionable button calls action invoker

    func test_actionable_button_calls_actionHandler() async throws {
        let spy = SpyActionInvoker()
        let factory = MockToastPanelFactory()
        let presenter = TestableErrorToastPresenter(spy: spy, factory: factory)

        let payload = makePayload(
            severity: "error",
            code: "rewriter.timeout",
            message: "LM Studio не ответил",
            actionable: true,
            actionId: "open_lm_studio"
        )

        presenter.present(error: payload)

        let panel = try XCTUnwrap(factory.createdPanels.first, "Panel should be created")
        let contentView = try XCTUnwrap(panel.contentView, "Panel must have contentView")
        let actionButton = try XCTUnwrap(
            findActionButton(in: contentView),
            "Action button should be present in toast view"
        )

        // Simulate button click
        actionButton.performClick(nil)

        // Give async Task a moment to run
        let waitExp = expectation(description: "async Task.sleep 0.2s")
        Task {
            try await Task.sleep(nanoseconds: 200_000_000)
            waitExp.fulfill()
        }
        await fulfillment(of: [waitExp], timeout: 2.0)

        XCTAssertEqual(
            spy.invokedActionIds,
            ["open_lm_studio"],
            "invokeAction must be called with correct action_id"
        )
        XCTAssertNil(presenter.activePanel, "Toast should be dismissed after button click")
    }

    // MARK: 5. action_label from context overrides default label

    func test_action_label_from_context_overrides_default() throws {
        let spy = SpyActionInvoker()
        let factory = MockToastPanelFactory()
        let presenter = TestableErrorToastPresenter(spy: spy, factory: factory)

        let payload = makePayload(
            severity: "warn",
            code: "diarization.no_token",
            message: "HF token не задан",
            actionable: true,
            actionId: "fix_hf_token",
            context: ["action_label": AnyCodable(value: "Открыть настройки")]
        )

        presenter.present(error: payload)

        let panel = try XCTUnwrap(factory.createdPanels.first, "Panel should be created")
        let contentView = try XCTUnwrap(panel.contentView, "Panel must have contentView")
        let actionButton = try XCTUnwrap(
            findActionButton(in: contentView),
            "Action button should be present when actionable=true"
        )

        XCTAssertEqual(
            actionButton.title,
            "Открыть настройки",
            "Button label should be from context['action_label'], not default 'Действие'"
        )
    }

    // MARK: - Helpers

    /// Recursively finds the first NSButton with a non-empty title that isn't "×" in the view tree.
    private func findActionButton(in view: NSView) -> NSButton? {
        if let button = view as? NSButton, !button.title.isEmpty, button.title != "×" {
            return button
        }
        for sub in view.subviews {
            if let found = findActionButton(in: sub) { return found }
        }
        return nil
    }
}
