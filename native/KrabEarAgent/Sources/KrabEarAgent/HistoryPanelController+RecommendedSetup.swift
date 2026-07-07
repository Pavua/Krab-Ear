/*
 Рекомендованная настройка (A1): секция Настроек с превью dry_run
 apply_recommended_setup + кнопками «Применить рекомендуемое» / «Отменить последнее».

 IPC-контракт:
   - apply_recommended_setup {dry_run: true}
       -> result {ok, dry_run, tier, applied: [{key, old_value, new_value, restart_required}],
                  skipped: [{key, reason}], rationale, snapshot_id, restart_required}
   - apply_recommended_setup {dry_run: false} -> тот же shape, snapshot_id заполнен.
   - list_settings_backups {limit: 10} -> {backups: [{backup_id, ts, reason, ...}]}
     — БЕЗ server-side фильтра по reason; секция фильтрует клиентски
     reason == "before_recommended_setup", берёт САМЫЙ СВЕЖИЙ (backups отсортированы
     от новых к старым, см. settings_service.py handle_list_settings_backups).
   - restore_settings_backup {backup_id} -> {restored_settings, backup_id[, warning,
     dropped_fields]}.

 Архитектура (зеркало HistoryPanelController+Calibration.swift):
   - buildRecommendedSetupSection() / cdBuildRecommendedSetupSection()
   - fetchAndRebuildRecommendedSetupCard(isClaudeDesign:) — dry_run превью, off-main.
   - onApplyRecommendedSetup(_:) — apply_recommended_setup{dry_run:false} off-main.
   - onUndoLastRecommendedSetup(_:) — находит последний backup с
     reason=before_recommended_setup, restore_settings_backup off-main.

 Правила AGENT-3: IPC строго DispatchQueue.global, UI-мутации строго main.
 Визуал — docs/design-briefs/2026-07-07-recommended-setup-ui.md (agy, отдельно).
*/

import AppKit
import Foundation

private enum RecommendedSetupAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var lastPreview: UInt8 = 0
}

struct RecommendedSetupPreview {
    let tier: String
    let applied: [[String: Any]]
    let skipped: [[String: Any]]
    let rationale: String
}

extension HistoryPanelController {

    @MainActor
    func buildRecommendedSetupSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "recommended_setup",
            title: "Рекомендованная настройка",
            isExpanded: false,
            iconSymbol: "wand.and.stars"
        )
        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка...")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &RecommendedSetupAssocKeys.card, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
        return section
    }

    @MainActor
    func cdBuildRecommendedSetupSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_recommended_setup", title: "Рекомендованная настройка", isExpanded: false
        )
        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка...")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(self, &RecommendedSetupAssocKeys.cdCard, card, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        return section
    }

    func fetchAndRebuildRecommendedSetupCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var preview: RecommendedSetupPreview?
            do {
                let resp = try ipc.call(method: "apply_recommended_setup", params: ["dry_run": true])
                let result = resp["result"] as? [String: Any] ?? [:]
                preview = RecommendedSetupPreview(
                    tier: (result["tier"] as? String) ?? "low",
                    applied: (result["applied"] as? [[String: Any]]) ?? [],
                    skipped: (result["skipped"] as? [[String: Any]]) ?? [],
                    rationale: (result["rationale"] as? String) ?? ""
                )
            } catch {
                preview = nil
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                objc_setAssociatedObject(
                    self, &RecommendedSetupAssocKeys.lastPreview, preview,
                    .OBJC_ASSOCIATION_RETAIN_NONATOMIC
                )
                if isClaudeDesign {
                    self.rebuildCDRecommendedSetupCard(preview: preview)
                } else {
                    self.rebuildGeminiRecommendedSetupCard(preview: preview)
                }
            }
        }
    }

    @MainActor
    private func rebuildGeminiRecommendedSetupCard(preview: RecommendedSetupPreview?) {
        guard let card = objc_getAssociatedObject(self, &RecommendedSetupAssocKeys.card) as? ThemeCardView else { return }
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        guard let preview else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }
        let summary = NSTextField(labelWithString:
            "Класс: \(preview.tier). Будет включено: \(preview.applied.count). Пропущено: \(preview.skipped.count).")
        summary.font = KrabEarTheme.Typography.captionMedium
        card.contentStackView.addArrangedSubview(summary)
        if !preview.rationale.isEmpty {
            let rationale = NSTextField(wrappingLabelWithString: preview.rationale)
            rationale.font = KrabEarTheme.Typography.caption
            rationale.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(rationale)
        }
        card.contentStackView.addArrangedSubview(recommendedSetupButtonRow(isClaudeDesign: false))
    }

    @MainActor
    private func rebuildCDRecommendedSetupCard(preview: RecommendedSetupPreview?) {
        guard let card = objc_getAssociatedObject(self, &RecommendedSetupAssocKeys.cdCard) as? CDSettingsCardView else { return }
        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        guard let preview else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }
        let summary = NSTextField(labelWithString:
            "Класс: \(preview.tier). Будет включено: \(preview.applied.count). Пропущено: \(preview.skipped.count).")
        summary.font = .systemFont(ofSize: 12, weight: .regular)
        card.contentStackView.addArrangedSubview(summary)
        card.contentStackView.addArrangedSubview(recommendedSetupButtonRow(isClaudeDesign: true))
    }

    @MainActor
    private func recommendedSetupButtonRow(isClaudeDesign: Bool) -> NSView {
        let applyButton = ThemePrimaryButton(
            title: "Применить рекомендуемое", target: self,
            action: #selector(onApplyRecommendedSetup(_:))
        )
        let undoButton = ThemeSecondaryButton(
            title: "Отменить последнее", target: self,
            action: #selector(onUndoLastRecommendedSetup(_:))
        )
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.addArrangedSubview(applyButton)
        stack.addArrangedSubview(undoButton)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        stack.addArrangedSubview(spacer)
        return stack
    }

    @objc func onApplyRecommendedSetup(_ sender: NSButton) {
        let ipc = ipcClient
        sender.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var ok = false
            do {
                _ = try ipc.call(method: "apply_recommended_setup", params: ["dry_run": false])
                ok = true
            } catch {
                ok = false
            }
            DispatchQueue.main.async { [weak self] in
                sender.isEnabled = true
                self?.recommendedSetupShowToast(ok ? "Рекомендованная настройка применена" : "Не удалось применить")
            }
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        }
    }

    @objc func onUndoLastRecommendedSetup(_ sender: NSButton) {
        let ipc = ipcClient
        sender.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var message = "Нет сохранённого снимка для отмены"
            do {
                let resp = try ipc.call(method: "list_settings_backups", params: ["limit": 10])
                let backups = (resp["result"] as? [String: Any])?["backups"] as? [[String: Any]] ?? []
                if let last = backups.first(where: { ($0["reason"] as? String) == "before_recommended_setup" }),
                   let backupId = last["backup_id"] as? String {
                    _ = try ipc.call(method: "restore_settings_backup", params: ["backup_id": backupId])
                    message = "Настройки возвращены к состоянию до применения"
                }
            } catch {
                message = "Не удалось отменить: \(error.localizedDescription)"
            }
            DispatchQueue.main.async { [weak self] in
                sender.isEnabled = true
                self?.recommendedSetupShowToast(message)
            }
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: false)
            self.fetchAndRebuildRecommendedSetupCard(isClaudeDesign: true)
        }
    }

    @MainActor
    private func recommendedSetupShowToast(_ message: String) {
        BackendToast.shared.show(message)
    }
}
