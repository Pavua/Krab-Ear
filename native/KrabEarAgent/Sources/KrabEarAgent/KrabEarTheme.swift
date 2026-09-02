import AppKit
import CoreText
import QuartzCore

@MainActor
public enum KrabEarTheme {
    
    /// Unified color system designed by Gemini 3.1 Pro (2026-04-16 v2).
    /// 9 semantic tokens. Migration from legacy hardcoded alphas to dynamic border + consistent cardBackground.
    public enum Colors {
        // MARK: - Backgrounds
        /// Window root (clear поверх NSVisualEffectView)
        public static var windowBackground: NSColor { .clear }

        /// Liquid Glass card: 0.5 alpha (было 0.65 — Gemini снизил для better vibrancy)
        public static var cardBackground: NSColor {
            NSColor.controlBackgroundColor.withAlphaComponent(0.5)
        }
        // MARK: - Interactive
        public static var accent: NSColor { .controlAccentColor }

        // MARK: - Typography colors
        public static var textPrimary: NSColor { .labelColor }
        public static var textSecondary: NSColor { .secondaryLabelColor }
        /// Disabled / muted text (replaces legacy textTertiary)
        public static var textDisabled: NSColor { .tertiaryLabelColor }

        // MARK: - Borders & Dividers
        /// Dynamic border: white 0.15 в dark mode, black 0.10 в light mode.
        /// Unifies legacy alphas 0.18 (card), 0.12 (overlay), 0.3 (grid) в один semantic token.
        public static var border: NSColor {
            NSColor(name: nil) { appearance in
                appearance.name == .darkAqua
                    ? NSColor.white.withAlphaComponent(0.15)
                    : NSColor.black.withAlphaComponent(0.10)
            }
        }
        /// Keep separator alias for backward-compat в нескольких local call sites.
        public static var separator: NSColor { border }

        // MARK: - Status (Diagnostics)
        public static var success: NSColor { .systemGreen }
        public static var error: NSColor { .systemRed }

        // MARK: - Overlays
        public static var overlayShadow: NSColor { NSColor.black.withAlphaComponent(0.25) }

        // MARK: - Legacy aliases (will be purged after full migration)
        /// @deprecated Use textDisabled
        public static var textTertiary: NSColor { textDisabled }
        /// @deprecated Warning not used; if нужен — вернуть .systemOrange
        public static var warning: NSColor { .systemOrange }
    }
    
    /// Unified typography system designed by Gemini 3.1 Pro (2026-04-16).
    /// 6 tokens покрывают все UI слои. См. docs/FONT_SYSTEM.md для migration notes.
    public enum Typography {
        /// Primary RealtimeOverlay transcription text (17pt regular)
        public static var display: NSFont { .systemFont(ofSize: 17, weight: .regular) }

        /// Все headers — панели, секции, группы настроек (13pt semibold).
        /// Изменение vs. legacy: bold → semibold (нативнее в macOS 13+).
        public static var sectionTitle: NSFont { .systemFont(ofSize: 13, weight: .semibold) }

        /// Body text, inputs, buttons, controls (13pt regular — macOS стандарт).
        public static var body: NSFont { .systemFont(ofSize: 13, weight: .regular) }

        /// Secondary captions, dates, filters (11pt regular).
        public static var caption: NSFont { .systemFont(ofSize: 11, weight: .regular) }

        /// Accented captions, badges, статусы (11pt medium).
        public static var captionMedium: NSFont { .systemFont(ofSize: 11, weight: .medium) }

        /// Logs, диагностика, raw data output (11pt monospaced).
        /// Для бейджей с цифрами используй `captionMedium.tabular()` вместо отдельного токена.
        public static var monospace: NSFont { .monospacedSystemFont(ofSize: 11, weight: .regular) }
    }

    // MARK: - Interaction States (Gemini 3.1 Pro 2026-04-16 v3)

