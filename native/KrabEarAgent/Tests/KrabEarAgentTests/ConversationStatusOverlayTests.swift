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

// MARK: - Укрепление 1 (code-review батч 3): off-screen guard в restorePosition()

/// Портировано из RealtimeOverlayController.restoreSavedPosition() (M2): сохранённая
/// позиция применяется, только если ≥80% frame пересекается с visibleFrame какого-нибудь
/// ТЕКУЩЕГО экрана. Без этой проверки отключение второго монитора навсегда прячет
/// панель за экраном (сохранённая позиция ссылалась на уже не существующий экран).
@MainActor
final class ConversationStatusOverlayPositionGuardTests: XCTestCase {

    private let positionKey = "KrabEar_ConversationStatusHUDPosition"

    override func tearDown() async throws {
        UserDefaults.standard.removeObject(forKey: positionKey)
        try await super.tearDown()
    }

    private func savePosition(x: CGFloat, y: CGFloat) {
        let dict: [String: CGFloat] = ["x": x, "y": y]
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let str = String(data: data, encoding: .utf8) else {
            return XCTFail("не удалось сериализовать тестовую позицию")
        }
        UserDefaults.standard.set(str, forKey: positionKey)
    }

    /// Заведомо off-screen сохранённая позиция (например после отключения второго
    /// монитора) НЕ должна применяться безусловно. Инвариант держится независимо от
    /// headless-среды CI: (99999, 99999) не может пересекаться ни с одним реальным
    /// экраном ≥80%, поэтому кандидат никогда не проходит guard — panel либо падает
    /// на placeTopRight() (есть NSScreen.main), либо остаётся на дефолтном contentRect
    /// панели (экранов нет вовсе). Ни один из исходов не равен (99999, 99999).
    func test_restorePosition_offScreen_doesNotApplyBogusOrigin() {
        savePosition(x: 99999, y: 99999)

        let overlay = ConversationStatusOverlay()
        defer { overlay.hide() }

        XCTAssertNotEqual(overlay._testPanelOrigin.x, 99999)
        XCTAssertNotEqual(overlay._testPanelOrigin.y, 99999)
    }

    /// Позитивный кейс: позиция внутри видимой области реального экрана восстанавливается
    /// как есть. Пропускается в headless-среде без NSScreen.main (нечего проверять).
    func test_restorePosition_onScreen_appliesSavedOrigin() throws {
        guard let screen = NSScreen.main else {
            throw XCTSkip("нет NSScreen.main в этой среде — позитивный кейс непроверяем headless")
        }
        let vf = screen.visibleFrame
        let x = vf.minX + 40
        let y = vf.minY + 40
        savePosition(x: x, y: y)

        let overlay = ConversationStatusOverlay()
        defer { overlay.hide() }

        XCTAssertEqual(overlay._testPanelOrigin.x, x, accuracy: 0.5)
        XCTAssertEqual(overlay._testPanelOrigin.y, y, accuracy: 0.5)
    }
}
