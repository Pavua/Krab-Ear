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
        public static let cardCornerRadius: CGFloat = 10.0
    }
    
    public static func applyTheme(to window: NSWindow) {
        window.backgroundColor = Colors.windowBackground
        window.titlebarAppearsTransparent = true
        window.styleMask.insert(.fullSizeContentView)
    }
    
    public static func styleCheckbox(_ checkbox: NSButton) {
        checkbox.setButtonType(.switch)
        checkbox.font = Typography.controlLabel
    }
}

@MainActor
public class ThemeCardView: NSView {
    
    public let contentStackView = NSStackView()
    private let titleLabel = NSTextField(labelWithString: "")
    private let containerStack = NSStackView()
    
    public var title: String = "" {
        didSet {
            titleLabel.stringValue = title
            titleLabel.isHidden = title.isEmpty
        }
    }
    
    public override var wantsUpdateLayer: Bool { true }
    
    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setup()
    }
    
    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }
    
    private func setup() {
        wantsLayer = true
        layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        layer?.borderWidth = 0.5
        layer?.masksToBounds = true  // Prevent rendering artifacts on scroll/hover

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

        // Liquid Glass: add vibrancy effect behind content
        let effectView = NSVisualEffectView()
        effectView.material = .hudWindow
        effectView.blendingMode = .behindWindow
        effectView.state = .active
        effectView.wantsLayer = true
        effectView.layer?.cornerRadius = KrabEarTheme.Metrics.cardCornerRadius
        effectView.layer?.masksToBounds = true  // Prevent ghost traces from alpha blending
        effectView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(effectView, positioned: .below, relativeTo: containerStack)

        NSLayoutConstraint.activate([
            effectView.topAnchor.constraint(equalTo: topAnchor),
            effectView.leadingAnchor.constraint(equalTo: leadingAnchor),
            effectView.trailingAnchor.constraint(equalTo: trailingAnchor),
            effectView.bottomAnchor.constraint(equalTo: bottomAnchor),
            containerStack.topAnchor.constraint(equalTo: topAnchor, constant: KrabEarTheme.Metrics.cardPadding),
            containerStack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.cardPadding),
            containerStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.cardPadding),
            containerStack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -KrabEarTheme.Metrics.cardPadding)
        ])
    }
    
    public override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = NSColor.controlBackgroundColor.withAlphaComponent(0.3).cgColor
        layer?.borderColor = KrabEarTheme.Colors.separator.cgColor
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

        headerStack.orientation = .horizontal
        headerStack.spacing = 4
        headerStack.alignment = .centerY
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

        if animated {
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = 0.2
                ctx.allowsImplicitAnimation = true
                self.headerSeparator.isHidden = !expanded
                self.contentStackView.isHidden = !expanded
                self.contentStackView.superview?.layoutSubtreeIfNeeded()
            })
        } else {
            headerSeparator.isHidden = !expanded
            contentStackView.isHidden = !expanded
        }

        UserDefaults.standard.set(expanded, forKey: "CollapsibleSection_\(sectionId)")
    }
}