/*
 DictationHeroView — главный экран «Диктовка» (редизайн 2026-11, PR 2; спека DESIGN_SHELL, Opus).

 Состав:
  - RecordButtonView: большая кнопка записи; состояние ВСЕГДА = цвет + иконка + слово.
  - DictationHeroView: кнопка, строка состояния под ней, сводка (горячая клавиша, профиль,
    авто-вставка, движок STT) в 2 колонки (1 колонка на узком контенте).
  - RecentDictationsView: последние результаты без вложенного скролла + пустое/загрузка/ошибка.

 Принципы:
  - Только представление: данные приходят снимком (`DictationHeroSnapshot`,
    `RecentDictationsView.State`), IPC и логика записи остаются в контроллере → тестируется офлайн.
  - Перерисовка только при изменении снимка (Equatable), без своих таймеров и пульсаций.
  - Текст на заливке кнопки выбирается по контрасту (белый/чёрный), а не по цвету акцента:
    у пользователя может стоять жёлтый или графитовый акцент.
*/

import AppKit

// MARK: - Снимок состояния

/// Фаза записи для экрана «Диктовка» (из `get_recording_state`).
enum DictationPhase: Equatable {
    case idle
    case recording(durationSec: Double)
    case finalizing
    /// Два подряд неуспешных опроса бэкенда.
    case offline
}

struct DictationHeroSnapshot: Equatable {
    var phase: DictationPhase
    var hotkey: String
    var profile: String
    var autoPaste: Bool
    var engine: String

    static let placeholder = DictationHeroSnapshot(phase: .idle, hotkey: "⌥", profile: "—", autoPaste: false, engine: "—")
}

// MARK: - Кнопка записи

@MainActor
final class RecordButtonView: NSView {
    static let preferredSize = NSSize(width: 240, height: 56)

    var onPress: (() -> Void)?
    private(set) var phase: DictationPhase = .idle

    private let iconView = NSImageView()
    private let titleLabel = NSTextField(labelWithString: "")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        layer?.cornerCurve = .continuous
        focusRingType = .default

