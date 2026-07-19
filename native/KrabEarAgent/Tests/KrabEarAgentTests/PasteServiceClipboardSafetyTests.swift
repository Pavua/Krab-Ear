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

    // MARK: - 4b. Fable-ревью F1: pasteToFrontmostApp НЕ синтезирует Cmd+V, если
    // запись в буфер пропущена — иначе в frontmost app вставится СТАРОЕ (возможно
    // защищённое) содержимое буфера вместо тихого отказа.

    func test_pasteToFrontmostApp_aborts_before_key_events_when_concealed() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.declareTypes([.string, concealedType], owner: nil)
        NSPasteboard.general.setString("super-secret-password", forType: .string)
        NSPasteboard.general.setData(Data(), forType: concealedType)

        // Гард срабатывает ДО resolvePreferredPasteTargetApp/waitForModifierRelease —
        // детерминированно и без зависимости от реального frontmost app в CI.
        let result = service.pasteToFrontmostApp("dictated text")

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "concealed_clipboard_skipped")
        XCTAssertEqual(
            NSPasteboard.general.string(forType: .string), "super-secret-password",
            "буфер не должен быть тронут — Cmd+V не должен был синтезироваться")
    }

    // MARK: - 4c. Fable-ревью F3: explicit user-initiated copy обходит guard

    func test_putToClipboardUserInitiated_bypasses_concealed_guard() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.declareTypes([.string, concealedType], owner: nil)
        NSPasteboard.general.setString("secret", forType: .string)
        NSPasteboard.general.setData(Data(), forType: concealedType)

        service.putToClipboardUserInitiated("explicit copy")

        XCTAssertEqual(
            NSPasteboard.general.string(forType: .string), "explicit copy",
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
        // Fable-ревью (double-notify, после F1-фикса): closure НЕ должна сама звать
        // notify() — единственный источник пользовательского уведомления теперь
        // явная Bool-проверка на каждом call site + handlePasteFailure. Второй notify
        // отсюда дублировал бы то же событие.
        XCTAssertFalse(closureBody.contains("notify("),
                        "closure не должна сама уведомлять — иначе дублирует handlePasteFailure/call-site notify")
    }

    func test_explicit_copy_sites_use_user_initiated_bypass() throws {
        // Fable-ревью F3: три explicit-copy call site'а должны звать
        // putToClipboardUserInitiated, НЕ putToClipboard (который теперь блокирует
        // запись при защищённом буфере) — иначе «Копировать последний»/«Копировать
        // заметку»/QuickReplace тихо перестают работать при заблокированном буфере.
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
            // Ищем ближайший putToClipboard*-вызов после маркера (в пределах ~500 символов).
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
