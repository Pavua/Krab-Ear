import AppKit
import XCTest
@testable import KrabEarAgent

/// Редизайн «Истории» (PR 4b): строка таблицы вмещает текст и ряд меток, метки по-русски,
/// прокрутка 5000 записей укладывается в бюджет.
@MainActor
final class HistoryRowsLayoutTests: XCTestCase {

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

    static func item(_ i: Int, text: String, paste: String = "ok", mode: String = "off",
                     confidence: Double? = 0.93) -> HistoryItem {
        var p: [String: Any] = [
            "id": "id-\(i)", "ts": "2026-11-25 10:\(String(format: "%02d", i % 60))",
            "text": text, "paste_status": paste, "translation_mode": mode,
        ]
        if let confidence { p["confidence"] = confidence }
        return HistoryItem(payload: p)!
    }

    static let samples: [HistoryItem] = [
        item(1, text: "Коротко."),
        item(2, text: "Средняя запись: напомнить завтра позвонить по поводу счёта и отправить документы.",
             paste: "failed", mode: "ru_to_es", confidence: 0.42),
        item(3, text: String(repeating: "Длинная диктовка о планах на неделю, встречах и задачах. ", count: 8),
             paste: "skipped", confidence: nil),
        item(4, text: "Ещё одна.", paste: "pending"),
    ]