        iconView.translatesAutoresizingMaskIntoConstraints = false
        titleLabel.translatesAutoresizingMaskIntoConstraints = false
        // 17 pt semibold — «крупный текст» по WCAG (порог контраста 3:1 на цветной заливке).
        titleLabel.font = .systemFont(ofSize: KrabEarTheme.Typography.display.pointSize, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.maximumNumberOfLines = 1

        let content = NSStackView(views: [iconView, titleLabel])
        content.orientation = .horizontal
        content.spacing = 10
        content.alignment = .centerY
        content.translatesAutoresizingMaskIntoConstraints = false
        addSubview(content)
        NSLayoutConstraint.activate([
            heightAnchor.constraint(equalToConstant: Self.preferredSize.height),
            iconView.widthAnchor.constraint(equalToConstant: 22),
            iconView.heightAnchor.constraint(equalToConstant: 22),
            content.centerXAnchor.constraint(equalTo: centerXAnchor),
            content.centerYAnchor.constraint(equalTo: centerYAnchor),
            content.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: KrabEarTheme.Metrics.relaxed),
            content.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -KrabEarTheme.Metrics.relaxed),
        ])

        setAccessibilityElement(true)
        setAccessibilityRole(.button)
        apply(phase: .idle)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    // MARK: Состояние

    /// Текст кнопки по фазе (виден всегда — состояние не передаётся только цветом).
    static func title(for phase: DictationPhase) -> String {
        switch phase {
        case .recording(let sec): return "Остановить · \(HistoryPanelController.formatDuration(sec))"
        case .idle, .finalizing, .offline: return "Начать диктовку"
        }
    }

    static func accessibilityTitle(for phase: DictationPhase) -> String {
        switch phase {
        case .recording: return "Остановить диктовку"
        case .idle, .finalizing, .offline: return "Начать диктовку"
        }
    }

    static func symbolName(for phase: DictationPhase) -> String {
        switch phase {
        case .idle, .finalizing: return "mic.fill"
        case .recording: return "stop.fill"
        case .offline: return "exclamationmark.triangle.fill"
        }
    }

    var titleText: String { titleLabel.stringValue }

    func apply(phase newPhase: DictationPhase) {
        phase = newPhase
        titleLabel.stringValue = Self.title(for: newPhase)
        let config = NSImage.SymbolConfiguration(pointSize: 18, weight: .semibold)
        iconView.image = NSImage(systemSymbolName: Self.symbolName(for: newPhase), accessibilityDescription: nil)?
            .withSymbolConfiguration(config)
        setAccessibilityLabel(Self.accessibilityTitle(for: newPhase))
        applyColors()
    }

    /// Заливка и цвет текста; текст — белый или чёрный, что контрастнее к заливке.
    private func applyColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let fill: NSColor?
            switch phase {
            case .idle, .finalizing: fill = .controlAccentColor
            case .recording: fill = .systemRed
            case .offline: fill = nil
            }
            if let base = fill {
                // Заливка затемняется, пока белый текст не даст ≥ 4.5:1 (гейт Opus #2250:
                // 17 pt — не «крупный» по WCAG, а системные синий/красный дают 3.4–4.0).
                let fill = Self.fillForReadableWhiteText(base)
                layer?.backgroundColor = fill.cgColor
                layer?.borderWidth = 0
                let text = Self.bestTextColor(on: fill)
                titleLabel.textColor = text
                iconView.contentTintColor = text
            } else {
                layer?.backgroundColor = NSColor.clear.cgColor
                layer?.borderWidth = 2
                layer?.borderColor = NSColor.systemOrange.cgColor
                titleLabel.textColor = KrabEarTheme.Colors.textPrimary
                iconView.contentTintColor = .systemOrange
            }
        }
    }

    /// Требуемый контраст текста кнопки: 4.5:1 (17 pt не дотягивает до «крупного» WCAG).
    static let minimumTextContrast: CGFloat = 4.5

    /// Относительная яркость WCAG.
    static func luminance(_ color: NSColor) -> CGFloat? {
        guard let c = color.usingColorSpace(.sRGB) else { return nil }
        func lin(_ v: CGFloat) -> CGFloat { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        return 0.2126 * lin(c.redComponent) + 0.7152 * lin(c.greenComponent) + 0.0722 * lin(c.blueComponent)
    }

    /// Контраст белого текста к заливке.
    static func whiteContrast(on fill: NSColor) -> CGFloat {
        guard let l = luminance(fill) else { return 1 }
        return 1.05 / (l + 0.05)
    }

    /// Затемняет заливку шагами по 5 % (смешение с чёрным), пока белый текст не даст ≥ 4.5:1.
    /// Красная кнопка с белым текстом — привычный сигнал записи, поэтому темним, а не
    /// меняем текст на чёрный. Светлые акценты (жёлтый) уходят в чёрный текст в bestTextColor.
    static func fillForReadableWhiteText(_ fill: NSColor) -> NSColor {
        guard let base = fill.usingColorSpace(.sRGB) else { return fill }
        var fraction: CGFloat = 0
        var result = base
        while whiteContrast(on: result) < minimumTextContrast, fraction < 0.6 {
            fraction += 0.05
            result = base.blended(withFraction: fraction, of: .black) ?? base
        }
        // Светлый акцент, которому не помогло затемнение до 60 % (жёлтый): оставляем как есть,
        // текст станет чёрным в bestTextColor.
        return whiteContrast(on: result) >= minimumTextContrast ? result : base
    }

    /// Цвет текста на (уже подобранной) заливке: белый при ≥ 4.5:1, иначе — что контрастнее.
    static func bestTextColor(on fill: NSColor) -> NSColor {
        guard let l = luminance(fill) else { return .white }
        let white = 1.05 / (l + 0.05)
        let black = (l + 0.05) / 0.05
        if white >= minimumTextContrast { return .white }
        return white >= black ? .white : .black
    }

    /// Для тестов контраста: фактические заливка и цвет текста отрисованной кнопки.
    var renderedFillColor: NSColor? { layer?.backgroundColor.flatMap { NSColor(cgColor: $0) } }
    var renderedTextColor: NSColor? { titleLabel.textColor }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    // MARK: Ввод

    override var acceptsFirstResponder: Bool { true }
    override func mouseDown(with event: NSEvent) { onPress?() }

    override func keyDown(with event: NSEvent) {
        // Пробел / Return — как нажатие кнопки.
        if event.keyCode == 49 || event.keyCode == 36 { onPress?() } else { super.keyDown(with: event) }
    }

    override func accessibilityPerformPress() -> Bool {
        onPress?()
        return true
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        frame.contains(point) ? self : nil
    }

    override var focusRingMaskBounds: NSRect { bounds }

    override func drawFocusRingMask() {
        NSBezierPath(roundedRect: bounds, xRadius: KrabEarTheme.Metrics.cardCornerRadius,
                     yRadius: KrabEarTheme.Metrics.cardCornerRadius).fill()
    }
}

