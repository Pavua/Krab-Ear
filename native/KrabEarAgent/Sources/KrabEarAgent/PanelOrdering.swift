/*
 PanelOrdering — единая граница команд, меняющих видимость служебных NSPanel.

 Рабочая реализация передаёт команды AppKit без изменений. Unit-тесты подменяют
 её записывающим no-op, поэтому могут проверять layout и логику HUD-панелей,
 не выводя окна поверх рабочего стола пользователя.
*/

import AppKit

protocol PanelOrdering: Sendable {
    @MainActor func orderFront(_ panel: NSPanel)
    @MainActor func orderFrontRegardless(_ panel: NSPanel)
    @MainActor func orderOut(_ panel: NSPanel)
}

/// Единственная production-реализация: сохраняет исходные вызовы AppKit 1-в-1.
struct AppKitPanelOrdering: PanelOrdering {
    @MainActor func orderFront(_ panel: NSPanel) {
        panel.orderFront(nil)
    }

    @MainActor func orderFrontRegardless(_ panel: NSPanel) {
        panel.orderFrontRegardless()
    }

    @MainActor func orderOut(_ panel: NSPanel) {
        panel.orderOut(nil)
    }
}
