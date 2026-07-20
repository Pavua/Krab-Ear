/*
 PasteServiceClipboardSafetyTests — проверяет защиту скрытого содержимого буфера.

 Почему отдельный именованный NSPasteboard:
 тесты не должны читать или менять буфер обмена пользователя. PasteService получает
 этот приватный экземпляр через DI, поэтому проверки безопасны для локального запуска и CI.
*/

import XCTest
@testable import KrabEarAgent

final class PasteServiceClipboardSafetyTests: XCTestCase {

    private var service: PasteService!
    private var pasteboard: NSPasteboard!
    private let concealedType = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")

    override func setUp() {
        super.setUp()
        pasteboard = NSPasteboard(name: .init("KrabEarClipboardSafetyTests.\(UUID().uuidString)"))
        service = PasteService(pasteboard: pasteboard)
    }

    override func tearDown() {
        pasteboard.clearContents()
        service = nil
        pasteboard = nil
        super.tearDown()
    }

    // MARK: - 1. Обычная запись не меняется

    func test_putToClipboard_writes_normally_when_no_concealed_content() {
        pasteboard.clearContents()
        pasteboard.setString("previous text", forType: .string)

        service.putToClipboard("new dictated text")

        XCTAssertEqual(pasteboard.string(forType: .string), "new dictated text")
    }

    // MARK: - 2. Concealed-контент не затирается

    func test_putToClipboard_skips_write_when_concealed_type_present() {
        pasteboard.clearContents()
        pasteboard.declareTypes([.string, concealedType], owner: nil)
        pasteboard.setString("super-secret-password", forType: .string)
        pasteboard.setData(Data(), forType: concealedType)

        service.putToClipboard("new dictated text")

        XCTAssertEqual(
            pasteboard.string(forType: .string), "super-secret-password",
            "защищённый буфер не должен быть затёрт диктовкой")
    }

    // MARK: - 3. Callback вызывается ровно при пропуске

    func test_putToClipboard_invokes_callback_only_on_skip() {
        var callbackCount = 0
        service.onConcealedClipboardSkipped = { callbackCount += 1 }

        pasteboard.clearContents()
        pasteboard.setString("plain text", forType: .string)
        service.putToClipboard("dictated 1")
        XCTAssertEqual(callbackCount, 0, "обычная запись не должна триггерить callback")

        pasteboard.clearContents()
        pasteboard.declareTypes([.string, concealedType], owner: nil)
        pasteboard.setString("secret", forType: .string)
        pasteboard.setData(Data(), forType: concealedType)
        service.putToClipboard("dictated 2")
        XCTAssertEqual(callbackCount, 1, "пропуск concealed-буфера должен триггерить callback ровно 1 раз")
    }

    // MARK: - 4. Пустой буфер не крешит guard

    func test_putToClipboard_empty_pasteboard_writes_normally() {
        pasteboard.clearContents()
        XCTAssertFalse(
            pasteboard.types?.contains(concealedType) ?? false,
            "предусловие: буфер не содержит concealedType")

        service.putToClipboard("first write")

        XCTAssertEqual(pasteboard.string(forType: .string), "first write")
    }

    // MARK: - 4b. При concealed-буфере вставка завершается до синтетического Cmd+V

    func test_pasteToFrontmostApp_aborts_before_key_events_when_concealed() {
        pasteboard.clearContents()
        pasteboard.declareTypes([.string, concealedType], owner: nil)
        pasteboard.setString("super-secret-password", forType: .string)
        pasteboard.setData(Data(), forType: concealedType)

        let result = service.pasteToFrontmostApp("dictated text")

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "concealed_clipboard_skipped")
        XCTAssertEqual(
            pasteboard.string(forType: .string), "super-secret-password",
            "буфер не должен быть тронут до обработки key events")
    }

    // MARK: - 4c. Явное пользовательское копирование обходит guard

    func test_putToClipboardUserInitiated_bypasses_concealed_guard() {
        pasteboard.clearContents()
        pasteboard.declareTypes([.string, concealedType], owner: nil)
        pasteboard.setString("secret", forType: .string)
        pasteboard.setData(Data(), forType: concealedType)

        service.putToClipboardUserInitiated("explicit copy")

        XCTAssertEqual(
            pasteboard.string(forType: .string), "explicit copy",
            "явное действие «Копировать» обязано писать безусловно")
    }

    // MARK: - 5. Source-contract: проводка в main.swift + защита от реентерабельности

    func test_onConcealedClipboardSkipped_is_wired_in_main() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/main.swift")
        let src = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(src.contains("onConcealedClipboardSkipped ="),
                      "closure обязана быть подключена в main.swift")

        guard let range = src.range(of: "onConcealedClipboardSkipped = ") else {
            return XCTFail("wiring not found")
        }
        let tail = src[range.upperBound...]
        guard let closingBrace = tail.firstIndex(of: "}") else {
            return XCTFail("closure body not found")
        }
        let closureBody = tail[..<closingBrace]
        XCTAssertFalse(closureBody.contains("handlePasteFailure"),
                       "closure не должна вести обратно в handlePasteFailure")
        XCTAssertFalse(closureBody.contains("notify("),
                       "closure не должна дублировать пользовательское уведомление")
    }

    func test_explicit_copy_sites_use_user_initiated_bypass() throws {
        let sites: [(file: String, marker: String)] = [
            ("main.swift", "func onCopyLastResult"),
            ("main+QuickCapture.swift", "func onQuickNoteItemClicked(_ sender: NSMenuItem)"),
            ("main+QuickReplace.swift", "newText"),
        ]
        for site in sites {
            let url = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("Sources/KrabEarAgent/\(site.file)")
            let src = try String(contentsOf: url, encoding: .utf8)
            guard let markerRange = src.range(of: site.marker) else {
                return XCTFail("marker \(site.marker) not found in \(site.file)")
            }
            let window = src[markerRange.upperBound...].prefix(500)
            XCTAssertTrue(window.contains("putToClipboardUserInitiated"),
                          "\(site.file): explicit-copy обязан идти через putToClipboardUserInitiated")
        }
    }

    // MARK: - 6. Risk-warning tooltip-тексты присутствуют

    func test_clipboard_mode_tooltips_mention_concealed_protection() throws {
        for name in ["HistoryPanelController.swift", "main+StatusMenu.swift"] {
            let url = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("Sources/KrabEarAgent/\(name)")
            let src = try String(contentsOf: url, encoding: .utf8)
            XCTAssertTrue(src.contains("не затираются"),
                          "\(name) обязан содержать risk-warning про защищённый контент")
        }
    }
}
