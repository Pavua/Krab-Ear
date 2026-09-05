/*
 * Webhooks (WebhookManager) — секция настроек.
 *
 * IPC-контракты (сверено буква-в-букву с backend/webhook_manager.py):
 *   - register_webhook {"url": String, "events": [String], "secret": String}
 *       -> {"webhook_id": "<uuid>"} или {"ok": false, "reason": "webhook_limit_reached"}
 *   - unregister_webhook {"webhook_id": String}
 *       -> {"removed": Bool}
 *   - list_webhooks {}
 *       -> {"webhooks": [{"webhook_id": String, "url": String, "events": [String], "has_secret": Bool, "enabled": Bool, "created_at": String, "deliveries": Int, "failures": Int, "last_status": Int?}]}
 *
 * Правила AGENT-3: IPC строго в DispatchQueue.global, мутации UI — строго в DispatchQueue.main.
 */

import AppKit
import Foundation

// MARK: - Associated-object ключи

enum WebhookManagerAssocKeys {
    nonisolated(unsafe) static var sectionCard: UInt8 = 0
    nonisolated(unsafe) static var urlField: UInt8 = 0
    nonisolated(unsafe) static var eventsField: UInt8 = 0
    nonisolated(unsafe) static var secretField: UInt8 = 0
}

extension HistoryPanelController {

    /// Строит секцию «Webhooks» для Gemini-дизайна (settingsBar).
    // MARK: - Helpers

    @MainActor
    func makeWebhookUrlField() -> NSTextField {
        let urlField = NSTextField(frame: .zero)
        urlField.placeholderString = "URL (напр. https://...)"
        urlField.font = KrabEarTheme.Typography.body
        urlField.bezelStyle = .roundedBezel
        urlField.isBordered = true
        urlField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        return urlField
    }

    @MainActor
    func makeWebhookSubmitButton() -> ThemePrimaryButton {
        let submitButton = ThemePrimaryButton(title: "Добавить webhook", target: self, action: #selector(onRegisterWebhook(_:)))
        submitButton.setContentHuggingPriority(.required, for: .horizontal)
        return submitButton
    }

    @MainActor
    func makeWebhookEventsField() -> NSTextField {
        let eventsField = NSTextField(frame: .zero)
        eventsField.placeholderString = "События (через запятую, пусто = все)"
        eventsField.font = KrabEarTheme.Typography.body
        eventsField.bezelStyle = .roundedBezel
        eventsField.isBordered = true
        eventsField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        eventsField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        return eventsField
    }

    @MainActor
    func makeWebhookSecretField() -> NSSecureTextField {
        let secretField = NSSecureTextField(frame: .zero)
        secretField.placeholderString = "Секрет (опционально, мин. 16 символов)"
        secretField.font = KrabEarTheme.Typography.body
        secretField.bezelStyle = .roundedBezel
        secretField.isBordered = true
        secretField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        secretField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        return secretField
    }

    func buildWebhookManagerSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "webhook_manager",
            title: "Webhooks",
            isExpanded: false,
            iconSymbol: "network"
        )

        let card = ThemeCardView()

        // 1. Форма добавления

        // URL Field
        let urlField = NSTextField(frame: .zero)
        urlField.placeholderString = "https://example.com/hook"
        urlField.font = KrabEarTheme.Typography.body
        urlField.bezelStyle = .roundedBezel
        urlField.isBordered = true
        urlField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        urlField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.urlField, urlField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Events Field
        let eventsField = makeWebhookEventsField()
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.eventsField, eventsField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Secret Field
        let secretField = makeWebhookSecretField()
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.secretField, secretField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Submit Button
        let submitButton = ThemePrimaryButton(title: "Зарегистрировать", target: self, action: #selector(onRegisterWebhook(_:)))
        submitButton.setContentHuggingPriority(.required, for: .horizontal)

        // Compositing form
        let formRow1 = makeSettingRow(label: "URL", control: urlField)
        let formRow2 = makeSettingRow(label: "События", control: eventsField)
        let formRow3 = makeSettingRow(label: "Секрет", control: secretField)
        
        let submitRow = NSStackView(views: [submitButton])
        submitRow.orientation = .horizontal
        submitRow.alignment = .trailing
        submitRow.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)
        
