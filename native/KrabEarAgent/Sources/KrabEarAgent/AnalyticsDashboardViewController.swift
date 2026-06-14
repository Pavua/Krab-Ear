/*
 AnalyticsDashboardViewController — модальная панель аналитики Krab Ear.

 Показывает агрегированные метрики из backend IPC get_analytics_dashboard:
   • Usage     — всего записей, общая длительность, слова (3 big-number cards)
   • Today     — записи/минуты/слова за сегодня
   • Trends    — confidence / pace / volume тренды (improving / stable / declining)
   • Languages — распределение языков, доля переводов
   • Quality   — avg confidence, low-conf rate, LLM rewrite rate
   • Engagement— streak дней, пиковый час, самый активный день недели
   • Storage   — history.ndjson / backups / cache (МБ)
   • Performance — avg STT latency, p95 latency

 Кнопки:
   • Обновить   — повторный IPC вызов
   • Экспорт HTML — IPC generate_html_report → открыть в браузере
*/

import AppKit
import Foundation

// MARK: - Data model

/// Распарсенный ответ IPC get_analytics_dashboard.
struct AnalyticsDashboardData {
    // overview
    var totalRecordings: Int = 0
    var totalHours: Double = 0
    var totalWords: Int = 0
    var avgDaily: Double = 0

    // today
    var todayRecordings: Int = 0
    var todayDurationMin: Double = 0
    var todayWords: Int = 0

    // trends
    var confidenceTrend: String = "stable"
    var paceTrend: String = "stable"
    var volumeTrend: String = "stable"

    // languages
    var langDistribution: [(lang: String, share: Double)] = []
    var translationRate: Double = 0

    // quality
    var avgConfidence: Double = 0
    var lowConfidenceRate: Double = 0
    var llmRewriteRate: Double = 0

    // engagement
    var streakDays: Int = 0
    var peakHour: Int? = nil
    var mostActiveDay: String = ""

    // storage
    var historySizeMB: Double = 0
    var backupsCount: Int = 0
    var cacheSizeMB: Double = 0

    // performance
    var avgSttLatencyMs: Double = 0
    var p95LatencyMs: Double = 0

    // MARK: - Parse from IPC result dict
    static func parse(from dict: [String: Any]) -> AnalyticsDashboardData {
        var d = AnalyticsDashboardData()

        if let ov = dict["overview"] as? [String: Any] {
            d.totalRecordings = (ov["total_recordings"] as? Int) ?? (ov["total_recordings"] as? NSNumber)?.intValue ?? 0
            d.totalHours = (ov["total_hours"] as? Double) ?? (ov["total_hours"] as? NSNumber)?.doubleValue ?? 0
            d.totalWords = (ov["total_words"] as? Int) ?? (ov["total_words"] as? NSNumber)?.intValue ?? 0
            d.avgDaily = (ov["avg_daily"] as? Double) ?? (ov["avg_daily"] as? NSNumber)?.doubleValue ?? 0
        }

        if let td = dict["today"] as? [String: Any] {
            d.todayRecordings = (td["recordings"] as? Int) ?? (td["recordings"] as? NSNumber)?.intValue ?? 0
            d.todayDurationMin = (td["duration_min"] as? Double) ?? (td["duration_min"] as? NSNumber)?.doubleValue ?? 0
            d.todayWords = (td["words"] as? Int) ?? (td["words"] as? NSNumber)?.intValue ?? 0
        }

        if let tr = dict["trends"] as? [String: Any] {
            d.confidenceTrend = (tr["confidence_trend"] as? String) ?? "stable"
            d.paceTrend = (tr["pace_trend"] as? String) ?? "stable"
            d.volumeTrend = (tr["volume_trend"] as? String) ?? "stable"
        }

        if let la = dict["languages"] as? [String: Any] {
            if let dist = la["distribution"] as? [String: Any] {
                d.langDistribution = dist.map { (lang: $0.key, share: ($0.value as? Double) ?? (($0.value as? NSNumber)?.doubleValue ?? 0)) }
                    .sorted { $0.share > $1.share }
            }
            d.translationRate = (la["translation_rate"] as? Double) ?? (la["translation_rate"] as? NSNumber)?.doubleValue ?? 0
        }

        if let qu = dict["quality"] as? [String: Any] {
            d.avgConfidence = (qu["avg_confidence"] as? Double) ?? (qu["avg_confidence"] as? NSNumber)?.doubleValue ?? 0
            d.lowConfidenceRate = (qu["low_confidence_rate"] as? Double) ?? (qu["low_confidence_rate"] as? NSNumber)?.doubleValue ?? 0
            d.llmRewriteRate = (qu["llm_rewrite_rate"] as? Double) ?? (qu["llm_rewrite_rate"] as? NSNumber)?.doubleValue ?? 0
        }

        if let en = dict["engagement"] as? [String: Any] {
            d.streakDays = (en["streak_days"] as? Int) ?? (en["streak_days"] as? NSNumber)?.intValue ?? 0
            d.peakHour = (en["peak_hour"] as? Int) ?? (en["peak_hour"] as? NSNumber)?.intValue
            d.mostActiveDay = (en["most_active_day"] as? String) ?? ""
        }

        if let st = dict["storage"] as? [String: Any] {
            d.historySizeMB = (st["history_size_mb"] as? Double) ?? (st["history_size_mb"] as? NSNumber)?.doubleValue ?? 0
            d.backupsCount = (st["backups_count"] as? Int) ?? (st["backups_count"] as? NSNumber)?.intValue ?? 0
            d.cacheSizeMB = (st["cache_size_mb"] as? Double) ?? (st["cache_size_mb"] as? NSNumber)?.doubleValue ?? 0
        }

        if let pe = dict["performance"] as? [String: Any] {
            d.avgSttLatencyMs = (pe["avg_stt_latency_ms"] as? Double) ?? (pe["avg_stt_latency_ms"] as? NSNumber)?.doubleValue ?? 0
            d.p95LatencyMs = (pe["p95_latency_ms"] as? Double) ?? (pe["p95_latency_ms"] as? NSNumber)?.doubleValue ?? 0
        }

        return d
    }

