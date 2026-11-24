/*
 PanelShellView — каркас главного окна: боковая панель разделов, шапка и область контента.

 Заменяет 7-сегментный NSSegmentedControl, который на узком окне обрезал вкладки
 (редизайн 2026-11, PR 1; спека — docs/design/REDESIGN_2026-11.md и DESIGN_SHELL_opus).

 Принципы:
  - Только представление: выбор раздела, `ui_last_tab`, IPC остаются в HistoryPanelController.
  - Самостоятельные NSView без зависимости от контроллера: раскладка тестируется
    без поднятия окна (PanelShellLayoutTests).
  - Режим боковой панели выбирается по ширине самой оболочки с гистерезисом,
    без анимации (мерцание NSVisualEffectView при анимации размера).
  - Выбранный раздел виден не только цветом: акцентная полоса + жирное начертание.
*/

import AppKit

// MARK: - Разделы панели

extension HistoryPanelController.PanelTab {
    /// Порядок = порядок NSTabViewItem и сочетаний ⌘1…⌘7.
    static let ordered: [HistoryPanelController.PanelTab] = [
        .dictation, .liveTranslation, .history, .conversation, .callAutomation, .diagnostics, .archive,
    ]

    /// Русское название раздела (также подпись VoiceOver и подсказка в режиме рейки).
    var title: String {
        switch self {
        case .dictation: return "Диктовка"
        case .liveTranslation: return "Live перевод"
        case .history: return "История"
        case .conversation: return "Разговор с AI"
        case .callAutomation: return "Автозвонки"
        case .diagnostics: return "Диагностика"
        case .archive: return "Архив"
        }
    }

    /// Имя SF Symbol (только ASCII; существование проверяет тест).
    var symbolName: String {
        switch self {
        case .dictation: return "mic.fill"
        case .liveTranslation: return "globe"
        case .history: return "clock.arrow.circlepath"
        case .conversation: return "bubble.left.and.bubble.right"
        case .callAutomation: return "phone.arrow.up.right"
        case .diagnostics: return "stethoscope"
        case .archive: return "archivebox"
        }
    }
}

// MARK: - Строка боковой панели

@MainActor
final class PanelSidebarRow: NSView {
    let tab: HistoryPanelController.PanelTab
    var onClick: ((HistoryPanelController.PanelTab) -> Void)?

    private let iconView = NSImageView()
    private let label = NSTextField(labelWithString: "")
    private let accentBar = NSView()
    private(set) var isSelected = false
    private(set) var isRail = false
    private var labelLeading: NSLayoutConstraint!
    private var iconLeading: NSLayoutConstraint!
    private var iconCenterX: NSLayoutConstraint!

    static let height: CGFloat = 36
    static let iconSize: CGFloat = 20

    init(tab: HistoryPanelController.PanelTab) {
        self.tab = tab
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.chipCornerRadius + 2
        layer?.cornerCurve = .continuous

        iconView.contentTintColor = KrabEarTheme.Colors.textPrimary
        iconView.translatesAutoresizingMaskIntoConstraints = false

        label.stringValue = tab.title
        label.font = KrabEarTheme.Typography.bodyLarge
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        label.translatesAutoresizingMaskIntoConstraints = false

        accentBar.wantsLayer = true
        accentBar.layer?.cornerRadius = 1.5
        accentBar.translatesAutoresizingMaskIntoConstraints = false
        accentBar.isHidden = true

        addSubview(accentBar)
        addSubview(iconView)
        addSubview(label)

        iconLeading = iconView.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.comfortable)
        iconCenterX = iconView.centerXAnchor.constraint(equalTo: centerXAnchor)
        iconCenterX.isActive = false
        labelLeading = label.leadingAnchor.constraint(equalTo: iconView.trailingAnchor, constant: 10)

