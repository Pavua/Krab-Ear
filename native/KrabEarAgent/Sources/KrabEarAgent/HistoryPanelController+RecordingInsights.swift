/*
 * File: HistoryPanelController+RecordingInsights.swift
 * Описание: Модуль для отображения секции "Инсайты записей" во вкладке истории.
 * Почему существует: Предоставляет пользователю текстовые инсайты об их активности (пиковые часы, смена языка и т.д.) 
 *                    в удобном виде. Бэкенд уже агрегирует данные, этот модуль отвечает только за фронтенд.
 * Связь: Вызывается в `HistoryPanelController+ApplyTheme+HistoryTab.swift` при формировании стека.
 *        Связывается с бэкендом через `ipcClient.call(method: "get_recording_insights")`.
 */

import AppKit

// MARK: - Models

/// Структура отдельного инсайта
struct RecordingInsight {
    let type: String
    let title: String
    let description: String
    let confidence: Double?
    
    init?(dict: [String: Any]) {
        guard let type = dict["type"] as? String,
              let title = dict["title"] as? String,
              let description = dict["description"] as? String else { return nil }
        self.type = type
        self.title = title
        self.description = description
        self.confidence = dict["confidence"] as? Double
    }
}

/// Структура ответа бэкенда с массивом инсайтов
struct RecordingInsightsData {
    let insights: [RecordingInsight]
    let count: Int
    let days: Int
    let privacyModeActive: Bool
    
    init(dict: [String: Any]) {
        if let insightsArray = dict["insights"] as? [[String: Any]] {
            self.insights = insightsArray.compactMap { RecordingInsight(dict: $0) }
        } else {
            self.insights = []
        }
        self.count = dict["count"] as? Int ?? 0
        self.days = dict["days"] as? Int ?? 7
        self.privacyModeActive = dict["privacy_mode_active"] as? Bool ?? false
    }
}

// MARK: - Insights View

/// Вьюха для рендеринга списка инсайтов.
@MainActor
final class RecordingInsightsView: NSView {
    private let stackView = NSStackView()
    private let emptyLabel = NSTextField(labelWithString: "")
    private var data: RecordingInsightsData?
    
    override var isFlipped: Bool { true }
    
    init() {
        super.init(frame: .zero)
        setupUI()
    }
    
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
    
