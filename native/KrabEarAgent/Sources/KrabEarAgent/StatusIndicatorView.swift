/*
 Маленький view с цветным dot — отображает HealthState.

 Phase A: зелёный/жёлтый/красный dot по supervisor state.
 Phase B.1: layered severity badge (6pt circle overlay в top-right углу).

 Используется:
 1) В menu bar (NSStatusItem.button.image) как глобальный статус.
 2) В History panel header (8x8 dot слева от заголовка).

 Связи модуля:
 1) HealthMonitor: подписка на изменения через onStateChange.
 2) main.swift: создаёт menu bar item.
 3) Phase B.1: applyErrorBadge(severity:) + hideBadge() добавляют severity overlay.
*/

import AppKit
import QuartzCore

// MARK: - HealthState

/// Наблюдаемое состояние backend'а.
///
/// Определяется здесь чтобы StatusIndicatorView был самодостаточным файлом
/// (HealthMonitor определяет тот же тип, но находится в отдельном pull).
enum HealthState: Sendable, Equatable {
    /// Backend жив, последние ping'и проходят.
    case healthy
    /// Backend завис: 2+ ping'а подряд не ответили.
    case hung
    /// Backend остановлен (явно через `stop()`).
    case stopped
}

// MARK: - StatusIndicatorView

/// View с круглым dot, цвет которого отражает HealthState.
///
/// Phase B.1: поддерживает severity badge — 6pt circle overlay в top-right углу
/// поверх основного supervisor dot.
final class StatusIndicatorView: NSView {

    // MARK: - Phase A state

    private var dotColor: NSColor = .systemGreen

    // MARK: - Phase B.1 badge state

    /// Текущий severity badge слой (nil если badge не отображается).
    private var badgeLayer: CALayer?

    /// Таймер для мигания critical badge. Хранится чтобы можно было инвалидировать.
    /// `internal` (не `private`) чтобы тест-расширение StatusIndicatorViewTests могло инспектировать.
    var blinkTimer: Timer?

    /// Последний severity применённый через applyErrorBadge — для idempotency guard.
    private var currentBadgeSeverity: String?

    // MARK: - Init

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    // MARK: - Phase A: supervisor state

