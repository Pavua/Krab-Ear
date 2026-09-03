/*
 HistoryPanelController+AllSettings — секция «Все настройки».

 Требование владельца: всё, что настраивается, должно настраиваться из панели.
 Замер 02.09.2026 показал, что из 258 живых настроек панель редактирует 86 —
 остальные 162 существуют только в settings.json. Строить 162 контрола руками
 бессмысленно: настройки прибавляются быстрее, чем секции, и следующий
 `overlay_follow_cursor` появился бы тем же способом.

 Поэтому таблица строится ИЗ ОТВЕТА `get_settings`: новая настройка бэкенда
 появляется здесь сама, без правки Swift. Тип значения выбирает редактор —
 переключатель для булева, поле для числа и строки.

 🔴 Секреты (`*_key`, `*_token`, `*_password`, `*_dsn`, `*_secret`) показываются
 как «задано / не задано» и НИКОГДА не печатаются: `get_settings` отдаёт их
 значением `REDACTED`, и запись этой строки обратно затёрла бы живой ключ.
 Защита есть и на стороне бэкенда (он выбрасывает `REDACTED` из входящих
 params), но показывать звёздочки честнее, чем делать вид, что значение перед
 глазами.

 Секция сознательно свёрнута по умолчанию и наполняется при первом раскрытии:
 258 строк — заметная работа для AppKit, платить за неё при каждом открытии
 панели незачем.
 */

import AppKit

private enum AllSettingsAssocKeys {
    nonisolated(unsafe) static var searchField: UInt8 = 0
    nonisolated(unsafe) static var rowsStack: UInt8 = 0
    nonisolated(unsafe) static var rowIndex: UInt8 = 0
    nonisolated(unsafe) static var statusLabel: UInt8 = 0
    nonisolated(unsafe) static var loaded: UInt8 = 0
}

/// Одна строка таблицы: ключ, его view и текст для поиска.
private final class AllSettingsRow {
    let key: String
    let view: NSView
    let haystack: String
    init(key: String, view: NSView, haystack: String) {
        self.key = key
        self.view = view
        self.haystack = haystack
    }
}

extension HistoryPanelController {

    private var allSettingsRowsStack: NSStackView? {
        objc_getAssociatedObject(self, &AllSettingsAssocKeys.rowsStack) as? NSStackView
    }
    private var allSettingsStatusLabel: NSTextField? {
        objc_getAssociatedObject(self, &AllSettingsAssocKeys.statusLabel) as? NSTextField
    }

    /// Ключи, значение которых нельзя ни показывать, ни отправлять обратно.
    static func isSecretSettingKey(_ key: String) -> Bool {
        let k = key.lowercased()
        // Суффикса мало: `sentry_dsn_agent` оканчивается на `_agent`, а DSN —
        // это учётные данные целиком. Поэтому «dsn» ищем вхождением, остальное
        // по концу имени, чтобы не ловить `stt_hotkey_profile` и `token_budget_sec`.
        return k.hasSuffix("_key") || k.hasSuffix("_token") || k.hasSuffix("_password")
            || k.hasSuffix("_secret") || k.contains("_dsn") || k.contains("auth_token")
    }

