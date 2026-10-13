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
   - HistoryMigrationObservationCoordinator — confirm sheet → migrate_history_encryption →
     poll get_history_encryption_status каждые ~0.5с; lifecycle поколения окна.

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
    nonisolated(unsafe) static var migrationCoordinator: UInt8 = 0
    // SSE-наблюдатель терминального статуса миграции (итог фонового потока приходит только событием)
    nonisolated(unsafe) static var migrationWatcher: UInt8 = 0
    nonisolated(unsafe) static var migrationWatchSession: UInt8 = 0
    nonisolated(unsafe) static var migrationWatchDelegate: UInt8 = 0
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
        migrationObservationCoordinator.refresh()
    }

    /// Один coordinator на переиспользуемую панель; все completions доставляются на main.
    @MainActor
    var migrationObservationCoordinator: HistoryMigrationObservationCoordinator {
        if let existing = objc_getAssociatedObject(self, &SecurityAssocKeys.migrationCoordinator)
            as? HistoryMigrationObservationCoordinator { return existing }
        let coordinator = HistoryMigrationObservationCoordinator(
            request: { [weak self] method, completion in
                guard let self else { return }
                DispatchQueue.global(qos: .userInitiated).async {
                    let response = try? self.ipcClient.call(method: method, params: [:])
                    DispatchQueue.main.async { completion(response) }
                }
            },
            schedule: { delay, action in
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { action() }
            },
            startWatcher: { [weak self] in self?.startMigrationEventWatch() },
            stopWatcher: { [weak self] in self?.stopMigrationEventWatch() },
            hasWatcher: { [weak self] in
                guard let self else { return false }
                return objc_getAssociatedObject(self, &SecurityAssocKeys.migrationWatcher) != nil
            },
            terminalStatus: { [weak self] in
                guard let self else { return nil }
                return (objc_getAssociatedObject(self, &SecurityAssocKeys.migrationWatcher)
                    as? HistoryMigrationTerminalWatcher)?.terminalStatus
            },
            render: { [weak self] event in self?.renderMigrationEvent(event) },
            presentConfirmation: { [weak self] plaintext, completion in
                guard let self else { return }
                let alert = NSAlert()
                alert.messageText = "Зашифровать существующие записи"
                alert.informativeText =
                    "Зашифровать \(plaintext) существующих записей? Будет создана резервная копия (.bak)."
                alert.alertStyle = .informational
                alert.addButton(withTitle: "Зашифровать")
                alert.addButton(withTitle: "Отмена")
                presentAlertSheet(alert, for: self.window) { response in
                    completion(response == .alertFirstButtonReturn)
                }
            }
        )
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationCoordinator, coordinator,
                                 .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return coordinator
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
        migrationObservationCoordinator.confirmAndStartMigration()
    }

    /// Только AppKit-эффекты: lifecycle, parsing и scheduling принадлежат coordinator.
    @MainActor
    private func renderMigrationEvent(_ event: HistoryMigrationObservationCoordinator.RenderEvent) {
        switch event {
        case .visibility(let enabled, let plaintext):
            applyMigrationVisibility(enabled: enabled, plaintext: plaintext)
        case .starting:
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
        case .polling:
            if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
                button.isEnabled = false
            }
            if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                progress.isHidden = false
            }
        case .progress(let pct, let encrypted, let total, let showCount):
            if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                progress.doubleValue = Double(pct)
            }
            if showCount, let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                status.stringValue = "\(encrypted)/\(total)"
                status.textColor = KrabEarTheme.Colors.textSecondary
            }
        case .finishing:
            if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                status.stringValue = "Завершение…"
                status.textColor = KrabEarTheme.Colors.textSecondary
            }
        case .unavailable:
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
        case .outcome(let outcome):
            presentMigrationOutcome(outcome)
        }
    }

    @MainActor
    private func presentMigrationOutcome(_ outcome: HistoryMigrationOutcome) {
        switch outcome {
        case .done:
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
        case .backupCleanupFailed:
            // Живой файл зашифрован, но plaintext .bak мог остаться на диске.
            if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                progress.isHidden = true
            }
            if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                status.stringValue = "Записи зашифрованы, но резервная копия (.bak) не удалена — "
                    + "она может содержать открытый текст. «Удалить все данные» удалит и её."
                status.textColor = KrabEarTheme.Colors.warning
            }
            if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
                button.isEnabled = false
                button.isHidden = true
            }
            if let row = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateRow) as? NSStackView {
                row.isHidden = false
            }
        case .failed(let failure):
            if let progress = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateProgress) as? NSProgressIndicator {
                progress.isHidden = true
            }
            if let status = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateStatus) as? NSTextField {
                status.stringValue = "Миграция завершилась с ошибкой: \(failure). Часть записей может остаться незашифрованной."
                status.textColor = KrabEarTheme.Colors.warning
            }
            if let row = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateRow) as? NSStackView {
                row.isHidden = false
            }
            // Кнопка остаётся доступной: миграция идемпотентна, можно повторить.
            if let button = objc_getAssociatedObject(self, &SecurityAssocKeys.migrateButton) as? NSButton {
                button.isEnabled = true
                button.isHidden = false
            }
        case .partial(let remaining):
            // Частично/прервано — возвращаем к обычному виду со счётчиком.
            applyMigrationVisibility(enabled: true, plaintext: remaining)
        }
    }

    // MARK: - Migration terminal-status watcher (SSE)

    /// Подписка на `history_encryption.migrate.progress` (REST /v1/events): backend отдаёт
    /// итог фонового потока (в т.ч. backup_cleanup_failed) только этим событием.
    /// Сбой подключения безопасен: вердикт тогда опирается на счётчики, как раньше.
    @MainActor
    private func startMigrationEventWatch() {
        stopMigrationEventWatch()
        guard let url = URL(string: "http://127.0.0.1:5005/v1/events?filter=history_encryption.migrate.progress")
        else { return }
        let watcher = HistoryMigrationTerminalWatcher()
        let delegate = SSESessionDelegate(onLine: { [weak watcher] line in watcher?.ingest(line: line) })
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        var request = URLRequest(url: url, timeoutInterval: 600)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatcher, watcher, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatchDelegate, delegate, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatchSession, session, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        session.dataTask(with: request).resume()
    }

    /// Вызывается при закрытии окна: стрим SSE не должен жить без UI.
    @MainActor
    func cancelMigrationEventWatch() {
        migrationObservationCoordinator.close()
    }

    @MainActor
    private func stopMigrationEventWatch() {
        (objc_getAssociatedObject(self, &SecurityAssocKeys.migrationWatchSession) as? URLSession)?.invalidateAndCancel()
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatchSession, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatchDelegate, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        objc_setAssociatedObject(self, &SecurityAssocKeys.migrationWatcher, nil, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
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

    /// Отправляет set_history_encryption IPC. При любом отказе (бросок IPC, ok=false,
    /// кривой ответ) возвращает тоггл к РЕАЛЬНОМУ состоянию (свежий get_encryption_status)
    /// и объясняет это листом — тоггл не остаётся «врущим» в выбранной позиции.
    private func applyEncryptionChange(desired: Bool, toggleRef: NSButton) {
        // Оптимистично — оставляем тоггл как есть; отменим при ошибке.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let resp = try? self.ipcClient.call(
                method: "set_history_encryption",
                params: ["enabled": desired]
            )
            let outcome = EncryptionChangeOutcome.resolve(response: resp)
            if outcome == .applied {
                DispatchQueue.main.async {
                    // Sync обоих вариантов на случай, если оба видимы.
                    self.syncSecuritySettings(enabled: desired, available: true)
                    // Пересчитываем видимость migrate-блока: включили шифрование при
                    // наличии plaintext → показать кнопку; выключили → скрыть.
                    self.refreshMigrationStatus()
                }
                return
            }
            // Отказ: перечитываем реальное состояние (off-main), затем откатываем на main.
            var reread: Bool?
            var available: Bool?
            if let statusResp = try? self.ipcClient.call(method: "get_encryption_status", params: [:]),
               let status = statusResp["result"] as? [String: Any], (status["ok"] as? Bool) == true {
                reread = status["enabled"] as? Bool
                available = status["available"] as? Bool
            }
            DispatchQueue.main.async {
                let known = outcome.knownEnabled(desired: desired, reread: reread)
                let actual = known ?? outcome.actualEnabled(desired: desired, reread: reread)
                var keychainMissing = available == false
                if case .keychainUnavailable = outcome { keychainMissing = true }
                toggleRef.state = actual ? .on : .off
                self.syncSecuritySettings(enabled: actual, available: !keychainMissing)
                let alert = NSAlert()
                if keychainMissing {
                    alert.messageText = "Шифрование недоступно"
                    alert.informativeText = "Шифрование недоступно: Keychain не найден.\nФункция поддерживается только на macOS."
                } else if known == nil {
                    alert.messageText = "Не удалось изменить шифрование"
                    alert.informativeText = "Не удалось подтвердить состояние шифрования. "
                        + "Закройте и откройте раздел заново, чтобы увидеть актуальное состояние."
                } else {
                    alert.messageText = "Не удалось изменить шифрование"
                    alert.informativeText = "Настройка не применена. Сейчас шифрование истории "
                        + (actual ? "включено." : "выключено.")
                }
                alert.alertStyle = .warning
                alert.addButton(withTitle: "OK")
                presentAlertSheet(alert, for: self.window) { _ in }
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
