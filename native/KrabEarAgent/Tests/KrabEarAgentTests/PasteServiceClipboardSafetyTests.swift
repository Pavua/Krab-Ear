/*
 PasteServiceClipboardSafetyTests — S34 clipboard safety (спека
 2026-07-19-s34-clipboard-safety-design.md).

 Тесты трогают РЕАЛЬНЫЙ NSPasteboard.general (прецедент PasteServiceRepastTests —
 прямая работа с system pasteboard/UserDefaults в тестах — норма проекта).
 setUp/tearDown сохраняют и восстанавливают исходное содержимое буфера, чтобы не
 оставлять мусор в CI-раннере после теста.
*/

import XCTest
@testable import KrabEarAgent

final class PasteServiceClipboardSafetyTests: XCTestCase {

    var service: PasteService!
    private let concealedType = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
    private var savedPasteboardString: String?

    override func setUp() {
        super.setUp()
        service = PasteService()
        savedPasteboardString = NSPasteboard.general.string(forType: .string)
    }

    override func tearDown() {
        NSPasteboard.general.clearContents()
        if let saved = savedPasteboardString {
            NSPasteboard.general.setString(saved, forType: .string)
        }
        service = nil
        super.tearDown()
    }

    // MARK: - 1. Обычная запись не меняется

    func test_putToClipboard_writes_normally_when_no_concealed_content() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("previous text", forType: .string)

        service.putToClipboard("new dictated text")

        XCTAssertEqual(NSPasteboard.general.string(forType: .string), "new dictated text")
    }

    // MARK: - 2. Concealed-контент не затирается

    func test_putToClipboard_skips_write_when_concealed_type_present() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.declareTypes([.string, concealedType], owner: nil)
        NSPasteboard.general.setString("super-secret-password", forType: .string)
        NSPasteboard.general.setData(Data(), forType: concealedType)

        service.putToClipboard("new dictated text")

        XCTAssertEqual(
            NSPasteboard.general.string(forType: .string), "super-secret-password",
            "защищённый буфер не должен быть затёрт диктовкой")
    }

    // MARK: - 3. Callback вызывается ровно при пропуске

    func test_putToClipboard_invokes_callback_only_on_skip() {
        var callbackCount = 0
        service.onConcealedClipboardSkipped = { callbackCount += 1 }

        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("plain text", forType: .string)
        service.putToClipboard("dictated 1")
        XCTAssertEqual(callbackCount, 0, "обычная запись не должна триггерить callback")

        NSPasteboard.general.clearContents()
        NSPasteboard.general.declareTypes([.string, concealedType], owner: nil)
        NSPasteboard.general.setString("secret", forType: .string)
        NSPasteboard.general.setData(Data(), forType: concealedType)
        service.putToClipboard("dictated 2")
        XCTAssertEqual(callbackCount, 1, "пропуск concealed-буфера должен триггерить callback ровно 1 раз")
    }

    // MARK: - 4. Пустой буфер (types == nil) не крешит guard

    func test_putToClipboard_empty_pasteboard_writes_normally() {
        NSPasteboard.general.clearContents()
        // На реальном macOS-буфере clearContents() даёт types == [] (пустой массив),
        // не nil — но guard использует `?? false`, поэтому оба случая безопасны;
        // проверяем именно отсутствие concealedType, а не точное значение types.
        XCTAssertFalse(
            NSPasteboard.general.types?.contains(concealedType) ?? false,
            "предусловие: буфер не содержит concealedType")

        service.putToClipboard("first write")

        XCTAssertEqual(NSPasteboard.general.string(forType: .string), "first write")
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

        // Реентерабельность (спека §2.2): closure НЕ должна звать handlePasteFailure,
        // иначе повторный putToClipboard внутри неё снова упрётся в guard -> цикл.
        guard let range = src.range(of: "onConcealedClipboardSkipped = ") else {
            return XCTFail("wiring not found")
        }
        let tail = src[range.upperBound...]
        guard let closingBrace = tail.firstIndex(of: "}") else {
            return XCTFail("closure body not found")
        }
        let closureBody = tail[..<closingBrace]
        XCTAssertFalse(closureBody.contains("handlePasteFailure"),
                        "closure не должна вести обратно в handlePasteFailure (реентерабельность)")
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