    /// Interaction state tokens для Liquid Glass. Alpha-композитинг поверх base
    /// колора сохраняет матовый эффект (vs. hardcoded HEX которые убили бы glass).
    public enum Interaction {
        /// Hover highlight — белый с 10% поверх фона (dark mode)
        public static let hoverOverlayAlpha: CGFloat = 0.10
        /// Pressed scale — микро-уменьшение 2% (0.98x)
        public static let pressedScale: CGFloat = 0.98
        /// Pressed overlay — чёрный 15% поверх фона
        public static let pressedOverlayAlpha: CGFloat = 0.15
        /// Disabled — общая прозрачность элемента 40%
        public static let disabledOpacity: CGFloat = 0.40
        /// Transparent buttons (headerClickButton) — едва заметный белый 5% на hover
        public static let transparentHoverAlpha: CGFloat = 0.05
    }

    // MARK: - Motion (Gemini 3.1 Pro v3 — unified from scattered 0.2/0.25/0.3/0.7 durations)

    /// Motion tokens: durations + easing curves + centralized animate() wrapper с
    /// automatic Reduce Motion support.
    public enum Motion {
        public enum Duration {
            /// Hover, press, checkbox toggle (0.15s)
            public static let micro: TimeInterval = 0.15
            /// Expand/collapse, tab switch (0.25s)
            public static let short: TimeInterval = 0.25
            /// Overlay show, modals (0.40s)
            public static let standard: TimeInterval = 0.40
            /// Pulse, attention loops (0.70s)
            public static let long: TimeInterval = 0.70
        }

        public enum Easing {
            public static var easeOut: CAMediaTimingFunction { CAMediaTimingFunction(name: .easeOut) }
            public static var easeIn: CAMediaTimingFunction { CAMediaTimingFunction(name: .easeIn) }
            public static var easeInOut: CAMediaTimingFunction { CAMediaTimingFunction(name: .easeInEaseOut) }
            public static var linear: CAMediaTimingFunction { CAMediaTimingFunction(name: .linear) }
        }

        /// Centralized animation wrapper с automatic Reduce Motion support.
        /// Если пользователь enabled "Reduce Motion" в System Settings → duration = 0.
        public static func animate(
            duration: TimeInterval,
            easing: CAMediaTimingFunction,
            animations: @escaping () -> Void
        ) {
            let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            let actualDuration = reduceMotion ? 0.0 : duration
            NSAnimationContext.runAnimationGroup { context in
                context.duration = actualDuration
                context.timingFunction = easing
                context.allowsImplicitAnimation = true
                animations()
            }
        }
    }

    // MARK: - Elevation (shadow hierarchy — Gemini 3.1 Pro v3)

    /// Elevation helpers — apply shadow spec на CALayer.
    /// Note: parent layer must NOT have `masksToBounds = true` (blocks shadows).
    public enum Elevation {
        /// Card-level shadow (subtle).
        public static func applyCard(to layer: CALayer) {
            layer.shadowColor = NSColor.black.cgColor
            layer.shadowOpacity = 0.15
            layer.shadowOffset = CGSize(width: 0, height: -2)
            layer.shadowRadius = 6
        }

        /// Main overlay (RealtimeOverlay panel) — отрыв от всех окон.
        public static func applyOverlay(to layer: CALayer) {
            layer.shadowColor = NSColor.black.cgColor
            layer.shadowOpacity = 0.30
            layer.shadowOffset = CGSize(width: 0, height: -12)
            layer.shadowRadius = 32
        }
    }
}

extension NSFont {
    /// Tabular figures — не-прыгающие цифры для бейджей, счётчиков, monospace-digit контекстов.
    /// Применяется к любому existing font: `Typography.captionMedium.tabular()`.
    public func tabular() -> NSFont {
        let descriptor = fontDescriptor.addingAttributes([
            .featureSettings: [[
                NSFontDescriptor.FeatureKey.typeIdentifier: kNumberSpacingType,
                NSFontDescriptor.FeatureKey.selectorIdentifier: kMonospacedNumbersSelector
            ]]
        ])
        return NSFont(descriptor: descriptor, size: pointSize) ?? self
    }
}

@MainActor
public extension KrabEarTheme {
    
