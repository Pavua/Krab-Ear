/*
 HistoryPanelController+DictationHeroCard.swift

 Phase 2 IA refactor (step 1): Hero Card в начале Dictation tab.

 Согласно Gemini 3.1 Pro design review 2026-04-26 — главная боль текущего
 layout это «равнозначность всех 11 секций». Hero Card создаёт **visual
 hierarchy primary/secondary** через always-expanded summary вверху.

 Hero card содержит read-only сводку самых частых параметров:
 - Hotkey (как monospace chip)
 - Quality profile
 - AutoPaste status
 - GigaAM status (если включен)

 Это **не interactive controls** — пользователь видит state at-a-glance
 без нажатия чтобы expand recording section. Для редактирования — кликает
 секцию ниже (как раньше).

 Pattern: simple ThemeCardView + horizontal NSStackView с 4 chip rows.
*/

import AppKit

extension HistoryPanelController {

    /// Build the Hero card with read-only summary chips. Refresh logic
    /// прикручиваем через notification observation for settings changes;
    /// для simplicity первая версия rebuilds на каждом applyVisualTheme.
    func buildDictationHeroCard() -> ThemeCardView {
        let card = ThemeCardView()
        // Hero gets accent border tint — отделяет от стандартных секций.
        card.wantsLayer = true
        card.layer?.borderWidth = 1.0
        card.layer?.borderColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.3).cgColor

        let title = NSTextField(labelWithString: "Быстрый обзор")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        title.textColor = KrabEarTheme.Colors.textPrimary

        let chipRow = NSStackView()
        chipRow.orientation = .horizontal
        chipRow.alignment = .centerY
        chipRow.spacing = KrabEarTheme.Metrics.standard
        chipRow.distribution = .fillProportionally
        chipRow.translatesAutoresizingMaskIntoConstraints = false

        // Hotkey chip — monospace tile that looks like a physical key.
        let hotkeyText = currentHotkeyLabel()
        let hotkeyChip = makeKeyChip(text: hotkeyText)

        // Quality profile chip — current STT quality.
        let qualityText = settingsProvider().qualityProfile.capitalized
        let qualityChip = makeStatChip(label: "Профиль", value: qualityText)

        // AutoPaste chip.
        let autoPaste = settingsProvider().autoPaste
        let autoPasteChip = makeStatChip(
            label: "Авто-вставка",
            value: autoPaste ? "вкл" : "выкл",
            highlight: autoPaste
        )

        // GigaAM chip (показываем только если enabled).
        let gigaamEnabled = settingsProvider().gigaamEnabled
        let gigaamChip = gigaamEnabled
            ? makeStatChip(label: "GigaAM", value: "RU on", highlight: true)
            : nil

        chipRow.addArrangedSubview(hotkeyChip)
        chipRow.addArrangedSubview(qualityChip)
        chipRow.addArrangedSubview(autoPasteChip)
        if let chip = gigaamChip {
            chipRow.addArrangedSubview(chip)
        }
        chipRow.addArrangedSubview(NSView()) // spacer

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(title)
        stack.addArrangedSubview(chipRow)

        card.contentStackView.addArrangedSubview(stack)
        return card
    }

    // MARK: - Chip helpers

    /// Monospace «physical key» chip — bordered + inset.
    private func makeKeyChip(text: String) -> NSView {
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .medium)
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.translatesAutoresizingMaskIntoConstraints = false

        let chip = NSView()
        chip.wantsLayer = true
        chip.layer?.cornerRadius = 5
        chip.layer?.borderWidth = 1
        chip.layer?.borderColor = NSColor.tertiaryLabelColor.cgColor
        chip.layer?.backgroundColor = NSColor.windowBackgroundColor.withAlphaComponent(0.4).cgColor
        chip.translatesAutoresizingMaskIntoConstraints = false
        chip.addSubview(label)
        NSLayoutConstraint.activate([
            label.topAnchor.constraint(equalTo: chip.topAnchor, constant: 4),
            label.bottomAnchor.constraint(equalTo: chip.bottomAnchor, constant: -4),
            label.leadingAnchor.constraint(equalTo: chip.leadingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: chip.trailingAnchor, constant: -8),
        ])
        return chip
    }

    /// Standard stat chip с label + value (e.g. "Профиль: Max").
    private func makeStatChip(label: String, value: String, highlight: Bool = false) -> NSView {
        let labelText = NSTextField(labelWithString: label.uppercased())
        labelText.font = NSFont.systemFont(ofSize: 9, weight: .semibold)
        labelText.textColor = KrabEarTheme.Colors.textSecondary
        labelText.translatesAutoresizingMaskIntoConstraints = false

        let valueText = NSTextField(labelWithString: value)
        valueText.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        valueText.textColor = highlight ? KrabEarTheme.Colors.accent : KrabEarTheme.Colors.textPrimary
        valueText.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 1
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(labelText)
        stack.addArrangedSubview(valueText)
        return stack
    }

    /// Returns user-facing string для current hotkey.
    /// Settings stores enum-like strings: "right_option_toggle", "left_option_toggle", "any_option".
    private func currentHotkeyLabel() -> String {
        let key = settingsProvider().hotkey
        if key.hasPrefix("right_option") { return "⌥ Right" }
        if key.hasPrefix("left_option")  { return "⌥ Left" }
        if key.hasPrefix("any_option")   { return "⌥ Any" }
        return "⌥"
    }
}
