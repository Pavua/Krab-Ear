import AppKit

@MainActor
public enum KrabEarTheme {
    
    public enum Colors {
        public static var windowBackground: NSColor { .windowBackgroundColor }
        /// Liquid Glass: semi-transparent card background with vibrancy
        public static var cardBackground: NSColor {
            NSColor.controlBackgroundColor.withAlphaComponent(0.65)
        }
        public static var accent: NSColor { .controlAccentColor }
        
        public static var textPrimary: NSColor { .labelColor }
        public static var textSecondary: NSColor { .secondaryLabelColor }
        public static var textTertiary: NSColor { .tertiaryLabelColor }
        
        public static var separator: NSColor { .separatorColor }
        
        public static var success: NSColor { .systemGreen }
        public static var warning: NSColor { .systemOrange }
        public static var error: NSColor { .systemRed }
    }
    
    public enum Typography {
        public static var sectionTitle: NSFont { .boldSystemFont(ofSize: 13) }
        public static var controlLabel: NSFont { .systemFont(ofSize: 12) }
        public static var smallCaption: NSFont { .systemFont(ofSize: 10) }
        public static var monospaced: NSFont { .monospacedDigitSystemFont(ofSize: 12, weight: .regular) }
    }
    
    public enum Metrics {
        public static let sectionSpacing: CGFloat = 16.0
        public static let itemSpacing: CGFloat = 8.0
        public static let cardPadding: CGFloat = 12.0
        public static let cardCornerRadius: CGFloat = 12.0
        /// Концентрический радиус для внутренних элементов (scroll views, text views внутри карточек).
        /// Apple's rule: inner radius = outer radius - padding/3.
        public static let innerCornerRadius: CGFloat = 8.0
    }
    
    public static func applyTheme(to window: NSWindow) {
        window.backgroundColor = .clear
        window.titlebarAppearsTransparent = true
        window.styleMask.insert(.fullSizeContentView)
        window.isOpaque = false

        // Add NSVisualEffectView as window background for full Liquid Glass
        if let contentView = window.contentView {
            let existingEffect = contentView.subviews.first(where: {
                $0 is NSVisualEffectView && $0.identifier == NSUserInterfaceItemIdentifier("krabEarWindowBg")
            })
            if existingEffect == nil {
                let bgEffect = NSVisualEffectView()
                // .popover — тот же material что у карточек (ThemeCardView).
                // Единая material для окна и карточек создаёт unified translucent
                // look: карточки выглядят как часть окна, а не парящие сверху.
                // .sidebar был слишком opaque — карточки выглядели чужеродно.
                bgEffect.material = .popover
                bgEffect.blendingMode = .behindWindow
                bgEffect.state = .active
                bgEffect.identifier = NSUserInterfaceItemIdentifier("krabEarWindowBg")
                bgEffect.translatesAutoresizingMaskIntoConstraints = false
                contentView.addSubview(bgEffect, positioned: .below, relativeTo: contentView.subviews.first)
                NSLayoutConstraint.activate([
                    bgEffect.topAnchor.constraint(equalTo: contentView.topAnchor),
                    bgEffect.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
                    bgEffect.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
                    bgEffect.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
                ])
            }
        }
    }
    
    public static func styleCheckbox(_ checkbox: NSButton) {
        checkbox.setButtonType(.switch)
        checkbox.font = Typography.controlLabel
    }
}

@MainActor
public class ThemeCardView: NSVisualEffectView {

    public let contentStackView = NSStackView()
    private let titleLabel = NSTextField(labelWithString: "")
    private let containerStack = NSStackView()

