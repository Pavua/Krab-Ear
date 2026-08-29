/*
 STT-движки: секция настроек с перечнем всех STT-движков, их доступностью
 и переключателями enable/disable.

 IPC-контракт:
   - list_stt_engines {}  → result.engines [[String:Any]], result.default String
   - set_settings {toggle_key: Bool}  → персистит в settings.json

 Архитектура:
   - buildSTTEnginesSection() — строит CollapsibleSectionView (Gemini variant, settingsBar).
   - cdBuildSTTEnginesSection() — строит CollapsibleSectionView (Claude Design, settingsBarCD).
   - fetchAndRebuildSTTEnginesCard(isClaudeDesign:) — загружает список с бэкенда,
     перестраивает карточку.
   - onSTTEngineToggleChanged(_:) — обработчик переключателей движков.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum STTEnginesAssocKeys {
    nonisolated(unsafe) static var enginesCard: UInt8 = 0
    nonisolated(unsafe) static var cdEnginesCard: UInt8 = 0

    // Карточка пикера транспорта GigaAM (Gemini)
    nonisolated(unsafe) static var gigaamTransportCard: UInt8 = 0
    nonisolated(unsafe) static var gigaamTransportPicker: UInt8 = 0
    nonisolated(unsafe) static var gigaamTransportWarnLabel: UInt8 = 0

    // Карточка пикера транспорта GigaAM (Claude Design)
    nonisolated(unsafe) static var cdGigaamTransportCard: UInt8 = 0
    nonisolated(unsafe) static var cdGigaamTransportPicker: UInt8 = 0
    nonisolated(unsafe) static var cdGigaamTransportWarnLabel: UInt8 = 0
}

// MARK: - Модель строки движка (internal, single-source)

struct STTEngineRow {
    let name: String
    let displayName: String
    let available: Bool
    let enabled: Bool
    let toggleKey: String?   // nil → always-on / non-toggleable default engine
    let note: String
}

// MARK: - HistoryPanelController+STTEnginesPicker

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «STT-движки» для Gemini-дизайн (settingsBar).
    /// Внутренняя карточка перестраивается асинхронно через fetchAndRebuildSTTEnginesCard.
    @MainActor
    func buildSTTEnginesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_stt_engines",
            title: "STT-движки",
            isExpanded: false,
            iconSymbol: "cpu"
        )

        // Пустая карточка — наполняется асинхронно при первом показе.
        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        // Сохраняем ссылку на карточку через associated object,
        // чтобы перестроить её, когда придут данные с бэкенда.
        objc_setAssociatedObject(
            self,
            &STTEnginesAssocKeys.enginesCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        let transportCard = buildGigaamTransportCard()
        section.contentStackView.addArrangedSubview(transportCard)

        // Запускаем загрузку движков (off-main, AGENT-3).
        fetchAndRebuildSTTEnginesCard(isClaudeDesign: false)

        return section
    }

    /// Статическая карточка пикера транспорта GigaAM: отдельно от асинхронно
    /// перестраиваемой карточки движков — та расставляет разделители по
    /// индексу (index < engines.count - 1), вставка строки внутрь её цикла
    /// сдвинула бы разделители. Видимость (isHidden) и mlxAvailable приходят
    /// позже, из completion fetchAndRebuildSTTEnginesCard (см. Step 5).
    @MainActor
    func buildGigaamTransportCard() -> NSView {
        let card = ThemeCardView()
        // Скрыта по умолчанию — до completion fetchAndRebuildSTTEnginesCard,
        // где пересчитывается по факту stt_gigaam_enabled. Без этого карточка
        // на долю секунды видна при построении секции, пока не пришёл ответ
        // IPC list_stt_engines (GigaAM выключен по умолчанию).
        card.isHidden = true

        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.addItems(withTitles: ["Стабильный (subprocess)", "Быстрый (MLX, экспериментальный)"])
        picker.target = self
        picker.action = #selector(onGigaamTransportChanged(_:))
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportPicker, picker,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let row = makeSettingRow(label: "Транспорт распознавания GigaAM", control: picker)
        card.contentStackView.addArrangedSubview(row)

        let warnLabel = NSTextField(labelWithString: "")
        warnLabel.font = KrabEarTheme.Typography.caption
        warnLabel.textColor = KrabEarTheme.Colors.warning
        warnLabel.isHidden = true
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportWarnLabel, warnLabel,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(warnLabel)

        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportCard, card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        return card
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «STT-движки» для Claude Design (settingsBarCD).
    @MainActor
    func cdBuildSTTEnginesSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_stt_engines",
            title: "STT-движки",
            isExpanded: false
        )

        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &STTEnginesAssocKeys.cdEnginesCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        let cdTransportCard = cdBuildGigaamTransportCard()
        section.contentStackView.addArrangedSubview(cdTransportCard)

        fetchAndRebuildSTTEnginesCard(isClaudeDesign: true)

        return section
    }

    /// Статическая карточка пикера транспорта GigaAM (Claude Design) —
    /// аналог buildGigaamTransportCard(), но на CDSettingsCardView/cdMakeRow.
    /// Скрыта по умолчанию (см. комментарий у Gemini-варианта) — видимость
    /// пересчитывается в completion fetchAndRebuildSTTEnginesCard.
    @MainActor
    func cdBuildGigaamTransportCard() -> NSView {
        let card = CDSettingsCardView()
        card.isHidden = true

        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.addItems(withTitles: ["Стабильный (subprocess)", "Быстрый (MLX, экспериментальный)"])
        picker.target = self
        picker.action = #selector(onGigaamTransportChanged(_:))
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportPicker, picker,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let row = cdMakeRow(label: "Транспорт распознавания GigaAM", control: picker)
        card.contentStackView.addArrangedSubview(row)

        let warnLabel = NSTextField(labelWithString: "")
        warnLabel.font = .systemFont(ofSize: 11, weight: .regular)
        warnLabel.textColor = KrabEarTheme.Colors.warning
        warnLabel.isHidden = true
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportWarnLabel, warnLabel,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(warnLabel)

        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportCard, card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        return card
    }

    // MARK: - Загрузка движков с бэкенда

    /// Запрашивает list_stt_engines строго off-main (AGENT-3), обновляет карточку на main.
    /// isClaudeDesign: выбирает, какую карточку перестраивать.
    func fetchAndRebuildSTTEnginesCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let engines: [STTEngineRow]
            var mlxAvailable = false
            var fetchSucceeded = false
            do {
                let resp = try ipc.call(method: "list_stt_engines", params: [:])
                let result = resp["result"] as? [String: Any]
                let rawList = result?["engines"] as? [[String: Any]] ?? []
                engines = rawList.compactMap { dict -> STTEngineRow? in
                    guard let name = dict["name"] as? String,
                          let displayName = dict["display_name"] as? String else { return nil }
                    return STTEngineRow(
                        name: name,
                        displayName: displayName,
                        available: dict["available"] as? Bool ?? false,
                        enabled: dict["enabled"] as? Bool ?? false,
                        toggleKey: dict["toggle_key"] as? String,
                        note: dict["note"] as? String ?? ""
                    )
                }
                for dict in rawList where (dict["name"] as? String) == "gigaam" {
                    mlxAvailable = dict["mlx_available"] as? Bool ?? false
                }
                fetchSucceeded = true
            } catch {
                engines = []
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDSTTEnginesCard(engines: engines)
                } else {
                    self.rebuildGeminiSTTEnginesCard(engines: engines)
                }

                // C5a(б): видимость карточки транспорта зависит от того,
                // включён ли GigaAM — тумблер живёт в СОСЕДНЕЙ асинхронно
                // перестраиваемой карточке, поэтому пересчёт здесь, а не
                // только при построении секции.
                // C5a(в): mlxAvailable приходит из ЭТОГО же асинхронного
                // ответа — доставляется в статическую карточку тем же completion.
                let gigaamRow = engines.first(where: { $0.name == "gigaam" })
                let gigaamEnabled = gigaamRow?.enabled ?? false

                if isClaudeDesign {
                    if let cdCard = objc_getAssociatedObject(
                        self, &STTEnginesAssocKeys.cdGigaamTransportCard
                    ) as? NSView {
                        cdCard.isHidden = !gigaamEnabled
                    }
                } else {
                    if let card = objc_getAssociatedObject(
                        self, &STTEnginesAssocKeys.gigaamTransportCard
                    ) as? NSView {
                        card.isHidden = !gigaamEnabled
                    }
                }
                // Неудачное наблюдение не должно перезаписывать последнее
                // известное хорошее значение — иначе временный сбой ОДНОГО
                // IPC-запроса на секунду показал бы ложную тревогу «MLX не
                // найден», хотя библиотека установлена и просто backend не
                // успел ответить. Самоисцеляется следующим успешным refetch.
                if fetchSucceeded {
                    self.lastKnownGigaamMlxAvailable = mlxAvailable
                }
                self.syncGigaamTransportControls(
                    settings: self.settingsProvider(),
                    mlxAvailable: self.lastKnownGigaamMlxAvailable
                )
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiSTTEnginesCard(engines: [STTEngineRow]) {
        guard let card = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.enginesCard
        ) as? ThemeCardView else { return }

        // Удаляем все ранее построенные строки.
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if engines.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        for (index, engine) in engines.enumerated() {
            let row = makeGeminiSTTEngineRow(engine: engine)
            card.contentStackView.addArrangedSubview(row)
            if index < engines.count - 1 {
                card.contentStackView.addArrangedSubview(sttMakeSeparator())
            }
        }
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDSTTEnginesCard(engines: [STTEngineRow]) {
        guard let card = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.cdEnginesCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        if engines.isEmpty {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        for (index, engine) in engines.enumerated() {
            let row = makeCDSTTEngineRow(engine: engine)
            card.contentStackView.addArrangedSubview(row)
            if index < engines.count - 1 {
                card.contentStackView.addArrangedSubview(cdMakeSeparator())
            }
        }
    }

    // MARK: - Строки для Gemini-дизайна

    @MainActor
    private func makeGeminiSTTEngineRow(engine: STTEngineRow) -> NSView {
        // Значок доступности (SF Symbol, no force-unwrap).
        let availBadge: NSView
        if engine.available {
            availBadge = makeBadge(
                text: "доступен",
                color: KrabEarTheme.Colors.success,
                tooltip: engine.note.isEmpty ? nil : engine.note,
                symbol: "checkmark.circle.fill"
            )
        } else {
            availBadge = makeBadge(
                text: "недоступен",
                color: KrabEarTheme.Colors.textDisabled,
                tooltip: engine.note.isEmpty ? "Не установлен" : engine.note,
                symbol: "xmark.circle"
            )
        }

        // Правый элемент управления: переключатель или значок «по умолчанию».
        let control: NSView
        if let toggleKey = engine.toggleKey {
            let toggle = NSButton(checkboxWithTitle: "", target: self, action: #selector(onSTTEngineToggleChanged(_:)))
            toggle.setButtonType(.switch)
            toggle.state = engine.enabled ? .on : .off
            toggle.isEnabled = engine.available
            if !engine.available {
                toggle.alphaValue = KrabEarTheme.Interaction.disabledOpacity
            }
            // Кодируем имя настройки как identifier для retrieval в handler.
            toggle.identifier = NSUserInterfaceItemIdentifier(toggleKey)
            toggle.setAccessibilityLabel("\(engine.displayName): включить/выключить")
            KrabEarTheme.styleCheckbox(toggle)
            control = toggle
        } else {
            // Движок по умолчанию — нетогглируемый, показываем звезду.
            let imageView = NSImageView()
            imageView.image = NSImage(systemSymbolName: "star.fill", accessibilityDescription: "по умолчанию")
            imageView.contentTintColor = KrabEarTheme.Colors.accent
            let symCfg = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
            imageView.symbolConfiguration = symCfg
            imageView.toolTip = "Основной движок — всегда активен"
            imageView.setAccessibilityLabel("По умолчанию, всегда активен")
            control = imageView
        }

        let row = makeSettingRow(label: engine.displayName, control: control, badge: availBadge)
        if !engine.note.isEmpty {
            row.toolTip = engine.note
        }
        return row
    }

    // MARK: - Строки для Claude Design

    @MainActor
    private func makeCDSTTEngineRow(engine: STTEngineRow) -> NSView {
        // SF-Symbol значок доступности (no force-unwrap на NSImage).
        let availImageView = NSImageView()
        if engine.available {
            availImageView.image = NSImage(
                systemSymbolName: "checkmark.circle.fill",
                accessibilityDescription: "доступен"
            )
            availImageView.contentTintColor = KrabEarTheme.Colors.success
        } else {
            availImageView.image = NSImage(
                systemSymbolName: "xmark.circle",
                accessibilityDescription: "недоступен"
            )
            availImageView.contentTintColor = KrabEarTheme.Colors.textDisabled
        }
        let symCfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
        availImageView.symbolConfiguration = symCfg
        availImageView.toolTip = engine.note.isEmpty
            ? (engine.available ? "Доступен" : "Не установлен")
            : engine.note

        // Правый элемент управления.
        let control: NSView
        if let toggleKey = engine.toggleKey {
            let toggle = NSButton(
                checkboxWithTitle: "",
                target: self,
                action: #selector(onSTTEngineToggleChanged(_:))
            )
            toggle.setButtonType(.switch)
            toggle.state = engine.enabled ? .on : .off
            toggle.isEnabled = engine.available
            if !engine.available {
                toggle.alphaValue = KrabEarTheme.Interaction.disabledOpacity
            }
            toggle.identifier = NSUserInterfaceItemIdentifier(toggleKey)
            toggle.setAccessibilityLabel("\(engine.displayName): включить/выключить")
            control = toggle
        } else {
            let imageView = NSImageView()
            imageView.image = NSImage(
                systemSymbolName: "star.fill",
                accessibilityDescription: "по умолчанию"
            )
            imageView.contentTintColor = KrabEarTheme.Colors.accent
            let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
            imageView.symbolConfiguration = cfg
            imageView.toolTip = "Основной движок — всегда активен"
            imageView.setAccessibilityLabel("По умолчанию, всегда активен")
            control = imageView
        }

        if !engine.note.isEmpty {
            control.toolTip = engine.note
        }

        return cdMakeRow(
            label: engine.displayName,
            control: control,
            badge: availImageView,
            badgeOnRight: false
        )
    }

    // MARK: - Toggle handler

    /// Обрабатывает нажатие переключателя STT-движка.
    /// Читает toggle_key из identifier, новое состояние из button.state,
    /// отправляет set_settings off-main (AGENT-3), затем перезагружает обе карточки.
    @objc func onSTTEngineToggleChanged(_ sender: NSButton) {
        guard let toggleKey = sender.identifier?.rawValue, !toggleKey.isEmpty else { return }
        let enabled = sender.state == .on

        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try ipc.call(method: "set_settings", params: [toggleKey: enabled])
            } catch {
                // Не удалось применить — откатываем toggle на main thread.
                DispatchQueue.main.async {
                    sender.state = enabled ? .off : .on
                }
                return
            }
            // Перезагружаем список движков для обеих карточек — отражаем новое состояние из бэкенда.
            self.fetchAndRebuildSTTEnginesCard(isClaudeDesign: false)
            self.fetchAndRebuildSTTEnginesCard(isClaudeDesign: true)
        }
    }

    // MARK: - Пикер транспорта GigaAM: обработчик + sync-хук

    /// Обрабатывает выбор транспорта GigaAM. isSyncingSettings защищает от
    /// цикла: syncSettingsControls выставляет этот флаг перед программной
    /// установкой значения пикера (Step 5) — без гварда программная
    /// синхронизация вызвала бы этот обработчик и записала настройку обратно.
    @objc func onGigaamTransportChanged(_ sender: NSPopUpButton) {
        guard !isSyncingSettings else { return }
        let transport = sender.indexOfSelectedItem == 1 ? "mlx" : "subprocess"
        applySettingsPatch(["stt_gigaam_transport": transport])
    }

    /// Синхронизирует пикер и предупреждающий бейдж с текущими settings.
    /// ОБЯЗАН вызываться из syncSettingsControls (Task 3.5) — иначе пикер
    /// не получит начальное значение и не ресинкнется после внешнего
    /// set_settings/apply_profile_preset (та же декоративная проводка,
    /// от которой уже страдал repo — MainErrorsWiringTests-класс).
    ///
    /// mlxAvailable приходит асинхронно из list_stt_engines (Task 1) — другим
    /// путём, чем settings; вызывающая сторона (completion
    /// fetchAndRebuildSTTEnginesCard, Task 3.5) обязана передать актуальное
    /// значение, иначе бейдж будет неактуален.
    @MainActor
    func syncGigaamTransportControls(settings: AgentSettings, mlxAvailable: Bool) {
        let transport = settings.gigaamTransport
        let idx = (transport == "mlx") ? 1 : 0

        if let picker = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportPicker
        ) as? NSPopUpButton {
            picker.selectItem(at: idx)
            // 🔴 БЕЗ ЭТОЙ СТРОКИ item.isEnabled НИЖЕ НЕ СРАБОТАЕТ: NSMenu
            // авто-валидирует пункты перед показом (autoenablesItems=true по
            // умолчанию) и перезаписывает ручное disabled-состояние —
            // прецедент main+CallObserver.swift:56, находка MED-2.
            picker.menu?.autoenablesItems = false
            if let mlxItem = picker.item(at: 1) {
                mlxItem.isEnabled = mlxAvailable
                mlxItem.toolTip = mlxAvailable ? nil : "Требуется библиотека gigaam_mlx"
            }
        }
        if let cdPicker = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportPicker
        ) as? NSPopUpButton {
            cdPicker.selectItem(at: idx)
            cdPicker.menu?.autoenablesItems = false
            if let mlxItem = cdPicker.item(at: 1) {
                mlxItem.isEnabled = mlxAvailable
                mlxItem.toolTip = mlxAvailable ? nil : "Требуется библиотека gigaam_mlx"
            }
        }

        let showWarning = (transport == "mlx") && !mlxAvailable
        let warnText = "MLX выбран, но библиотека не найдена — GigaAM отключён"
        if let warnLabel = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportWarnLabel
        ) as? NSTextField {
            warnLabel.stringValue = warnText
            warnLabel.isHidden = !showWarning
        }
        if let cdWarnLabel = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportWarnLabel
        ) as? NSTextField {
            cdWarnLabel.stringValue = warnText
            cdWarnLabel.isHidden = !showWarning
        }
    }

    // MARK: - Вспомогательный separator (только для этого extension)

    /// NSBox separator — аналог приватного makeSeparator() в +Settings.swift.
    @MainActor
    private func sttMakeSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }
}