    /// Обновляет цвет dot. Можно вызывать с любого потока.
    func updateState(_ state: HealthState) {
        let newColor: NSColor
        switch state {
        case .healthy: newColor = .systemGreen
        case .hung:    newColor = .systemYellow
        case .stopped: newColor = .systemRed
        }
        DispatchQueue.main.async { [weak self] in
            self?.dotColor = newColor
            self?.needsDisplay = true
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let radius = min(bounds.width, bounds.height) / 2
        let dotRect = NSRect(
            x: bounds.midX - radius,
            y: bounds.midY - radius,
            width: radius * 2,
            height: radius * 2
        )
        dotColor.setFill()
        let path = NSBezierPath(ovalIn: dotRect)
        path.fill()
    }

    // MARK: - Phase B.2: Privacy Badge

    private var privacyBadgeLayer: CALayer?
    private var isPrivacyModeEnabled: Bool = false

    /// Показывает/скрывает privacy оверлей (SF Symbol lock.fill)
    func setPrivacyMode(_ on: Bool) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.isPrivacyModeEnabled = on
            self._updatePrivacyBadge()
        }
    }

    private func _updatePrivacyBadge() {
        if !isPrivacyModeEnabled {
            privacyBadgeLayer?.removeFromSuperlayer()
            privacyBadgeLayer = nil
            return
        }

        if privacyBadgeLayer != nil { return } // уже есть

        wantsLayer = true
        guard let hostLayer = self.layer else { return }

        // Размещаем bottom-left
        let badgeSize: CGFloat = 8.0
        let badgeFrame = CGRect(
            x: -2,
            y: -2,
            width: badgeSize,
            height: badgeSize
        )

        let badge = CALayer()
        badge.frame = badgeFrame
        badge.cornerRadius = badgeSize / 2
        badge.backgroundColor = KrabEarTheme.Colors.accent.cgColor
        badge.zPosition = 10

        if let lockImg = NSImage(systemSymbolName: "lock.fill", accessibilityDescription: nil) {
            let config = NSImage.SymbolConfiguration(pointSize: 6, weight: .semibold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
            if let configLock = lockImg.withSymbolConfiguration(config),
               let cgImage = configLock.cgImage(forProposedRect: nil, context: nil, hints: nil) {
                let imgLayer = CALayer()
                imgLayer.frame = CGRect(x: 1, y: 1, width: 6, height: 6)
                imgLayer.contents = cgImage
                imgLayer.contentsGravity = .resizeAspect
                badge.addSublayer(imgLayer)
            }
        }

        hostLayer.addSublayer(badge)
        privacyBadgeLayer = badge
    }

    // MARK: - Phase B.1: severity badge

    /// Добавляет 6pt circle overlay в top-right corner.
    ///
    /// - `critical`: красный, мигает (alpha 0.5↔1.0 раз в 1s).
    /// - `error`:    оранжевый, steady.
    /// - `warn`:     жёлтый, steady.
    /// - `info`:     badge скрывается (эквивалент hideBadge).
    /// - любое другое значение: badge скрывается.
    ///
    /// Idempotent — повторный вызов с тем же severity — no-op.
    /// Thread-safe — безопасно вызывать из любого потока.
    func applyErrorBadge(severity: String) {
        DispatchQueue.main.async { [weak self] in
            self?._applyErrorBadge(severity: severity)
        }
    }

    /// Убирает badge и останавливает мигание.
    ///
    /// Idempotent — безопасно вызывать когда badge уже убран.
    /// Thread-safe — безопасно вызывать из любого потока.
    func hideBadge() {
        DispatchQueue.main.async { [weak self] in
            self?._hideBadge()
        }
    }

    // MARK: - Phase B.1: flash green on probe recovery

    /// Кратковременно (800ms) мигает dot ярко-зелёным, затем возвращает прежний цвет.
    ///
    /// Используется HealthMonitor.subscribeToProbeEvents при `rewriter_recovered` событии.
    /// Thread-safe — безопасно вызывать из любого потока.
    /// - Parameter reason: причина для лог-записи.
    func flashGreen(reason: String) {
        DispatchQueue.main.async { [weak self] in
            self?._flashGreen(reason: reason)
        }
    }

    private func _flashGreen(reason: String) {
        // Запоминаем текущий цвет чтобы восстановить после вспышки
        let previousColor = dotColor
        dotColor = .systemGreen
        needsDisplay = true

        // Через 800ms возвращаем прежний цвет
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self else { return }
            // Восстанавливаем только если цвет не изменился из-за другого updateState
            if self.dotColor == .systemGreen {
                self.dotColor = previousColor
                self.needsDisplay = true
            }
        }
    }

    // MARK: - Private badge helpers (must be called on Main thread)

    private func _applyErrorBadge(severity: String) {
        // Определяем цвет и нужно ли мигание
        let badgeColor: NSColor
        let shouldBlink: Bool

        switch severity {
        case "critical":
            badgeColor = .systemRed
            shouldBlink = true
        case "error":
            badgeColor = .systemOrange
            shouldBlink = false
        case "warn":
            badgeColor = .systemYellow
            shouldBlink = false
        case "info":
            _hideBadge()
            return
        default:
            _hideBadge()
            return
        }

        // Idempotency: тот же severity → no-op (не пересоздаём слой, не сбрасываем анимацию)
        if currentBadgeSeverity == severity { return }
        currentBadgeSeverity = severity

        // Убираем предыдущий badge перед созданием нового
        _removeBadgeLayerOnly()

        // Убеждаемся что у вью есть CA layer
        wantsLayer = true
        guard let hostLayer = self.layer else { return }

        // Создаём badge layer: 6pt circle в top-right corner
        let badgeSize: CGFloat = 6.0
        let badgeFrame = CGRect(
            x: bounds.width - badgeSize,
            y: bounds.height - badgeSize,
            width: badgeSize,
            height: badgeSize
        )

        let badge = CALayer()
        badge.frame = badgeFrame
        badge.cornerRadius = badgeSize / 2
        badge.backgroundColor = badgeColor.cgColor
        badge.zPosition = 10  // поверх основного dot

        hostLayer.addSublayer(badge)
        badgeLayer = badge

        // Запускаем мигание если нужно
        if shouldBlink {
            _startBlinking()
        }
    }

    private func _hideBadge() {
        currentBadgeSeverity = nil
        _stopBlinking()
        _removeBadgeLayerOnly()
    }

    /// Убирает только CALayer, не трогает таймер (используется при replace).
    private func _removeBadgeLayerOnly() {
        badgeLayer?.removeFromSuperlayer()
        badgeLayer = nil
    }

    private func _startBlinking() {
        // Останавливаем предыдущий таймер на случай повторного вызова
        _stopBlinking()

        // Начальное состояние — полная непрозрачность
        badgeLayer?.opacity = 1.0

        // isVisible is captured by the class instance to avoid Sendable mutation warnings
        var isVisible = true
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            // Timer callbacks run on the main run loop; assumeIsolated is safe here.
            MainActor.assumeIsolated {
                guard let self, let badge = self.badgeLayer else { return }
                isVisible.toggle()
                // CATransaction.withDisabledActions отключает implicit animation чтобы
                // переключение было мгновенным (alpha skips, не fade)
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                badge.opacity = isVisible ? 1.0 : 0.5
                CATransaction.commit()
            }
        }
        // Добавляем в .common чтобы таймер работал при трекинге мыши
        RunLoop.current.add(timer, forMode: .common)
        blinkTimer = timer
    }

    private func _stopBlinking() {
        blinkTimer?.invalidate()
        blinkTimer = nil
        // Сбрасываем opacity badge (если он ещё есть)
        badgeLayer?.opacity = 1.0
    }
}

