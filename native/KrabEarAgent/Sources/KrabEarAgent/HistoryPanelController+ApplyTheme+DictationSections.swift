/*
 HistoryPanelController+ApplyTheme+DictationSections.swift

 Builders для inline-секций Dictation tab которые раньше были раскиданы
 по 100+ строк внутри applyVisualTheme. Каждая возвращает готовую
 CollapsibleSectionView (или tuple с extra views для delayed constraints).

 applyVisualTheme после extract'а зовёт `setupDictationXSection()` методы
 и просто добавляет результат в settingsBar — orchestrator вместо assembler.

 Pattern совпадает с уже existing builders (`buildHotkeySection`,
 `buildSystemSection`, `buildLLMSection`, `buildAudioPipelineSection`,
 `buildVoiceAssistantSection`, `buildQuickPresetSection`,
 `buildSelectionTranslatorSection`).
*/

import AppKit

extension HistoryPanelController {

    /// Diagnostics & Metrics section. Возвращает tuple (section, diagCard) —
    /// `diagCard` нужен caller'у для activate'а width constraint ПОСЛЕ
    /// добавления section в view hierarchy (см. applyVisualTheme).
    func setupDictationDiagnosticsSection() -> (section: CollapsibleSectionView, diagCard: ThemeCardView) {
        let diagSection = CollapsibleSectionView(
            sectionId: "dictation_diagnostics",
            title: "Диагностика и метрики",
            isExpanded: false
        )
        let diagCard = ThemeCardView()

        diagnosticsRow.orientation = .horizontal
        diagnosticsRow.spacing = KrabEarTheme.Metrics.standard
        diagnosticsRow.alignment = .centerY
        diagnosticsRow.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsButton.target = self
        diagnosticsButton.action = #selector(onDiagnostics)
        metricsButton.target = self
        metricsButton.action = #selector(onMetrics)
        recordingStatsButton.target = self
        recordingStatsButton.action = #selector(onRecordingStats)
        storageInfoButton.target = self
        storageInfoButton.action = #selector(onStorageInfo)
        diagnosticsRow.addArrangedSubview(diagnosticsButton)
        diagnosticsRow.addArrangedSubview(metricsButton)
        diagnosticsRow.addArrangedSubview(recordingStatsButton)
        diagnosticsRow.addArrangedSubview(storageInfoButton)
        diagCard.contentStackView.addArrangedSubview(diagnosticsRow)

        diagnosticsOutputView.isEditable = false
        diagnosticsOutputView.isSelectable = true
        diagnosticsOutputView.font = KrabEarTheme.Typography.monospace
        diagnosticsOutputView.textColor = KrabEarTheme.Colors.textSecondary
        diagnosticsOutputView.backgroundColor = .clear
        diagnosticsOutputView.drawsBackground = false
        diagnosticsOutputScroll.documentView = diagnosticsOutputView
        diagnosticsOutputScroll.drawsBackground = false
        diagnosticsOutputScroll.hasVerticalScroller = true
        diagnosticsOutputScroll.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsOutputScroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
        // Width constraint активируется caller'ом после добавления diagCard в hierarchy.
        diagCard.contentStackView.addArrangedSubview(diagnosticsOutputScroll)

        diagSection.contentStackView.addArrangedSubview(diagCard)
        self.diagnosticsSection = diagSection
        return (diagSection, diagCard)
    }

