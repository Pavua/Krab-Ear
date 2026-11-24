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

    /// Допустимое число литералов `ofSize: N` с N < 12 по файлам на момент PR 0.
    /// Список только СОКРАЩАЕТСЯ: новый мелкий шрифт вне темы роняет тест; исправил — уменьши число.
    private static let smallFontAllowlist: [String: Int] = [
        "CallAutomationController.swift": 1,
        "HistoryPanelController+AllSettings.swift": 1,
        "HistoryPanelController+Calibration.swift": 1,
        "HistoryPanelController+CloudRewriter.swift": 2,
        "HistoryPanelController+CompareRecordings.swift": 1,
        "HistoryPanelController+ConfigPresets.swift": 2,
        "HistoryPanelController+DictationHeroCard.swift": 2,
        "HistoryPanelController+InlineTranslation.swift": 3,
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

    func test_smallFontLiterals_doNotGrowOutsideTheme() throws {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        let files = try FileManager.default.contentsOfDirectory(atPath: sources.path)
            .filter { $0.hasSuffix(".swift") && $0 != "KrabEarTheme.swift" }
        // Гард от пустого списка: пустота не должна читаться как «всё чисто».
        XCTAssertGreaterThan(files.count, 100, "каталог исходников не найден: \(sources.path)")

        let regex = try NSRegularExpression(pattern: #"ofSize:\s*([0-9]+(?:\.[0-9]+)?)"#)
        var offenders: [String] = []
        for file in files {
            let text = try String(contentsOf: sources.appendingPathComponent(file), encoding: .utf8)
            let range = NSRange(text.startIndex..., in: text)
            var small = 0
            regex.enumerateMatches(in: text, range: range) { m, _, _ in
                guard let m, let r = Range(m.range(at: 1), in: text), let size = Double(text[r]) else { return }
                if size < Double(KrabEarTheme.Typography.minimumPointSize) { small += 1 }
            }
            let allowed = Self.smallFontAllowlist[file] ?? 0
            if small > allowed { offenders.append("\(file): \(small) > \(allowed)") }
        }
        XCTAssertTrue(offenders.isEmpty, "новые шрифты < 12 pt вне темы: \(offenders)")
    }
}
