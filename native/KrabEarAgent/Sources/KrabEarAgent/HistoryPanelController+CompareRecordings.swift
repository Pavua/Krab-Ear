/*
 * HistoryPanelController+CompareRecordings.swift
 * Описание: Модуль для сравнения 2+ выбранных записей.
 * Почему существует: Даёт пользователю возможность сравнить выбранные записи истории side-by-side: матрица сходства, общие и уникальные слова.
 * Связь: Вызывается из контекстного меню таблицы истории (пункт «Сравнить выбранные»).
 */

import AppKit

// MARK: - CompareRecordingsViewController

@MainActor
final class CompareRecordingsViewController: NSViewController {
    
    private let itemsData: [[String: Any]]
    private let matrix: [[Double]]
    private let commonWords: [String]
    private let uniqueWords: [[String]]
    
    init(items: [[String: Any]], matrix: [[Double]], commonWords: [String], uniqueWords: [[String]]) {
        self.itemsData = items
        self.matrix = matrix
        self.commonWords = commonWords
        self.uniqueWords = uniqueWords
        super.init(nibName: nil, bundle: nil)
    }
    
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
    
    override func loadView() {
        self.view = NSView()
        self.view.wantsLayer = true
        // KrabEarTheme compatible background
        self.view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        
        let containerStack = NSStackView()
        containerStack.orientation = .vertical
        containerStack.spacing = KrabEarTheme.Metrics.standard
        containerStack.edgeInsets = NSEdgeInsets(top: KrabEarTheme.Metrics.standard, left: KrabEarTheme.Metrics.standard, bottom: KrabEarTheme.Metrics.standard, right: KrabEarTheme.Metrics.standard)
        containerStack.translatesAutoresizingMaskIntoConstraints = false
        
        self.view.addSubview(containerStack)
        
        NSLayoutConstraint.activate([
            containerStack.topAnchor.constraint(equalTo: self.view.topAnchor),
            containerStack.leadingAnchor.constraint(equalTo: self.view.leadingAnchor),
            containerStack.trailingAnchor.constraint(equalTo: self.view.trailingAnchor),
            containerStack.bottomAnchor.constraint(equalTo: self.view.bottomAnchor)
        ])
        
        // Title
        let titleLabel = NSTextField(labelWithString: "Сравнение записей")
        titleLabel.font = NSFont.systemFont(ofSize: 20, weight: .bold)
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.isSelectable = false
        titleLabel.isBordered = false
        titleLabel.drawsBackground = false
        containerStack.addArrangedSubview(titleLabel)
        
        // Content Stack
        let contentStack = NSStackView()
        contentStack.orientation = .horizontal
        contentStack.alignment = .top
        contentStack.spacing = KrabEarTheme.Metrics.standard * 2
        containerStack.addArrangedSubview(contentStack)
        
        // --- Left: Matrix ---
        let leftCol = NSStackView()
        leftCol.orientation = .vertical
        leftCol.alignment = .leading
        leftCol.spacing = KrabEarTheme.Metrics.tight
        contentStack.addArrangedSubview(leftCol)
        
        let matrixTitle = NSTextField(labelWithString: "Матрица сходства (%)")
        matrixTitle.font = KrabEarTheme.Typography.sectionTitle
        matrixTitle.textColor = KrabEarTheme.Colors.textPrimary
        matrixTitle.isSelectable = false
        matrixTitle.isBordered = false
        matrixTitle.drawsBackground = false
        leftCol.addArrangedSubview(matrixTitle)
        
        let matrixView = createMatrixView()
        leftCol.addArrangedSubview(matrixView)
        
        // --- Right: Words ---
        let rightCol = NSStackView()
        rightCol.orientation = .vertical
        rightCol.alignment = .leading
        rightCol.spacing = KrabEarTheme.Metrics.standard
        contentStack.addArrangedSubview(rightCol)
        
        // Common words
        let commonTitle = NSTextField(labelWithString: "Общие слова")
        commonTitle.font = KrabEarTheme.Typography.sectionTitle
        commonTitle.textColor = KrabEarTheme.Colors.textPrimary
        commonTitle.isSelectable = false
        commonTitle.isBordered = false
        commonTitle.drawsBackground = false
        rightCol.addArrangedSubview(commonTitle)
        
        let commonCloud = createCloudView(words: commonWords, accent: true)
        rightCol.addArrangedSubview(commonCloud)
        
        // Unique words per item
        let uniqueTitle = NSTextField(labelWithString: "Уникальные слова")
        uniqueTitle.font = KrabEarTheme.Typography.sectionTitle
        uniqueTitle.textColor = KrabEarTheme.Colors.textPrimary
        uniqueTitle.isSelectable = false
        uniqueTitle.isBordered = false
        uniqueTitle.drawsBackground = false
        rightCol.addArrangedSubview(uniqueTitle)
        
        let uniqueStack = NSStackView()
        uniqueStack.orientation = .vertical
        uniqueStack.spacing = KrabEarTheme.Metrics.tight
        uniqueStack.alignment = .leading
        
        for (i, item) in itemsData.enumerated() {
            let itemName = itemTitle(item, index: i)
            let itemStack = NSStackView()
            itemStack.orientation = .vertical
            itemStack.spacing = 2
            itemStack.alignment = .leading
            
            let itemLabel = NSTextField(labelWithString: itemName)
            itemLabel.font = KrabEarTheme.Typography.captionMedium
            itemLabel.textColor = KrabEarTheme.Colors.textSecondary
            itemLabel.isSelectable = false
            itemLabel.isBordered = false
            itemLabel.drawsBackground = false
            itemStack.addArrangedSubview(itemLabel)
            
            let words = (i < uniqueWords.count) ? uniqueWords[i] : []
            let wordsCloud = createCloudView(words: words, accent: false)
            itemStack.addArrangedSubview(wordsCloud)
            
            uniqueStack.addArrangedSubview(itemStack)
        }
        
        let scrollUnique = NSScrollView()
        scrollUnique.hasVerticalScroller = true
        scrollUnique.documentView = uniqueStack
        scrollUnique.drawsBackground = false
        scrollUnique.heightAnchor.constraint(equalToConstant: 250).isActive = true
        scrollUnique.widthAnchor.constraint(equalToConstant: 300).isActive = true
        rightCol.addArrangedSubview(scrollUnique)
        
        // Spacer and Close button
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .vertical)
        containerStack.addArrangedSubview(spacer)
        
