/*
 Хранение истории — секция настроек авто-удаления старых записей.

 Surfacing двух настроек Python backend (core/config.py):
   auto_purge_enabled        (default: false)
   auto_purge_retention_days (default: 90, range 1–3650)

 IPC-контракты:
   - set_settings {auto_purge_enabled: Bool}
   - set_settings {auto_purge_retention_days: Int}

 Архитектура:
   - buildRetentionSettingsSection()   — вариант для Gemini-дизайна (settingsBar).
   - cdBuildRetentionSettingsSection() — вариант для Claude Design (settingsBarCD).
   - syncRetentionSettings(enabled:retentionDays:) — обновляет контролы при
     смене настроек (вызывается из syncSettingsControls).

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Никакого runModal() — переключатели не требуют подтверждения.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum RetentionAssocKeys {
    nonisolated(unsafe) static var enabledToggle: UInt8 = 0
    nonisolated(unsafe) static var daysStepper: UInt8 = 0
    nonisolated(unsafe) static var daysLabel: UInt8 = 0
    nonisolated(unsafe) static var cdEnabledToggle: UInt8 = 0
    nonisolated(unsafe) static var cdDaysStepper: UInt8 = 0
    nonisolated(unsafe) static var cdDaysLabel: UInt8 = 0
    nonisolated(unsafe) static var statusIcon: UInt8 = 0
    nonisolated(unsafe) static var cdStatusIcon: UInt8 = 0
}

// MARK: - HistoryPanelController+RetentionSettings

extension HistoryPanelController {

    // MARK: - Gemini variant

    /// Строит секцию «Хранение истории» для Gemini-дизайна (settingsBar).
    @MainActor
    func buildRetentionSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "history_retention_settings",
            title: "Хранение истории",
            isExpanded: false,
            iconSymbol: "clock.arrow.circlepath"
        )

        let card = ThemeCardView()

        let iconView = NSImageView()
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        if let icon = NSImage(systemSymbolName: "clock.arrow.circlepath", accessibilityDescription: nil) {
            iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
        }
        iconView.contentTintColor = AgentSettings.default.autoPurgeEnabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &RetentionAssocKeys.statusIcon, iconView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Тоггл «Авто-удаление старых записей»
        let enabledToggle = NSButton(
            checkboxWithTitle: "",
            target: self,
            action: #selector(onAutoPurgeEnabledChanged)
        )
        enabledToggle.state = AgentSettings.default.autoPurgeEnabled ? .on : .off
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.enabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let controlStack = NSStackView(views: [iconView, enabledToggle])
        controlStack.orientation = .horizontal
        controlStack.alignment = .centerY
        controlStack.spacing = KrabEarTheme.Metrics.tight

        let enabledRow = makeSettingRow(
            label: "Авто-удаление старых записей",
            description: "Записи старше указанного срока удаляются автоматически. Удаление необратимо.",
            control: controlStack
        )

        // Степпер «Хранить записи (дней)»
        let stepper = NSStepper()
        stepper.minValue = 1
        stepper.maxValue = 3650
        stepper.increment = 1
        stepper.integerValue = AgentSettings.default.autoPurgeRetentionDays
        stepper.autorepeat = true
        stepper.target = self
        stepper.action = #selector(onRetentionDaysChanged)
        stepper.isEnabled = AgentSettings.default.autoPurgeEnabled
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.daysStepper, stepper, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let daysLabel = NSTextField(labelWithString: "\(AgentSettings.default.autoPurgeRetentionDays) дн. (1–3650)")
        daysLabel.font = KrabEarTheme.Typography.body
        daysLabel.textColor = KrabEarTheme.Colors.textPrimary
        daysLabel.alignment = .right
        daysLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.daysLabel, daysLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let stepperStack = NSStackView(views: [daysLabel, stepper])
        stepperStack.orientation = .horizontal
        stepperStack.alignment = .centerY
        stepperStack.spacing = KrabEarTheme.Metrics.tight

        let daysRow = makeSettingRow(
            label: "Хранить записи (дней)",
            description: "Диапазон: 1–3650 дней.",
            control: stepperStack
        )

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(daysRow)
        section.contentStackView.addArrangedSubview(card)

        return section
    }

    // MARK: - Claude Design variant

    /// Строит секцию «Хранение истории» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildRetentionSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_history_retention",
            title: "Хранение истории",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        let iconView = NSImageView()
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        if let icon = NSImage(systemSymbolName: "clock.arrow.circlepath", accessibilityDescription: nil) {
            iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
        }
        iconView.contentTintColor = AgentSettings.default.autoPurgeEnabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &RetentionAssocKeys.cdStatusIcon, iconView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Тоггл «Авто-удаление»
        let enabledToggle = NSButton(
            checkboxWithTitle: "",
            target: self,
            action: #selector(onAutoPurgeEnabledChangedCD)
        )
        enabledToggle.state = AgentSettings.default.autoPurgeEnabled ? .on : .off
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.cdEnabledToggle, enabledToggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let controlStack = NSStackView(views: [iconView, enabledToggle])
        controlStack.orientation = .horizontal
        controlStack.alignment = .centerY
        controlStack.spacing = 6

        let enabledRow = cdMakeRow(label: "Авто-удаление старых записей", control: controlStack)

        // Степпер «Хранить (дней)»
        let stepper = NSStepper()
        stepper.minValue = 1
        stepper.maxValue = 3650
        stepper.increment = 1
        stepper.integerValue = AgentSettings.default.autoPurgeRetentionDays
        stepper.autorepeat = true
        stepper.target = self
        stepper.action = #selector(onRetentionDaysChangedCD)
        stepper.isEnabled = AgentSettings.default.autoPurgeEnabled
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.cdDaysStepper, stepper, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let daysLabel = NSTextField(labelWithString: "\(AgentSettings.default.autoPurgeRetentionDays) дн. (1–3650)")
        daysLabel.font = .systemFont(ofSize: 12, weight: .regular)
        daysLabel.textColor = KrabEarTheme.Colors.textPrimary
        daysLabel.alignment = .right
        daysLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        objc_setAssociatedObject(
            self, &RetentionAssocKeys.cdDaysLabel, daysLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let stepperStack = NSStackView(views: [daysLabel, stepper])
        stepperStack.orientation = .horizontal
        stepperStack.alignment = .centerY
        stepperStack.spacing = 4

        let daysRow = cdMakeRow(label: "Хранить записи (дней)", control: stepperStack)

        // Подпись «Удаление необратимо»
        let captionLabel = NSTextField(
            labelWithString: "Удаление записей необратимо — восстановление невозможно."
        )
        captionLabel.font = .systemFont(ofSize: 11, weight: .regular)
        captionLabel.textColor = KrabEarTheme.Colors.textSecondary
        captionLabel.lineBreakMode = .byWordWrapping
        captionLabel.maximumNumberOfLines = 2

        card.contentStackView.addArrangedSubview(enabledRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(daysRow)
        card.contentStackView.addArrangedSubview(captionLabel)

        section.contentStackView.addArrangedSubview(card)

        return section
    }

    // MARK: - Toggle actions (Gemini)

    @objc func onAutoPurgeEnabledChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(
            self, &RetentionAssocKeys.enabledToggle
        ) as? NSButton else { return }
        let enabled = toggle.state == .on
        applySettingsPatch(["auto_purge_enabled": enabled])
        // Dim stepper when disabled.
        if let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.daysStepper
        ) as? NSStepper {
            stepper.isEnabled = enabled
        }
        if let iconView = objc_getAssociatedObject(self, &RetentionAssocKeys.statusIcon) as? NSImageView {
            iconView.contentTintColor = enabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        }
    }

    @objc func onRetentionDaysChanged() {
        guard !isSyncingSettings else { return }
        guard let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.daysStepper
        ) as? NSStepper else { return }
        let days = stepper.integerValue
        if let label = objc_getAssociatedObject(
            self, &RetentionAssocKeys.daysLabel
        ) as? NSTextField {
            label.stringValue = "\(days) дн. (1–3650)"
        }
        applySettingsPatch(["auto_purge_retention_days": days])
    }

    // MARK: - Toggle actions (Claude Design)

    @objc func onAutoPurgeEnabledChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdEnabledToggle
        ) as? NSButton else { return }
        let enabled = toggle.state == .on
        applySettingsPatch(["auto_purge_enabled": enabled])
        if let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdDaysStepper
        ) as? NSStepper {
            stepper.isEnabled = enabled
        }
        if let iconView = objc_getAssociatedObject(self, &RetentionAssocKeys.cdStatusIcon) as? NSImageView {
            iconView.contentTintColor = enabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        }
    }

    @objc func onRetentionDaysChangedCD() {
        guard !isSyncingSettings else { return }
        guard let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdDaysStepper
        ) as? NSStepper else { return }
        let days = stepper.integerValue
        if let label = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdDaysLabel
        ) as? NSTextField {
            label.stringValue = "\(days) дн. (1–3650)"
        }
        applySettingsPatch(["auto_purge_retention_days": days])
    }

    // MARK: - Sync from backend settings (called by syncSettingsControls)

    /// Обновляет контролы из свежих настроек (вызывается из syncSettingsControls).
    @MainActor
    func syncRetentionSettings(enabled: Bool, retentionDays: Int) {
        // Gemini variant
        if let toggle = objc_getAssociatedObject(
            self, &RetentionAssocKeys.enabledToggle
        ) as? NSButton {
            toggle.state = enabled ? .on : .off
        }
        if let iconView = objc_getAssociatedObject(self, &RetentionAssocKeys.statusIcon) as? NSImageView {
            iconView.contentTintColor = enabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        }
        if let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.daysStepper
        ) as? NSStepper {
            stepper.integerValue = retentionDays
            stepper.isEnabled = enabled
        }
        if let label = objc_getAssociatedObject(
            self, &RetentionAssocKeys.daysLabel
        ) as? NSTextField {
            label.stringValue = "\(retentionDays) дн. (1–3650)"
        }

        // Claude Design variant
        if let toggle = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdEnabledToggle
        ) as? NSButton {
            toggle.state = enabled ? .on : .off
        }
        if let iconView = objc_getAssociatedObject(self, &RetentionAssocKeys.cdStatusIcon) as? NSImageView {
            iconView.contentTintColor = enabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
        }
        if let stepper = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdDaysStepper
        ) as? NSStepper {
            stepper.integerValue = retentionDays
            stepper.isEnabled = enabled
        }
        if let label = objc_getAssociatedObject(
            self, &RetentionAssocKeys.cdDaysLabel
        ) as? NSTextField {
            label.stringValue = "\(retentionDays) дн. (1–3650)"
        }
    }
}