    /// Profile presets & audio devices section. Self-contained, no width constraint
    /// post-hookup.
    func setupDictationProfileAudioSection() -> CollapsibleSectionView {
        let profAudioSection = CollapsibleSectionView(
            sectionId: "dictation_profile_audio",
            title: "Профили и устройства",
            isExpanded: false
        )
        let profAudioCard = ThemeCardView()

        profileRow.orientation = .horizontal
        profileRow.spacing = KrabEarTheme.Metrics.standard
        profileRow.alignment = .centerY
        profileRow.translatesAutoresizingMaskIntoConstraints = false
        let profileLabel = NSTextField(labelWithString: "Профиль:")
        profileLabel.font = KrabEarTheme.Typography.body
        profilePresetSelector.removeAllItems()
        profilePresetSelector.addItem(withTitle: "Загрузка...")
        applyProfileButton.target = self
        applyProfileButton.action = #selector(onApplyProfile)
        profileRow.addArrangedSubview(profileLabel)
        profileRow.addArrangedSubview(profilePresetSelector)
        profileRow.addArrangedSubview(applyProfileButton)
        profAudioCard.contentStackView.addArrangedSubview(profileRow)

        audioDeviceRow.orientation = .horizontal
        audioDeviceRow.spacing = KrabEarTheme.Metrics.standard
        audioDeviceRow.alignment = .centerY
        audioDeviceRow.translatesAutoresizingMaskIntoConstraints = false
        let audioLabel = NSTextField(labelWithString: "Микрофон:")
        audioLabel.font = KrabEarTheme.Typography.body
        audioDeviceSelector.removeAllItems()
        audioDeviceSelector.addItem(withTitle: "По умолчанию")
        testMicButton.target = self
        testMicButton.action = #selector(onTestMicrophone)
        micTestResultLabel.font = KrabEarTheme.Typography.caption
        micTestResultLabel.textColor = KrabEarTheme.Colors.textSecondary
        audioDeviceRow.addArrangedSubview(audioLabel)
        audioDeviceRow.addArrangedSubview(audioDeviceSelector)
        audioDeviceRow.addArrangedSubview(testMicButton)
        audioDeviceRow.addArrangedSubview(micTestResultLabel)
        profAudioCard.contentStackView.addArrangedSubview(audioDeviceRow)

        profAudioSection.contentStackView.addArrangedSubview(profAudioCard)
        self.profileAudioSection = profAudioSection
        return profAudioSection
    }

    /// Clipboard history section. Self-contained.
    func setupDictationClipboardSection() -> CollapsibleSectionView {
        let clipSection = CollapsibleSectionView(
            sectionId: "dictation_clipboard",
            title: "Буфер обмена",
            isExpanded: false
        )
        let clipCard = ThemeCardView()

        clipboardRow.orientation = .horizontal
        clipboardRow.spacing = KrabEarTheme.Metrics.standard
        clipboardRow.alignment = .centerY
        clipboardRow.translatesAutoresizingMaskIntoConstraints = false
        clipboardHistoryButton.target = self
        clipboardHistoryButton.action = #selector(onClipboardHistory)
        repasteButton.target = self
        repasteButton.action = #selector(onRepasteItem)
        clipboardRow.addArrangedSubview(clipboardHistoryButton)
        clipboardRow.addArrangedSubview(repasteButton)
        clipCard.contentStackView.addArrangedSubview(clipboardRow)
        clipSection.contentStackView.addArrangedSubview(clipCard)
        self.clipboardSection = clipSection
        return clipSection
    }

    // MARK: - CD Builders

    @MainActor
    func cdBuildDictationProfileAudioSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_dictation_profile_audio",
            title: "Профили и устройства",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let profileStack = NSStackView(views: [profilePresetSelector, applyProfileButton])
        profileStack.orientation = .horizontal
        profileStack.spacing = KrabEarTheme.Metrics.tight
        let profRow = cdMakeRow(label: "Профиль", control: profileStack)

        let audioStack = NSStackView(views: [audioDeviceSelector, testMicButton, micTestResultLabel])
        audioStack.orientation = .horizontal
        audioStack.spacing = KrabEarTheme.Metrics.tight
        let audRow = cdMakeRow(label: "Микрофон", control: audioStack)

        card.contentStackView.addArrangedSubview(profRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(audRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    @MainActor
    func cdBuildDictationClipboardSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_dictation_clipboard",
            title: "Буфер обмена",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let clipStack = NSStackView(views: [clipboardHistoryButton, repasteButton])
        clipStack.orientation = .horizontal
        clipStack.spacing = KrabEarTheme.Metrics.tight
        let clipRow = cdMakeRow(label: "История и вставка", control: clipStack)

        card.contentStackView.addArrangedSubview(clipRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

}
