import AppKit

/// Контроллер вкладки «Архив».
/// Выполнен с использованием Liquid Glass aesthetic (через KrabEarTheme).
/// Component Hierarchy:
/// - NSView (view)
///   - NSStackView (mainStack, vertical)
///     - NSStackView (statsBar, horizontal)
///       - NSTextField (statsLabel: "N записей | X MB | старейшая: DD.MM")
///       - NSView (spacer)
///     - NSScrollView (tableScroll)
///       - NSTableView (tableView) -> NSTableCellView -> NSStackView (row content)
///     - NSStackView (emptyStateContainer, vertical)
///       - NSImageView (emptyIcon)
///       - NSTextField (emptyLabel: "Архив пуст")
///     - NSStackView (privacyStateContainer, vertical)
///       - NSImageView (privacyIcon: "lock.fill")
///       - NSTextField (privacyLabel: "Недоступно в приватном режиме")
final class ArchiveTabViewController: NSViewController, NSTableViewDataSource, NSTableViewDelegate {
    
    // UI Components
    private let mainStack = NSStackView()
    private let statsBar = NSStackView()
    // Wave 621/658 (AGENT-J): no dangerous Unicode glyphs in NSTextField(labelWithString:)
    // Use " | " separator instead of the bullet glyph (U+2022) which triggers CoreText hang.
    private let statsLabel = NSTextField(labelWithString: "0 записей | 0 MB | старейшая: --.--")
    
    private let tableScroll = NSScrollView()
    private let tableView = NSTableView()
    
    private let emptyStateContainer = NSStackView()
    private let privacyStateContainer = NSStackView()
    
    // State
    var isPrivacyModeEnabled: Bool = false {
        didSet { updateVisibility() }
    }
    
    var archivedItems: [HistoryItem] = [] {
        didSet {
            tableView.reloadData()
            updateStatsBar()
            updateVisibility()
        }
    }
    
    typealias HistoryItem = ArchivedEntry

    /// Канал IPC (задаёт HistoryPanelController). Без него вкладка не пытается грузить архив.
    var ipcProvider: (() -> IPCClient?)?
    /// Поколение запроса списка: опоздавший ответ предыдущего reload() не затирает свежий.
    private var loadGeneration = 0
    /// Записи, чьё восстановление уже идёт (защита от двойного клика).
    private(set) var restoringIDs = Set<String>()
    private var ledger = ArchiveWarningLedger()
    private var transientMessage: String?
    private let noticeLabel = NSTextField(labelWithString: "")

    init() {
        super.init(nibName: nil, bundle: nil)
    }
    
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
    
    override func loadView() {
        self.view = NSView()
        self.view.wantsLayer = true
        
        setupComponentHierarchy()
        applyTokensAndRoles()
        updateVisibility()
    }
    
    private func setupComponentHierarchy() {
        mainStack.orientation = .vertical
        mainStack.spacing = KrabEarTheme.Metrics.standard
        mainStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.comfortable
        )
        mainStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(mainStack)
        
        NSLayoutConstraint.activate([
            mainStack.topAnchor.constraint(equalTo: view.topAnchor),
            mainStack.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            mainStack.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            mainStack.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])
        
        // Stats bar
        statsBar.orientation = .horizontal
        statsBar.spacing = KrabEarTheme.Metrics.standard
        statsBar.addArrangedSubview(statsLabel)
        
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        statsBar.addArrangedSubview(spacer)
        
        mainStack.addArrangedSubview(statsBar)

        // Строка состояния: итог восстановления / предупреждение / ошибка загрузки.
        noticeLabel.isHidden = true
        noticeLabel.lineBreakMode = .byWordWrapping
        noticeLabel.maximumNumberOfLines = 3
        mainStack.addArrangedSubview(noticeLabel)
        
        // TableView
        tableScroll.hasVerticalScroller = true
        tableScroll.drawsBackground = false
        