        let closeBtn = NSButton(title: "Закрыть", target: self, action: #selector(onClose))
        closeBtn.bezelStyle = .rounded
        containerStack.addArrangedSubview(closeBtn)
        containerStack.alignment = .centerX
    }
    
    private func itemTitle(_ item: [String: Any], index: Int) -> String {
        if let title = item["title"] as? String, !title.isEmpty {
            return title
        }
        if let text = item["text"] as? String, !text.isEmpty {
            let limit = 20
            if text.count > limit {
                return text.prefix(limit) + "..."
            }
            return text
        }
        return "Запись #\(index + 1)"
    }
    
    private func createMatrixView() -> NSView {
        let grid = NSStackView()
        grid.orientation = .vertical
        grid.spacing = 2
        grid.alignment = .leading
        
        let n = itemsData.count
        
        // Header row
        let headerRow = NSStackView()
        headerRow.orientation = .horizontal
        headerRow.spacing = 2
        let emptyCorner = NSTextField(labelWithString: "")
        emptyCorner.isBordered = false
        emptyCorner.drawsBackground = false
        emptyCorner.frame = CGRect(x:0, y:0, width:60, height:20)
        emptyCorner.widthAnchor.constraint(equalToConstant: 60).isActive = true
        headerRow.addArrangedSubview(emptyCorner)
        
        for i in 0..<n {
            let lbl = NSTextField(labelWithString: "#\(i+1)")
            lbl.alignment = .center
            lbl.isBordered = false
            lbl.drawsBackground = false
            lbl.font = KrabEarTheme.Typography.captionMedium
            lbl.textColor = KrabEarTheme.Colors.textSecondary
            lbl.widthAnchor.constraint(equalToConstant: 40).isActive = true
            headerRow.addArrangedSubview(lbl)
        }
        grid.addArrangedSubview(headerRow)
        
        for i in 0..<n {
            let rowStack = NSStackView()
            rowStack.orientation = .horizontal
            rowStack.spacing = 2
            
            let rowLabel = NSTextField(labelWithString: itemTitle(itemsData[i], index: i))
            rowLabel.font = KrabEarTheme.Typography.caption
            rowLabel.textColor = KrabEarTheme.Colors.textPrimary
            rowLabel.lineBreakMode = .byTruncatingTail
            rowLabel.isSelectable = false
            rowLabel.isBordered = false
            rowLabel.drawsBackground = false
            rowLabel.widthAnchor.constraint(equalToConstant: 60).isActive = true
            rowStack.addArrangedSubview(rowLabel)
            
            for j in 0..<n {
                let cell = NSView()
                cell.wantsLayer = true
                cell.widthAnchor.constraint(equalToConstant: 40).isActive = true
                cell.heightAnchor.constraint(equalToConstant: 40).isActive = true
                
                var val: Double = 0.0
                if i < matrix.count, j < matrix[i].count {
                    val = matrix[i][j]
                }
                
                let alpha = max(0.1, CGFloat(val))
                cell.layer?.backgroundColor = KrabEarTheme.Colors.accent.withAlphaComponent(alpha).cgColor
                cell.layer?.cornerRadius = 4
                
                let text = NSTextField(labelWithString: "\(Int(val * 100))")
                text.font = NSFont.systemFont(ofSize: 11)
                text.textColor = val > 0.5 ? NSColor.white : KrabEarTheme.Colors.textPrimary
                text.isBordered = false
                text.drawsBackground = false
                text.translatesAutoresizingMaskIntoConstraints = false
                cell.addSubview(text)
                NSLayoutConstraint.activate([
                    text.centerXAnchor.constraint(equalTo: cell.centerXAnchor),
                    text.centerYAnchor.constraint(equalTo: cell.centerYAnchor)
                ])
                
                rowStack.addArrangedSubview(cell)
            }
            grid.addArrangedSubview(rowStack)
        }
        
        return grid
    }
    
