import Cocoa

private nonisolated(unsafe) var statsTextViewKey: UInt8 = 0
private nonisolated(unsafe) var collectionPopUpKey: UInt8 = 0
private nonisolated(unsafe) var speakerTextFieldKey: UInt8 = 0

extension HistoryPanelController {
    
    @MainActor
    private var statsTextView: NSTextView? {
        get { objc_getAssociatedObject(self, &statsTextViewKey) as? NSTextView }
        set { objc_setAssociatedObject(self, &statsTextViewKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }
    
    @MainActor
    private var collectionPopUp: NSPopUpButton? {
        get { objc_getAssociatedObject(self, &collectionPopUpKey) as? NSPopUpButton }
        set { objc_setAssociatedObject(self, &collectionPopUpKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }
    
    @MainActor
    private var speakerTextField: NSTextField? {
        get { objc_getAssociatedObject(self, &speakerTextFieldKey) as? NSTextField }
        set { objc_setAssociatedObject(self, &speakerTextFieldKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }
    
    @MainActor
    public func setupManagementSections() -> (CollapsibleSectionView, CollapsibleSectionView) {
        
        // --- Раздел 1: Управление ---
        let managementSection = CollapsibleSectionView(sectionId: "history_management", title: "Управление", isExpanded: false)
        
        let favButton = NSButton(title: "В избранное", target: self, action: #selector(handleToggleFavorite))
        favButton.bezelStyle = .push
        managementSection.contentStackView.addArrangedSubview(createRow(views: [favButton]))
        
        let backupButton = NSButton(title: "Бэкап", target: self, action: #selector(handleBackupHistory))
        backupButton.bezelStyle = .push
        managementSection.contentStackView.addArrangedSubview(createRow(views: [backupButton]))
        
        let versionsButton = NSButton(title: "Версии", target: self, action: #selector(handleGetVersions))
        versionsButton.bezelStyle = .push
        managementSection.contentStackView.addArrangedSubview(createRow(views: [versionsButton]))
        
        let popUp = NSPopUpButton(frame: .zero, pullsDown: false)
        popUp.addItems(withTitles: ["Коллекция 1", "Коллекция 2", "Архив"])
        self.collectionPopUp = popUp
        let addCollectionButton = NSButton(title: "Добавить", target: self, action: #selector(handleAddCollection))
        addCollectionButton.bezelStyle = .push
        managementSection.contentStackView.addArrangedSubview(createRow(views: [popUp, addCollectionButton]))
        
        let speakerField = NSTextField(string: "SPEAKER_00")
        speakerField.widthAnchor.constraint(equalToConstant: 100).isActive = true
        self.speakerTextField = speakerField
        let searchSpeakerButton = NSButton(title: "Найти", target: self, action: #selector(handleSearchSpeaker))
        searchSpeakerButton.bezelStyle = .push
        managementSection.contentStackView.addArrangedSubview(createRow(views: [speakerField, searchSpeakerButton]))
        
        // --- Раздел 2: Статистика ---
        let statsSection = CollapsibleSectionView(sectionId: "history_stats", title: "Статистика", isExpanded: false)
        
        let freqButton = NSButton(title: "Частотный анализ", target: self, action: #selector(handleWordFrequency))
        freqButton.bezelStyle = .push
        statsSection.contentStackView.addArrangedSubview(createRow(views: [freqButton]))
        
        let statsButton = NSButton(title: "Статистика", target: self, action: #selector(handleGetStatistics))
        statsButton.bezelStyle = .push
        statsSection.contentStackView.addArrangedSubview(createRow(views: [statsButton]))
        
        let topicsButton = NSButton(title: "Темы", target: self, action: #selector(handleGetTopics))
        topicsButton.bezelStyle = .push
        statsSection.contentStackView.addArrangedSubview(createRow(views: [topicsButton]))
        
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        
        let textView = NSTextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.textContainer?.widthTracksTextView = true
        textView.font = .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        
        scrollView.documentView = textView
        scrollView.heightAnchor.constraint(equalToConstant: 100).isActive = true
        self.statsTextView = textView
        
        statsSection.contentStackView.addArrangedSubview(scrollView)
        
        return (managementSection, statsSection)
    }
    
    @MainActor
    private func createRow(views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 8
        stack.alignment = .centerY
        return stack
    }
    
    // MARK: - Actions (Управление)
    
    @objc private func handleToggleFavorite() {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            showDiagnosticsOutput("Выберите запись в таблице")
            return
        }
        let item = items[row]
        
        executeIPC(method: "toggle_favorite", params: ["id": item.id]) { [weak self] response in
            self?.showDiagnosticsOutput(response)
        }
    }
    
    @objc private func handleBackupHistory() {
        executeIPC(method: "backup_history") { [weak self] response in
            self?.showDiagnosticsOutput(response)
        }
    }
    
    @objc private func handleGetVersions() {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            showDiagnosticsOutput("Выберите запись в таблице")
            return
        }
        let item = items[row]
        
        executeIPC(method: "get_transcript_versions", params: ["item_id": item.id]) { [weak self] response in
            self?.showDiagnosticsOutput(response)
        }
    }
    
    @objc private func handleAddCollection() {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            showDiagnosticsOutput("Выберите запись в таблице")
            return
        }
        let item = items[row]
        let collection = collectionPopUp?.titleOfSelectedItem ?? ""
        
        executeIPC(method: "add_to_collection", params: ["id": item.id, "collection": collection]) { [weak self] response in
            self?.showDiagnosticsOutput(response)
        }
    }
    
    @objc private func handleSearchSpeaker() {
        let speaker = speakerTextField?.stringValue ?? ""
        executeIPC(method: "search_by_speaker", params: ["speaker": speaker]) { [weak self] response in
            self?.showDiagnosticsOutput(response)
        }
    }
    
    // MARK: - Actions (Статистика)
    
    @objc private func handleWordFrequency() {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            showDiagnosticsOutput("Выберите запись в таблице")
            return
        }
        let item = items[row]
        
        executeIPC(method: "word_frequency_analysis", params: ["id": item.id]) { [weak self] response in
            self?.statsTextView?.string = response
        }
    }
    
    @objc private func handleGetStatistics() {
        executeIPC(method: "get_history_statistics") { [weak self] response in
            self?.statsTextView?.string = response
        }
    }
    
    @objc private func handleGetTopics() {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else {
            showDiagnosticsOutput("Выберите запись в таблице")
            return
        }
        let item = items[row]
        
        executeIPC(method: "get_topic_timeline", params: ["id": item.id]) { [weak self] response in
            self?.statsTextView?.string = response
        }
    }
    
    // MARK: - IPC Helper
    
    private func executeIPC(method: String, params: [String: Any] = [:], completion: @escaping @MainActor (String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try self.ipcClient.call(method: method, params: params)
                let output = response.description
                DispatchQueue.main.async {
                    completion(output)
                }
            } catch {
                DispatchQueue.main.async {
                    completion("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
}
