import AppKit

// MARK: - Автодополнение поля поиска истории (недавние запросы из backend)
//
// Backend пишет каждый выполненный поиск через record_search в
// HistoryService.handle_search_history (после privacy-гейта), а
// get_recent_searches их отдаёт. Здесь — только чтение и показ:
//
//   1. setupSearchSuggestionsMenu() ставит searchMenuTemplate с recents-тегами,
//      чтобы у NSSearchField появился штатный выпадающий список «недавние поиски».
//   2. refreshSearchSuggestions() подтягивает запросы из backend и кладёт в
//      searchField.recentSearches (off-main-thread IPC по паттерну AGENT-3,
//      установка свойства — на main).
//
// Когда пользователь выбирает запись из recents-меню, NSSearchField сам ставит
// stringValue и шлёт свой action (#selector(onSearch)) → поиск выполняется без
// дополнительной проводки. recentSearches наполняем вручную из backend (а не
// через recentsAutosaveName), чтобы показывать запросы из всех сессий.
extension HistoryPanelController {

    /// Теги пунктов recents-меню NSSearchField (стабильные константы AppKit;
    /// именованы локально, чтобы не зависеть от различий имён символов в Swift).
    private enum RecentsTag {
        static let title = 1000      // NSSearchFieldRecentsTitleMenuItemTag
        static let recents = 1001    // NSSearchFieldRecentsMenuItemTag
        static let clear = 1002      // NSSearchFieldClearRecentsMenuItemTag
        static let noRecents = 1003  // NSSearchFieldNoRecentsMenuItemTag
    }

    /// Настраивает выпадающее меню «недавние поиски» на главном поле истории.
    /// Вызывать один раз при сборке панели (после конфигурации searchField).
    func setupSearchSuggestionsMenu() {
        let menu = NSMenu()

        let title = NSMenuItem(title: "Недавние поиски", action: nil, keyEquivalent: "")
        title.tag = RecentsTag.title
        menu.addItem(title)

        let recents = NSMenuItem(title: "Recents", action: nil, keyEquivalent: "")
        recents.tag = RecentsTag.recents
        menu.addItem(recents)

        menu.addItem(NSMenuItem.separator())

        let clear = NSMenuItem(title: "Очистить недавние", action: nil, keyEquivalent: "")
        clear.tag = RecentsTag.clear
        menu.addItem(clear)

        let noRecents = NSMenuItem(title: "Нет недавних поисков", action: nil, keyEquivalent: "")
        noRecents.tag = RecentsTag.noRecents
        menu.addItem(noRecents)

        searchField.searchMenuTemplate = menu
        // Локальный автосейв не нужен — recentSearches наполняем из backend.
        searchField.recentsAutosaveName = nil
    }

    /// Подтягивает недавние поисковые запросы из backend в recents-меню.
    /// - Parameter prepending: только что выполненный запрос — кладётся в начало
    ///   оптимистично (чтобы появился сразу, не дожидаясь персиста record_search).
    func refreshSearchSuggestions(prepending newQuery: String? = nil) {
        let ipcClient = self.ipcClient
        let optimistic = newQuery?.trimmingCharacters(in: .whitespacesAndNewlines)

        DispatchQueue.global(qos: .utility).async { [weak self] in
            let response = try? ipcClient.call(method: "get_recent_searches", params: ["limit": 12])
            let result = (response?["result"] as? [String: Any]) ?? [:]
            let searches = (result["searches"] as? [[String: Any]]) ?? []

            var seen = Set<String>()
            var queries: [String] = []

            func append(_ raw: String?) {
                guard let q = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !q.isEmpty else { return }
                if seen.insert(q.lowercased()).inserted {
                    queries.append(q)
                }
            }

            append(optimistic)
            for entry in searches {
                append(entry["query"] as? String)
            }

            // NSSearchField показывает максимум ~10 недавних; ограничим аккуратно.
            let limited = Array(queries.prefix(10))
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.searchField.recentSearches = limited
            }
        }
    }
}
