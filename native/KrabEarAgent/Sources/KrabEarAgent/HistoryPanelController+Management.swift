import Cocoa

private struct ManagementAssociatedKeys {
    nonisolated(unsafe) static var annotationTextField = "annotationTextField"
    nonisolated(unsafe) static var collectionsPopUp = "collectionsPopUp"
    nonisolated(unsafe) static var speakerTextField = "speakerTextField"
    nonisolated(unsafe) static var statsPopUp = "statsPopUp"
    nonisolated(unsafe) static var statsTextView = "statsTextView"
}

@MainActor
extension HistoryPanelController {

    private var annotationTextField: NSTextField? {
        get { objc_getAssociatedObject(self, &ManagementAssociatedKeys.annotationTextField) as? NSTextField }
        set { objc_setAssociatedObject(self, &ManagementAssociatedKeys.annotationTextField, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    private var collectionsPopUp: NSPopUpButton? {
        get { objc_getAssociatedObject(self, &ManagementAssociatedKeys.collectionsPopUp) as? NSPopUpButton }
        set { objc_setAssociatedObject(self, &ManagementAssociatedKeys.collectionsPopUp, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    private var speakerTextField: NSTextField? {
        get { objc_getAssociatedObject(self, &ManagementAssociatedKeys.speakerTextField) as? NSTextField }
        set { objc_setAssociatedObject(self, &ManagementAssociatedKeys.speakerTextField, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    private var statsPopUp: NSPopUpButton? {
        get { objc_getAssociatedObject(self, &ManagementAssociatedKeys.statsPopUp) as? NSPopUpButton }
        set { objc_setAssociatedObject(self, &ManagementAssociatedKeys.statsPopUp, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    private var statsTextView: NSTextView? {
        get { objc_getAssociatedObject(self, &ManagementAssociatedKeys.statsTextView) as? NSTextView }
        set { objc_setAssociatedObject(self, &ManagementAssociatedKeys.statsTextView, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    private func getSelectedID() -> String? {
        let row = tableView.selectedRow
        guard row >= 0, row < items.count else { return nil }
        return items[row].id
    }

    private func makeHorizontalStack(views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 8
        stack.alignment = .centerY
        stack.distribution = .fillProportionally
        return stack
    }

    func setupManagementSections() -> (management: CollapsibleSectionView, stats: CollapsibleSectionView) {

        // --- SECTION 1: Управление ---

        // Row 1
        let btnFavorite = NSButton(title: "В избранное", target: self, action: #selector(toggleFavoriteAction))
        let btnFavoritesList = NSButton(title: "Избранное", target: self, action: #selector(getFavoritesAction))
        let row1 = makeHorizontalStack(views: [btnFavorite, btnFavoritesList])

        // Row 2
        let txtAnnotation = NSTextField()
        txtAnnotation.placeholderString = "Заметка..."
        txtAnnotation.setContentHuggingPriority(.defaultLow, for: .horizontal)
        self.annotationTextField = txtAnnotation
        let btnSaveAnnotation = NSButton(title: "Сохранить", target: self, action: #selector(setAnnotationAction))
        let row2 = makeHorizontalStack(views: [txtAnnotation, btnSaveAnnotation])

        // Row 3
        let btnBackup = NSButton(title: "Бэкап", target: self, action: #selector(backupHistoryAction))
        let btnListBackups = NSButton(title: "Список копий", target: self, action: #selector(listBackupsAction))
        let row3 = makeHorizontalStack(views: [btnBackup, btnListBackups])

        // Row 4
        let btnVersions = NSButton(title: "Версии", target: self, action: #selector(getTranscriptVersionsAction))
        let btnSaveVersion = NSButton(title: "Сохранить версию", target: self, action: #selector(saveTranscriptVersionAction))
        let row4 = makeHorizontalStack(views: [btnVersions, btnSaveVersion])

        // Row 5
        let popUpCollections = NSPopUpButton()
        popUpCollections.addItems(withTitles: ["General", "Work", "Ideas"])
        self.collectionsPopUp = popUpCollections
        let btnAddCollection = NSButton(title: "Добавить", target: self, action: #selector(addToCollectionAction))
        let btnNewCollection = NSButton(title: "Новая", target: self, action: #selector(createCollectionAction))
        let row5 = makeHorizontalStack(views: [popUpCollections, btnAddCollection, btnNewCollection])

        // Row 6
        let txtSpeaker = NSTextField()
        txtSpeaker.placeholderString = "SPEAKER_00"
        self.speakerTextField = txtSpeaker
        let btnSearchSpeaker = NSButton(title: "Найти", target: self, action: #selector(searchBySpeakerAction))
        let row6 = makeHorizontalStack(views: [txtSpeaker, btnSearchSpeaker])

        let managementContainer = NSStackView(views: [row1, row2, row3, row4, row5, row6])
        managementContainer.orientation = .vertical
        managementContainer.spacing = 10
        managementContainer.alignment = .leading

        row1.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true
        row2.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true
        row3.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true
        row4.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true
        row5.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true
        row6.widthAnchor.constraint(equalTo: managementContainer.widthAnchor).isActive = true

        let managementCard = ThemeCardView()
        managementCard.addSubview(managementContainer)
        managementContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            managementContainer.topAnchor.constraint(equalTo: managementCard.topAnchor, constant: 10),
            managementContainer.leadingAnchor.constraint(equalTo: managementCard.leadingAnchor, constant: 10),
            managementContainer.trailingAnchor.constraint(equalTo: managementCard.trailingAnchor, constant: -10),
            managementContainer.bottomAnchor.constraint(equalTo: managementCard.bottomAnchor, constant: -10)
        ])

        let managementSection = CollapsibleSectionView(sectionId: "history_management", title: "Управление")
        managementSection.contentStackView.addArrangedSubview(managementCard)

        // --- SECTION 2: Статистика ---

        // Row 1
        let popUpStats = NSPopUpButton()
        popUpStats.addItems(withTitles: ["10", "25", "50"])
        self.statsPopUp = popUpStats
        let btnFreqAnalysis = NSButton(title: "Частотный анализ", target: self, action: #selector(wordFrequencyAnalysisAction))
        let statsRow1 = makeHorizontalStack(views: [popUpStats, btnFreqAnalysis])

        // Row 2
        let btnHistoryStats = NSButton(title: "Статистика", target: self, action: #selector(getHistoryStatisticsAction))
        let statsRow2 = makeHorizontalStack(views: [btnHistoryStats])

        // Row 3
        let btnTopicTimeline = NSButton(title: "Темы", target: self, action: #selector(getTopicTimelineAction))
        let statsRow3 = makeHorizontalStack(views: [btnTopicTimeline])

        // Row 4
        let btnKeywordCloud = NSButton(title: "Облако слов", target: self, action: #selector(getKeywordCloudAction))
        let statsRow4 = makeHorizontalStack(views: [btnKeywordCloud])

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.borderType = .bezelBorder

        let textView = NSTextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.textContainerInset = NSSize(width: 5, height: 5)
        scrollView.documentView = textView
        self.statsTextView = textView

        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.heightAnchor.constraint(equalToConstant: 96).isActive = true // Approx 6 lines

        let statsContainer = NSStackView(views: [statsRow1, statsRow2, statsRow3, statsRow4, scrollView])
        statsContainer.orientation = .vertical
        statsContainer.spacing = 10
        statsContainer.alignment = .leading

        statsRow1.widthAnchor.constraint(equalTo: statsContainer.widthAnchor).isActive = true
        scrollView.widthAnchor.constraint(equalTo: statsContainer.widthAnchor).isActive = true

        let statsCard = ThemeCardView()
        statsCard.addSubview(statsContainer)
        statsContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            statsContainer.topAnchor.constraint(equalTo: statsCard.topAnchor, constant: 10),
            statsContainer.leadingAnchor.constraint(equalTo: statsCard.leadingAnchor, constant: 10),
            statsContainer.trailingAnchor.constraint(equalTo: statsCard.trailingAnchor, constant: -10),
            statsContainer.bottomAnchor.constraint(equalTo: statsCard.bottomAnchor, constant: -10)
        ])

        let statsSection = CollapsibleSectionView(sectionId: "history_stats", title: "Статистика")
        statsSection.contentStackView.addArrangedSubview(statsCard)

        return (managementSection, statsSection)
    }

    // MARK: - IPC Actions

    @objc private func toggleFavoriteAction() {
        guard let id = getSelectedID() else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "toggle_favorite", params: ["id": id]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("toggle_favorite: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func getFavoritesAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_favorites", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("get_favorites: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func setAnnotationAction() {
        guard let id = getSelectedID(), let text = annotationTextField?.stringValue, !text.isEmpty else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "set_annotation", params: ["id": id, "annotation": text]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("set_annotation: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func backupHistoryAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "backup_history", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("backup_history: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func listBackupsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "list_backups", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("list_backups: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func getTranscriptVersionsAction() {
        guard let id = getSelectedID() else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_transcript_versions", params: ["id": id]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("get_transcript_versions: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func saveTranscriptVersionAction() {
        guard let id = getSelectedID() else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "save_transcript_version", params: ["id": id]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("save_transcript_version: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func addToCollectionAction() {
        guard let id = getSelectedID(), let collection = collectionsPopUp?.titleOfSelectedItem else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "add_to_collection", params: ["id": id, "collection": collection]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("add_to_collection: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func createCollectionAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "create_collection", params: ["name": "Новая коллекция"]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("create_collection: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func searchBySpeakerAction() {
        guard let speaker = speakerTextField?.stringValue, !speaker.isEmpty else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "search_by_speaker", params: ["speaker": speaker]) ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("search_by_speaker: \(response)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error)")
                }
            }
        }
    }

    @objc private func wordFrequencyAnalysisAction() {
        let limitStr = statsPopUp?.titleOfSelectedItem ?? "10"
        let limit = Int(limitStr) ?? 10
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "word_frequency_analysis", params: ["limit": limit]) ?? [:]
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Частотный анализ:\n\(response)"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    @objc private func getHistoryStatisticsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_history_statistics", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Статистика:\n\(response)"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    @objc private func getTopicTimelineAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_topic_timeline", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Темы:\n\(response)"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    @objc private func getKeywordCloudAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_keyword_cloud", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Облако слов:\n\(response)"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statsTextView?.string = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }
}
