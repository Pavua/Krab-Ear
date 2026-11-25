import AppKit
import XCTest
@testable import KrabEarAgent

/// Редизайн «Истории» (PR 4a): фильтры — пары «подпись + список» в переносящемся ряду,
/// быстрые фильтры переносятся рядами, подписи по-русски, значения по индексу не сдвинуты.
@MainActor
final class HistoryFiltersLayoutTests: XCTestCase {

    // MARK: - FlowRowView

    private func box(_ w: CGFloat, _ h: CGFloat) -> NSView {
        let v = NSView()
        v.translatesAutoresizingMaskIntoConstraints = false
        v.widthAnchor.constraint(equalToConstant: w).isActive = true
        v.heightAnchor.constraint(equalToConstant: h).isActive = true
        return v
    }

    func test_flow_wrapsWholeItems_andCentersOnLine() {
        let flow = FlowRowView()
        flow.spacing = 10
        flow.lineSpacing = 4
        for (w, h) in [(100, 20), (100, 30), (100, 20)] as [(CGFloat, CGFloat)] { flow.addItem(box(w, h)) }
        // 250: две в первой строке (100+10+100), третья переносится.
        let a = flow.arrangement(for: 250)
        XCTAssertEqual(a.frames[0].origin, NSPoint(x: 0, y: 5), "по центру строки высотой 30")
        XCTAssertEqual(a.frames[1].origin, NSPoint(x: 110, y: 0))
        XCTAssertEqual(a.frames[2].origin, NSPoint(x: 0, y: 34))
        XCTAssertEqual(a.height, 54)
        // Широко — одна строка.
        XCTAssertEqual(flow.arrangement(for: 400).height, 30)
    }

    func test_flow_hiddenItemsTakeNoSpace_andWideItemIsClamped() {
        let flow = FlowRowView()
        flow.spacing = 10
        let hidden = box(100, 20)
        hidden.isHidden = true
        flow.addItem(hidden)
        flow.addItem(box(500, 20))
        let a = flow.arrangement(for: 200)
        XCTAssertEqual(a.frames[1], NSRect(x: 0, y: 0, width: 200, height: 20), "шире ряда — по ширине ряда")
        XCTAssertEqual(a.height, 20)
    }

    func test_labeledPair_labelIsSingleLineAndTruncates() {
        let pair = KrabEarTheme.labeledPair("Вставка:", NSPopUpButton())
        guard let label = pair.arrangedSubviews.first as? NSTextField else { return XCTFail() }
        KrabEarTheme.wrapLongLabels(in: pair)
        XCTAssertEqual(label.lineBreakMode, .byTruncatingTail)
        XCTAssertNotEqual(label.cell?.wraps, true, "перенос в горизонтальной паре лишил бы подпись ширины (#2267)")
    }

    // MARK: - Вкладка «История»

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

    private func lineCount(_ flow: FlowRowView) -> Int {
        Set(flow.items.filter { !$0.isHidden }.map { Int($0.frame.midY.rounded()) }).count
    }

    func test_filters_keepPairsTogether_andFitFewLines() {
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        controller.selectPanelTab(.history)
        controller.historyFiltersSection?.setExpanded(true, animated: false)
        // Было (AdaptiveRow столбиком): 13 строк фильтров и 8 строк быстрых фильтров на 860.
        for (width, maxFilterLines, maxQuickLines) in [(860.0, 3, 2), (640.0, 4, 3)] {
            window.setContentSize(NSSize(width: width, height: 820))
            window.contentView?.layoutSubtreeIfNeeded()
            window.contentView?.layoutSubtreeIfNeeded()
            let filters = controller.historyFilterFlow
            XCTAssertEqual(filters.items.count, 6)
            for case let pair as NSStackView in filters.items {
                guard pair.arrangedSubviews.count == 2 else { return XCTFail("пара из двух элементов") }
                let (label, control) = (pair.arrangedSubviews[0], pair.arrangedSubviews[1])
                XCTAssertEqual(label.frame.midY, control.frame.midY, accuracy: 2, "подпись на одной строке со своим списком")
                XCTAssertGreaterThan(label.frame.width, 20, "подпись видна (\(width))")
            }
            XCTAssertLessThanOrEqual(lineCount(filters), maxFilterLines, "фильтры на \(width): строк")
            XCTAssertLessThanOrEqual(lineCount(controller.historyQuickFilterFlow), maxQuickLines, "быстрые фильтры на \(width)")
            XCTAssertGreaterThan(filters.bounds.width, width * 0.5, "ряд фильтров растянут по карточке")
        }
    }

    func test_titles_areRussian_andIndexOrderPreserved() {
        let controller = makeController()
        _ = controller.window
        // Индексы читают selectedHistory*Filter / onHistoryDensityChanged — число пунктов прежнее.
        XCTAssertEqual(controller.historyPasteStatusFilter.numberOfItems, 3)
        XCTAssertEqual(controller.historyTranslationModeFilter.numberOfItems, 8)
        XCTAssertEqual(controller.historyTranslationStatusFilter.numberOfItems, 9)
        XCTAssertEqual(controller.historyActionItemsFilter.numberOfItems, 3)
        XCTAssertEqual(controller.historyDensitySelector.itemTitles, ["Обычная", "Компактная"])
        controller.historyTranslationStatusFilter.selectItem(at: 8)
        XCTAssertEqual(controller.selectedHistoryTranslationStatusFilter(), "translate_error")
        controller.historyTranslationModeFilter.selectItem(at: 7)
        XCTAssertEqual(controller.selectedHistoryTranslationModeFilter(), "bilingual_ru_es")
        controller.historyPasteStatusFilter.selectItem(at: 2)
        XCTAssertEqual(controller.selectedHistoryPasteStatusFilter(), "failed")
        XCTAssertFalse(controller.historyFocusModeButton.title.contains("ON"))
    }

    /// Офлайн-рендер вкладки с раскрытыми фильтрами (только при KRAB_RENDER_DIR).
    func test_renderHistoryFilters() throws {
        guard let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] else { return }
        let controller = makeController()
        guard let window = controller.window else { return XCTFail("нет окна") }
        controller.selectPanelTab(.history)
        controller.historyFiltersSection?.setExpanded(true, animated: false)
        for width in [860.0, 640.0] {
            window.appearance = NSAppearance(named: .aqua)
            window.setContentSize(NSSize(width: width, height: 820))
            window.contentView?.layoutSubtreeIfNeeded()
            window.contentView?.layoutSubtreeIfNeeded()
            guard let content = window.contentView,
                  let rep = content.bitmapImageRepForCachingDisplay(in: content.bounds) else { continue }
            content.cacheDisplay(in: content.bounds, to: rep)
            try rep.representation(using: .png, properties: [:])?
                .write(to: URL(fileURLWithPath: dir).appendingPathComponent("history_filters_\(Int(width)).png"))
        }
    }
}