// MARK: - Карточка «Диктовка»

@MainActor
final class DictationHeroView: NSView {
    /// Ширина контента, ниже которой сводка — в одну колонку, а кнопка — во всю ширину.
    static let narrowThreshold: CGFloat = 600

    let recordButton = RecordButtonView()
    var onRecordPress: (() -> Void)? {
        get { recordButton.onPress }
        set { recordButton.onPress = newValue }
    }
    /// Кнопка «Диагностика» в состоянии «Нет связи».
    var onOpenDiagnostics: (() -> Void)?

    private(set) var snapshot = DictationHeroSnapshot.placeholder
    private(set) var isNarrow = false

    private let card = ThemeCardView()
    private let stack = NSStackView()
    private let statusRow = NSStackView()
    private let statusLabel = NSTextField(labelWithString: "")
    private let spinner = NSProgressIndicator()
    private let diagnosticsButton = NSButton(title: "Диагностика", target: nil, action: nil)
    private let summaryGrid = NSGridView()
    private var buttonPreferredWidth: NSLayoutConstraint!
    private var buttonFullWidth: NSLayoutConstraint!
    private var valueLabels: [String: NSTextField] = [:]
    private var titleLabels: [String: NSTextField] = [:]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        card.translatesAutoresizingMaskIntoConstraints = false
        addSubview(card)

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.comfortable
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.contentStackView.addArrangedSubview(stack)