    public var title: String = "" {
        didSet {
            titleLabel.stringValue = title
            titleLabel.isHidden = title.isEmpty
        }
    }

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }

    private func setup() {
        // Liquid Glass: настоящий frosted glass эффект.
        //
        // .popover — средний вариант между .menu (слишком прозрачный
        // = карточки выглядят чужеродно на .sidebar фоне окна) и
        // .sidebar (сольётся с фоном = невидимая карточка).
        // Даёт чистый frosted glass look ближе к window .sidebar фону.
        //
        // .behindWindow (а не .withinWindow!) — КЛЮЧЕВОЙ момент:
        // окно уже имеет .sidebar + .behindWindow фон. Если карточка
        // тоже использует .behindWindow — macOS умеет обрабатывать
        // вложенные behindWindow: верхний слой «пробивает» нижний и
        // блюрит рабочий стол напрямую со своим материалом, создавая
        // эффект парящего стекла.
        // .withinWindow бы блюрил УЖЕ заблюренный фон окна = мутный пластик.
        material = .popover
        blendingMode = .behindWindow
        state = .active
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        // cornerCurve = .continuous — Apple's «яблочные» плавные углы
        // (squircle, а не простой rounded rect)
        layer?.cornerCurve = .continuous
        layer?.borderWidth = 1.0
        // Чуть более заметный edge — карточка visible как distinct element
        // на фоне window same material. Alpha 0.18 даёт subtle но чёткую
        // границу (user feedback: карточки должны быть «более выделяющимися»).
        layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
        layer?.masksToBounds = true
        // Note: drop shadow не применён сознательно — при masksToBounds=true
        // (нужно для rounded corners clipping) layer shadow не рендерится.
        // Для shadow нужен wrapper container view — избыточно для subtle edge.

        titleLabel.font = KrabEarTheme.Typography.sectionTitle
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.isEditable = false
        titleLabel.isBordered = false
        titleLabel.drawsBackground = false
        titleLabel.isHidden = true

        contentStackView.orientation = .vertical
        contentStackView.spacing = KrabEarTheme.Metrics.itemSpacing
        contentStackView.alignment = .leading

        containerStack.orientation = .vertical
        containerStack.spacing = KrabEarTheme.Metrics.itemSpacing
        containerStack.alignment = .leading
        containerStack.translatesAutoresizingMaskIntoConstraints = false

        containerStack.addArrangedSubview(titleLabel)
        containerStack.addArrangedSubview(contentStackView)
        addSubview(containerStack)

        NSLayoutConstraint.activate([
            containerStack.topAnchor.constraint(equalTo: topAnchor, constant: KrabEarTheme.Metrics.cardPadding),
            containerStack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.cardPadding),
            containerStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.cardPadding),
            containerStack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -KrabEarTheme.Metrics.cardPadding)
        ])
    }

    public override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        layer?.borderColor = NSColor.separatorColor.cgColor
    }
}

@MainActor
public class ThemePrimaryButton: NSButton {
    
    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }
    
    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }
    
    private func setup() {
        bezelStyle = .push
        isBordered = true
        bezelColor = KrabEarTheme.Colors.accent
        font = KrabEarTheme.Typography.controlLabel
    }
}

@MainActor
public class ThemeSecondaryButton: NSButton {
    
    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }
    
    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }
    
    private func setup() {
        bezelStyle = .push
        isBordered = true
        font = KrabEarTheme.Typography.controlLabel
    }
}

@MainActor
public class CollapsibleSectionView: NSView {

    public let sectionId: String
    public let disclosureButton = NSButton(frame: .zero)
    public let titleLabel = NSTextField(labelWithString: "")
    public let headerStack = NSStackView()
    public let contentStackView = NSStackView()
    private let containerStack = NSStackView()
    private let headerSeparator = NSBox()

    public private(set) var isExpanded: Bool

    public init(sectionId: String, title: String, isExpanded: Bool = true) {
        self.sectionId = sectionId
        self.isExpanded = isExpanded
        super.init(frame: .zero)

        let key = "CollapsibleSection_\(sectionId)"
        if UserDefaults.standard.object(forKey: key) != nil {
            self.isExpanded = UserDefaults.standard.bool(forKey: key)
        }

        setup(title: title)
    }

    public required init?(coder: NSCoder) {
        fatalError("init(coder:) not supported")
    }

    private func setup(title: String) {
        translatesAutoresizingMaskIntoConstraints = false

        disclosureButton.setButtonType(.pushOnPushOff)
        disclosureButton.bezelStyle = .disclosure
        disclosureButton.title = ""
        disclosureButton.state = isExpanded ? .on : .off
        disclosureButton.target = self
        disclosureButton.action = #selector(onToggle)

        titleLabel.stringValue = title
        titleLabel.font = KrabEarTheme.Typography.sectionTitle
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.isEditable = false
        titleLabel.isBordered = false
        titleLabel.drawsBackground = false

        // Make the entire header row clickable (bigger hit target)
        let headerClickButton = NSButton(frame: .zero)
        headerClickButton.title = ""
        headerClickButton.isBordered = false
        headerClickButton.isTransparent = true
        headerClickButton.target = self
        headerClickButton.action = #selector(onToggle)
        headerClickButton.translatesAutoresizingMaskIntoConstraints = false

        disclosureButton.controlSize = .regular

        headerStack.orientation = .horizontal
        headerStack.spacing = 4
        headerStack.alignment = .centerY
        headerStack.edgeInsets = NSEdgeInsets(top: 0, left: 4, bottom: 0, right: 0)
        headerStack.addArrangedSubview(disclosureButton)
        headerStack.addArrangedSubview(titleLabel)
        headerStack.addArrangedSubview(NSView()) // spacer — makes full width clickable

        // Overlay invisible button on entire header for bigger click target
        headerStack.addSubview(headerClickButton)
        NSLayoutConstraint.activate([
            headerClickButton.topAnchor.constraint(equalTo: headerStack.topAnchor),
            headerClickButton.leadingAnchor.constraint(equalTo: headerStack.leadingAnchor),
            headerClickButton.trailingAnchor.constraint(equalTo: headerStack.trailingAnchor),
            headerClickButton.bottomAnchor.constraint(equalTo: headerStack.bottomAnchor),
            headerStack.heightAnchor.constraint(greaterThanOrEqualToConstant: 28),
        ])

        // Subtle separator between header and content (visible only when expanded)
        headerSeparator.boxType = .separator
        headerSeparator.translatesAutoresizingMaskIntoConstraints = false
        headerSeparator.isHidden = !isExpanded

        contentStackView.orientation = .vertical
        contentStackView.spacing = KrabEarTheme.Metrics.itemSpacing
        contentStackView.alignment = .leading
        contentStackView.isHidden = !isExpanded

        containerStack.orientation = .vertical
        containerStack.spacing = KrabEarTheme.Metrics.itemSpacing
        containerStack.alignment = .leading
        containerStack.translatesAutoresizingMaskIntoConstraints = false

        containerStack.addArrangedSubview(headerStack)
        containerStack.addArrangedSubview(headerSeparator)
        containerStack.addArrangedSubview(contentStackView)
        addSubview(containerStack)

        NSLayoutConstraint.activate([
            containerStack.topAnchor.constraint(equalTo: topAnchor),
            containerStack.leadingAnchor.constraint(equalTo: leadingAnchor),
            containerStack.trailingAnchor.constraint(equalTo: trailingAnchor),
            containerStack.bottomAnchor.constraint(equalTo: bottomAnchor),
            headerSeparator.widthAnchor.constraint(equalTo: containerStack.widthAnchor),
        ])
    }

