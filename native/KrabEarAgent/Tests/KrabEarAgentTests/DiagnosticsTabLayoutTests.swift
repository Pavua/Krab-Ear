import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class DiagnosticsTabLayoutTests: XCTestCase {
    /// Карточка «Журнал ошибок» обязана занимать ширину вкладки (раньше схлопывалась в ~170 pt).
    func testErrorLogCardFillsTabWidth() {
        let vc = DiagnosticsTabViewController(ipcClient: IPCClient(socketPath: "/nonexistent.sock"))
        vc.loadView()
        let root = vc.view
        root.frame = NSRect(x: 0, y: 0, width: 1000, height: 600)
        root.layoutSubtreeIfNeeded()
        let cards = Self.descendants(of: root).compactMap { $0 as? ThemeCardView }
        XCTAssertFalse(cards.isEmpty)
        for card in cards {
            XCTAssertGreaterThan(card.frame.width, 800, "карточка схлопнулась: \(card.frame.width) pt")
        }
    }

    private static func descendants(of view: NSView) -> [NSView] {
        view.subviews + view.subviews.flatMap { descendants(of: $0) }
    }
}
