/*
 RedesignTokensTests — токены редизайна 2026-11 (PR 0) и гейт размера шрифта.

 Покрытие:
   - Контраст: текст на фоне ≥ порогов WCAG в светлой и тёмной темах;
   - «Увеличить контраст» делает границу плотной, «Уменьшить прозрачность» — фон сплошным
     (флаги подменяются через KrabEarAccessibility, реальные настройки Mac не влияют);
   - новые токены (pageTitle, scale, windowMinWidth, relaxed/roomy) на сетке 4 pt;
   - гейт: число литералов `ofSize: N` (N < 12) вне KrabEarTheme не растёт (храповик).
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class RedesignTokensTests: XCTestCase {

    private var savedContrast: (() -> Bool)!
    private var savedTransparency: (() -> Bool)!

    override func setUp() {
        super.setUp()
        savedContrast = KrabEarAccessibility.increaseContrast
        savedTransparency = KrabEarAccessibility.reduceTransparency
        KrabEarAccessibility.increaseContrast = { false }
        KrabEarAccessibility.reduceTransparency = { false }
    }

    override func tearDown() {
        KrabEarAccessibility.increaseContrast = savedContrast
        KrabEarAccessibility.reduceTransparency = savedTransparency
        KrabEarTheme.Typography.scale = 1.0
        super.tearDown()
    }

    // MARK: - Помощники WCAG

    private func rgb(_ color: NSColor, in appearance: NSAppearance) -> (r: CGFloat, g: CGFloat, b: CGFloat, a: CGFloat) {
        var out: (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 1)
        appearance.performAsCurrentDrawingAppearance {
            if let c = color.usingColorSpace(.sRGB) {
                out = (c.redComponent, c.greenComponent, c.blueComponent, c.alphaComponent)
            }
        }
        return out
    }

    private func luminance(_ c: (r: CGFloat, g: CGFloat, b: CGFloat, a: CGFloat)) -> CGFloat {
        func lin(_ v: CGFloat) -> CGFloat { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)
    }

    /// Контраст переднего цвета (с учётом его alpha) к сплошному фону.
    private func contrast(_ fg: NSColor, over bg: NSColor, in appearance: NSAppearance) -> CGFloat {
        let b = rgb(bg, in: appearance)
        let f = rgb(fg, in: appearance)
        let blended = (r: f.r * f.a + b.r * (1 - f.a),
                       g: f.g * f.a + b.g * (1 - f.a),
                       b: f.b * f.a + b.b * (1 - f.a), a: CGFloat(1))
        let l1 = luminance(blended), l2 = luminance((b.r, b.g, b.b, 1))
        return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
    }

    private let appearances: [(String, NSAppearance)] = [
        ("светлая", NSAppearance(named: .aqua)!),
        ("тёмная", NSAppearance(named: .darkAqua)!),
    ]

    // MARK: - Контраст

    func test_textPrimary_contrastOverWindowBackground_atLeast7() {
        for (name, ap) in appearances {
            let r = contrast(KrabEarTheme.Colors.textPrimary, over: .windowBackgroundColor, in: ap)
            XCTAssertGreaterThanOrEqual(r, 7.0, "основной текст, \(name) тема: \(r)")
        }
    }

    func test_textSecondary_contrastOverWindowBackground_atLeast4_5() {
        for (name, ap) in appearances {
            let r = contrast(KrabEarTheme.Colors.textSecondary, over: .windowBackgroundColor, in: ap)
            XCTAssertGreaterThanOrEqual(r, 4.5, "вторичный текст, \(name) тема: \(r)")
        }
    }

    // MARK: - Флаги доступности

    func test_border_increaseContrast_makesBorderDense() {
        for (name, ap) in appearances {
            KrabEarAccessibility.increaseContrast = { false }
            let normal = rgb(KrabEarTheme.Colors.border, in: ap).a
            KrabEarAccessibility.increaseContrast = { true }
            let strong = rgb(KrabEarTheme.Colors.border, in: ap).a
            XCTAssertGreaterThanOrEqual(strong, 0.55, "граница при Increase Contrast, \(name)")
            XCTAssertGreaterThan(strong, normal * 3, "граница должна заметно плотнеть, \(name)")
        }
    }

    func test_border_increaseContrast_borderContrastAtLeast3() {
        KrabEarAccessibility.increaseContrast = { true }
        for (name, ap) in appearances {
            let r = contrast(KrabEarTheme.Colors.border, over: .windowBackgroundColor, in: ap)
            XCTAssertGreaterThanOrEqual(r, 3.0, "контраст границы к фону, \(name): \(r)")
        }
    }

    func test_cardBackground_reduceTransparency_isOpaque() {
        for (name, ap) in appearances {
            KrabEarAccessibility.reduceTransparency = { false }
            XCTAssertEqual(rgb(KrabEarTheme.Colors.cardBackground, in: ap).a, 0.5, accuracy: 0.01, "обычный режим, \(name)")
            KrabEarAccessibility.reduceTransparency = { true }
            XCTAssertEqual(rgb(KrabEarTheme.Colors.cardBackground, in: ap).a, 1.0, accuracy: 0.01, "без прозрачности, \(name)")
        }
    }

    // MARK: - Новые токены

    func test_pageTitle_is24Bold_andFollowsScale() {
        XCTAssertEqual(KrabEarTheme.Typography.pageTitle.pointSize, 24, accuracy: 0.1)
        KrabEarTheme.Typography.scale = 1.15
        XCTAssertEqual(KrabEarTheme.Typography.pageTitle.pointSize, 28, accuracy: 0.6)
    }

    func test_largeTextTokens_are15and13_andFollowScale() {
        XCTAssertEqual(KrabEarTheme.Typography.bodyLarge.pointSize, 15, accuracy: 0.1)
        XCTAssertEqual(KrabEarTheme.Typography.captionLarge.pointSize, 13, accuracy: 0.1)
        KrabEarTheme.Typography.scale = 1.15
        XCTAssertEqual(KrabEarTheme.Typography.bodyLarge.pointSize, 17, accuracy: 0.6)
    }

    func test_scale_defaultIsOne_soExistingFontsUnchanged() {
        XCTAssertEqual(KrabEarTheme.Typography.scale, 1.0)
        XCTAssertEqual(KrabEarTheme.Typography.body.pointSize, 13, accuracy: 0.1)
        XCTAssertEqual(KrabEarTheme.Typography.caption.pointSize, 11, accuracy: 0.1)
    }

    func test_metrics_newSteps_onFourPointGrid() {
        for v in [KrabEarTheme.Metrics.relaxed, KrabEarTheme.Metrics.roomy, KrabEarTheme.Metrics.windowMinWidth] {
            XCTAssertEqual(v.truncatingRemainder(dividingBy: 4), 0, "значение \(v) вне сетки 4 pt")
        }
        XCTAssertGreaterThanOrEqual(KrabEarTheme.Metrics.windowMinWidth, 640)
    }

    // MARK: - Гейт размера шрифта (храповик)

    /// Точное число мелких шрифтов (< 12 pt) по файлам: литерал `ofSize: N`, `NSFont(name:…, size: N)`,
    /// `NSFont.smallSystemFontSize` (11) и `NSFont.labelFontSize` (10). Строки-комментарии не считаются.
    /// ХРАПОВИК: число должно совпадать ТОЧНО. Добавил мелкий шрифт — тест красный; убрал —
    /// тест тоже красный, пока не уменьшишь число здесь (иначе можно снова вырасти до старого).
    private static let smallFontAllowlist: [String: Int] = [
        "CallAutomationController.swift": 1,
        "HistoryPanelController+AllSettings.swift": 1,
        "HistoryPanelController+Calibration.swift": 1,
        "HistoryPanelController+CloudRewriter.swift": 2,
        "HistoryPanelController+CompareRecordings.swift": 1,
        "HistoryPanelController+ConfigPresets.swift": 2,
        "HistoryPanelController+DictationHeroCard.swift": 1,  // PR 2: карточка «Быстрый обзор» (9 pt) удалена
        "HistoryPanelController+InlineTranslation.swift": 3,
        "HistoryPanelController+QuickActions.swift": 2,
        "HistoryPanelController+RetentionSettings.swift": 1,
        "HistoryPanelController+STTModelMemory.swift": 1,
        "HistoryPanelController+STTVocabulary.swift": 3,
        "HistoryPanelController+SecuritySettings.swift": 1,
        "HistoryPanelController+Settings+ClaudeDesign.swift": 6,
        "HistoryPanelController+Settings.swift": 1,
        "HistoryPanelController+SummaryProfiles.swift": 3,
        "HistoryPanelController+VoiceCommands.swift": 2,
        "HistoryPanelController.swift": 1,
        "ModelDownloadStep.swift": 1,
        "main.swift": 2,
    ]

    /// Размер шрифта из переменной/выражения (`ofSize: x`), кроме токенов темы: тот же храповик,
    /// отдельным счётчиком на весь каталог. TODO PR 5: `controlSize = .small/.mini` и `.caption2`.
    private static let variableFontSizeAllowance = 1

    private struct FontScan {
        var small: [String: Int] = [:]
        var variable: [String: Int] = [:]
        var fileCount = 0
    }

    private func scanSources() throws -> FontScan {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        let literal = try NSRegularExpression(pattern: #"(?:ofSize|size):\s*([0-9]+(?:\.[0-9]+)?)"#)
        let named = try NSRegularExpression(pattern: #"NSFont\.(?:smallSystemFontSize|labelFontSize)"#)
        let variable = try NSRegularExpression(pattern: #"ofSize:\s*([A-Za-z_][A-Za-z0-9_.]*)"#)
        var scan = FontScan()
        guard let walker = FileManager.default.enumerator(at: sources, includingPropertiesForKeys: nil) else {
            return scan
        }
        for case let url as URL in walker where url.pathExtension == "swift" {
            let file = url.lastPathComponent
            guard file != "KrabEarTheme.swift" else { continue }
            scan.fileCount += 1
            let text = try String(contentsOf: url, encoding: .utf8)
            var small = 0, vars = 0
            for raw in text.components(separatedBy: .newlines) {
                let line = raw.trimmingCharacters(in: .whitespaces)
                if line.hasPrefix("//") || line.hasPrefix("*") || line.hasPrefix("/*") { continue }
                let r = NSRange(line.startIndex..., in: line)
                for m in literal.matches(in: line, range: r) {
                    guard let rr = Range(m.range(at: 1), in: line), let size = Double(line[rr]) else { continue }
                    // `size:` учитываем только у NSFont(name:…, size:) — у других API это не шрифт.
                    let whole = line[Range(m.range, in: line)!]
                    if whole.hasPrefix("size"), !line.contains("NSFont(name:") { continue }
                    if size < Double(KrabEarTheme.Typography.minimumPointSize) { small += 1 }
                }
                small += named.numberOfMatches(in: line, range: r)
                for m in variable.matches(in: line, range: r) {
                    guard let rr = Range(m.range(at: 1), in: line) else { continue }
                    let expr = line[rr]
                    if expr.hasPrefix("KrabEarTheme.") || expr.hasPrefix("NSFont.smallSystemFontSize")
                        || expr.hasPrefix("NSFont.labelFontSize") { continue }
                    vars += 1
                }
            }
            if small > 0 { scan.small[file] = small }
            if vars > 0 { scan.variable[file] = vars }
        }
        return scan
    }

    func test_smallFonts_exactRatchetOutsideTheme() throws {
        let scan = try scanSources()
        // Гард от пустого обхода: пустота не должна читаться как «всё чисто».
        XCTAssertGreaterThan(scan.fileCount, 100, "исходники не найдены")
        var problems: [String] = []
        for file in Set(scan.small.keys).union(Self.smallFontAllowlist.keys).sorted() {
            let actual = scan.small[file] ?? 0, allowed = Self.smallFontAllowlist[file] ?? 0
            if actual > allowed { problems.append("\(file): \(actual) > \(allowed) — новый шрифт < 12 pt, бери токен темы") }
            if actual < allowed { problems.append("\(file): \(actual) < \(allowed) — исправлено, уменьши число в allowlist") }
        }
        XCTAssertTrue(problems.isEmpty, "храповик мелких шрифтов: \(problems); факт: \(scan.small)")
    }

    func test_variableFontSizes_exactRatchet() throws {
        let scan = try scanSources()
        let total = scan.variable.values.reduce(0, +)
        XCTAssertEqual(total, Self.variableFontSizeAllowance,
                       "ofSize: <выражение> вне токенов темы: \(scan.variable) — бери токены KrabEarTheme")
    }
}