        NSLayoutConstraint.activate([
            heightAnchor.constraint(equalToConstant: Self.height),
            accentBar.leadingAnchor.constraint(equalTo: leadingAnchor),
            accentBar.centerYAnchor.constraint(equalTo: centerYAnchor),
            accentBar.widthAnchor.constraint(equalToConstant: 3),
            accentBar.heightAnchor.constraint(equalToConstant: 20),
            iconLeading,
            iconView.centerYAnchor.constraint(equalTo: centerYAnchor),
            iconView.widthAnchor.constraint(equalToConstant: Self.iconSize),
            iconView.heightAnchor.constraint(equalToConstant: Self.iconSize),
            labelLeading,
            label.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])

        // VoiceOver: полное название и в режиме рейки; выбор озвучивается как «выбрано».
        setAccessibilityElement(true)
        setAccessibilityRole(.radioButton)
        setAccessibilityLabel(tab.title)
        toolTip = tab.title
        applyState()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    func setRail(_ rail: Bool) {
        guard rail != isRail else { return }
        isRail = rail
        label.isHidden = rail
        labelLeading.isActive = !rail
        iconLeading.isActive = !rail
        iconCenterX.isActive = rail
    }

    func setSelected(_ selected: Bool) {
        guard selected != isSelected else { return }
        isSelected = selected
        applyState()
        applyColors()
    }

    /// Панель в фокусе клавиатуры: у выбранной строки рамка системного цвета фокуса.
    /// (Системное кольцо AppKit рисует только у самого first responder — им является панель.)
    var showsKeyboardFocus = false {
        didSet {
            guard showsKeyboardFocus != oldValue else { return }
            applyColors()
        }
    }

    private func applyState() {
        // Не только цвет: полоса слева + жирное начертание у выбранного раздела.
        label.font = isSelected
            ? .systemFont(ofSize: KrabEarTheme.Typography.bodyLarge.pointSize, weight: .semibold)
            : KrabEarTheme.Typography.bodyLarge
        accentBar.isHidden = !isSelected
        // В режиме рейки подписи нет — выбор читается по полосе, подложке и жирной иконке.
        let config = NSImage.SymbolConfiguration(pointSize: isSelected ? 17 : 16, weight: isSelected ? .bold : .medium)
        iconView.image = NSImage(systemSymbolName: tab.symbolName, accessibilityDescription: nil)?
            .withSymbolConfiguration(config)
        setAccessibilitySelected(isSelected)
        needsDisplay = true
    }

    override func updateLayer() {
        applyColors()
    }

    override var wantsUpdateLayer: Bool { true }

    private func applyColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = isSelected
                ? NSColor.controlAccentColor.withAlphaComponent(0.18).cgColor
                : NSColor.clear.cgColor
            accentBar.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
            let focused = showsKeyboardFocus && isSelected
            layer?.borderWidth = focused ? 2 : 0
            layer?.borderColor = NSColor.keyboardFocusIndicatorColor.cgColor
        }
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    /// Клик по иконке/подписи — тоже клик по строке (подписи иначе забирают событие).
    override func hitTest(_ point: NSPoint) -> NSView? {
        frame.contains(point) ? self : nil
    }

    override func mouseDown(with event: NSEvent) {
        onClick?(tab)
    }

    override func accessibilityPerformPress() -> Bool {
        onClick?(tab)
        return true
    }
}

// MARK: - Боковая панель

@MainActor
final class PanelSidebarView: NSView {
    enum Mode { case full, rail }

    static let fullWidth: CGFloat = 184
    static let railWidth: CGFloat = 56

    private(set) var mode: Mode = .full
    private(set) var rows: [PanelSidebarRow] = []
    private(set) var selectedTab: HistoryPanelController.PanelTab = .dictation
    /// Вызывается ТОЛЬКО при клике пользователя; программный `select` не зовёт.
    var onSelect: ((HistoryPanelController.PanelTab) -> Void)?

    private let stack = NSStackView()
    private let separator = NSView()
    private(set) var widthConstraint: NSLayoutConstraint!
    private var stackTop: NSLayoutConstraint!

    func setTopInset(_ inset: CGFloat) { stackTop.constant = inset }

