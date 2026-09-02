import XCTest
@testable import KrabEarAgent

/// Source-контракт проводки переключателя «Оверлей за курсором» (2026-09-02).
///
/// Настройка `overlay_follow_cursor` приехала раньше своего контрола: оверлей
/// её читал, `AgentSettings.init(from:)` разбирал, а `toPayload()` не отправлял
/// — панель могла показать значение, но не сохранить, и владелец справедливо
/// спросил «где я могу это включить?». Один переключатель здесь обязан пройти
/// ЧЕТЫРЕ независимых участка, и провал любого делает его декоративным:
///
///   1. `toPayload()` — иначе сохранить нельзя в принципе;
///   2. `target/action` в ОБОИХ вариантах дизайна — вариант выбирается
///      UserDefaults-ключом, так что «работает у меня» ничего не доказывает;
///   3. добавление строки в карточку Claude Design — построенная, но не
///      добавленная строка невидима, а код выглядит присутствующим;
///   4. синхронизация состояния из настроек — иначе галочка врёт после
///      перезапуска панели.
///
/// Паттерн файла: STTTransportPickerWiringTests / MainErrorsWiringTests.
final class OverlayFollowCursorWiringTests: XCTestCase {

    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: OverlayFollowCursorWiringTests.self).bundleURL
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
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent
            .deletingLastPathComponent()  // native
            .deletingLastPathComponent()  // repo root
        return try String(
            contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8
        )
    }

    private let models = "native/KrabEarAgent/Sources/KrabEarAgent/Models.swift"
    private let panel = "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift"
    private let settings = "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift"
    private let claudeDesign =
        "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings+ClaudeDesign.swift"

    // MARK: 1. Ключ ездит в обе стороны

    func test_overlayFollowCursor_isBothParsedAndSent() throws {
        let src = try readSourceFile(models)
        XCTAssertTrue(
            src.contains("payload[\"overlay_follow_cursor\"]"),
            "AgentSettings.init(from:) обязан разбирать overlay_follow_cursor"
        )
        XCTAssertTrue(
            src.contains("\"overlay_follow_cursor\": overlayFollowCursor"),
            "toPayload() обязан отправлять overlay_follow_cursor — иначе панель не сохранит настройку"
        )
    }

    // MARK: 2. Контрол существует и синхронизируется

    func test_toggleExists_andSyncsFromSettings() throws {
        let panelSrc = try readSourceFile(panel)
        XCTAssertTrue(
            panelSrc.contains("let overlayFollowCursorButton"),
            "переключатель должен быть объявлен рядом с сиблингами настроек оверлея"
        )
        let settingsSrc = try readSourceFile(settings)
        XCTAssertTrue(
            settingsSrc.contains("overlayFollowCursorButton.state = settings.overlayFollowCursor"),
            "syncSettingsControls обязан отражать реальное значение — иначе галочка врёт после перезапуска"
        )
        XCTAssertTrue(
            settingsSrc.contains("func onOverlayFollowCursorChanged()"),
            "нужен @objc-хендлер, отправляющий патч настройки"
        )
        XCTAssertTrue(
            settingsSrc.contains("applySettingsPatch([\"overlay_follow_cursor\":"),
            "хендлер обязан сохранять значение через applySettingsPatch"
        )
    }

    // MARK: 3. Оба варианта дизайна подключают контрол к хендлеру

    func test_bothDesignVariants_wireTargetAndAction() throws {
        for path in [panel, claudeDesign] {
            let src = try readSourceFile(path)
            XCTAssertTrue(
                src.contains("overlayFollowCursorButton.target = self"),
                "\(path): контрол без target остаётся мёртвым"
            )
            XCTAssertTrue(
                src.contains("#selector(onOverlayFollowCursorChanged)"),
                "\(path): контрол без action остаётся мёртвым"
            )
        }
    }

    // MARK: 4. Строка реально попадает в видимую иерархию

    func test_claudeDesign_rowIsAddedToCard() throws {
        let src = try readSourceFile(claudeDesign)
        XCTAssertTrue(
            src.contains("let overlayFollowRow = cdMakeRow("),
            "строка Claude Design должна строиться"
        )
        XCTAssertTrue(
            src.contains("card.contentStackView.addArrangedSubview(overlayFollowRow)"),
            "построенная строка обязана быть добавлена в карточку — иначе переключателя не видно"
        )
    }

    func test_geminiVariant_rowIsAddedToRow3() throws {
        let src = try readSourceFile(panel)
        XCTAssertTrue(
            src.contains("settingsRow3.addArrangedSubview(overlayFollowCursorButton)"),
            "вариант Gemini обязан добавить контрол в видимый ряд настроек"
        )
    }
}
