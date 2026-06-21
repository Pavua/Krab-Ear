import AppKit

// MARK: - Models

struct ActivityCalendarData {
    struct DayInfo {
        let date: String
        let recordings: Int
        let duration_min: Double
        let words: Int
        let level: Int
        
        init?(dict: [String: Any]?) {
            guard let dict = dict,
                  let date = dict["date"] as? String,
                  let level = dict["level"] as? Int else { return nil }
            self.date = date
            self.recordings = dict["recordings"] as? Int ?? 0
            self.duration_min = dict["duration_min"] as? Double ?? 0.0
            self.words = dict["words"] as? Int ?? 0
            self.level = level
        }
    }
    let weeks: [[DayInfo?]]
    let total_active_days: Int
    let longest_streak: Int
    let current_streak: Int
    
    init?(dict: [String: Any]) {
        guard let weeksArray = dict["weeks"] as? [[Any]] else { return nil }
        
        self.weeks = weeksArray.map { week in
            week.map { dayAny -> DayInfo? in
                if let dayDict = dayAny as? [String: Any] {
                    return DayInfo(dict: dayDict)
                }
                return nil
            }
        }
        self.total_active_days = dict["total_active_days"] as? Int ?? 0
        self.longest_streak = dict["longest_streak"] as? Int ?? 0
        self.current_streak = dict["current_streak"] as? Int ?? 0
    }
}

// MARK: - Heatmap View

@MainActor
final class ActivityCalendarHeatmapView: NSView {
    private let cellSize: CGFloat = 11.0
    private let cellGap: CGFloat = 3.0
    private let leftPadding: CGFloat = 30.0
    private let topPadding: CGFloat = 20.0
    
    private var data: ActivityCalendarData?
    private var cellLayers: [CALayer] = []
    private var staticLabels: [NSTextField] = []
    
    let summaryLabel = NSTextField(labelWithString: "")
    
    override var isFlipped: Bool { true }
    
    init() {
        super.init(frame: .zero)
        wantsLayer = true
        
        summaryLabel.font = KrabEarTheme.Typography.captionMedium
        summaryLabel.textColor = KrabEarTheme.Colors.textPrimary
        summaryLabel.isSelectable = false
        summaryLabel.isBordered = false
        summaryLabel.drawsBackground = false
        addSubview(summaryLabel)
    }
    
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
    
    func showEmptyState() {
        self.data = nil
        removeAllToolTips()
        cellLayers.forEach { $0.removeFromSuperlayer() }
        cellLayers.removeAll()
        staticLabels.forEach { $0.removeFromSuperview() }
        staticLabels.removeAll()
        
        summaryLabel.stringValue = "Нет данных активности"
        needsLayout = true
        invalidateIntrinsicContentSize()
    }
    
