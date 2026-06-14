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

    private var dailyRecapContentView: NSStackView {
        if let v = objc_getAssociatedObject(self, &DailyRecapAssoc.resultsView) as? NSStackView { return v }
        let v = NSStackView()
        v.orientation = .vertical
        v.alignment = .leading
        v.spacing = KrabEarTheme.Metrics.comfortable
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

        card.contentStackView.addArrangedSubview(controlsRow)
        card.contentStackView.addArrangedSubview(dailyRecapStatusLabel)
        
        let container = dailyRecapContentView
        container.translatesAutoresizingMaskIntoConstraints = false
        card.contentStackView.addArrangedSubview(container)
        
        NSLayoutConstraint.activate([
            container.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor)
        ])

        let section = CollapsibleSectionView(
            sectionId: "history_daily_recap",
            title: "Сводка дня",
            isExpanded: false
        )
        
        let sectionContainer = NSStackView()
        sectionContainer.orientation = .vertical
        sectionContainer.addArrangedSubview(card)
        sectionContainer.translatesAutoresizingMaskIntoConstraints = false
        section.contentStackView.addArrangedSubview(sectionContainer)
        
        NSLayoutConstraint.activate([
            sectionContainer.widthAnchor.constraint(equalTo: section.contentStackView.widthAnchor)
        ])
        
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
                        self?.clearDailyRecap()
                    }
                    return
                }
                
                DispatchQueue.main.async {
                    self?.dailyRecapStatusLabel.stringValue = "Готово"
                    if let container = self?.dailyRecapContentView {
                        Self.fillDailyRecap(in: container, data: result)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.dailyRecapStatusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    private func clearDailyRecap() {
        dailyRecapContentView.arrangedSubviews.forEach { $0.removeFromSuperview() }
    }

    /// Сегодняшняя дата в ISO (yyyy-MM-dd); self-contained, без private-хелперов панели.
    private static func todayISO() -> String {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd"
        return fmt.string(from: Date())
    }

    // MARK: - UI Builders
    
    private static func fillDailyRecap(in container: NSStackView, data: [String: Any]) {
        container.arrangedSubviews.forEach { $0.removeFromSuperview() }

        let recordings = (data["total_recordings"] as? Int) ?? 0
        if recordings == 0 {
            let emptyLabel = NSTextField(labelWithString: "За этот день записей нет.")
            emptyLabel.font = KrabEarTheme.Typography.body
            emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
            container.addArrangedSubview(emptyLabel)
            return
        }

        let durationMin = (data["total_duration_min"] as? Double) ?? 0.0
        let words = (data["total_words"] as? Int) ?? 0
        let languages = (data["languages_used"] as? [String: Int]) ?? [:]
        let topics = (data["top_topics"] as? [String]) ?? []
        let highlights = (data["highlights"] as? [String]) ?? []

        // Metrics Row
        let metricsRow = NSStackView()
        metricsRow.orientation = .horizontal
        metricsRow.distribution = .fillEqually
        metricsRow.spacing = KrabEarTheme.Metrics.standard
        metricsRow.addArrangedSubview(createMetricTile(title: "Записей", value: "\(recordings)"))
        metricsRow.addArrangedSubview(createMetricTile(title: "Минут", value: String(format: "%.1f", durationMin)))
        metricsRow.addArrangedSubview(createMetricTile(title: "Слов", value: "\(words)"))
        container.addArrangedSubview(metricsRow)
        
        metricsRow.translatesAutoresizingMaskIntoConstraints = false
        metricsRow.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true

        // Languages
        if !languages.isEmpty {
            let sorted = languages.sorted { $0.value > $1.value }
            let chips = sorted.map { createChip(text: "\($0.key) · \($0.value)") }
            container.addArrangedSubview(createSectionHeader(title: "🌐 Языки"))
            let scroll = createChipsScroll(chips: chips)
            container.addArrangedSubview(scroll)
            scroll.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true
        }

        // Topics
        if !topics.isEmpty {
            let chips = topics.map { createChip(text: $0) }
            container.addArrangedSubview(createSectionHeader(title: "🏷 Темы"))
            let scroll = createChipsScroll(chips: chips)
            container.addArrangedSubview(scroll)
            scroll.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true
        }

        // Highlights
        if !highlights.isEmpty {
            container.addArrangedSubview(createSectionHeader(title: "✨ Главное"))
            let highlightsStack = NSStackView()
            highlightsStack.orientation = .vertical
            highlightsStack.alignment = .leading
            highlightsStack.spacing = KrabEarTheme.Metrics.tight
            
            for h in highlights {
                let bullet = NSTextField(labelWithString: "•")
                bullet.font = KrabEarTheme.Typography.body
                bullet.textColor = KrabEarTheme.Colors.accent
                
                let text = NSTextField(wrappingLabelWithString: h)
                text.font = KrabEarTheme.Typography.body
                text.textColor = KrabEarTheme.Colors.textPrimary
                text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                
                let hStack = NSStackView(views: [bullet, text])
                hStack.orientation = .horizontal
                hStack.alignment = .top
                hStack.spacing = KrabEarTheme.Metrics.tight
                highlightsStack.addArrangedSubview(hStack)
            }
            container.addArrangedSubview(highlightsStack)
            highlightsStack.widthAnchor.constraint(equalTo: container.widthAnchor).isActive = true
        }
    }

    private static func createMetricTile(title: String, value: String) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 2
        stack.wantsLayer = true
        stack.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
        stack.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        stack.edgeInsets = NSEdgeInsets(top: KrabEarTheme.Metrics.comfortable, left: KrabEarTheme.Metrics.standard, bottom: KrabEarTheme.Metrics.comfortable, right: KrabEarTheme.Metrics.standard)

        let valLabel = NSTextField(labelWithString: value)
        valLabel.font = .systemFont(ofSize: 22, weight: .semibold)
        valLabel.textColor = KrabEarTheme.Colors.textPrimary

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = KrabEarTheme.Typography.captionMedium
        titleLabel.textColor = KrabEarTheme.Colors.textSecondary

        stack.addArrangedSubview(valLabel)
        stack.addArrangedSubview(titleLabel)
        return stack
    }

    private static func createChip(text: String) -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 4
        stack.wantsLayer = true
        stack.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
        stack.layer?.cornerRadius = 10
        stack.edgeInsets = NSEdgeInsets(top: 4, left: 8, bottom: 4, right: 8)

        let lbl = NSTextField(labelWithString: text)
        lbl.font = KrabEarTheme.Typography.captionMedium
        lbl.textColor = KrabEarTheme.Colors.textPrimary
        stack.addArrangedSubview(lbl)

        return stack
    }

    private static func createChipsScroll(chips: [NSView]) -> NSView {
        let stack = NSStackView(views: chips)
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.tight

        let scroll = NSScrollView()
        scroll.hasHorizontalScroller = false
        scroll.hasVerticalScroller = false
        scroll.documentView = stack
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: 24).isActive = true
        return scroll
    }

    private static func createSectionHeader(title: String) -> NSTextField {
        let lbl = NSTextField(labelWithString: title)
        lbl.font = KrabEarTheme.Typography.sectionTitle
        lbl.textColor = KrabEarTheme.Colors.textSecondary
        return lbl
    }
}
