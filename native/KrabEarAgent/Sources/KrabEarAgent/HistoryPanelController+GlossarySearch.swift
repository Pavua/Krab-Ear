/*
 HistoryPanelController+GlossarySearch.swift

 Поиск/фильтр по записям глоссария перевода.

 Добавляет секцию «Глоссарий» в Live Translation таб (CollapsibleSectionView).
 Секция содержит:
   1. NSSearchField — фильтрует по исходному или целевому термину (case-insensitive).
      Placeholder: «Поиск по словарю…».
      Последний запрос сохраняется в UserDefaults ключ GlossarySearch_LastQuery.
   2. glossaryListStack — вертикальный стек строк «источник → перевод».
      Строки пересобираются при каждом изменении настроек или запроса поиска.

 Точка входа:
   - setupGlossarySearchSection() — строит CollapsibleSectionView и возвращает
     его для добавления в liveStack (вызывается из HistoryPanelController+ApplyTheme+LiveTab).
   - reloadGlossaryList(glossary:query:) — пересобирает glossaryListStack.
     Вызывается из syncSettingsControls (HistoryPanelController+Settings.swift).
*/

import AppKit
import Foundation

// MARK: - UserDefaults key

private let kGlossaryLastQueryKey = "GlossarySearch_LastQuery"

// MARK: - Associated object key for the section view

// nonisolated(unsafe) — адрес используется ObjC runtime как статический ключ.
nonisolated(unsafe) private var glossarySearchSectionKey: UInt8 = 0

// MARK: - Extension

extension HistoryPanelController: NSSearchFieldDelegate {

    // MARK: - Section builder

    /// Строит и возвращает CollapsibleSectionView «Глоссарий» для liveStack.
    /// Вызывать ОДИН раз из setupLiveTranslationTab() после остальных секций.
    @MainActor
    func setupGlossarySearchSection() -> CollapsibleSectionView {
        // Search field setup
        glossarySearchField.placeholderString = "Поиск по словарю…"
        glossarySearchField.translatesAutoresizingMaskIntoConstraints = false
        glossarySearchField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        glossarySearchField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        glossarySearchField.font = KrabEarTheme.Typography.body
        glossarySearchField.target = self
        glossarySearchField.action = #selector(onGlossarySearchChanged)
        glossarySearchField.delegate = self

        // Restore last query from UserDefaults
        let savedQuery = UserDefaults.standard.string(forKey: kGlossaryLastQueryKey) ?? ""
        glossarySearchField.stringValue = savedQuery

        // List stack setup
        glossaryListStack.orientation = .vertical
        glossaryListStack.alignment = .leading
        glossaryListStack.spacing = KrabEarTheme.Metrics.tight
        glossaryListStack.translatesAutoresizingMaskIntoConstraints = false

        // Placeholder label while list is empty
        let emptyLabel = buildGlossaryEmptyLabel()
        glossaryListStack.addArrangedSubview(emptyLabel)

        // Card wrapping
        let card = ThemeCardView()
        card.contentStackView.addArrangedSubview(glossarySearchField)
        card.contentStackView.addArrangedSubview(glossaryListStack)
        glossarySearchField.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true
        glossaryListStack.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true

        let section = CollapsibleSectionView(
            sectionId: "live_glossary_list",
            title: "Глоссарий",
            isExpanded: false
        )
        section.contentStackView.addArrangedSubview(card)

        // Persist strong reference on the controller.
        objc_setAssociatedObject(self, &glossarySearchSectionKey, section, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        return section
    }

    // MARK: - Filtering (pure, testable)

    /// Фильтрует и сортирует записи глоссария по поисковому запросу.
    /// Совпадение по исходному ИЛИ целевому термину, case-insensitive.
    /// Пустой запрос → все записи.
    /// Вынесена как `static` для возможности unit-тестирования без UI.
    static func filterGlossary(
        _ glossary: [String: String],
        query: String
    ) -> [(key: String, value: String)] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if trimmed.isEmpty {
            return glossary.sorted { $0.key < $1.key }
        }
        return glossary
            .filter { pair in
                pair.key.lowercased().contains(trimmed)
                || pair.value.lowercased().contains(trimmed)
            }
            .sorted { $0.key < $1.key }
    }

    // MARK: - Reload list

