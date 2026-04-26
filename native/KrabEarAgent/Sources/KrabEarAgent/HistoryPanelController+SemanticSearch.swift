/*
 Семантический поиск по истории транскрипций (PR #284 — backend готов).

 Backend IPC:
   - semantic_search        → {results: [{id, score}], mode: "semantic"|"keyword"|"disabled"}
   - semantic_search_status → {enabled, model_loaded, model_name, model_error, indexed_count}
   - semantic_search_reindex → {indexed, skipped, errors}

 UI: CollapsibleSection "Семантический поиск" в History tab. Search field + режим
 (Авто/Semantic/Keyword) + кнопки Искать/Переиндексировать. Результаты — текстовый
 список с оценкой релевантности (cosine similarity score 0-1).
*/

import AppKit
import ObjectiveC

private enum SemanticSearchAssoc {
    nonisolated(unsafe) static var searchField: UInt8 = 0
    nonisolated(unsafe) static var modeSelector: UInt8 = 0
    nonisolated(unsafe) static var statusLabel: UInt8 = 0
    nonisolated(unsafe) static var resultsView: UInt8 = 0
}

@MainActor
extension HistoryPanelController {

    // MARK: - Lazy UI elements (associated objects, тот же паттерн что в +Analytics)

    private var semanticSearchField: NSSearchField {
        if let v = objc_getAssociatedObject(self, &SemanticSearchAssoc.searchField) as? NSSearchField {
            return v
        }
        let v = NSSearchField()
        v.placeholderString = "Запрос по смыслу (а не точные слова)"
        v.font = KrabEarTheme.Typography.body
        v.target = self
        v.action = #selector(runSemanticSearchAction)
        objc_setAssociatedObject(self, &SemanticSearchAssoc.searchField, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var semanticModeSelector: NSPopUpButton {
        if let v = objc_getAssociatedObject(self, &SemanticSearchAssoc.modeSelector) as? NSPopUpButton {
            return v
        }
        let v = NSPopUpButton(frame: .zero, pullsDown: false)
        v.addItems(withTitles: ["Авто (semantic + fallback)", "Только semantic", "Только keyword"])
        objc_setAssociatedObject(self, &SemanticSearchAssoc.modeSelector, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var semanticStatusLabel: NSTextField {
        if let v = objc_getAssociatedObject(self, &SemanticSearchAssoc.statusLabel) as? NSTextField {
            return v
        }
        let v = NSTextField(labelWithString: "Статус: —")
        v.font = KrabEarTheme.Typography.caption
        v.textColor = NSColor.secondaryLabelColor
        objc_setAssociatedObject(self, &SemanticSearchAssoc.statusLabel, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var semanticResultsView: NSTextView {
        if let v = objc_getAssociatedObject(self, &SemanticSearchAssoc.resultsView) as? NSTextView {
            return v
        }
        let v = NSTextView()
        v.isEditable = false
        v.isSelectable = true
        v.font = KrabEarTheme.Typography.body
        v.textContainerInset = NSSize(width: 8, height: 8)
        v.minSize = NSSize(width: 0, height: 120)
        objc_setAssociatedObject(self, &SemanticSearchAssoc.resultsView, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    // MARK: - Section builder (вызывается из applyVisualTheme в HistoryPanelController.swift)

    public func setupSemanticSearchSection() -> CollapsibleSectionView {
        let card = ThemeCardView()

        let searchRow = NSStackView(views: [semanticSearchField, semanticModeSelector])
        searchRow.orientation = .horizontal
        searchRow.spacing = KrabEarTheme.Metrics.standard
        searchRow.distribution = .fill

        let runButton = ThemeSecondaryButton(title: "Искать", target: self, action: #selector(runSemanticSearchAction))
        runButton.applyThemeSecondary()
        let reindexButton = ThemeSecondaryButton(title: "Переиндексировать", target: self, action: #selector(reindexSemanticSearchAction))
        reindexButton.applyThemeSecondary()
        let statusButton = ThemeSecondaryButton(title: "Обновить статус", target: self, action: #selector(refreshSemanticStatusAction))
        statusButton.applyThemeSecondary()
        let actionsRow = NSStackView(views: [runButton, reindexButton, statusButton, NSView()])
        actionsRow.orientation = .horizontal
        actionsRow.spacing = KrabEarTheme.Metrics.standard
        actionsRow.distribution = .fill

        let resultsScroll = NSScrollView()
        resultsScroll.hasVerticalScroller = true
        resultsScroll.borderType = .lineBorder
        resultsScroll.documentView = semanticResultsView
        resultsScroll.translatesAutoresizingMaskIntoConstraints = false
        resultsScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 140).isActive = true

        card.contentStackView.addArrangedSubview(searchRow)
        card.contentStackView.addArrangedSubview(actionsRow)
        card.contentStackView.addArrangedSubview(semanticStatusLabel)
        card.contentStackView.addArrangedSubview(resultsScroll)

        let section = CollapsibleSectionView(
            sectionId: "history_semantic_search",
            title: "Семантический поиск",
            isExpanded: false
        )
        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Actions

    @objc func runSemanticSearchAction() {
        let query = semanticSearchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            semanticResultsView.string = "Введите запрос."
            return
        }
        let modeIdx = semanticModeSelector.indexOfSelectedItem
        let useFallback = modeIdx != 1   // 1 = "Только semantic" → fallback off
        let forceKeyword = modeIdx == 2  // 2 = "Только keyword" → используем фолбэк handler

        nonisolated(unsafe) let ipcClient = self.ipcClient
        nonisolated(unsafe) let queryCopy = query
        nonisolated(unsafe) let resultsView = self.semanticResultsView
        let items = self.items

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                if forceKeyword {
                    let keywordResults = HistoryPanelController.keywordFallback(
                        query: queryCopy,
                        items: items.map { ["id": $0.id, "text": $0.text] },
                        topK: 10
                    )
                    let formatted = HistoryPanelController.formatSearchResults(
                        results: keywordResults,
                        items: items,
                        mode: "keyword (forced)"
                    )
                    DispatchQueue.main.async {
                        resultsView.string = formatted
                    }
                    return
                }

                let r = try ipcClient.call(
                    method: "semantic_search",
                    params: ["query": queryCopy, "top_k": 10, "fallback": useFallback]
                )
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                let mode = result["mode"] as? String ?? "?"
                let rawResults = result["results"] as? [[String: Any]] ?? []
                let formatted = HistoryPanelController.formatSearchResults(
                    results: rawResults.map { dict -> (String, Double) in
                        let id = dict["id"] as? String ?? ""
                        let score = (dict["score"] as? Double) ?? 0.0
                        return (id, score)
                    },
                    items: items,
                    mode: mode
                )
                DispatchQueue.main.async {
                    resultsView.string = formatted
                }
            } catch {
                DispatchQueue.main.async {
                    resultsView.string = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    @objc func reindexSemanticSearchAction() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        semanticStatusLabel.stringValue = "Переиндексирую… (может занять минуту)"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(
                    method: "semantic_search_reindex",
                    params: ["force": false]
                )
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                let indexed = result["indexed"] as? Int ?? 0
                let skipped = result["skipped"] as? Int ?? 0
                let errors = result["errors"] as? Int ?? 0
                let reason = result["reason"] as? String
                DispatchQueue.main.async {
                    if let reason = reason {
                        self?.semanticStatusLabel.stringValue = "Не выполнено: \(reason)"
                    } else {
                        self?.semanticStatusLabel.stringValue = "Готово: проиндексировано \(indexed), пропущено \(skipped), ошибок \(errors)"
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.semanticStatusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    @objc func refreshSemanticStatusAction() {
        nonisolated(unsafe) let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "semantic_search_status", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                let enabled = result["enabled"] as? Bool ?? false
                let modelLoaded = result["model_loaded"] as? Bool ?? false
                let modelName = result["model_name"] as? String ?? "—"
                let modelError = result["model_error"] as? String
                let indexed = result["indexed_count"] as? Int ?? 0
                let parts: [String] = [
                    "enabled=\(enabled ? "✓" : "✗")",
                    "model=\(modelLoaded ? "✓" : "✗") \(modelName)",
                    "indexed=\(indexed)",
                    modelError.map { "err=\($0)" } ?? ""
                ].filter { !$0.isEmpty }
                let summary = parts.joined(separator: " · ")
                DispatchQueue.main.async {
                    self?.semanticStatusLabel.stringValue = "Статус: \(summary)"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.semanticStatusLabel.stringValue = "Статус: ошибка \(error.localizedDescription)"
                }
            }
        }
    }

    // MARK: - Pure helpers (testable)

    /// Простой keyword-фолбэк на стороне клиента — используется когда mode = "Только keyword".
    /// Дублирует логику `keyword_fallback_search` из backend/semantic_search.py.
    nonisolated static func keywordFallback(
        query: String,
        items: [[String: String]],
        topK: Int
    ) -> [(String, Double)] {
        let queryWords = Set(query.lowercased().split(separator: " ").map(String.init))
        guard !queryWords.isEmpty else { return [] }
        var results: [(String, Double)] = []
        for item in items {
            guard let id = item["id"], !id.isEmpty,
                  let text = item["text"]?.lowercased(), !text.isEmpty else { continue }
            let matched = queryWords.filter { text.contains($0) }.count
            if matched > 0 {
                let score = Double(matched) / Double(max(queryWords.count, 1))
                results.append((id, score))
            }
        }
        return Array(results.sorted { $0.1 > $1.1 }.prefix(topK))
    }

    /// Форматирует список (id, score) в человекочитаемый текст для NSTextView.
    nonisolated static func formatSearchResults(
        results: [(String, Double)],
        items: [HistoryItem],
        mode: String
    ) -> String {
        if results.isEmpty {
            return "Ничего не найдено (режим: \(mode))."
        }
        let byID: [String: HistoryItem] = Dictionary(uniqueKeysWithValues: items.map { ($0.id, $0) })
        var lines = ["Режим: \(mode)  ·  результатов: \(results.count)\n"]
        for (i, pair) in results.enumerated() {
            let scorePct = String(format: "%.0f%%", pair.1 * 100)
            let idShort = String(pair.0.prefix(8))
            let preview: String
            if let item = byID[pair.0] {
                let raw = item.text.replacingOccurrences(of: "\n", with: " ")
                preview = raw.count > 100 ? String(raw.prefix(100)) + "…" : raw
            } else {
                preview = "(не найдено в текущей странице истории)"
            }
            lines.append("\(i + 1). [\(scorePct)] \(idShort)…  \(preview)")
        }
        return lines.joined(separator: "\n")
    }
}
