/*
 PrivacyDashboardAuditWiringTests — source-контракты волны «вернуть Privacy Audit
 viewer + тумблер privacy mode» (2026-08-22).

 Контекст: Privacy Audit viewer никогда не был достижим из прода — его секция-хозяйка
 (Phase D.5 buildPrivacySection) с рождения не вставлялась в Settings-таб и была
 удалена волной dead-swift-methods-b. Вместе с ней из UI исчез и тумблер privacy mode
 (privacyModeButton остался сиротой: создан, синкается, но не в иерархии).

 Волна возвращает обе фичи в ЖИВУЮ секцию «Приватность и данные»
 (HistoryPanelController+PrivacyDashboard.swift) и вырезает из viewer'а кнопку
 «Очистить»: её IPC-метод clear_privacy_audit_log намеренно удалён из диспетча
 (W957 SECURITY — уничтожение compliance-трейла через неподписанный IPC запрещено).

 Паттерн — source-контракт (SparkleWiringSourceContractTests): класс бага
 «определён, но не вызван» ловится только чтением исходника, не unit-тестом.
*/

import XCTest
import Foundation

final class PrivacyDashboardAuditWiringTests: XCTestCase {

    // MARK: - W957: у viewer'а нет функции очистки audit-лога

    func test_viewer_has_no_clear_audit_log_function() throws {
        let src = try String(
            contentsOf: Self.sourceURL("PrivacyAuditViewerWindow.swift"), encoding: .utf8)
        // Матчим ВЫЗОВ (method: "..."), не голое имя: честный комментарий про W957
        // в шапке файла не должен ронять корректную реализацию (правило кодекса).
        XCTAssertFalse(
            src.contains("method: \"clear_privacy_audit_log\""),
            "W957 SECURITY: clear_privacy_audit_log удалён из IPC-диспетча — "
                + "viewer не должен его вызывать (кнопка гарантированно падала бы)"
        )
        XCTAssertFalse(
            src.contains("clearButton"),
            "Кнопка «Очистить» должна быть вырезана целиком, не только её IPC-вызов"
        )
    }

    // MARK: - Кнопка «Журнал аудита» подключена в живой секции

    func test_audit_viewer_launcher_wired_in_privacy_dashboard() throws {
        let src = try String(
            contentsOf: Self.sourceURL("HistoryPanelController+PrivacyDashboard.swift"),
            encoding: .utf8)
        XCTAssertTrue(
            src.contains("onShowPrivacyAuditLog"),
            "Секция «Приватность и данные» должна содержать кнопку «Журнал аудита»"
        )
        XCTAssertTrue(
            src.contains("PrivacyAuditViewerWindowController(ipcClient:"),
            "Launcher должен создавать PrivacyAuditViewerWindowController"
        )
        XCTAssertTrue(
            src.contains(".showAndLoad()"),
            "Launcher должен вызывать showAndLoad() — иначе viewer остаётся мёртвым"
        )
    }

    // MARK: - Тумблер privacy mode живёт в секции «Приватность и данные»

    func test_privacy_mode_toggle_wired_in_privacy_dashboard() throws {
        let src = try String(
            contentsOf: Self.sourceURL("HistoryPanelController+PrivacyDashboard.swift"),
            encoding: .utf8)
        XCTAssertTrue(
            src.contains("onPrivacyModeToggled"),
            "Секция должна содержать живой тумблер privacy mode (action)"
        )
        XCTAssertTrue(
            src.contains("applySettingsPatch([\"privacy_mode_enabled\""),
            "Тумблер обязан писать privacy_mode_enabled через applySettingsPatch"
        )
        XCTAssertTrue(
            src.contains("setPrivacyMode("),
            "Тумблер обязан синхронизировать индикатор меню-бара (setPrivacyMode)"
        )
    }

    // MARK: - Из fallback «нет данных» обязан быть выход

    /// Живой инцидент 2026-08-23: карточка ушла в ветку `guard let data else`
    /// и унесла с собой ВСЕ кнопки — «Обновить» больше не нарисована, повторный
    /// запрос из UI невозможен, состояние залипает до пересоздания панели
    /// (класс «sticky state without an exit»).
    func test_fallback_branch_keeps_retry_button() throws {
        let src = try String(
            contentsOf: Self.sourceURL("HistoryPanelController+PrivacyDashboard.swift"),
            encoding: .utf8)
        // Обе ветки fallback (Gemini + CD) обязаны добавлять строку кнопок
        // ДО раннего return, иначе «нет данных» становится терминальным.
        let fallbackChunks = src.components(separatedBy: "guard let data else {")
        XCTAssertEqual(
            fallbackChunks.count, 3,
            "Ожидались ровно две fallback-ветки (Gemini + Claude Design)"
        )
        for (idx, chunk) in fallbackChunks.dropFirst().enumerated() {
            guard let untilReturn = chunk.range(of: "return") else {
                XCTFail("fallback-ветка \(idx + 1): не найден return")
                continue
            }
            let body = chunk[chunk.startIndex..<untilReturn.lowerBound]
            XCTAssertTrue(
                body.contains("pdButtonRow()"),
                "fallback-ветка \(idx + 1) обязана оставлять кнопку «Обновить» — "
                    + "иначе из состояния «нет данных» нет выхода"
            )
        }
    }

    // MARK: - Сирота privacyModeButton удалён

    func test_orphan_privacyModeButton_removed() throws {
        for file in ["HistoryPanelController.swift", "HistoryPanelController+Settings.swift"] {
            let src = try String(contentsOf: Self.sourceURL(file), encoding: .utf8)
            XCTAssertFalse(
                src.contains("privacyModeButton"),
                "\(file): privacyModeButton — сирота Phase D.5 (создан, но не в UI); "
                    + "живой тумблер теперь в PrivacyDashboard"
            )
        }
    }

    /// Резолв файла исходников из тест-бандла (паттерн SFSymbolVerificationTests).
    private static func sourceURL(_ name: String) -> URL {
        let bundleURL = Bundle(for: PrivacyDashboardAuditWiringTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
