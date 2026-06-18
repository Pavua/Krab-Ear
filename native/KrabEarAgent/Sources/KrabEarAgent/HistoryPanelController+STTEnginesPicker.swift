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

        // Запускаем загрузку движков (off-main, AGENT-3).
        fetchAndRebuildSTTEnginesCard(isClaudeDesign: false)

        return section
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
        fetchAndRebuildSTTEnginesCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка движков с бэкенда

    /// Запрашивает list_stt_engines строго off-main (AGENT-3), обновляет карточку на main.
    /// isClaudeDesign: выбирает, какую карточку перестраивать.
    func fetchAndRebuildSTTEnginesCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            let engines: [STTEngineRow]
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
