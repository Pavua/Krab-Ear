/*
 Claude Design A/B variant — Settings panel redesign.

 Параллельный вариант к Gemini #115. Aesthetic: macOS System Settings density
 (compact padding, tighter row spacing, inline value labels, no long description
 sub-text). Те же 5 секций и те же IPC bindings — только визуальный стиль другой.

 Активируется через UserDefaults "KrabEar_UseClaudeDesign" = true.
 По умолчанию выключен (default off). Переключение через toggle в секции
 "Интерфейс" в любом варианте Settings panel.

 НЕ заменяет Gemini-вариант — выберает пользователь.
*/

import AppKit
import Foundation

// MARK: - UserDefaults key

extension UserDefaults {
    static let krabUseClaudeDesignKey = "KrabEar_UseClaudeDesign"

    var useClaudeDesignVariant: Bool {
        get { bool(forKey: UserDefaults.krabUseClaudeDesignKey) }
        set { set(newValue, forKey: UserDefaults.krabUseClaudeDesignKey) }
    }
}

// MARK: - Compact card view (Claude Design: tighter padding)

/// ThemeCardView с уменьшенным padding — System Settings density.
/// Использует те же материалы Liquid Glass, но внутренние отступы меньше
/// (comfortable=12 → 8, tight=4 → 2) чтобы добиться компактности.
@MainActor
final class CDSettingsCardView: NSVisualEffectView {

    let contentStackView = NSStackView()
    private let containerStack = NSStackView()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }

    private func setup() {
        material = .popover
        blendingMode = .behindWindow
        state = .active
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius // 8pt — чуть меньше чем Gemini 12pt
        layer?.cornerCurve = .continuous
        layer?.borderWidth = 0.5                                      // тоньше — CD эстетика
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
        layer?.masksToBounds = true

        contentStackView.orientation = .vertical
        contentStackView.spacing = 0          // нет зазора между строками — разделитель сам даёт визуальный gap
        contentStackView.alignment = .leading

        containerStack.orientation = .vertical
        containerStack.spacing = 0
        containerStack.alignment = .leading
        containerStack.translatesAutoresizingMaskIntoConstraints = false
        containerStack.addArrangedSubview(contentStackView)
        addSubview(containerStack)

        let inset: CGFloat = KrabEarTheme.Metrics.standard // 8pt vs Gemini's 12pt
        NSLayoutConstraint.activate([
            containerStack.topAnchor.constraint(equalTo: topAnchor, constant: inset),
            containerStack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: inset),
            containerStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -inset),
            containerStack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -inset),
        ])
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
    }
}

// MARK: - Claude Design helpers

extension HistoryPanelController {

    // MARK: Row helpers

    /// Compact row: лейбл слева (12pt), optional badge, control прижат вправо.
    /// Высота фиксирована ~28pt — System Settings density.
    @MainActor
    func cdMakeRow(
        label: String,
        control: NSView,
        badge: NSView? = nil,
        badgeOnRight: Bool = false
    ) -> NSView {
        let labelField = NSTextField(labelWithString: label)
        labelField.font = .systemFont(ofSize: 12, weight: .regular)
        labelField.textColor = KrabEarTheme.Colors.textPrimary

        var leadingViews: [NSView] = [labelField]
        if let badge, !badgeOnRight { leadingViews.append(badge) }

        let labelStack = NSStackView(views: leadingViews)
        labelStack.orientation = .horizontal
        labelStack.spacing = KrabEarTheme.Metrics.tight
        labelStack.alignment = .centerY

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)

        var trailingViews: [NSView] = []
        if let badge, badgeOnRight { trailingViews.append(badge) }
        trailingViews.append(control)

        let trailingStack = NSStackView(views: trailingViews)
        trailingStack.orientation = .horizontal
        trailingStack.spacing = KrabEarTheme.Metrics.tight
        trailingStack.alignment = .centerY

