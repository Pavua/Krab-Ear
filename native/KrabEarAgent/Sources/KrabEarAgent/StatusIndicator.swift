/*
 Маленький view с цветным dot — отображает HealthState.

 Используется:
 1) В menu bar (NSStatusItem.button.image) как глобальный статус.
 2) В History panel header (8x8 dot слева от заголовка).

 Связи модуля:
 1) HealthMonitor: подписка на изменения через onStateChange.
 2) main.swift: создаёт menu bar item.
*/

import AppKit

/// View с круглым dot, цвет которого отражает HealthState.
final class StatusIndicatorView: NSView {
    private var dotColor: NSColor = .systemGreen

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    /// Обновляет цвет dot. Можно вызывать с любого потока.
    func updateState(_ state: HealthState) {
        let newColor: NSColor
        switch state {
        case .healthy: newColor = .systemGreen
        case .hung: newColor = .systemYellow
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
}

/// Helper: создаёт NSImage с dot указанного цвета — для NSStatusItem.button.image.
enum StatusIndicatorImage {
    static func image(for state: HealthState, size: CGFloat = 14) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size))
        img.lockFocus()
        let color: NSColor
        switch state {
        case .healthy: color = .systemGreen
        case .hung: color = .systemYellow
        case .stopped: color = .systemRed
        }
        color.setFill()
        let rect = NSRect(x: 2, y: 2, width: size - 4, height: size - 4)
        NSBezierPath(ovalIn: rect).fill()
        img.unlockFocus()
        return img
    }
}