    /// Unified spacing & sizing system designed by Gemini 3.1 Pro (2026-04-16 v2).
    /// 4-pt grid aligned; 10pt/6pt legacy values migrate к standard/tight.
    enum Metrics {
        // MARK: - Spacing (4-pt grid)
        /// Minor offsets, disclosure padding (4pt).
        public static let tight: CGFloat = 4.0
        /// Item/inner padding — dominant default (8pt, bывший itemSpacing).
        public static let standard: CGFloat = 8.0
        /// Card padding, mid-level inset (12pt, бывший cardPadding).
        public static let comfortable: CGFloat = 12.0
        /// Window root padding, external margins (24pt, был sectionSpacing=16 — shifted к 24pt Gemini recommendation).
        public static let spacious: CGFloat = 24.0

        // MARK: - Radii
        public static let cardCornerRadius: CGFloat = 12.0
        /// Концентрический радиус для внутренних элементов (scroll views, text views внутри карточек).
        /// Apple's rule: inner radius = outer radius - padding/3.
        public static let innerCornerRadius: CGFloat = 8.0

        // MARK: - Sizing
        /// Standard NSControl.ControlSize.regular height (24pt, hardcoded 9x до migration).
        public static let controlHeight: CGFloat = 24.0

        // MARK: - Legacy aliases (will be purged after full migration)
        public static let sectionSpacing: CGFloat = spacious
        public static let itemSpacing: CGFloat = standard
        public static let cardPadding: CGFloat = comfortable
    }
    
    static func applyTheme(to window: NSWindow) {
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
    
    static func styleCheckbox(_ checkbox: NSButton) {
        checkbox.setButtonType(.switch)
        checkbox.font = Typography.body
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
        // Dynamic semantic border token — adapts to light/dark mode automatically.
        // Replaced hardcoded white@0.18 (was overridden by viewDidChangeEffectiveAppearance
        // with NSColor.separatorColor, causing Figma↔Swift drift). Now uses a single
        // dynamic color that matches the Figma `border` token in both appearances.
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
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
        // KrabEarTheme.Colors.border is a dynamic NSColor provider — it resolves
        // correctly for both dark/light mode without a manual override. Reassign to
        // re-trigger CGColor resolution in the new appearance context.
        layer?.borderColor = KrabEarTheme.Colors.border.cgColor
    }
}

/// Base class for all Krab Ear themed buttons.
/// Manages standard pointer interaction lifecycle (hover, press, disable) via `NSTrackingArea`.
/// Applies subtle overlays and scaling, delegating all animation timing to `KrabEarTheme.Motion.animate`.
/// Reduce Motion accessibility preferences are intrinsically respected by the Motion wrapper.
@MainActor
open class ThemeButton: NSButton {

    private var trackingArea: NSTrackingArea?
    private var isHovered: Bool = false
    private var isPressed: Bool = false
    private var isFocused: Bool = false

    /// Opt-in for transparent styles (e.g., toolbar rows / headerClickButton).
    /// When true (or when the button is borderless), hover uses the softer
    /// `Interaction.transparentHoverAlpha` (5%) instead of the standard 10% tint.
    open var isTransparentStyle: Bool = false

    private let overlayLayer = CALayer()
    private let focusRingLayer = CALayer()

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setupInteraction()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupInteraction()
    }

    private func setupInteraction() {
        wantsLayer = true

        // DEFENSIVE LAYER ORDERING:
        // overlayLayer at index 0 (bottom). focusRingLayer at index 1.
        // Both stay UNDER the native AppKit title/image rendering, acting strictly
        // as decorative backing elements that cannot hide the label.
        overlayLayer.backgroundColor = NSColor.clear.cgColor
        layer?.insertSublayer(overlayLayer, at: 0)

        // Focus ring: 2pt ring + subtle outer glow. Colors applied in layout()
        // so they track effective appearance changes (light/dark mode).
        focusRingLayer.borderWidth = 2.0
        focusRingLayer.shadowOpacity = 0.25
        focusRingLayer.shadowRadius = 2.0
        focusRingLayer.shadowOffset = .zero
        focusRingLayer.opacity = 0.0
        layer?.insertSublayer(focusRingLayer, at: 1)

        // Apply initial state silently without triggering entry animations
        applyInteractionState(suppressAnimation: true)
    }