        statusLabel.font = KrabEarTheme.Typography.captionLarge
        statusLabel.textColor = KrabEarTheme.Colors.textSecondary
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(250), for: .horizontal)
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        spinner.translatesAutoresizingMaskIntoConstraints = false
        diagnosticsButton.bezelStyle = .rounded
        diagnosticsButton.controlSize = .large
        diagnosticsButton.font = KrabEarTheme.Typography.captionLarge
        diagnosticsButton.target = self
        diagnosticsButton.action = #selector(onDiagnosticsPressed)
        diagnosticsButton.isHidden = true
        statusRow.orientation = .horizontal
        statusRow.alignment = .centerY
        statusRow.spacing = KrabEarTheme.Metrics.standard
        statusRow.setViews([spinner, statusLabel, diagnosticsButton], in: .leading)

        summaryGrid.translatesAutoresizingMaskIntoConstraints = false
        // Сводка компактна: пары стоят рядом, а не растягиваются на всю карточку.
        // Порядок приоритетов: растяжение стеком (250) < hugging сетки (300) < CR подписей
        // (400) < windowSizeStayPut (500). С .required сетка сжимала ярлыки до «Горячая кл…»
        // (рендер #2255); ниже 250 — растягивалась на всю карточку.
        summaryGrid.setContentHuggingPriority(NSLayoutConstraint.Priority(300), for: .horizontal)
        summaryGrid.rowSpacing = KrabEarTheme.Metrics.standard
        summaryGrid.columnSpacing = KrabEarTheme.Metrics.relaxed

        stack.addArrangedSubview(recordButton)
        stack.addArrangedSubview(statusRow)
        stack.addArrangedSubview(summaryGrid)

        buttonPreferredWidth = recordButton.widthAnchor.constraint(equalToConstant: RecordButtonView.preferredSize.width)
        buttonFullWidth = recordButton.widthAnchor.constraint(equalTo: stack.widthAnchor)
        NSLayoutConstraint.activate([
            card.topAnchor.constraint(equalTo: topAnchor),
            card.leadingAnchor.constraint(equalTo: leadingAnchor),
            card.trailingAnchor.constraint(equalTo: trailingAnchor),
            card.bottomAnchor.constraint(equalTo: bottomAnchor),
            // Явная ширина детей (урок #2240): NSStackView центрирует по intrinsic size.
            stack.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor),
            statusRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            summaryGrid.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor),
            card.contentStackView.widthAnchor.constraint(equalTo: card.widthAnchor, constant: -2 * KrabEarTheme.Metrics.cardPadding),
            buttonPreferredWidth,
        ])
        rebuildSummary()
        apply(snapshot, force: true)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    // MARK: Данные

    /// Применяет снимок; без изменений — ничего не перерисовывает.
    func apply(_ new: DictationHeroSnapshot, force: Bool = false) {
        guard force || new != snapshot else { return }
        let previous = snapshot.phase
        snapshot = new
        recordButton.apply(phase: new.phase)

        switch new.phase {
        case .idle:
            statusLabel.stringValue = "Готов · или нажмите \(new.hotkey)"
        case .recording:
            statusLabel.stringValue = "Идёт запись"
        case .finalizing:
            statusLabel.stringValue = "Распознаю предыдущую запись…"
        case .offline:
            statusLabel.stringValue = "Нет связи с движком распознавания"
        }
        // Скрытый спиннер не держит место в строке (иначе текст съезжает вправо).
        if case .finalizing = new.phase {
            spinner.isHidden = false
            spinner.startAnimation(nil)
        } else {
            spinner.stopAnimation(nil)
            spinner.isHidden = true
        }
        diagnosticsButton.isHidden = new.phase != .offline

        valueLabels["hotkey"]?.stringValue = new.hotkey
        valueLabels["profile"]?.stringValue = new.profile
        valueLabels["autoPaste"]?.stringValue = new.autoPaste ? "вкл" : "выкл"
        valueLabels["engine"]?.stringValue = new.engine

        if !force { announceIfNeeded(from: previous, to: new.phase) }
    }

    /// Озвучка VoiceOver один раз на переход фаз (тики длительности не озвучиваются).
    private func announceIfNeeded(from old: DictationPhase, to new: DictationPhase) {
        let text: String?
        switch (old, new) {
        case (.recording, .recording): text = nil
        case (_, .recording): text = "Идёт запись"
        case (.recording, _): text = "Запись остановлена"
        case (_, .offline) where old != .offline: text = "Нет связи с движком распознавания"
        default: text = nil
        }
        guard let text else { return }
        lastAnnouncement = text
        NSAccessibility.post(
            element: window ?? self,
            notification: .announcementRequested,
            userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue]
        )
    }

    /// Для тестов: последняя озвученная фраза.
    private(set) var lastAnnouncement: String?

    var statusText: String { statusLabel.stringValue }
    var isDiagnosticsButtonVisible: Bool { !diagnosticsButton.isHidden }
    var summaryColumnCount: Int { summaryGrid.numberOfColumns / 2 }

    @objc private func onDiagnosticsPressed() { onOpenDiagnostics?() }

    // MARK: Раскладка

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        let narrow = newSize.width < Self.narrowThreshold
        guard narrow != isNarrow else { return }
        isNarrow = narrow
        buttonPreferredWidth.isActive = !narrow
        buttonFullWidth.isActive = narrow
        rebuildSummary()
    }

    /// Сводка: пары «ярлык / значение»; 2 пары в строке на широком контенте, 1 — на узком.
    private func rebuildSummary() {
        while summaryGrid.numberOfRows > 0 { summaryGrid.removeRow(at: 0) }
        while summaryGrid.numberOfColumns > 0 { summaryGrid.removeColumn(at: 0) }
        let pairs: [(key: String, title: String)] = [
            ("hotkey", "Горячая клавиша"), ("profile", "Профиль"),
            ("autoPaste", "Авто-вставка"), ("engine", "Движок STT"),
        ]
        let perRow = isNarrow ? 1 : 2
        var row: [NSView] = []
        for pair in pairs {
            // Переиспользуем, как и значения: NSGridView.removeRow не убирает вью из иерархии,
            // и новые ярлыки на каждом переключении узко/широко оставляли старые «призраками».
            let title = titleLabels[pair.key] ?? NSTextField(labelWithString: pair.title)
            titleLabels[pair.key] = title
            title.font = KrabEarTheme.Typography.captionLarge
            title.textColor = KrabEarTheme.Colors.textSecondary
            title.lineBreakMode = .byTruncatingTail
            // Сетка не стек — общее правило подписей её не видит. Значение (движок, профиль)
            // приходит из настроек любой длины; с сопротивлением 750 сетка распирала окно
            // до 3955 pt (стресс-проход). 400 (< 500, > hugging сетки 300): окно не растёт,
            // обычный текст не сокращается, длинный — с многоточием.
            title.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(400), for: .horizontal)
            let value = valueLabels[pair.key] ?? {
                let v = NSTextField(labelWithString: "—")
                v.font = KrabEarTheme.Typography.bodyLarge
                v.textColor = KrabEarTheme.Colors.textPrimary
                v.lineBreakMode = .byTruncatingTail
                v.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(400), for: .horizontal)
                return v
            }()
            valueLabels[pair.key] = value
            value.setAccessibilityLabel(pair.title)
            row.append(contentsOf: [title, value])
            if row.count == perRow * 2 {
                summaryGrid.addRow(with: row)
                row = []
            }
        }
        if !row.isEmpty { summaryGrid.addRow(with: row) }
        // Вью, не попавшие в текущие пары (ключ убран из списка), не висят в сетке.
        let live = Set(pairs.flatMap { [titleLabels[$0.key], valueLabels[$0.key]] }.compactMap { $0.map(ObjectIdentifier.init) })
        for v in summaryGrid.subviews where !live.contains(ObjectIdentifier(v)) { v.removeFromSuperview() }
        for col in 0..<summaryGrid.numberOfColumns {
            summaryGrid.column(at: col).xPlacement = .leading
            // Между парами «ярлык/значение» — заметный зазор, чтобы пары не слипались.
            if col > 0, col % 2 == 0 { summaryGrid.column(at: col).leadingPadding = KrabEarTheme.Metrics.spacious }
        }
        summaryGrid.rowAlignment = .firstBaseline
    }
}