        let formStack = NSStackView(views: [formRow1, formRow2, formRow3, submitRow])
        formStack.orientation = .vertical
        formStack.spacing = KrabEarTheme.Metrics.tight
        formStack.alignment = .leading

        let mainRow = makeSettingRow(
            label: "Новый Webhook",
            description: "Укажите URL для получения событий.",
            control: formStack
        )

        // Загрузочная карточка
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary

        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.sectionCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        card.contentStackView.addArrangedSubview(mainRow)
        card.contentStackView.addArrangedSubview(webhookMakeSeparator())
        card.contentStackView.addArrangedSubview(loadingLabel)

        section.contentStackView.addArrangedSubview(card)

        // Первоначальная загрузка списка
        fetchAndRebuildWebhookCard()

        return section
    }

    // MARK: - Загрузка списка

    func fetchAndRebuildWebhookCard() {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let webhooks: [[String: Any]]
            do {
                let resp = try ipc.call(method: "list_webhooks", params: [:])
                let result = resp["result"] as? [String: Any]
                webhooks = result?["webhooks"] as? [[String: Any]] ?? []
            } catch {
                webhooks = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.rebuildWebhookCard(webhooks: webhooks)
            }
        }
    }

    @MainActor
    private func rebuildWebhookCard(webhooks: [[String: Any]]) {
        guard let card = objc_getAssociatedObject(self, &WebhookManagerAssocKeys.sectionCard) as? NSView else { return }

        let isCD = card is CDSettingsCardView
        let stack = (card as? ThemeCardView)?.contentStackView ?? (card as? CDSettingsCardView)?.contentStackView
        guard let contentStack = stack else { return }
        let arrangedViews = contentStack.arrangedSubviews
        // Сохраняем первые 2 вьюхи (форму добавления и разделитель)
        for v in arrangedViews.dropFirst(2) {
            contentStack.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        let subhead = isCD ? NSTextField(labelWithString: "ЗАРЕГИСТРИРОВАННЫЕ WEBHOOKS") : makeSubhead("ЗАРЕГИСТРИРОВАННЫЕ WEBHOOKS")
        if isCD {
            subhead.font = KrabEarTheme.Typography.captionMedium
            subhead.textColor = KrabEarTheme.Colors.textSecondary
            subhead.isBordered = false
            subhead.drawsBackground = false
        }
        contentStack.addArrangedSubview(subhead)

        if webhooks.isEmpty {
            let empty = NSTextField(labelWithString: "Нет зарегистрированных вебхуков")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            contentStack.addArrangedSubview(empty)
        } else {
            for (index, webhook) in webhooks.enumerated() {
                guard let id = webhook["webhook_id"] as? String,
                      let url = webhook["url"] as? String else { continue }
                
                let events = webhook["events"] as? [String] ?? []
                let hasSecret = webhook["has_secret"] as? Bool ?? false
                let deliveries = webhook["deliveries"] as? Int ?? 0
                let failures = webhook["failures"] as? Int ?? 0
                let lastStatus = webhook["last_status"] as? Int
                
                if isCD && index > 0 {
                    contentStack.addArrangedSubview(cdMakeSeparator())
                }
                
                let row = makeWebhookRow(
                    id: id,
                    url: url,
                    events: events,
                    hasSecret: hasSecret,
                    deliveries: deliveries,
                    failures: failures,
                    lastStatus: lastStatus,
                    isCD: isCD
                )
                contentStack.addArrangedSubview(row)
            }
        }
    }

    @MainActor
    private func makeWebhookRow(id: String, url: String, events: [String], hasSecret: Bool, deliveries: Int, failures: Int, lastStatus: Int?, isCD: Bool = false) -> NSView {
        let urlLabel = NSTextField(labelWithString: url)
        urlLabel.font = isCD ? KrabEarTheme.Typography.body : NSFont.systemFont(ofSize: 13, weight: .medium)
        urlLabel.textColor = KrabEarTheme.Colors.textPrimary
        urlLabel.lineBreakMode = .byTruncatingMiddle
        urlLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        var subtitleParts: [String] = []
        if events.isEmpty {
            subtitleParts.append("События: все")
        } else {
            subtitleParts.append("События: \(events.joined(separator: ", "))")
        }
        subtitleParts.append("Доставлено: \(deliveries), Ошибок: \(failures)")
        
        let descLabel = NSTextField(labelWithString: subtitleParts.joined(separator: " · "))
        descLabel.font = KrabEarTheme.Typography.caption
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.lineBreakMode = .byTruncatingTail
        descLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        
        let textStack = NSStackView(views: [urlLabel, descLabel])
        textStack.orientation = .vertical
        textStack.alignment = .leading
        textStack.spacing = 2
        textStack.setContentHuggingPriority(.defaultLow, for: .horizontal)

        var badges: [NSView] = []
        if hasSecret {
            let badge = isCD ? cdMakeBadge(text: "Секрет", color: KrabEarTheme.Colors.accent) : makeBadge(text: "Секрет", color: KrabEarTheme.Colors.accent, tooltip: "Используется HMAC подпись", symbol: "lock.fill")
            badges.append(badge)
        }

        let statusText: String
        let statusColor: NSColor
        if let status = lastStatus {
            statusText = String(status)
            statusColor = (200...299).contains(status) ? KrabEarTheme.Colors.success : KrabEarTheme.Colors.error
        } else {
            statusText = "—"
            statusColor = KrabEarTheme.Colors.textSecondary
        }
        
        let statusBadge = isCD ? cdMakeBadge(text: statusText, color: statusColor) : makeBadge(text: statusText, color: statusColor, tooltip: "HTTP-статус последней доставки", symbol: nil)
        badges.append(statusBadge)

        let deleteButton = NSButton(frame: .zero)
        deleteButton.bezelStyle = .inline
        deleteButton.isBordered = false
        if let img = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Удалить") {
            deleteButton.image = img
            deleteButton.imageScaling = .scaleProportionallyDown
            deleteButton.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        }
        deleteButton.contentTintColor = KrabEarTheme.Colors.error
        deleteButton.toolTip = "Удалить webhook"
        deleteButton.setAccessibilityLabel("Удалить webhook")
        deleteButton.identifier = NSUserInterfaceItemIdentifier(id)
        deleteButton.target = self
        deleteButton.action = #selector(onUnregisterWebhook(_:))
        deleteButton.setContentHuggingPriority(.required, for: .horizontal)

        let badgesStack = NSStackView(views: badges)
        badgesStack.orientation = .horizontal
        badgesStack.spacing = KrabEarTheme.Metrics.tight
        badgesStack.alignment = .centerY

        let trailingStack = NSStackView(views: [badgesStack, deleteButton])
        trailingStack.orientation = .horizontal
        trailingStack.spacing = KrabEarTheme.Metrics.standard
        trailingStack.alignment = .centerY
        trailingStack.setContentHuggingPriority(.required, for: .horizontal)

        let row = NSStackView(views: [textStack, trailingStack])
        row.orientation = .horizontal
        row.distribution = .fill
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.standard
        row.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)
        return row
    }

    // MARK: - Обработчики действий

    @objc private func onRegisterWebhook(_ sender: Any) {
        guard let urlField = objc_getAssociatedObject(self, &WebhookManagerAssocKeys.urlField) as? NSTextField,
              let eventsField = objc_getAssociatedObject(self, &WebhookManagerAssocKeys.eventsField) as? NSTextField,
              let secretField = objc_getAssociatedObject(self, &WebhookManagerAssocKeys.secretField) as? NSSecureTextField else { return }

        let url = urlField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if url.isEmpty {
            showInfoAlert(title: "Регистрация Webhook", body: "URL не может быть пустым.")
            return
        }

        let eventsStr = eventsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        var events: [String] = []
        if !eventsStr.isEmpty {
            events = eventsStr.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        }

        let secret = secretField.stringValue
        
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "register_webhook", params: [
                    "url": url,
                    "events": events,
                    "secret": secret
                ])
                
                let result = resp["result"] as? [String: Any] ?? resp
                
                if let ok = result["ok"] as? Bool, !ok {
                    if let reason = result["reason"] as? String, reason == "webhook_limit_reached" {
                        DispatchQueue.main.async {
                            BackendToast.shared.show("Достигнут лимит webhook-ов", duration: 4.0)
                        }
                    } else {
                        let errorMessage = (resp["error"] as? [String: Any])?["message"] as? String ?? "Неизвестная ошибка"
                        DispatchQueue.main.async {
                            BackendToast.shared.show("Ошибка: \(errorMessage)", duration: 4.0)
                        }
                    }
                    return
                }
                
                DispatchQueue.main.async {
                    // Успех, сброс полей
                    urlField.stringValue = ""
                    eventsField.stringValue = ""
                    secretField.stringValue = ""
                    BackendToast.shared.show("Webhook зарегистрирован", duration: 2.0)
                }
                self.fetchAndRebuildWebhookCard()
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка IPC: \(error.localizedDescription)", duration: 4.0)
                }
            }
        }
    }

    @objc private func onUnregisterWebhook(_ sender: NSButton) {
        guard let webhookId = sender.identifier?.rawValue, !webhookId.isEmpty else { return }

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let resp = try ipc.call(method: "unregister_webhook", params: ["webhook_id": webhookId])
                
                // Проверяем removed в result или корне
                let removedInRoot = resp["removed"] as? Bool
                let resultObj = resp["result"] as? [String: Any]
                let removedInResult = resultObj?["removed"] as? Bool
                
                if let isRemoved = removedInRoot ?? removedInResult, isRemoved {
                    DispatchQueue.main.async {
                        BackendToast.shared.show("Webhook удалён", duration: 2.0)
                    }
                } else {
                    let errorMessage = (resp["error"] as? [String: Any])?["message"] as? String ?? "Не удалось удалить webhook"
                    DispatchQueue.main.async {
                        BackendToast.shared.show("Ошибка удаления: \(errorMessage)", duration: 4.0)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    BackendToast.shared.show("Ошибка IPC: \(error.localizedDescription)", duration: 4.0)
                }
            }
            self.fetchAndRebuildWebhookCard()
        }
    }

    @MainActor
    private func webhookMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    // MARK: - CD Builders

    @MainActor
    func cdBuildWebhookManagerSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_webhook_manager",
            title: "Webhooks",
            isExpanded: false
        )
        let card = CDSettingsCardView()
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.sectionCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let urlField = makeWebhookUrlField()
        let eventsField = makeWebhookEventsField()
        let secretField = makeWebhookSecretField()
        let submitButton = makeWebhookSubmitButton()
        
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.urlField, urlField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.eventsField, eventsField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &WebhookManagerAssocKeys.secretField, secretField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        
        let urlRow = cdMakeRow(label: "URL", control: urlField)
        let eventsRow = cdMakeRow(label: "События", control: eventsField)
        let secretRow = cdMakeRow(label: "Секрет", control: secretField)

        let submitRow = NSStackView(views: [submitButton])
        submitRow.orientation = .horizontal
        submitRow.alignment = .trailing
        submitRow.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 4, right: 0)

        let formStack = NSStackView(views: [
            urlRow,
            cdMakeSeparator(),
            eventsRow,
            cdMakeSeparator(),
            secretRow,
            cdMakeSeparator(),
            submitRow,
        ])
        formStack.orientation = .vertical
        formStack.spacing = KrabEarTheme.Metrics.tight
        formStack.alignment = .leading
        
        card.contentStackView.addArrangedSubview(formStack)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        
        section.contentStackView.addArrangedSubview(card)
        
        fetchAndRebuildWebhookCard()
        
        return section
    }

}