    // Suppress the default macOS focus ring so our custom focusRingLayer handles
    // focus visualization completely. AppKit's ring would otherwise composite on
    // top and fight our layer ordering + tint logic.
    open override func drawFocusRingMask() {}
    open override var focusRingMaskBounds: NSRect { .zero }

    open override func layout() {
        super.layout()

        // Expand overlay to fill bounds
        overlayLayer.frame = bounds

        // Match base corner radius dynamically. Fallback to our inner glass token if unset.
        let currentRadius = layer?.cornerRadius ?? 0
        let radius = currentRadius > 0 ? currentRadius : KrabEarTheme.Metrics.innerCornerRadius
        overlayLayer.cornerRadius = radius

        // Keep focus ring geometry synchronized.
        // Update colors dynamically here to safely support light/dark appearance changes.
        focusRingLayer.frame = bounds
        focusRingLayer.cornerRadius = radius
        focusRingLayer.borderColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.85).cgColor
        focusRingLayer.shadowColor = KrabEarTheme.Colors.accent.cgColor
    }

    open override func updateTrackingAreas() {
        super.updateTrackingAreas()

        if let existing = trackingArea {
            removeTrackingArea(existing)
        }

        let options: NSTrackingArea.Options = [
            .mouseEnteredAndExited,
            .activeInKeyWindow,
            .inVisibleRect,
            .assumeInside
        ]

        let newArea = NSTrackingArea(rect: bounds, options: options, owner: self, userInfo: nil)
        addTrackingArea(newArea)
        trackingArea = newArea
    }

    open override var isEnabled: Bool {
        didSet {
            applyInteractionState(suppressAnimation: false)
        }
    }

    open override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        needsDisplay = true
        updateTrackingAreas()
    }

    open override func mouseEntered(with event: NSEvent) {
        isHovered = true
        applyInteractionState()
    }

    open override func mouseExited(with event: NSEvent) {
        isHovered = false
        applyInteractionState()
    }

    open override func mouseDown(with event: NSEvent) {
        isPressed = true
        applyInteractionState()

        // Pass to AppKit's standard tracking loop to handle action triggers correctly.
        // AppKit blocks here (handling drag-out/drag-back) until mouseUp concludes.
        super.mouseDown(with: event)

        isPressed = false
        applyInteractionState()
    }

    // MARK: - Focus state

    // We rely on standard AppKit first-responder transitions instead of overriding
    // keyDown/keyUp for Space/Return: NSButton's super.keyDown enters a blocking
    // event-tracking loop that swallows keyUp, making manual press-state tracking
    // for keyboard activation unreliable. AppKit handles the title cell dimming for us.
    open override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted {
            isFocused = true
            applyInteractionState()
        }
        return accepted
    }

    open override func resignFirstResponder() -> Bool {
        let resigned = super.resignFirstResponder()
        if resigned {
            isFocused = false
            applyInteractionState()
        }
        return resigned
    }

    /// Computes a scale transform anchored to the center of the bounds.
    /// This helper prevents origin-jumping layout artifacts natively present in AppKit when
    /// modifying `layer.anchorPoint` directly on auto-layout backed views.
    private func scaleTransform(scale: CGFloat) -> CATransform3D {
        let cx = bounds.midX
        let cy = bounds.midY
        var t = CATransform3DIdentity
        t = CATransform3DTranslate(t, cx, cy, 0)
        t = CATransform3DScale(t, scale, scale, 1.0)
        t = CATransform3DTranslate(t, -cx, -cy, 0)
        return t
    }

    /// Applies hover/pressed/focus styling.
    /// Note: Reduce Motion fallback is safely handled inside the `Motion.animate` wrapper.
    private func applyInteractionState(suppressAnimation: Bool = false) {
        guard isEnabled else {
            let apply = {
                self.alphaValue = KrabEarTheme.Interaction.disabledOpacity
                self.layer?.transform = CATransform3DIdentity
                self.overlayLayer.backgroundColor = NSColor.clear.cgColor
                // Disabled buttons never show the focus ring, even if they somehow
                // remain first responder during a state change.
                self.focusRingLayer.opacity = 0.0
            }
            if suppressAnimation {
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                apply()
                CATransaction.commit()
            } else {
                KrabEarTheme.Motion.animate(
                    duration: KrabEarTheme.Motion.Duration.micro,
                    easing: KrabEarTheme.Motion.Easing.easeOut,
                    animations: apply
                )
            }
            return
        }

        let apply = {
            self.alphaValue = 1.0

            if self.isPressed {
                self.layer?.transform = self.scaleTransform(scale: KrabEarTheme.Interaction.pressedScale)
                self.overlayLayer.backgroundColor = NSColor.black.withAlphaComponent(KrabEarTheme.Interaction.pressedOverlayAlpha).cgColor
            } else if self.isHovered {
                self.layer?.transform = CATransform3DIdentity
                // Transparent/borderless buttons (e.g., header rows, toolbar hit targets)
                // use the softer 5% hover tint so they don't flash aggressively over cards.
                let useTransparent = self.isTransparentStyle || !self.isBordered
                let hoverAlpha = useTransparent
                    ? KrabEarTheme.Interaction.transparentHoverAlpha
                    : KrabEarTheme.Interaction.hoverOverlayAlpha
                self.overlayLayer.backgroundColor = NSColor.white.withAlphaComponent(hoverAlpha).cgColor
            } else {
                self.layer?.transform = CATransform3DIdentity
                self.overlayLayer.backgroundColor = NSColor.clear.cgColor
            }

            // Focus ring visibility — respect per-button focusRingType intent so
            // explicit `.none` opt-outs (decorative buttons) still work.
            if self.isFocused && self.focusRingType != .none {
                self.focusRingLayer.opacity = 1.0
            } else {
                self.focusRingLayer.opacity = 0.0
            }
        }

        if suppressAnimation {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            apply()
            CATransaction.commit()
        } else {
            KrabEarTheme.Motion.animate(
                duration: KrabEarTheme.Motion.Duration.micro,
                easing: KrabEarTheme.Motion.Easing.easeOut,
                animations: apply
            )
        }
    }
}

