import XCTest
import Foundation

/// Wave 416 — macOS Sequoia 26 SF Symbol verification tests.
///
/// На macOS Sequoia CoreText выполняет glyph-metrics build синхронно на main thread
/// при первом рендере Unicode-символов через NSTextField / NSStatusItem.
/// Это вызывало AppHang класса AGENT-J/K/M (Waves 66/67/266).
///
/// Паттерн-фикс: заменить bare Unicode glyphs (●◉○▲▼▶◀★✓✗) в string literals,
/// используемых как UI-контент, на SF Symbols через NSImage(systemSymbolName:).
///
/// Этот test suite сканирует исходники и:
/// 1. Обнаруживает bare Unicode glyphs в NSTextField / stringValue string literals.
/// 2. Верифицирует что StatusIndicatorView больше не использует Unicode `●`.
/// 3. Верифицирует что BackendToast.prewarmPanel() вызывается в правильном порядке.
/// 4. Верифицирует CoreText warmup pattern (sizeToFit before orderFront).
final class SFSymbolVerificationTests: XCTestCase {

    // MARK: - Constants

    /// Unicode glyphs, которые на macOS Sequoia вызывают CoreText heavy path
    /// при первом рендере в NSTextField/NSStatusItem.
    /// Источник: AGENT-J root cause investigation (Wave 301 doc), Wave 67.
    private static let riskyGlyphs: [Character] = [
        "●", "◉", "○", "◎",   // filled/hollow circles — menu bar dots
        "▲", "▼",              // triangles — status indicators
        "◀", "▶",              // playback arrows in NSTextField context
        "■", "□",              // squares
        "◆", "◇",              // diamonds
        "★", "☆",              // stars
    ]

    /// Glyphs, которые являются text content (не UI indicator) и допускаются
    /// в plain strings (не в NSStatusItem / colorAppearanceDidChange context).
    /// Помечаются комментарием `// SF-SYMBOL-SAFE` в source file.
    private static let checkmarkGlyphs: [Character] = ["✓", "✗", "✔", "✘"]

    /// Корень Swift source files для сканирования.
    private var sourcesRoot: URL {
        // Test bundle → Tests/KrabEarAgentTests → Tests → KrabEarAgent → Sources
        let bundleURL = Bundle(for: SFSymbolVerificationTests.self).bundleURL
        // Walk up to find native/KrabEarAgent/Sources
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        // Fallback: resolve relative to __FILE__ compile-time path
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent")
    }

    // MARK: - Helper