    init(topInset: CGFloat) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("Разделы")

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = KrabEarTheme.Metrics.tight
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)

        separator.wantsLayer = true
        separator.translatesAutoresizingMaskIntoConstraints = false
        addSubview(separator)

        for tab in HistoryPanelController.PanelTab.ordered {
            let row = PanelSidebarRow(tab: tab)
            row.onClick = { [weak self] t in
                guard let self else { return }
                self.select(t)
                self.onSelect?(t)
            }
            rows.append(row)
            stack.addArrangedSubview(row)
            // Явная ширина строки (урок #2240): NSStackView иначе центрирует по intrinsic.
            row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }

        widthConstraint = widthAnchor.constraint(equalToConstant: Self.fullWidth)
        stackTop = stack.topAnchor.constraint(equalTo: topAnchor, constant: topInset)
        NSLayoutConstraint.activate([
            widthConstraint,
            stackTop,
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard - 1),
            separator.topAnchor.constraint(equalTo: topAnchor),
            separator.bottomAnchor.constraint(equalTo: bottomAnchor),
            separator.trailingAnchor.constraint(equalTo: trailingAnchor),
            separator.widthAnchor.constraint(equalToConstant: 1),
        ])
        select(.dictation)
        updateSeparatorColor()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    /// Программный выбор: подсвечивает строку, `onSelect` НЕ вызывает (иначе петля с NSTabView).
    func select(_ tab: HistoryPanelController.PanelTab) {
        selectedTab = tab
        for row in rows { row.setSelected(row.tab == tab) }
    }

    func setMode(_ newMode: Mode) {
        guard newMode != mode else { return }
        mode = newMode
        widthConstraint.constant = (newMode == .full) ? Self.fullWidth : Self.railWidth
        for row in rows { row.setRail(newMode == .rail) }
    }

    /// Выбор режима по ширине оболочки с гистерезисом ±8 pt вокруг порога 820.
    static func mode(forShellWidth width: CGFloat, current: Mode) -> Mode {
        switch current {
        case .full: return width < 812 ? .rail : .full
        case .rail: return width >= 828 ? .full : .rail
        }
    }

    // MARK: Клавиатура: ↑/↓ в сфокусированной панели

    override var acceptsFirstResponder: Bool { true }

    override func becomeFirstResponder() -> Bool {
        let ok = super.becomeFirstResponder()
        if ok { setKeyboardFocus(true) }
        return ok
    }

    override func resignFirstResponder() -> Bool {
        let ok = super.resignFirstResponder()
        if ok { setKeyboardFocus(false) }
        return ok
    }

    /// Видимый фокус: кольцо вокруг выбранной строки, пока панель в фокусе клавиатуры.
    private(set) var hasKeyboardFocus = false

    private func setKeyboardFocus(_ focused: Bool) {
        hasKeyboardFocus = focused
        for row in rows { row.showsKeyboardFocus = focused }
    }

    override func keyDown(with event: NSEvent) {
        let tabs = HistoryPanelController.PanelTab.ordered
        guard let idx = tabs.firstIndex(of: selectedTab) else { return super.keyDown(with: event) }
        switch event.keyCode {
        case 125 where idx + 1 < tabs.count:  // вниз
            select(tabs[idx + 1]); onSelect?(tabs[idx + 1])
        case 126 where idx > 0:  // вверх
            select(tabs[idx - 1]); onSelect?(tabs[idx - 1])
        default:
            super.keyDown(with: event)
        }
    }

    private func updateSeparatorColor() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            separator.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
        }
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateSeparatorColor()
    }
}

// MARK: - Шапка

@MainActor
final class PanelHeaderView: NSView {
    static let height: CGFloat = 48

    private let titleLabel = NSTextField(labelWithString: "")
    private(set) var statusSlot: NSView?