    @objc private func onToggle() {
        let newState = !isExpanded
        setExpanded(newState, animated: true)
    }

    public func setExpanded(_ expanded: Bool, animated: Bool) {
        self.isExpanded = expanded
        disclosureButton.state = expanded ? .on : .off

        // Capture the enclosing scroll view PATH before layout changes —
        // chain walks up to find parent NSScrollView (outer tab scroll).
        // Needed чтобы scroll bar и document size пересчитались после expand/collapse.
        let outerScrollView = self.enclosingScrollView

        let applyChanges: () -> Void = { [weak self] in
            guard let self else { return }
            self.headerSeparator.isHidden = !expanded
            self.contentStackView.isHidden = !expanded
            // Force full layout pass up to the window — without этого
            // NSScrollView не знает что document height изменился,
            // оставляя visual empty space или blocking scroll.
            self.window?.layoutIfNeeded()
            // Invalidate scroll tile — обновляет scrollbar и valid scroll range.
            if let scroll = outerScrollView {
                scroll.reflectScrolledClipView(scroll.contentView)
            }
        }

        if animated {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.2
                ctx.allowsImplicitAnimation = true
                applyChanges()
            }
        } else {
            applyChanges()
        }

        UserDefaults.standard.set(expanded, forKey: "CollapsibleSection_\(sectionId)")
    }
}

// MARK: - Unified Theme Extensions (Liquid Glass consistency)
// Эти extensions унифицируют применение стилей к NSControl'ам.
// Правило: после инициализации любой кнопки/текст-филда/scroll-вью
// вызывать соответствующий applyTheme* метод для однородного Liquid Glass вида.

@MainActor
public extension NSButton {
    /// Primary button: акцентный цвет, rounded bezel.
    /// Для главных действий (Старт/Стоп, Submit, Применить).
    func applyThemePrimary() {
        self.bezelStyle = .rounded
        self.controlSize = .regular
        self.font = KrabEarTheme.Typography.controlLabel
        self.bezelColor = KrabEarTheme.Colors.accent
    }

    /// Secondary button: стандартный вид, rounded bezel, без акцента.
    /// Для вторичных действий (Копировать, Экспорт, Настройки).
    func applyThemeSecondary() {
        self.bezelStyle = .rounded
        self.controlSize = .regular
        self.font = KrabEarTheme.Typography.controlLabel
        self.bezelColor = nil
    }

    /// Checkbox style: switch type, тематический шрифт.
    func applyThemeCheckbox() {
        self.setButtonType(.switch)
        self.font = KrabEarTheme.Typography.controlLabel
    }
}

@MainActor
public extension NSTextField {
    /// Input field style: rounded bezel, transparent background.
    /// drawsBackground = false позволяет фону карточки просвечивать,
    /// оставляя только рамку и focus ring — Liquid Glass-friendly.
    func applyThemeInput() {
        self.isBordered = true
        self.bezelStyle = .roundedBezel
        self.controlSize = .regular
        self.font = KrabEarTheme.Typography.controlLabel
        self.textColor = KrabEarTheme.Colors.textPrimary
        self.drawsBackground = false
    }
}

@MainActor
public extension NSScrollView {
    /// Inner scroll style: концентрический радиус (8pt) для scrolls внутри cards (12pt radius).
    /// Apple's design rule: inner radius = outer radius - padding/3.
    /// Transparent background чтобы не перекрывать frosted glass карточки.
    func applyThemeInnerScroll() {
        self.wantsLayer = true
        self.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        self.layer?.masksToBounds = true
        self.drawsBackground = false
        self.borderType = .noBorder
    }
}