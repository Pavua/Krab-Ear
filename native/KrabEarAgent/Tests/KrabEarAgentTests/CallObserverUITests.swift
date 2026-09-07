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

    func test_hud_screening_caller_and_did_survive_elapsed_ticks() {
        let hud = CallObserverHUD()
        defer { hud.hideHUD() }

        let s = session(
            "screening-timer",
            isScreening: true,
            forwardedFrom: "+16895551234"
        )
        hud.showHUD(session: s)
        let title = hud.testHook_statusText
        XCTAssertTrue(title.contains(s.phone))
        XCTAssertTrue(title.contains(s.forwardedFrom))

        for tick in 1...2 {
            hud.testHook_fireElapsedTimer()
            let text = hud.testHook_statusText
            XCTAssertTrue(
                text.hasPrefix(title + " · "),
                "Полный заголовок должен сохраниться после тика \(tick)"
            )
            XCTAssertTrue(text.contains(s.phone))
            XCTAssertTrue(text.contains(s.forwardedFrom))
            let parts = text.components(separatedBy: " · ")
            XCTAssertEqual(parts.count, 3, "Один разделитель заголовка и один времени")
            XCTAssertNotNil(
                parts.last?.range(
                    of: "^[0-9]{2,}:[0-9]{2}$",
                    options: .regularExpression
                ),
                "Таймер должен действительно добавить время"
            )
        }
    }

    func test_hud_new_call_replaces_previous_screening_title() {
        let hud = CallObserverHUD()
        defer { hud.hideHUD() }
        let screening = session(
            "old-screening",
            isScreening: true,
            forwardedFrom: "+16895551234"
        )
        hud.showHUD(session: screening)
        hud.testHook_fireElapsedTimer()

        let outbound = session("new-outbound")
        hud.showHUD(session: outbound)
        let expectedTitle = "\(outbound.callDirection) \(outbound.phone)"
        XCTAssertEqual(hud.testHook_statusText, expectedTitle)
        hud.testHook_fireElapsedTimer()
        XCTAssertTrue(hud.testHook_statusText.hasPrefix(expectedTitle + " · "))
        XCTAssertFalse(hud.testHook_statusText.contains(screening.forwardedFrom))
        XCTAssertFalse(hud.testHook_statusText.contains("Скрининг"))
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
