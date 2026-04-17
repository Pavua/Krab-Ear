/*
 ConversationViewController+UI — построение интерфейса вкладки «Разговор с AI».

 Элементы:
 - statusLabel      — «🟢 Слушает» / «🟡 Думает» / «🔴 Говорит»
 - waveformPlaceholder — заглушка визуализатора (полный waveform — отдельный PR)
 - transcriptView   — скролл с расшифровкой, обновляется при stt.partial
 - startButton      — «🎙 Начать разговор» (ThemePrimaryButton)
 - interruptButton  — «Прервать AI» (ThemeSecondaryButton), скрыт пока AI не говорит
 - settingsDrawer   — свёрнутый блок: язык / движок / мозг

 Визуальный стиль — skeleton без color/font изменений (дизайн — отдельный PR через Gemini).
*/

import AppKit

extension ConversationViewController {

    // MARK: - Build UI

    func buildUI() {
        view.wantsLayer = true

        // --- Root scroll (all content inside) ---
        let outerScroll = NSScrollView()
        outerScroll.translatesAutoresizingMaskIntoConstraints = false
        outerScroll.hasVerticalScroller = true
        outerScroll.autohidesScrollers = true
        outerScroll.drawsBackground = false
        view.addSubview(outerScroll)
        NSLayoutConstraint.activate([
            outerScroll.topAnchor.constraint(equalTo: view.topAnchor),
            outerScroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            outerScroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            outerScroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])

        let root = NSStackView()
        root.orientation = .vertical
        root.spacing = KrabEarTheme.Metrics.standard
        root.alignment = .leading
        root.translatesAutoresizingMaskIntoConstraints = false
        root.edgeInsets = NSEdgeInsets(
            top:    KrabEarTheme.Metrics.comfortable,
            left:   KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.comfortable,
            right:  KrabEarTheme.Metrics.comfortable
        )
        outerScroll.documentView = root
        NSLayoutConstraint.activate([
            root.widthAnchor.constraint(equalTo: outerScroll.widthAnchor),
        ])

        // --- Status card ---
        let statusCard = makeCard()
        let statusRow  = hStack()
        styleLabel(statusLabel, font: KrabEarTheme.Typography.sectionTitle)
        statusRow.addArrangedSubview(statusLabel)
        statusRow.addArrangedSubview(NSView()) // spacer
        statusCard.contentStackView.addArrangedSubview(statusRow)
        root.addArrangedSubview(statusCard)

        // --- Waveform placeholder card ---
        let waveCard = makeCard(title: "Визуализатор")
        waveformPlaceholder.translatesAutoresizingMaskIntoConstraints = false
        waveformPlaceholder.wantsLayer = true
        waveformPlaceholder.layer?.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.3).cgColor
        waveformPlaceholder.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        waveCard.contentStackView.addArrangedSubview(waveformPlaceholder)
        waveformPlaceholder.heightAnchor.constraint(equalToConstant: 48).isActive = true
        let waveHintLabel = NSTextField(labelWithString: "Визуализация звука появится в следующем PR")
        styleLabel(waveHintLabel, font: KrabEarTheme.Typography.caption)
        waveHintLabel.textColor = KrabEarTheme.Colors.textSecondary
        waveCard.contentStackView.addArrangedSubview(waveHintLabel)
        root.addArrangedSubview(waveCard)

        // --- Transcript card ---
        let transcriptCard = makeCard(title: "Транскрипция")
        let transcriptScroll = NSScrollView()
        transcriptScroll.translatesAutoresizingMaskIntoConstraints = false
        transcriptScroll.hasVerticalScroller = true
        transcriptScroll.autohidesScrollers = true
        transcriptScroll.drawsBackground = false
        transcriptScroll.borderType = .noBorder

        transcriptView.isEditable = false
        transcriptView.isSelectable = true
        transcriptView.backgroundColor = .clear
        transcriptView.textContainerInset = NSSize(width: 4, height: 4)
        transcriptView.font = KrabEarTheme.Typography.body
        transcriptView.textColor = KrabEarTheme.Colors.textPrimary
        transcriptView.string = "Нажмите «Начать разговор» чтобы начать диалог с AI."
        transcriptScroll.documentView = transcriptView
        transcriptScroll.heightAnchor.constraint(equalToConstant: 180).isActive = true

        transcriptCard.contentStackView.addArrangedSubview(transcriptScroll)
        root.addArrangedSubview(transcriptCard)

        // --- Controls row ---
        let controlsCard = makeCard()
        let controlsRow  = hStack()

        startButton.target = self
        startButton.action = #selector(onStartStopTapped)
        controlsRow.addArrangedSubview(startButton)

        interruptButton.target = self
        interruptButton.action = #selector(onInterruptTapped)
        interruptButton.isHidden = true
        controlsRow.addArrangedSubview(interruptButton)
        controlsRow.addArrangedSubview(NSView()) // spacer

        controlsCard.contentStackView.addArrangedSubview(controlsRow)
        root.addArrangedSubview(controlsCard)

        // --- Settings drawer ---
        let settingsCard = makeCard()
        let disclosureRow = hStack()

        settingsDisclosure.setButtonType(.pushOnPushOff)
        settingsDisclosure.bezelStyle = .disclosure
        settingsDisclosure.title = ""
        settingsDisclosure.state = .off
        settingsDisclosure.target = self
        settingsDisclosure.action = #selector(onSettingsDisclosureTapped)