// MARK: - StatusIndicatorImage

/// Helper: создаёт NSImage с dot и severity overlay — для NSStatusItem.button.image.
enum StatusIndicatorImage {
    static func badgeColor(for severity: String?) -> NSColor? {
        switch severity {
        case "critical": return .systemRed
        case "error":    return .systemOrange
        case "warn":     return .systemYellow
        default:           return nil
        }
    }

    /// Возвращает только severity, которые являются видимым error badge.
    /// `info` и неизвестные значения одновременно служат явным clear-сигналом.
    static func visibleBadgeSeverity(_ severity: String?) -> String? {
        badgeColor(for: severity) == nil ? nil : severity
    }

    static func image(
        for state: HealthState,
        privacyMode: Bool = false,
        errorSeverity: String? = nil,
        badgeOpacity: CGFloat = 1.0,
        size: CGFloat = 14
    ) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size))
        img.lockFocus()
        let color: NSColor
        switch state {
        case .healthy: color = .systemGreen
        case .hung:    color = .systemYellow
        case .stopped: color = .systemRed
        }
        color.setFill()
        let rect = NSRect(x: 2, y: 2, width: size - 4, height: size - 4)
        NSBezierPath(ovalIn: rect).fill()

        if let badgeColor = badgeColor(for: errorSeverity) {
            badgeColor.withAlphaComponent(badgeOpacity).setFill()
            let badgeSize: CGFloat = 6.0
            let badgeRect = NSRect(
                x: size - badgeSize,
                y: size - badgeSize,
                width: badgeSize,
                height: badgeSize
            )
            NSBezierPath(ovalIn: badgeRect).fill()
        }

        if privacyMode {
            // lock.fill in bottom-left corner
            KrabEarTheme.Colors.accent.setFill()
            let badgeSize: CGFloat = 8.0
            let badgeRect = NSRect(x: 0, y: 0, width: badgeSize, height: badgeSize)
            NSBezierPath(ovalIn: badgeRect).fill()

            if let lockImg = NSImage(systemSymbolName: "lock.fill", accessibilityDescription: nil) {
                let lockConfig = NSImage.SymbolConfiguration(pointSize: 6, weight: .semibold)
                    .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
                if let configLock = lockImg.withSymbolConfiguration(lockConfig) {
                    let lockRect = NSRect(x: 1, y: 1, width: 6, height: 6)
                    configLock.draw(in: lockRect)
                }
            }
        }

        img.unlockFocus()
        return img
    }
}