    func buildAllSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "all_settings_table",
            title: "Все настройки",
            isExpanded: false,
            iconSymbol: "slider.horizontal.3"
        )
        let card = ThemeCardView()

        let searchField = NSSearchField()
        searchField.placeholderString = "Поиск по названию настройки"
        searchField.target = self
        searchField.action = #selector(onAllSettingsSearchChanged)
        searchField.sendsSearchStringImmediately = true
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.searchField, searchField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let reloadButton = ThemeSecondaryButton(
            title: "Обновить", target: self, action: #selector(onAllSettingsReload)
        )
        let statusLabel = NSTextField(labelWithString: "Раскройте секцию, чтобы загрузить список")
        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.statusLabel, statusLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let header = NSStackView(views: [searchField, reloadButton])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = KrabEarTheme.Metrics.standard
        searchField.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let rowsStack = NSStackView()
        rowsStack.orientation = .vertical
        rowsStack.alignment = .leading
        rowsStack.spacing = 2
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.rowsStack, rowsStack, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(header)
        card.contentStackView.addArrangedSubview(statusLabel)
        card.contentStackView.addArrangedSubview(rowsStack)
        section.contentStackView.addArrangedSubview(card)

        // Наполняем при первом раскрытии, а не при сборке панели.
        section.onExpandedChange = { [weak self] expanded in
            guard expanded else { return }
            self?.loadAllSettingsIfNeeded()
        }
        return section
    }

    // MARK: - Загрузка

    func loadAllSettingsIfNeeded() {
        let already = (objc_getAssociatedObject(self, &AllSettingsAssocKeys.loaded) as? Bool) ?? false
        guard !already else { return }
        objc_setAssociatedObject(self, &AllSettingsAssocKeys.loaded, true, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        reloadAllSettingsTable()
    }

    @objc func onAllSettingsReload() {
        reloadAllSettingsTable()
    }

    /// IPC строго off-main (AGENT-3: синхронный вызов на главном потоке = AppHang).
    private func reloadAllSettingsTable() {
        allSettingsStatusLabel?.stringValue = "Загружаю…"
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let response = try? ipc.call(method: "get_settings", params: [:])
            let values = (response?["result"] as? [String: Any]) ?? [:]
            DispatchQueue.main.async {
                self?.rebuildAllSettingsRows(from: values)
            }
        }
    }

    private func rebuildAllSettingsRows(from values: [String: Any]) {
        guard let stack = allSettingsRowsStack else { return }
        for view in stack.arrangedSubviews {
            stack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        var rows: [AllSettingsRow] = []
        // `settings` — вложенное эхо всего словаря, не настройка: показывать его
        // строкой значило бы предложить редактировать сам ответ.
        for key in values.keys.sorted() where key != "settings" && key != "ok" {
            guard let row = makeAllSettingsRow(key: key, value: values[key]) else { continue }
            rows.append(row)
            stack.addArrangedSubview(row.view)
        }
        objc_setAssociatedObject(self, &AllSettingsAssocKeys.rowIndex, rows, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        allSettingsStatusLabel?.stringValue = "Настроек: \(rows.count)"
        applyAllSettingsFilter()
    }

    private func makeAllSettingsRow(key: String, value: Any?) -> AllSettingsRow? {
        let control: NSView
        if Self.isSecretSettingKey(key) {
            let text = (value as? String) ?? ""
            let isSet = !text.isEmpty
            let label = NSTextField(labelWithString: isSet ? "задано" : "не задано")
            label.textColor = isSet ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textSecondary
            label.font = KrabEarTheme.Typography.caption
            control = label
        } else if let flag = value as? Bool {
            let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onAllSettingsToggle(_:)))
            toggle.state = flag ? .on : .off
            toggle.identifier = NSUserInterfaceItemIdentifier(key)
            control = toggle
        } else if value is NSNumber || value is String {
            let field = NSTextField(string: String(describing: value ?? ""))
            field.identifier = NSUserInterfaceItemIdentifier(key)
            field.target = self
            field.action = #selector(onAllSettingsFieldCommitted(_:))
            field.widthAnchor.constraint(equalToConstant: 220).isActive = true
            control = field
        } else {
            // Списки и словари редактировать построчно нечем — показываем как есть.
            let label = NSTextField(labelWithString: String(describing: value ?? "—"))
            label.font = KrabEarTheme.Typography.caption
            label.textColor = KrabEarTheme.Colors.textSecondary
            label.lineBreakMode = .byTruncatingTail
            label.widthAnchor.constraint(equalToConstant: 220).isActive = true
            control = label
        }
        let view = makeSettingRow(label: key, description: nil, control: control)
        return AllSettingsRow(key: key, view: view, haystack: key.lowercased())
    }

    // MARK: - Поиск

    @objc func onAllSettingsSearchChanged() {
        applyAllSettingsFilter()
    }

    private func applyAllSettingsFilter() {
        guard let rows = objc_getAssociatedObject(self, &AllSettingsAssocKeys.rowIndex) as? [AllSettingsRow]
        else { return }
        let field = objc_getAssociatedObject(self, &AllSettingsAssocKeys.searchField) as? NSSearchField
        let query = (field?.stringValue ?? "").trimmingCharacters(in: .whitespaces).lowercased()
        var shown = 0
        for row in rows {
            let visible = query.isEmpty || row.haystack.contains(query)
            row.view.isHidden = !visible
            if visible { shown += 1 }
        }
        if !query.isEmpty {
            allSettingsStatusLabel?.stringValue = "Найдено: \(shown) из \(rows.count)"
        } else {
            allSettingsStatusLabel?.stringValue = "Настроек: \(rows.count)"
        }
    }

    // MARK: - Запись

    @objc func onAllSettingsToggle(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue, !isSyncingSettings else { return }
        applySettingsPatch([key: sender.state == .on])
    }

    /// Тип значения сохраняем: строковое поле для числовой настройки вернуло бы
    /// строку, и валидатор бэкенда либо отверг бы её, либо записал бы текст в
    /// числовое поле.
    @objc func onAllSettingsFieldCommitted(_ sender: NSTextField) {
        guard let key = sender.identifier?.rawValue, !isSyncingSettings else { return }
        let raw = sender.stringValue.trimmingCharacters(in: .whitespaces)
        let patched: Any
        if let intValue = Int(raw) {
            patched = intValue
        } else if let doubleValue = Double(raw) {
            patched = doubleValue
        } else {
            patched = raw
        }
        applySettingsPatch([key: patched])
        allSettingsStatusLabel?.stringValue = "Сохранено: \(key)"
    }

    // MARK: - CD Builders

    @MainActor
    func cdBuildAllSettingsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_all_settings_table",
            title: "Все настройки",
            isExpanded: false
        )
        let card = CDSettingsCardView()

        let searchField = NSSearchField()
        searchField.placeholderString = "Поиск по ключу"
        searchField.target = self
        searchField.action = #selector(onAllSettingsSearchChanged)
        searchField.sendsSearchStringImmediately = true
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.searchField, searchField, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let reloadButton = ThemeSecondaryButton(
            title: "Обновить", target: self, action: #selector(onAllSettingsReload)
        )
        
        let headerRow = cdMakeRow(label: "Поиск и обновление", control: NSStackView(views: [searchField, reloadButton]))
        (headerRow.subviews.last as? NSStackView)?.spacing = KrabEarTheme.Metrics.tight
        searchField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        
        let statusLabel = NSTextField(labelWithString: "Раскройте секцию, чтобы загрузить список")
        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.statusLabel, statusLabel, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let rowsStack = NSStackView()
        rowsStack.orientation = .vertical
        rowsStack.alignment = .leading
        rowsStack.spacing = 2
        objc_setAssociatedObject(
            self, &AllSettingsAssocKeys.rowsStack, rowsStack, .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        card.contentStackView.addArrangedSubview(headerRow)
        card.contentStackView.addArrangedSubview(statusLabel)
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(rowsStack)

        section.contentStackView.addArrangedSubview(card)

        section.onExpandedChange = { [weak self] expanded in
            guard expanded else { return }
            self?.loadAllSettingsIfNeeded()
        }
        
        return section
    }

}
