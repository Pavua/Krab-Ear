/*
 HistoryPanelController+ApplyTheme+HistoryTab.swift

 History tab assembly extracted из applyVisualTheme.
 Continuing PR #327 / #328 incremental split pattern.

 Tab включает:
 - Filters section (filterRow1/2 + historyQuickPresetRow)
 - Primary actions row (load more, jump, copy, paste, delete, status labels)
 - Advanced actions section (toolbar row, secondary actions, second overflow row, history enhancements)
 - Import section (importRow + dropZoneView)
 - Status row (glossary + import status)
 - Search/actions card + table card + management/stats sections
 - Width constraints applied to all historyStack children
*/

import AppKit

extension HistoryPanelController {

    /// Assemble History tab. Mutates self.historyStack +
    /// self.themeWidthConstraints + several `*Section` properties.
    /// Caller просто зовёт после Live tab готов.
    func setupHistoryTab() {
        historyPreviewContainer.isHidden = true
        historyScrollMinHeightConstraint?.constant = 180

        // Filters section — reparenting filter rows.
        let filtersSection = CollapsibleSectionView(
            sectionId: "history_filters",
            title: "Фильтры",
            isExpanded: false
        )
        let filtersCard = ThemeCardView()
        // Отцепляем rows из предыдущего parent'а (applyVisualTheme может
        // быть вызван повторно — rows как class properties могут висеть в
        // старой filtersCard).
        filterRow1.removeFromSuperview()
        filterRow2.removeFromSuperview()
        historyQuickPresetRow.removeFromSuperview()
        filtersCard.contentStackView.addArrangedSubview(filterRow1)
        filtersCard.contentStackView.addArrangedSubview(filterRow2)
        filtersCard.contentStackView.addArrangedSubview(historyQuickPresetRow)
        filtersSection.contentStackView.addArrangedSubview(filtersCard)
        self.historyFiltersSection = filtersSection

        // Primary actions row.
        primaryActionsRow.addArrangedSubview(loadMoreButton)
        primaryActionsRow.addArrangedSubview(jumpToLatestButton)
        primaryActionsRow.addArrangedSubview(copyButton)
        primaryActionsRow.addArrangedSubview(pasteSelectedButton)
        primaryActionsRow.addArrangedSubview(deleteButton)
        primaryActionsRow.addArrangedSubview(NSView()) // Spacer
        primaryActionsRow.addArrangedSubview(historyOverviewLabel)
        primaryActionsRow.addArrangedSubview(historyStatusLabel)

        // Advanced actions section.
        let advancedSection = CollapsibleSectionView(
            sectionId: "history_advanced",
            title: "Расширенные действия",
            isExpanded: false
        )
        let advancedToolbarRow = NSStackView()
        advancedToolbarRow.orientation = .horizontal
        advancedToolbarRow.spacing = KrabEarTheme.Metrics.standard
        advancedToolbarRow.alignment = .centerY
        advancedToolbarRow.distribution = .fill
        advancedToolbarRow.setHuggingPriority(.defaultLow, for: .horizontal)
        advancedToolbarRow.setClippingResistancePriority(.required, for: .horizontal)
        helpButton.removeFromSuperview()
        liveTranslatePresetButton.removeFromSuperview()
        advancedToolbarRow.addArrangedSubview(helpButton)
        advancedToolbarRow.addArrangedSubview(liveTranslatePresetButton)
        advancedToolbarRow.addArrangedSubview(openTranscriptsButton)
        advancedToolbarRow.addArrangedSubview(NSView()) // Spacer
        let advancedCard = ThemeCardView()
        advancedCard.contentStackView.addArrangedSubview(advancedToolbarRow)

        secondaryActionsRow.addArrangedSubview(loadAllButton)
        secondaryActionsRow.addArrangedSubview(copyOriginalButton)
        secondaryActionsRow.addArrangedSubview(copyTranslationButton)
        secondaryActionsRow.addArrangedSubview(retranslateButton)
        secondaryActionsRow.addArrangedSubview(summarizeSelectedButton)
        secondaryActionsRow.addArrangedSubview(NSView()) // Spacer
        advancedCard.contentStackView.addArrangedSubview(secondaryActionsRow)

        // Second row for overflow buttons.
        let secondaryActionsRow2 = NSStackView()
        secondaryActionsRow2.orientation = .horizontal
        secondaryActionsRow2.spacing = KrabEarTheme.Metrics.standard
        secondaryActionsRow2.alignment = .centerY
        secondaryActionsRow2.distribution = .fill
        secondaryActionsRow2.setHuggingPriority(.defaultLow, for: .horizontal)
        secondaryActionsRow2.setClippingResistancePriority(.required, for: .horizontal)
        secondaryActionsRow2.addArrangedSubview(exportButton)
        secondaryActionsRow2.addArrangedSubview(exportNdjsonButton)
        secondaryActionsRow2.addArrangedSubview(importNdjsonButton)
        secondaryActionsRow2.addArrangedSubview(compactButton)
        secondaryActionsRow2.addArrangedSubview(NSView()) // Spacer
        advancedCard.contentStackView.addArrangedSubview(secondaryActionsRow2)

        // History enhancements row.
        historyEnhancementsRow.orientation = .horizontal
        historyEnhancementsRow.spacing = KrabEarTheme.Metrics.standard
        historyEnhancementsRow.alignment = .centerY
        historyEnhancementsRow.translatesAutoresizingMaskIntoConstraints = false
        historyEnhancementsRow.distribution = .fill
        historyEnhancementsRow.setHuggingPriority(.defaultLow, for: .horizontal)
        historyEnhancementsRow.setClippingResistancePriority(.required, for: .horizontal)
        exportSrtButton.target = self
        exportSrtButton.action = #selector(onExportSrt)
        cleanupDaysSelector.removeAllItems()
        cleanupDaysSelector.addItems(withTitles: ["30 дней", "60 дней", "90 дней", "180 дней", "365 дней"])
        cleanupHistoryButton.target = self
        cleanupHistoryButton.action = #selector(onCleanupHistory)
        vocabSuggestionsButton.target = self
        vocabSuggestionsButton.action = #selector(onVocabSuggestions)
        glossarySuggestionsButton.target = self
        glossarySuggestionsButton.action = #selector(onGlossarySuggestions)
        sendToAppleNotesButton.target = self
        sendToAppleNotesButton.action = #selector(onSendToAppleNotes)
        sendToRemindersButton.target = self
        sendToRemindersButton.action = #selector(onSendToReminders)
        createCalendarEventButton.target = self
        createCalendarEventButton.action = #selector(onCreateCalendarEvent)
        sendToImessageButton.target = self
        sendToImessageButton.action = #selector(onSendToImessage)
        historyEnhancementsRow.addArrangedSubview(exportSrtButton)
        historyEnhancementsRow.addArrangedSubview(cleanupDaysSelector)
        historyEnhancementsRow.addArrangedSubview(cleanupHistoryButton)
        historyEnhancementsRow.addArrangedSubview(vocabSuggestionsButton)
        historyEnhancementsRow.addArrangedSubview(glossarySuggestionsButton)
        historyEnhancementsRow.addArrangedSubview(sendToAppleNotesButton)
        historyEnhancementsRow.addArrangedSubview(sendToRemindersButton)
        historyEnhancementsRow.addArrangedSubview(createCalendarEventButton)
        historyEnhancementsRow.addArrangedSubview(sendToImessageButton)
        sendToTelegramButton.target = self
        sendToTelegramButton.action = #selector(onSendToTelegram)
        historyEnhancementsRow.addArrangedSubview(sendToTelegramButton)
        historyEnhancementsRow.addArrangedSubview(NSView()) // Spacer
        advancedCard.contentStackView.addArrangedSubview(historyEnhancementsRow)
        advancedSection.contentStackView.addArrangedSubview(advancedCard)
        self.historyAdvancedSection = advancedSection

        // Import section.
        let importSection = CollapsibleSectionView(
            sectionId: "history_import",
            title: "Импорт аудио",
            isExpanded: false
        )
        let importCard = ThemeCardView()
        importCard.contentStackView.addArrangedSubview(importRow)
        importCard.contentStackView.addArrangedSubview(dropZoneView)
        importSection.contentStackView.addArrangedSubview(importCard)
        self.historyImportSection = importSection

        // Status row.
        statusRow.addArrangedSubview(glossaryStatusLabel)
        statusRow.addArrangedSubview(NSView()) // Spacer
        statusRow.addArrangedSubview(importStatusLabel)

        // Search/actions + table cards.
        let searchActionsCard = ThemeCardView()
        searchActionsCard.contentStackView.addArrangedSubview(topSearchRow)
        searchActionsCard.contentStackView.addArrangedSubview(topActionsRow)

        // Empty state setup
        historyEmptyStateContainer.orientation = .vertical
        historyEmptyStateContainer.spacing = KrabEarTheme.Metrics.tight
        historyEmptyStateContainer.alignment = .centerX
        let emptyIcon = NSImageView(image: NSImage(systemSymbolName: "tray", accessibilityDescription: nil)!.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 32, weight: .regular))!)
        emptyIcon.contentTintColor = KrabEarTheme.Colors.textSecondary
        let emptyLabel = NSTextField(labelWithString: "История пуста")
        emptyLabel.font = KrabEarTheme.Typography.caption
        emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
        emptyLabel.isBordered = false
        emptyLabel.drawsBackground = false
        emptyLabel.isEditable = false
        emptyLabel.isSelectable = false
        historyEmptyStateContainer.addArrangedSubview(emptyIcon)
        historyEmptyStateContainer.addArrangedSubview(emptyLabel)
        historyEmptyStateContainer.isHidden = true
        
        // overlay over scrollView
        let tableOverlayContainer = NSView()
        tableOverlayContainer.translatesAutoresizingMaskIntoConstraints = false
        tableOverlayContainer.addSubview(scrollView)
        tableOverlayContainer.addSubview(historyEmptyStateContainer)
        historyEmptyStateContainer.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: tableOverlayContainer.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: tableOverlayContainer.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: tableOverlayContainer.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: tableOverlayContainer.bottomAnchor),
            historyEmptyStateContainer.centerXAnchor.constraint(equalTo: tableOverlayContainer.centerXAnchor),
            historyEmptyStateContainer.centerYAnchor.constraint(equalTo: tableOverlayContainer.centerYAnchor)
        ])

        let tableCard = ThemeCardView()
        tableCard.contentStackView.addArrangedSubview(tableOverlayContainer)

        let primaryActionsCard = ThemeCardView()
        primaryActionsCard.contentStackView.addArrangedSubview(primaryActionsRow)

        let statusCard = ThemeCardView()
        statusCard.contentStackView.addArrangedSubview(statusRow)

        // Assemble historyStack.
        historyStack.addArrangedSubview(searchActionsCard)
        historyStack.addArrangedSubview(filtersSection)
        historyStack.addArrangedSubview(tableCard)
        historyStack.addArrangedSubview(primaryActionsCard)
        historyStack.addArrangedSubview(advancedSection)
        historyStack.addArrangedSubview(importSection)
        // Семантический поиск (PR #284 backend, отдельный extension UI).
        historyStack.addArrangedSubview(setupSemanticSearchSection())
        // Сводка дня (backend daily_digest, отдельный extension +DailyRecap).
        historyStack.addArrangedSubview(setupDailyRecapSection())
        // Действия и решения (PR #289 backend, Action Items extractor через LLM).
        historyStack.addArrangedSubview(setupActionItemsSection())
        // Gemini 3.1 Pro: управление + статистика.
        let (managementSection, statsSection) = setupManagementSections()
        historyStack.addArrangedSubview(managementSection)
        historyStack.addArrangedSubview(statsSection)
        // Календарь активности (GitHub-style heatmap)
        historyStack.addArrangedSubview(setupActivityCalendarSection())
        historyStack.addArrangedSubview(statusCard)

        // Width constraints для всех history children.
        for child in historyStack.arrangedSubviews {
            let c = child.widthAnchor.constraint(equalTo: historyStack.widthAnchor)
            c.isActive = true
            themeWidthConstraints.append(c)
        }

        // Экспорт выбранных записей: multi-select + context menu.
        setupExportSelectionContextMenu()
    }
}
