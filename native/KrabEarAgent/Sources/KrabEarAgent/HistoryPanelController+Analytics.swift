import Cocoa
import ObjectiveC

@MainActor
extension HistoryPanelController {
    
    private struct AssociatedKeys {
        nonisolated(unsafe) static var todayLabel: UInt8 = 0
        nonisolated(unsafe) static var weekLabel: UInt8 = 0
        nonisolated(unsafe) static var totalLabel: UInt8 = 0
        nonisolated(unsafe) static var scoreLabel: UInt8 = 0
        nonisolated(unsafe) static var sttHealthLabel: UInt8 = 0
        nonisolated(unsafe) static var llmHealthLabel: UInt8 = 0
        nonisolated(unsafe) static var historyHealthLabel: UInt8 = 0
        nonisolated(unsafe) static var translationHealthLabel: UInt8 = 0
    }
    
    private var todayLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.todayLabel) as? NSTextField {
                return label
            }
            let label = createLabel("Сегодня: —")
            objc_setAssociatedObject(self, &AssociatedKeys.todayLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var weekLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.weekLabel) as? NSTextField {
                return label
            }
            let label = createLabel("Неделя: —")
            objc_setAssociatedObject(self, &AssociatedKeys.weekLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var totalLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.totalLabel) as? NSTextField {
                return label
            }
            let label = createLabel("Всего: —")
            objc_setAssociatedObject(self, &AssociatedKeys.totalLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var scoreLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.scoreLabel) as? NSTextField {
                return label
            }
            let label = createLabel("Оценка: —")
            objc_setAssociatedObject(self, &AssociatedKeys.scoreLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var sttHealthLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.sttHealthLabel) as? NSTextField {
                return label
            }
            let label = createLabel("STT")
            objc_setAssociatedObject(self, &AssociatedKeys.sttHealthLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var llmHealthLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.llmHealthLabel) as? NSTextField {
                return label
            }
            let label = createLabel("LLM")
            objc_setAssociatedObject(self, &AssociatedKeys.llmHealthLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var historyHealthLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.historyHealthLabel) as? NSTextField {
                return label
            }
            let label = createLabel("История")
            objc_setAssociatedObject(self, &AssociatedKeys.historyHealthLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private var translationHealthLabel: NSTextField {
        get {
            if let label = objc_getAssociatedObject(self, &AssociatedKeys.translationHealthLabel) as? NSTextField {
                return label
            }
            let label = createLabel("Перевод")
            objc_setAssociatedObject(self, &AssociatedKeys.translationHealthLabel, label, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return label
        }
    }
    
    private func createLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = NSFont.systemFont(ofSize: 12)
        label.textColor = NSColor.textColor
        return label
    }
    
    public func setupAnalyticsSections() -> (CollapsibleSectionView, CollapsibleSectionView) {
        // --- Analytics section ---
        let analyticsCard = ThemeCardView()

        let usageStack = NSStackView(views: [todayLabel, weekLabel, totalLabel])
        usageStack.orientation = .horizontal
        usageStack.spacing = 8
        let refreshButton = NSButton(title: "Обновить", target: self, action: #selector(refreshUsageStatsAction))
        let usageRow = NSStackView(views: [usageStack, refreshButton])
        usageRow.orientation = .horizontal
        usageRow.spacing = 10

        let errorsButton = NSButton(title: "Ошибки", target: self, action: #selector(fetchErrorStatsAction))

        let scoreButton = NSButton(title: "Оценка", target: self, action: #selector(scoreTranscriptionAction))
        let scoreRow = NSStackView(views: [scoreButton, scoreLabel])
        scoreRow.orientation = .horizontal
        scoreRow.spacing = 10

        analyticsCard.contentStackView.addArrangedSubview(usageRow)
        analyticsCard.contentStackView.addArrangedSubview(errorsButton)
        analyticsCard.contentStackView.addArrangedSubview(scoreRow)

        let analyticsSection = CollapsibleSectionView(sectionId: "dictation_analytics", title: "Аналитика", isExpanded: false)
        analyticsSection.contentStackView.addArrangedSubview(analyticsCard)

        // --- Health section ---
        let healthCard = ThemeCardView()

        let checkButton = NSButton(title: "Проверить", target: self, action: #selector(runHealthCheckAction))
        let healthLabelsStack = NSStackView(views: [sttHealthLabel, llmHealthLabel, historyHealthLabel, translationHealthLabel])
        healthLabelsStack.orientation = .horizontal
        healthLabelsStack.spacing = 8
        let healthRow = NSStackView(views: [checkButton, healthLabelsStack])
        healthRow.orientation = .horizontal
        healthRow.spacing = 10

        let llmDiffButton = NSButton(title: "LLM diff", target: self, action: #selector(fetchLLMDiffAction))

        let exportButton = NSButton(title: "Экспорт настроек", target: self, action: #selector(exportSettingsAction))
        let importButton = NSButton(title: "Импорт настроек", target: self, action: #selector(importSettingsAction))
        let settingsRow = NSStackView(views: [exportButton, importButton])
        settingsRow.orientation = .horizontal
        settingsRow.spacing = 10

        healthCard.contentStackView.addArrangedSubview(healthRow)
        healthCard.contentStackView.addArrangedSubview(llmDiffButton)
        healthCard.contentStackView.addArrangedSubview(settingsRow)

        let healthSection = CollapsibleSectionView(sectionId: "dictation_health", title: "Здоровье", isExpanded: false)
        healthSection.contentStackView.addArrangedSubview(healthCard)

        return (analyticsSection, healthSection)
    }
    
    @objc private func refreshUsageStatsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "get_usage_stats", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.todayLabel.stringValue = "Сегодня: \(result["today"] ?? "0")"
                    self?.weekLabel.stringValue = "Неделя: \(result["week"] ?? "0")"
                    self?.totalLabel.stringValue = "Всего: \(result["total"] ?? "0")"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func fetchErrorStatsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "get_error_stats", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    let text = result.map { "\($0.key): \($0.value)" }.joined(separator: "\n")
                    self?.showDiagnosticsOutput("Статистика ошибок:\n\(text)")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func scoreTranscriptionAction() {
        let selectedRow = self.tableView.selectedRow
        let textToScore = selectedRow >= 0 && selectedRow < items.count ? items[selectedRow].text : items.first?.text ?? ""
        
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "score_transcription", params: ["text": textToScore])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.scoreLabel.stringValue = "Оценка: \(result["score"] ?? "—")"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func runHealthCheckAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "health_check", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.sttHealthLabel.textColor = (result["stt"] as? Bool == true) ? NSColor.systemGreen : NSColor.systemRed
                    self.llmHealthLabel.textColor = (result["llm"] as? Bool == true) ? NSColor.systemGreen : NSColor.systemRed
                    self.historyHealthLabel.textColor = (result["history"] as? Bool == true) ? NSColor.systemGreen : NSColor.systemRed
                    self.translationHealthLabel.textColor = (result["translation"] as? Bool == true) ? NSColor.systemGreen : NSColor.systemRed
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func fetchLLMDiffAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "get_last_llm_diff", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("LLM diff:\n\(result["diff"] ?? "Нет данных")")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func exportSettingsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "export_settings", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Настройки экспортированы: \(result["path"] ?? "Успешно")")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
    
    @objc private func importSettingsAction() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try self?.ipcClient.call(method: "import_settings", params: [:])
                let result = r?["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Настройки импортированы: \(result["status"] ?? "Успешно")")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }
}