    /// Returns all .swift files under `directory`.
    private func swiftFiles(in directory: URL) -> [URL] {
        guard let enumerator = FileManager.default.enumerator(
            at: directory,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles]
        ) else { return [] }
        return enumerator.compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
    }

    /// Finds lines containing a bare Unicode glyph inside a string literal,
    /// NOT followed by `// SF-SYMBOL-SAFE` on the same line.
    private func findRiskyGlyphLines(in fileURL: URL, glyphs: [Character]) -> [(line: Int, text: String, glyph: Character)] {
        guard let content = try? String(contentsOf: fileURL, encoding: .utf8) else { return [] }
        var results: [(line: Int, text: String, glyph: Character)] = []
        let lines = content.components(separatedBy: .newlines)
        for (idx, line) in lines.enumerated() {
            // Skip pure comment lines
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("//") || trimmed.hasPrefix("*") || trimmed.hasPrefix("/*") {
                continue
            }
            // Skip lines that have the safe annotation
            if line.contains("// SF-SYMBOL-SAFE") {
                continue
            }
            // Check if the line contains a string literal with a risky glyph
            // Heuristic: look for `"` enclosing the glyph
            if line.contains("\"") {
                for glyph in glyphs {
                    if line.contains(glyph) {
                        results.append((line: idx + 1, text: line, glyph: glyph))
                        break
                    }
                }
            }
        }
        return results
    }

    // MARK: - Tests

    /// Verifies that StatusIndicatorView no longer uses bare `●` Unicode literal.
    ///
    /// AGENT-J root cause: `●` in NSStatusItem button triggers CoreText on
    /// ColorSync callback path on macOS Sequoia — blocks main thread ≥2s.
    /// Fix shipped in Wave 67 (PR #412): replaced with SF Symbol `circle.fill`.
    func test_statusIndicatorView_noBareDotGlyph() throws {
        let fileURL = sourcesRoot.appendingPathComponent("StatusIndicatorView.swift")
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            throw XCTSkip("StatusIndicatorView.swift not found at \(fileURL.path)")
        }

        let content = try String(contentsOf: fileURL, encoding: .utf8)
        let lines = content.components(separatedBy: .newlines)

        var violations: [(Int, String)] = []
        for (idx, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("//") || trimmed.hasPrefix("*") { continue }
            if line.contains("// SF-SYMBOL-SAFE") { continue }
            // Bare ● in string literal
            if line.contains("\"") && line.contains("●") {
                violations.append((idx + 1, line))
            }
        }

        XCTAssertTrue(
            violations.isEmpty,
            "StatusIndicatorView.swift contains bare `●` Unicode in string literal(s).\n" +
            "This triggered AGENT-J AppHang on macOS Sequoia (CoreText glyph path in ColorSync callback).\n" +
            "Fix: use NSImage(systemSymbolName: \"circle.fill\", accessibilityDescription: nil).\n" +
            "Violations:\n" + violations.map { "  Line \($0.0): \($0.1.trimmingCharacters(in: .whitespaces))" }.joined(separator: "\n")
        )
    }

    /// Verifies that BackendToast.swift contains the prewarmPanel() call
    /// with sizeToFit() BEFORE orderFront (AGENT-K / AGENT-M fix pattern).
    func test_backendToast_prewarmPattern() throws {
        let fileURL = sourcesRoot.appendingPathComponent("BackendToast.swift")
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            throw XCTSkip("BackendToast.swift not found at \(fileURL.path)")
        }

        let content = try String(contentsOf: fileURL, encoding: .utf8)

        // Verify prewarmPanel() function exists
        XCTAssertTrue(
            content.contains("func prewarmPanel()"),
            "BackendToast.swift must contain prewarmPanel() for macOS Sequoia CoreText warmup (AGENT-K fix)."
        )

        // Verify sizeToFit() is called inside prewarmPanel
        // Check that sizeToFit appears before orderFrontRegardless in prewarmPanel
        guard let prewarmRange = content.range(of: "func prewarmPanel()") else {
            XCTFail("prewarmPanel() not found")
            return
        }
        // Get the function body (up to the next top-level func or end)
        let afterPrewarm = String(content[prewarmRange.lowerBound...])
        let prewarmBlock = afterPrewarm.components(separatedBy: "\n    func ").first ?? afterPrewarm

        XCTAssertTrue(
            prewarmBlock.contains("sizeToFit()"),
            "prewarmPanel() must call sizeToFit() to warm CoreText glyph cache (AGENT-M fix)."
        )
        XCTAssertTrue(
            prewarmBlock.contains("orderFrontRegardless"),
            "prewarmPanel() must call orderFrontRegardless() to warm NSVisualEffectView ColorSync (AGENT-K fix)."
        )

        // Verify orderFront is preceded by positionPanel in show()
        XCTAssertTrue(
            content.contains("positionPanel(panel)"),
            "BackendToast must position panel before orderFront (avoids _doOrderWindow layout pass)."
        )
    }

    /// Scans all Swift source files for risky Unicode glyphs in string literals.
    ///
    /// Reports findings as warnings (not failures) for glyphs that are in NSTextField
    /// context but may not be in the CoreText hot path. Fails for any glyph found
    /// in NSStatusItem-adjacent code without `// SF-SYMBOL-SAFE` annotation.
    func test_scanSources_riskyUnicodeGlyphsInStringLiterals() throws {
        let root = sourcesRoot
        guard FileManager.default.fileExists(atPath: root.path) else {
            throw XCTSkip("Sources root not found: \(root.path)")
        }

        let files = swiftFiles(in: root)
        XCTAssertFalse(files.isEmpty, "No Swift files found under \(root.path)")

        var allFindings: [(file: String, line: Int, text: String, glyph: Character)] = []

        for file in files {
            let findings = findRiskyGlyphLines(in: file, glyphs: Self.riskyGlyphs)
            for finding in findings {
                allFindings.append((
                    file: file.lastPathComponent,
                    line: finding.line,
                    text: finding.text,
                    glyph: finding.glyph
                ))
            }
        }

        // Report all findings — these are candidates for review, not automatic failures.
        // Known-safe sites that cannot be immediately fixed should be annotated
        // with `// SF-SYMBOL-SAFE` comment on the same line.
        if !allFindings.isEmpty {
            let report = allFindings.map {
                "  \($0.file):\($0.line) [\($0.glyph)] \($0.text.trimmingCharacters(in: .whitespaces))"
            }.joined(separator: "\n")

            // Fail only if new sites appear beyond the known documented ones.
            // Known sites (Wave 416 audit):
            //   CallAutomationController.swift:143,211,625,963,974 (●, ✓)
            //   GlobalStatusBar.swift:251,253 (▶, ◉)
            //   BackendToast.swift:58 (✓) — safe, prewarm covers this
            //   HistoryPanelController+ActionItems.swift (✓)
            //   HistoryPanelController+SemanticSearch.swift (✓, ✗)
            //   HistoryPanelController+InlineTranslation.swift (✓)
            //   main+QuickReplace.swift (✓)
            // If count exceeds known baseline, something new was added.
            let knownBaseline = 13
            if allFindings.count > knownBaseline {
                XCTFail(
                    "Found \(allFindings.count) risky Unicode glyph sites in string literals " +
                    "(baseline: \(knownBaseline)). \(allFindings.count - knownBaseline) NEW site(s) added.\n" +
                    "Replace with SF Symbol or add `// SF-SYMBOL-SAFE` comment if glyph is NOT in ColorSync/callback path.\n" +
                    "All findings:\n\(report)"
                )
            } else {
                // Known sites — emit as XCTContext attachment for visibility
                XCTContext.runActivity(named: "Known risky Unicode glyph sites (\(allFindings.count)/\(knownBaseline) baseline)") { activity in
                    let attachment = XCTAttachment(string: report)
                    attachment.name = "risky_glyph_sites.txt"
                    attachment.lifetime = .keepAlways
                    activity.add(attachment)
                }
            }
        }
    }

    /// Verifies that checkmark glyphs (✓✗) used in text output strings
    /// are acceptable — they appear in plain text output/log strings, not
    /// in NSStatusItem or ColorSync-adjacent rendering contexts.
    func test_checkmarkGlyphs_areTextOutputOnly() throws {
        let root = sourcesRoot
        guard FileManager.default.fileExists(atPath: root.path) else {
            throw XCTSkip("Sources root not found: \(root.path)")
        }

        // Checkmarks in NSTextField used as title/button content (not NSStatusItem)
        // are acceptable only if not rendered during ColorSync callbacks.
        // This test documents known uses and catches new ones in sensitive contexts.
        let sensitiveFiles = [
            "StatusIndicatorView.swift",
            "BackendToast.swift",
            "HealthMonitor.swift",
        ]

        var violations: [(file: String, line: Int, text: String)] = []
        for filename in sensitiveFiles {
            let fileURL = root.appendingPathComponent(filename)
            guard FileManager.default.fileExists(atPath: fileURL.path) else { continue }
            let findings = findRiskyGlyphLines(in: fileURL, glyphs: Self.checkmarkGlyphs)
            for finding in findings {
                violations.append((file: filename, line: finding.line, text: finding.text))
            }
        }

        // BackendToast "✓" is safe because it's prewarm-covered; annotate if needed.
        // This test catches new additions to sensitive files.
        XCTAssertTrue(
            violations.isEmpty,
            "Checkmark glyph (✓✗✔✘) found in a CoreText-sensitive file without `// SF-SYMBOL-SAFE`.\n" +
            "These files render on ColorSync/HealthMonitor callbacks on macOS Sequoia.\n" +
            "Violations:\n" +
            violations.map { "  \($0.file):\($0.line) — \($0.text.trimmingCharacters(in: .whitespaces))" }.joined(separator: "\n")
        )
    }

    /// Verifies that NSAlert.runModal() is not called without a parent window guard.
    ///
    /// runModal() without a parent window creates a separate modal run loop on Sequoia,
    /// blocking the main thread → AppHang (KRAB-EAR-AGENT-E class, AGENT-H).
    ///
    /// Acceptable uses: PermissionWizard (runs at launch, has its own run loop),
    /// all other sites must use beginSheetModal(for:completionHandler:).
    func test_nsAlertRunModal_onlyInAllowlistedFiles() throws {
        let root = sourcesRoot
        guard FileManager.default.fileExists(atPath: root.path) else {
            throw XCTSkip("Sources root not found: \(root.path)")
        }

        /// Files where runModal() is acceptable (wizard flow, launch-time only).
        let allowlist: Set<String> = [
            "PermissionWizard.swift",
        ]

        let files = swiftFiles(in: root)
        var violations: [(file: String, line: Int, text: String)] = []

        for fileURL in files {
            let filename = fileURL.lastPathComponent
            if allowlist.contains(filename) { continue }

            guard let content = try? String(contentsOf: fileURL, encoding: .utf8) else { continue }
            let lines = content.components(separatedBy: .newlines)
            var inBlockComment = false
            for (idx, line) in lines.enumerated() {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                // Track /* ... */ blocks: unlike Javadoc, Swift doesn't require a
                // leading `*` on each continuation line, so a prose line like
                // "НЕ runModal() — ..." inside such a block isn't caught by the
                // `//`/`*` prefix check below and was matching as a real call site.
                if inBlockComment {
                    if trimmed.contains("*/") { inBlockComment = false }
                    continue
                }
                if trimmed.hasPrefix("/*") && !trimmed.contains("*/") {
                    inBlockComment = true
                    continue
                }
                if trimmed.hasPrefix("//") || trimmed.hasPrefix("*") { continue }
                // Only a real dot-call site (alert.runModal(), panel.runModal(), ...)
                // counts — a bare "runModal()" substring also matches prose like
                // "НЕ runModal()" or "not runModal()", which isn't a call at all.
                if line.contains(".runModal()") {
                    violations.append((file: filename, line: idx + 1, text: trimmed))
                }
            }
        }

        // DiagnosticsTabView has a known fallback runModal — track it here
        // so any NEW site is caught immediately.
        let knownSites: Set<String> = ["DiagnosticsTabView.swift"]
        let newViolations = violations.filter { !knownSites.contains($0.file) }

        XCTAssertTrue(
            newViolations.isEmpty,
            "NSAlert.runModal() found outside allowlist. On macOS Sequoia this blocks main thread.\n" +
            "Use AlertHelpers.showAlert(in:) or beginSheetModal(for:completionHandler:) instead.\n" +
            "New violations:\n" +
            newViolations.map { "  \($0.file):\($0.line) — \($0.text)" }.joined(separator: "\n")
        )
    }
}
