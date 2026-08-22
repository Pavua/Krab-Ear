import Cocoa
import ObjectiveC

// MARK: - Associated key for AnalyticsDashboard window (Analytics tab button)
private enum AnalyticsDashboardAssocKey {
    nonisolated(unsafe) static var windowController: UInt8 = 0
}

@MainActor
extension HistoryPanelController {

    @objc func openAnalyticsDashboard() {
        let wc = AnalyticsDashboardWindowController(ipcClient: ipcClient)
        wc.showWindow(nil)
        objc_setAssociatedObject(self, &AnalyticsDashboardAssocKey.windowController, wc, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
    }

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
        label.font = KrabEarTheme.Typography.body
        label.textColor = NSColor.textColor
        return label
    }
    
    public func setupAnalyticsSections() -> (CollapsibleSectionView, CollapsibleSectionView) {
        // --- Analytics section ---
        let analyticsCard = ThemeCardView()

        let usageStack = NSStackView(views: [todayLabel, weekLabel, totalLabel])
        usageStack.orientation = .horizontal
        usageStack.spacing = KrabEarTheme.Metrics.standard
        let refreshButton = ThemeSecondaryButton(title: "Обновить", target: self, action: #selector(refreshUsageStatsAction))
        refreshButton.applyThemeSecondary()
        let usageRow = NSStackView(views: [usageStack, refreshButton])
        usageRow.orientation = .horizontal
        usageRow.spacing = KrabEarTheme.Metrics.standard

        let errorsButton = ThemeSecondaryButton(title: "Ошибки", target: self, action: #selector(fetchErrorStatsAction))
        errorsButton.applyThemeSecondary()

        let scoreButton = ThemeSecondaryButton(title: "Оценка", target: self, action: #selector(scoreTranscriptionAction))
        scoreButton.applyThemeSecondary()
        let scoreRow = NSStackView(views: [scoreButton, scoreLabel])
        scoreRow.orientation = .horizontal
        scoreRow.spacing = KrabEarTheme.Metrics.standard

        let dashboardButton = ThemeSecondaryButton(title: "Открыть аналитику", target: self, action: #selector(openAnalyticsDashboard))
        dashboardButton.applyThemeSecondary()

        analyticsCard.contentStackView.addArrangedSubview(usageRow)
        analyticsCard.contentStackView.addArrangedSubview(dashboardButton)
        analyticsCard.contentStackView.addArrangedSubview(errorsButton)
        analyticsCard.contentStackView.addArrangedSubview(scoreRow)

        let analyticsSection = CollapsibleSectionView(sectionId: "dictation_analytics", title: "Аналитика", isExpanded: false)
        analyticsSection.contentStackView.addArrangedSubview(analyticsCard)

        // --- Health section ---
        let healthCard = ThemeCardView()

        let checkButton = ThemeSecondaryButton(title: "Проверить", target: self, action: #selector(runHealthCheckAction))
        checkButton.applyThemeSecondary()
        let healthLabelsStack = NSStackView(views: [sttHealthLabel, llmHealthLabel, historyHealthLabel, translationHealthLabel])
        healthLabelsStack.orientation = .horizontal
        healthLabelsStack.spacing = KrabEarTheme.Metrics.standard
        let healthRow = NSStackView(views: [checkButton, healthLabelsStack])
        healthRow.orientation = .horizontal
        healthRow.spacing = KrabEarTheme.Metrics.standard

        let llmDiffButton = ThemeSecondaryButton(title: "LLM diff", target: self, action: #selector(fetchLLMDiffAction))
        llmDiffButton.applyThemeSecondary()

        let exportButton = ThemeSecondaryButton(title: "Экспорт настроек", target: self, action: #selector(exportSettingsAction))
        exportButton.applyThemeSecondary()
        let importButton = ThemeSecondaryButton(title: "Импорт настроек", target: self, action: #selector(importSettingsAction))
        importButton.applyThemeSecondary()
        let settingsRow = NSStackView(views: [exportButton, importButton])
        settingsRow.orientation = .horizontal
        settingsRow.spacing = KrabEarTheme.Metrics.standard

        healthCard.contentStackView.addArrangedSubview(healthRow)
        healthCard.contentStackView.addArrangedSubview(llmDiffButton)
        healthCard.contentStackView.addArrangedSubview(settingsRow)

        let healthSection = CollapsibleSectionView(sectionId: "dictation_health", title: "Здоровье", isExpanded: false)
        healthSection.contentStackView.addArrangedSubview(healthCard)

        return (analyticsSection, healthSection)
    }
    
    // MARK: - Testable static helpers (pure functions)

    /// Форматирует результат get_usage_stats в строки для метки.
    /// `static` — доступна для юнит-тестов без инстанцирования.
    ///
    /// Backend (`UsageTracker.get_usage_stats`) отдаёт периоды как ВЛОЖЕННЫЕ
    /// dict'ы: `today`/`this_week`/`all_time` → `{recordings, total_duration_sec,
    /// total_words}` (usage_tracker.py:110). Метки показывают счётчик записей.
    /// Ранее читались несуществующие плоские ключи `week`/`total` → всегда "0".
    static func usageLabelTexts(from result: [String: Any]) -> (today: String, week: String, total: String) {
        func recordings(_ key: String) -> String {
            guard let period = result[key] as? [String: Any] else { return "0" }
            // recordings — счётчик: всегда целое. Backend шлёт int, но через JSON
            // он может прийти как NSNumber/Double — нормализуем к целому виду.
            if let i = period["recordings"] as? Int { return "\(i)" }
            if let n = period["recordings"] as? NSNumber { return "\(n.intValue)" }
            return "0"
        }
        let today = "Сегодня: \(recordings("today"))"
        let week  = "Неделя: \(recordings("this_week"))"
        let total = "Всего: \(recordings("all_time"))"
        return (today, week, total)
    }

    /// Форматирует результат score_transcription в строку метки.
    ///
    /// Backend (`TextProcessingService.handle_score_transcription`) возвращает
    /// `overall_score` (0–100) + `grade` (A–F) — НЕ плоский `score`
    /// (text_processing_service.py:280). Ранее читался несуществующий `score`
    /// → метка всегда "Оценка: —".
    static func scoreLabelText(from result: [String: Any]) -> String {
        // overall_score is a 0–100 score; transcription_scorer rounds it to a
        // float, so it can arrive as Double (87.0) — coerce to an integer string
        // so the label reads "87", not "87.0".
        let score: String
        if let i = result["overall_score"] as? Int {
            score = "\(i)"
        } else if let n = result["overall_score"] as? NSNumber {
            score = "\(n.intValue)"
        } else {
            score = "—"
        }
        if let grade = result["grade"] as? String, !grade.isEmpty {
            return "Оценка: \(score) (\(grade))"
        }
        return "Оценка: \(score)"
    }

    /// Форматирует error_stats dict в строку диагностики.
    static func errorStatsText(from result: [String: Any]) -> String {
        let text = result.map { "\($0.key): \($0.value)" }.sorted().joined(separator: "\n")
        return "Статистика ошибок:\n\(text)"
    }

    /// Определяет, здоров ли подсистемный компонент по ответу health_check.
    /// Возвращает `true` (success) / `false` (error) — маппинг на NSColor в UI.
    ///
    /// Backend (`HealthChecker.check_all`) кладёт проверки во ВЛОЖЕННЫЙ
    /// `checks` dict, каждая со строковым `status` (health_checker.py:53):
    /// `{"checks": {"stt_model": {"status": "ok"}, "llm": {...}, ...}}`.
    /// `key` — backend-имя проверки (`stt_model`/`llm`/`history_store`/…).
    /// Ранее читались несуществующие плоские Bool `stt`/`llm`/… → панель
    /// всегда показывала все компоненты как нездоровые (красные).
    static func componentHealthy(_ result: [String: Any], key: String) -> Bool {
        guard let checks = result["checks"] as? [String: Any],
              let check = checks[key] as? [String: Any],
              let status = check["status"] as? String else {
            return false
        }
        return status == "ok"
    }

    /// Здоров ли backend в целом (для подсистем без отдельной health-проверки,
    /// напр. offline-переводчик): `status` ∈ ok/degraded, но не error.
    static func backendOverallHealthy(_ result: [String: Any]) -> Bool {
        // HealthChecker._aggregate_status returns ONLY "healthy" / "degraded" /
        // "unhealthy" (never "error" — that's a per-subsystem checks[X].status).
        // The previous `status != "error"` was therefore ALWAYS true → the
        // translation health dot showed green even when the backend was unhealthy.
        // Green only when the backend is fully healthy.
        guard let status = result["status"] as? String else { return false }
        return status == "healthy"
    }

    @objc private func refreshUsageStatsAction() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "get_usage_stats", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    let texts = HistoryPanelController.usageLabelTexts(from: result)
                    self?.todayLabel.stringValue = texts.today
                    self?.weekLabel.stringValue = texts.week
                    self?.totalLabel.stringValue = texts.total
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }

    @objc private func fetchErrorStatsAction() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "get_error_stats", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput(HistoryPanelController.errorStatsText(from: result))
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
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "score_transcription", params: ["text": textToScore])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    self?.scoreLabel.stringValue = HistoryPanelController.scoreLabelText(from: result)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }

    @objc private func runHealthCheckAction() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "health_check", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    func color(_ healthy: Bool) -> NSColor {
                        healthy ? KrabEarTheme.Colors.success : KrabEarTheme.Colors.error
                    }
                    // Backend health_check кладёт проверки в result["checks"][<имя>]["status"]
                    // (health_checker.py): stt_model / llm / history_store. У перевода
                    // отдельной проверки нет (offline-движок) → отражаем общий статус backend.
                    self.sttHealthLabel.textColor = color(HistoryPanelController.componentHealthy(result, key: "stt_model"))
                    self.llmHealthLabel.textColor = color(HistoryPanelController.componentHealthy(result, key: "llm"))
                    self.historyHealthLabel.textColor = color(HistoryPanelController.componentHealthy(result, key: "history_store"))
                    self.translationHealthLabel.textColor = color(HistoryPanelController.backendOverallHealthy(result))
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }

    @objc private func fetchLLMDiffAction() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "get_last_llm_diff", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
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
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "export_settings", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
                DispatchQueue.main.async {
                    // Backend export_settings возвращает "file" (путь), не "path"
                    // (settings_service.py:664).
                    self?.showDiagnosticsOutput("Настройки экспортированы: \(result["file"] ?? "Успешно")")
                }
            } catch {
                DispatchQueue.main.async {
                    self?.showDiagnosticsOutput("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }

    @objc private func importSettingsAction() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "import_settings", params: [:])
                nonisolated(unsafe) let result = r["result"] as? [String: Any] ?? [:]
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
