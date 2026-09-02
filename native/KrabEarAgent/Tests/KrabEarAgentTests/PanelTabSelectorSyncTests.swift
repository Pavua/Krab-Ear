import XCTest
@testable import KrabEarAgent

/// Подсветка вкладки обязана совпадать с показанным содержимым (02.09.2026).
///
/// `showPanel()` сначала выбирает «Историю» как безопасный fallback — этот вызов
/// идёт БЕЗ флага `isSyncingTabs`, поэтому делегат `tabView(_:didSelect:)`
/// срабатывает и переводит сегментный контрол на «Историю». Затем
/// `syncSettingsControls()` восстанавливает настоящую `ui_last_tab`, но уже ПОД
/// флагом — делегат молчит, и сегмент остаётся на «Истории», хотя содержимое
/// уехало на сохранённую вкладку.
///
/// Живое воспроизведение: `ui_last_tab = dictation` → панель открывается с
/// содержимым «Диктовки» и подсвеченной «Историей». Владелец видит одно, а
/// работает с другим; при этом ни один тест не краснел.
///
/// Поэтому инвариант структурный: синхронизация сегмента с `mainTabView` обязана
/// стоять ПОСЛЕ `syncSettingsControls()`. Раньше по тексту — бесполезна, её
/// затрёт восстановление; отсутствует — возвращается баг.
final class PanelTabSelectorSyncTests: XCTestCase {

    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: PanelTabSelectorSyncTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent(relativePath)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        let repoRoot = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8
        )
    }

    /// Тело `func showPanel()` построчно.
    private func showPanelBody() throws -> [String] {
        let src = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift"
        )
        let lines = src.components(separatedBy: .newlines)
        guard let start = lines.firstIndex(where: { $0.contains("func showPanel()") }) else {
            XCTFail("не найден func showPanel()")
            return []
        }
        let indent = lines[start].prefix(while: { $0 == " " }).count
        let close = String(repeating: " ", count: indent) + "}"
        guard let end = lines[(start + 1)...].firstIndex(where: { $0 == close }) else {
            XCTFail("не найдена закрывающая скобка showPanel()")
            return []
        }
        // Комментарии выбрасываем: в showPanel() их текст УПОМИНАЕТ
        // syncSettingsControls(), и поиск по подстроке иначе находит комментарий
        // вместо вызова — тест краснел бы не по той причине.
        return Array(lines[start...end]).filter {
            !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//")
        }
    }

    func test_showPanel_syncsSelectorAfterRestoringSavedTab() throws {
        let body = try showPanelBody()
        XCTAssertFalse(body.isEmpty)

        guard let restoreIdx = body.firstIndex(where: { $0.contains("syncSettingsControls()") }) else {
            XCTFail("showPanel() обязан восстанавливать настройки через syncSettingsControls()")
            return
        }
        let syncIdx = body.firstIndex { line in
            line.contains("tabSelector.selectedSegment")
                || line.contains("syncTabSelectorFromTabView")
        }
        guard let syncIdx else {
            XCTFail("""
                showPanel() не синхронизирует сегментный контрол с mainTabView. \
                Восстановление ui_last_tab идёт под isSyncingTabs, делегат молчит — \
                панель откроется с подсветкой одной вкладки и содержимым другой.
                """)
            return
        }
        XCTAssertGreaterThan(
            syncIdx, restoreIdx,
            "синхронизация сегмента обязана идти ПОСЛЕ syncSettingsControls(), иначе её затрёт восстановление"
        )
    }

    /// Fallback обязан идти ПОД `isSyncingTabs`.
    ///
    /// Делегат `tabView(_:didSelect:)` не только двигает сегмент — он ещё и
    /// СОХРАНЯЕТ `ui_last_tab`. Незаглушённый fallback поэтому затирал
    /// запомненную вкладку «Историей» при каждом открытии панели, и функция
    /// «помнить последнюю вкладку» не работала никогда. Замер 02.09.2026:
    /// `ui_last_tab` = dictation до открытия, history — сразу после.
    func test_fallbackSelection_isMutedSoItDoesNotOverwriteSavedTab() throws {
        let body = try showPanelBody()
        guard let fallbackIdx = body.firstIndex(where: {
            $0.contains("mainTabView.selectTabViewItem(at:")
        }) else {
            XCTFail("не найден fallback-выбор вкладки")
            return
        }
        let before = body[..<fallbackIdx].last { $0.contains("isSyncingTabs") }
        XCTAssertEqual(
            before?.trimmingCharacters(in: .whitespaces), "isSyncingTabs = true",
            """
            fallback обязан выполняться под isSyncingTabs = true: иначе делегат \
            сохранит ui_last_tab = history и запомненная вкладка будет потеряна
            """
        )
        let after = body[(fallbackIdx + 1)...].first { $0.contains("isSyncingTabs") }
        XCTAssertEqual(
            after?.trimmingCharacters(in: .whitespaces), "isSyncingTabs = false",
            "флаг обязан сниматься сразу после fallback, иначе он заглушит и остальные переключения"
        )
    }

    /// Fallback-выбор «Истории» сам по себе законен, но он и есть источник
    /// расхождения — тест фиксирует, что он остаётся ПЕРЕД восстановлением.
    func test_fallbackTabSelection_precedesRestore() throws {
        let body = try showPanelBody()
        guard
            let fallbackIdx = body.firstIndex(where: { $0.contains("mainTabView.selectTabViewItem(at:") }),
            let restoreIdx = body.firstIndex(where: { $0.contains("syncSettingsControls()") })
        else {
            XCTFail("ожидались и fallback-выбор вкладки, и восстановление настроек")
            return
        }
        XCTAssertLessThan(
            fallbackIdx, restoreIdx,
            "fallback обязан стоять до восстановления — иначе он затрёт сохранённую вкладку"
        )
    }
}