        let row = NSStackView(views: [labelStack, spacer, trailingStack])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.standard
        row.edgeInsets = NSEdgeInsets(top: 5, left: 0, bottom: 5, right: 0)
        return row
    }

    /// Slider row со встроенным value label справа (Claude Design inline pattern).
    @MainActor
    func cdMakeSliderRow(label: String, slider: NSSlider, valueLabel: NSTextField) -> NSView {
        let sliderStack = NSStackView(views: [slider, valueLabel])
        sliderStack.orientation = .horizontal
        sliderStack.spacing = KrabEarTheme.Metrics.tight
        sliderStack.alignment = .centerY
        valueLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        valueLabel.textColor = KrabEarTheme.Colors.textSecondary
        valueLabel.alignment = .right
        valueLabel.setContentHuggingPriority(.required, for: .horizontal)
        slider.widthAnchor.constraint(greaterThanOrEqualToConstant: 100).isActive = true
        return cdMakeRow(label: label, control: sliderStack)
    }

    /// Separator между строками — hairline 0.5pt NSBox.
    @MainActor
    func cdMakeSeparator() -> NSView {
        let sep = NSBox()
        sep.boxType = .separator
        sep.translatesAutoresizingMaskIntoConstraints = false
        return sep
    }

    /// Section label (like System Settings section header above a group).
    @MainActor
    private func cdMakeSectionLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text.uppercased())
        label.font = .systemFont(ofSize: 10, weight: .semibold)
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.letterSpacing = 0.4
        return label
    }

    /// Small badge pill — rounded background (CD design token #FF9500 warning, #0066FF accent).
    @MainActor
    private func cdMakeBadge(text: String, color: NSColor) -> NSView {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 9, weight: .semibold)
        label.textColor = color
        label.isBordered = false
        label.drawsBackground = false
        label.setContentHuggingPriority(.required, for: .horizontal)
        return label
    }

    // MARK: - Section 1: Запись и STT (Claude Design)
    // Maps to: Gemini's "Аудио-пайплайн" + recording controls
    // CD aesthetic: tight rows, inline badges, no sub-descriptions

    @MainActor
    func cdBuildRecordingSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_recording_stt",
            title: "Запись и STT",
            isExpanded: true
        )
        let card = CDSettingsCardView()

        // 1. Quality profile
        let qualRow = cdMakeRow(label: "Качество", control: qualitySelector)

        // 2. Cleanup profile
        let cleanRow = cdMakeRow(label: "Очистка текста", control: cleanupSelector)

        // 3. Diarization toggle with beta badge
        diarizationButton.title = ""
        diarizationButton.setButtonType(.switch)
        let betaBadge = cdMakeBadge(text: "⚠ бета", color: .systemOrange)
        let diarRow = cdMakeRow(
            label: "Разделение говорящих",
            control: diarizationButton,
            badge: betaBadge,
            badgeOnRight: false
        )

        // 4. Audio ducking toggle
        audioDuckingButton.title = ""
        audioDuckingButton.setButtonType(.switch)
        let duckRow = cdMakeRow(label: "Приглушение при записи", control: audioDuckingButton)

        // 5. Audio ducking percent slider
        let duckSliderRow = cdMakeSliderRow(
            label: "Уровень приглушения",
            slider: audioDuckingSlider,
            valueLabel: audioDuckingValueLabel
        )

        // 6. Live субтитры для видео (Phase 2B, macOS 12.3+, default OFF)
        let liveSubsToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onLiveSubsChanged))
        liveSubsToggle.setButtonType(.switch)
        liveSubsToggle.state = UserDefaults.standard.liveSubsEnabled ? .on : .off
        liveSubsToggle.tag = 202601 // Phase 2B tag
        let liveSubsBadge = cdMakeBadge(text: "2B", color: .controlAccentColor)
        let liveSubsRow = cdMakeRow(
            label: "Live субтитры для видео",
            control: liveSubsToggle,
            badge: liveSubsBadge,
            badgeOnRight: false
        )

        card.contentStackView.addArrangedSubview(qualRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(cleanRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(diarRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(duckRow)
        card.contentStackView.addArrangedSubview(duckSliderRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(liveSubsRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Section 2: Перевод (Claude Design)

    @MainActor
    func cdBuildTranslationSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_translation",
            title: "Перевод",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let modeRow  = cdMakeRow(label: "Режим", control: translationSelector)
        let styleRow = cdMakeRow(label: "Стиль", control: translationStyleSelector)
        let netRow   = cdMakeRow(label: "Сеть", control: networkSelector)

        // Translate-and-paste toggle
        translateAndPasteButton.title = ""
        translateAndPasteButton.setButtonType(.switch)
        let tpRow = cdMakeRow(label: "Перевод при вставке", control: translateAndPasteButton)

        card.contentStackView.addArrangedSubview(modeRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(styleRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(netRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(tpRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Section 3: LLM (Claude Design)

    @MainActor
    func cdBuildLLMSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_llm",
            title: "LLM",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        llmRewriteButton.title = ""
        llmRewriteButton.setButtonType(.switch)
        let llmBadge = cdMakeBadge(text: "бета", color: .systemOrange)
        let llmRow   = cdMakeRow(label: "Постобработка LLM", control: llmRewriteButton, badge: llmBadge)
        let modelRow = cdMakeRow(label: "Модель", control: llmModelSelector)

        card.contentStackView.addArrangedSubview(llmRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(modelRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Section 4: Горячие клавиши (Claude Design)

    @MainActor
    func cdBuildHotkeySection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_hotkeys",
            title: "Горячие клавиши",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let keyRow     = cdMakeRow(label: "Клавиша записи", control: hotkeySelector)
        let profileRow = cdMakeRow(label: "Профиль", control: hotkeyProfileSelector)

        card.contentStackView.addArrangedSubview(keyRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(profileRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Section 5: Интерфейс (Claude Design)
    // Includes: overlay opacity, auto-paste, realtime preview, auto-start, dock icon,
    // and the A/B variant toggle itself.

    @MainActor
    func cdBuildInterfaceSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_interface",
            title: "Интерфейс",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        // Overlay opacity
        let overlayRow = cdMakeSliderRow(
            label: "Прозрачность Live Preview",
            slider: overlayOpacitySlider,
            valueLabel: overlayOpacityValueLabel
        )

        // Auto-paste
        autoPasteButton.title = ""
        autoPasteButton.setButtonType(.switch)
        let pasteRow = cdMakeRow(label: "Авто-вставка", control: autoPasteButton)

        // Realtime preview
        realtimePreviewButton.title = ""
        realtimePreviewButton.setButtonType(.switch)
        let realtimeRow = cdMakeRow(label: "Realtime preview", control: realtimePreviewButton)

        // Start sound
        startSoundButton.title = ""
        startSoundButton.setButtonType(.switch)
        let soundRow = cdMakeRow(label: "Звук при старте", control: startSoundButton)

        // Auto-start
        autoStartButton.title = ""
        autoStartButton.setButtonType(.switch)
        let autoStartRow = cdMakeRow(label: "Автозапуск (LaunchAgent)", control: autoStartButton)

        // Dock icon
        dockIconButton.title = ""
        dockIconButton.setButtonType(.switch)
        let dockRow = cdMakeRow(label: "Иконка в Dock", control: dockIconButton)

        // A/B toggle — Claude Design variant switch
        let abToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onUseClaudeDesignChanged))
        abToggle.setButtonType(.switch)
        abToggle.state = UserDefaults.standard.useClaudeDesignVariant ? .on : .off
        abToggle.tag = 202501  // для поиска в syncSettingsControls если нужно
        let abBadge = cdMakeBadge(text: "A/B", color: .controlAccentColor)
        let abRow = cdMakeRow(
            label: "Claude Design (этот вариант)",
            control: abToggle,
            badge: abBadge,
            badgeOnRight: false
        )

        card.contentStackView.addArrangedSubview(overlayRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(pasteRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(realtimeRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(soundRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(autoStartRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(dockRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(abRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Live субтитры toggle handler (Phase 2B)

    @objc func onLiveSubsChanged(_ sender: NSButton) {
        let enabled = sender.state == .on
        // Делегируем в AgentAppDelegate через responder chain
        if let appDelegate = NSApp.delegate as? AgentAppDelegate {
            appDelegate.applyLiveSubsEnabled(enabled)
        } else {
            // Fallback: только сохранить UserDefaults
            UserDefaults.standard.liveSubsEnabled = enabled
        }
    }

    // MARK: - A/B toggle handler

    @objc func onUseClaudeDesignChanged(_ sender: NSButton) {
        let enabled = sender.state == .on
        UserDefaults.standard.useClaudeDesignVariant = enabled
        // Preference persisted. Layout rebuilds automatically on next window display
        // (applyVisualTheme is called from windowDidBecomeKey / setup).
        // Notify user that the change takes effect after re-opening the panel.
        let alert = NSAlert()
        alert.messageText = enabled
            ? "Claude Design включён"
            : "Gemini Design включён"
        alert.informativeText = "Вариант будет применён при следующем открытии панели настроек."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        if let window = self.window {
            alert.beginSheetModal(for: window, completionHandler: nil)
        } else {
            alert.runModal()
        }
    }

    // MARK: - Claude Design top-level builder

    /// Строит все 5 секций Claude Design и добавляет их в settingsBar.
    /// Вызывается из rebuildSettingsLayout() когда KrabEar_UseClaudeDesign = true.
    @MainActor
    func buildClaudeDesignSettingsSections() {
        // Section 1 — Запись и STT
        let s1 = cdBuildRecordingSection()
        // Wire recording targets (controls shared with Gemini variant)
        qualitySelector.target = self
        qualitySelector.action = #selector(onQualityChanged)
        cleanupSelector.target = self
        cleanupSelector.action = #selector(onCleanupProfileChanged)
        diarizationButton.target = self
        diarizationButton.action = #selector(onDiarizationChanged)
        audioDuckingButton.target = self
        audioDuckingButton.action = #selector(onAudioDuckingChanged)
        audioDuckingSlider.target = self
        audioDuckingSlider.action = #selector(onAudioDuckingPercentChanged)

        // Section 2 — Перевод
        let s2 = cdBuildTranslationSection()
        translationSelector.target = self
        translationSelector.action = #selector(onTranslationModeChanged)
        translationStyleSelector.target = self
        translationStyleSelector.action = #selector(onTranslationStyleChanged)
        networkSelector.target = self
        networkSelector.action = #selector(onNetworkModeChanged)
        translateAndPasteButton.target = self
        translateAndPasteButton.action = #selector(onTranslateAndPasteChanged)

        // Section 3 — LLM
        let s3 = cdBuildLLMSection()
        llmRewriteButton.target = self
        llmRewriteButton.action = #selector(onLlmRewriteChanged)
        llmModelSelector.target = self
        llmModelSelector.action = #selector(onLlmModelChanged)

        // Section 4 — Горячие клавиши
        let s4 = cdBuildHotkeySection()
        hotkeySelector.target = self
        hotkeySelector.action = #selector(onHotkeyChanged)
        hotkeyProfileSelector.target = self
        hotkeyProfileSelector.action = #selector(onHotkeyProfileChanged)

        // Section 5 — Интерфейс
        let s5 = cdBuildInterfaceSection()
        overlayOpacitySlider.target = self
        overlayOpacitySlider.action = #selector(onOverlayOpacityChanged)
        autoPasteButton.target = self
        autoPasteButton.action = #selector(onAutoPasteChanged)
        realtimePreviewButton.target = self
        realtimePreviewButton.action = #selector(onRealtimePreviewChanged)
        startSoundButton.target = self
        startSoundButton.action = #selector(onStartSoundChanged)
        autoStartButton.target = self
        autoStartButton.action = #selector(onAutostartChanged)
        dockIconButton.target = self
        dockIconButton.action = #selector(onDockChanged)

        settingsBarCD.removeFromSuperview()
        settingsBarCD = NSStackView()
        settingsBarCD.orientation = .vertical
        settingsBarCD.spacing = KrabEarTheme.Metrics.tight
        settingsBarCD.alignment = .leading
        settingsBarCD.translatesAutoresizingMaskIntoConstraints = false

        for s in [s1, s2, s3, s4, s5] {
            settingsBarCD.addArrangedSubview(s)
        }
    }
}

// MARK: - NSTextField letter spacing helper

private extension NSTextField {
    /// Simple character spacing via attributed string (не нативный API — ok для static labels).
    var letterSpacing: CGFloat {
        get { 0 }
        set {
            let attrs: [NSAttributedString.Key: Any] = [
                .kern: newValue,
                .font: self.font ?? NSFont.systemFont(ofSize: 10),
                .foregroundColor: self.textColor ?? NSColor.secondaryLabelColor,
            ]
            self.attributedStringValue = NSAttributedString(string: self.stringValue, attributes: attrs)
        }
    }
}
