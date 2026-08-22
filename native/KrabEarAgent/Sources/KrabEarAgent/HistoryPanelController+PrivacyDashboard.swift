/*
 Приватность и данные: секция настроек со сводкой приватности/безопасности.
 Показывает режим приватности, шифрование истории, объём хранилища, политику
 авто-очистки/purge и счётчик событий аудита приватности. Два живых контрола
 (2026-08-22, возврат privacy-UI после волны dead-swift-methods-b):
   - тумблер «Режим приватности» (единственная точка включения privacy mode из UI —
     секция Phase D.5 с прежним тумблером с рождения не вставлялась в таб);
   - кнопка «Журнал аудита» → PrivacyAuditViewerWindowController (просмотр без
     очистки: clear_privacy_audit_log удалён из IPC-диспетча, W957 SECURITY).
 Остальное — только чтение, никаких транскриптов/словарей/спикеров (их нет в payload).

 IPC-контракт (только счётчики/флаги/размеры — никакого текста транскриптов):
   - get_privacy_dashboard {}
       → result {
           ok Bool,
           privacy_mode Bool,
           encryption_enabled Bool,
           storage {item_count Int, history_bytes Int, history_file_size_mb Double,
                    transcripts_count Int, transcripts_size_mb Double,
                    total_bytes Int, total_data_mb Double},
           retention {auto_cleanup_enabled Bool, auto_cleanup_after_days Int,
                      auto_purge_enabled Bool, auto_purge_retention_days Int},
           audit {total_events Int, last_event_ts String|Double|null,
                  by_type {action→count}},
           purge_available Bool
         }

 Читаются ТОЛЬКО перечисленные числовые/булевы поля. Содержимое истории
 (текст/словарь/спикеры) НЕ запрашивается и НЕ отображается.

 Архитектура (зеркало HistoryPanelController+Calibration.swift):
   - buildPrivacyDashboardSection() — Gemini-вариант (settingsBar, ThemeCardView).
   - cdBuildPrivacyDashboardSection() — Claude Design (settingsBarCD, CDSettingsCardView).
   - fetchAndRebuildPrivacyDashboardCard(isClaudeDesign:) — грузит IPC off-main,
     перестраивает карточку.
   - onRefreshPrivacyDashboard(_:) — перезагружает обе карточки.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Глифы: только ASCII + установленные SF Symbols (lock.shield/lock.fill/lock.open/
 externaldrive/clock.arrow.circlepath/checkmark.seal.fill) + установленные
 пунктуационные символы (· …), уже использующиеся в проекте.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum PrivacyDashboardAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var auditViewer: UInt8 = 0
}

// MARK: - Модель приватности (internal, single-source)

struct PrivacyDashboardData {
    let privacyMode: Bool
    let encryptionEnabled: Bool
    // storage
    let itemCount: Int
    let transcriptsCount: Int
    let totalDataMb: Double
    // retention
    let autoCleanupEnabled: Bool
    let autoCleanupAfterDays: Int
    let autoPurgeEnabled: Bool
    let autoPurgeRetentionDays: Int
    // audit
    let auditTotalEvents: Int
    let auditLastEventTs: String?
    let purgeAvailable: Bool
}

// MARK: - HistoryPanelController+PrivacyDashboard

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Приватность и данные» (Gemini-дизайн, settingsBar).
    /// Внутренняя карточка наполняется асинхронно при первом показе (off-main).
    @MainActor
    func buildPrivacyDashboardSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "privacy_dashboard",
            title: "Приватность и данные",
            isExpanded: false,
            iconSymbol: "lock.shield"
        )

        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &PrivacyDashboardAssocKeys.card,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        // Загрузка данных (off-main, AGENT-3).
        fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Приватность и данные» (Claude Design, settingsBarCD).
    @MainActor
    func cdBuildPrivacyDashboardSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_privacy_dashboard",
            title: "Приватность и данные",
            isExpanded: false
        )

        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &PrivacyDashboardAssocKeys.cdCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает get_privacy_dashboard строго off-main (AGENT-3),
    /// обновляет карточку на main.
    func fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            var data: PrivacyDashboardData?
            do {
                let resp = try ipc.call(method: "get_privacy_dashboard", params: [:])
                let result = resp["result"] as? [String: Any] ?? [:]

                // Без ok=true карточка бессмысленна — считаем данные отсутствующими.
                if (result["ok"] as? Bool) == true {
                    let storage = result["storage"] as? [String: Any] ?? [:]
                    let retention = result["retention"] as? [String: Any] ?? [:]
                    let audit = result["audit"] as? [String: Any] ?? [:]

                    data = PrivacyDashboardData(
                        privacyMode: (result["privacy_mode"] as? Bool) ?? false,
                        encryptionEnabled: (result["encryption_enabled"] as? Bool) ?? false,
                        itemCount: (storage["item_count"] as? Int) ?? 0,
                        transcriptsCount: (storage["transcripts_count"] as? Int) ?? 0,
                        totalDataMb: HistoryPanelController.pdDouble(storage["total_data_mb"]),
                        autoCleanupEnabled: (retention["auto_cleanup_enabled"] as? Bool) ?? false,
                        autoCleanupAfterDays: (retention["auto_cleanup_after_days"] as? Int) ?? 0,
                        autoPurgeEnabled: (retention["auto_purge_enabled"] as? Bool) ?? false,
                        autoPurgeRetentionDays: (retention["auto_purge_retention_days"] as? Int) ?? 0,
                        auditTotalEvents: (audit["total_events"] as? Int) ?? 0,
                        auditLastEventTs: HistoryPanelController.pdTimestamp(audit["last_event_ts"]),
                        purgeAvailable: (result["purge_available"] as? Bool) ?? false
                    )
                }
            } catch {
                data = nil
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDPrivacyDashboardCard(data: data)
                } else {
                    self.rebuildGeminiPrivacyDashboardCard(data: data)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiPrivacyDashboardCard(data: PrivacyDashboardData?) {
        guard let card = objc_getAssociatedObject(
            self, &PrivacyDashboardAssocKeys.card
        ) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard let data else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        // Режим приватности — живой тумблер (единственная точка включения из UI).
        card.contentStackView.addArrangedSubview(
            makeSettingRow(
                label: "Режим приватности",
                control: pdPrivacyModeToggle(on: data.privacyMode),
                badge: pdToggleBadge(
                    on: data.privacyMode,
                    onSymbol: "lock.fill",
                    offSymbol: "lock.open"
                )
            )
        )
        card.contentStackView.addArrangedSubview(pdSeparator())

        // Шифрование истории.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(
                label: "Шифрование истории",
                control: pdValueLabel(pdOnOff(data.encryptionEnabled)),
                badge: pdToggleBadge(
                    on: data.encryptionEnabled,
                    onSymbol: "checkmark.seal.fill",
                    offSymbol: nil
                )
            )
        )
        card.contentStackView.addArrangedSubview(pdSeparator())

        // Хранилище.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Объём данных", control: pdValueLabel(pdMb(data.totalDataMb)))
        )
        card.contentStackView.addArrangedSubview(pdSeparator())
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Записей в истории", control: pdValueLabel("\(data.itemCount)"))
        )
        card.contentStackView.addArrangedSubview(pdSeparator())
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Файлов транскриптов", control: pdValueLabel("\(data.transcriptsCount)"))
        )
        card.contentStackView.addArrangedSubview(pdSeparator())

        // Авто-очистка.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(
                label: "Авто-очистка",
                control: pdValueLabel(pdRetentionValue(
                    enabled: data.autoCleanupEnabled, days: data.autoCleanupAfterDays))
            )
        )
        card.contentStackView.addArrangedSubview(pdSeparator())

        // Авто-purge.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(
                label: "Авто-удаление (purge)",
                control: pdValueLabel(pdRetentionValue(
                    enabled: data.autoPurgeEnabled, days: data.autoPurgeRetentionDays))
            )
        )
        card.contentStackView.addArrangedSubview(pdSeparator())

        // Аудит приватности.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Событий аудита", control: pdValueLabel("\(data.auditTotalEvents)"))
        )
        if let ts = data.auditLastEventTs, !ts.isEmpty {
            card.contentStackView.addArrangedSubview(pdSeparator())
            card.contentStackView.addArrangedSubview(
                makeSettingRow(label: "Последнее событие", control: pdValueLabel(ts))
            )
        }

        // Кнопка обновления.
        card.contentStackView.addArrangedSubview(pdButtonRow())
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDPrivacyDashboardCard(data: PrivacyDashboardData?) {
        guard let card = objc_getAssociatedObject(
            self, &PrivacyDashboardAssocKeys.cdCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard let data else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Режим приватности", control: pdPrivacyModeToggle(on: data.privacyMode))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Шифрование истории", control: pdValueLabel(pdOnOff(data.encryptionEnabled)))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Объём данных", control: pdValueLabel(pdMb(data.totalDataMb)))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Записей в истории", control: pdValueLabel("\(data.itemCount)"))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Файлов транскриптов", control: pdValueLabel("\(data.transcriptsCount)"))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Авто-очистка", control: pdValueLabel(pdRetentionValue(
                enabled: data.autoCleanupEnabled, days: data.autoCleanupAfterDays)))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Авто-удаление (purge)", control: pdValueLabel(pdRetentionValue(
                enabled: data.autoPurgeEnabled, days: data.autoPurgeRetentionDays)))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Событий аудита", control: pdValueLabel("\(data.auditTotalEvents)"))
        )
        if let ts = data.auditLastEventTs, !ts.isEmpty {
            card.contentStackView.addArrangedSubview(cdMakeSeparator())
            card.contentStackView.addArrangedSubview(
                cdMakeRow(label: "Последнее событие", control: pdValueLabel(ts))
            )
        }

        card.contentStackView.addArrangedSubview(pdButtonRow())
    }

    // MARK: - Кнопки секции

    @MainActor
    private func pdButtonRow() -> NSView {
        let refreshButton = ThemeSecondaryButton(
            title: "Обновить",
            target: self,
            action: #selector(onRefreshPrivacyDashboard(_:))
        )
        refreshButton.setAccessibilityLabel("Обновить сводку приватности и данных")

        let auditButton = ThemeSecondaryButton(
            title: "Журнал аудита",
            target: self,
            action: #selector(onShowPrivacyAuditLog(_:))
        )
        auditButton.setAccessibilityLabel(
            "Открыть журнал событий приватности: заблокированные Sentry-отчёты, "
                + "принудительный offline-перевод, включения и выключения режима."
        )

        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.alignment = .centerY
        stack.distribution = .fill
        stack.addArrangedSubview(refreshButton)
        stack.addArrangedSubview(auditButton)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)
        stack.addArrangedSubview(spacer)
        return stack
    }

    // MARK: - Живой тумблер privacy mode

    /// Свежий switch на каждый rebuild карточки; состояние берётся из payload.
    @MainActor
    private func pdPrivacyModeToggle(on: Bool) -> NSButton {
        let toggle = NSButton(checkboxWithTitle: "", target: self,
                              action: #selector(onPrivacyModeToggled(_:)))
        toggle.setButtonType(.switch)
        toggle.state = on ? .on : .off
        toggle.setAccessibilityLabel(
            "Режим приватности: отключает Sentry telemetry и принудительно "
                + "переводит перевод в offline-режим. LM Studio (127.0.0.1) остаётся разрешённым."
        )
        return toggle
    }

    // MARK: - Handlers

    /// Перечитывает сводку приватности для обеих карточек.
    @objc func onRefreshPrivacyDashboard(_ sender: NSButton) {
        fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: false)
        fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: true)
    }

    /// Переключает privacy mode из секции «Приватность и данные».
    @objc func onPrivacyModeToggled(_ sender: NSButton) {
        let enabled = sender.state == .on
        applySettingsPatch(["privacy_mode_enabled": enabled])
        (NSApp.delegate as? AgentAppDelegate)?.setPrivacyMode(enabled)
        // set_settings уходит асинхронно (optimistic local + Task в persistSettingsPayload) —
        // немедленный fetch прочитал бы с backend ещё старое значение.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
            guard let self else { return }
            self.fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: false)
            self.fetchAndRebuildPrivacyDashboardCard(isClaudeDesign: true)
        }
    }

    /// Открывает Privacy Audit viewer (просмотр журнала, без очистки — W957).
    @objc func onShowPrivacyAuditLog(_ sender: NSButton) {
        let viewer = PrivacyAuditViewerWindowController(ipcClient: ipcClient)
        // Держим сильную ссылку, пока окно открыто.
        objc_setAssociatedObject(
            self,
            &PrivacyDashboardAssocKeys.auditViewer,
            viewer,
            .OBJC_ASSOCIATION_RETAIN
        )
        viewer.showAndLoad()
    }

    // MARK: - Вспомогательные элементы (только для этого extension)

    /// Значение справа в строке: tabular, основной цвет.
    @MainActor
    private func pdValueLabel(_ text: String) -> NSView {
        let label = NSTextField(labelWithString: text)
        label.font = KrabEarTheme.Typography.captionMedium.tabular()
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    /// Вкл/Выкл подпись.
    private func pdOnOff(_ on: Bool) -> String {
        on ? "Вкл" : "Выкл"
    }

    /// Объём в МБ с одним знаком.
    private func pdMb(_ mb: Double) -> String {
        String(format: "%.1f МБ", mb)
    }

    /// Подпись политики хранения: "Вкл · N дн." или "Выкл".
    private func pdRetentionValue(enabled: Bool, days: Int) -> String {
        guard enabled else { return "Выкл" }
        if days > 0 {
            return "Вкл · \(days) дн."
        }
        return "Вкл"
    }

    /// Цветной бейдж для булева флага (зелёный когда защита включена).
    @MainActor
    private func pdToggleBadge(on: Bool, onSymbol: String?, offSymbol: String?) -> NSView {
        if on {
            return makeBadge(
                text: "Вкл",
                color: KrabEarTheme.Colors.success,
                tooltip: nil,
                symbol: onSymbol
            )
        }
        return makeBadge(
            text: "Выкл",
            color: KrabEarTheme.Colors.textDisabled,
            tooltip: nil,
            symbol: offSymbol
        )
    }

    /// NSBox separator (аналог приватного makeSeparator()).
    @MainActor
    private func pdSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    // MARK: - Парсинг payload (static, без MainActor)

    /// Извлекает Double из Int/Double/NSNumber значения payload.
    static func pdDouble(_ value: Any?) -> Double {
        if let d = value as? Double { return d }
        if let i = value as? Int { return Double(i) }
        if let n = value as? NSNumber { return n.doubleValue }
        return 0
    }

    /// Нормализует last_event_ts: может прийти строкой, числом (epoch) или null.
    static func pdTimestamp(_ value: Any?) -> String? {
        if let s = value as? String {
            return s.isEmpty ? nil : s
        }
        if let d = value as? Double {
            let date = Date(timeIntervalSince1970: d)
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm"
            return fmt.string(from: date)
        }
        if let i = value as? Int {
            let date = Date(timeIntervalSince1970: Double(i))
            let fmt = DateFormatter()
            fmt.dateFormat = "yyyy-MM-dd HH:mm"
            return fmt.string(from: date)
        }
        return nil
    }
}
