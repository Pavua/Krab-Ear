/*
 HistoryPanelController+History.swift
 Расширение: обработчики действий истории, пагинация, экспорт/импорт,
 фильтрация, вспомогательные методы отображения.
*/

import AppKit
import Foundation
import UniformTypeIdentifiers

extension HistoryPanelController {

    // MARK: - Pagination

    @objc func onLoadMore() {
        guard let cursor = nextCursor else {
            updateHistoryStatusLabel()
            return
        }
        let pageSize = settingsProvider().historyPageSize
        if currentQuery.isEmpty {
            appendPageAsync(
                method: "get_history_page",
                params: buildHistoryQueryParams(cursor: cursor, limit: pageSize),
                completion: nil
            )
        } else {
            var params = buildHistoryQueryParams(cursor: cursor, limit: pageSize)
            params["query"] = currentQuery
            appendPageAsync(method: "search_history", params: params, completion: nil)
        }
    }

    /// Загружает все доступные страницы по chain. Раньше блокировал main thread
    /// в while-loop с sync IPC (до 120 sync calls × до 5 сек каждый = AppHang).
    /// Теперь — recursive async chain: каждая следующая страница load'ится после
    /// completion предыдущей. UI остаётся responsive; user видит progressive
    /// заполнение таблицы.
    @objc func onLoadAll() {
        loadAllPagesRecursive(remaining: 120)
    }

    private func loadAllPagesRecursive(remaining: Int) {
        guard let cursor = nextCursor else { return }
        guard remaining > 0 else {
            showInfoAlert(
                title: "История",
                body: "Загружен лимит страниц за один проход. Сузьте фильтр или повторите действие."
            )
            return
        }
        let pageSize = settingsProvider().historyPageSize
        let method: String
        let params: [String: Any]
        if currentQuery.isEmpty {
            method = "get_history_page"
            params = buildHistoryQueryParams(cursor: cursor, limit: pageSize)
        } else {
            var p = buildHistoryQueryParams(cursor: cursor, limit: pageSize)
            p["query"] = currentQuery
            method = "search_history"
            params = p
        }
        appendPageAsync(method: method, params: params) { [weak self] _ in
            guard let self = self else { return }
            // Если есть ещё страницы — продолжаем chain; остановка по cursor=nil или counter=0.
            if self.nextCursor != nil {
                self.loadAllPagesRecursive(remaining: remaining - 1)
            }
        }
    }

    @objc func onJumpToLatest() {
        guard !items.isEmpty else { return }
        tableView.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        tableView.scrollRowToVisible(0)
    }

    // MARK: - Copy / Paste actions

