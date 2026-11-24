/*
 PanelLiveResizeTests — «живой ресайз» настоящего HistoryPanelController (редизайн 2026-11).

 Поднимает контроллер окна без показа на экране (IPC на несуществующий сокет, без
 микрофона и сети), проходит все 7 разделов на ширинах 640 → 1600 → 640 и на
 сохранённой рамке владельца 860×820, в светлой и тёмной теме, и проверяет:
   - нет схлопнутых карточек (уже 40 % ширины раздела) среди карточек верхнего уровня;
   - ничто не выходит за правую границу раздела (кроме содержимого горизонтальных скроллов);
   - AppKit не пишет «Unable to simultaneously satisfy constraints» (stderr перехватывается);
   - однострочные подписи не обрезаны без многоточия.
 KRAB_RESIZE_REPORT=1 печатает полный отчёт (для расследования), не только нарушения.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class PanelLiveResizeTests: XCTestCase {

    private typealias Tab = HistoryPanelController.PanelTab

    private func makeController() -> HistoryPanelController {
        let ipc = IPCClient(socketPath: "/nonexistent-krab-ear-test.sock")
        return HistoryPanelController(
            ipcClient: ipc,
            plaintextExportCoordinator: PlaintextExportCoordinator(ipc: ipc),
            settingsProvider: { .default },
            settingsUpdater: { _ in .default },
            onToggleRecording: {},
            onRestartAgent: {},
            onStopAgent: {},
            onPasteHistoryItem: { _ in },
            onSwapRuEsDirection: {}
        )
    }

    // MARK: - Перехват stderr (сообщения AppKit о конфликтах констрейнтов)

    private func captureStderr(_ body: () -> Void) -> String {
        let pipe = Pipe()
        let saved = dup(STDERR_FILENO)
        fflush(stderr)
        dup2(pipe.fileHandleForWriting.fileDescriptor, STDERR_FILENO)
        body()
        fflush(stderr)
        dup2(saved, STDERR_FILENO)
        close(saved)
        pipe.fileHandleForWriting.closeFile()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }

    // MARK: - Обход

    private func descendants(_ v: NSView) -> [NSView] {
        v.subviews + v.subviews.flatMap { descendants($0) }
    }

    private func isEffectivelyVisible(_ v: NSView, under root: NSView) -> Bool {
        var cur: NSView? = v
        while let c = cur, c !== root {
            if c.isHidden || c.alphaValue == 0 { return false }
            cur = c.superview
        }
        return cur === root
    }

    /// Внутри ли горизонтально прокручиваемого содержимого (таблицы, длинные строки).
    private func insideHorizontalScroll(_ v: NSView, under root: NSView) -> Bool {
        var cur = v.superview
        while let c = cur, c !== root {
            if let sv = c as? NSScrollView, sv.hasHorizontalScroller { return true }
            if c is NSTableView || c is NSOutlineView { return true }
            cur = c.superview
        }
        return false
    }

    private struct Issue: CustomStringConvertible {
        let tab: String, width: CGFloat, theme: String, kind: String, detail: String
        var description: String { "[\(tab) @\(Int(width)) \(theme)] \(kind): \(detail)" }
    }

    private func name(_ v: NSView) -> String {
        var s = String(describing: type(of: v))
        if let t = (v as? ThemeCardView)?.title, !t.isEmpty { s += "«\(t)»" }
        if let f = v as? NSTextField, !f.stringValue.isEmpty { s += "«\(f.stringValue.prefix(40))»" }
        if let b = v as? NSButton, !b.title.isEmpty { s += "«\(b.title.prefix(30))»" }
        if let id = v.identifier?.rawValue { s += "#\(id)" }
        return s
    }

    /// Цепочка родителей (до 5) с подписями секций/карточек — найти, где схлопнулось.
    private func path(_ v: NSView, _ root: NSView) -> String {
        var parts: [String] = []
        var cur = v.superview
        while let c = cur, c !== root, parts.count < 5 {
            var s = String(describing: type(of: c))
            if let sec = c as? CollapsibleSectionView { s += "«\(sec.sectionId)»" }
            if let st = c as? NSStackView { s += "(\(st.orientation == .vertical ? "v" : "h"),\(Int(c.frame.width)))" }
            parts.append(s)
            cur = c.superview
        }
        let inner = descendants(v).compactMap { ($0 as? NSTextField)?.stringValue }.first { !$0.isEmpty } ?? ""
        return parts.joined(separator: " ← ") + " | текст: «\(inner.prefix(30))»"
    }

    private func inspect(tab: Tab, controller: HistoryPanelController, width: CGFloat, theme: String) -> [Issue] {
        guard let item = controller.mainTabView.selectedTabViewItem, let root = item.view else { return [] }
        var issues: [Issue] = []
        let rootW = root.bounds.width
        guard rootW > 0 else {
            return [Issue(tab: tab.title, width: width, theme: theme, kind: "пустой раздел", detail: "ширина 0")]
        }
        // 0) Окно сжалось до запрошенной ширины, и сам раздел не шире окна: минимальные ширины
        //    содержимого с приоритетом > 500 распирают окно (оно «не сужается»).
        if let content = controller.window?.contentView {
            if content.bounds.width > width + 1 {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "окно не сжалось",
                                    detail: "запрошено \(Int(width)), осталось \(Int(content.bounds.width))"))
            }
            let r = root.convert(root.bounds, to: content)
            if r.maxX > content.bounds.width + 1 {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "раздел шире окна",
                                    detail: "maxX \(Int(r.maxX)) > окно \(Int(content.bounds.width))"))
            }
        }
        for v in descendants(root) where isEffectivelyVisible(v, under: root) {
            let f = v.convert(v.bounds, to: root)
            if f.width <= 0 || f.height <= 0 { continue }
            let hScroll = insideHorizontalScroll(v, under: root)
            // 1) Схлопнутые карточки и секции верхнего уровня.
            if (v is ThemeCardView || v is CollapsibleSectionView), !hScroll,
               !(v.superview is NSStackView && (v.superview as? NSStackView)?.orientation == .horizontal),
               f.width < rootW * 0.4 {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "схлопнута",
                                    detail: "\(name(v)) \(Int(f.width)) из \(Int(rootW)) pt; путь: \(path(v, root))"))
            }
            // 2) Выход за правую границу раздела.
            if !hScroll, f.maxX > rootW + 1, !(v is NSScroller) {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "за границей",
                                    detail: "\(name(v)) maxX \(Int(f.maxX)) > \(Int(rootW))"))
            }
            // 3) Однострочная подпись обрезана без многоточия.
            if let label = v as? NSTextField, !label.isEditable, !hScroll,
               label.maximumNumberOfLines == 1 || label.cell?.wraps == false,
               label.lineBreakMode == .byClipping || label.lineBreakMode == .byWordWrapping || label.lineBreakMode == .byCharWrapping,
               !label.stringValue.isEmpty,
               label.intrinsicContentSize.width > label.bounds.width + 2,
               label.cell?.wraps != true {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "обрезана подпись",
                                    detail: "\(name(label)) нужно \(Int(label.intrinsicContentSize.width)) есть \(Int(label.bounds.width))"))
            }
        }
        return issues
    }

    // MARK: - Тест

    private let widths: [CGFloat] = [640, 860, 1100, 1600, 1100, 860, 640]

    func test_liveResize_allTabs_bothThemes() {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        var issues: [Issue] = []
        var conflictLog = ""
        let verbose = ProcessInfo.processInfo.environment["KRAB_RESIZE_REPORT"] != nil

        for (theme, ap) in [("светлая", NSAppearance.Name.aqua), ("тёмная", .darkAqua)] {
            window.appearance = NSAppearance(named: ap)
            for tab in Tab.ordered {
                conflictLog += captureStderr {
                    controller.selectPanelTab(tab)
                    for w in widths + [860] {
                        let h: CGFloat = (w == 860 && tab == Tab.ordered.last) ? 820 : 820
                        window.setContentSize(NSSize(width: w, height: h))
                        window.contentView?.layoutSubtreeIfNeeded()
                        window.contentView?.layoutSubtreeIfNeeded()
                        issues += inspect(tab: tab, controller: controller, width: w, theme: theme)
                    }
                }
            }
        }

        let conflicts = conflictLog.components(separatedBy: "Unable to simultaneously satisfy").count - 1
        // Дубликаты (одинаковая проблема на нескольких ширинах/темах) сворачиваем.
        var seen = Set<String>()
        let unique = issues.filter { seen.insert("\($0.tab)|\($0.kind)|\($0.detail)").inserted }
        if verbose {
            print("=== PanelLiveResize: нарушений \(unique.count), конфликтов констрейнтов \(conflicts)")
            unique.forEach { print("  \($0)") }
            if conflicts > 0 { print(conflictLog.prefix(4000)) }
        }
        XCTAssertEqual(conflicts, 0, "конфликты констрейнтов при ресайзе: \(conflicts)")
        XCTAssertTrue(unique.isEmpty, "нарушения раскладки при ресайзе (\(unique.count)):\n" + unique.prefix(40).map(\.description).joined(separator: "\n"))
    }

    /// Поле ключа Voice Gateway обязано быть на экране (владелец не нашёл, где его вводить).
    func test_voiceGatewayKeyField_isReachableOnScreen() {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        window.setContentSize(NSSize(width: 860, height: 820))
        var found: [String] = []
        for tab in Tab.ordered {
            controller.selectPanelTab(tab)
            window.contentView?.layoutSubtreeIfNeeded()
            guard let root = controller.mainTabView.selectedTabViewItem?.view else { continue }
            let field = controller.voiceGatewayAPIKeyField
            if field.window != nil, isEffectivelyVisible(field, under: root), field.bounds.width > 40 {
                found.append(tab.title)
            }
        }
        if ProcessInfo.processInfo.environment["KRAB_RESIZE_REPORT"] != nil {
            print("=== Поле API key Voice Gateway видно в разделах: \(found)")
        }
        XCTAssertFalse(found.isEmpty, "поле ключа Voice Gateway не видно ни в одном разделе")
    }
}
