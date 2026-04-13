import Cocoa
import ObjectiveC

@MainActor
private class AnalyticsUIState {
    let todayLabel = NSTextField(labelWithString: "Сегодня: -")
    let weekLabel = NSTextField(labelWithString: "Неделя: -")
    let totalLabel = NSTextField(labelWithString: "Всего: -")
    let paceLabel = NSTextField(labelWithString: "Темп: -")
    let qualityLabel = NSTextField(labelWithString: "Оценка: -")

    let sttStatus = NSTextField(labelWithString: "STT: ?")
    let llmStatus = NSTextField(labelWithString: "LLM: ?")
    let historyStatus = NSTextField(labelWithString: "История: ?")
    let translationStatus = NSTextField(labelWithString: "Перевод: ?")

    init() {
        let labels = [todayLabel, weekLabel, totalLabel, paceLabel, qualityLabel]
        for label in labels {
            label.font = KrabEarTheme.Typography.controlLabel
            label.isEditable = false
            label.isBordered = false
            label.backgroundColor = .clear
        }

        let statusLabels = [sttStatus, llmStatus, historyStatus, translationStatus]
        for label in statusLabels {
            label.font = KrabEarTheme.Typography.monospaced
            label.isEditable = false
            label.isBordered = false
            label.backgroundColor = .clear
        }
    }
}

nonisolated(unsafe) private var analyticsUIStateKey: UInt8 = 0

@MainActor
extension HistoryPanelController {

