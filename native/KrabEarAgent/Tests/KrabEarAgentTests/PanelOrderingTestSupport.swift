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

    func orderFront(_ panel: NSPanel) {
        orderFrontCallCount += 1
    }

    func orderFrontRegardless(_ panel: NSPanel) {
        orderFrontRegardlessCallCount += 1
    }

    func orderOut(_ panel: NSPanel) {
        orderOutCallCount += 1
    }

    func reset() {
        orderFrontCallCount = 0
        orderFrontRegardlessCallCount = 0
        orderOutCallCount = 0
    }
}