@MainActor
public class ThemePrimaryButton: ThemeButton {

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
        font = KrabEarTheme.Typography.body
    }
}

@MainActor
public class ThemeSecondaryButton: ThemeButton {

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
        font = KrabEarTheme.Typography.body
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
    /// Хранилище состояния раскрытия; `.standard` сохраняет production-поведение.
    private let userDefaults: UserDefaults

    // MARK: - Hover state (Gemini design 2026-04-26 microinteraction)

    /// Hover tint backdrop — visible только при mouse enter в headerStack.
    /// Делает collapsible header feel interactive — отвечает на pointer.
    private let hoverBackdrop = NSView()
    private var headerTrackingArea: NSTrackingArea?

    public private(set) var isExpanded: Bool

    public init(
        sectionId: String,
        title: String,
        isExpanded: Bool = true,
        iconSymbol: String? = nil,
        userDefaults: UserDefaults = .standard
    ) {
        self.sectionId = sectionId
        self.isExpanded = isExpanded
        self.userDefaults = userDefaults
        super.init(frame: .zero)

        let key = "CollapsibleSection_\(sectionId)"
        if userDefaults.object(forKey: key) != nil {
            self.isExpanded = userDefaults.bool(forKey: key)
        }

        setup(title: title, iconSymbol: iconSymbol)
    }

    public required init?(coder: NSCoder) {
        fatalError("init(coder:) not supported")
    }

    private func setup(title: String, iconSymbol: String?) {
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
        headerStack.spacing = KrabEarTheme.Metrics.tight
        headerStack.alignment = .centerY
        // Explicit .fill — spacer (line ниже) relies on .fill чтобы titleLabel остался слева.
        // Если .distribution defaults ever change in AppKit — spacer layout сломается без этой строки.
        headerStack.distribution = .fill
        headerStack.edgeInsets = NSEdgeInsets(top: 0, left: KrabEarTheme.Metrics.tight, bottom: 0, right: 0)
        headerStack.wantsLayer = true

        // Hover backdrop — sits behind header content, fades in on mouseEnter.
        // Subtle 4% white tint => feels interactive at AA contrast level.
        hoverBackdrop.wantsLayer = true
        hoverBackdrop.layer?.cornerRadius = 6
        hoverBackdrop.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.04).cgColor
        hoverBackdrop.alphaValue = 0
        hoverBackdrop.translatesAutoresizingMaskIntoConstraints = false
        headerStack.addSubview(hoverBackdrop)
        NSLayoutConstraint.activate([
            hoverBackdrop.topAnchor.constraint(equalTo: headerStack.topAnchor, constant: -2),
            hoverBackdrop.leadingAnchor.constraint(equalTo: headerStack.leadingAnchor, constant: -2),
            hoverBackdrop.trailingAnchor.constraint(equalTo: headerStack.trailingAnchor, constant: 2),
            hoverBackdrop.bottomAnchor.constraint(equalTo: headerStack.bottomAnchor, constant: 2),
        ])