    @objc func onCopy() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let text = items[selected].text
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc func onPasteSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        onPasteHistoryItem(items[selected])
    }

    @objc func onCopyOriginal() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let text = item.sourceText.isEmpty ? item.text : item.sourceText
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc func onCopyTranslation() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let text = item.translatedText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            showInfoAlert(title: "Копирование перевода", body: "Для этой записи перевод отсутствует.")
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    // MARK: - Export / Import

    @objc func onExportHistory() {
        guard !items.isEmpty else {
            showInfoAlert(title: "Экспорт истории", body: "История пуста, экспортировать нечего.")
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suggestedName = "krab_ear_history_\(formatter.string(from: Date())).md"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.plainText]
        panel.title = "Сохранить экспорт истории"
        panel.prompt = "Сохранить"

        guard panel.runModal() == .OK, let outputURL = panel.url else { return }

        let content = buildHistoryMarkdownExport()
        do {
            try content.write(to: outputURL, atomically: true, encoding: .utf8)
            showInfoAlert(
                title: "Экспорт истории",
                body: "Сохранено записей: \(items.count)\n\(outputURL.path)"
            )
        } catch {
            showInfoAlert(
                title: "Экспорт истории",
                body: "Не удалось сохранить файл: \(error.localizedDescription)"
            )
        }
        // Также сохраняем копию через IPC (export_history) в transcripts/
        if let ipcResponse = try? ipcClient.call(
            method: "export_history",
            params: ["format": "md", "save_to_file": true]
        ), let ipcResult = ipcResponse["result"] as? [String: Any],
           let serverPath = ipcResult["path"] as? String {
            notificationService.notify(title: "Krab Ear", body: "Серверная копия: \(serverPath)")
        }
    }

    @objc func onExportHistoryNdjson() {
        guard !items.isEmpty else {
            showInfoAlert(title: "Экспорт NDJSON", body: "История пуста, экспортировать нечего.")
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let suggestedName = "krab_ear_history_\(formatter.string(from: Date())).ndjson"

        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = suggestedName
        panel.allowedContentTypes = [.json]
        panel.title = "Сохранить экспорт NDJSON"
        panel.prompt = "Сохранить"

        guard panel.runModal() == .OK, let outputURL = panel.url else { return }
        let content = buildHistoryNdjsonExport()
        do {
            try content.write(to: outputURL, atomically: true, encoding: .utf8)
            showInfoAlert(
                title: "Экспорт NDJSON",
                body: "Сохранено записей: \(items.count)\n\(outputURL.path)"
            )
        } catch {
            showInfoAlert(
                title: "Экспорт NDJSON",
                body: "Не удалось сохранить файл: \(error.localizedDescription)"
            )
        }
    }

    @objc func onImportHistoryNdjson() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.title = "Выберите NDJSON-файл истории"
        panel.prompt = "Импортировать"

        guard panel.runModal() == .OK, let inputURL = panel.url else { return }
        let inputPath = inputURL.path

        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "import_history_ndjson",
                    params: ["path": inputPath]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Импорт NDJSON", body: "Ошибка при импорте файла.")
                }
                return
            }

            let imported = (result["imported"] as? Int) ?? 0
            let skipped = (result["skipped"] as? Int) ?? 0
            let errors = (result["errors"] as? Int) ?? 0
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loadInitial()
                self.showInfoAlert(
                    title: "Импорт NDJSON",
                    body: "Импортировано: \(imported)\nПропущено дублей: \(skipped)\nОшибок: \(errors)"
                )
            }
        }
    }

    // MARK: - Retranslate / Summarize

    @objc func onRetranslateSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }

        let item = items[selected]
        let sourceText = item.sourceText.isEmpty ? item.text : item.sourceText
        let cleanSource = sourceText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanSource.isEmpty else {
            showInfoAlert(title: "Повторить перевод", body: "У выбранной записи нет исходного текста.")
            return
        }

        let targetMode: String
        switch translationSelector.indexOfSelectedItem {
        case 1:
            targetMode = "ru_to_es"
        case 2:
            targetMode = "es_to_ru"
        case 3:
            targetMode = "en_to_ru"
        case 4:
            targetMode = "auto"
        case 5:
            targetMode = "bilingual_ru_es"
        case 6:
            targetMode = "auto_to_ru"
        default:
            targetMode = "off"
        }

        guard targetMode != "off" else {
            showInfoAlert(title: "Повторить перевод", body: "Выберите режим перевода отличный от Off.")
            return
        }

        let providerSettings = settingsProvider()
        let translationStyle = providerSettings.translationStyle
        let networkMode = providerSettings.networkMode
        let shouldPasteTranslated = translateAndPasteButton.state == .on

        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let translateResponse = try? ipcClient.call(
                    method: "translate_text",
                    params: [
                        "text": cleanSource,
                        "translation_mode": targetMode,
                        "translation_style": translationStyle,
                        "network_mode": networkMode,
                    ]
                ),
                let translateResult = translateResponse["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Повторить перевод", body: "Не удалось выполнить перевод.")
                }
                return
            }

            let status = (translateResult["status"] as? String) ?? "unknown"
            let translatedText = ((translateResult["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if status != "ok" || translatedText.isEmpty {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Повторить перевод", body: "Перевод недоступен: \(status).")
                }
                return
            }

            let newText = shouldPasteTranslated ? translatedText : cleanSource
            let engine = (translateResult["engine"] as? String) ?? ""
            let sourceLang = (translateResult["source_lang"] as? String) ?? ""
            let targetLang = (translateResult["target_lang"] as? String) ?? ""

            let _ = try? ipcClient.call(
                method: "add_history_item",
                params: [
                    "text": newText,
                    "paste_status": "failed",
                    "source_text": cleanSource,
                    "translated_text": translatedText,
                    "translation_mode": targetMode,
                    "translation_status": status,
                    "translation_engine": engine,
                    "source_lang": sourceLang,
                    "target_lang": targetLang,
                ]
            )

            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loadInitial()
                self.showInfoAlert(
                    title: "Повторить перевод",
                    body: "Готово. Создана новая запись истории с обновлённым переводом."
                )
            }
        }
    }

    @objc func onSummarizeSelected() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let source = item.sourceText.isEmpty ? item.text : item.sourceText
        let cleanSource = source.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanSource.isEmpty else {
            showInfoAlert(title: "Summary", body: "У выбранной записи пустой текст.")
            return
        }

        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(
                    method: "summarize_text",
                    params: [
                        "text": cleanSource,
                        "mode": "summary_short",
                        "max_points": 4,
                    ]
                ),
                let result = response["result"] as? [String: Any]
            else {
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Summary", body: "Не удалось построить summary.")
                }
                return
            }
            let summary = (result["summary"] as? String) ?? ""
            let bullets = (result["bullets"] as? [String]) ?? []
            let bulletText = bullets.isEmpty ? "- (нет пунктов)" : bullets.prefix(6).map { "- \($0)" }.joined(separator: "\n")
            let text = """
            \(summary.isEmpty ? "—" : summary)

            Пункты:
            \(bulletText)
            """
            DispatchQueue.main.async {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                self?.showInfoAlert(title: "Summary", body: "\(text)\n\n(Скопировано в буфер)")
            }
        }
    }

    // MARK: - Delete / Compact / Transcripts

    @objc func onDelete() {
        let selected = tableView.selectedRow
        guard selected >= 0, selected < items.count else { return }
        let item = items[selected]
        let itemID = item.id

        // UI update sync — пользователь видит мгновенный эффект.
        items.remove(at: selected)
        tableView.reloadData()

        // IPC удаление — на background, не блокирует UI.
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? ipcClient.call(method: "delete_history_item", params: ["id": itemID])
        }
    }

    @objc func onCompact() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipcClient.call(method: "compact_history", params: [:])
            if let result = response?["result"] as? [String: Any] {
                let reclaimed = (result["reclaimed_bytes"] as? Int) ?? 0
                let beforeBytes = (result["before_total_bytes"] as? Int) ?? 0
                let afterBytes = (result["after_total_bytes"] as? Int) ?? 0
                let beforeActive = (result["before_active_count"] as? Int) ?? 0
                let afterActive = (result["after_active_count"] as? Int) ?? 0
                let body = """
                Активных записей: \(beforeActive) -> \(afterActive)
                Размер: \(self?.formatBytes(beforeBytes) ?? "?") -> \(self?.formatBytes(afterBytes) ?? "?")
                Освобождено: \(self?.formatBytes(max(0, reclaimed)) ?? "?")
                """
                DispatchQueue.main.async {
                    self?.showInfoAlert(title: "Оптимизация истории", body: body)
                }
            }
            DispatchQueue.main.async {
                self?.loadInitial()
            }
        }
    }

    @objc func onOpenTranscripts() {
        let transcriptsPath = NSString(string: "~/Library/Application Support/KrabEar/transcripts").expandingTildeInPath
        let url = URL(fileURLWithPath: transcriptsPath, isDirectory: true)
        // Create directory if needed
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
    }

    // MARK: - Load / Append

    func loadInitial() {
        items = []
        nextCursor = nil
        tableView.reloadData()
        jumpToLatestButton.isEnabled = false
        updateHistoryStatusLabel()
        updateDictationHistoryPreview()
        updateHistoryFiltersBadge()
        updateHistoryPreviewCard()

        let pageSize = settingsProvider().historyPageSize
        let hadActiveFilters = hasActiveHistoryFiltersOrQuery()
        let method: String
        let params: [String: Any]
        if currentQuery.isEmpty {
            method = "get_history_page"
            params = buildHistoryQueryParams(cursor: NSNull(), limit: pageSize)
        } else {
            var p = buildHistoryQueryParams(cursor: NSNull(), limit: pageSize)
            p["query"] = currentQuery
            method = "search_history"
            params = p
        }

        // recoverHistoryIfFiltersHideAllRows должен сработать ПОСЛЕ загрузки
        // первой страницы (раньше работал sync — race condition был скрыт).
        // Теперь зовём из completion callback после async appendPage.
        appendPageAsync(method: method, params: params) { [weak self] _ in
            self?.recoverHistoryIfFiltersHideAllRows(limit: pageSize, hadActiveFilters: hadActiveFilters)
        }
    }

    /// Async-wrap для get_history_page / search_history IPC. Блокирующий sync IPC
    /// был последним unmitigated AppHang trigger в hot path (loadInitial, onLoadMore,
    /// onLoadAll вызывают его при каждой filter change / pagination). После этого
    /// PR — все основные UI flow non-blocking.
    ///
    /// `completion` вызывается на main thread с success-flag. Nil completion = no-op.
    func appendPageAsync(method: String, params: [String: Any], completion: (@MainActor @Sendable (Bool) -> Void)?) {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        nonisolated(unsafe) let methodCopy = method
        nonisolated(unsafe) let paramsCopy = params

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard
                let response = try? ipcClient.call(method: methodCopy, params: paramsCopy),
                let result = response["result"] as? [String: Any],
                let rawItems = result["items"] as? [[String: Any]]
            else {
                DispatchQueue.main.async {
                    completion?(false)
                }
                return
            }

            let mapped = rawItems.compactMap(HistoryItem.init(payload:))
            let newCursor = result["next_cursor"] as? String

            DispatchQueue.main.async {
                guard let self = self else {
                    completion?(false)
                    return
                }
                self.items.append(contentsOf: mapped)
                self.nextCursor = newCursor
                self.loadMoreButton.isEnabled = (newCursor != nil)
                self.loadAllButton.isEnabled = (newCursor != nil)
                self.updateLoadMoreButtonCaption()
                self.updateHistoryStatusLabel()
                self.updateDictationHistoryPreview()
                self.tableView.reloadData()
                self.updateHistoryPreviewCard()
                self.jumpToLatestButton.isEnabled = !self.items.isEmpty
                completion?(true)
            }
        }
    }

    /// Sync wrapper deprecated — оставлен для recoverHistoryIfFiltersHideAllRows
    /// (control-flow caller). Все hot-path UI callers переведены на appendPageAsync.
    /// Удалить при следующей итерации после verify recovery работает async.
    func appendPage(method: String, params: [String: Any]) {
        guard let response = try? ipcClient.call(method: method, params: params),
              let result = response["result"] as? [String: Any],
              let rawItems = result["items"] as? [[String: Any]] else {
            return
        }
        let mapped = rawItems.compactMap(HistoryItem.init(payload:))
        items.append(contentsOf: mapped)
        nextCursor = result["next_cursor"] as? String
        loadMoreButton.isEnabled = (nextCursor != nil)
        loadAllButton.isEnabled = (nextCursor != nil)
        updateLoadMoreButtonCaption()
        updateHistoryStatusLabel()
        updateDictationHistoryPreview()
        tableView.reloadData()
        updateHistoryPreviewCard()
        jumpToLatestButton.isEnabled = !items.isEmpty
    }

    // MARK: - Filter helpers

    func hasActiveHistoryFiltersOrQuery() -> Bool {
        if !currentQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        if historyPasteStatusFilter.indexOfSelectedItem > 0 {
            return true
        }
        if historyTranslationModeFilter.indexOfSelectedItem > 0 {
            return true
        }
        if historyTranslationStatusFilter.indexOfSelectedItem > 0 {
            return true
        }
        if !historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        if !historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
        return false
    }

    func recoverHistoryIfFiltersHideAllRows(limit: Int, hadActiveFilters: Bool) {
        guard hadActiveFilters else { return }
        guard items.isEmpty else { return }
        guard !isRecoveringHistoryFromFilters else { return }
        guard let stats = fetchHistoryStats(), stats.activeCount > 0 else { return }

        // UX-страховка: если записи есть, но пользователь их "скрыл" фильтрами,
        // автоматически показываем последние элементы вместо пустой таблицы.
        isRecoveringHistoryFromFilters = true
        defer { isRecoveringHistoryFromFilters = false }

        currentQuery = ""
        searchField.stringValue = ""
        historyPasteStatusFilter.selectItem(at: 0)
        historyTranslationModeFilter.selectItem(at: 0)
        historyTranslationStatusFilter.selectItem(at: 0)
        historyFromDateField.stringValue = ""
        historyToDateField.stringValue = ""

        items = []
        nextCursor = nil
        tableView.reloadData()
        appendPage(method: "get_history_page", params: buildHistoryQueryParams(cursor: NSNull(), limit: limit))
        if !items.isEmpty {
            historyStatusLabel.stringValue = "Фильтры скрывали записи. Показаны последние \(items.count)."
        }
        updateDictationHistoryPreview()
    }

    // MARK: - Focus mode / Text density

    func applyHistoryFocusMode(_ enabled: Bool) {
        // Collapse or expand all history collapsible sections
        historyFiltersSection?.setExpanded(!enabled, animated: true)
        historyAdvancedSection?.setExpanded(!enabled, animated: true)
        historyImportSection?.setExpanded(!enabled, animated: true)

        // Disable disclosure buttons in focus mode so user can't expand them
        historyFiltersSection?.disclosureButton.isEnabled = !enabled
        historyAdvancedSection?.disclosureButton.isEnabled = !enabled
        historyImportSection?.disclosureButton.isEnabled = !enabled

        historyScrollMinHeightConstraint?.constant = enabled ? 240 : 180
        historyFocusModeButton.title = enabled ? "Фокус истории: ON" : "Фокус истории: OFF"
    }

    func applyHistoryTextDensity(_ density: String) {
        let compact = (density == "compact")
        tableView.rowHeight = compact ? 24 : 28
        historyDensitySelector.selectItem(at: compact ? 1 : 0)
    }

    func historyBodyFont() -> NSFont {
        return settingsProvider().historyTextDensity == "compact"
            ? .systemFont(ofSize: 12)
            : .systemFont(ofSize: NSFont.systemFontSize)
    }

    func historyMinRowHeight() -> CGFloat {
        return settingsProvider().historyTextDensity == "compact" ? 24 : 28
    }

    func updateLoadMoreButtonCaption() {
        let pageSize = settingsProvider().historyPageSize
        loadMoreButton.title = "Показать ещё (\(pageSize))"
        loadAllButton.title = "Загрузить всё"
    }

    func normalizePageSize(_ value: Int) -> Int {
        if value <= 25 { return 25 }
        if value <= 50 { return 50 }
        if value <= 100 { return 100 }
        return 200
    }

    // MARK: - Status labels

    /// Обновляет status + overview labels. IPC-запросы (`get_history_stats`,
    /// `get_history_overview`) делаются на background, UI update — на main.
    /// Без async wrap эта функция блокировала main thread на каждом
    /// `loadInitial`/`appendPage`/filter change → AppHang ≥2000ms (KRAB-EAR-AGENT-3).
    func updateHistoryStatusLabel() {
        // Установить базовый текст немедленно (пока IPC stats fetch'атся).
        let baseText: String
        if items.isEmpty {
            baseText = "История пуста"
        } else if nextCursor == nil {
            baseText = "Показаны все: \(items.count)"
        } else {
            baseText = "Показано: \(items.count) (есть ещё)"
        }
        historyStatusLabel.stringValue = baseText

        // Background: получить stats + overview, потом update labels на main.
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // Inline fetch stats (re-implemented because fetchHistoryStats() — instance method).
            var statsSuffix = ""
            if let response = try? ipcClient.call(method: "get_history_stats", params: [:]),
               let result = response["result"] as? [String: Any] {
                let activeCount = (result["active_count"] as? Int) ?? 0
                let totalBytes = (result["total_bytes"] as? Int) ?? 0
                statsSuffix = " • Активных: \(activeCount), \(HistoryPanelController.formatBytesIfStatic(totalBytes))"
            }
            // Inline fetch overview.
            var overview = "Обзор: недоступен"
            if let response = try? ipcClient.call(method: "get_history_overview", params: [:]),
               let result = response["result"] as? [String: Any] {
                overview = HistoryPanelController.formatHistoryOverview(result: result)
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.historyStatusLabel.stringValue = baseText + statsSuffix
                self.historyOverviewLabel.stringValue = overview
            }
        }
    }

    /// Pure helper — `formatBytes` не доступна на main (instance method).
    /// Делает то же самое для использования в background closures.
    /// Если `formatBytes` будет refactor'ена в `nonisolated static` — этот helper
    /// можно удалить и звать `HistoryPanelController.formatBytes(_:)`.
    nonisolated static func formatBytesIfStatic(_ value: Int) -> String {
        let safe = max(0, value)
        if safe < 1024 { return "\(safe) B" }
        let kb = Double(safe) / 1024.0
        if kb < 1024 { return String(format: "%.1f KB", kb) }
        let mb = kb / 1024.0
        if mb < 1024 { return String(format: "%.1f MB", mb) }
        let gb = mb / 1024.0
        return String(format: "%.2f GB", gb)
    }

    /// Pure helper — форматирует ответ `get_history_overview` в overview label.
    /// Включает все поля: today/24h counts + paste ok/err + translation ok/err.
    /// `nonisolated static` — тестируется без instance.
    nonisolated static func formatHistoryOverview(result: [String: Any]) -> String {
        let todayCount = (result["today_count"] as? Int) ?? 0
        let last24hCount = (result["last_24h_count"] as? Int) ?? 0
        let pasteOk = (result["paste_ok"] as? Int) ?? 0
        let pasteFailed = (result["paste_failed"] as? Int) ?? 0
        let translatedOk = (result["translated_ok"] as? Int) ?? 0
        let translatedError = (result["translated_error"] as? Int) ?? 0
        return "Обзор: сегодня \(todayCount), 24ч \(last24hCount), вставка ok/err \(pasteOk)/\(pasteFailed), перевод ok/err \(translatedOk)/\(translatedError)"
    }

    func updateDictationHistoryPreview() {
        let stats = fetchHistoryStats()
        let activeCount = stats?.activeCount ?? 0

        if items.isEmpty {
            dictationHistoryOpenButton.isEnabled = activeCount > 0
            if activeCount > 0 {
                let suffix = hasActiveHistoryFiltersOrQuery() ? " (фильтры/поиск активны)" : ""
                dictationHistoryHintLabel.stringValue = "В истории есть \(activeCount) записей\(suffix)."
                dictationHistoryPreviewView.string = "Записи есть, но текущая выборка пустая. Нажмите «Открыть историю»."
            } else {
                dictationHistoryHintLabel.stringValue = "История пока пустая. После первой транскрибации записи появятся здесь."
                dictationHistoryPreviewView.string = "Пока нет записей для предпросмотра."
            }
            return
        }

        dictationHistoryOpenButton.isEnabled = true
        dictationHistoryHintLabel.stringValue = "Показаны последние \(min(items.count, 5)) из \(max(activeCount, items.count)) записей."
        let lines = items.prefix(5).enumerated().map { index, item -> String in
            let raw = item.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let shortText = raw.count > 120 ? String(raw.prefix(120)) + "…" : raw
            return "\(index + 1). [\(item.ts)] \(shortText)"
        }
        dictationHistoryPreviewView.string = lines.joined(separator: "\n")
    }

    func updateHistoryPreviewCard() {
        if items.isEmpty {
            let message = hasActiveHistoryFiltersOrQuery()
                ? "Фильтры/поиск скрывают записи. Снимите фильтры или обновите поиск."
                : "История пуста. Запишите что-нибудь — запись появится здесь."
            historyPreviewTextView.string = message
            return
        }

        let previewLines = items.prefix(3).enumerated().map { index, item -> String in
            let snippet = item.text.trimmingCharacters(in: .whitespacesAndNewlines)
            let truncated = snippet.count > 150 ? String(snippet.prefix(150)) + "…" : snippet
            return "\(index + 1). [\(item.ts)] \(truncated)"
        }
        historyPreviewTextView.string = previewLines.joined(separator: "\n\n")
    }

    // MARK: - Stats / Overview

    func fetchHistoryStats() -> (activeCount: Int, totalBytes: Int)? {
        guard
            let response = try? ipcClient.call(method: "get_history_stats", params: [:]),
            let result = response["result"] as? [String: Any]
        else {
            return nil
        }
        return (
            activeCount: (result["active_count"] as? Int) ?? 0,
            totalBytes: (result["total_bytes"] as? Int) ?? 0
        )
    }

    // buildHistoryOverviewLabel() удалена в этом PR — заменена inline IPC fetch +
    // pure helper `formatHistoryOverview(result:)` в updateHistoryStatusLabel
    // (см. PR #315). Все поля сохранены (в т.ч. translated_ok/error добавлены
    // в formatHistoryOverview этим PR).

    /// Pure helper — может вызываться из любого thread (nonisolated).
    /// Используется в onCompact() из background closure для форматирования
    /// строки до перехода на main thread (избегаем race на @MainActor).
    nonisolated func formatBytes(_ value: Int) -> String {
        let safe = max(0, value)
        if safe < 1024 {
            return "\(safe) B"
        }
        let kb = Double(safe) / 1024.0
        if kb < 1024 {
            return String(format: "%.1f KB", kb)
        }
        let mb = kb / 1024.0
        if mb < 1024 {
            return String(format: "%.1f MB", mb)
        }
        let gb = mb / 1024.0
        return String(format: "%.2f GB", gb)
    }

    // MARK: - Query params

    func buildHistoryQueryParams(cursor: Any, limit: Int) -> [String: Any] {
        var params: [String: Any] = [
            "cursor": cursor,
            "limit": limit,
        ]

        let pasteStatus = selectedHistoryPasteStatusFilter()
        if let pasteStatus {
            params["paste_status"] = pasteStatus
        }
        let translationMode = selectedHistoryTranslationModeFilter()
        if let translationMode {
            params["translation_mode"] = translationMode
        }
        let translationStatus = selectedHistoryTranslationStatusFilter()
        if let translationStatus {
            params["translation_status"] = translationStatus
        }
        let fromTs = historyFromDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !fromTs.isEmpty {
            params["from_ts"] = fromTs
        }
        let toTs = historyToDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !toTs.isEmpty {
            params["to_ts"] = toTs
        }
        return params
    }

    func selectedHistoryPasteStatusFilter() -> String? {
        let idx = historyPasteStatusFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "ok"
        case 2:
            return "failed"
        default:
            return nil
        }
    }

    func selectedHistoryTranslationModeFilter() -> String? {
        let idx = historyTranslationModeFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "off"
        case 2:
            return "ru_to_es"
        case 3:
            return "es_to_ru"
        case 4:
            return "en_to_ru"
        case 5:
            return "auto"
        case 6:
            return "auto_to_ru"
        case 7:
            return "bilingual_ru_es"
        default:
            return nil
        }
    }

    func selectedHistoryTranslationStatusFilter() -> String? {
        let idx = historyTranslationStatusFilter.indexOfSelectedItem
        switch idx {
        case 1:
            return "ok"
        case 2:
            return "not_requested"
        case 3:
            return "model_unavailable_offline"
        case 4:
            return "model_unavailable_online"
        case 5:
            return "model_unavailable_cached"
        case 6:
            return "cannot_detect_language"
        case 7:
            return "already_target_language"
        case 8:
            return "translate_error"
        default:
            return nil
        }
    }

    // MARK: - Export builders

    func buildHistoryNdjsonExport() -> String {
        var lines: [String] = []
        for item in items {
            let payload: [String: Any] = [
                "id": item.id,
                "ts": item.ts,
                "text": item.text,
                "paste_status": item.pasteStatus,
                "source_text": item.sourceText,
                "translated_text": item.translatedText,
                "translation_mode": item.translationMode,
                "translation_status": item.translationStatus,
            ]
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
               let raw = String(data: data, encoding: .utf8) {
                lines.append(raw)
            }
        }
        return lines.joined(separator: "\n") + "\n"
    }

    // MARK: - NSTableViewDataSource / NSTableViewDelegate

    func numberOfRows(in tableView: NSTableView) -> Int {
        items.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row < items.count else { return nil }
        let item = items[row]

        let identifier = tableColumn?.identifier.rawValue ?? "unknown"
        let text: String
        switch identifier {
        case "ts":
            text = item.ts
        case "status":
            text = item.pasteStatus
        default:
            let translationBadge = buildTranslationBadge(item)
            text = translationBadge + item.text
        }

        let isTextColumn = identifier == "text"
        let bodyFont = historyBodyFont()
        let label = isTextColumn
            ? NSTextField(wrappingLabelWithString: text)
            : NSTextField(labelWithString: text)
        label.font = bodyFont
        label.maximumNumberOfLines = isTextColumn ? 0 : 1
        label.lineBreakMode = isTextColumn ? .byWordWrapping : .byTruncatingTail

        let cell = NSTableCellView()
        cell.addSubview(label)
        label.translatesAutoresizingMaskIntoConstraints = false

        if isTextColumn {
            // Кнопка инлайн-перевода (🌐 / spinner-символ) справа от текста.
            let isLoading = inlineTranslationLoading.contains(item.id)
            let isVisible = inlineTranslationVisible.contains(item.id)
            let btnTitle = isLoading ? "⏳" : (isVisible ? "🔤" : "🌐")
            let translateBtn = NSButton(title: btnTitle, target: self, action: #selector(onInlineTranslateToggle(_:)))
            translateBtn.bezelStyle = .inline
            translateBtn.isBordered = false
            translateBtn.tag = row
            translateBtn.translatesAutoresizingMaskIntoConstraints = false
            translateBtn.toolTip = isVisible ? "Скрыть перевод" : "Показать перевод"
            cell.addSubview(translateBtn)

            // Вторичная строка перевода (italic, alpha 0.8) — показывается когда isVisible.
            let translationLabel = NSTextField(wrappingLabelWithString: "")
            translationLabel.translatesAutoresizingMaskIntoConstraints = false
            translationLabel.isHidden = !isVisible
            translationLabel.alphaValue = 0.8
            translationLabel.maximumNumberOfLines = 0
            translationLabel.lineBreakMode = .byWordWrapping

            let italicDescriptor2 = bodyFont.fontDescriptor.withSymbolicTraits(.italic)
            let italicFont = NSFont(descriptor: italicDescriptor2, size: bodyFont.pointSize - 1) ?? bodyFont
            translationLabel.font = italicFont
            translationLabel.textColor = NSColor.secondaryLabelColor

            if isVisible {
                if let cached = inlineTranslationCache.object(forKey: item.id as NSString) {
                    translationLabel.stringValue = cached as String
                } else if isLoading {
                    translationLabel.stringValue = "…"
                }
            }

            cell.addSubview(translationLabel)

            if let confidence = item.confidence {
                // Горизонтальный индикатор уверенности STT (3pt высота) под переводом.
                let bar = NSView()
                bar.wantsLayer = true
                bar.translatesAutoresizingMaskIntoConstraints = false
                let barColor = confidenceColor(for: confidence)
                bar.layer?.backgroundColor = barColor.cgColor
                bar.layer?.cornerRadius = 1.5

                bar.alphaValue = 0
                KrabEarTheme.Motion.animate(
                    duration: KrabEarTheme.Motion.Duration.micro,
                    easing: KrabEarTheme.Motion.Easing.easeOut
                ) {
                    bar.alphaValue = 1
                }

                cell.addSubview(bar)
                NSLayoutConstraint.activate([
                    // Основной текст — оставляем место для кнопки справа.
                    label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    label.trailingAnchor.constraint(equalTo: translateBtn.leadingAnchor, constant: -4),
                    label.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),

                    // Кнопка перевода — прижата к правому краю, выровнена с первой строкой текста.
                    translateBtn.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    translateBtn.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),
                    translateBtn.widthAnchor.constraint(equalToConstant: 20),
                    translateBtn.heightAnchor.constraint(equalToConstant: 18),

                    // Строка перевода под основным текстом.
                    translationLabel.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    translationLabel.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    translationLabel.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 2),
                    translationLabel.bottomAnchor.constraint(equalTo: bar.topAnchor, constant: -2),

                    // Полоска уверенности.
                    bar.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    bar.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    bar.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -4),
                    bar.heightAnchor.constraint(equalToConstant: 3),
                ])
            } else {
                NSLayoutConstraint.activate([
                    label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    label.trailingAnchor.constraint(equalTo: translateBtn.leadingAnchor, constant: -4),
                    label.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),

                    translateBtn.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    translateBtn.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),
                    translateBtn.widthAnchor.constraint(equalToConstant: 20),
                    translateBtn.heightAnchor.constraint(equalToConstant: 18),

                    translationLabel.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                    translationLabel.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                    translationLabel.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 2),
                    translationLabel.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -4),
                ])
            }
        } else {
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
                label.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4),
                label.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -4),
            ])
        }
        return cell
    }

    // MARK: - Inline translation toggle handler

    @objc func onInlineTranslateToggle(_ sender: NSButton) {
        let row = sender.tag
        guard row >= 0, row < items.count else { return }
        let item = items[row]

        if inlineTranslationVisible.contains(item.id) {
            // Toggle OFF — скрыть перевод.
            inlineTranslationVisible.remove(item.id)
            tableView.reloadData(forRowIndexes: IndexSet(integer: row), columnIndexes: IndexSet(integer: textColumnIndex()))
            return
        }

        // Toggle ON — проверяем кэш.
        if let cached = inlineTranslationCache.object(forKey: item.id as NSString) as String?,
           !cached.isEmpty {
            inlineTranslationVisible.insert(item.id)
            tableView.reloadData(forRowIndexes: IndexSet(integer: row), columnIndexes: IndexSet(integer: textColumnIndex()))
            return
        }

        // Ничего в кэше — делаем IPC-запрос.
        guard !inlineTranslationLoading.contains(item.id) else { return }
        inlineTranslationLoading.insert(item.id)
        inlineTranslationVisible.insert(item.id)
        tableView.reloadData(forRowIndexes: IndexSet(integer: row), columnIndexes: IndexSet(integer: textColumnIndex()))

        let textToTranslate = item.text
        let itemID = item.id
        let capturedRow = row
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let response = try? self.ipcClient.call(
                method: "translate_text",
                params: [
                    "text": textToTranslate,
                    "translation_mode": "auto",
                ]
            )
            let result = response?["result"] as? [String: Any]
            let translated = ((result?["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

            DispatchQueue.main.async {
                self.inlineTranslationLoading.remove(itemID)
                if !translated.isEmpty {
                    self.inlineTranslationCache.setObject(translated as NSString, forKey: itemID as NSString)
                } else {
                    // Ошибка перевода — скрыть строку.
                    self.inlineTranslationVisible.remove(itemID)
                }
                if capturedRow < self.items.count {
                    self.tableView.reloadData(
                        forRowIndexes: IndexSet(integer: capturedRow),
                        columnIndexes: IndexSet(integer: self.textColumnIndex())
                    )
                }
            }
        }
    }

    /// Возвращает индекс колонки "text" в tableView.
    func textColumnIndex() -> Int {
        tableView.tableColumns.firstIndex { $0.identifier.rawValue == "text" } ?? 0
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        guard row < items.count else { return historyMinRowHeight() }
        let item = items[row]

        let textColumn = tableView.tableColumns.first { $0.identifier.rawValue == "text" }
        // Оставляем 28pt под кнопку перевода справа.
        let columnWidth = max(180, (textColumn?.width ?? 700) - 36)
        // Cache key учитывает item.id, columnWidth и состояние inline translation —
        // при изменении любого пересчитываем (KRAB-EAR-AGENT-5 fix).
        let translationVisible = inlineTranslationVisible.contains(item.id)
        let cacheKey = "\(item.id):\(Int(columnWidth)):\(translationVisible ? 1 : 0)"
        if let cached = rowHeightCache[cacheKey] { return cached }

        let sampleText = item.text as NSString
        let bodyFont = historyBodyFont()
        let textHeight = sampleText.boundingRect(
            with: NSSize(width: columnWidth, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: bodyFont],
            context: nil
        ).height

        var totalHeight = ceil(textHeight) + 10

        // Если перевод виден — добавляем высоту второй строки.
        if translationVisible {
            let translationText: String
            if let cached = inlineTranslationCache.object(forKey: item.id as NSString) {
                translationText = cached as String
            } else {
                translationText = "…"
            }
            let italicDescriptor = bodyFont.fontDescriptor.withSymbolicTraits(.italic)
            let italicFont = NSFont(descriptor: italicDescriptor, size: bodyFont.pointSize - 1) ?? bodyFont
            let translationHeight = (translationText as NSString).boundingRect(
                with: NSSize(width: columnWidth, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading],
                attributes: [.font: italicFont],
                context: nil
            ).height
            totalHeight += ceil(translationHeight) + 6
        }

        let height = max(historyMinRowHeight(), totalHeight)
        rowHeightCache[cacheKey] = height
        return height
    }

    func buildTranslationBadge(_ item: HistoryItem) -> String {
        guard item.translationMode != "off" else { return "" }
        let statusMark: String
        switch item.translationStatus {
        case "ok":
            statusMark = "ok"
        case "not_requested":
            statusMark = "skip"
        case "model_unavailable_offline":
            statusMark = "offline"
        case "model_unavailable_online":
            statusMark = "online?"
        case "model_unavailable_cached":
            statusMark = "cached"
        case "cannot_detect_language":
            statusMark = "lang?"
        case "already_target_language":
            statusMark = "ru=ok"
        case "translate_error":
            statusMark = "error"
        default:
            statusMark = "warn"
        }
        return "[\(item.translationMode):\(statusMark)] "
    }

    func buildHistoryMarkdownExport() -> String {
        var lines: [String] = []
        lines.append("# Krab Ear History Export")
        lines.append("")
        lines.append("- exported_at: \(ISO8601DateFormatter().string(from: Date()))")
        lines.append("- items: \(items.count)")
        lines.append("- search_query: \(currentQuery.isEmpty ? "(none)" : currentQuery)")
        lines.append("")

        for (index, item) in items.enumerated() {
            lines.append("## \(index + 1). \(item.ts)")
            lines.append("")
            lines.append("- id: \(item.id)")
            lines.append("- paste_status: \(item.pasteStatus)")
            lines.append("- translation_mode: \(item.translationMode)")
            lines.append("- translation_status: \(item.translationStatus)")
            lines.append("")
            lines.append("### text")
            lines.append("")
            lines.append(item.text)
            lines.append("")
            if !item.sourceText.isEmpty {
                lines.append("### source_text")
                lines.append("")
                lines.append(item.sourceText)
                lines.append("")
            }
            if !item.translatedText.isEmpty {
                lines.append("### translated_text")
                lines.append("")
                lines.append(item.translatedText)
                lines.append("")
            }
        }

        return lines.joined(separator: "\n")
    }

    // MARK: - Testable helpers (inline translation cache logic)

    /// Возвращает true если кэш уже содержит перевод для данного itemID.
    static func inlineTranslationCacheHit(
        cache: NSCache<NSString, NSString>,
        itemID: String
    ) -> Bool {
        cache.object(forKey: itemID as NSString) != nil
    }

    /// Вычисляет новое состояние видимости перевода при toggle:
    /// - если уже visible → returns false (скрыть)
    /// - если not visible → returns true (показать)
    static func inlineTranslationNextVisible(
        currentlyVisible: Set<String>,
        itemID: String
    ) -> Bool {
        !currentlyVisible.contains(itemID)
    }
}
