import XCTest
@testable import KrabEarAgent

/// Оба варианта дизайна обязаны давать доступ к одним и тем же настройкам.
///
/// Панель собирается в двух вариантах: «Gemini» (`settingsBar`) и «Claude
/// Design» (`settingsBarCD`), выбор — UserDefaults-ключ `KrabEar_UseClaudeDesign`.
/// Секции для CD писались отдельным набором `cdBuild…`, и одиннадцать секций
/// Gemini-варианта аналога не получили. У владельца включён CD — значит целые
/// куски настроек были ему недоступны физически, а не «спрятаны поглубже»:
/// выбор микрофона, режим буфера обмена, быстрые заметки, авто-перевод
/// выделения, вебхуки, планировщик записей и другие.
///
/// Обнаружено обходом панели 02.09.2026: пикер микрофона починили в проводке,
/// после чего он всё равно не находился на экране — секции, в которой он живёт,
/// в CD-варианте просто нет.
///
/// Секции строятся в `applyVisualTheme()` безусловно (обе ветки видят одни и те
/// же локальные переменные), поэтому недостающие достаточно добавить в
/// `settingsBarCD`: скрытый `settingsBar` их не удерживает.
final class DesignVariantSectionParityTests: XCTestCase {

    /// Секции, которых у CD-варианта нет своих; каждая обязана попасть в
    /// `settingsBarCD` из общей части сборки.
    private let geminiOnlySections = [
        "audioPipelineSection",
        "profAudioSection",
        "builtSystemSection",
        "clipSection",
        "quickCaptureSection",
        "quickPresetSection",
        "selTransSection",
        "vaSection",
        "schedulerSection",
        "webhookManagerSection",
        "callObserverSettingsSection",
    ]

    private func readPanelSource() throws -> String {
        let relativePath = "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift"
        let bundleURL = Bundle(for: DesignVariantSectionParityTests.self).bundleURL
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


    /// Тело ветки `if UserDefaults.standard.useClaudeDesignVariant { … }` —
    /// от открывающей скобки до парной ей, считая вложенность.
    private func claudeDesignBranchBody(of src: String) -> String? {
        guard let start = src.range(of: "if UserDefaults.standard.useClaudeDesignVariant {") else {
            return nil
        }
        var depth = 1
        var idx = start.upperBound
        while idx < src.endIndex {
            let ch = src[idx]
            if ch == "{" { depth += 1 }
            if ch == "}" {
                depth -= 1
                if depth == 0 { return String(src[start.upperBound..<idx]) }
            }
            idx = src.index(after: idx)
        }
        return nil
    }

    func test_claudeDesignVariant_receivesSectionsItHasNoOwnVersionOf() throws {
        let src = try readPanelSource()
        // Ищем внутри ТЕЛА CD-ветки, а не по всему файлу: имя секции встречается
        // и в общей части, где она строится. Форма добавления не важна — прямой
        // вызов или цикл, — важно, что секция попадает именно в CD-стек.
        guard let branch = claudeDesignBranchBody(of: src) else {
            XCTFail("не удалось выделить тело ветки Claude Design")
            return
        }
        let addsToCD = branch.contains("settingsBarCD.addArrangedSubview")
        XCTAssertTrue(addsToCD, "ветка Claude Design ничего не добавляет в свой стек")
        let missing = geminiOnlySections.filter { !branch.contains($0) }
        XCTAssertTrue(
            missing.isEmpty,
            """
            вариант Claude Design не получает \(missing.count) секц(ию/ий): \
            \(missing.joined(separator: ", ")). Своей версии у них нет, значит \
            эти настройки владельцу недоступны вовсе — включая выбор микрофона.
            """
        )
    }

    /// Скрытый `settingsBar` не должен удерживать те же view: одна NSView живёт
    /// ровно в одной иерархии, и добавление в CD-стек её оттуда переносит. Тест
    /// фиксирует, что общая часть по-прежнему строит секции ДО развилки — иначе
    /// переменных в CD-ветке просто не будет в области видимости.
    func test_sectionsAreBuiltBeforeVariantBranch() throws {
        let src = try readPanelSource()
        guard let branchRange = src.range(of: "if UserDefaults.standard.useClaudeDesignVariant {") else {
            XCTFail("не найдена развилка вариантов дизайна")
            return
        }
        let head = String(src[src.startIndex..<branchRange.lowerBound])
        for section in geminiOnlySections {
            XCTAssertTrue(
                head.contains("let \(section) ="),
                "\(section) обязана строиться до развилки, иначе CD-ветка её не увидит"
            )
        }
    }
}