    private func setupUI() {
        // Контейнер для карточек инсайтов
        stackView.orientation = .vertical
        stackView.spacing = KrabEarTheme.Metrics.standard
        stackView.alignment = .leading
        stackView.translatesAutoresizingMaskIntoConstraints = false
        
        addSubview(stackView)
        
        // Плейсхолдер для пустого состояния
        emptyLabel.font = KrabEarTheme.Typography.captionMedium
        emptyLabel.textColor = KrabEarTheme.Colors.textSecondary
        emptyLabel.isSelectable = false
        emptyLabel.isBordered = false
        emptyLabel.drawsBackground = false
        emptyLabel.translatesAutoresizingMaskIntoConstraints = false
        emptyLabel.isHidden = true
        
        addSubview(emptyLabel)
        
        NSLayoutConstraint.activate([
            stackView.topAnchor.constraint(equalTo: topAnchor),
            stackView.leadingAnchor.constraint(equalTo: leadingAnchor),
            stackView.trailingAnchor.constraint(equalTo: trailingAnchor),
            stackView.bottomAnchor.constraint(equalTo: bottomAnchor),
            
            emptyLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            emptyLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
            emptyLabel.topAnchor.constraint(greaterThanOrEqualTo: topAnchor, constant: KrabEarTheme.Metrics.standard),
            emptyLabel.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -KrabEarTheme.Metrics.standard)
        ])
    }
    
    /// Показывает пустое состояние (когда нет инсайтов)
    func showEmptyState(message: String = "Пока нет инсайтов — записывайте больше, чтобы Krab Ear нашёл закономерности") {
        self.data = nil
        stackView.arrangedSubviews.forEach { $0.removeFromSuperview() }
        
        emptyLabel.stringValue = message
        emptyLabel.isHidden = false
        stackView.isHidden = true
    }
    
    /// Обновляет UI свежими данными
    func update(with data: RecordingInsightsData) {
        self.data = data
        
        stackView.arrangedSubviews.forEach { $0.removeFromSuperview() }
        
        if data.insights.isEmpty || data.privacyModeActive || data.count == 0 {
            let msg = data.privacyModeActive ? "Инсайты недоступны в приватном режиме" : "Пока нет инсайтов — записывайте больше, чтобы Krab Ear нашёл закономерности"
            showEmptyState(message: msg)
            return
        }
        
        emptyLabel.isHidden = true
        stackView.isHidden = false
        
        for insight in data.insights {
            let card = createInsightCard(for: insight)
            stackView.addArrangedSubview(card)
            card.widthAnchor.constraint(equalTo: stackView.widthAnchor).isActive = true
        }
    }
    
    /// Выбираем иконку по типу инсайта
    private func iconName(for type: String) -> String {
        switch type {
        case "peak_hour": return "clock"
        case "language_shift": return "globe"
        case "confidence_change": return "checkmark.seal"
        case "streak": return "flame"
        case "topic": return "tag"
        case "speech_pace": return "waveform"
        default: return "lightbulb"
        }
    }
    
    /// Создает карточку для одного инсайта
    private func createInsightCard(for insight: RecordingInsight) -> NSView {
        let card = ThemeCardView()
        
        let container = NSStackView()
        container.orientation = .horizontal
        container.spacing = KrabEarTheme.Metrics.standard
        container.alignment = .top
        container.translatesAutoresizingMaskIntoConstraints = false
        
        let iconNameStr = iconName(for: insight.type)
        // systemSymbolName может вернуть nil (символ отсутствует в этой ОС или опечатка в имени) —
        // НЕ форс-анврапим: пустой NSImageView без иконки безопаснее краша.
        let iconView = NSImageView()
        iconView.image = NSImage(systemSymbolName: iconNameStr, accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 18, weight: .regular))
        iconView.contentTintColor = KrabEarTheme.Colors.accent
        iconView.translatesAutoresizingMaskIntoConstraints = false
        
        let textContainer = NSStackView()
        textContainer.orientation = .vertical
        textContainer.spacing = 2
        textContainer.alignment = .leading
        
        // Заголовок
        let titleLabel = NSTextField(labelWithString: insight.title)
        titleLabel.font = KrabEarTheme.Typography.sectionTitle
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.isSelectable = false
        titleLabel.lineBreakMode = .byWordWrapping
        titleLabel.maximumNumberOfLines = 0
        
        // Описание
        let descLabel = NSTextField(labelWithString: insight.description)
        descLabel.font = KrabEarTheme.Typography.caption
        descLabel.textColor = KrabEarTheme.Colors.textSecondary
        descLabel.isSelectable = false
        descLabel.lineBreakMode = .byWordWrapping
        descLabel.maximumNumberOfLines = 0
        
        textContainer.addArrangedSubview(titleLabel)
        textContainer.addArrangedSubview(descLabel)
        
        container.addArrangedSubview(iconView)
        container.addArrangedSubview(textContainer)
        
        // Опциональный бейдж confidence
        if let confidence = insight.confidence {
            let confContainer = NSView()
            confContainer.translatesAutoresizingMaskIntoConstraints = false
            let confLabel = NSTextField(labelWithString: "\(Int(confidence * 100))%")
            confLabel.font = KrabEarTheme.Typography.captionMedium
            confLabel.textColor = KrabEarTheme.Colors.textSecondary.withAlphaComponent(0.6)
            confLabel.isSelectable = false
            confLabel.isBordered = false
            confLabel.drawsBackground = false
            confLabel.translatesAutoresizingMaskIntoConstraints = false
            confContainer.addSubview(confLabel)
            
            NSLayoutConstraint.activate([
                confLabel.centerYAnchor.constraint(equalTo: confContainer.centerYAnchor),
                confLabel.trailingAnchor.constraint(equalTo: confContainer.trailingAnchor),
                confLabel.leadingAnchor.constraint(equalTo: confContainer.leadingAnchor),
                confContainer.widthAnchor.constraint(greaterThanOrEqualToConstant: 30)
            ])
            container.addArrangedSubview(confContainer)
            container.setCustomSpacing(KrabEarTheme.Metrics.standard, after: textContainer)
        }
        
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        descLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        
        card.contentStackView.addArrangedSubview(container)
        return card
    }
}

