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
    
    nonisolated(unsafe) static var customUrlField: UInt8 = 0
    nonisolated(unsafe) static var customUrlRow: UInt8 = 0
    nonisolated(unsafe) static var customModelField: UInt8 = 0
    nonisolated(unsafe) static var customModelRow: UInt8 = 0
    nonisolated(unsafe) static var customPrivacyWarnLabel: UInt8 = 0
    
    nonisolated(unsafe) static var cdToggle: UInt8 = 0
    nonisolated(unsafe) static var cdProviderPicker: UInt8 = 0
    nonisolated(unsafe) static var cdApiKeyField: UInt8 = 0
    nonisolated(unsafe) static var cdPrivacyWarnLabel: UInt8 = 0
    
    nonisolated(unsafe) static var cdCustomUrlField: UInt8 = 0
    nonisolated(unsafe) static var cdCustomUrlRow: UInt8 = 0
    nonisolated(unsafe) static var cdCustomModelField: UInt8 = 0
    nonisolated(unsafe) static var cdCustomModelRow: UInt8 = 0
    nonisolated(unsafe) static var cdCustomPrivacyWarnLabel: UInt8 = 0
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
        providerPicker.addItems(withTitles: ["OpenAI", "Anthropic", "Custom (свой сервер)"])
        providerPicker.target = self
        providerPicker.action = #selector(onCloudRewriterProviderChanged(_:))
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.providerPicker, providerPicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let providerRow = makeSettingRow(label: "Провайдер", control: providerPicker)
        
        let customUrlField = NSTextField(frame: .zero)
        customUrlField.placeholderString = "http://localhost:11434/v1"
        customUrlField.font = KrabEarTheme.Typography.body
        customUrlField.target = self
        customUrlField.action = #selector(onCloudRewriterCustomUrlChanged(_:))
        customUrlField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.customUrlField, customUrlField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let customUrlRow = makeSettingRow(label: "URL endpoint", control: customUrlField)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.customUrlRow, customUrlRow, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        let customModelField = NSTextField(frame: .zero)
        customModelField.placeholderString = "qwen2.5:7b"
        customModelField.font = KrabEarTheme.Typography.body
        customModelField.target = self
        customModelField.action = #selector(onCloudRewriterCustomModelChanged(_:))
        customModelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.customModelField, customModelField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let customModelRow = makeSettingRow(label: "Модель", control: customModelField)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.customModelRow, customModelRow, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        let customPrivacyWarnLabel = NSTextField(labelWithString: "✅ Рекомендуется для приватности: укажите свой self-hosted сервер (Ollama/vLLM) или no-log провайдера — транскрипты идут только туда.")
        customPrivacyWarnLabel.font = KrabEarTheme.Typography.caption
        customPrivacyWarnLabel.textColor = KrabEarTheme.Colors.textSecondary
        customPrivacyWarnLabel.isEditable = false
        customPrivacyWarnLabel.isSelectable = false
        customPrivacyWarnLabel.isBordered = false
        customPrivacyWarnLabel.drawsBackground = false
        customPrivacyWarnLabel.lineBreakMode = .byWordWrapping
        customPrivacyWarnLabel.preferredMaxLayoutWidth = 300
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.customPrivacyWarnLabel, customPrivacyWarnLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
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
        card.contentStackView.addArrangedSubview(customUrlRow)
        card.contentStackView.addArrangedSubview(customModelRow)
        card.contentStackView.addArrangedSubview(apiKeyRow)
        card.contentStackView.addArrangedSubview(customPrivacyWarnLabel)
        
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
        providerPicker.addItems(withTitles: ["OpenAI", "Anthropic", "Custom (свой сервер)"])
        providerPicker.target = self
        providerPicker.action = #selector(onCloudRewriterProviderChanged(_:))
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdProviderPicker, providerPicker, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let providerRow = cdMakeRow(label: "Провайдер", control: providerPicker)
        
        let customUrlField = NSTextField(frame: .zero)
        customUrlField.placeholderString = "http://localhost:11434/v1"
        customUrlField.font = .systemFont(ofSize: 12, weight: .regular)
        customUrlField.target = self
        customUrlField.action = #selector(onCloudRewriterCustomUrlChanged(_:))
        customUrlField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdCustomUrlField, customUrlField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let customUrlRow = cdMakeRow(label: "URL endpoint", control: customUrlField)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdCustomUrlRow, customUrlRow, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        let customModelField = NSTextField(frame: .zero)
        customModelField.placeholderString = "qwen2.5:7b"
        customModelField.font = .systemFont(ofSize: 12, weight: .regular)
        customModelField.target = self
        customModelField.action = #selector(onCloudRewriterCustomModelChanged(_:))
        customModelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdCustomModelField, customModelField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        let customModelRow = cdMakeRow(label: "Модель", control: customModelField)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdCustomModelRow, customModelRow, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        let customPrivacyWarnLabel = NSTextField(labelWithString: "✅ Рекомендуется для приватности: укажите свой self-hosted сервер (Ollama/vLLM) или no-log провайдера — транскрипты идут только туда.")
        customPrivacyWarnLabel.font = .systemFont(ofSize: 11, weight: .regular)
        customPrivacyWarnLabel.textColor = KrabEarTheme.Colors.textSecondary
        customPrivacyWarnLabel.isEditable = false
        customPrivacyWarnLabel.isSelectable = false
        customPrivacyWarnLabel.isBordered = false
        customPrivacyWarnLabel.drawsBackground = false
        customPrivacyWarnLabel.lineBreakMode = .byWordWrapping
        customPrivacyWarnLabel.preferredMaxLayoutWidth = 300
        let cdCustomWarnContainer = NSStackView()
        cdCustomWarnContainer.orientation = .vertical
        cdCustomWarnContainer.edgeInsets = NSEdgeInsets(top: 4, left: 16, bottom: 8, right: 16)
        cdCustomWarnContainer.addArrangedSubview(customPrivacyWarnLabel)
        objc_setAssociatedObject(self, &CloudRewriterAssocKeys.cdCustomPrivacyWarnLabel, cdCustomWarnContainer, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
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
        card.contentStackView.addArrangedSubview(customUrlRow)
        card.contentStackView.addArrangedSubview(customModelRow)
        card.contentStackView.addArrangedSubview(apiKeyRow)
        card.contentStackView.addArrangedSubview(cdCustomWarnContainer)
        
        section.contentStackView.addArrangedSubview(card)
        return section
    }
    
    // MARK: - Handlers
    
    @objc func onCloudRewriterEnabledChanged(_ sender: NSButton) {
        guard !isSyncingSettings else { return }
        let enabled = sender.state == .on
        applySettingsPatch(["cloud_rewriter_enabled": enabled])
    }
    
    /// Текущий провайдер по состоянию пикера (для записи модели в верный ключ).
    /// Читаем именно контрол, а не снапшот настроек: пользователь мог сменить
    /// провайдера и тут же поправить модель, до прихода нового снапшота.
    func currentCloudRewriterProvider() -> String {
        guard let picker = objc_getAssociatedObject(
            self, &CloudRewriterAssocKeys.providerPicker
        ) as? NSPopUpButton else { return "openai" }
        switch picker.indexOfSelectedItem {
        case 1:  return "anthropic"
        case 2:  return "custom"
        default: return "openai"
        }
    }

    @objc func onCloudRewriterProviderChanged(_ sender: NSPopUpButton) {
        guard !isSyncingSettings else { return }
        let provider = sender.indexOfSelectedItem == 0 ? "openai" : (sender.indexOfSelectedItem == 1 ? "anthropic" : "custom")
        applySettingsPatch(["cloud_rewriter_provider": provider])
    }
    
    @objc func onCloudRewriterCustomUrlChanged(_ sender: NSTextField) {
        guard !isSyncingSettings else { return }
        let val = sender.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        applySettingsPatch(["cloud_rewriter_base_url": val])
    }
    
    @objc func onCloudRewriterCustomModelChanged(_ sender: NSTextField) {
        guard !isSyncingSettings else { return }
        let val = sender.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        // Одно поле «Модель» на все провайдеры: пишем в ключ ТЕКУЩЕГО, иначе
        // пользователь менял бы модель у одного, а видел её у другого.
        applySettingsPatch([Self.cloudRewriterModelSettingKey(for: currentCloudRewriterProvider()): val])
    }

    /// Ключ настройки модели для провайдера. Разные провайдеры — разные линейки
    /// имён, поэтому значения не переиспользуются между ними.
    static func cloudRewriterModelSettingKey(for provider: String) -> String {
        switch provider {
        case "anthropic": return "cloud_rewriter_anthropic_model"
        case "custom":    return "cloud_rewriter_custom_model"
        default:          return "cloud_rewriter_openai_model"
        }
    }

    /// Значение модели текущего провайдера из снапшота настроек.
    static func cloudRewriterModelValue(for provider: String, settings: AgentSettings) -> String {
        switch provider {
        case "anthropic": return settings.cloudRewriterAnthropicModel
        case "custom":    return settings.cloudRewriterCustomModel
        default:          return settings.cloudRewriterOpenaiModel
        }
    }
    
    @objc func onCloudRewriterApiKeyChanged(_ sender: NSSecureTextField) {
        guard !isSyncingSettings else { return }
        let apiKey = sender.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        
        var provider = "openai"
        if let picker = objc_getAssociatedObject(self, &CloudRewriterAssocKeys.providerPicker) as? NSPopUpButton {
            provider = picker.indexOfSelectedItem == 0 ? "openai" : (picker.indexOfSelectedItem == 1 ? "anthropic" : "custom")
        } else if let cdPicker = objc_getAssociatedObject(self, &CloudRewriterAssocKeys.cdProviderPicker) as? NSPopUpButton {
            provider = cdPicker.indexOfSelectedItem == 0 ? "openai" : (cdPicker.indexOfSelectedItem == 1 ? "anthropic" : "custom")
        }
        
        let patchKey: String
        if provider == "openai" { patchKey = "openai_api_key" }
        else if provider == "anthropic" { patchKey = "anthropic_api_key" }
        else { patchKey = "cloud_rewriter_api_key" }
        
        applySettingsPatch([patchKey: apiKey])
    }
    
    // MARK: - Sync
    
    @MainActor
    func syncCloudRewriterControls(settings: AgentSettings) {
        let isPrivacyMode = settings.privacyModeEnabled
        let isEnabled = settings.cloudRewriterEnabled && !isPrivacyMode
        let provider = settings.cloudRewriterProvider
        
        let syncGroup = { (toggleKey: UnsafeRawPointer, pickerKey: UnsafeRawPointer, fieldKey: UnsafeRawPointer, warnKey: UnsafeRawPointer,
                           customUrlFieldKey: UnsafeRawPointer, customUrlRowKey: UnsafeRawPointer,
                           customModelFieldKey: UnsafeRawPointer, customModelRowKey: UnsafeRawPointer,
                           customWarnKey: UnsafeRawPointer) in
            
            let isCustom = (provider == "custom")
            
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
                let idx: Int
                if provider == "anthropic" { idx = 1 }
                else if provider == "custom" { idx = 2 }
                else { idx = 0 }
                picker.selectItem(at: idx)
            }
            
            if let field = objc_getAssociatedObject(self, fieldKey) as? NSSecureTextField {
                let val: String
                if provider == "anthropic" { val = settings.anthropicApiKey }
                else if provider == "custom" { val = settings.cloudRewriterApiKey }
                else { val = settings.openaiApiKey }
                
                if field.stringValue != val {
                    field.stringValue = val
                }
                
                field.placeholderString = isCustom ? "API ключ (опционально)" : "API ключ"
            }
            
            if let urlRow = objc_getAssociatedObject(self, customUrlRowKey) as? NSView { urlRow.isHidden = !isCustom }
            // Строка «Модель» видна ВСЕГДА: модель есть у каждого провайдера,
            // раньше её можно было задать только для self-hosted.
            if let modelRow = objc_getAssociatedObject(self, customModelRowKey) as? NSView { modelRow.isHidden = false }
            if let customWarn = objc_getAssociatedObject(self, customWarnKey) as? NSView { customWarn.isHidden = !isCustom }
            
            if let urlField = objc_getAssociatedObject(self, customUrlFieldKey) as? NSTextField {
                if urlField.stringValue != settings.cloudRewriterBaseUrl {
                    urlField.stringValue = settings.cloudRewriterBaseUrl
                }
            }
            if let modelField = objc_getAssociatedObject(self, customModelFieldKey) as? NSTextField {
                let modelValue = Self.cloudRewriterModelValue(for: provider, settings: settings)
                if modelField.stringValue != modelValue {
                    modelField.stringValue = modelValue
                }
                modelField.placeholderString = isCustom ? "qwen2.5:7b" : "модель провайдера"
            }
            
            if let warnLabel = objc_getAssociatedObject(self, warnKey) as? NSTextField {
                if isPrivacyMode {
                    warnLabel.textColor = .systemRed
                } else {
                    warnLabel.textColor = KrabEarTheme.Colors.textSecondary
                }
                
                warnLabel.isHidden = isCustom
                if let superview = warnLabel.superview as? NSStackView, superview.arrangedSubviews.count == 1 {
                    superview.isHidden = isCustom
                }
            }
        }
        
        syncGroup(&CloudRewriterAssocKeys.toggle, &CloudRewriterAssocKeys.providerPicker, &CloudRewriterAssocKeys.apiKeyField, &CloudRewriterAssocKeys.privacyWarnLabel,
                  &CloudRewriterAssocKeys.customUrlField, &CloudRewriterAssocKeys.customUrlRow,
                  &CloudRewriterAssocKeys.customModelField, &CloudRewriterAssocKeys.customModelRow,
                  &CloudRewriterAssocKeys.customPrivacyWarnLabel)
                  
        syncGroup(&CloudRewriterAssocKeys.cdToggle, &CloudRewriterAssocKeys.cdProviderPicker, &CloudRewriterAssocKeys.cdApiKeyField, &CloudRewriterAssocKeys.cdPrivacyWarnLabel,
                  &CloudRewriterAssocKeys.cdCustomUrlField, &CloudRewriterAssocKeys.cdCustomUrlRow,
                  &CloudRewriterAssocKeys.cdCustomModelField, &CloudRewriterAssocKeys.cdCustomModelRow,
                  &CloudRewriterAssocKeys.cdCustomPrivacyWarnLabel)
    }
}
