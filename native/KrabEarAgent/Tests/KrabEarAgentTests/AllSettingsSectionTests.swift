import XCTest
@testable import KrabEarAgent

/// Секция «Все настройки»: покрытие и защита секретов (02.09.2026).
///
/// Замер показал, что из 258 живых настроек панель редактировала 86. Строить
/// 162 контрола руками бессмысленно — настройки прибавляются быстрее, чем
/// секции. Таблица строится из ответа `get_settings`, поэтому новая настройка
/// бэкенда появляется в панели сама.
///
/// Главный риск такой таблицы — секреты: `get_settings` отдаёт их значением
/// `REDACTED`, и запись этой строки обратно затёрла бы живой ключ. Поэтому
/// классификатор секретных ключей проверяется как обычная логика, а не
/// «на глаз».
final class AllSettingsSectionTests: XCTestCase {

    func test_secretKeys_areRecognised() {
        for key in [
            "openai_api_key", "hf_token", "smtp_password", "ipc_signing_secret",
            "sentry_dsn", "sentry_dsn_agent", "rest_api_auth_token",
        ] {
            XCTAssertTrue(
                HistoryPanelController.isSecretSettingKey(key),
                "\(key) обязан считаться секретом — иначе его значение попадёт на экран"
            )
        }
    }

    func test_ordinaryKeys_areNotTreatedAsSecrets() {
        for key in [
            "auto_paste", "quality_profile", "overlay_opacity_percent",
            "stt_gigaam_device", "gigaam_idle_unload_sec", "selected_input_device",
        ] {
            XCTAssertFalse(
                HistoryPanelController.isSecretSettingKey(key),
                "\(key) не секрет — маскировать его значит спрятать обычную настройку"
            )
        }
    }

    /// `_keyboard`/`_tokenizer`-подобные имена не должны ловиться суффиксом:
    /// классификатор смотрит на КОНЕЦ ключа, а не на вхождение подстроки.
    func test_substringLookalikes_areNotSecrets() {
        XCTAssertFalse(HistoryPanelController.isSecretSettingKey("stt_hotkey_profile"))
        XCTAssertFalse(HistoryPanelController.isSecretSettingKey("token_budget_sec"))
    }

    // MARK: - Source-контракт проводки

    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: AllSettingsSectionTests.self).bundleURL
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
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }

    func test_sectionIsAddedToBothDesignVariants() throws {
        let src = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift"
        )
        XCTAssertTrue(
            src.contains("settingsBar.addArrangedSubview(allSettingsSection)"),
            "секция должна попасть в вариант Gemini"
        )
        XCTAssertTrue(
            src.contains("allSettingsSection,"),
            "секция должна попасть и в список, переносимый в Claude Design"
        )
    }

    func test_ipcCallsAreOffMainThread() throws {
        let src = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+AllSettings.swift"
        )
        guard let range = src.range(of: "ipc.call(method: \"get_settings\"") else {
            XCTFail("секция обязана читать настройки через get_settings")
            return
        }
        let head = String(src[src.startIndex..<range.lowerBound])
        XCTAssertTrue(
            head.contains("DispatchQueue.global"),
            "синхронный IPC на главном потоке даёт AppHang (AGENT-3) — вызов обязан быть off-main"
        )
    }
}