        headerStack.addArrangedSubview(disclosureButton)
        if let iconSymbol = iconSymbol, let image = NSImage(systemSymbolName: iconSymbol, accessibilityDescription: nil) {
            let imageView = NSImageView(image: image)
            imageView.contentTintColor = KrabEarTheme.Colors.textSecondary
            imageView.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
            headerStack.addArrangedSubview(imageView)
        }
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

    /// Уведомление о смене состояния секции.
    ///
    /// Нужно секциям, которым дорого наполняться заранее: «Все настройки»
    /// строит около 260 строк и делает это при первом раскрытии, а не при
    /// каждой сборке панели.
    public var onExpandedChange: ((Bool) -> Void)?

    public func setExpanded(_ expanded: Bool, animated: Bool) {
        self.isExpanded = expanded
        disclosureButton.state = expanded ? .on : .off
        onExpandedChange?(expanded)

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
            // Spring-like timing function: easeOut с лёгким overshoot — collapse/expand
            // выглядит "natural", как Apple System Settings 16. Honors Reduce Motion.
            let reducedMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = reducedMotion ? 0.001 : 0.28
                ctx.allowsImplicitAnimation = true
                ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0.0, 1.0) // ease-out cubic
                applyChanges()
            }
        } else {
            applyChanges()
        }

        userDefaults.set(expanded, forKey: "CollapsibleSection_\(sectionId)")
    }

    // MARK: - Hover tracking (Gemini design 2026-04-26)

    public override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = headerTrackingArea {
            headerStack.removeTrackingArea(existing)
        }
        let opts: NSTrackingArea.Options = [.activeAlways, .mouseEnteredAndExited, .inVisibleRect]
        let area = NSTrackingArea(rect: headerStack.bounds, options: opts, owner: self, userInfo: nil)
        headerTrackingArea = area
        headerStack.addTrackingArea(area)
    }

    public override func mouseEntered(with event: NSEvent) {
        guard event.trackingArea == headerTrackingArea else { return super.mouseEntered(with: event) }
        animateHover(visible: true)
    }

    public override func mouseExited(with event: NSEvent) {
        guard event.trackingArea == headerTrackingArea else { return super.mouseExited(with: event) }
        animateHover(visible: false)
    }

    private func animateHover(visible: Bool) {
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            hoverBackdrop.alphaValue = visible ? 1.0 : 0.0
            return
        }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.15
            ctx.allowsImplicitAnimation = true
            hoverBackdrop.animator().alphaValue = visible ? 1.0 : 0.0
        }
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
        self.font = KrabEarTheme.Typography.body
        self.bezelColor = KrabEarTheme.Colors.accent
    }

    /// Secondary button: стандартный вид, rounded bezel, без акцента.
    /// Для вторичных действий (Копировать, Экспорт, Настройки).
    func applyThemeSecondary() {
        self.bezelStyle = .rounded
        self.controlSize = .regular
        self.font = KrabEarTheme.Typography.body
        self.bezelColor = nil
    }

    /// Checkbox style: switch type, тематический шрифт.
    func applyThemeCheckbox() {
        self.setButtonType(.switch)
        self.font = KrabEarTheme.Typography.body
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
        self.font = KrabEarTheme.Typography.body
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

// MARK: - History Custom Cell (Gemini Liquid Glass)

@MainActor
public class HistoryBadgeView: NSView {
    private let label = NSTextField(labelWithString: "")
    private let iconView = NSImageView()
    private let backgroundLayer = CALayer()

    public init(text: String, symbol: String? = nil, color: NSColor = KrabEarTheme.Colors.textSecondary) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.addSublayer(backgroundLayer)

        label.font = KrabEarTheme.Typography.captionMedium.tabular()
        label.textColor = color
        label.maximumNumberOfLines = 1
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false
        label.drawsBackground = false
        label.isBordered = false
        label.isEditable = false
        label.isSelectable = false

        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 4
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false

        if let symbol = symbol, let img = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            let config = NSImage.SymbolConfiguration(textStyle: .caption1, scale: .small)
            iconView.image = img.withSymbolConfiguration(config)
            iconView.contentTintColor = color
            iconView.translatesAutoresizingMaskIntoConstraints = false
            stack.addArrangedSubview(iconView)
        }
        
        stack.addArrangedSubview(label)

        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2)
        ])

        backgroundLayer.backgroundColor = color.withAlphaComponent(0.1).cgColor
        backgroundLayer.cornerRadius = 4 // Капсула
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    public override func layout() {
        super.layout()
        backgroundLayer.frame = bounds
        backgroundLayer.cornerRadius = bounds.height / 2
    }
}

