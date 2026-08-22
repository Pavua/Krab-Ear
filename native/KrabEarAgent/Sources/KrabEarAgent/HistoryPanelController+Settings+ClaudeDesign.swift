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

// MARK: - Associated object key for analytics dashboard

private enum AssocDashboardKey {
    nonisolated(unsafe) static var dashboardWC: UInt8 = 0
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
        liveSubsToggle.state = UserDefaults.standard.bool(forKey: UserDefaults.liveSubsEnabledKey) ? .on : .off
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

        // Glossary suggestions button row
        let glossaryButton = cdMakeGlossarySuggestButton()
        let glossaryRow = cdMakeRow(label: "Глоссарий", control: glossaryButton)

        card.contentStackView.addArrangedSubview(modeRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(styleRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(netRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(tpRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(glossaryRow)

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

        // Quick Edit before paste
        quickEditButton.title = ""
        quickEditButton.setButtonType(.switch)
        quickEditButton.target = self
        quickEditButton.action = #selector(onQuickEditChanged)
        let quickEditRow = cdMakeRow(label: "Быстрое редактирование перед вставкой", control: quickEditButton)

        // Quick Edit timeout stepper (1–30 sec, step 1)
        quickEditTimeoutStepper.minValue = 1
        quickEditTimeoutStepper.maxValue = 30
        quickEditTimeoutStepper.increment = 1
        quickEditTimeoutStepper.valueWraps = false
        quickEditTimeoutStepper.integerValue = 5
        quickEditTimeoutStepper.target = self
        quickEditTimeoutStepper.action = #selector(onQuickEditTimeoutChanged(_:))
        quickEditTimeoutValueLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        quickEditTimeoutValueLabel.textColor = KrabEarTheme.Colors.textSecondary
        quickEditTimeoutValueLabel.alignment = .right
        quickEditTimeoutValueLabel.setContentHuggingPriority(.required, for: .horizontal)
        quickEditTimeoutValueLabel.stringValue = "5 сек"
        let timeoutControlStack = NSStackView(views: [quickEditTimeoutStepper, quickEditTimeoutValueLabel])
        timeoutControlStack.orientation = .horizontal
        timeoutControlStack.spacing = KrabEarTheme.Metrics.tight
        timeoutControlStack.alignment = .centerY
        let quickEditTimeoutRow = cdMakeRow(label: "Таймаут редактирования (сек)", control: timeoutControlStack)

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

        // Analytics dashboard button
        let analyticsBtn = ThemeSecondaryButton(title: "Открыть аналитику", target: self, action: #selector(onOpenAnalyticsDashboard))
        let analyticsRow = cdMakeRow(label: "Дашборд аналитики", control: analyticsBtn)

        card.contentStackView.addArrangedSubview(overlayRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(pasteRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(quickEditRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(quickEditTimeoutRow)
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
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(analyticsRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    @objc func onOpenAnalyticsDashboard() {
        let wc = AnalyticsDashboardWindowController(ipcClient: ipcClient)
        wc.showWindow(nil)
        // Keep strong reference while shown
        objc_setAssociatedObject(self, &AssocDashboardKey.dashboardWC, wc, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }

    // MARK: - Section 6: Автозвонки (Claude Design)
    // Phase 3.4 Call Automation settings: Telnyx credentials + call limits.

    @MainActor
    func cdBuildCallAutomationSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_call_automation",
            title: "Автозвонки",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        // Telnyx API key (secure field shown as password)
        let apiKeyField = NSSecureTextField(frame: .zero)
        apiKeyField.placeholderString = "Вставьте ключ Telnyx API"
        apiKeyField.font = KrabEarTheme.Typography.body
        apiKeyField.tag = 31001 // CA: api key tag
        apiKeyField.target = self
        apiKeyField.action = #selector(onTelnyxAPIKeyChanged)
        apiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true
        let apiKeyRow = cdMakeRow(label: "Telnyx API Key", control: apiKeyField)

        // From number
        let fromField = NSTextField(string: "")
        fromField.placeholderString = "+79991234567"
        fromField.font = KrabEarTheme.Typography.body
        fromField.tag = 31002
        fromField.target = self
        fromField.action = #selector(onTelnyxFromNumberChanged)
        fromField.widthAnchor.constraint(greaterThanOrEqualToConstant: 140).isActive = true
        let fromRow = cdMakeRow(label: "Исходящий номер", control: fromField)

        // Max call duration slider (5-60 min)
        let maxDurSlider = NSSlider(value: 30, minValue: 5, maxValue: 60, target: self, action: #selector(onCallMaxDurationChanged))
        maxDurSlider.tag = 31003
        let maxDurLabel = NSTextField(labelWithString: "30 мин")
        maxDurLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        maxDurLabel.textColor = KrabEarTheme.Colors.textSecondary
        maxDurLabel.tag = 31013 // value label
        maxDurLabel.setContentHuggingPriority(.required, for: .horizontal)
        let maxDurRow = cdMakeSliderRow(label: "Макс. длительность", slider: maxDurSlider, valueLabel: maxDurLabel)

        // Cost warn threshold slider ($1-$20)
        let costSlider = NSSlider(value: 5, minValue: 1, maxValue: 20, target: self, action: #selector(onCallCostWarnChanged))
        costSlider.tag = 31004
        let costLabel = NSTextField(labelWithString: "$5")
        costLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        costLabel.textColor = KrabEarTheme.Colors.textSecondary
        costLabel.tag = 31014
        costLabel.setContentHuggingPriority(.required, for: .horizontal)
        let costRow = cdMakeSliderRow(label: "Предупреждение стоимости", slider: costSlider, valueLabel: costLabel)

        // Auto-end on silence toggle
        let silenceToggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onCallAutoEndOnSilenceChanged))
        silenceToggle.setButtonType(.switch)
        silenceToggle.state = .on
        silenceToggle.tag = 31005
        let silenceRow = cdMakeRow(label: "Авто-завершение при тишине", control: silenceToggle)

        card.contentStackView.addArrangedSubview(apiKeyRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(fromRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(maxDurRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(costRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(silenceRow)

        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Call Automation settings handlers

    @objc func onTelnyxAPIKeyChanged(_ sender: NSSecureTextField) {
        guard !isSyncingSettings else { return }
        applySettingsPatch(["telnyx_api_key": sender.stringValue])
    }

    @objc func onTelnyxFromNumberChanged(_ sender: NSTextField) {
        guard !isSyncingSettings else { return }
        applySettingsPatch(["telnyx_from_number": sender.stringValue])
    }

    @objc func onCallMaxDurationChanged(_ sender: NSSlider) {
        guard !isSyncingSettings else { return }
        let value = Int(sender.doubleValue)
        applySettingsPatch(["call_max_duration_min": value])
        // Update inline label
        if let label = sender.superview?.subviews.compactMap({ $0 as? NSTextField }).first(where: { $0.tag == 31013 }) {
            label.stringValue = "\(value) мин"
        }
    }

    @objc func onCallCostWarnChanged(_ sender: NSSlider) {
        guard !isSyncingSettings else { return }
        let value = Int(sender.doubleValue)
        applySettingsPatch(["call_cost_warn_usd": Double(value)])
        if let label = sender.superview?.subviews.compactMap({ $0 as? NSTextField }).first(where: { $0.tag == 31014 }) {
            label.stringValue = "$\(value)"
        }
    }

    @objc func onCallAutoEndOnSilenceChanged(_ sender: NSButton) {
        guard !isSyncingSettings else { return }
        applySettingsPatch(["call_auto_end_on_silence": sender.state == .on])
    }

    // MARK: - Live субтитры toggle handler (Phase 2B)

    @objc func onLiveSubsChanged(_ sender: NSButton) {
        let enabled = sender.state == .on
        // Делегируем в AgentAppDelegate через responder chain
        UserDefaults.standard.set(enabled, forKey: UserDefaults.liveSubsEnabledKey)
        if let appDelegate = NSApp.delegate as? AgentAppDelegate {
            if enabled {
                appDelegate.startLiveSubsCapture()
            } else {
                appDelegate.stopLiveSubsCapture()
            }
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
        presentAlertSheet(alert, for: self.window) { _ in }
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

        // Section 6 — Автозвонки (Phase 3.4)
        let s6 = cdBuildCallAutomationSection()

        // Section 7 — STT-движки (доступность + enable/disable каждого движка)
        let s7 = cdBuildSTTEnginesSection()

        // Section 7.5 — Калибровка (аппаратно-зависимая рекомендация STT-модели)
        let s7b = cdBuildCalibrationSection()

        // Section 7.6 — A1: рекомендованная настройка в один тап
        let s7c = cdBuildRecommendedSetupSection()

        // Section 8 — Словарь STT (hotwords + предложения из истории)
        let s8 = cdBuildSTTVocabularySection()

        // Section 8.5 — Пресеты конфигурации (встроенные + кастомные шаблоны настроек)
        let s8_5 = cdBuildConfigPresetsSection()

        // Section 8.6 — Профили резюмирования (встроенные + кастомные стили для summarize_item)
        let s8_6 = cdBuildSummaryProfilesSection()

        // Section 9 — Голосовые команды (включить/строгий режим + справочник)
        let s9 = cdBuildVoiceCommandsSection()

        // Section 9.5 — Текстовые сниппеты
        let s9_5 = cdBuildTextSnippetsSection()

        // Section 9.6 — Фонетический словарь
        let s9_6 = cdBuildPhoneticVocabSection()

        // Section 10 — Хранение истории (авто-удаление старых записей)
        let s10 = cdBuildRetentionSettingsSection()

        // Section 11 — Безопасность (шифрование истории AES-256-GCM, Keychain)
        let s11 = cdBuildSecuritySettingsSection()

        // Section 11.5 — Приватность и данные (read-only сводка)
        let s11b = cdBuildPrivacyDashboardSection()

        // Section 12 — Шаблоны вывода (управление пользовательскими шаблонами)
        let s12 = cdBuildOutputTemplatesSection()
        
        let sCloudRewriter = cdBuildCloudRewriterSection()

        settingsBarCD.removeFromSuperview()
        settingsBarCD = NSStackView()
        settingsBarCD.orientation = .vertical
        settingsBarCD.spacing = KrabEarTheme.Metrics.tight
        settingsBarCD.alignment = .leading
        settingsBarCD.translatesAutoresizingMaskIntoConstraints = false

        // Hero card — Phase 2 IA (consistency с Gemini variant). Видим summary
        // сразу, без скролла на длинный список секций.
        settingsBarCD.addArrangedSubview(buildDictationHeroCard())

        for s in [s1, s2, s3, s4, s5, s6, s7, s7b, s7c, s8, s8_5, s8_6, s9, s9_5, s9_6, s10, s11, s11b, s12, sCloudRewriter] {
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
