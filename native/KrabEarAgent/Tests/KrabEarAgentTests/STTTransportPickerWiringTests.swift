import XCTest
@testable import KrabEarAgent

/// Source-контракт тесты для пикера транспорта GigaAM (2026-08-23).
///
/// Каждый тест проверяет ФАКТ ВЫЗОВА, а не факт существования кода — три
/// механизма (autoenablesItems, sync-хук, completion-проводка) в неправильной
/// реализации выглядят присутствующими и дают зелёные unit-тесты при мёртвом
/// UI. Паттерн: MainErrorsWiringTests / MainHealthMonitorWiringTests.
final class STTTransportPickerWiringTests: XCTestCase {

    /// Резолвит путь ОТ ЭТОГО тестового файла до корня репозитория. Тот же
    /// bundle-based паттерн, что MainErrorsWiringTests.mainSwiftURL — bundle
    /// на CI может лежать не там же, где исходники, поэтому обход вверх по
    /// файловой системе надёжнее жёсткого подсчёта deletingLastPathComponent.
    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: STTTransportPickerWiringTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent(relativePath)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        // Фолбэк (тот же паттерн, что MainErrorsWiringTests.mainSwiftURL):
        // от #file поднимаемся до repo root — 5 компонентов пути теста
        // (native/KrabEarAgent/Tests/KrabEarAgentTests/<файл>.swift).
        let fileURL = URL(fileURLWithPath: #file)
        let repoRoot = fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .deletingLastPathComponent()  // native
            .deletingLastPathComponent()  // repo root
        return try String(
            contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8
        )
    }

    /// H1: autoenablesItems=false обязан стоять рядом с item.isEnabled для
    /// пункта MLX — иначе NSMenu перезаписывает disabled-состояние перед
    /// показом (прецедент: main+CallObserver.swift:56, находка MED-2).
    func test_autoenablesItemsFalse_isSetNextToMlxItemDisable() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(
            source.contains("menu?.autoenablesItems = false"),
            "Пикер транспорта GigaAM обязан выставлять autoenablesItems = false "
            + "перед item(at:).isEnabled — иначе задизейбленный пункт MLX "
            + "останется кликабельным (см. main+CallObserver.swift:56)"
        )
    }

    /// C5a(а): syncGigaamTransportControls ОПРЕДЕЛЁН — но это не доказывает,
    /// что он вызывается. Следующий тест проверяет именно вызов.
    func test_syncGigaamTransportControls_isDefined() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(source.contains("func syncGigaamTransportControls"))
    }

    /// C5a(а), ГЛАВНЫЙ ГАРД: syncGigaamTransportControls обязан вызываться
    /// из syncSettingsControls — по образцу syncCloudRewriterControls
    /// (HistoryPanelController+Settings.swift). Без этого вызова пикер
    /// не получит начальное значение при открытии Settings и не ресинкнется
    /// после внешнего set_settings/apply_profile_preset.
    func test_syncGigaamTransportControls_isCalledFromSyncSettingsControls() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift"
        )
        XCTAssertTrue(
            source.contains("syncGigaamTransportControls("),
            "syncSettingsControls() обязан вызывать syncGigaamTransportControls "
            + "(по образцу syncCloudRewriterControls) — иначе пикер транспорта "
            + "GigaAM никогда не отразит реальное состояние настроек"
        )
    }

    /// C5a(б,в): видимость карточки и mlxAvailable обязаны пересчитываться
    /// в completion fetchAndRebuildSTTEnginesCard — тумблер GigaAM живёт в
    /// СОСЕДНЕЙ асинхронно перестраиваемой карточке.
    func test_gigaamTransportCard_visibilityWiredFromEnginesCompletion() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(
            source.contains("gigaamEnabled"),
            "Completion fetchAndRebuildSTTEnginesCard обязан пересчитывать "
            + "видимость карточки транспорта по актуальному состоянию тумблера "
            + "GigaAM, а не только при первом построении секции"
        )
        XCTAssertTrue(
            source.contains("mlx_available"),
            "Completion обязан извлекать mlx_available из сырого ответа "
            + "list_stt_engines и передавать в syncGigaamTransportControls"
        )
    }
}