@MainActor
public class HistoryItemCellView: NSTableCellView {
    public let transcriptLabel = NSTextField(wrappingLabelWithString: "")
    public let translationLabel = NSTextField(wrappingLabelWithString: "")
    public let metaStack = NSStackView()
    private let backgroundHighlight = CALayer()

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.addSublayer(backgroundHighlight)
        backgroundHighlight.backgroundColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.0).cgColor
        backgroundHighlight.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius

        transcriptLabel.font = KrabEarTheme.Typography.body
        transcriptLabel.textColor = KrabEarTheme.Colors.textPrimary
        transcriptLabel.maximumNumberOfLines = 2
        transcriptLabel.lineBreakMode = .byTruncatingTail
        transcriptLabel.drawsBackground = false
        transcriptLabel.isBordered = false
        transcriptLabel.isEditable = false
        transcriptLabel.isSelectable = false
        transcriptLabel.translatesAutoresizingMaskIntoConstraints = false

        translationLabel.font = NSFont(descriptor: KrabEarTheme.Typography.body.fontDescriptor.withSymbolicTraits(.italic), size: KrabEarTheme.Typography.body.pointSize - 1)
        translationLabel.textColor = NSColor.secondaryLabelColor
        translationLabel.maximumNumberOfLines = 0
        translationLabel.lineBreakMode = .byWordWrapping
        translationLabel.drawsBackground = false
        translationLabel.isBordered = false
        translationLabel.isEditable = false
        translationLabel.isSelectable = false
        translationLabel.translatesAutoresizingMaskIntoConstraints = false
        translationLabel.isHidden = true
        translationLabel.alphaValue = 0.8

        metaStack.orientation = .horizontal
        metaStack.spacing = KrabEarTheme.Metrics.standard
        metaStack.alignment = .centerY
        metaStack.translatesAutoresizingMaskIntoConstraints = false

        let contentStack = NSStackView()
        contentStack.orientation = .vertical
        contentStack.spacing = 2
        contentStack.alignment = .leading
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        contentStack.addArrangedSubview(transcriptLabel)
        contentStack.addArrangedSubview(translationLabel)
        contentStack.addArrangedSubview(metaStack)

        addSubview(contentStack)

        NSLayoutConstraint.activate([
            contentStack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: KrabEarTheme.Metrics.standard),
            contentStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -KrabEarTheme.Metrics.standard),
            contentStack.topAnchor.constraint(equalTo: topAnchor, constant: KrabEarTheme.Metrics.tight),
            contentStack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -KrabEarTheme.Metrics.tight)
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    public override func layout() {
        super.layout()
        backgroundHighlight.frame = bounds.insetBy(dx: 2, dy: 2)
    }

    public override var backgroundStyle: NSView.BackgroundStyle {
        didSet {
            if backgroundStyle == .emphasized {
                backgroundHighlight.backgroundColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.2).cgColor
            } else {
                backgroundHighlight.backgroundColor = KrabEarTheme.Colors.accent.withAlphaComponent(0.0).cgColor
            }
        }
    }
}