    private func createLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = KrabEarTheme.Typography.caption
        label.textColor = KrabEarTheme.Colors.textSecondary
        label.isSelectable = false
        label.isBordered = false
        label.drawsBackground = false
        addSubview(label)
        staticLabels.append(label)
        return label
    }
    
    private func getMonthName(_ monthStr: String) -> String {
        switch monthStr {
        case "01": return "Янв"
        case "02": return "Фев"
        case "03": return "Мар"
        case "04": return "Апр"
        case "05": return "Май"
        case "06": return "Июн"
        case "07": return "Июл"
        case "08": return "Авг"
        case "09": return "Сен"
        case "10": return "Окт"
        case "11": return "Ноя"
        case "12": return "Дек"
        default: return ""
        }
    }
    
    func update(with data: ActivityCalendarData) {
        self.data = data
        
        removeAllToolTips()
        cellLayers.forEach { $0.removeFromSuperlayer() }
        cellLayers.removeAll()
        staticLabels.forEach { $0.removeFromSuperview() }
        staticLabels.removeAll()
        
        if data.weeks.isEmpty {
            showEmptyState()
            return
        }
        
        let activeDays = data.total_active_days
        let streak = data.current_streak
        let record = data.longest_streak
        
        func pluralizeDays(_ count: Int) -> String {
            let mod10 = count % 10
            let mod100 = count % 100
            if mod10 == 1 && mod100 != 11 { return "день" }
            if mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20) { return "дня" }
            return "дней"
        }
        
        summaryLabel.stringValue = "🔥 Текущий стрик: \(streak) \(pluralizeDays(streak)) · Рекорд: \(record) \(pluralizeDays(record)) · Активных дней: \(activeDays)"
        
        let rootLayer = layer!

        // data.weeks is [weekday][week_index]: outer index = weekday (0=Mon…6=Sun),
        // inner index = week column. We need 7 rows (weekday, Y) × N columns (week, X).
        // Determine number of week columns from the longest weekday row.
        let numCols = data.weeks.map { $0.count }.max() ?? 0

        // Month labels: one per week column (X axis). Sample the first non-nil day in
        // each column across all weekday rows to get the month for that column.
        var currentMonth = ""
        for col in 0 ..< numCols {
            let firstValidDay = data.weeks.compactMap { weekdayRow -> ActivityCalendarData.DayInfo? in
                guard col < weekdayRow.count else { return nil }
                return weekdayRow[col]
            }.first
            if let date = firstValidDay?.date {
                let parts = date.split(separator: "-")
                if parts.count >= 2 {
                    let monthNum = String(parts[1])
                    if monthNum != currentMonth {
                        currentMonth = monthNum
                        let monthName = getMonthName(monthNum)
                        let label = createLabel(monthName)
                        let x = leftPadding + CGFloat(col) * (cellSize + cellGap)
                        label.frame = CGRect(x: x, y: 0, width: 40, height: 16)
                    }
                }
            }
        }

        // Cell grid: outer = row (weekday, Y), inner = col (week, X).
        for (row, weekdayRow) in data.weeks.enumerated() {
            for (col, day) in weekdayRow.enumerated() {
                let cellLayer = CALayer()
                let x = leftPadding + CGFloat(col) * (cellSize + cellGap)
                let y = topPadding + CGFloat(row) * (cellSize + cellGap)
                cellLayer.frame = CGRect(x: x, y: y, width: cellSize, height: cellSize)
                cellLayer.cornerRadius = 2.0
                cellLayer.backgroundColor = colorForLevel(day?.level ?? 0).cgColor

                // Implicit animations disabled for setup
                cellLayer.actions = ["bounds": NSNull(), "position": NSNull(), "backgroundColor": NSNull()]

                rootLayer.addSublayer(cellLayer)
                cellLayers.append(cellLayer)

                if let day = day {
                    let rect = CGRect(x: x, y: y, width: cellSize, height: cellSize)
                    let tooltipText = "\(day.date): \(day.recordings) записей, \(String(format: "%.1f", day.duration_min)) мин, \(day.words) слов"
                    _ = addToolTip(rect, owner: tooltipText, userData: nil)
                }
            }
        }
        
        let dayNames = ["Пн", "Ср", "Пт"]
        let rowIndices = [0, 2, 4]
        for (i, rowIndex) in rowIndices.enumerated() {
            let label = createLabel(dayNames[i])
            let y = topPadding + CGFloat(rowIndex) * (cellSize + cellGap) - 2.0
            label.frame = CGRect(x: 0, y: y, width: 25, height: 16)
        }
        
        needsLayout = true
        invalidateIntrinsicContentSize()
    }
    
    private func colorForLevel(_ level: Int) -> NSColor {
        if level == 0 {
            // resolve via trait? Just use a dynamic color if possible, or border token
            // border token uses an alpha component natively (0.15 in dark, 0.10 in light)
            return KrabEarTheme.Colors.border
        }
        let alphas: [CGFloat] = [0, 0.35, 0.60, 0.85, 1.0]
        let index = max(1, min(level, 4))
        return KrabEarTheme.Colors.accent.withAlphaComponent(alphas[index])
    }
    
    override func layout() {
        super.layout()
        guard let data = data, !data.weeks.isEmpty else {
            summaryLabel.frame = CGRect(x: 0, y: 0, width: bounds.width, height: 20)
            return
        }
        
        let gridHeight = 7 * (cellSize + cellGap) - cellGap
        summaryLabel.frame = CGRect(x: 0, y: topPadding + gridHeight + 12.0, width: bounds.width, height: 20)
    }
    
    override var intrinsicContentSize: NSSize {
        guard let data = data, !data.weeks.isEmpty else {
            return NSSize(width: NSView.noIntrinsicMetric, height: 20)
        }
        // data.weeks is [weekday][week_index]: width is determined by the number of week columns.
        let weeksCount = CGFloat(data.weeks.map { $0.count }.max() ?? 0)
        let width = leftPadding + weeksCount * (cellSize + cellGap) - cellGap
        let gridHeight = 7 * (cellSize + cellGap) - cellGap
        let totalHeight = topPadding + gridHeight + 12.0 + 20.0
        return NSSize(width: width, height: totalHeight)
    }
    
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        guard let data = data else { return }
        
        var layerIndex = 0
        for week in data.weeks {
            for day in week {
                if layerIndex < cellLayers.count {
                    let level = day?.level ?? 0
                    var resolvedColor = colorForLevel(level)
                    effectiveAppearance.performAsCurrentDrawingAppearance {
                        resolvedColor = colorForLevel(level)
                    }
                    cellLayers[layerIndex].backgroundColor = resolvedColor.cgColor
                    layerIndex += 1
                }
            }
        }
    }
}

// MARK: - Panel Extension

@MainActor
extension HistoryPanelController {
    
    private enum ActivityCalendarAssocKey {
        nonisolated(unsafe) static var heatmapView: UInt8 = 0
    }
    
    private var heatmapView: ActivityCalendarHeatmapView {
        get {
            if let v = objc_getAssociatedObject(self, &ActivityCalendarAssocKey.heatmapView) as? ActivityCalendarHeatmapView {
                return v
            }
            let v = ActivityCalendarHeatmapView()
            objc_setAssociatedObject(self, &ActivityCalendarAssocKey.heatmapView, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return v
        }
    }
    
    public func setupActivityCalendarSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(sectionId: "history_activity_calendar", title: "Календарь активности", isExpanded: true)
        let card = ThemeCardView()
        
        let heatmap = self.heatmapView
        heatmap.translatesAutoresizingMaskIntoConstraints = false
        
        // Wrap in scroll view in case window is too narrow
        let scrollView = NSScrollView()
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.drawsBackground = false
        scrollView.hasVerticalScroller = false
        scrollView.hasHorizontalScroller = true
        scrollView.documentView = heatmap
        
        // Add padding around heatmap
        let container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(scrollView)
        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: container.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            scrollView.heightAnchor.constraint(equalTo: heatmap.heightAnchor)
        ])
        
        card.contentStackView.addArrangedSubview(container)
        section.contentStackView.addArrangedSubview(card)
        
        fetchActivityCalendar()
        
        return section
    }
    
    private func fetchActivityCalendar() {
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "get_activity_calendar", params: ["months": 12])
                if let resultDict = r["result"] as? [String: Any],
                   let data = ActivityCalendarData(dict: resultDict) {
                    DispatchQueue.main.async {
                        self?.heatmapView.update(with: data)
                    }
                } else {
                    DispatchQueue.main.async {
                        self?.heatmapView.showEmptyState()
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.heatmapView.showEmptyState()
                }
            }
        }
    }
}
