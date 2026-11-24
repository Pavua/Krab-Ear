/*
 PanelShellLayoutTests — каркас окна (редизайн 2026-11, PR 1).

 Проверяет без поднятия HistoryPanelController:
   - режим боковой панели по ширине (рейка при 640, полная при 860/1280) и гистерезис;
   - ничто не выходит за окно, контент ≥ 559 pt при 640, строки ≥ 32 pt;
   - у каждой строки полное название в VoiceOver и подсказке (важно в режиме рейки);
   - все SF Symbols существуют; выбор раздела виден не только цветом;
   - гейт: выбор раздела по литеральному индексу запрещён (кроме fallback в showPanel).
 Если задана переменная окружения KRAB_RENDER_DIR, пишет PNG каркаса
 640/860/1280 × светлая/тёмная для визуальной проверки.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class PanelShellLayoutTests: XCTestCase {

    private typealias Tab = HistoryPanelController.PanelTab

    /// Каркас + NSTabView с 7 разделами, как в контроллере.
    private func makeShell(width: CGFloat, height: CGFloat = 820) -> (NSView, PanelShellView, NSTabView) {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        let shell = PanelShellView()
        let tabView = NSTabView()
        tabView.tabViewType = .noTabsNoBorder
        tabView.translatesAutoresizingMaskIntoConstraints = false
        for tab in Tab.ordered {
            let item = NSTabViewItem(identifier: tab.rawValue)
            item.label = tab.title
            item.view = NSView()
            tabView.addTabViewItem(item)
        }
        root.addSubview(shell)
        shell.contentHost.addSubview(tabView)
        let status = NSView()
        shell.header.installStatusView(status)
        NSLayoutConstraint.activate([
            shell.topAnchor.constraint(equalTo: root.topAnchor),
            shell.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            shell.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            shell.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            status.heightAnchor.constraint(equalToConstant: 28),
            tabView.topAnchor.constraint(equalTo: shell.contentHost.topAnchor),
            tabView.leadingAnchor.constraint(equalTo: shell.contentHost.leadingAnchor),
            tabView.trailingAnchor.constraint(equalTo: shell.contentHost.trailingAnchor),
            tabView.bottomAnchor.constraint(equalTo: shell.contentHost.bottomAnchor),
        ])
        root.layoutSubtreeIfNeeded()
        // Второй проход: смена ширины панели после первого размещения.
        root.layoutSubtreeIfNeeded()
        return (root, shell, tabView)
    }

    private func frameInRoot(_ v: NSView, _ root: NSView) -> NSRect {
        v.convert(v.bounds, to: root)
    }

    // MARK: - Режимы

    func test_mode_railAt640_fullAt860And1280() {
        XCTAssertEqual(makeShell(width: 640).1.sidebar.mode, .rail)
        XCTAssertEqual(makeShell(width: 860).1.sidebar.mode, .full)
        XCTAssertEqual(makeShell(width: 1280).1.sidebar.mode, .full)
    }

    func test_mode_hysteresis() {
        typealias S = PanelSidebarView
        XCTAssertEqual(S.mode(forShellWidth: 815, current: .full), .full, "в полосе гистерезиса режим держится")
        XCTAssertEqual(S.mode(forShellWidth: 811, current: .full), .rail)
        XCTAssertEqual(S.mode(forShellWidth: 825, current: .rail), .rail)
        XCTAssertEqual(S.mode(forShellWidth: 828, current: .rail), .full)
    }

    func test_toggleSidebarMode_switchesBothWays() {
        let shell = makeShell(width: 1280).1
        shell.toggleSidebarMode()
        XCTAssertEqual(shell.sidebar.mode, .rail)
        shell.toggleSidebarMode()
        XCTAssertEqual(shell.sidebar.mode, .full)
    }

    // MARK: - Границы

    func test_nothingOverflows_andContentIsWideEnough() {
        for (width, minContent) in [(CGFloat(640), CGFloat(559)), (860, 640), (1280, 1000)] {
            let (root, shell, tabView) = makeShell(width: width)
            let header = frameInRoot(shell.header, root)
            let content = frameInRoot(tabView, root)
            let sidebar = frameInRoot(shell.sidebar, root)
            XCTAssertLessThanOrEqual(header.maxX, width + 0.5, "шапка за окном при \(width)")
            XCTAssertGreaterThanOrEqual(content.minX, sidebar.maxX, "контент залез под панель при \(width)")
            XCTAssertLessThanOrEqual(content.maxX, width + 0.5, "контент за окном при \(width)")
            XCTAssertGreaterThanOrEqual(content.width, minContent, "контент узкий при \(width): \(content.width)")
            for row in shell.sidebar.rows {
                let f = frameInRoot(row, root)
                XCTAssertGreaterThanOrEqual(f.minX, 0)
                XCTAssertLessThanOrEqual(f.maxX, sidebar.maxX + 0.5, "строка шире панели при \(width)")
                XCTAssertGreaterThanOrEqual(f.height, 32, "цель клика < 32 pt")
            }
            XCTAssertFalse(root.hasAmbiguousLayout, "неоднозначная раскладка при \(width)")
        }
    }

    // MARK: - Доступность и содержимое

    func test_rows_haveFullTitleForVoiceOverAndTooltip_inRail() {
        let shell = makeShell(width: 640).1
        XCTAssertEqual(shell.sidebar.rows.count, 7)
        for row in shell.sidebar.rows {
            XCTAssertTrue(row.isRail)
            XCTAssertEqual(row.accessibilityLabel(), row.tab.title)
            XCTAssertEqual(row.toolTip, row.tab.title)
        }
        XCTAssertEqual(shell.sidebar.accessibilityLabel(), "Разделы")
    }

    func test_allSymbolsExist_andOrderMatchesShortcuts() {
        for tab in Tab.ordered {
            XCTAssertNotNil(NSImage(systemSymbolName: tab.symbolName, accessibilityDescription: nil), tab.symbolName)
            XCTAssertTrue(tab.symbolName.allSatisfy { $0.isASCII }, "глиф-гейт: только ASCII")
        }
        XCTAssertEqual(Tab.ordered.map(\.title),
                       ["Диктовка", "Live перевод", "История", "Разговор с AI", "Автозвонки", "Диагностика", "Архив"])
    }

    func test_select_isProgrammatic_andMarksSelectionNotOnlyByColor() {
        let shell = makeShell(width: 860).1
        var clicked: [Tab] = []
        shell.sidebar.onSelect = { clicked.append($0) }
        shell.sidebar.select(.diagnostics)
        XCTAssertTrue(clicked.isEmpty, "программный выбор не должен звать onSelect (петля с NSTabView)")
        let selected = shell.sidebar.rows.filter(\.isSelected)
        XCTAssertEqual(selected.map(\.tab), [.diagnostics])
        XCTAssertTrue(selected[0].isAccessibilitySelected())
        _ = selected[0].accessibilityPerformPress()
        XCTAssertEqual(clicked, [.diagnostics], "нажатие пользователя зовёт onSelect")
    }

    func test_rows_useLargeBodyText() {
        let shell = makeShell(width: 860).1
        for row in shell.sidebar.rows {
            let label = row.subviews.compactMap { $0 as? NSTextField }.first
            XCTAssertEqual(label?.font?.pointSize ?? 0, 15, accuracy: 0.1, "подпись раздела 15 pt")
        }
    }

    func test_header_titleIsPageTitleFont() {
        let shell = makeShell(width: 860).1
        shell.header.title = "Диктовка"
        let label = shell.header.subviews.compactMap { $0 as? NSTextField }.first
        XCTAssertEqual(label?.font?.pointSize ?? 0, KrabEarTheme.Typography.pageTitle.pointSize, accuracy: 0.1)
    }

    // MARK: - Гейт: выбор раздела только через selectPanelTab

    func test_noLiteralIndexTabSelectionOutsideShowPanelFallback() throws {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent")
        let files = try FileManager.default.contentsOfDirectory(atPath: sources.path)
            .filter { $0.hasPrefix("HistoryPanelController") && $0.hasSuffix(".swift") }
        XCTAssertGreaterThan(files.count, 20, "исходники не найдены")
        let literal = try NSRegularExpression(pattern: #"selectTabViewItem\(at:\s*[0-9]+\)"#)
        var hits: [String] = []
        for file in files {
            let text = try String(contentsOf: sources.appendingPathComponent(file), encoding: .utf8)
            for line in text.components(separatedBy: .newlines) {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("//") { continue }
                if trimmed.contains("tabSelector") { hits.append("\(file): \(trimmed)") }
                let r = NSRange(trimmed.startIndex..., in: trimmed)
                if literal.firstMatch(in: trimmed, range: r) != nil { hits.append("\(file): \(trimmed)") }
            }
        }
        // Единственное законное место — fallback «История» в showPanel() под isSyncingTabs.
        XCTAssertEqual(hits, ["HistoryPanelController.swift: mainTabView.selectTabViewItem(at: 2)"],
                       "выбор раздела — только через selectPanelTab(_:)")
    }

    // MARK: - Офлайн-рендер PNG (для визуального гейта)

    func test_renderPNG_whenRequested() throws {
        guard let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] else {
            throw XCTSkip("KRAB_RENDER_DIR не задан — рендер пропущен")
        }
        for width in [CGFloat(640), 860, 1280] {
            for (name, ap) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                let (root, shell, tabView) = makeShell(width: width, height: 560)
                root.appearance = NSAppearance(named: ap)
                root.wantsLayer = true
                tabView.selectTabViewItem(at: 0)
                shell.sidebar.select(.dictation)
                shell.header.title = Tab.dictation.title
                let card = ThemeCardView()
                card.title = "Пример карточки"
                card.translatesAutoresizingMaskIntoConstraints = false
                tabView.selectedTabViewItem?.view?.addSubview(card)
                if let host = tabView.selectedTabViewItem?.view {
                    NSLayoutConstraint.activate([
                        card.topAnchor.constraint(equalTo: host.topAnchor),
                        card.leadingAnchor.constraint(equalTo: host.leadingAnchor),
                        card.trailingAnchor.constraint(equalTo: host.trailingAnchor),
                        card.heightAnchor.constraint(equalToConstant: 120),
                    ])
                }
                root.appearance?.performAsCurrentDrawingAppearance {
                    root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
                }
                root.layoutSubtreeIfNeeded()
                root.layoutSubtreeIfNeeded()
                guard let rep = root.bitmapImageRepForCachingDisplay(in: root.bounds) else { continue }
                root.cacheDisplay(in: root.bounds, to: rep)
                let url = URL(fileURLWithPath: dir).appendingPathComponent("shell_\(Int(width))_\(name).png")
                try rep.representation(using: .png, properties: [:])?.write(to: url)
            }
        }
    }
}