    // MARK: - Testable formatting helpers (static — no UI dependency)

    static func trendEmoji(_ trend: String) -> String {
        switch trend {
        case "improving": return "↑"
        case "declining": return "↓"
        default: return "→"
        }
    }

    static func trendDescription(_ trend: String) -> String {
        switch trend {
        case "improving": return "растёт"
        case "declining": return "снижается"
        default: return "стабильно"
        }
    }

    static func formatHour(_ hour: Int) -> String {
        "\(hour):00"
    }

    static func formatPercent(_ value: Double) -> String {
        String(format: "%.1f%%", value * 100)
    }

    static func formatMB(_ mb: Double) -> String {
        mb < 1.0 ? String(format: "%.0f КБ", mb * 1024) : String(format: "%.2f МБ", mb)
    }

    static func confidenceColor(_ value: Double) -> NSColor {
        if value >= 0.85 { return .systemGreen }
        if value >= 0.70 { return .systemOrange }
        return .systemRed
    }
}

// MARK: - Main window controller

/// NSWindowController-обёртка для показа AnalyticsDashboardViewController как sheet.
@MainActor
final class AnalyticsDashboardWindowController: NSWindowController {

    convenience init(ipcClient: IPCClient) {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 720),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        win.title = "Аналитика Krab Ear"
        win.minSize = NSSize(width: 520, height: 500)
        let vc = AnalyticsDashboardViewController(ipcClient: ipcClient)
        win.contentViewController = vc
        self.init(window: win)
    }
}

// MARK: - View Controller

@MainActor
final class AnalyticsDashboardViewController: NSViewController {

    // MARK: - Injected dependencies
    // internal (не private): доступ из AnalyticsDashboardViewController+PDFExport.swift
    let ipcClient: IPCClient

    // MARK: - State
    private var data = AnalyticsDashboardData()
    private var isLoading = false

    // MARK: - UI root
    private let scrollView = NSScrollView()
    private let contentStack = NSStackView()

    // MARK: - Usage section labels
    private let totalRecordingsLabel = NSTextField(labelWithString: "—")
    private let totalHoursLabel = NSTextField(labelWithString: "—")
    private let totalWordsLabel = NSTextField(labelWithString: "—")
    private let avgDailyLabel = NSTextField(labelWithString: "—")

    // MARK: - Today section labels
    private let todayRecordingsLabel = NSTextField(labelWithString: "—")
    private let todayMinLabel = NSTextField(labelWithString: "—")
    private let todayWordsLabel = NSTextField(labelWithString: "—")