// MARK: - Последние результаты

@MainActor
final class RecentDictationsView: NSView {
    struct Item: Equatable {
        let id: String
        let time: String
        let text: String
    }

    enum State: Equatable {
        case loading
        case empty(title: String, hint: String)
        case error(message: String)
        case items([Item])
    }

    static let maxRows = 5
    /// Задержка показа индикатора загрузки: быстрые ответы не мигают спиннером.
    static let loadingDelay: TimeInterval = 0.15

    var onCopy: ((String) -> Void)?
    var onOpenHistory: (() -> Void)?
    var onRetry: (() -> Void)?

    private(set) var state: State?
    private let card = ThemeCardView()
    private let body = NSStackView()
    private let openButton = NSButton(title: "Вся история", target: nil, action: nil)
    private var pendingLoading: DispatchWorkItem?
    private(set) var rowViews: [RecentDictationRow] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        card.translatesAutoresizingMaskIntoConstraints = false
        addSubview(card)

        let title = NSTextField(labelWithString: "Последние результаты")
        title.font = .systemFont(ofSize: KrabEarTheme.Typography.display.pointSize, weight: .semibold)
        title.textColor = KrabEarTheme.Colors.textPrimary
        title.setAccessibilityRole(.staticText)

        openButton.bezelStyle = .rounded
        openButton.controlSize = .large
        openButton.font = KrabEarTheme.Typography.captionLarge
        openButton.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: nil)
        openButton.imagePosition = .imageTrailing
        openButton.target = self
        openButton.action = #selector(onOpenPressed)
        openButton.setAccessibilityLabel("Открыть всю историю")

        let header = NSStackView()
        header.orientation = .horizontal
        header.alignment = .centerY
        header.setViews([title], in: .leading)
        header.setViews([openButton], in: .trailing)
        header.translatesAutoresizingMaskIntoConstraints = false

        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = KrabEarTheme.Metrics.standard
        body.translatesAutoresizingMaskIntoConstraints = false

        card.contentStackView.addArrangedSubview(header)
        card.contentStackView.addArrangedSubview(body)
        NSLayoutConstraint.activate([
            card.topAnchor.constraint(equalTo: topAnchor),
            card.leadingAnchor.constraint(equalTo: leadingAnchor),
            card.trailingAnchor.constraint(equalTo: trailingAnchor),
            card.bottomAnchor.constraint(equalTo: bottomAnchor),
            card.contentStackView.widthAnchor.constraint(equalTo: card.widthAnchor, constant: -2 * KrabEarTheme.Metrics.cardPadding),
            header.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor),
            body.widthAnchor.constraint(equalTo: card.contentStackView.widthAnchor),
        ])
        apply(.loading)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    var isOpenHistoryEnabled: Bool { openButton.isEnabled }

    /// Применяет состояние; повтор того же состояния ничего не перерисовывает.
    func apply(_ new: State) {
        guard new != state else { return }
        state = new
        pendingLoading?.cancel()
        pendingLoading = nil
        clearBody()
        switch new {
        case .loading:
            openButton.isEnabled = false
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.state == .loading else { return }
                self.showMessage(icon: nil, title: "Загружаю последние записи…", hint: nil, spinner: true)
            }
            pendingLoading = work
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.loadingDelay, execute: work)
        case .empty(let title, let hint):
            openButton.isEnabled = true
            showMessage(icon: "mic", title: title, hint: hint, spinner: false)
        case .error(let message):
            openButton.isEnabled = true
            showMessage(icon: "exclamationmark.triangle", title: message, hint: nil, spinner: false)
            let retry = NSButton(title: "Повторить", target: self, action: #selector(onRetryPressed))
            retry.bezelStyle = .rounded
            retry.controlSize = .large
            retry.font = KrabEarTheme.Typography.captionLarge
            body.addArrangedSubview(retry)
        case .items(let items):
            openButton.isEnabled = true
            for item in items.prefix(Self.maxRows) {
                let row = RecentDictationRow(item: item)
                row.onCopy = { [weak self] text in self?.onCopy?(text) }
                body.addArrangedSubview(row)
                row.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
                rowViews.append(row)
            }
        }
    }

    private func clearBody() {
        for v in body.arrangedSubviews {
            body.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        rowViews.removeAll()
    }

    /// Текст сообщения (пусто/загрузка/ошибка) — для тестов и VoiceOver.
    private(set) var messageText: String?
    var hasRetryButton: Bool { body.arrangedSubviews.contains { ($0 as? NSButton)?.title == "Повторить" } }

    private func showMessage(icon: String?, title: String, hint: String?, spinner: Bool) {
        messageText = title
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = KrabEarTheme.Metrics.comfortable
        if spinner {
            let s = NSProgressIndicator()
            s.style = .spinning
            s.controlSize = .small
            s.startAnimation(nil)
            row.addArrangedSubview(s)
        }
        if let icon, let image = NSImage(systemSymbolName: icon, accessibilityDescription: nil) {
            let iv = NSImageView(image: image.withSymbolConfiguration(.init(pointSize: 28, weight: .regular)) ?? image)
            iv.contentTintColor = KrabEarTheme.Colors.textSecondary
            row.addArrangedSubview(iv)
        }
        let texts = NSStackView()
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 2
        let t = NSTextField(wrappingLabelWithString: title)
        t.font = KrabEarTheme.Typography.bodyLarge
        t.textColor = KrabEarTheme.Colors.textPrimary
        texts.addArrangedSubview(t)
        if let hint {
            let h = NSTextField(wrappingLabelWithString: hint)
            h.font = KrabEarTheme.Typography.captionLarge
            h.textColor = KrabEarTheme.Colors.textSecondary
            texts.addArrangedSubview(h)
        }
        row.addArrangedSubview(texts)
        body.addArrangedSubview(row)
        row.widthAnchor.constraint(lessThanOrEqualTo: body.widthAnchor).isActive = true
    }

    @objc private func onOpenPressed() { onOpenHistory?() }
    @objc private func onRetryPressed() { onRetry?() }

    /// «HH:mm» из ISO-метки истории («2026-11-24T12:41:05…»); иначе — как есть.
    nonisolated static func shortTime(from ts: String) -> String {
        if let t = ts.firstIndex(of: "T") {
            let rest = ts[ts.index(after: t)...]
            if rest.count >= 5 { return String(rest.prefix(5)) }
        }
        if ts.count >= 16, ts[ts.index(ts.startIndex, offsetBy: 10)] == " " {
            return String(ts.dropFirst(11).prefix(5))
        }
        return ts
    }
}

