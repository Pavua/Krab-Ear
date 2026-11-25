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
 Стресс (test_liveResize_longTextStress): все подписи ~200 символов + проверка обрезки
 перенесённого текста по высоте. KRAB_STRESS_DIAG=1 — самые широкие «листья» на 640 и их
 горизонтальные констрейнты с приоритетом > 500 (так найден виновник «окно не сжалось»).
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
        if let f = v as? NSTextField, f.stringValue.isEmpty, let ph = f.placeholderString, !ph.isEmpty { s += "‹\(ph.prefix(30))›" }
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
            let hScroll = insideHorizontalScroll(v, under: root)
            // 0б) Поле ввода сжато до нечитаемого — до пропуска нулевых рамок: поиск «Истории»
            //     на 640 получал ширину 0 (на экране — один значок лупы под «Сбросить»).
            if let field = v as? NSTextField, field.isEditable, !hScroll, f.height > 0, field.bounds.width < 40 {
                issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "сжато поле",
                                    detail: "\(name(field)) \(Int(field.bounds.width)) pt; путь: \(path(field, root))"))
            }
            if f.width <= 0 || f.height <= 0 { continue }
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
                                    detail: "\(name(v)) maxX \(Int(f.maxX)) > \(Int(rootW)); путь: \(path(v, root))"))
            }
            // 2б) Подпись в ручном горизонтальном ряду сложилась «столбиком слов» (нит гейта
            //     #2255: «Источник звонка:» 28×96 pt) — такой ряд должен быть AdaptiveRowStackView.
            if let label = v as? NSTextField, !label.isEditable, !hScroll, label.cell?.wraps == true,
               let row = label.superview as? NSStackView, row.orientation == .horizontal,
               let font = label.font {
                let line = NSLayoutManager().defaultLineHeight(for: font)
                if label.bounds.height > line * 2.5, label.bounds.width < 160 {
                    issues.append(Issue(tab: tab.title, width: width, theme: theme, kind: "подпись столбиком",
                                        detail: "\(name(label)) \(Int(label.bounds.width))×\(Int(label.bounds.height)); путь: \(path(label, root))"))
                }
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

    /// Раскрывает все CollapsibleSectionView во всех разделах (без анимации).
    private func expandAllSections(in controller: HistoryPanelController) {
        for item in controller.mainTabView.tabViewItems {
            guard let root = item.view else { continue }
            for case let section as CollapsibleSectionView in descendants(root) where !section.isExpanded {
                section.setExpanded(true, animated: false)
            }
        }
    }

    // MARK: - Тест

    private let widths: [CGFloat] = [640, 860, 1100, 1600, 1100, 860, 640]

    /// XCTest сообщает о программной активации конфликтующих констрейнтов отдельным issue
    /// («Conflicting constraints detected»), а не строкой в stderr — перехват stderr его не видел
    /// (нит гейта #2255: конфликт setRail прошёл незамеченным). Собираем и проверяем явно.
    private var conflictIssues: [String] = []

    override func record(_ issue: XCTIssue) {
        if issue.compactDescription.contains("Conflicting constraints") {
            conflictIssues.append(issue.compactDescription)
        }
        super.record(issue)
    }

    override func tearDown() {
        let found = conflictIssues
        conflictIssues = []
        XCTAssertTrue(found.isEmpty, "конфликты констрейнтов (issue XCTest): \(found.prefix(3))")
        super.tearDown()
    }

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
                    // Гейт Opus #2254: 72 из 85 секций по умолчанию свёрнуты, и их содержимое
                    // (настройки) пропускалось как скрытое; состояние ещё и читается из
                    // UserDefaults. Раскрываем ВСЁ — проверка не зависит от окружения.
                    expandAllSections(in: controller)
                    for w in widths + [860] {
                        window.setContentSize(NSSize(width: w, height: 820))
                        window.contentView?.layoutSubtreeIfNeeded()
                        window.contentView?.layoutSubtreeIfNeeded()
                        issues += inspect(tab: tab, controller: controller, width: w, theme: theme)
                    }
                    // Отрисовка — обязательная часть проверки: таблицы раскладывают колонки
                    // только при показе (таблица «Автозвонков» выходила за край на 640 лишь
                    // после отрисовки). PNG пишутся при KRAB_RENDER_DIR (светлая тема).
                    if let content = window.contentView {
                        for w: CGFloat in [860, 640] {
                            window.setContentSize(NSSize(width: w, height: 820))
                            content.layoutSubtreeIfNeeded()
                            content.layoutSubtreeIfNeeded()
                            guard let rep = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { continue }
                            content.cacheDisplay(in: content.bounds, to: rep)
                            content.layoutSubtreeIfNeeded()
                            issues += inspect(tab: tab, controller: controller, width: w, theme: theme + ", после отрисовки")
                            if theme == "светлая", let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] {
                                try? rep.representation(using: .png, properties: [:])?
                                    .write(to: URL(fileURLWithPath: dir).appendingPathComponent("panel_\(tab.rawValue)_\(Int(w)).png"))
                            }
                        }
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

    /// Подпись с переносом, у которой текст не помещается по высоте (видна только часть строк).
    private func heightClipped(_ label: NSTextField) -> Bool {
        guard !label.isEditable, label.cell?.wraps == true, label.maximumNumberOfLines == 0,
              label.bounds.width > 0, let cell = label.cell else { return false }
        let need = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: label.bounds.width,
                                                   height: .greatestFiniteMagnitude)).height
        return need > label.bounds.height + 2
    }

    /// Стресс (гейт Opus #2254): текст подписей меняется в рантайме (статусы, ошибки шлюза,
    /// пояснения). Все подписи получают ~200 символов — окно всё равно сжимается до 640,
    /// ничто не выходит за границу, перенесённый текст не обрезан по высоте.
    func test_liveResize_longTextStress() {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        let long = String(repeating: "очень длинное пояснение ", count: 9)
        var issues: [Issue] = []
        var conflictLog = ""
        for tab in Tab.ordered {
            conflictLog += captureStderr {
                controller.selectPanelTab(tab)
                expandAllSections(in: controller)
                guard let root = controller.mainTabView.selectedTabViewItem?.view else { return }
                for case let label as NSTextField in descendants(root)
                where !label.isEditable && !label.stringValue.isEmpty && !insideHorizontalScroll(label, under: root) {
                    label.stringValue = long
                }
                for w: CGFloat in [860, 640, 1100, 640] {
                    window.setContentSize(NSSize(width: w, height: 820))
                    window.contentView?.layoutSubtreeIfNeeded()
                    window.contentView?.layoutSubtreeIfNeeded()
                    issues += inspect(tab: tab, controller: controller, width: w, theme: "стресс")
                    if ProcessInfo.processInfo.environment["KRAB_STRESS_DIAG"] != nil, w == 640 {
                        for v in descendants(root) where isEffectivelyVisible(v, under: root)
                            && v.subviews.isEmpty == false && v.frame.width > 700 && !insideHorizontalScroll(v, under: root) {
                            let kids = v.subviews.filter { $0.frame.width > 700 }
                            if kids.isEmpty {
                                print("DIAG[\(tab.title)] \(name(v)) w=\(Int(v.frame.width)) TAMIC=\(v.translatesAutoresizingMaskIntoConstraints) путь: \(path(v, root))")
                                for c in v.constraintsAffectingLayout(for: .horizontal) where c.priority.rawValue > 500 {
                                    print("   C \(c)")
                                }
                                for s in v.subviews {
                                    print("   KID \(name(s)) w=\(Int(s.frame.width)) intr=\(Int(s.intrinsicContentSize.width)) fit=\(Int(s.fittingSize.width)) CR=\(s.contentCompressionResistancePriority(for: .horizontal).rawValue)")
                                }
                            }
                        }
                    }
                    for case let label as NSTextField in descendants(root)
                    where isEffectivelyVisible(label, under: root) && !insideHorizontalScroll(label, under: root)
                        && heightClipped(label) {
                        issues.append(Issue(tab: tab.title, width: w, theme: "стресс", kind: "обрезана по высоте",
                                            detail: "\(name(label)) высота \(Int(label.bounds.height)); путь: \(path(label, root))"))
                    }
                }
            }
        }
        let conflicts = conflictLog.components(separatedBy: "Unable to simultaneously satisfy").count - 1
        var seen = Set<String>()
        let unique = issues.filter { seen.insert("\($0.tab)|\($0.kind)|\($0.detail)").inserted }
        if ProcessInfo.processInfo.environment["KRAB_RESIZE_REPORT"] != nil {
            print("=== Стресс длинного текста: нарушений \(unique.count), конфликтов \(conflicts)")
            unique.forEach { print("  \($0)") }
        }
        XCTAssertEqual(conflicts, 0, "конфликты констрейнтов (стресс): \(conflicts)")
        XCTAssertTrue(unique.isEmpty, "нарушения при длинном тексте (\(unique.count)):\n" + unique.prefix(40).map(\.description).joined(separator: "\n"))
    }

    /// Перенесённая длинная подпись получает высоту нескольких строк (а не обрезается по высоте).
    func test_wrapLongLabels_wrapsAndGrowsInHeight() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 400))
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.translatesAutoresizingMaskIntoConstraints = false
        let label = NSTextField(labelWithString: String(repeating: "длинное пояснение настройки ", count: 12))
        stack.addArrangedSubview(label)
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.widthAnchor.constraint(equalToConstant: 320),
        ])
        KrabEarTheme.wrapLongLabels(in: root)
        root.layoutSubtreeIfNeeded()
        root.layoutSubtreeIfNeeded()
        let oneLine = label.font!.boundingRectForFont.height
        XCTAssertLessThanOrEqual(label.alignmentRect(forFrame: label.frame).width, 320.5, "подпись не шире стека")
        XCTAssertGreaterThan(label.frame.height, oneLine * 2.5, "перенос по словам: высота \(label.frame.height)")
    }

    /// Поле ключа Voice Gateway обязано быть на экране (владелец не нашёл, где его вводить).
    /// После #2252 ключ вводится в карточке подключения вкладки «Разговор с AI»; старая строка
    /// на «Live перевод» скрыта, вместо неё — кнопка-ссылка (не шире раздела на 640).
    func test_voiceGatewayKeyField_isReachableOnScreen() {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        let verbose = ProcessInfo.processInfo.environment["KRAB_RESIZE_REPORT"] != nil
        for w: CGFloat in [860, 640] {
            window.setContentSize(NSSize(width: w, height: 820))
            var found: [String] = []
            for tab in Tab.ordered {
                controller.selectPanelTab(tab)
                expandAllSections(in: controller)
                window.contentView?.layoutSubtreeIfNeeded()
                window.contentView?.layoutSubtreeIfNeeded()
                guard let root = controller.mainTabView.selectedTabViewItem?.view else { continue }
                let field = controller.voiceGatewayConnectionView.keyField
                if field.window != nil, isEffectivelyVisible(field, under: root), field.bounds.width > 40 {
                    found.append(tab.title)
                }
                // Ссылка на «Live перевод»: длинная надпись на .rounded не выходит за раздел.
                if tab == .liveTranslation {
                    let link = descendants(root).compactMap { $0 as? NSButton }
                        .first { $0.title.contains("Разговор с AI") && $0.action == #selector(HistoryPanelController.selectVoiceGatewayCardTab) }
                    XCTAssertNotNil(link, "нет ссылки на карточку ключа на «Live перевод»")
                    // Свёрнутая секция (состояние из UserDefaults) не должна молча пропускать
                    // проверку (нит гейта): секции раскрыты, ссылка обязана быть видна.
                    if let link { XCTAssertTrue(isEffectivelyVisible(link, under: root), "ссылка скрыта на \(Int(w))") }
                    if let link, isEffectivelyVisible(link, under: root) {
                        let f = link.convert(link.bounds, to: root)
                        XCTAssertLessThanOrEqual(f.maxX, root.bounds.width + 1, "ссылка за границей на \(Int(w)): \(f.maxX)")
                    }
                }
            }
            if verbose { print("=== Поле ключа Voice Gateway видно (\(Int(w))): \(found)") }
            XCTAssertEqual(found, [Tab.conversation.title], "поле ключа — в «Разговор с AI» и только там (\(Int(w)))")
        }
    }
}
