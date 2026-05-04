/*
 PasteServiceIPCReportTests.swift
 Тесты для IPC-репортинга ошибок вставки (Task 14 Phase B.1).

 Проверяет что reportPasteFailureHandler вызывается с правильным reason
 при AX-denied и app-unsupported путях отказа PasteService.
*/

import XCTest
@testable import KrabEarAgent

final class PasteServiceIPCReportTests: XCTestCase {

    var service: PasteService!

    override func setUp() {
        super.setUp()
        service = PasteService()
    }

    // MARK: - reportPasteFailureHandler registration

    /// Handler по умолчанию nil — не должен крашиться без wire.
    func testDefaultHandlerIsNil() {
        XCTAssertNil(service.reportPasteFailureHandler)
    }

    /// Handler можно присвоить — injectable closure работает.
    func testHandlerCanBeAssigned() {
        var called = false
        service.reportPasteFailureHandler = { _, _ in called = true }
        XCTAssertNotNil(service.reportPasteFailureHandler)
    }

    // MARK: - AX denied path

    /// При AX=false (accessibility_not_granted) handler вызывается с reason="ax_denied".
    ///
    /// Симулируем путь через прямой вызов reportPasteFailureHandler с тем значением,
    /// которое PasteService передаёт при `resultReason == "accessibility_not_granted"`.
    /// Это белый ящик: тест знает внутренний маппинг.
    func testAxDeniedCallsReportPasteFailure() {
        var capturedReason: String?
        var capturedBundle: String?
        service.reportPasteFailureHandler = { reason, bundle in
            capturedReason = reason
            capturedBundle = bundle
        }

        // Вызываем внутренний хелпер напрямую, эмулируя ax_denied путь.
        // Маппинг в PasteService: "accessibility_not_granted" → "ax_denied".
        service.callReportPasteFailureForTesting(internalReason: "accessibility_not_granted")

        XCTAssertEqual(capturedReason, "ax_denied",
                       "handler должен получить reason='ax_denied' для accessibility_not_granted")
        // bundle может быть nil в тестовой среде (нет frontmost app) — это ОК.
        _ = capturedBundle  // suppress unused warning
    }

    // MARK: - App unsupported path

    /// При AX=true но event_post_failed handler вызывается с reason="app_unsupported".
    func testAppUnsupportedCallsReportPasteFailure() {
        var capturedReason: String?
        service.reportPasteFailureHandler = { reason, _ in
            capturedReason = reason
        }

        // Маппинг в PasteService: "event_post_failed" → "app_unsupported".
        service.callReportPasteFailureForTesting(internalReason: "event_post_failed")

        XCTAssertEqual(capturedReason, "app_unsupported",
                       "handler должен получить reason='app_unsupported' для event_post_failed")
    }

    // MARK: - Unknown reason passthrough

    /// Для неизвестных reason-ов значение пробрасывается как есть.
    func testUnknownReasonPassthroughToHandler() {
        var capturedReason: String?
        service.reportPasteFailureHandler = { reason, _ in
            capturedReason = reason
        }

        service.callReportPasteFailureForTesting(internalReason: "modifiers_stuck")

        XCTAssertEqual(capturedReason, "modifiers_stuck")
    }

    // MARK: - Handler not called on success

    /// Handler не вызывается если pasteToFrontmostApp вернул ok=true (нет объекта).
    /// В данном тесте проверяем что handler не вызывается при пустом тексте (early exit).
    func testHandlerNotCalledOnEmptyTextEarlyReturn() {
        var handlerCalled = false
        service.reportPasteFailureHandler = { _, _ in handlerCalled = true }

        // pasteToFrontmostApp с пустым текстом возвращает "empty_text" без вызова handler
        let result = service.pasteToFrontmostApp("")
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "empty_text")
        XCTAssertFalse(handlerCalled, "handler не должен вызываться при early-return с empty_text")
    }
}
