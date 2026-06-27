/*
 Безопасность — секция настроек шифрования истории на диске.

 Использует IPC-методы (backend/service.py):
   - get_encryption_status {}
       → {ok:true, enabled:bool, available:bool}
   - set_history_encryption {enabled:Bool}
       → {ok:true, enabled:bool, available:bool}
       | {ok:false, error:"keychain_unavailable", enabled:bool}
   - get_history_encryption_status {}
       → {ok:true, enabled:bool, total:int, encrypted:int, plaintext:int,
          pct:int, migrating:bool}
   - migrate_history_encryption {}
       → {ok:true, status:"started"|"already_running"}
       | {ok:false, status:"encryption_unavailable"}

 Архитектура:
   - buildSecuritySettingsSection()   — вариант для Gemini-дизайна (settingsBar).
   - cdBuildSecuritySettingsSection() — вариант для Claude Design (settingsBarCD).
   - syncSecuritySettings(enabled:available:) — обновляет контролы при смене
     настроек (вызывается из syncSettingsControls).
   - refreshMigrationStatus() — off-main get_history_encryption_status →
     показывает/скрывает кнопку «Зашифровать существующие записи» и счётчик
     незашифрованных записей. Вызывается при загрузке секции и смене тоггла.
   - startHistoryMigration() — confirm sheet → migrate_history_encryption →
     poll get_history_encryption_status каждые ~0.5с → прогресс-бар + счётчик.

 Правила AGENT-3 (AppHang-класс):
   IPC строго в DispatchQueue.global, мутации UI — строго в DispatchQueue.main.
   НИКОГДА runModal() — используется presentAlertSheet для confirm/ошибок.

 Ключ шифрования в Keychain НЕ удаляется при выключении тоггла.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum SecurityAssocKeys {
    nonisolated(unsafe) static var encryptionToggle: UInt8 = 0
    nonisolated(unsafe) static var cdEncryptionToggle: UInt8 = 0
    nonisolated(unsafe) static var statusIcon: UInt8 = 0
    nonisolated(unsafe) static var cdStatusIcon: UInt8 = 0
    nonisolated(unsafe) static var statusBadge: UInt8 = 0
    nonisolated(unsafe) static var cdStatusBadge: UInt8 = 0
    // Миграция «зашифровать существующие записи» (Gemini variant)
    nonisolated(unsafe) static var migrateRow: UInt8 = 0
    nonisolated(unsafe) static var migrateButton: UInt8 = 0
    nonisolated(unsafe) static var migrateProgress: UInt8 = 0
    nonisolated(unsafe) static var migrateStatus: UInt8 = 0
    nonisolated(unsafe) static var migratePolling: UInt8 = 0
}

// MARK: - HistoryPanelController+SecuritySettings

extension HistoryPanelController {

    // MARK: - Gemini variant

    /// Строит секцию «Безопасность» для Gemini-дизайна (settingsBar).
    @MainActor
    func buildSecuritySettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "history_security_settings",
            title: "Безопасность",
            isExpanded: false,
            iconSymbol: "lock.shield"
        )

        let card = ThemeCardView()

        let iconView = NSImageView()
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        if let icon = NSImage(systemSymbolName: "lock.open", accessibilityDescription: nil) {
            iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
        }
        iconView.contentTintColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &SecurityAssocKeys.statusIcon, iconView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let badgeLabel = NSTextField(labelWithString: "Выключено")
        badgeLabel.font = KrabEarTheme.Typography.caption
        badgeLabel.textColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &SecurityAssocKeys.statusBadge, badgeLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Тоггл «Шифровать историю на диске»
        let toggle = NSButton(
            checkboxWithTitle: "",
            target: self,
            action: #selector(onEncryptionToggleChanged)
        )
        toggle.state = .off
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.encryptionToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let controlStack = NSStackView(views: [iconView, badgeLabel, toggle])
        controlStack.orientation = .horizontal
        controlStack.alignment = .centerY
        controlStack.spacing = KrabEarTheme.Metrics.tight

        let toggleRow = makeSettingRow(
            label: "Шифровать историю на диске",
            description: "Новые записи сохраняются в зашифрованном виде (AES-256-GCM). Старые записи остаются как есть.",
            control: controlStack
        )

        // Предупреждение о ключе Keychain
        let warningLabel = NSTextField(
            labelWithString: "⚠️ Ключ шифрования хранится в Связке ключей macOS. НЕ удаляйте его — иначе зашифрованные записи станут НЕДОСТУПНЫ. Новые записи шифруются; старые остаются как есть."
        )
        warningLabel.font = KrabEarTheme.Typography.caption
        warningLabel.textColor = KrabEarTheme.Colors.textSecondary
        warningLabel.lineBreakMode = .byWordWrapping
        warningLabel.maximumNumberOfLines = 4
        warningLabel.setContentCompressionResistancePriority(.defaultHigh, for: .vertical)

        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(warningLabel)

        // --- Миграция существующих записей -----------------------------------
        // Кнопка «Зашифровать существующие записи» + прогресс-бар + счётчик.
        // Скрыта по умолчанию; показывается через refreshMigrationStatus(),
        // когда шифрование включено И plaintext > 0.
        let migrateButton = NSButton(
            title: "Зашифровать существующие записи",
            target: self,
            action: #selector(onMigrateHistoryEncryptionClicked)
        )
        migrateButton.bezelStyle = .rounded
        migrateButton.controlSize = .small
        migrateButton.font = KrabEarTheme.Typography.caption
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.migrateButton, migrateButton, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let migrateProgress = NSProgressIndicator()
        migrateProgress.isIndeterminate = false
        migrateProgress.minValue = 0
        migrateProgress.maxValue = 100
        migrateProgress.doubleValue = 0
        migrateProgress.controlSize = .small
        migrateProgress.isHidden = true
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.migrateProgress, migrateProgress, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let migrateStatus = NSTextField(labelWithString: "")
        migrateStatus.font = KrabEarTheme.Typography.caption
        migrateStatus.textColor = KrabEarTheme.Colors.textSecondary
        migrateStatus.lineBreakMode = .byTruncatingTail
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.migrateStatus, migrateStatus, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let migrateStack = NSStackView(views: [migrateButton, migrateProgress, migrateStatus])
        migrateStack.orientation = .vertical
        migrateStack.alignment = .leading
        migrateStack.spacing = KrabEarTheme.Metrics.tight
        migrateStack.isHidden = true   // показываем только при наличии plaintext-записей
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.migrateRow, migrateStack, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(migrateStack)
        section.contentStackView.addArrangedSubview(card)

        // Загружаем реальный статус из backend при создании секции
        loadEncryptionStatus()
        refreshMigrationStatus()

        return section
    }

    // MARK: - Claude Design variant

    /// Строит секцию «Безопасность» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildSecuritySettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_history_security",
            title: "Безопасность",
            isExpanded: false
        )

        let card = CDSettingsCardView()

        let iconView = NSImageView()
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        if let icon = NSImage(systemSymbolName: "lock.open", accessibilityDescription: nil) {
            iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
        }
        iconView.contentTintColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &SecurityAssocKeys.cdStatusIcon, iconView, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        let badgeLabel = NSTextField(labelWithString: "Выключено")
        badgeLabel.font = .systemFont(ofSize: 12, weight: .regular)
        badgeLabel.textColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(self, &SecurityAssocKeys.cdStatusBadge, badgeLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Тоггл
        let toggle = NSButton(
            checkboxWithTitle: "",
            target: self,
            action: #selector(onEncryptionToggleChangedCD)
        )
        toggle.state = .off
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.cdEncryptionToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let controlStack = NSStackView(views: [iconView, badgeLabel, toggle])
        controlStack.orientation = .horizontal
        controlStack.alignment = .centerY
        controlStack.spacing = 6

        let toggleRow = cdMakeRow(label: "Шифровать историю на диске", control: controlStack)

        // Предупреждение
        let warningLabel = NSTextField(
            labelWithString: "⚠️ Ключ шифрования хранится в Связке ключей macOS. НЕ удаляйте его — иначе зашифрованные записи станут НЕДОСТУПНЫ."
        )
        warningLabel.font = .systemFont(ofSize: 11, weight: .regular)
        warningLabel.textColor = KrabEarTheme.Colors.textSecondary
        warningLabel.lineBreakMode = .byWordWrapping
        warningLabel.maximumNumberOfLines = 3

        card.contentStackView.addArrangedSubview(toggleRow)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(warningLabel)

        section.contentStackView.addArrangedSubview(card)

        return section
    }

    // MARK: - Load status from backend

    /// Загружает статус шифрования из backend и обновляет тоггл.
    /// IPC строго off-main (AGENT-3).
    @MainActor
    func loadEncryptionStatus() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? self.ipcClient.call(method: "get_encryption_status", params: [:]),
                  let result = resp["result"] as? [String: Any] else { return }
            let enabled = result["enabled"] as? Bool ?? false
            let available = result["available"] as? Bool ?? false
            DispatchQueue.main.async {
                self.syncSecuritySettings(enabled: enabled, available: available)
            }
        }
    }

    // MARK: - Migration status (existing plaintext entries)

    /// Off-main get_history_encryption_status → показывает/скрывает кнопку
    /// «Зашифровать существующие записи» и счётчик «N незашифрованных».
    /// Видна, когда шифрование включено И plaintext > 0. IPC строго off-main.
    @MainActor
    func refreshMigrationStatus() {
        // Во время активной миграции счётчик обновляет поллер — не перетираем.
        if (objc_getAssociatedObject(self, &SecurityAssocKeys.migratePolling) as? Bool) == true {
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? self.ipcClient.call(method: "get_history_encryption_status", params: [:]),
                  let result = resp["result"] as? [String: Any] else { return }
            let enabled = result["enabled"] as? Bool ?? false
            let plaintext = result["plaintext"] as? Int ?? 0
            let migrating = result["migrating"] as? Bool ?? false
            DispatchQueue.main.async {
                if migrating {
                    // Миграция уже идёт (например, после рестарта UI) — подхватываем поллингом.
                    self.beginMigrationPolling()
                    return
                }
                self.applyMigrationVisibility(enabled: enabled, plaintext: plaintext)
            }
        }
    }

    /// Обновляет видимость migrate-блока и текст счётчика на main.
    @MainActor
    private func applyMigrationVisibility(enabled: Bool, plaintext: Int) {
        let shouldShow = enabled && plaintext > 0
        if let row = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateRow) as? NSStackView {
            row.isHidden = !shouldShow
        }
        if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
            button.isEnabled = shouldShow
            button.isHidden = !shouldShow
        }
        if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
            progress.isHidden = true
            progress.doubleValue = 0
        }
        if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
            if shouldShow {
                status.stringValue = "\(plaintext) незашифрованных"
                status.textColor = KrabEarTheme.Colors.textSecondary
            } else {
                status.stringValue = ""
            }
        }
    }

    // MARK: - Migration trigger + polling

    @objc func onMigrateHistoryEncryptionClicked() {
        // Снимаем текущий счётчик plaintext для текста подтверждения.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var plaintext = 0
            if let resp = try? self.ipcClient.call(method: "get_history_encryption_status", params: [:]),
               let result = resp["result"] as? [String: Any] {
                plaintext = result["plaintext"] as? Int ?? 0
            }
            DispatchQueue.main.async {
                guard plaintext > 0 else {
                    self.refreshMigrationStatus()
                    return
                }
                let alert = NSAlert()
                alert.messageText = "Зашифровать существующие записи"
                alert.informativeText =
                    "Зашифровать \(plaintext) существующих записей? Будет создана резервная копия (.bak)."
                alert.alertStyle = .informational
                alert.addButton(withTitle: "Зашифровать")
                alert.addButton(withTitle: "Отмена")
                presentAlertSheet(alert, for: self.window) { response in
                    guard response == .alertFirstButtonReturn else { return }
                    self.startHistoryMigration()
                }
            }
        }
    }

    /// Отправляет migrate_history_encryption (off-main), затем запускает поллинг.
    @MainActor
    private func startHistoryMigration() {
        // Готовим UI: блокируем кнопку, показываем прогресс-бар на 0%.
        if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
            button.isEnabled = false
        }
        if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
            progress.isHidden = false
            progress.doubleValue = 0
        }
        if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
            status.stringValue = "Шифрование…"
            status.textColor = KrabEarTheme.Colors.textSecondary
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? self.ipcClient.call(method: "migrate_history_encryption", params: [:]),
                  let result = resp["result"] as? [String: Any] else {
                DispatchQueue.main.async { self.handleMigrationUnavailable() }
                return
            }
            let ok = result["ok"] as? Bool ?? false
            let migrateStatus = result["status"] as? String ?? ""
            DispatchQueue.main.async {
                if !ok || migrateStatus == "encryption_unavailable" {
                    self.handleMigrationUnavailable()
                    return
                }
                // status == "started" или "already_running" — поллим прогресс.
                self.beginMigrationPolling()
            }
        }
    }

    /// Состояние «шифрование недоступно» — нет ключа в Keychain.
    @MainActor
    private func handleMigrationUnavailable() {
        objc_setAssociatedObject(self, &SecurityAssocKeys.migratePolling, false, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
            progress.isHidden = true
        }
        if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
            status.stringValue = "Шифрование недоступно — нет ключа в Keychain"
            status.textColor = KrabEarTheme.Colors.warning
        }
        if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
            button.isEnabled = true
        }
    }

    /// Запускает цикл опроса get_history_encryption_status каждые ~0.5с.
    @MainActor
    private func beginMigrationPolling() {
        if (objc_getAssociatedObject(self, &SecurityAssocKeys.migratePolling) as? Bool) == true {
            return   // уже поллим
        }
        objc_setAssociatedObject(self, &SecurityAssocKeys.migratePolling, true, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
            button.isEnabled = false
        }
        if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
            progress.isHidden = false
        }
        pollMigrationOnce()
    }

    /// Один цикл опроса: IPC off-main → обновление прогресса на main →
    /// планирование следующего опроса или завершение.
    @MainActor
    private func pollMigrationOnce() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? self.ipcClient.call(method: "get_history_encryption_status", params: [:]),
                  let result = resp["result"] as? [String: Any] else {
                // Транзиентная ошибка IPC — повторяем через 0.5с.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    self?.pollMigrationOnce()
                }
                return
            }
            let total = result["total"] as? Int ?? 0
            let encrypted = result["encrypted"] as? Int ?? 0
            let plaintext = result["plaintext"] as? Int ?? 0
            let pct = result["pct"] as? Int ?? 0
            let migrating = result["migrating"] as? Bool ?? false
            DispatchQueue.main.async {
                if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                    progress.doubleValue = Double(pct)
                }
                if migrating || plaintext > 0 {
                    // Ещё идёт — показываем encrypted/total и опрашиваем снова.
                    if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                        status.stringValue = "\(encrypted)/\(total)"
                        status.textColor = KrabEarTheme.Colors.textSecondary
                    }
                    if migrating {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                            self?.pollMigrationOnce()
                        }
                        return
                    }
                }
                // migrating == false: миграция завершена.
                self.finishMigrationPolling(total: total, encrypted: encrypted, plaintext: plaintext)
            }
        }
    }

    /// Завершение миграции: «Готово ✓» при plaintext==0, иначе показываем счётчик.
    @MainActor
    private func finishMigrationPolling(total: Int, encrypted: Int, plaintext: Int) {
        objc_setAssociatedObject(self, &SecurityAssocKeys.migratePolling, false, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        if plaintext == 0 {
            // ✓ — established glyph (используется в Diagnostics/SemanticSearch и др.).
            if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                progress.doubleValue = 100
                progress.isHidden = true
            }
            if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                status.stringValue = "Готово ✓"
                status.textColor = KrabEarTheme.Colors.success
            }
            if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
                button.isEnabled = false
                button.isHidden = true
            }
            if let row = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateRow) as? NSStackView {
                // Блок остаётся видимым с «Готово ✓» до следующей перезагрузки секции.
                row.isHidden = false
            }
        } else {
            // Частично/прервано — возвращаем к обычному виду со счётчиком.
            applyMigrationVisibility(enabled: true, plaintext: plaintext)
        }
    }

    // MARK: - Toggle actions (Gemini)

    @objc func onEncryptionToggleChanged() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.encryptionToggle
        ) as? NSButton else { return }
        let desired = toggle.state == .on
        applyEncryptionChange(desired: desired, toggleRef: toggle)
    }

    // MARK: - Toggle actions (Claude Design)

    @objc func onEncryptionToggleChangedCD() {
        guard !isSyncingSettings else { return }
        guard let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.cdEncryptionToggle
        ) as? NSButton else { return }
        let desired = toggle.state == .on
        applyEncryptionChange(desired: desired, toggleRef: toggle)
    }

    // MARK: - Shared IPC call

    /// Отправляет set_history_encryption IPC, откатывает тоггл при ошибке Keychain.
    private func applyEncryptionChange(desired: Bool, toggleRef: NSButton) {
        // Оптимистично — оставляем тоггл как есть; отменим при ошибке.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            guard let resp = try? self.ipcClient.call(
                method: "set_history_encryption",
                params: ["enabled": desired]
            ) else { return }
            DispatchQueue.main.async {
                guard let result = resp["result"] as? [String: Any] else { return }
                let ok = result["ok"] as? Bool ?? false
                if ok {
                    // Успех — ничего делать не нужно, тоггл уже в правильном состоянии.
                    // Sync обоих вариантов на случай, если оба видимы.
                    self.syncSecuritySettings(enabled: desired, available: true)
                    // Пересчитываем видимость migrate-блока: включили шифрование при
                    // наличии plaintext → показать кнопку; выключили → скрыть.
                    self.refreshMigrationStatus()
                } else {
                    // Ошибка Keychain — откатываем тоггл.
                    toggleRef.state = desired ? .off : .on
                    // Синхронизируем второй вариант
                    let current = result["enabled"] as? Bool ?? !desired
                    self.syncSecuritySettings(enabled: current, available: false)
                    // Показываем сообщение без runModal
                    let alert = NSAlert()
                    alert.messageText = "Шифрование недоступно"
                    alert.informativeText = "Шифрование недоступно: Keychain не найден.\nФункция поддерживается только на macOS."
                    alert.alertStyle = .warning
                    alert.addButton(withTitle: "OK")
                    presentAlertSheet(alert, for: self.window) { _ in }
                }
            }
        }
    }

    // MARK: - Sync from backend settings

    /// Обновляет контролы из свежих настроек (вызывается из syncSettingsControls
    /// или после get_encryption_status/set_history_encryption).
    @MainActor
    func syncSecuritySettings(enabled: Bool, available: Bool) {
        let toggleState: NSControl.StateValue = enabled ? .on : .off
        let iconSymbol = enabled ? "lock.fill" : "lock.open"
        let statusText = !available ? "Недоступно (Keychain)" : (enabled ? "Зашифровано" : "Выключено")
        let statusColor = !available ? KrabEarTheme.Colors.warning : (enabled ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary)

        // Gemini variant
        if let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.encryptionToggle
        ) as? NSButton {
            toggle.state = toggleState
            toggle.isEnabled = available
            toggle.alphaValue = available ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        }
        if let iconView = objc_getAssociatedObject(self, &SecurityAssocKeys.statusIcon) as? NSImageView {
            if let icon = NSImage(systemSymbolName: iconSymbol, accessibilityDescription: nil) {
                iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
            }
            iconView.contentTintColor = statusColor
        }
        if let badge = objc_getAssociatedObject(self, &SecurityAssocKeys.statusBadge) as? NSTextField {
            badge.stringValue = statusText
            badge.textColor = statusColor
        }

        // Claude Design variant
        if let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.cdEncryptionToggle
        ) as? NSButton {
            toggle.state = toggleState
            toggle.isEnabled = available
            toggle.alphaValue = available ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        }
        if let iconView = objc_getAssociatedObject(self, &SecurityAssocKeys.cdStatusIcon) as? NSImageView {
            if let icon = NSImage(systemSymbolName: iconSymbol, accessibilityDescription: nil) {
                iconView.image = icon.withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
            }
            iconView.contentTintColor = statusColor
        }
        if let badge = objc_getAssociatedObject(self, &SecurityAssocKeys.cdStatusBadge) as? NSTextField {
            badge.stringValue = statusText
            badge.textColor = statusColor
        }
    }
}
