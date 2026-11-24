/*
 HistoryPanelController+DictationHeroCard.swift

 Помощники экрана «Диктовка». Прежняя карточка «Быстрый обзор» (чипы 9/12 pt)
 заменена DictationHeroView (редизайн 2026-11, PR 2): сводка теперь в карточке
 кнопки записи, без второго обзора в списке настроек.
*/

import AppKit

extension HistoryPanelController {

    /// Returns user-facing string для current hotkey.
    /// Settings stores enum-like strings: "right_option_toggle", "left_option_toggle", "any_option".
    func currentHotkeyLabel() -> String {
        let key = settingsProvider().hotkey
        if key.hasPrefix("right_option") { return "⌥ Right" }
        if key.hasPrefix("left_option")  { return "⌥ Left" }
        if key.hasPrefix("any_option")   { return "⌥ Any" }
        return "⌥"
    }

    /// Build category header (small uppercased label) для visual grouping
    /// между sections в Dictation tab. Phase 2 IA refactor step 2.
    /// Pattern Apple System Settings 16: caption-size, semibold, textSecondary.
    func makeCategoryHeader(text: String) -> NSView {
        let label = NSTextField(labelWithString: text.uppercased())
        label.font = NSFont.systemFont(ofSize: 10, weight: .semibold)
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.translatesAutoresizingMaskIntoConstraints = false

        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor, constant: -4),
            label.topAnchor.constraint(equalTo: container.topAnchor, constant: KrabEarTheme.Metrics.standard),
            label.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -2),
        ])
        return container
    }
}