        tableView.dataSource = self
        tableView.delegate = self
        tableView.headerView = nil
        tableView.backgroundColor = .clear
        tableView.style = .plain
        
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("ArchiveColumn"))
        column.resizingMask = .autoresizingMask
        tableView.addTableColumn(column)
        
        tableScroll.documentView = tableView
        mainStack.addArrangedSubview(tableScroll)
        
        // Empty State
        emptyStateContainer.orientation = .vertical
        emptyStateContainer.spacing = KrabEarTheme.Metrics.tight
        emptyStateContainer.alignment = .centerX
        let emptyIcon = NSImageView(image: NSImage(systemSymbolName: "archivebox", accessibilityDescription: nil) ?? NSImage())
        emptyIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 48, weight: .regular)
        emptyIcon.contentTintColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.5)
        let emptyLabel = NSTextField(labelWithString: "Архив пуст")
        emptyStateContainer.addArrangedSubview(emptyIcon)
        emptyLabel.alignment = .center
        emptyStateContainer.addArrangedSubview(emptyLabel)
        emptyStateContainer.isHidden = true
        
        // Overlay constraint trick: place it in the center of mainStack
        view.addSubview(emptyStateContainer)
        emptyStateContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            emptyStateContainer.centerXAnchor.constraint(equalTo: tableScroll.centerXAnchor),
            emptyStateContainer.centerYAnchor.constraint(equalTo: tableScroll.centerYAnchor),
            // Без ограничения ширины центрированный блок выходил за край узкого окна.
            emptyStateContainer.widthAnchor.constraint(lessThanOrEqualTo: tableScroll.widthAnchor,
                                          constant: -2 * KrabEarTheme.Metrics.comfortable)
        ])
        
        // Privacy State
        privacyStateContainer.orientation = .vertical
        privacyStateContainer.spacing = KrabEarTheme.Metrics.tight
        privacyStateContainer.alignment = .centerX
        let privacyIcon = NSImageView(image: NSImage(systemSymbolName: "lock.fill", accessibilityDescription: nil) ?? NSImage())
        privacyIcon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 48, weight: .regular)
        privacyIcon.contentTintColor = KrabEarTheme.Colors.textSecondary
        let privacyLabel = NSTextField(labelWithString: "Недоступно в приватном режиме")
        privacyStateContainer.addArrangedSubview(privacyIcon)
        privacyLabel.alignment = .center
        privacyStateContainer.addArrangedSubview(privacyLabel)
        privacyStateContainer.isHidden = true
        
        view.addSubview(privacyStateContainer)
        privacyStateContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            privacyStateContainer.centerXAnchor.constraint(equalTo: tableScroll.centerXAnchor),
            privacyStateContainer.centerYAnchor.constraint(equalTo: tableScroll.centerYAnchor),
            // Без ограничения ширины центрированный блок выходил за край узкого окна.
            privacyStateContainer.widthAnchor.constraint(lessThanOrEqualTo: tableScroll.widthAnchor,
                                          constant: -2 * KrabEarTheme.Metrics.comfortable)
        ])
    }
    
    private func applyTokensAndRoles() {
        // Typography
        statsLabel.font = KrabEarTheme.Typography.captionMedium.tabular()
        statsLabel.textColor = KrabEarTheme.Colors.textSecondary
        
        if let emptyLabel = emptyStateContainer.arrangedSubviews.last as? NSTextField {
            emptyLabel.font = KrabEarTheme.Typography.body
            emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
        }
        
        if let privacyLabel = privacyStateContainer.arrangedSubviews.last as? NSTextField {
            privacyLabel.font = KrabEarTheme.Typography.body
            privacyLabel.textColor = KrabEarTheme.Colors.textSecondary
        }
    }
    
    private func updateVisibility() {
        if isPrivacyModeEnabled {
            tableScroll.isHidden = true
            emptyStateContainer.isHidden = true
            privacyStateContainer.isHidden = false
            statsBar.isHidden = true
        } else if archivedItems.isEmpty {
            tableScroll.isHidden = true
            emptyStateContainer.isHidden = false
            privacyStateContainer.isHidden = true
            statsBar.isHidden = true
        } else {
            tableScroll.isHidden = false
            emptyStateContainer.isHidden = true
            privacyStateContainer.isHidden = true
            statsBar.isHidden = false
        }
    }
    
    private func updateStatsBar() {
        guard !archivedItems.isEmpty else { return }
        
        let totalBytes = archivedItems.reduce(0) { $0 + $1.sizeBytes }
        let mb = Double(totalBytes) / 1024.0 / 1024.0
        
        let oldest = archivedItems.min(by: { $0.date < $1.date })?.date
        let formatter = DateFormatter()
        formatter.dateFormat = "dd.MM"
        let oldestStr = oldest != nil ? formatter.string(from: oldest!) : "--.--"
        
        // Без U+2022: этот глиф вешает CoreText (Wave 621/658), разделитель — " | ".
        statsLabel.stringValue = "\(archivedItems.count) записей | \(String(format: "%.1f", mb)) MB | старейшая: \(oldestStr)"
    }
    
    // MARK: - NSTableViewDataSource & Delegate
    
    func numberOfRows(in tableView: NSTableView) -> Int {
        return archivedItems.count
    }
    
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = archivedItems[row]
        let cellID = NSUserInterfaceItemIdentifier("ArchiveCell")
        
        var cell = tableView.makeView(withIdentifier: cellID, owner: self) as? NSTableCellView
        if cell == nil {
            cell = NSTableCellView()
            cell?.identifier = cellID
            
            let stack = NSStackView()
            stack.orientation = .horizontal
            stack.spacing = KrabEarTheme.Metrics.standard
            stack.alignment = .centerY
            stack.translatesAutoresizingMaskIntoConstraints = false
            
            // Color Role: Archived Item -> muted text, greyed background
            stack.wantsLayer = true
            stack.layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
            stack.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
            
            let label = NSTextField(labelWithString: "")
            label.font = KrabEarTheme.Typography.body
            label.textColor = KrabEarTheme.Colors.textSecondary // Muted role
            label.lineBreakMode = .byTruncatingTail
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            
            // Button "Восстановить"
            let unarchiveButton = NSButton(title: "Восстановить", target: self, action: #selector(onUnarchiveClicked(_:)))
            unarchiveButton.bezelStyle = .inline
            unarchiveButton.controlSize = .small
            
            stack.addArrangedSubview(label)
            stack.addArrangedSubview(unarchiveButton)
            
            cell?.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.topAnchor.constraint(equalTo: cell!.topAnchor, constant: 4),
                stack.leadingAnchor.constraint(equalTo: cell!.leadingAnchor, constant: 8),
                stack.trailingAnchor.constraint(equalTo: cell!.trailingAnchor, constant: -8),
                stack.bottomAnchor.constraint(equalTo: cell!.bottomAnchor, constant: -4)
            ])
            
            // Tag label for later update
            label.tag = 100
        }
        
        if let stack = cell?.subviews.first as? NSStackView,
           let label = stack.viewWithTag(100) as? NSTextField {
            label.stringValue = item.text
            
            // Кнопка привязана к ID записи, а не к номеру строки: после обновления списка строки сдвигаются.
            if let button = stack.arrangedSubviews.last as? NSButton {
                button.identifier = NSUserInterfaceItemIdentifier(item.id)
                button.isEnabled = !restoringIDs.contains(item.id)
            }
        }
        
        return cell
    }
    
    @objc private func onUnarchiveClicked(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue, !id.isEmpty else { return }
        restore(id: id)
    }

    // MARK: - Загрузка и восстановление (IPC вне main: sync read() на main = AppHang)

    /// Перечитывает архив. Вызывается при показе вкладки и после восстановления.
    /// Не гейтится кэш-флагом приватности: решает backend (`privacy_mode_active`),
    /// иначе выключение приватности после блокировки не сняло бы заглушку (ревью #2283).
    func reload() {
        guard let ipc = ipcProvider?() else { return }
        loadGeneration += 1
        let generation = loadGeneration
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(method: "list_archived", params: ["limit": 500], timeoutSec: 5)
            DispatchQueue.main.async {
                guard let self, generation == self.loadGeneration else { return }
                self.apply(listOutcome: ArchiveListDecoder.decode(response))
            }
        }
    }

    func apply(listOutcome: ArchiveListOutcome) {
        switch listOutcome {
        case .items(let entries):
            isPrivacyModeEnabled = false
            archivedItems = entries
            ledger.reconcile(archiveIDs: Set(entries.map { $0.id }))
        case .privacyBlocked:
            isPrivacyModeEnabled = true
            archivedItems = []
            ledger.reset()
            transientMessage = nil
        case .unavailable:
            transientMessage = "Не удалось загрузить архив. Показан прежний список."
        }
        updateNotice()
    }

    func restore(id: String) {
        guard !restoringIDs.contains(id), let ipc = ipcProvider?() else { return }
        restoringIDs.insert(id)
        tableView.reloadData()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(method: "unarchive_items", params: ["item_ids": [id]], timeoutSec: 15)
            DispatchQueue.main.async {
                self?.finishRestore(id: id, response: response)
            }
        }
    }

    /// Только для тестов: пометить запись «восстанавливается» без IPC.
    func beginRestoreForTesting(id: String) {
        restoringIDs.insert(id)
        tableView.reloadData()
    }

    /// Завершение восстановления на main: снимает «в процессе» и перерисовывает таблицу
    /// независимо от того, удастся ли следующий `list_archived` (иначе при сбое списка
    /// кнопка остаётся серой и повтор невозможен — ревью Codex #2283).
    func finishRestore(id: String, response: [String: Any]?) {
        restoringIDs.remove(id)
        tableView.reloadData()
        apply(restoreOutcome: ArchiveRestoreDecoder.decode(response, requested: [id]))
        reload()
    }

    func apply(restoreOutcome: ArchiveRestoreOutcome) {
        ledger.record(restoreOutcome)
        switch restoreOutcome {
        case .complete(let restored):
            transientMessage = "Восстановлено записей: \(restored.count)."
        case .partial:
            transientMessage = nil  // предупреждение строится из реестра по ID
        case .rejected(let reason):
            transientMessage = "Восстановление отклонено (\(reason))."
        case .unknown:
            transientMessage = "Нет подтверждения восстановления от backend. Проверьте список."
        }
        updateNotice()
    }

    private func updateNotice() {
        if !ledger.isClear {
            let ids = ledger.unresolved.sorted().prefix(3).map { String($0.prefix(8)) }.joined(separator: ", ")
            noticeLabel.stringValue = "Не восстановлено записей: \(ledger.unresolved.count) (\(ids)). Они остались в архиве, повторите попытку."
            noticeLabel.isHidden = false
        } else if let message = transientMessage {
            noticeLabel.stringValue = message
            noticeLabel.isHidden = false
        } else {
            noticeLabel.stringValue = ""
            noticeLabel.isHidden = true
        }
    }

    /// Для тестов: текущий текст строки состояния и видимость.
    var noticeTextForTesting: String? { noticeLabel.isHidden ? nil : noticeLabel.stringValue }
}
