/*
 Безопасность — секция настроек шифрования истории на диске.

 Использует два IPC-метода (Chunk 2, backend/service.py):
   - get_encryption_status {}
       → {ok:true, enabled:bool, available:bool}
   - set_history_encryption {enabled:Bool}
       → {ok:true, enabled:bool, available:bool}
       | {ok:false, error:"keychain_unavailable", enabled:bool}

 Архитектура:
   - buildSecuritySettingsSection()   — вариант для Gemini-дизайна (settingsBar).
   - cdBuildSecuritySettingsSection() — вариант для Claude Design (settingsBarCD).
   - syncSecuritySettings(enabled:available:) — обновляет контролы при смене
     настроек (вызывается из syncSettingsControls).

 Правила AGENT-3 (AppHang-класс):
   IPC строго в DispatchQueue.global, мутации UI — строго в DispatchQueue.main.
   НИКОГДА runModal() — используется presentAlertSheet для ошибок Keychain.

 Ключ шифрования в Keychain НЕ удаляется при выключении тоггла.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum SecurityAssocKeys {
    nonisolated(unsafe) static var encryptionToggle: UInt8 = 0
    nonisolated(unsafe) static var cdEncryptionToggle: UInt8 = 0
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

        let toggleRow = makeSettingRow(
            label: "Шифровать историю на диске",
            description: "Новые записи сохраняются в зашифрованном виде (AES-256-GCM). Старые записи остаются как есть.",
            control: toggle
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
        section.contentStackView.addArrangedSubview(card)

        // Загружаем реальный статус из backend при создании секции
        loadEncryptionStatus()

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

        // Тоггл
        let toggle = NSButton(
            checkboxWithTitle: "Шифровать историю",
            target: self,
            action: #selector(onEncryptionToggleChangedCD)
        )
        toggle.state = .off
        objc_setAssociatedObject(
            self, &SecurityAssocKeys.cdEncryptionToggle, toggle, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let toggleRow = cdMakeRow(label: "Шифровать историю на диске", control: toggle)

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

        // Gemini variant
        if let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.encryptionToggle
        ) as? NSButton {
            toggle.state = toggleState
            toggle.isEnabled = available
            toggle.alphaValue = available ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        }

        // Claude Design variant
        if let toggle = objc_getAssociatedObject(
            self, &SecurityAssocKeys.cdEncryptionToggle
        ) as? NSButton {
            toggle.state = toggleState
            toggle.isEnabled = available
            toggle.alphaValue = available ? 1.0 : KrabEarTheme.Interaction.disabledOpacity
        }
    }
}
