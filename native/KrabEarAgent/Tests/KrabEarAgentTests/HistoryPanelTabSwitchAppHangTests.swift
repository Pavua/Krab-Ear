/*
 HistoryPanelTabSwitchAppHangTests.swift

 Регрессионные тесты для AGE-51 / KRAB-EAR-AGENT-P.
 Файл проверяет, что открытие панели Krab Ear больше не прогревает все вкладки
 последовательным `selectTabViewItem`: именно этот путь заводил AppKit в
 синхронную сборку тяжёлого UI на главном потоке при переключении вкладок.
*/

import Foundation
import XCTest

final class HistoryPanelTabSwitchAppHangTests: XCTestCase {

    private var controllerSourceURL: URL {
        URL(fileURLWithPath: #file)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController.swift")
    }

    func testShowPanelDoesNotCycleThroughAllTabs() throws {
        let source = try String(contentsOf: controllerSourceURL, encoding: .utf8)
        let showPanelBody = executableLines(
            in: try functionBody(named: "showPanel", in: source)
        )

        XCTAssertFalse(
            showPanelBody.contains("for i in 0..<self.mainTabView.numberOfTabViewItems"),
            "showPanel() не должен циклом выбирать все вкладки: это возвращает AGE-51 AppHang."
        )
        XCTAssertFalse(
            showPanelBody.contains("selectTabViewItem(at: i)"),
            "showPanel() не должен делать selectTabViewItem(at: i) для прогрева вкладок."
        )
        XCTAssertFalse(
            showPanelBody.contains("setFrame(frame, display: true)"),
            "Микро-ресайз окна ради раскладки снова принудительно коммитит AppKit на главном потоке."
        )
        XCTAssertTrue(
            showPanelBody.contains("layoutVisiblePanelTab()"),
            "showPanel() должен пересчитывать только видимую вкладку без смены selection."
        )
    }

    func testVisibleTabLayoutHelperDoesNotSwitchTabs() throws {
        let source = try String(contentsOf: controllerSourceURL, encoding: .utf8)
        let helperBody = executableLines(
            in: try functionBody(named: "layoutVisiblePanelTab", in: source)
        )

        XCTAssertFalse(
            helperBody.contains("selectTabViewItem"),
            "layoutVisiblePanelTab() обязан быть layout-only helper без переключения вкладок."
        )
        XCTAssertTrue(
            helperBody.contains("selectedTabViewItem?.view?.needsLayout"),
            "Helper должен работать с выбранной вкладкой, а не со всеми табами."
        )
    }

    private func functionBody(named name: String, in source: String) throws -> String {
        guard let signatureRange = source.range(of: "func \(name)") else {
            XCTFail("Функция \(name) не найдена в HistoryPanelController.swift")
            return ""
        }
        guard let openingBrace = source[signatureRange.lowerBound...].firstIndex(of: "{") else {
            XCTFail("У функции \(name) не найдена открывающая скобка")
            return ""
        }

        var depth = 0
        var index = openingBrace
        while index < source.endIndex {
            let char = source[index]
            if char == "{" {
                depth += 1
            } else if char == "}" {
                depth -= 1
                if depth == 0 {
                    return String(source[openingBrace...index])
                }
            }
            index = source.index(after: index)
        }

        XCTFail("У функции \(name) не найдена закрывающая скобка")
        return ""
    }

    private func executableLines(in body: String) -> String {
        body
            .components(separatedBy: .newlines)
            .filter { line in
                let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                return !trimmed.hasPrefix("//")
                    && !trimmed.hasPrefix("/*")
                    && !trimmed.hasPrefix("*")
            }
            .joined(separator: "\n")
    }
}