    private func createCloudView(words: [String], accent: Bool) -> NSView {
        if words.isEmpty {
            let lbl = NSTextField(labelWithString: "Нет слов")
            lbl.textColor = KrabEarTheme.Colors.textSecondary
            lbl.isBordered = false
            lbl.drawsBackground = false
            lbl.isSelectable = false
            return lbl
        }
        
        let text = words.joined(separator: " • ")
        let field = NSTextField(labelWithString: text)
        field.isSelectable = true
        field.isBordered = false
        field.drawsBackground = false
        field.lineBreakMode = .byWordWrapping
        field.maximumNumberOfLines = 0
        field.preferredMaxLayoutWidth = 280
        if accent {
            field.font = KrabEarTheme.Typography.captionMedium
            field.textColor = KrabEarTheme.Colors.accent
        } else {
            field.font = KrabEarTheme.Typography.caption
            field.textColor = KrabEarTheme.Colors.textPrimary
        }
        return field
    }
    
    @objc private func onClose() {
        if let window = self.view.window, let parent = window.sheetParent {
            parent.endSheet(window)
        } else {
            self.dismiss(nil)
        }
    }
}

// MARK: - HistoryPanelController Extension

@MainActor
extension HistoryPanelController {
    
    @objc func onCompareSelected() {
        let selectedIndexes = tableView.selectedRowIndexes
        guard selectedIndexes.count >= 2 else {
            let alert = NSAlert()
            alert.messageText = "Сравнение записей"
            alert.informativeText = "Для сравнения выберите 2 или более записей."
            alert.addButton(withTitle: "OK")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }
        
        let ids: [String] = selectedIndexes.compactMap { row in
            guard row < items.count else { return nil }
            return items[row].id
        }
        guard ids.count >= 2 else {
            showInfoAlert(title: "Сравнение записей", body: "Не удалось определить идентификаторы записей.")
            return
        }
        
        let ipcClient = self.ipcClient
        // AGENT-3: IPC off-main
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                nonisolated(unsafe) let response = try ipcClient.call(
                    method: "compare_recordings",
                    params: ["item_ids": ids]
                )
                nonisolated(unsafe) let result = response["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentCompareSheet(result: result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Сравнение записей",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }
    
    private func presentCompareSheet(result: [String: Any]) {
        if let privacy = result["privacy_mode_active"] as? Bool, privacy == true {
            showInfoAlert(title: "Сравнение записей", body: "Сравнение недоступно в режиме приватности.")
            return
        }
        
        guard let itemsData = result["items"] as? [[String: Any]], !itemsData.isEmpty else {
            showInfoAlert(title: "Сравнение записей", body: "Сравнение недоступно (нет данных).")
            return
        }
        
        let matrix = result["text_similarity_matrix"] as? [[Double]] ?? []
        let commonWords = result["common_words"] as? [String] ?? []
        let uniqueWords = result["unique_words_per_item"] as? [[String]] ?? []
        
        let vc = CompareRecordingsViewController(
            items: itemsData,
            matrix: matrix,
            commonWords: commonWords,
            uniqueWords: uniqueWords
        )
        
        let sheetWindow = NSWindow(contentViewController: vc)
        sheetWindow.styleMask = [.titled, .closable, .resizable]
        sheetWindow.title = "Сравнение записей"
        
        if let hostWindow = self.window {
            hostWindow.beginSheet(sheetWindow, completionHandler: nil)
        }
    }
}