    private func showHistory(_ controller: HistoryPanelController, items: [HistoryItem], width: CGFloat) -> NSWindow? {
        guard let window = controller.window else { return nil }
        controller.selectPanelTab(.history)
        window.setContentSize(NSSize(width: width, height: 820))
        controller.items = items
        controller.tableView.reloadData()
        window.contentView?.layoutSubtreeIfNeeded()
        window.contentView?.layoutSubtreeIfNeeded()
        // Ячейки таблицы создаются при отрисовке.
        if let content = window.contentView, let rep = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
            content.cacheDisplay(in: content.bounds, to: rep)
        }
        return window
    }

    func test_rowHeight_pureFunction() {
        // Короткий текст: одна строка + ряд меток + отступы.
        XCTAssertEqual(HistoryPanelController.historyRowHeight(
            textHeight: 18, lineHeight: 18, maxLines: 2, metaHeight: 20, translationHeight: nil), 48)
        // Длинный текст — не выше двух строк (дальше многоточие).
        XCTAssertEqual(HistoryPanelController.historyRowHeight(
            textHeight: 180, lineHeight: 18, maxLines: 2, metaHeight: 20, translationHeight: nil), 66)
        // Компактная: одна строка.
        XCTAssertEqual(HistoryPanelController.historyRowHeight(
            textHeight: 180, lineHeight: 16, maxLines: 1, metaHeight: 20, translationHeight: nil), 46)
        // Перевод добавляет свою высоту.
        XCTAssertEqual(HistoryPanelController.historyRowHeight(
            textHeight: 18, lineHeight: 18, maxLines: 2, metaHeight: 20, translationHeight: 17.2), 68)
        XCTAssertEqual(HistoryPanelController.pasteStatusTitle("failed"), "Не вставлено")
        XCTAssertEqual(HistoryPanelController.pasteStatusTitle("weird"), "weird")
    }

    /// Замер спеки PR 4b: 5000 записей — перезагрузка и прокрутка с отрисовкой видимой области.
    /// Бюджеты с большим запасом (CI-машина бывает загружена); цифры печатаются в лог.
    func test_scroll5000_withinBudget() {
        let controller = makeController()
        let texts = HistoryRowsLayoutTests.samples.map(\.text)
        let many = (0..<5000).map { i in
            Self.item(i, text: texts[i % texts.count], paste: ["ok", "failed", "skipped", "pending"][i % 4],
                      mode: i % 3 == 0 ? "ru_to_es" : "off", confidence: i % 5 == 0 ? nil : 0.7)
        }
        guard let window = controller.window else { return XCTFail("нет окна") }
        controller.selectPanelTab(.history)
        window.setContentSize(NSSize(width: 860, height: 820))
        window.contentView?.layoutSubtreeIfNeeded()

        let t0 = CFAbsoluteTimeGetCurrent()
        controller.items = many
        controller.tableView.reloadData()
        window.contentView?.layoutSubtreeIfNeeded()
        let reloadSec = CFAbsoluteTimeGetCurrent() - t0

        let scroll = controller.scrollView
        let clip = scroll.contentView
        let total = controller.tableView.bounds.height
        let steps = 60
        let t1 = CFAbsoluteTimeGetCurrent()
        for step in 0..<steps {
            let y = (total - clip.bounds.height) * CGFloat(step) / CGFloat(steps - 1)
            clip.scroll(to: NSPoint(x: 0, y: max(0, y)))
            scroll.reflectScrolledClipView(clip)
            if let rep = scroll.bitmapImageRepForCachingDisplay(in: scroll.bounds) {
                scroll.cacheDisplay(in: scroll.bounds, to: rep)
            }
        }
        let perFrameMs = (CFAbsoluteTimeGetCurrent() - t1) / Double(steps) * 1000
        print("[HistoryScroll5000] reload \(String(format: "%.3f", reloadSec)) с, кадр \(String(format: "%.1f", perFrameMs)) мс, высота таблицы \(Int(total)) pt")
        XCTAssertEqual(controller.tableView.numberOfRows, 5000)
        XCTAssertLessThan(reloadSec, 5, "перезагрузка 5000 записей")
        XCTAssertLessThan(perFrameMs, 200, "кадр прокрутки (с отрисовкой видимых строк)")
    }

    /// Офлайн-рендер таблицы с образцами (только при KRAB_RENDER_DIR).
    func test_renderHistoryRows() throws {
        guard let dir = ProcessInfo.processInfo.environment["KRAB_RENDER_DIR"] else { return }
        let controller = makeController()
        for width in [860.0, 640.0] {
            guard let window = showHistory(controller, items: Self.samples, width: width),
                  let content = window.contentView else { continue }
            controller.scrollView.layoutSubtreeIfNeeded()
            let target = controller.scrollView
            guard let rep = target.bitmapImageRepForCachingDisplay(in: target.bounds) else { continue }
            target.cacheDisplay(in: target.bounds, to: rep)
            _ = content
            try rep.representation(using: .png, properties: [:])?
                .write(to: URL(fileURLWithPath: dir).appendingPathComponent("history_rows_\(Int(width)).png"))
        }
    }

    func test_rowFitsTextAndMetaRow_noClipping_noEmptyTail() {
        let controller = makeController()
        for width in [860.0, 640.0] {
            guard showHistory(controller, items: Self.samples, width: width) != nil else { return XCTFail("нет окна") }
            let table = controller.tableView
            let column = table.column(withIdentifier: NSUserInterfaceItemIdentifier("text"))
            for row in 0..<Self.samples.count {
                guard let cell = table.view(atColumn: column, row: row, makeIfNecessary: true) as? HistoryItemCellView else {
                    return XCTFail("нет ячейки \(row)")
                }
                cell.layoutSubtreeIfNeeded()
                let rowHeight = table.rect(ofRow: row).height
                let meta = cell.metaStack.convert(cell.metaStack.bounds, to: cell)
                let text = cell.transcriptLabel.convert(cell.transcriptLabel.bounds, to: cell)
                XCTAssertGreaterThanOrEqual(meta.minY, -0.5, "ряд меток строки \(row) не обрезан снизу (\(width))")
                XCTAssertLessThanOrEqual(meta.maxY, cell.bounds.height + 0.5, "ряд меток строки \(row) в пределах (\(width))")
                // Видимая часть текста (до maximumNumberOfLines строк) помещается по высоте.
                let label = cell.transcriptLabel
                let need = label.cell?.cellSize(forBounds: NSRect(x: 0, y: 0, width: label.bounds.width, height: 10_000)).height ?? 0
                let lineHeight = NSLayoutManager().defaultLineHeight(for: label.font ?? KrabEarTheme.Typography.body)
                let shown = label.maximumNumberOfLines > 0 ? min(need, lineHeight * CGFloat(label.maximumNumberOfLines) + 2) : need
                XCTAssertLessThanOrEqual(shown, text.height + 2, "текст строки \(row) не срезан по высоте (\(width)): нужно \(shown), есть \(text.height)")
                // Пустой хвост: строка не выше содержимого больше чем на одну строку текста.
                let content = text.height + meta.height + 24
                XCTAssertLessThanOrEqual(rowHeight, content + 24, "строка \(row) без лишней пустоты (\(width)): \(rowHeight) vs \(content)")
            }
        }
    }
}