// MARK: - Panel Extension

@MainActor
extension HistoryPanelController {
    
    private enum RecordingInsightsAssocKey {
        nonisolated(unsafe) static var insightsView: UInt8 = 0
    }
    
    /// Свойство-расширение для хранения вьюхи инсайтов
    private var recordingInsightsView: RecordingInsightsView {
        get {
            if let v = objc_getAssociatedObject(self, &RecordingInsightsAssocKey.insightsView) as? RecordingInsightsView {
                return v
            }
            let v = RecordingInsightsView()
            objc_setAssociatedObject(self, &RecordingInsightsAssocKey.insightsView, v, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            return v
        }
    }
    
    /// Инициализация секции "Инсайты записей"
    public func setupRecordingInsightsSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(sectionId: "history_recording_insights", title: "Инсайты записей", isExpanded: true)
        
        let headerControls = NSStackView()
        headerControls.orientation = .horizontal
        headerControls.spacing = KrabEarTheme.Metrics.tight
        
        // Селектор периода для инсайтов
        let periodSelector = NSPopUpButton(frame: .zero, pullsDown: false)
        periodSelector.addItems(withTitles: ["7 дней", "14 дней", "30 дней"])
        periodSelector.selectItem(at: 0)
        periodSelector.target = self
        periodSelector.action = #selector(onInsightsPeriodChanged(_:))
        periodSelector.font = KrabEarTheme.Typography.caption
        
        headerControls.addArrangedSubview(periodSelector)
        section.headerStack.addArrangedSubview(headerControls)
        
        let insightsView = self.recordingInsightsView
        insightsView.translatesAutoresizingMaskIntoConstraints = false
        
        section.contentStackView.addArrangedSubview(insightsView)
        
        // Первичный фетч данных (по умолчанию за 7 дней)
        fetchRecordingInsights(days: 7)
        
        return section
    }
    
    /// Обработчик смены периода в селекторе
    @objc private func onInsightsPeriodChanged(_ sender: NSPopUpButton) {
        let days: Int
        switch sender.indexOfSelectedItem {
        case 0: days = 7
        case 1: days = 14
        case 2: days = 30
        default: days = 7
        }
        
        // Покажем "загрузку" путем очистки
        self.recordingInsightsView.showEmptyState(message: "Загрузка инсайтов...")
        fetchRecordingInsights(days: days)
    }
    
    /// Запрос инсайтов в фоне и обновление UI
    private func fetchRecordingInsights(days: Int) {
        // Мы используем тот же механизм off-main IPC вызовов, как и в других местах
        let ipcClient = self.ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try ipcClient.call(method: "get_recording_insights", params: ["days": days])
                if let resultDict = r["result"] as? [String: Any] {
                    let data = RecordingInsightsData(dict: resultDict)
                    DispatchQueue.main.async {
                        self?.recordingInsightsView.update(with: data)
                    }
                } else {
                    DispatchQueue.main.async {
                        self?.recordingInsightsView.showEmptyState()
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self?.recordingInsightsView.showEmptyState(message: "Ошибка загрузки инсайтов")
                }
            }
        }
    }
}