        let settingsHeaderLabel = NSTextField(labelWithString: "Настройки разговора")
        styleLabel(settingsHeaderLabel, font: KrabEarTheme.Typography.body)
        disclosureRow.addArrangedSubview(settingsDisclosure)
        disclosureRow.addArrangedSubview(settingsHeaderLabel)
        disclosureRow.addArrangedSubview(NSView())

        settingsDrawer.orientation = .vertical
        settingsDrawer.spacing     = KrabEarTheme.Metrics.standard
        settingsDrawer.alignment   = .leading
        settingsDrawer.isHidden    = true
        settingsDrawer.translatesAutoresizingMaskIntoConstraints = false

        buildSettingsDrawer()

        settingsCard.contentStackView.addArrangedSubview(disclosureRow)
        settingsCard.contentStackView.addArrangedSubview(settingsDrawer)
        root.addArrangedSubview(settingsCard)
    }

    // MARK: - Settings drawer content

    private func buildSettingsDrawer() {
        // Language hint row
        let langRow = hStack()
        let langLabel = NSTextField(labelWithString: "Язык подсказки:")
        styleLabel(langLabel, font: KrabEarTheme.Typography.body)
        langHintSelector.addItems(withTitles: ["Авто", "RU", "EN", "ES"])
        langHintSelector.target  = self
        langHintSelector.action  = #selector(onLangHintChanged)
        langRow.addArrangedSubview(langLabel)
        langRow.addArrangedSubview(langHintSelector)
        langRow.addArrangedSubview(NSView())
        settingsDrawer.addArrangedSubview(langRow)

        // Engine row
        let engineRow = hStack()
        let engineLabel = NSTextField(labelWithString: "Движок:")
        styleLabel(engineLabel, font: KrabEarTheme.Typography.body)
        engineSelector.addItems(withTitles: ["Авто", "moshi", "seamless"])
        engineSelector.target = self
        engineSelector.action = #selector(onEngineChanged)
        engineRow.addArrangedSubview(engineLabel)
        engineRow.addArrangedSubview(engineSelector)
        engineRow.addArrangedSubview(NSView())
        settingsDrawer.addArrangedSubview(engineRow)

        // Brain row
        let brainRow = hStack()
        let brainLabel = NSTextField(labelWithString: "Мозг:")
        styleLabel(brainLabel, font: KrabEarTheme.Typography.body)
        brainSelector.addItems(withTitles: ["Авто", "qwen3-4b", "llama-3.2-3b"])
        brainSelector.target = self
        brainSelector.action = #selector(onBrainChanged)
        brainRow.addArrangedSubview(brainLabel)
        brainRow.addArrangedSubview(brainSelector)
        brainRow.addArrangedSubview(NSView())
        settingsDrawer.addArrangedSubview(brainRow)

        // Gateway URL (read-only, from config)
        let urlRow = hStack()
        let urlLabel = NSTextField(labelWithString: "Gateway URL:")
        styleLabel(urlLabel, font: KrabEarTheme.Typography.body)
        let urlValue = NSTextField(labelWithString: config.wsURLString)
        styleLabel(urlValue, font: KrabEarTheme.Typography.caption)
        urlValue.textColor = KrabEarTheme.Colors.textSecondary
        urlValue.lineBreakMode = .byTruncatingMiddle
        urlRow.addArrangedSubview(urlLabel)
        urlRow.addArrangedSubview(urlValue)
        urlRow.addArrangedSubview(NSView())
        settingsDrawer.addArrangedSubview(urlRow)
    }

    // MARK: - Settings actions

    @objc func onLangHintChanged() {
        let titles = ["auto", "ru", "en", "es"]
        let idx = langHintSelector.indexOfSelectedItem
        config.languageHint = (idx >= 0 && idx < titles.count) ? titles[idx] : "auto"
    }

    @objc func onEngineChanged() {
        let titles = ["auto", "moshi", "seamless"]
        let idx = engineSelector.indexOfSelectedItem
        config.engine = (idx >= 0 && idx < titles.count) ? titles[idx] : "auto"
    }

    @objc func onBrainChanged() {
        let titles = ["auto", "qwen3-4b", "llama-3.2-3b"]
        let idx = brainSelector.indexOfSelectedItem
        config.brain = (idx >= 0 && idx < titles.count) ? titles[idx] : "auto"
    }

    // MARK: - Private helpers

    private func makeCard(title: String = "") -> ThemeCardView {
        let card = ThemeCardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        card.title = title
        card.contentStackView.orientation = .vertical
        card.contentStackView.spacing = KrabEarTheme.Metrics.standard
        card.contentStackView.alignment = .leading
        // Stretch card full width
        card.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return card
    }

    private func hStack() -> NSStackView {
        let s = NSStackView()
        s.orientation = .horizontal
        s.spacing     = KrabEarTheme.Metrics.standard
        s.alignment   = .centerY
        s.translatesAutoresizingMaskIntoConstraints = false
        s.setHuggingPriority(.defaultLow, for: .horizontal)
        return s
    }

    private func styleLabel(_ label: NSTextField, font: NSFont) {
        label.font              = font
        label.isEditable        = false
        label.isBordered        = false
        label.drawsBackground   = false
        label.translatesAutoresizingMaskIntoConstraints = false
    }
}
