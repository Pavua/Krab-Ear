/*
 PanelOrderingTestSupport — безопасный двойник порядка окон для unit-тестов.

 Он записывает намерение показать или скрыть NSPanel, но никогда не передаёт
 команду AppKit. Благодаря этому тесты сохраняют реальную сборку и layout панели,
 не выводя служебные окна поверх рабочего стола пользователя.
*/

import AppKit
@testable import KrabEarAgent

@MainActor
final class RecordingPanelOrdering: PanelOrdering {
    private(set) var orderFrontCallCount = 0
    private(set) var orderFrontRegardlessCallCount = 0
    private(set) var orderOutCallCount = 0
    private var visiblePanels: Set<ObjectIdentifier> = []

    func orderFront(_ panel: NSPanel) {
        orderFrontCallCount += 1
        visiblePanels.insert(ObjectIdentifier(panel))
    }

    func orderFrontRegardless(_ panel: NSPanel) {
        orderFrontRegardlessCallCount += 1
        visiblePanels.insert(ObjectIdentifier(panel))
    }

    func orderOut(_ panel: NSPanel) {
        orderOutCallCount += 1
        visiblePanels.remove(ObjectIdentifier(panel))
    }

    func isVisible(_ panel: NSPanel) -> Bool {
        visiblePanels.contains(ObjectIdentifier(panel))
    }

    func reset() {
        orderFrontCallCount = 0
        orderFrontRegardlessCallCount = 0
        orderOutCallCount = 0
        visiblePanels.removeAll()
    }
}