    /// Пересобирает glossaryListStack исходя из словаря и поискового запроса.
    /// Вызывается из syncSettingsControls после получения свежих настроек.
    @MainActor
    func reloadGlossaryList(glossary: [String: String], query: String) {
        // Remove all existing arranged subviews
        for sub in glossaryListStack.arrangedSubviews {
            glossaryListStack.removeArrangedSubview(sub)
            sub.removeFromSuperview()
        }

        let filtered = HistoryPanelController.filterGlossary(glossary, query: query)

        if filtered.isEmpty {
            glossaryListStack.addArrangedSubview(buildGlossaryEmptyLabel())
        } else {
            for (source, target) in filtered {
                let row = buildGlossaryRow(source: source, target: target)
                glossaryListStack.addArrangedSubview(row)
                row.widthAnchor.constraint(equalTo: glossaryListStack.widthAnchor).isActive = true
            }
        }
    }

    // MARK: - NSSearchFieldDelegate

    /// Вызывается при нажатии Return или очистке поля через кнопку × .
    func searchFieldDidEndSearching(_ sender: NSSearchField) {
        persistAndReloadGlossarySearch(query: "")
    }

    // MARK: - Action

    @objc func onGlossarySearchChanged() {
        let query = glossarySearchField.stringValue
        persistAndReloadGlossarySearch(query: query)
    }

    // MARK: - Private helpers

    private func persistAndReloadGlossarySearch(query: String) {
        UserDefaults.standard.set(query, forKey: kGlossaryLastQueryKey)
        let glossary = settingsProvider().translationGlossary
        reloadGlossaryList(glossary: glossary, query: query)
    }

    private func buildGlossaryEmptyLabel() -> NSTextField {
        let label = NSTextField(labelWithString: "Глоссарий пуст. Добавьте термины через «Добавить термин».")
        label.font = KrabEarTheme.Typography.caption
        label.textColor = KrabEarTheme.Colors.textDisabled
        label.lineBreakMode = .byWordWrapping
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    /// Строка «источник → перевод» с кнопкой удаления.
    private func buildGlossaryRow(source: String, target: String) -> NSView {
        let sourceLabel = NSTextField(labelWithString: source)
        sourceLabel.font = KrabEarTheme.Typography.body
        sourceLabel.textColor = KrabEarTheme.Colors.textPrimary
        sourceLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)

        let arrowLabel = NSTextField(labelWithString: "→")
        arrowLabel.font = KrabEarTheme.Typography.body
        arrowLabel.textColor = KrabEarTheme.Colors.textSecondary

        let targetLabel = NSTextField(labelWithString: target)
        targetLabel.font = KrabEarTheme.Typography.body
        targetLabel.textColor = KrabEarTheme.Colors.textSecondary
        targetLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        targetLabel.lineBreakMode = .byTruncatingTail

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)

        let deleteBtn = NSButton(title: "✕", target: nil, action: nil)
        deleteBtn.bezelStyle = .inline
        deleteBtn.isBordered = false
        deleteBtn.font = KrabEarTheme.Typography.caption
        deleteBtn.contentTintColor = KrabEarTheme.Colors.textDisabled
        deleteBtn.toolTip = "Удалить «\(source)» из глоссария"
        // Capture source for the action closure.
        deleteBtn.target = GlossaryDeleteTarget(source: source, controller: self)
        deleteBtn.action = #selector(GlossaryDeleteTarget.deleteEntry)

        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.tight
        row.addArrangedSubview(sourceLabel)
        row.addArrangedSubview(arrowLabel)
        row.addArrangedSubview(targetLabel)
        row.addArrangedSubview(spacer)
        row.addArrangedSubview(deleteBtn)
        return row
    }
}

// MARK: - GlossaryDeleteTarget

/// Промежуточный target для кнопок удаления строк глоссария.
/// NSButton требует (target, action) из Obj-C runtime — используем маленький
/// объект-делегат чтобы не засорять HistoryPanelController @objc-методами
/// на каждую строку.
@MainActor
private final class GlossaryDeleteTarget: NSObject {
    let source: String
    weak var controller: HistoryPanelController?

    init(source: String, controller: HistoryPanelController) {
        self.source = source
        self.controller = controller
    }

    @objc func deleteEntry() {
        guard let controller else { return }
        let src = source
        let ipc = controller.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak controller] in
            let ok = (try? ipc.call(
                method: "remove_translation_glossary_item",
                params: ["source": src]
            ))?["ok"] as? Bool == true
            DispatchQueue.main.async { [weak controller] in
                guard let controller else { return }
                if !ok {
                    controller.showInfoAlert(title: "Глоссарий", body: "Не удалось удалить термин.")
                    return
                }
                var nextPayload = controller.settingsProvider().toPayload()
                var glossary = controller.settingsProvider().translationGlossary
                glossary.removeValue(forKey: src)
                nextPayload["translation_glossary"] = glossary
                _ = controller.settingsUpdater(nextPayload)
                controller.syncSettingsControls()
            }
        }
    }
}
