import AppKit
import XCTest
@testable import KrabEarAgent

@MainActor
final class CallObserverUITests: XCTestCase {
    private func session(_ id: String = "s1", isScreening: Bool = false, forwardedFrom: String = "") -> VGSessionInfo {
        VGSessionInfo(id: id, status: "running", phone: "+34 600 000 000", forwardedFrom: forwardedFrom,
                      callDirection: "outbound", createdAt: "2026-08-21T10:00:00Z",
                      updatedAt: "2026-08-21T10:00:00Z", srcLang: "es", tgtLang: "ru", callBrief: "",
                      isScreening: isScreening, agentRole: isScreening ? "inbound_screener" : "")
    }

    func test_hud_show_hide_visibility() {
        let hud = CallObserverHUD()
        XCTAssertFalse(hud.isHUDVisible)
        hud.showHUD(session: session())
        XCTAssertTrue(hud.isHUDVisible)
        hud.hideHUD()
        XCTAssertFalse(hud.isHUDVisible)
    }

    func test_hud_buttons_are_sf_symbols_not_text_glyphs() {
        let hud = CallObserverHUD()
        hud.showHUD(session: session())
        XCTAssertNotNil(hud.testHook_listenButton.image, "кнопка прослушки обязана быть SF Symbol")
        XCTAssertNotNil(hud.testHook_hangupButton.image)
        XCTAssertTrue(hud.testHook_listenButton.title.isEmpty, "никаких эмодзи-тайтлов (AGENT-J/M)")
        hud.hideHUD()
    }

    func test_hud_click_vs_drag_threshold() {
        XCTAssertTrue(CallObserverHUD.isClick(down: .init(x: 10, y: 10), up: .init(x: 12, y: 11)))
        XCTAssertFalse(CallObserverHUD.isClick(down: .init(x: 10, y: 10), up: .init(x: 40, y: 10)))
    }

    func test_panel_terminal_and_live_states() {
        let panel = CallObserverPanelController()
        panel.showPanel(session: session())
        panel.setTerminal(message: "Звонок завершён")
        XCTAssertEqual(panel.testHook_stateBadgeText, "Звонок завершён")
        panel.setLive()
        XCTAssertNotEqual(panel.testHook_stateBadgeText, "Звонок завершён")
        panel.close()
    }

    func test_panel_renders_interrupted_prefix() {
        let panel = CallObserverPanelController()
        panel.showPanel(session: session())
        panel.updateTranscript([
            .init(kind: .agent(text: "Полный текст", textRu: nil, utteranceTs: "u1",
                               interrupted: true, spokenText: "Полн", spokenFraction: 0.3)),
        ])
        let rendered = panel.testHook_transcriptPlainText
        XCTAssertTrue(rendered.contains("Полн"))
        XCTAssertTrue(rendered.contains("прервано"), "показать, ЧТО собеседник реально услышал")
        panel.close()
    }

    func test_panel_no_runModal_source_contract() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        for file in ["CallObserverPanelController.swift", "CallObserverHUD.swift", "main+CallObserver.swift"] {
            let text = try String(contentsOf: root.appendingPathComponent(file), encoding: .utf8)
            XCTAssertFalse(text.contains("runModal"), "\(file): runModal запрещён (Sequoia AppHang)")
        }
    }

    func test_panel_header_layout_no_overlap() {
        let panel = CallObserverPanelController()
        let s = session("s1")
        panel.showPanel(session: s)
        
        // .titled + hidden visibility сохраняет крестик, но убирает нативный тайтл
        XCTAssertEqual(panel.window?.titleVisibility, .hidden)
        XCTAssertTrue(panel.window?.styleMask.contains(.titled) == true)
        
        let expectedPhone = s.phone
        XCTAssertTrue(panel.window?.title.contains(expectedPhone) == true)
        XCTAssertTrue(panel.testHook_inContentTitleLabel.stringValue.contains(expectedPhone))
        
        panel.setTerminal(message: "Звонок завершён")
        XCTAssertEqual(panel.testHook_stateBadgeText, "Звонок завершён")
        
        guard let contentView = panel.window?.contentView else {
            XCTFail("No content view")
            return
        }
        contentView.layoutSubtreeIfNeeded()
        
        let titleFrame = panel.testHook_inContentTitleLabel.convert(panel.testHook_inContentTitleLabel.bounds, to: nil)
        let badgeFrame = panel.testHook_stateBadgeBox.convert(panel.testHook_stateBadgeBox.bounds, to: nil)
        
        XCTAssertFalse(titleFrame.intersects(badgeFrame), "Title and badge should not intersect / overlap")
        panel.close()
    }
    
    func test_panel_header_layout_screening_no_overlap() {
        let panel = CallObserverPanelController()
        let s = session("s2", isScreening: true, forwardedFrom: "+16895551234")
        panel.showPanel(session: s)
        
        let titleString = panel.testHook_inContentTitleLabel.stringValue
        XCTAssertTrue(titleString.contains("Скрининг входящего"), "должна быть метка скрининга")
        XCTAssertTrue(titleString.contains(s.phone), "должен быть caller")
        XCTAssertTrue(titleString.contains(s.forwardedFrom), "должен быть DID")
        XCTAssertNotEqual(s.phone, s.forwardedFrom, "caller ≠ DID")
        
        panel.setTerminal(message: "Звонок завершён")
        
        guard let contentView = panel.window?.contentView else {
            XCTFail("No content view")
            return
        }
        contentView.layoutSubtreeIfNeeded()
        
        let titleFrame = panel.testHook_inContentTitleLabel.convert(panel.testHook_inContentTitleLabel.bounds, to: nil)
        let badgeFrame = panel.testHook_stateBadgeBox.convert(panel.testHook_stateBadgeBox.bounds, to: nil)
        
        XCTAssertFalse(titleFrame.intersects(badgeFrame), "Long screening title and badge should not intersect")
        panel.close()
    }
    
    func test_panel_outbound_no_screening_badge() {
        let panel = CallObserverPanelController()
        let s = session("s3", isScreening: false)
        panel.showPanel(session: s)
        
        let titleString = panel.testHook_inContentTitleLabel.stringValue
        XCTAssertFalse(titleString.contains("Скрининг"), "outbound без screening-метки")
        XCTAssertTrue(titleString.contains("Звонок агента"))
        
        panel.close()
    }
}