    // MARK: - Trends labels
    private let confTrendLabel = NSTextField(labelWithString: "—")
    private let paceTrendLabel = NSTextField(labelWithString: "—")
    private let volTrendLabel = NSTextField(labelWithString: "—")

    // MARK: - Languages
    private let langStack = NSStackView()
    private let translationRateLabel = NSTextField(labelWithString: "—")

    // MARK: - Quality
    private let avgConfLabel = NSTextField(labelWithString: "—")
    private let lowConfLabel = NSTextField(labelWithString: "—")
    private let llmRateLabel = NSTextField(labelWithString: "—")

    // MARK: - Engagement
    private let streakLabel = NSTextField(labelWithString: "—")
    private let peakHourLabel = NSTextField(labelWithString: "—")
    private let mostActiveDayLabel = NSTextField(labelWithString: "—")

    // MARK: - Storage
    private let historyLabel = NSTextField(labelWithString: "—")
    private let backupsLabel = NSTextField(labelWithString: "—")
    private let cacheLabel = NSTextField(labelWithString: "—")

    // MARK: - Performance
    private let avgLatencyLabel = NSTextField(labelWithString: "—")
    private let p95LatencyLabel = NSTextField(labelWithString: "—")

    // MARK: - Status
    let statusLabel = NSTextField(labelWithString: "")  // internal: +PDFExport access