@MainActor
final class RecentDictationRow: NSView {
    let item: RecentDictationsView.Item
    var onCopy: ((String) -> Void)?
    let textLabel: NSTextField
    let copyButton = NSButton()

    init(item: RecentDictationsView.Item) {
        self.item = item
        textLabel = NSTextField(wrappingLabelWithString: item.text)
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false

        let time = NSTextField(labelWithString: item.time)
        time.font = KrabEarTheme.Typography.captionLarge.tabular()
        time.textColor = KrabEarTheme.Colors.textSecondary
        time.translatesAutoresizingMaskIntoConstraints = false

        textLabel.font = KrabEarTheme.Typography.bodyLarge
        textLabel.textColor = KrabEarTheme.Colors.textPrimary
        textLabel.maximumNumberOfLines = 2
        textLabel.lineBreakMode = .byWordWrapping
        textLabel.cell?.truncatesLastVisibleLine = true
        textLabel.translatesAutoresizingMaskIntoConstraints = false
        textLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        copyButton.bezelStyle = .regularSquare
        copyButton.isBordered = false
        copyButton.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: "Копировать")?
            .withSymbolConfiguration(.init(pointSize: 15, weight: .regular))
        copyButton.contentTintColor = KrabEarTheme.Colors.textSecondary
        copyButton.setAccessibilityLabel("Копировать")
        copyButton.toolTip = "Копировать"
        copyButton.target = self
        copyButton.action = #selector(onCopyPressed)
        copyButton.translatesAutoresizingMaskIntoConstraints = false

