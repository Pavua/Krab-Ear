/*
 HistoryPanelController+ActionItemsFilter.swift

 Client-side фильтр истории по наличию action_items / decisions / questions.
 Backend `get_history_page` не поддерживает `has_action_items` query — filter
 применяется на стороне клиента над уже загруженной страницей.

 Зависит от полей `actionItems` / `decisions` / `questions` на HistoryItem
 (PR feat/history-item-action-items-fields #295).

 Не пересекается с +ActionItems.swift (UI для extract — PR #294); этот
 extension изолирован и независим от #294.
*/

import AppKit

extension HistoryPanelController {

    /// Handler для popup `historyActionItemsFilter`. Применяет client-side filter.
    ///
    /// `mode`:
    ///   0 = "Все" → reload from backend (полная страница)
    ///   1 = "Только с action items" → keep items where any of arrays non-empty
    ///   2 = "Только без" → keep items where все arrays пустые
    @objc func onActionItemsFilterChanged() {
        let mode = historyActionItemsFilter.indexOfSelectedItem
        if mode == 0 {
            // "Все" — refresh из backend (восстанавливает unfiltered set).
            loadInitial()
            return
        }
        items = HistoryPanelController.filterByActionItemsPresence(items: items, mode: mode)
        tableView.reloadData()
        updateHistoryStatusLabel()
        updateHistoryFiltersBadge()
    }

    /// Pure helper — фильтрует items по наличию action data.
    /// `mode`: 0 = passthrough, 1 = только с action data, 2 = только без.
    /// `nonisolated static` — тестируется без instance.
    nonisolated static func filterByActionItemsPresence(
        items: [HistoryItem],
        mode: Int
    ) -> [HistoryItem] {
        switch mode {
        case 1:
            return items.filter { item in
                !item.actionItems.isEmpty || !item.decisions.isEmpty || !item.questions.isEmpty
            }
        case 2:
            return items.filter { item in
                item.actionItems.isEmpty && item.decisions.isEmpty && item.questions.isEmpty
            }
        default:
            return items
        }
    }
}