    // MARK: - Init

    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) not supported")
    }

    // MARK: - Lifecycle

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 640, height: 720))
        view.wantsLayer = true
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        buildUI()
        refresh()
    }

    // MARK: - Build UI

    private func buildUI() {
        // ---- Toolbar row ----
        let toolbarStack = NSStackView()
        toolbarStack.orientation = .horizontal
        toolbarStack.spacing = KrabEarTheme.Metrics.standard
        toolbarStack.alignment = .centerY

        let titleLabel = NSTextField(labelWithString: "Аналитика")
        titleLabel.font = .systemFont(ofSize: 16, weight: .semibold)
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary

        let refreshBtn = ThemeSecondaryButton(title: "Обновить", target: self, action: #selector(onRefresh))
        let exportBtn = ThemeSecondaryButton(title: "Экспорт HTML", target: self, action: #selector(onExportHTML))
        let exportPDFBtn = ThemeSecondaryButton(title: "Экспорт PDF", target: self, action: #selector(onExportPDF))

        statusLabel.font = KrabEarTheme.Typography.caption
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.stringValue = ""

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)

        toolbarStack.addArrangedSubview(titleLabel)
        toolbarStack.addArrangedSubview(spacer)
        toolbarStack.addArrangedSubview(statusLabel)
        toolbarStack.addArrangedSubview(refreshBtn)
        toolbarStack.addArrangedSubview(exportBtn)
        toolbarStack.addArrangedSubview(exportPDFBtn)

        // ---- Scroll + content ----
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.drawsBackground = false
        scrollView.automaticallyAdjustsContentInsets = false

        contentStack.orientation = .vertical
        contentStack.spacing = KrabEarTheme.Metrics.standard
        contentStack.alignment = .leading
        contentStack.edgeInsets = NSEdgeInsets(
            top: KrabEarTheme.Metrics.comfortable,
            left: KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.comfortable,
            right: KrabEarTheme.Metrics.comfortable
        )
        contentStack.translatesAutoresizingMaskIntoConstraints = false

        // ---- Build sections ----
        contentStack.addArrangedSubview(buildUsageSection())
        contentStack.addArrangedSubview(buildTodaySection())
        contentStack.addArrangedSubview(buildTrendsSection())
        contentStack.addArrangedSubview(buildLanguagesSection())
        contentStack.addArrangedSubview(buildQualitySection())
        contentStack.addArrangedSubview(buildEngagementSection())
        contentStack.addArrangedSubview(buildStorageSection())
        contentStack.addArrangedSubview(buildPerformanceSection())

        let clipView = NSClipView()
        clipView.documentView = contentStack
        scrollView.contentView = clipView

        // ---- Layout ----
        toolbarStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(toolbarStack)
        view.addSubview(scrollView)

        NSLayoutConstraint.activate([
            toolbarStack.topAnchor.constraint(equalTo: view.topAnchor, constant: KrabEarTheme.Metrics.comfortable),
            toolbarStack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            toolbarStack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -KrabEarTheme.Metrics.comfortable),

            scrollView.topAnchor.constraint(equalTo: toolbarStack.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            contentStack.widthAnchor.constraint(equalTo: scrollView.widthAnchor),
        ])
    }

    // MARK: - Section builders

    private func buildSectionCard(title: String) -> (CollapsibleSectionView, ThemeCardView) {
        let section = CollapsibleSectionView(sectionId: "analytics_\(title.lowercased())", title: title, isExpanded: true)
        let card = ThemeCardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        section.contentStackView.addArrangedSubview(card)
        section.translatesAutoresizingMaskIntoConstraints = false
        return (section, card)
    }

    private func makeLabelRow(title: String, valueLabel: NSTextField) -> NSStackView {
        let titleField = NSTextField(labelWithString: title)
        titleField.font = KrabEarTheme.Typography.body
        titleField.textColor = KrabEarTheme.Colors.textSecondary
        titleField.setContentHuggingPriority(.defaultLow, for: .horizontal)

        valueLabel.font = KrabEarTheme.Typography.body
        valueLabel.textColor = KrabEarTheme.Colors.textPrimary
        valueLabel.alignment = .right

        let row = NSStackView(views: [titleField, valueLabel])
        row.orientation = .horizontal
        row.spacing = KrabEarTheme.Metrics.standard
        row.distribution = .fill
        return row
    }

    private func makeBigNumberCard(title: String, valueLabel: NSTextField, subtitle: String = "") -> NSView {
        valueLabel.font = .monospacedDigitSystemFont(ofSize: 22, weight: .semibold)
        valueLabel.textColor = KrabEarTheme.Colors.textPrimary
        valueLabel.alignment = .center

        let titleField = NSTextField(labelWithString: title)
        titleField.font = KrabEarTheme.Typography.caption
        titleField.textColor = KrabEarTheme.Colors.textSecondary
        titleField.alignment = .center

        let stack = NSStackView(views: subtitle.isEmpty ? [valueLabel, titleField] : [valueLabel, titleField])
        stack.orientation = .vertical
        stack.spacing = KrabEarTheme.Metrics.tight
        stack.alignment = .centerX

        let card = ThemeCardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        card.contentStackView.addArrangedSubview(stack)
        card.contentStackView.alignment = .centerX
        return card
    }

    // ---- Section 1: Usage ----
    private func buildUsageSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Использование")

        // Big number row
        let recordingsCard = makeBigNumberCard(title: "Записей", valueLabel: totalRecordingsLabel)
        let hoursCard = makeBigNumberCard(title: "Часов", valueLabel: totalHoursLabel)
        let wordsCard = makeBigNumberCard(title: "Слов", valueLabel: totalWordsLabel)

        let bigRow = NSStackView(views: [recordingsCard, hoursCard, wordsCard])
        bigRow.orientation = .horizontal
        bigRow.distribution = .fillEqually
        bigRow.spacing = KrabEarTheme.Metrics.standard

        let avgRow = makeLabelRow(title: "Среднее в день:", valueLabel: avgDailyLabel)

        card.contentStackView.addArrangedSubview(bigRow)
        card.contentStackView.addArrangedSubview(avgRow)
        card.contentStackView.alignment = .leading

        bigRow.translatesAutoresizingMaskIntoConstraints = false
        bigRow.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor).isActive = true

        return section
    }

    // ---- Section 2: Today ----
    private func buildTodaySection() -> NSView {
        let (section, card) = buildSectionCard(title: "Сегодня")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Записей:", valueLabel: todayRecordingsLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Минут:", valueLabel: todayMinLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Слов:", valueLabel: todayWordsLabel))

        return section
    }

    // ---- Section 3: Trends ----
    private func buildTrendsSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Тренды (30 дней)")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Уверенность:", valueLabel: confTrendLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Темп речи:", valueLabel: paceTrendLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Объём:", valueLabel: volTrendLabel))

        return section
    }

    // ---- Section 4: Languages ----
    private func buildLanguagesSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Языки")

        langStack.orientation = .vertical
        langStack.spacing = KrabEarTheme.Metrics.tight
        langStack.alignment = .leading
        card.contentStackView.addArrangedSubview(langStack)
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Доля переводов:", valueLabel: translationRateLabel))

        return section
    }

    // ---- Section 5: Quality ----
    private func buildQualitySection() -> NSView {
        let (section, card) = buildSectionCard(title: "Качество")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Средняя уверенность:", valueLabel: avgConfLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Низкая уверенность (<70%):", valueLabel: lowConfLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "LLM переписаний:", valueLabel: llmRateLabel))

        return section
    }

    // ---- Section 6: Engagement ----
    private func buildEngagementSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Активность")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Стрик дней:", valueLabel: streakLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Пиковый час:", valueLabel: peakHourLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Активный день:", valueLabel: mostActiveDayLabel))

        return section
    }

    // ---- Section 7: Storage ----
    private func buildStorageSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Хранилище")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "История:", valueLabel: historyLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Резервные копии:", valueLabel: backupsLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Кэш:", valueLabel: cacheLabel))

        return section
    }

    // ---- Section 8: Performance ----
    private func buildPerformanceSection() -> NSView {
        let (section, card) = buildSectionCard(title: "Производительность")

        card.contentStackView.addArrangedSubview(makeLabelRow(title: "Средняя задержка STT:", valueLabel: avgLatencyLabel))
        card.contentStackView.addArrangedSubview(makeLabelRow(title: "p95 задержка STT:", valueLabel: p95LatencyLabel))

        return section
    }

    // MARK: - Data loading

    func refresh() {
        guard !isLoading else { return }
        isLoading = true
        statusLabel.stringValue = "Загрузка…"

        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try client.call(method: "get_analytics_dashboard", params: ["days": 30])
                let result = (response["result"] as? [String: Any]) ?? [:]
                let parsed = AnalyticsDashboardData.parse(from: result)
                DispatchQueue.main.async {
                    self?.apply(parsed)
                    self?.isLoading = false
                    self?.statusLabel.stringValue = "Обновлено: \(Self.shortTime())"
                }
            } catch {
                DispatchQueue.main.async {
                    self?.isLoading = false
                    self?.statusLabel.stringValue = "Ошибка: \(error.localizedDescription)"
                }
            }
        }
    }

    // MARK: - Apply data to UI

    func apply(_ d: AnalyticsDashboardData) {
        self.data = d

        // Usage
        totalRecordingsLabel.stringValue = "\(d.totalRecordings)"
        totalHoursLabel.stringValue = String(format: "%.1f", d.totalHours)
        totalWordsLabel.stringValue = Self.formatNumber(d.totalWords)
        avgDailyLabel.stringValue = String(format: "%.1f", d.avgDaily)

        // Today
        todayRecordingsLabel.stringValue = "\(d.todayRecordings)"
        todayMinLabel.stringValue = String(format: "%.1f", d.todayDurationMin)
        todayWordsLabel.stringValue = "\(d.todayWords)"

        // Trends
        confTrendLabel.stringValue = "\(AnalyticsDashboardData.trendEmoji(d.confidenceTrend)) \(AnalyticsDashboardData.trendDescription(d.confidenceTrend))"
        confTrendLabel.textColor = Self.trendColor(d.confidenceTrend)

        paceTrendLabel.stringValue = "\(AnalyticsDashboardData.trendEmoji(d.paceTrend)) \(AnalyticsDashboardData.trendDescription(d.paceTrend))"
        paceTrendLabel.textColor = Self.trendColor(d.paceTrend)

        volTrendLabel.stringValue = "\(AnalyticsDashboardData.trendEmoji(d.volumeTrend)) \(AnalyticsDashboardData.trendDescription(d.volumeTrend))"
        volTrendLabel.textColor = Self.trendColor(d.volumeTrend)

        // Languages
        langStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for entry in d.langDistribution.prefix(6) {
            let row = NSStackView()
            row.orientation = .horizontal
            row.spacing = KrabEarTheme.Metrics.standard

            let langLabel = NSTextField(labelWithString: entry.lang.uppercased())
            langLabel.font = KrabEarTheme.Typography.captionMedium
            langLabel.textColor = KrabEarTheme.Colors.textSecondary
            langLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)

            let pct = entry.share
            let bar = buildProgressBar(value: pct)

            let pctLabel = NSTextField(labelWithString: AnalyticsDashboardData.formatPercent(pct))
            pctLabel.font = KrabEarTheme.Typography.caption
            pctLabel.textColor = KrabEarTheme.Colors.textPrimary
            pctLabel.setContentHuggingPriority(.defaultHigh, for: .horizontal)

            row.addArrangedSubview(langLabel)
            row.addArrangedSubview(bar)
            row.addArrangedSubview(pctLabel)
            langStack.addArrangedSubview(row)
        }
        if d.langDistribution.isEmpty {
            let empty = NSTextField(labelWithString: "Нет данных")
            empty.font = KrabEarTheme.Typography.caption
            empty.textColor = KrabEarTheme.Colors.textSecondary
            langStack.addArrangedSubview(empty)
        }
        translationRateLabel.stringValue = AnalyticsDashboardData.formatPercent(d.translationRate)

        // Quality
        let confPct = Int(d.avgConfidence * 100)
        avgConfLabel.stringValue = "\(confPct)%"
        avgConfLabel.textColor = AnalyticsDashboardData.confidenceColor(d.avgConfidence)
        lowConfLabel.stringValue = AnalyticsDashboardData.formatPercent(d.lowConfidenceRate)
        llmRateLabel.stringValue = AnalyticsDashboardData.formatPercent(d.llmRewriteRate)

        // Engagement
        streakLabel.stringValue = d.streakDays > 0 ? "\(d.streakDays) \(dayWord(d.streakDays)) подряд" : "0"
        if let h = d.peakHour {
            peakHourLabel.stringValue = AnalyticsDashboardData.formatHour(h)
        } else {
            peakHourLabel.stringValue = "Нет данных"
        }
        mostActiveDayLabel.stringValue = d.mostActiveDay.isEmpty ? "Нет данных" : d.mostActiveDay

        // Storage
        historyLabel.stringValue = AnalyticsDashboardData.formatMB(d.historySizeMB)
        backupsLabel.stringValue = "\(d.backupsCount) файлов"
        cacheLabel.stringValue = AnalyticsDashboardData.formatMB(d.cacheSizeMB)

        // Performance
        avgLatencyLabel.stringValue = d.avgSttLatencyMs > 0 ? String(format: "%.0f мс", d.avgSttLatencyMs) : "—"
        p95LatencyLabel.stringValue = d.p95LatencyMs > 0 ? String(format: "%.0f мс", d.p95LatencyMs) : "—"
    }

    // MARK: - Actions

    @objc private func onRefresh() {
        refresh()
    }

    @objc private func onExportHTML() {
        statusLabel.stringValue = "Генерируем HTML…"
        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try client.call(method: "generate_html_report", params: [:])
                let result = (response["result"] as? [String: Any]) ?? [:]
                let path = (result["path"] as? String) ?? ""
                DispatchQueue.main.async {
                    self?.statusLabel.stringValue = "HTML готов"
                    if !path.isEmpty, let url = URL(string: "file://\(path)") {
                        NSWorkspace.shared.open(url)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.statusLabel.stringValue = "Ошибка экспорта: \(error.localizedDescription)"
                }
            }
        }
    }

    // MARK: - Helpers

    private func buildProgressBar(value: Double) -> NSProgressIndicator {
        let bar = NSProgressIndicator()
        bar.style = .bar
        bar.minValue = 0
        bar.maxValue = 100
        bar.doubleValue = max(1, value * 100)
        bar.isIndeterminate = false
        bar.controlSize = .small
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.widthAnchor.constraint(greaterThanOrEqualToConstant: 80).isActive = true
        return bar
    }

    private static func trendColor(_ trend: String) -> NSColor {
        switch trend {
        case "improving": return .systemGreen
        case "declining": return .systemRed
        default: return KrabEarTheme.Colors.textPrimary
        }
    }

    private func dayWord(_ n: Int) -> String {
        let mod10 = n % 10
        let mod100 = n % 100
        if mod100 >= 11 && mod100 <= 14 { return "дней" }
        switch mod10 {
        case 1: return "день"
        case 2, 3, 4: return "дня"
        default: return "дней"
        }
    }

    private static func formatNumber(_ n: Int) -> String {
        n >= 1000 ? String(format: "%d K", n / 1000) : "\(n)"
    }

    private static func shortTime() -> String {
        let f = DateFormatter()
        f.timeStyle = .short
        return f.string(from: Date())
    }
}

