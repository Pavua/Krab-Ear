import AppKit

// MARK: - Секция «Сводка дня» (Daily Recap)
//
// Поверхность для backend generate_daily_digest (DailyDigestGenerator): даёт
// нарративную сводку за день — количество записей, длительность, слова, языки,
// топ-темы и «главное». Backend privacy-gated (privacy_mode → ok:false). Поведение
// (секция + IPC off-main по AGENT-3 + заполнение на main) — здесь; визуал собран на
// существующих токенах KrabEarTheme для консистентности.

private enum DailyRecapAssoc {
    nonisolated(unsafe) static var dateField: UInt8 = 0
    nonisolated(unsafe) static var resultsView: UInt8 = 0
    nonisolated(unsafe) static var statusLabel: UInt8 = 0
}

extension HistoryPanelController {

    private var dailyRecapDateField: NSTextField {
        if let v = objc_getAssociatedObject(self, &DailyRecapAssoc.dateField) as? NSTextField { return v }
        let v = NSTextField()
        v.placeholderString = "ГГГГ-ММ-ДД (пусто = сегодня)"
        v.font = KrabEarTheme.Typography.body
        objc_setAssociatedObject(self, &DailyRecapAssoc.dateField, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var dailyRecapResultsView: NSTextView {
        if let v = objc_getAssociatedObject(self, &DailyRecapAssoc.resultsView) as? NSTextView { return v }
        let v = NSTextView()
        v.isEditable = false
        v.isSelectable = true
        v.font = KrabEarTheme.Typography.body
        v.textContainerInset = NSSize(width: 8, height: 8)
        v.minSize = NSSize(width: 0, height: 160)
        objc_setAssociatedObject(self, &DailyRecapAssoc.resultsView, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    private var dailyRecapStatusLabel: NSTextField {
        if let v = objc_getAssociatedObject(self, &DailyRecapAssoc.statusLabel) as? NSTextField { return v }
        let v = NSTextField(labelWithString: "—")
        v.font = KrabEarTheme.Typography.caption
        v.textColor = NSColor.secondaryLabelColor
        objc_setAssociatedObject(self, &DailyRecapAssoc.statusLabel, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return v
    }

    /// Builder секции — вызывается из applyVisualTheme истории.
    public func setupDailyRecapSection() -> CollapsibleSectionView {
        let card = ThemeCardView()

        // По умолчанию — сегодня (isoDateString уже используется пресетами истории).
        dailyRecapDateField.stringValue = Self.todayISO()

        let refreshBtn = ThemeSecondaryButton(title: "Обновить сводку", target: self, action: #selector(onRefreshDailyRecap))
        refreshBtn.applyThemeSecondary()
        let todayBtn = ThemeSecondaryButton(title: "Сегодня", target: self, action: #selector(onDailyRecapToday))
        todayBtn.applyThemeSecondary()

        let controlsRow = NSStackView(views: [dailyRecapDateField, refreshBtn, todayBtn, NSView()])
        controlsRow.orientation = .horizontal
        controlsRow.spacing = KrabEarTheme.Metrics.standard
        controlsRow.distribution = .fill

        let resultsScroll = NSScrollView()
        resultsScroll.hasVerticalScroller = true
        resultsScroll.borderType = .lineBorder
        resultsScroll.documentView = dailyRecapResultsView
        resultsScroll.translatesAutoresizingMaskIntoConstraints = false
        resultsScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true

        card.contentStackView.addArrangedSubview(controlsRow)
        card.contentStackView.addArrangedSubview(dailyRecapStatusLabel)
        card.contentStackView.addArrangedSubview(resultsScroll)

        let section = CollapsibleSectionView(
            sectionId: "history_daily_recap",
            title: "Сводка дня",
            isExpanded: false
        )
        section.contentStackView.addArrangedSubview(card)
        return section
    }

    // MARK: - Actions

    @objc func onDailyRecapToday() {
        dailyRecapDateField.stringValue = Self.todayISO()
        onRefreshDailyRecap()
    }

    @objc func onRefreshDailyRecap() {
        let dateStr = dailyRecapDateField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        dailyRecapStatusLabel.stringValue = "Генерируем сводку…"
        let client = ipcClient
        var params: [String: Any] = [:]
        if !dateStr.isEmpty { params["date_str"] = dateStr }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try client.call(method: "generate_daily_digest", params: params)
                let result = (response["result"] as? [String: Any]) ?? [:]
                if (result["ok"] as? Bool) == false {
                    let privacy = (result["reason"] as? String) == "privacy_mode_active"
                    DispatchQueue.main.async {
                        self?.dailyRecapStatusLabel.stringValue = privacy
                            ? "Сводка недоступна в режиме приватности"
                            : "Нет данных за дату"
                        self?.dailyRecapResultsView.string = ""
                    }
                    return
                }
                let text = HistoryPanelController.formatDailyRecap(result)
                DispatchQueue.main.async {
                    self?.dailyRecapStatusLabel.stringValue = "Готово"
                    self?.dailyRecapResultsView.string = text
                }
            } catch {
                DispatchQueue.main.async {
                    self?.dailyRecapStatusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    /// Сегодняшняя дата в ISO (yyyy-MM-dd); self-contained, без private-хелперов панели.
    private static func todayISO() -> String {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd"
        return fmt.string(from: Date())
    }

    /// Форматирует структурированный digest в читаемый текст.
    static func formatDailyRecap(_ d: [String: Any]) -> String {
        let date = (d["date"] as? String) ?? "—"
        let recordings = (d["total_recordings"] as? Int) ?? 0
        let durationMin = (d["total_duration_min"] as? Double) ?? 0.0
        let words = (d["total_words"] as? Int) ?? 0
        let languages = (d["languages_used"] as? [String: Int]) ?? [:]
        let topics = (d["top_topics"] as? [String]) ?? []
        let highlights = (d["highlights"] as? [String]) ?? []

        var lines: [String] = []
        lines.append("📅 Сводка за \(date)")
        lines.append("")

        if recordings == 0 {
            lines.append("За этот день записей нет.")
            return lines.joined(separator: "\n")
        }

        lines.append("🎙 Записей: \(recordings)    ⏱ \(String(format: "%.1f", durationMin)) мин    📝 \(words) слов")

        if !languages.isEmpty {
            let langStr = languages
                .sorted { $0.value > $1.value }
                .map { "\($0.key): \($0.value)" }
                .joined(separator: ", ")
            lines.append("🌐 Языки: \(langStr)")
        }
        if !topics.isEmpty {
            lines.append("🏷 Темы: \(topics.joined(separator: ", "))")
        }
        if !highlights.isEmpty {
            lines.append("")
            lines.append("✨ Главное:")
            for h in highlights {
                lines.append("  • \(h)")
            }
        }
        return lines.joined(separator: "\n")
    }
}