        addSubview(time)
        addSubview(textLabel)
        addSubview(copyButton)
        NSLayoutConstraint.activate([
            time.leadingAnchor.constraint(equalTo: leadingAnchor),
            time.firstBaselineAnchor.constraint(equalTo: textLabel.firstBaselineAnchor),
            time.widthAnchor.constraint(equalToConstant: 48),
            textLabel.leadingAnchor.constraint(equalTo: time.trailingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            textLabel.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            textLabel.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4),
            textLabel.trailingAnchor.constraint(equalTo: copyButton.leadingAnchor, constant: -KrabEarTheme.Metrics.standard),
            copyButton.trailingAnchor.constraint(equalTo: trailingAnchor),
            copyButton.centerYAnchor.constraint(equalTo: centerYAnchor),
            copyButton.widthAnchor.constraint(equalToConstant: 32),
            copyButton.heightAnchor.constraint(equalToConstant: 32),
            heightAnchor.constraint(greaterThanOrEqualToConstant: 40),
        ])
        setAccessibilityElement(false)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    override func layout() {
        // Перенос по словам в Auto Layout: ширина строки известна только здесь.
        let w = textLabel.frame.width
        if w > 0, abs(textLabel.preferredMaxLayoutWidth - w) > 0.5 {
            textLabel.preferredMaxLayoutWidth = w
        }
        super.layout()
    }

    @objc private func onCopyPressed() { onCopy?(item.text) }
}
