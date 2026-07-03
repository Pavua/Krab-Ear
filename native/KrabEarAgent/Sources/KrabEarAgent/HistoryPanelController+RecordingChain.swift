/*
 HistoryPanelController+RecordingChain.swift

 «Цепочки записей» (RecordingChainManager) — связывание нескольких записей
 истории в упорядоченную последовательность (многочасовые совещания с
 перерывами, продолжение разговора, серия интервью по одной теме).

 IPC-контракты (KrabEar/backend/recording_chain.py — сверено буква-в-букву):
   - start_chain {name} -> {chain_id} | {ok:false, error, reason:"limit_exceeded"} | {ok:false, error}
     (пустое имя -> ValueError, приходит как thrown error, не inline-ответ)
   - add_to_chain {chain_id, item_id} -> {ok:true} | {ok:false, error, reason:"chain_ended"|"limit_exceeded"} | {ok:false, error}
   - end_chain {chain_id} -> {ok:true} | {ok:false, error}
   - get_chain {chain_id} -> {chain_id, name, created_at, ended_at, item_ids, items, total_duration_sec, total_word_count, [privacy_mode]}
   - list_chains {limit} -> {chains: [{chain_id, name, created_at, ended_at, item_count}]}  (КРАТКАЯ форма)
   - merge_chain_text {chain_id} -> {text}
   - unlink_recording_from_chain {chain_id, item_id} -> {ok:true, removed} | {ok:false, error}

 Паттерны (см. HistoryPanelController+ExportSelection.swift, +TimelineExport.swift,
 +RecordingScheduler.swift, +MeetingMode.swift):
 - ipcClient.call — строго off-main (DispatchQueue.global → DispatchQueue.main). AGENT-3.
 - НЕТ runModal() нигде — NSAlert/NSSavePanel только через presentAlertSheet/presentPanelSheet
   (AlertHelpers.swift) либо hostWindow.beginSheet для отдельного окна (детали цепочки).
 - BackendToast.shared.show — non-modal уведомление об успехе/ошибке/лимитах.
 - KrabEarTheme токены — ноль хардкодных цветов/шрифтов/отступов.
 - Glyph-free: только SF Symbols для иконок, plain RU текст в NSTextField.
 - reason: "limit_exceeded" / "chain_ended" -> человекочитаемое сообщение, не generic error text.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum RecordingChainAssocKeys {
    nonisolated(unsafe) static var listCard: UInt8 = 0
    nonisolated(unsafe) static var nameField: UInt8 = 0
}

extension HistoryPanelController {

    // MARK: - Section builder (History tab)

    /// Строит секцию «Цепочки записей» для History-таба.
    /// Вызывается из setupHistoryTab() в +ApplyTheme+HistoryTab.swift.
    @MainActor
    func setupRecordingChainSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "history_recording_chains",
            title: "Цепочки записей",
            isExpanded: false,
            iconSymbol: "link"
        )

        let card = ThemeCardView()

        // 1. Форма «Начать новую цепочку».
        let nameField = NSTextField(frame: .zero)
        nameField.placeholderString = "Например: Совещание по проекту X"
        nameField.font = KrabEarTheme.Typography.body
        nameField.bezelStyle = .roundedBezel
        nameField.isBordered = true
        nameField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        nameField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        objc_setAssociatedObject(self, &RecordingChainAssocKeys.nameField, nameField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let startButton = ThemePrimaryButton(
            title: "Начать цепочку",
            target: self,
            action: #selector(onStartNewChain)
        )
        startButton.setContentHuggingPriority(.required, for: .horizontal)

        let formRow = NSStackView(views: [nameField, startButton])
        formRow.orientation = .horizontal
        formRow.spacing = KrabEarTheme.Metrics.standard
        formRow.alignment = .centerY

        let mainRow = makeSettingRow(
            label: "Новая цепочка",
            description: "Объедините несколько записей истории в одну упорядоченную последовательность.",
            control: formRow
        )
        card.contentStackView.addArrangedSubview(mainRow)
        card.contentStackView.addArrangedSubview(chainMakeSeparator())

        // 2. Список существующих цепочек (заполняется асинхронно).
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &RecordingChainAssocKeys.listCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        section.contentStackView.addArrangedSubview(card)

        fetchAndRebuildChainListCard()

        return section
    }

    // MARK: - Список цепочек: загрузка + перестройка

    /// Перезагружает list_chains и перестраивает карточку списка. Публичный (internal),
    /// т.к. вызывается и из этого файла, и после успешных мутаций (start/end/unlink).
    func fetchAndRebuildChainListCard() {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let chains: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_chains", params: ["limit": 50])
                let result = resp["result"] as? [String: Any]
                chains = result?["chains"] as? [[String: Any]] ?? []
            } catch {
                chains = []
            }
            DispatchQueue.main.async {
                self?.rebuildChainListCard(chains: chains)
            }
        }
    }

    @MainActor
    private func rebuildChainListCard(chains: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &RecordingChainAssocKeys.listCard) as? ThemeCardView else { return }

        // Сохраняем первые 2 вьюхи (форма добавления + разделитель), остальное перестраиваем.
        let arrangedViews = card.contentStackView.arrangedSubviews
        for v in arrangedViews.dropFirst(2) {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        let subhead = makeSubhead("ЦЕПОЧКИ")
        card.contentStackView.addArrangedSubview(subhead)

        if chains.isEmpty {
            let empty = NSTextField(labelWithString: "Нет ни одной цепочки записей")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(empty)
            return
        }

        let dateFormatter = ISO8601DateFormatter()
        dateFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let dateFormatterNoFrac = ISO8601DateFormatter()
        dateFormatterNoFrac.formatOptions = [.withInternetDateTime]

        let displayFormatter = DateFormatter()
        displayFormatter.dateStyle = .medium
        displayFormatter.timeStyle = .short

        for chain in chains {
            guard let chainId = chain["chain_id"] as? String else { continue }
            let name = chain["name"] as? String ?? "Без названия"
            let itemCount = chain["item_count"] as? Int ?? 0
            let endedAt = chain["ended_at"] as? String
            let createdAtRaw = chain["created_at"] as? String ?? ""

            var displayDate = createdAtRaw
            if let d = dateFormatter.date(from: createdAtRaw) ?? dateFormatterNoFrac.date(from: createdAtRaw) {
                displayDate = displayFormatter.string(from: d)
            }

            let row = makeChainRow(
                chainId: chainId,
                name: name,
                displayDate: displayDate,
                itemCount: itemCount,
                isActive: endedAt == nil
            )
            card.contentStackView.addArrangedSubview(row)
        }
    }

    @MainActor
    private func makeChainRow(chainId: String, name: String, displayDate: String, itemCount: Int, isActive: Bool) -> NSView {
        let nameLabel = NSTextField(labelWithString: name)
        nameLabel.font = NSFont.systemFont(ofSize: 13, weight: .medium)
        nameLabel.textColor = KrabEarTheme.Colors.textPrimary
        nameLabel.lineBreakMode = .byTruncatingTail
        nameLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let metaLabel = NSTextField(labelWithString: "\(displayDate) · записей: \(itemCount)")
        metaLabel.font = KrabEarTheme.Typography.caption
        metaLabel.textColor = KrabEarTheme.Colors.textSecondary

        let textStack = NSStackView(views: [nameLabel, metaLabel])
        textStack.orientation = .vertical
        textStack.alignment = .leading
        textStack.spacing = 2
        textStack.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let statusBadge = makeBadge(
            text: isActive ? "Активна" : "Завершена",
            color: isActive ? KrabEarTheme.Colors.success : KrabEarTheme.Colors.textSecondary,
            symbol: isActive ? "circle.fill" : "checkmark.circle.fill"
        )

        let openButton = NSButton(title: "Открыть", target: self, action: #selector(onOpenChain(_:)))
        openButton.bezelStyle = .inline
        openButton.identifier = NSUserInterfaceItemIdentifier(chainId)
        openButton.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [textStack, statusBadge, openButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.standard
        row.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)
        return row
    }

    // MARK: - Обработчик «Начать цепочку»

    @objc private func onStartNewChain() {
        guard let nameField = objc_getAssociatedObject(self, &RecordingChainAssocKeys.nameField) as? NSTextField else { return }
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            BackendToast.shared.show("Введите имя цепочки", duration: 3.0)
            return
        }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let resp = try ipc.call(method: "start_chain", params: ["name": name])
                let result = resp["result"] as? [String: Any] ?? [:]

                if let ok = result["ok"] as? Bool, !ok {
                    let reason = result["reason"] as? String
                    let message = self?.chainErrorMessage(result: result, reason: reason) ?? "Не удалось создать цепочку"
                    DispatchQueue.main.async {
                        BackendToast.shared.show(message, duration: 4.0)
                    }
                    return
                }

                DispatchQueue.main.async {
                    nameField.stringValue = ""
                    BackendToast.shared.show("Цепочка «\(name)» создана", duration: 2.5)
                }
                self?.fetchAndRebuildChainListCard()
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Сбой создания цепочки: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    // MARK: - Обработчик «Открыть» (детали цепочки)

    @objc private func onOpenChain(_ sender: NSButton) {
        guard let chainId = sender.identifier?.rawValue, !chainId.isEmpty else { return }
        openChainDetail(chainId: chainId)
    }

    /// Загружает get_chain и открывает окно деталей. Используется и после
    /// «Открыть», и после успешного «Добавить в цепочку…» (чтобы сразу увидеть результат).
    func openChainDetail(chainId: String) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let resp = try ipc.call(method: "get_chain", params: ["chain_id": chainId])
                let result = resp["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.presentChainDetail(result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showInfoAlert(
                        title: "Цепочка записей",
                        body: "Ошибка IPC: \(error.localizedDescription)"
                    )
                }
            }
        }
    }

    @MainActor
    private func presentChainDetail(_ result: [String: Any]) {
        guard let chainId = result["chain_id"] as? String else {
            showInfoAlert(title: "Цепочка записей", body: "Цепочка не найдена или бэкенд вернул пустой ответ.")
            return
        }
        let name = result["name"] as? String ?? "Без названия"
        let itemIds = result["item_ids"] as? [String] ?? []
        let items = result["items"] as? [[String: Any]] ?? []
        let endedAt = result["ended_at"] as? String
        let totalDuration = result["total_duration_sec"] as? Double ?? 0.0
        let totalWords = result["total_word_count"] as? Int ?? 0
        let isPrivacyMode = result["privacy_mode"] as? Bool ?? false

        let vc = RecordingChainDetailViewController(
            chainId: chainId,
            name: name,
            itemIds: itemIds,
            items: items,
            isActive: endedAt == nil,
            totalDurationSec: totalDuration,
            totalWordCount: totalWords,
            isPrivacyMode: isPrivacyMode,
            controller: self
        )

        let sheetWindow = NSWindow(contentViewController: vc)
        sheetWindow.styleMask = [.titled, .closable, .resizable]
        sheetWindow.title = "Цепочка записей"
        sheetWindow.setContentSize(NSSize(width: 560, height: 520))

        if let hostWindow = self.window {
            hostWindow.beginSheet(sheetWindow, completionHandler: nil)
        }
    }

    // MARK: - Контекстное меню таблицы истории: «Добавить в цепочку…»

    /// Возвращает NSMenuItem «Добавить в цепочку…» для контекстного меню таблицы истории.
    /// Активен при выборе ≥1 записи (в отличие от single-selection пунктов Quick Actions/Meeting).
    func makeAddToChainMenuItem() -> NSMenuItem {
        let item = NSMenuItem(
            title: "Добавить в цепочку...",
            action: #selector(onAddSelectedToChain),
            keyEquivalent: ""
        )
        item.target = self
        if let icon = NSImage(systemSymbolName: "link.badge.plus", accessibilityDescription: nil) {
            item.image = icon
        }
        return item
    }

    @objc func onAddSelectedToChain() {
        let rows = tableView.selectedRowIndexes
        guard !rows.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "Добавить в цепочку"
            alert.informativeText = "Не выбрано ни одной записи."
            alert.addButton(withTitle: "OK")
            presentAlertSheet(alert, for: self.window) { _ in }
            return
        }

        let ids: [String] = rows.compactMap { idx in
            guard idx < items.count else { return nil }
            return items[idx].id
        }
        guard !ids.isEmpty else { return }

        // Шаг 1: загружаем список цепочек off-main, чтобы предложить ТОЛЬКО активные.
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let resp = try? ipc.call(method: "list_chains", params: ["limit": 50])
            let result = resp?["result"] as? [String: Any] ?? [:]
            let allChains = result["chains"] as? [[String: Any]] ?? []
            let activeChains = allChains.filter { ($0["ended_at"] as? String) == nil }

            DispatchQueue.main.async {
                self?.presentAddToChainSheet(activeChains: activeChains, ids: ids, ipcClient: ipc)
            }
        }
    }

    // MARK: - Add-to-chain sheet (main thread only)

    @MainActor
    private func presentAddToChainSheet(activeChains: [[String: Any]], ids: [String], ipcClient: IPCClient) {
        let existingNames: [String] = activeChains.compactMap { $0["name"] as? String }
        let nameToId: [String: String] = activeChains.reduce(into: [:]) { dict, chain in
            if let name = chain["name"] as? String, let cid = chain["chain_id"] as? String {
                dict[name] = cid
            }
        }

        let alert = NSAlert()
        alert.messageText = "Добавить в цепочку"
        alert.informativeText = "Выберите активную цепочку или введите имя новой (\(ids.count) записей)."
        alert.addButton(withTitle: "Добавить")   // .alertFirstButtonReturn
        alert.addButton(withTitle: "Отмена")

        let comboBox = NSComboBox(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        comboBox.isEditable = true
        for name in existingNames {
            comboBox.addItem(withObjectValue: name)
        }
        alert.accessoryView = comboBox
        alert.window.initialFirstResponder = comboBox

        presentAlertSheet(alert, for: self.window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            let name = comboBox.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty else {
                self?.showInfoAlert(title: "Добавить в цепочку", body: "Имя цепочки не может быть пустым.")
                return
            }

            let existingChainId = nameToId[name]
            let capturedIds = ids

            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                var chainId = existingChainId
                var creationError: String?

                if chainId == nil {
                    do {
                        let resp = try ipcClient.call(method: "start_chain", params: ["name": name])
                        let result = resp["result"] as? [String: Any] ?? [:]
                        if let ok = result["ok"] as? Bool, !ok {
                            creationError = self?.chainErrorMessage(result: result, reason: result["reason"] as? String)
                        } else {
                            chainId = result["chain_id"] as? String
                        }
                    } catch {
                        creationError = "Ошибка создания цепочки: \(error.localizedDescription)"
                    }
                }

                guard let finalChainId = chainId else {
                    DispatchQueue.main.async {
                        BackendToast.shared.show(creationError ?? "Не удалось создать цепочку", duration: 4.0)
                    }
                    return
                }

                // Шаг 2: последовательно добавляем каждую запись (off-main, sequential).
                var added = 0
                var lastFailureMessage: String?
                for id in capturedIds {
                    do {
                        let resp = try ipcClient.call(
                            method: "add_to_chain",
                            params: ["chain_id": finalChainId, "item_id": id]
                        )
                        let result = resp["result"] as? [String: Any] ?? [:]
                        if let ok = result["ok"] as? Bool, !ok {
                            lastFailureMessage = self?.chainErrorMessage(result: result, reason: result["reason"] as? String)
                        } else {
                            added += 1
                        }
                    } catch {
                        lastFailureMessage = "Ошибка IPC: \(error.localizedDescription)"
                    }
                }

                DispatchQueue.main.async {
                    if added == capturedIds.count {
                        BackendToast.shared.show("Добавлено записей: \(added) в цепочку «\(name)»", duration: 3.0)
                    } else if added > 0 {
                        BackendToast.shared.show(
                            "Добавлено: \(added) из \(capturedIds.count). \(lastFailureMessage ?? "")",
                            duration: 4.0
                        )
                    } else {
                        BackendToast.shared.show(
                            lastFailureMessage ?? "Не удалось добавить записи в цепочку",
                            duration: 4.0
                        )
                    }
                }
                self?.fetchAndRebuildChainListCard()
            }
        }
    }

    // MARK: - Человекочитаемые сообщения об ошибках цепочек

    /// Преобразует reason ("limit_exceeded"/"chain_ended") в понятное RU-сообщение.
    /// Используется и секцией списка, и context-menu добавлением, и detail view (unlink/end).
    func chainErrorMessage(result: [String: Any], reason: String?) -> String {
        switch reason {
        case "limit_exceeded":
            return "Достигнут лимит записей в цепочке"
        case "chain_ended":
            return "Цепочка уже завершена"
        default:
            let rawError = result["error"] as? String
            return rawError ?? "Неизвестная ошибка цепочки"
        }
    }

    @MainActor
    private func chainMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}

// MARK: - RecordingChainDetailViewController

/// Окно деталей одной цепочки: список записей (id + дата), «Завершить цепочку»
/// (только для активных), «Копировать объединённый текст», удаление отдельных
/// записей из цепочки.
@MainActor
final class RecordingChainDetailViewController: NSViewController {

    private let chainId: String
    private let chainName: String
    private var itemIds: [String]
    private let itemsDetail: [[String: Any]]
    private var isActive: Bool
    private let totalDurationSec: Double
    private let totalWordCount: Int
    private let isPrivacyMode: Bool
    private weak var controller: HistoryPanelController?

    private let itemsStack = NSStackView()
    private var endChainButton: NSButton?
    private var statusLabel: NSTextField?

    init(
        chainId: String,
        name: String,
        itemIds: [String],
        items: [[String: Any]],
        isActive: Bool,
        totalDurationSec: Double,
        totalWordCount: Int,
        isPrivacyMode: Bool,
        controller: HistoryPanelController
    ) {
        self.chainId = chainId
        self.chainName = name
        self.itemIds = itemIds
        self.itemsDetail = items
        self.isActive = isActive
        self.totalDurationSec = totalDurationSec
        self.totalWordCount = totalWordCount
        self.isPrivacyMode = isPrivacyMode
        self.controller = controller
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = KrabEarTheme.Colors.windowBackground.cgColor
        self.view = root

        let outerStack = NSStackView()
        outerStack.orientation = .vertical
        outerStack.spacing = KrabEarTheme.Metrics.comfortable
        outerStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.spacious,
            left: KrabEarTheme.Metrics.spacious,
            bottom: KrabEarTheme.Metrics.spacious,
            right: KrabEarTheme.Metrics.spacious
        )
        outerStack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(outerStack)
        NSLayoutConstraint.activate([
            outerStack.topAnchor.constraint(equalTo: root.topAnchor),
            outerStack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            outerStack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            outerStack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        let contentWidth: CGFloat = 512

        // Title + meta.
        let titleLabel = makeLabel(text: chainName, font: KrabEarTheme.Typography.display, color: KrabEarTheme.Colors.textPrimary)
        titleLabel.maximumNumberOfLines = 2

        let statusLabel = makeLabel(
            text: buildMetaString(),
            font: KrabEarTheme.Typography.caption,
            color: KrabEarTheme.Colors.textSecondary
        )
        self.statusLabel = statusLabel

        let titleRow = NSStackView(views: [titleLabel, statusLabel])
        titleRow.orientation = .vertical
        titleRow.spacing = KrabEarTheme.Metrics.tight
        titleRow.alignment = .leading
        outerStack.addArrangedSubview(titleRow)

        outerStack.addArrangedSubview(makeSeparator())

        if isPrivacyMode {
            let privacyLabel = makeLabel(
                text: "Содержимое записей скрыто в режиме приватности.",
                font: KrabEarTheme.Typography.body,
                color: KrabEarTheme.Colors.textSecondary
            )
            privacyLabel.maximumNumberOfLines = 0
            privacyLabel.preferredMaxLayoutWidth = contentWidth
            outerStack.addArrangedSubview(privacyLabel)
        } else {
            // Список записей внутри scroll view.
            itemsStack.orientation = .vertical
            itemsStack.spacing = KrabEarTheme.Metrics.tight
            itemsStack.alignment = .leading
            itemsStack.translatesAutoresizingMaskIntoConstraints = false
            rebuildItemsStack()

            let scrollView = NSScrollView()
            scrollView.hasVerticalScroller = true
            scrollView.autohidesScrollers = true
            scrollView.drawsBackground = false
            scrollView.translatesAutoresizingMaskIntoConstraints = false

            let clipView = scrollView.contentView
            scrollView.documentView = itemsStack
            NSLayoutConstraint.activate([
                itemsStack.widthAnchor.constraint(equalTo: clipView.widthAnchor),
            ])
            scrollView.heightAnchor.constraint(equalToConstant: 300).isActive = true
            outerStack.addArrangedSubview(scrollView)
            scrollView.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        }

        outerStack.addArrangedSubview(makeSeparator())

        // Button row.
        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.spacing = KrabEarTheme.Metrics.standard
        buttonRow.alignment = .centerY

        let closeBtn = ThemeSecondaryButton(title: "Закрыть", target: self, action: #selector(onClose))
        buttonRow.addArrangedSubview(closeBtn)

        let copyBtn = ThemeSecondaryButton(title: "Копировать объединённый текст", target: self, action: #selector(onCopyMergedText))
        copyBtn.applyThemeSecondary()
        buttonRow.addArrangedSubview(copyBtn)

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        buttonRow.addArrangedSubview(spacer)

        if isActive {
            let endBtn = ThemePrimaryButton(title: "Завершить цепочку", target: self, action: #selector(onEndChain))
            buttonRow.addArrangedSubview(endBtn)
            self.endChainButton = endBtn
        }

        outerStack.addArrangedSubview(buttonRow)

        for v in [titleRow, buttonRow] {
            v.widthAnchor.constraint(equalToConstant: contentWidth).isActive = true
        }
    }

    // MARK: - Items list

    private func rebuildItemsStack() {
        for v in itemsStack.arrangedSubviews {
            itemsStack.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if itemIds.isEmpty {
            let empty = makeLabel(text: "В цепочке пока нет записей", font: KrabEarTheme.Typography.caption, color: KrabEarTheme.Colors.textSecondary)
            itemsStack.addArrangedSubview(empty)
            return
        }

        let dateFormatter = ISO8601DateFormatter()
        dateFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let dateFormatterNoFrac = ISO8601DateFormatter()
        dateFormatterNoFrac.formatOptions = [.withInternetDateTime]
        let displayFormatter = DateFormatter()
        displayFormatter.dateStyle = .short
        displayFormatter.timeStyle = .short

        // itemsDetail хранит полные dict-объекты в том же порядке, что и itemIds
        // (см. RecordingChainManager.get_chain — items_detail строится по item_ids).
        let itemsById: [String: [String: Any]] = itemsDetail.reduce(into: [:]) { dict, item in
            if let id = item["id"] as? String {
                dict[id] = item
            }
        }

        for itemId in itemIds {
            let detail = itemsById[itemId]
            let ts = detail?["ts"] as? String ?? ""
            var displayDate = ts
            if !ts.isEmpty, let d = dateFormatter.date(from: ts) ?? dateFormatterNoFrac.date(from: ts) {
                displayDate = displayFormatter.string(from: d)
            }
            let text = detail?["text"] as? String ?? ""
            let preview = text.isEmpty ? "(текст недоступен)" : String(text.prefix(80)).replacingOccurrences(of: "\n", with: " ")

            itemsStack.addArrangedSubview(makeItemRow(itemId: itemId, displayDate: displayDate, preview: preview))
        }
    }

    private func makeItemRow(itemId: String, displayDate: String, preview: String) -> NSView {
        let dateLabel = makeLabel(text: displayDate.isEmpty ? itemId : displayDate, font: KrabEarTheme.Typography.captionMedium, color: KrabEarTheme.Colors.textSecondary)
        dateLabel.translatesAutoresizingMaskIntoConstraints = false
        dateLabel.widthAnchor.constraint(equalToConstant: 120).isActive = true

        let previewLabel = makeLabel(text: preview, font: KrabEarTheme.Typography.body, color: KrabEarTheme.Colors.textPrimary)
        previewLabel.lineBreakMode = .byTruncatingTail
        previewLabel.maximumNumberOfLines = 1
        previewLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let removeButton = NSButton(title: "Убрать", target: self, action: #selector(onRemoveItem(_:)))
        removeButton.bezelStyle = .inline
        removeButton.identifier = NSUserInterfaceItemIdentifier(itemId)
        removeButton.setContentHuggingPriority(.required, for: .horizontal)
        removeButton.isEnabled = isActive

        let row = NSStackView(views: [dateLabel, previewLabel, removeButton])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.standard
        row.edgeInsets = NSEdgeInsets(top: 2, left: 0, bottom: 2, right: 0)
        return row
    }

    // MARK: - Actions

    @objc private func onRemoveItem(_ sender: NSButton) {
        guard let itemId = sender.identifier?.rawValue, !itemId.isEmpty, let ipc = controller?.ipcClient else { return }
        let capturedChainId = chainId

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let resp = try ipc.call(
                    method: "unlink_recording_from_chain",
                    params: ["chain_id": capturedChainId, "item_id": itemId]
                )
                let result = resp["result"] as? [String: Any] ?? [:]
                let ok = result["ok"] as? Bool ?? false
                let removed = result["removed"] as? Bool ?? false

                DispatchQueue.main.async {
                    guard let self else { return }
                    if ok {
                        self.itemIds.removeAll { $0 == itemId }
                        self.rebuildItemsStack()
                        BackendToast.shared.show(removed ? "Запись убрана из цепочки" : "Запись уже отсутствовала в цепочке", duration: 2.5)
                        self.controller?.fetchAndRebuildChainListCard()
                    } else {
                        let message = self.controller?.chainErrorMessage(result: result, reason: result["reason"] as? String) ?? "Не удалось убрать запись"
                        BackendToast.shared.show(message, duration: 4.0)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка IPC: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    @objc private func onEndChain() {
        guard let ipc = controller?.ipcClient else { return }
        let capturedChainId = chainId

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let resp = try ipc.call(method: "end_chain", params: ["chain_id": capturedChainId])
                let result = resp["result"] as? [String: Any] ?? [:]
                let ok = result["ok"] as? Bool ?? false

                DispatchQueue.main.async {
                    guard let self else { return }
                    if ok {
                        self.isActive = false
                        self.endChainButton?.isHidden = true
                        self.statusLabel?.stringValue = self.buildMetaString()
                        self.rebuildItemsStack()
                        BackendToast.shared.show("Цепочка завершена", duration: 2.5)
                        self.controller?.fetchAndRebuildChainListCard()
                    } else {
                        let message = self.controller?.chainErrorMessage(result: result, reason: result["reason"] as? String) ?? "Не удалось завершить цепочку"
                        BackendToast.shared.show(message, duration: 4.0)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка IPC: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    @objc private func onCopyMergedText() {
        guard let ipc = controller?.ipcClient else { return }
        let capturedChainId = chainId

        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let resp = try ipc.call(method: "merge_chain_text", params: ["chain_id": capturedChainId])
                let result = resp["result"] as? [String: Any] ?? [:]
                let text = result["text"] as? String ?? ""

                DispatchQueue.main.async {
                    if text.isEmpty {
                        BackendToast.shared.show("Недоступно в режиме приватности или текст пуст", duration: 3.5)
                    } else {
                        let pb = NSPasteboard.general
                        pb.clearContents()
                        pb.setString(text, forType: .string)
                        BackendToast.shared.show("Объединённый текст скопирован", duration: 2.5)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка IPC: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    @objc private func onClose() {
        if let window = view.window, let parent = window.sheetParent {
            parent.endSheet(window)
        } else {
            dismiss(nil)
        }
    }

    // MARK: - View helpers

    private func makeLabel(text: String, font: NSFont, color: NSColor) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = font
        label.textColor = color
        label.isBordered = false
        label.drawsBackground = false
        label.isSelectable = false
        label.lineBreakMode = .byWordWrapping
        label.maximumNumberOfLines = 0
        return label
    }

    private func makeSeparator() -> NSView {
        let line = NSBox()
        line.boxType = .separator
        line.translatesAutoresizingMaskIntoConstraints = false
        return line
    }

    private func buildMetaString() -> String {
        var parts: [String] = []
        parts.append(isActive ? "Активна" : "Завершена")
        parts.append("записей: \(itemIds.count)")
        if totalDurationSec > 0 {
            parts.append("\(HistoryPanelController.formatDuration(totalDurationSec))")
        }
        if totalWordCount > 0 {
            parts.append("\(totalWordCount) слов")
        }
        return parts.joined(separator: " · ")
    }
}
