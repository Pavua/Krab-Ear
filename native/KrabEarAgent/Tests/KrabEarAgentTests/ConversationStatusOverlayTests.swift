/*
 ConversationStatusOverlayTests — Волна 3c, плавающий статус-HUD разговора.
 Панель headless (orderFront в тестах не вызываем на show? вызываем — NSPanel
 без NSApp.run безопасен, прецедент LiveSubtitlesOverlay-тесты).
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class ConversationStatusOverlayTests: XCTestCase {

    private var overlay: ConversationStatusOverlay!

    override func setUp() async throws {
        try await super.setUp()
        overlay = ConversationStatusOverlay()
    }

    override func tearDown() async throws {
        overlay.hide()
        overlay = nil
        UserDefaults.standard.removeObject(forKey: "KrabEar_ConversationStatusHUDPosition")
        try await super.tearDown()
    }

    func test_panel_isFloating_andDraggable() {
        XCTAssertEqual(overlay._testPanelLevel, .floating)
        XCTAssertTrue(overlay._testPanelIsDraggable)
    }

    func test_update_setsStatusText() {
        overlay.update(state: .thinking)
        XCTAssertEqual(overlay._testStatusText, "🟡 Думает")
    }

    func test_update_interruptButton_visibleOnlyWhenSpeaking() {
        overlay.update(state: .speaking)
        XCTAssertFalse(overlay.interruptButton.isHidden)
        overlay.update(state: .listening)
        XCTAssertTrue(overlay.interruptButton.isHidden)
    }

    func test_showHide_togglesIsVisible() {
        overlay.show()
        XCTAssertTrue(overlay.isVisible)
        overlay.hide()
        XCTAssertFalse(overlay.isVisible)
    }

    func test_interruptButton_firesCallback() {
        var fired = 0
        overlay.onInterrupt = { fired += 1 }
        overlay.interruptButton.performClick(nil)
        XCTAssertEqual(fired, 1)
    }

    func test_pushLevel_updatesMeter_noCrash() {
        overlay.show()
        overlay.pushLevel(0.6)  // smoke: не падает, meter принимает значение
    }
}

// MARK: - Task 6: проводка в ConversationViewController

@MainActor
final class ConversationOverlayWiringTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default)
        vc.loadView()
        vc.viewDidLoad()
    }

    override func tearDown() async throws {
        vc.statusOverlay?.hide()
        vc.interruptFallbackTimer?.invalidate()
        vc = nil
        try await super.tearDown()
    }

    func test_shouldShowOverlay_matrix() {
        XCTAssertTrue(ConversationViewController.shouldShowOverlay(sessionActive: true,  windowIsKey: false))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: true,  windowIsKey: true))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: false, windowIsKey: false))
        XCTAssertFalse(ConversationViewController.shouldShowOverlay(sessionActive: false, windowIsKey: true))
    }

    func test_applyState_updatesOverlayText() {
        let overlay = ConversationStatusOverlay()
        vc.statusOverlay = overlay
        vc.conversationState = .speaking
        XCTAssertEqual(overlay._testStatusText, "🔴 Говорит")
    }

    func test_overlayInterrupt_wiredTo_interruptAI() {
        vc.isSessionActive = true
        vc.conversationState = .speaking
        vc.ensureStatusOverlay()
        vc.statusOverlay?.onInterrupt?()
        // interruptAI НЕ переключает состояние сам (Task 2) — но взводит fallback-таймер.
        XCTAssertNotNil(vc.interruptFallbackTimer,
                        "onInterrupt overlay должен вести в interruptAI()")
    }

    func test_computeAndPushLevel_feedsOverlay_noCrash() {
        let overlay = ConversationStatusOverlay()
        vc.statusOverlay = overlay
        vc.computeAndPushLevel([0.4, 0.5, 0.6])  // smoke: пуш в meter overlay не падает
    }
}
