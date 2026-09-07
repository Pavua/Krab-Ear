import AppKit
import XCTest
@testable import KrabEarAgent

/// Секция «Автозвонки» обязана ПОКАЗЫВАТЬ сохранённые значения, а не дефолты.
///
/// Проверяет три оставшихся контрола: длительность, стоимость и завершение
/// при тишине. Устаревшие Telnyx-поля удалены; legacy-схема сохраняется отдельно.
///
/// 🔴 Опаснее, чем «не видно значения»: слайдер, показывающий 30 при
/// сохранённых 15, при первом же касании запишет ~30 — открытие панели и
/// случайное движение мыши молча меняют настройку владельца. Это зеркало бага
/// пикера микрофона (тот был проведён, но ничего не писал; эти пишут, но ничего
/// не читают) — половины одного контракта «контрол отражает состояние».
///
/// Секция живёт только в варианте Claude Design, поэтому Gemini-версии здесь нет.
final class CallAutomationSyncTests: XCTestCase {

    private func source(_ relative: String) throws -> String {
        var url = Bundle(for: CallAutomationSyncTests.self).bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent(relative)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        var root = URL(fileURLWithPath: #file)
        for _ in 0..<5 { root = root.deletingLastPathComponent() }
        return try String(contentsOf: root.appendingPathComponent(relative), encoding: .utf8)
    }

    private func claudeDesignSource() throws -> String {
        try source("native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings+ClaudeDesign.swift")
    }

    func test_callAutomationSection_hasNoRemovedProviderControls() throws {
        let src = try claudeDesignSource()
        for token in [
            "#selector(onTelnyxAPIKeyChanged)",
            "#selector(onTelnyxFromNumberChanged)",
            "func onTelnyxAPIKeyChanged(",
            "func onTelnyxFromNumberChanged(",
            "CallAutomationAssocKeys.apiKeyField",
            "CallAutomationAssocKeys.fromField",
            "settings.telnyxAPIKey",
            "settings.telnyxFromNumber",
        ] {
            XCTAssertFalse(src.contains(token), "Мёртвый Telnyx-контрол остался: \(token)")
        }
    }

    func test_sharedCallPanel_doesNotRecommendRemovedProvider() throws {
        let src = try source("native/KrabEarAgent/Sources/KrabEarAgent/CallAutomationController.swift")
        for copy in [
            "Настройте Telnyx API key",
            "Проверьте настройки Telnyx.",
        ] {
            XCTAssertFalse(src.contains(copy), "Панель направляет в удалённый провайдер: \(copy)")
        }
    }

    @MainActor
    func test_configBanner_keeps_gateway_guidance_when_shown() throws {
        // Не загружаем view: init создаёт только контроллер и не вызывает IPC.
        let controller = CallAutomationController(
            ipcClient: IPCClient(socketPath: "/tmp/krabear-unused-\(UUID().uuidString).sock")
        )
        let label = try XCTUnwrap(
            Mirror(reflecting: controller).children
                .first(where: { $0.label == "configBannerLabel" })?.value as? NSTextField
        )
        let guidance = label.stringValue
        XCTAssertTrue(guidance.contains("Voice Gateway"))
        controller.showConfigBanner(true)
        XCTAssertEqual(label.stringValue, guidance, "Показ не должен перезаписывать актуальную подсказку")
        controller.showConfigBanner(false)
        controller.showConfigBanner(true)
        XCTAssertEqual(label.stringValue, guidance)
    }

    /// Синхронизация обязана существовать отдельной функцией — её зовут из двух
    /// мест (построение секции и `syncSettingsControls`).
    func test_syncFunctionExists() throws {
        let src = try claudeDesignSource()
        XCTAssertTrue(
            src.contains("func syncCallAutomationControls("),
            "нет функции синхронизации секции «Автозвонки» — контролы показывают литералы"
        )
    }

    /// Каждое из трёх полей настроек должно применяться к своему контролу.
    func test_everySettingIsApplied() throws {
        let src = try claudeDesignSource()
        for field in ["callMaxDurationMin", "callCostWarnUSD", "callAutoEndOnSilence"] {
            XCTAssertTrue(
                src.contains("settings.\(field)"),
                "\(field) не применяется к контролу — значение владельца не видно в панели"
            )
        }
    }

    /// Без вызова из `syncSettingsControls` синхронизация мертва: секция
    /// строится один раз в `applyVisualTheme`, а панель открывается многократно.
    func test_syncIsCalledFromSettingsSync() throws {
        let settings = try source("native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift")
        guard let range = settings.range(of: "func syncSettingsControls(") else {
            return XCTFail("не найдена syncSettingsControls")
        }
        var depth = 0
        var started = false
        var body = ""
        for ch in settings[range.lowerBound...] {
            if ch == "{" { depth += 1; started = true }
            if started { body.append(ch) }
            if ch == "}" { depth -= 1; if started && depth == 0 { break } }
        }
        XCTAssertTrue(
            body.contains("syncCallAutomationControls("),
            "syncSettingsControls не обновляет «Автозвонки» — при повторном открытии панели там снова дефолты"
        )
    }

    /// Запись под синхронизацией запрещена: обработчики гейтятся
    /// `isSyncingSettings`, и без флага sync сам бы записал настройки обратно.
    func test_syncGuardsAgainstEchoWrite() throws {
        let src = try claudeDesignSource()
        guard let idx = src.range(of: "func syncCallAutomationControls(") else { return }
        let window = String(src[idx.lowerBound...].prefix(1200))
        XCTAssertTrue(
            window.contains("isSyncingSettings"),
            "синхронизация обязана поднимать isSyncingSettings — иначе присваивание значений вызовет обработчики и перезапишет настройки"
        )
    }
}