    var title: String {
        get { titleLabel.stringValue }
        set {
            titleLabel.stringValue = newValue
            titleLabel.setAccessibilityLabel(newValue)
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        titleLabel.font = KrabEarTheme.Typography.pageTitle
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.maximumNumberOfLines = 1
        titleLabel.translatesAutoresizingMaskIntoConstraints = false
        titleLabel.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        addSubview(titleLabel)
        NSLayoutConstraint.activate([
            heightAnchor.constraint(equalToConstant: Self.height),
            titleLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.relaxed),
            titleLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    /// Встраивает статус-полосу (GlobalStatusBar) справа: не шире 60 % шапки,
    /// не заезжает на заголовок. Высота полосы (28) задаётся ею самой.
    func installStatusView(_ view: NSView) {
        statusSlot?.removeFromSuperview()
        statusSlot = view
        view.translatesAutoresizingMaskIntoConstraints = false
        addSubview(view)
        let preferred = view.widthAnchor.constraint(equalToConstant: 320)
        preferred.priority = .defaultHigh
        NSLayoutConstraint.activate([
            view.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.relaxed),
            view.centerYAnchor.constraint(equalTo: centerYAnchor),
            view.leadingAnchor.constraint(greaterThanOrEqualTo: titleLabel.trailingAnchor, constant: KrabEarTheme.Metrics.comfortable),
            view.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, multiplier: 0.6),
            preferred,
        ])
    }
}

// MARK: - Оболочка

@MainActor
final class PanelShellView: NSView {
    /// Отступ сверху под заголовок окна (окно использует fullSizeContentView). Берётся из
    /// safeAreaInsets (высота заголовка/тулбара); без окна — стандартные 28 pt + зазор 12.
    static let fallbackTitlebarHeight: CGFloat = 28
    static let titlebarGap: CGFloat = 12
    static var titlebarInset: CGFloat { fallbackTitlebarHeight + titlebarGap }

    /// Текущий отступ сверху (обновляется в layout по safeAreaInsets).
    private(set) var topInset: CGFloat = PanelShellView.titlebarInset
    private var headerTop: NSLayoutConstraint!

    let sidebar: PanelSidebarView
    let header = PanelHeaderView()
    /// Сюда контроллер кладёт NSTabView (контент разделов).
    let contentHost = NSView()

    override init(frame frameRect: NSRect) {
        sidebar = PanelSidebarView(topInset: Self.titlebarInset)
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        contentHost.translatesAutoresizingMaskIntoConstraints = false
        addSubview(sidebar)
        addSubview(header)
        addSubview(contentHost)

        let gap = KrabEarTheme.Metrics.comfortable
        headerTop = header.topAnchor.constraint(equalTo: topAnchor, constant: Self.titlebarInset - 8)
        NSLayoutConstraint.activate([
            sidebar.topAnchor.constraint(equalTo: topAnchor),
            sidebar.leadingAnchor.constraint(equalTo: leadingAnchor),
            sidebar.bottomAnchor.constraint(equalTo: bottomAnchor),

            headerTop,
            header.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor),
            header.trailingAnchor.constraint(equalTo: trailingAnchor),

            contentHost.topAnchor.constraint(equalTo: header.bottomAnchor, constant: KrabEarTheme.Metrics.standard),
            contentHost.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: gap),
            contentHost.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -gap),
            contentHost.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -gap),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) не поддерживается") }

    override func layout() {
        // Константы меняем до super.layout(): это тот же проход раскладки, без петли.
        let titlebar = safeAreaInsets.top > 0 ? safeAreaInsets.top : Self.fallbackTitlebarHeight
        let inset = titlebar + Self.titlebarGap
        if abs(inset - topInset) > 0.5 {
            topInset = inset
            headerTop.constant = inset - 8
            sidebar.setTopInset(inset)
        }
        super.layout()
    }

    /// Пересчёт режима панели при смене размера (без анимации).
    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        applyMode(forWidth: newSize.width)
    }

    func applyMode(forWidth width: CGFloat) {
        guard width > 0 else { return }
        sidebar.setMode(PanelSidebarView.mode(forShellWidth: width, current: sidebar.mode))
    }

    /// Ручной переключатель (⌃⌘S): инвертирует режим независимо от ширины до следующего ресайза.
    func toggleSidebarMode() {
        sidebar.setMode(sidebar.mode == .full ? .rail : .full)
    }
}
