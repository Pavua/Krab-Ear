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
        // 03.09.2026: в Claude Design секция идёт своей CD-версией (cdBuildAllSettingsSection),
        // не переносом Gemini-view; годится любой из двух путей.
        XCTAssertTrue(
            src.contains("allSettingsSection,") || src.contains("cdBuildAllSettingsSection(),"),
            "секция должна попасть и в список Claude Design — переносом или своей CD-версией"
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

    // MARK: - Тесты русского глоссария настроек

    func test_settingsGlossary_knownKeys_haveRussianTitleAndGroup() {
        let sampleKeys = [
            "overlay_follow_cursor",
            "privacy_mode_enabled",
            "auto_paste",
            "quality_profile",
            "stt_gigaam_enabled",
            "llm_rewrite_enabled",
            "voice_gateway_url",
            "audio_ducking_enabled",
            "silence_guard_enabled",
        ]
        for key in sampleKeys {
            let desc = SettingsGlossary.lookup(key)
            XCTAssertNotEqual(desc.titleRU, key, "Ключ \(key) обязан иметь человекочитаемый русский заголовок")
            XCTAssertFalse(desc.groupRU.isEmpty, "Ключ \(key) обязан иметь категорию")
            XCTAssertNotEqual(desc.groupRU, "Разное", "Известный ключ \(key) не должен попадать в категорию Разное")
        }
    }

    func test_settingsGlossary_unknownKey_gracefulFallback() {
        let unknownKey = "future_unreleased_experiment_key_2027"
        let desc = SettingsGlossary.lookup(unknownKey)
        XCTAssertEqual(desc.titleRU, unknownKey, "Неизвестный ключ должен возвращать сам ключ как fallback")
        XCTAssertEqual(desc.groupRU, "Разное", "Неизвестный ключ должен относиться к группе Разное")
        XCTAssertNil(desc.descriptionRU)
    }

    func test_settingsGlossary_itemsCount_coversSettings() {
        XCTAssertGreaterThanOrEqual(
            SettingsGlossary.items.count, 245,
            "В словаре глоссария должно быть не менее 245 параметров конфигурации"
        )
    }

    func test_allSettings_searchHaystackAndGlossaryWiring() throws {
        let src = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+AllSettings.swift"
        )
        XCTAssertTrue(
            src.contains("SettingsGlossary.lookup(key)"),
            "makeAllSettingsRow обязан резолвить русский дескриптор через SettingsGlossary"
        )
        XCTAssertTrue(
            src.contains("desc.titleRU"),
            "makeAllSettingsRow обязан использовать русское название"
        )
        XCTAssertTrue(
            src.contains("desc.groupRU"),
            "makeAllSettingsRow обязан использовать категорию"
        )
        XCTAssertTrue(
            src.contains("desc.descriptionRU"),
            "search haystack обязан включать описание настройки"
        )
    }
}
