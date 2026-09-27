/*
 Тесты строки статуса истории при незавершённом восстановлении (A5.2b2, M1).

 Контекст: backend при незавершённом restore отдаёт журналы как смесь состояний
 до/после снимка и сигналит об этом полем `restore_pending`. Агент обязан
 показать предупреждение, но НЕ прятать данные: просмотр истории посреди работы
 важнее предупреждения о возможной неполноте.

 Логика живёт в pure static-хелперере, потому что HistoryPanelController
 нельзя инстанцировать в headless-тестах (см. шапку
 HistoryPanelAnalyticsTests) — так тестируется весь приоритет текстов.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryRestorePendingSignalTests: XCTestCase {
    /// Смесь состояний не должна выглядеть как «записи исчезли» — в статусе
    /// появляется явное предупреждение вместо обычного счётчика.
    func test_pendingReplacesNormalCounters() {
        let text = HistoryPanelController.historyStatusText(
            itemCount: 120, hasMore: false, restorePending: true
        )
        XCTAssertTrue(text.contains("Восстановление не завершено"), "текст: \(text)")
        XCTAssertFalse(text.contains("Показаны все"), "при pending обычный счётчик врёт: \(text)")
        XCTAssertFalse(text.contains("Показано:"), "при pending обычный счётчик врёт: \(text)")
    }

    /// Признак сильнее любого состояния списка: даже пустая история при pending
    /// должна предупреждать (иначе «История пуста» выглядит как потеря данных).
    func test_pendingWinsOverEmptyState() {
        let text = HistoryPanelController.historyStatusText(
            itemCount: 0, hasMore: false, restorePending: true
        )
        XCTAssertTrue(text.contains("Восстановление не завершено"), "текст: \(text)")
        XCTAssertFalse(text.contains("История пуста"), "текст: \(text)")
    }

    /// Обычные состояния не должны пострадать от новой ветки.
    func test_normalStatesUnchanged() {
        XCTAssertEqual(
            HistoryPanelController.historyStatusText(itemCount: 0, hasMore: false, restorePending: false),
            "История пуста"
        )
        XCTAssertEqual(
            HistoryPanelController.historyStatusText(itemCount: 1, hasMore: false, restorePending: false),
            "Показаны все: 1"
        )
        XCTAssertEqual(
            HistoryPanelController.historyStatusText(itemCount: 1, hasMore: true, restorePending: false),
            "Показано: 1 (есть ещё)"
        )
    }

    /// Возврат к нормальному состоянию (например после докачки при следующей
    /// загрузке страницы) снимает предупреждение.
    func test_clearingPendingRestoresNormalText() {
        let pending = HistoryPanelController.historyStatusText(
            itemCount: 5, hasMore: false, restorePending: true
        )
        XCTAssertTrue(pending.contains("Восстановление не завершено"))

        let normal = HistoryPanelController.historyStatusText(
            itemCount: 5, hasMore: false, restorePending: false
        )
        XCTAssertEqual(normal, "Показаны все: 5")
    }
}
