/*
 HistoryPanelController+CloudRewriter.swift

 GUI для настроек облачной полировки (cloud rewriter)
 */

import AppKit
import Foundation

private enum CloudRewriterAssocKeys {
    nonisolated(unsafe) static var toggle: UInt8 = 0
    nonisolated(unsafe) static var providerPicker: UInt8 = 0
    nonisolated(unsafe) static var apiKeyField: UInt8 = 0
    nonisolated(unsafe) static var privacyWarnLabel: UInt8 = 0
    
    nonisolated(unsafe) static var cdToggle: UInt8 = 0
    nonisolated(unsafe) static var cdProviderPicker: UInt8 = 0
    nonisolated(unsafe) static var cdApiKeyField: UInt8 = 0
    nonisolated(unsafe) static var cdPrivacyWarnLabel: UInt8 = 0
}

extension HistoryPanelController {
    
    // MARK: - Gemini Variant
    
    @MainActor
    func buildCloudRewriterSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cloud_rewriter",
            title: "Облачная полировка",
            isExpanded: false
        )
        let card = ThemeCardView()
        
        let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onCloudRewriterEnabledChanged(_:)))
        toggle.setButtonType(.switch)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.toggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let toggleRow = makeSettingRow(label: "Включить облачную полировку (fallback)", control: toggle)
        
        let providerPicker = NSPopUpButton(frame: .zero, pullsDown: false)
        providerPicker.addItems(withTitles: ["OpenAI", "Anthropic"])
        providerPicker.target = self
        providerPicker.action = #selector(onCloudRewriterProviderChanged(_:))
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.providerPicker, providerPicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let providerRow = makeSettingRow(label: "Провайдер", control: providerPicker)
        
        let apiKeyField = NSSecureTextField(frame: .zero)
        apiKeyField.placeholderString = "API ключ"
        apiKeyField.font = KrabEarTheme.Typography.body
        apiKeyField.target = self
        apiKeyField.action = #selector(onCloudRewriterApiKeyChanged(_:))
        apiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.apiKeyField, apiKeyField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let apiKeyRow = makeSettingRow(label: "API-ключ", control: apiKeyField)
        
        let privacyWarnLabel = NSTextField(labelWithString: "⚠️ При включении ваши транскрипты отправляются выбранному облачному провайдеру для полировки. Это нарушает локально-приватный режим. Не включайте для конфиденциального контента. В режиме приватности фича автоматически отключена.")
        privacyWarnLabel.font = KrabEarTheme.Typography.caption
        privacyWarnLabel.textColor = KrabEarTheme.Colors.textSecondary
        privacyWarnLabel.isEditable = false
        privacyWarnLabel.isSelectable = false
        privacyWarnLabel.isBordered = false
        privacyWarnLabel.drawsBackground = false
        privacyWarnLabel.lineBreakMode = .byWordWrapping
        privacyWarnLabel.preferredMaxLayoutWidth = 300
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.privacyWarnLabel, privacyWarnLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(privacyWarnLabel)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(providerRow)
        card.contentStackView.addArrangedSubview(makeSeparator())
        card.contentStackView.addArrangedSubview(apiKeyRow)
        
        section.contentStackView.addArrangedSubview(card)
        return section
    }
    
    // MARK: - Claude Design Variant
    
    @MainActor
    func cdBuildCloudRewriterSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cloud_rewriter",
            title: "Облачная полировка",
            isExpanded: false
        )
        let card = CDSettingsCardView()
        
        let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onCloudRewriterEnabledChanged(_:)))
        toggle.setButtonType(.switch)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let toggleRow = cdMakeRow(label: "Включить облачную полировку (fallback)", control: toggle)
        
        let providerPicker = NSPopUpButton(frame: .zero, pullsDown: false)
        providerPicker.addItems(withTitles: ["OpenAI", "Anthropic"])
        providerPicker.target = self
        providerPicker.action = #selector(onCloudRewriterProviderChanged(_:))
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdProviderPicker, providerPicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let providerRow = cdMakeRow(label: "Провайдер", control: providerPicker)
        
        let apiKeyField = NSSecureTextField(frame: .zero)
        apiKeyField.placeholderString = "API ключ"
        apiKeyField.font = .systemFont(ofSize: 12, weight: .regular)
        apiKeyField.target = self
        apiKeyField.action = #selector(onCloudRewriterApiKeyChanged(_:))
        apiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdApiKeyField, apiKeyField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let apiKeyRow = cdMakeRow(label: "API-ключ", control: apiKeyField)
        
        let privacyWarnLabel = NSTextField(labelWithString: "⚠️ При включении ваши транскрипты отправляются выбранному облачному провайдеру для полировки. Это нарушает локально-приватный режим. Не включайте для конфиденциального контента. В режиме приватности фича автоматически отключена.")
        privacyWarnLabel.font = .systemFont(ofSize: 11, weight: .regular)
        privacyWarnLabel.textColor = KrabEarTheme.Colors.textSecondary
        privacyWarnLabel.isEditable = false
        privacyWarnLabel.isSelectable = false
        privacyWarnLabel.isBordered = false
        privacyWarnLabel.drawsBackground = false
        privacyWarnLabel.lineBreakMode = .byWordWrapping
        privacyWarnLabel.preferredMaxLayoutWidth = 300
        let warnContainer = NSStackView()
        warnContainer.orientation = .vertical
        warnContainer.edgeInsets = NSEdgeInsets(top: 4, left: 16, bottom: 8, right: 16)
        warnContainer.addArrangedSubview(privacyWarnLabel)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdPrivacyWarnLabel, privacyWarnLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(warnContainer)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(providerRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(apiKeyRow)
        
        section.contentStackView.addArrangedSubview(card)
        return section
    }
    
    // MARK: - Handlers
    
    @objc func onCloudRewriterEnabledChanged(_ sender: NSButton) {
        guard !isSyncingSettings else { return }
        let enabled = sender.state == .on
        applySettingsPatch(["cloud_rewriter_enabled": enabled])
    }
    
    @objc func onCloudRewriterProviderChanged(_ sender: NSPopUpButton) {
        guard !isSyncingSettings else { return }
        let provider = sender.indexOfSelectedItem == 0 ? "openai" : "anthropic"
        applySettingsPatch(["cloud_rewriter_provider": provider])
    }
    
    @objc func onCloudRewriterApiKeyChanged(_ sender: NSSecureTextField) {
        guard !isSyncingSettings else { return }
        let apiKey = sender.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        
        var provider = "openai"
        if let picker = objc_getAssociatedObject(self, &CloudRewriterAssocKeys.providerPicker) as? NSPopUpButton {
            provider = picker.indexOfSelectedItem == 0 ? "openai" : "anthropic"
        } else if let cdPicker = objc_getAssociatedObject(self, &CloudRewriterAssocKeys.cdProviderPicker) as? NSPopUpButton {
            provider = cdPicker.indexOfSelectedItem == 0 ? "openai" : "anthropic"
        }
        
        let patchKey = provider == "openai" ? "openai_api_key" : "anthropic_api_key"
        applySettingsPatch([patchKey: apiKey])
    }
    
    // MARK: - Sync
    
    @MainActor
    func syncCloudRewriterControls(settings: AgentSettings) {
        let isPrivacyMode = settings.privacyModeEnabled
        let isEnabled = settings.cloudRewriterEnabled && !isPrivacyMode
        let provider = settings.cloudRewriterProvider
        let isAnthropic = (provider == "anthropic")
        
        let syncGroup = { (toggleKey: UnsafeRawPointer, pickerKey: UnsafeRawPointer, fieldKey: UnsafeRawPointer, warnKey: UnsafeRawPointer) in
            if let toggle = objc_getAssociatedObject(self, toggleKey) as? NSButton {
                toggle.state = isEnabled ? .on : .off
                toggle.isEnabled = !isPrivacyMode
                
                if isPrivacyMode {
                    toggle.title = " Недоступно в режиме приватности"
                } else {
                    toggle.title = ""
                }
            }
            
            if let picker = objc_getAssociatedObject(self, pickerKey) as? NSPopUpButton {
                picker.selectItem(at: isAnthropic ? 1 : 0)
            }
            
            if let field = objc_getAssociatedObject(self, fieldKey) as? NSSecureTextField {
                let val = isAnthropic ? settings.anthropicApiKey : settings.openaiApiKey
                // To avoid interrupting typing, we check if the string is completely out of sync before setting it.
                // However, `syncSettingsControls` is typically called with isSyncingSettings = true
                if field.stringValue != val {
                    field.stringValue = val
                }
            }
            
            if let warnLabel = objc_getAssociatedObject(self, warnKey) as? NSTextField {
                if isPrivacyMode {
                    warnLabel.textColor = .systemRed
                } else {
                    warnLabel.textColor = KrabEarTheme.Colors.textSecondary
                }
            }
        }
        
        syncGroup(&CloudRewriterAssocKeys.toggle, &CloudRewriterAssocKeys.providerPicker, &CloudRewriterAssocKeys.apiKeyField, &CloudRewriterAssocKeys.privacyWarnLabel)
        syncGroup(&CloudRewriterAssocKeys.cdToggle, &CloudRewriterAssocKeys.cdProviderPicker, &CloudRewriterAssocKeys.cdApiKeyField, &CloudRewriterAssocKeys.cdPrivacyWarnLabel)
    }
}
