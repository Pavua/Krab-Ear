import AppKit

@MainActor
public enum KrabEarTheme {
    
    public enum Colors {
        public static var windowBackground: NSColor { .windowBackgroundColor }
        public static var cardBackground: NSColor { .controlBackgroundColor }
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
        layer?.borderWidth = 1.0
        
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
    
    public override func updateLayer() {
        super.updateLayer()
        layer?.backgroundColor = KrabEarTheme.Colors.cardBackground.cgColor
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