    private var analyticsUIState: AnalyticsUIState {
        if let state = objc_getAssociatedObject(self, &analyticsUIStateKey) as? AnalyticsUIState {
            return state
        }
        let state = AnalyticsUIState()
        objc_setAssociatedObject(self, &analyticsUIStateKey, state, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return state
    }

    var todayLabel: NSTextField { analyticsUIState.todayLabel }
    var weekLabel: NSTextField { analyticsUIState.weekLabel }
    var totalLabel: NSTextField { analyticsUIState.totalLabel }
    var paceLabel: NSTextField { analyticsUIState.paceLabel }
    var qualityLabel: NSTextField { analyticsUIState.qualityLabel }
    var sttStatus: NSTextField { analyticsUIState.sttStatus }
    var llmStatus: NSTextField { analyticsUIState.llmStatus }
    var historyStatus: NSTextField { analyticsUIState.historyStatus }
    var translationStatus: NSTextField { analyticsUIState.translationStatus }

    func setupAnalyticsSections() -> (analytics: CollapsibleSectionView, systemHealth: CollapsibleSectionView) {
        let analyticsSection = CollapsibleSectionView(sectionId: "dictation_analytics", title: "Аналитика", isExpanded: false)
        let analyticsCard = ThemeCardView()

        let updateStatsBtn = NSButton(title: "Обновить", target: self, action: #selector(onUpdateStats))
        let row1 = NSStackView(views: [todayLabel, weekLabel, totalLabel, updateStatsBtn])
        row1.orientation = .horizontal
        row1.spacing = 16

        let errorStatsBtn = NSButton(title: "Ошибки", target: self, action: #selector(onErrorStats))
        let speechPaceBtn = NSButton(title: "Темп", target: self, action: #selector(onSpeechPace))
        let row2 = NSStackView(views: [errorStatsBtn, speechPaceBtn, paceLabel])
        row2.orientation = .horizontal
        row2.spacing = 16

        let qualityScoreBtn = NSButton(title: "Оценка", target: self, action: #selector(onQualityScore))
        let row3 = NSStackView(views: [qualityScoreBtn, qualityLabel])
        row3.orientation = .horizontal
        row3.spacing = 16

        analyticsCard.contentStackView.addArrangedSubview(row1)
        analyticsCard.contentStackView.addArrangedSubview(row2)
        analyticsCard.contentStackView.addArrangedSubview(row3)

        if let stack = analyticsSection as? NSStackView {
            stack.addArrangedSubview(analyticsCard)
        } else {
            analyticsSection.addSubview(analyticsCard)
            analyticsCard.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                analyticsCard.topAnchor.constraint(equalTo: analyticsSection.topAnchor, constant: 24),
                analyticsCard.leadingAnchor.constraint(equalTo: analyticsSection.leadingAnchor),
                analyticsCard.trailingAnchor.constraint(equalTo: analyticsSection.trailingAnchor),
                analyticsCard.bottomAnchor.constraint(equalTo: analyticsSection.bottomAnchor)
            ])
        }

        let healthSection = CollapsibleSectionView(sectionId: "dictation_system_health", title: "Здоровье системы", isExpanded: false)
        let healthCard = ThemeCardView()

        let healthCheckBtn = NSButton(title: "Проверить", target: self, action: #selector(onHealthCheck))
        let hRow1 = NSStackView(views: [healthCheckBtn, sttStatus, llmStatus, historyStatus, translationStatus])
        hRow1.orientation = .horizontal
        hRow1.spacing = 12

        let llmDiffBtn = NSButton(title: "LLM diff", target: self, action: #selector(onLLMDiff))
        let contextSttBtn = NSButton(title: "Контекст STT", target: self, action: #selector(onContextMemory))
        let hRow2 = NSStackView(views: [llmDiffBtn, contextSttBtn])
        hRow2.orientation = .horizontal
        hRow2.spacing = 16

        let exportBtn = NSButton(title: "Экспорт настроек", target: self, action: #selector(onExportSettings))
        let importBtn = NSButton(title: "Импорт настроек", target: self, action: #selector(onImportSettings))
        let hRow3 = NSStackView(views: [exportBtn, importBtn])
        hRow3.orientation = .horizontal
        hRow3.spacing = 16

        healthCard.contentStackView.addArrangedSubview(hRow1)
        healthCard.contentStackView.addArrangedSubview(hRow2)
        healthCard.contentStackView.addArrangedSubview(hRow3)

        if let stack = healthSection as? NSStackView {
            stack.addArrangedSubview(healthCard)
        } else {
            healthSection.addSubview(healthCard)
            healthCard.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                healthCard.topAnchor.constraint(equalTo: healthSection.topAnchor, constant: 24),
                healthCard.leadingAnchor.constraint(equalTo: healthSection.leadingAnchor),
                healthCard.trailingAnchor.constraint(equalTo: healthSection.trailingAnchor),
                healthCard.bottomAnchor.constraint(equalTo: healthSection.bottomAnchor)
            ])
        }

        return (analyticsSection, healthSection)
    }

    @objc func onUpdateStats() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_usage_stats", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let today = response["today"] { self.todayLabel.stringValue = "Сегодня: \(today)" }
                    if let week = response["week"] { self.weekLabel.stringValue = "Неделя: \(week)" }
                    if let total = response["total"] { self.totalLabel.stringValue = "Всего: \(total)" }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка обновления статистики: \(error)")
                }
            }
        }
    }

    @objc func onErrorStats() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_error_stats", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    let statsString = response.map { "\($0.key): \($0.value)" }.joined(separator: "\n")
                    self.showDiagnosticsOutput("Статистика ошибок:\n\(statsString)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка получения лога ошибок: \(error)")
                }
            }
        }
    }

    @objc func onSpeechPace() {
        let row = tableView.selectedRow
        guard items.indices.contains(row) else {
            showDiagnosticsOutput("Выберите элемент в истории для анализа темпа.")
            return
        }
        let itemID = items[row].id
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "analyze_speech_pace", params: ["id": itemID]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let pace = response["pace"] as? String {
                        self.paceLabel.stringValue = "Темп: \(pace)"
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка анализа темпа: \(error)")
                }
            }
        }
    }

    @objc func onQualityScore() {
        let row = tableView.selectedRow
        guard items.indices.contains(row) else {
            showDiagnosticsOutput("Выберите элемент в истории для оценки качества.")
            return
        }
        let itemID = items[row].id
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "score_transcription", params: ["id": itemID]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let score = response["score"] {
                        self.qualityLabel.stringValue = "Оценка: \(score)"
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка оценки: \(error)")
                }
            }
        }
    }

    @objc func onHealthCheck() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "health_check", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.updateHealthStatus(label: self.sttStatus, prefix: "STT", status: response["stt"] as? String ?? "unknown")
                    self.updateHealthStatus(label: self.llmStatus, prefix: "LLM", status: response["llm"] as? String ?? "unknown")
                    self.updateHealthStatus(label: self.historyStatus, prefix: "История", status: response["history"] as? String ?? "unknown")
                    self.updateHealthStatus(label: self.translationStatus, prefix: "Перевод", status: response["translation"] as? String ?? "unknown")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка проверки здоровья: \(error)")
                }
            }
        }
    }

    private func updateHealthStatus(label: NSTextField, prefix: String, status: String) {
        label.stringValue = "● \(prefix): \(status)"
        switch status.lowercased() {
        case "ok", "online", "success", "active":
            label.textColor = KrabEarTheme.Colors.success
        case "error", "offline", "fail", "disconnected":
            label.textColor = KrabEarTheme.Colors.error
        case "warning", "degraded":
            label.textColor = KrabEarTheme.Colors.warning
        default:
            label.textColor = NSColor.labelColor
        }
    }

    @objc func onLLMDiff() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_last_llm_diff", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let diff = response["diff"] as? String {
                        self.showDiagnosticsOutput("LLM Diff:\n\(diff)")
                    } else {
                        self.showDiagnosticsOutput("LLM Diff: нет данных")
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка получения LLM diff: \(error)")
                }
            }
        }
    }

    @objc func onContextMemory() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "get_context_memory", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if let memory = response["memory"] as? String {
                        self.showDiagnosticsOutput("Контекст STT:\n\(memory)")
                    } else {
                        self.showDiagnosticsOutput("Контекст STT: пусто")
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка получения контекста: \(error)")
                }
            }
        }
    }

    @objc func onExportSettings() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try self?.ipcClient.call(method: "export_settings", params: [:]) ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    let panel = NSSavePanel()
                    panel.allowedContentTypes = [.json]
                    panel.nameFieldStringValue = "settings.json"

                    if panel.runModal() == .OK, let url = panel.url {
                        do {
                            let jsonData = try JSONSerialization.data(withJSONObject: response, options: .prettyPrinted)
                            try jsonData.write(to: url)
                            self.showDiagnosticsOutput("Настройки успешно экспортированы.")
                        } catch {
                            self.showDiagnosticsOutput("Ошибка сохранения файла: \(error.localizedDescription)")
                        }
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка экспорта настроек: \(error)")
                }
            }
        }
    }

    @objc func onImportSettings() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.canChooseFiles = true
        panel.canChooseDirectories = false

        if panel.runModal() == .OK, let url = panel.url {
            do {
                let data = try Data(contentsOf: url)
                guard let json = try JSONSerialization.jsonObject(with: data, options: []) as? [String: Any] else {
                    showDiagnosticsOutput("Неверный формат файла настроек.")
                    return
                }

                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                    do {
                        _ = try self?.ipcClient.call(method: "import_settings", params: json) ?? [:]
                        DispatchQueue.main.async {
                            self?.showDiagnosticsOutput("Настройки успешно импортированы.")
                        }
                    } catch {
                        DispatchQueue.main.async {
                            self?.showDiagnosticsOutput("Ошибка импорта настроек сервером: \(error)")
                        }
                    }
                }
            } catch {
                showDiagnosticsOutput("Ошибка чтения файла: \(error.localizedDescription)")
            }
        }
    }
}
