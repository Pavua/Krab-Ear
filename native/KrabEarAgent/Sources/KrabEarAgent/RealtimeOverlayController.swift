/*
 Экранный realtime-оверлей Krab Ear.

 Связи модуля:
 1) main.swift: показывает и обновляет оверлей во время активной записи.
 2) IPC get_recording_state: источник промежуточного текста и таймера.
*/

import AppKit
import Foundation

/// Лёгкое плавающее окно внизу экрана для realtime-превью диктовки.
@MainActor
final class RealtimeOverlayController {
    private let panel: NSPanel
    private let modeLabel = NSTextField(labelWithString: "OFF")
    private let statusLabel = NSTextField(labelWithString: "00:00")
    private let textView = NSTextView()
    private var opacityPercent: Int = 45

    init() {
        let initialRect = NSRect(x: 0, y: 0, width: 700, height: 128)
        self.panel = NSPanel(
            contentRect: initialRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        setupPanel()
        setupUI()
    }

    func show() {
        positionAtBottomCenter()
        panel.orderFront(nil)
    }

    func hide() {
        panel.orderOut(nil)
    }

    func update(previewText: String, translatedText: String?, durationText: String, modeHint: String) {
        statusLabel.stringValue = durationText
        modeLabel.stringValue = "Mode: \(modeHint)"
        let cleanPreview = previewText.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanPreview.isEmpty {
            textView.string = "Слушаю... промежуточный текст появится через 1-2 секунды."
        } else {
            let cleanTranslation = (translatedText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if cleanTranslation.isEmpty {
                textView.string = cleanPreview
            } else {
                textView.string = "\(cleanPreview)\n\n↔ Перевод\n\(cleanTranslation)"
            }
        }
        if panel.isVisible {
            positionAtBottomCenter()
        }
    }

    func setOpacityPercent(_ value: Int) {
        let safe = max(15, min(90, value))
        opacityPercent = safe
        panel.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: CGFloat(Double(safe) / 100.0))
    }

    private func setupPanel() {
        panel.level = .statusBar
        panel.isFloatingPanel = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = NSColor(calibratedWhite: 0.08, alpha: 0.45)
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
    }

    private func setupUI() {
        guard let contentView = panel.contentView else { return }

        modeLabel.textColor = NSColor(calibratedWhite: 0.82, alpha: 1.0)
        modeLabel.font = NSFont.systemFont(ofSize: 10, weight: .medium)
        modeLabel.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.textColor = NSColor(calibratedWhite: 0.86, alpha: 1.0)
        statusLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .medium)
        statusLabel.alignment = .right
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        let textScroll = NSScrollView()
        textScroll.translatesAutoresizingMaskIntoConstraints = false
        textScroll.hasVerticalScroller = true
        textScroll.borderType = .noBorder
        textScroll.drawsBackground = false

        textView.isEditable = false
        textView.drawsBackground = false
        textView.textColor = NSColor(calibratedWhite: 0.95, alpha: 1.0)
        textView.font = NSFont.systemFont(ofSize: 13, weight: .regular)
        textView.string = "Слушаю... промежуточный текст появится через 1-2 секунды."
        textView.textContainerInset = NSSize(width: 4, height: 6)
        textScroll.documentView = textView

        contentView.addSubview(modeLabel)
        contentView.addSubview(statusLabel)
        contentView.addSubview(textScroll)

        NSLayoutConstraint.activate([
            modeLabel.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 8),
            modeLabel.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 12),

            statusLabel.centerYAnchor.constraint(equalTo: modeLabel.centerYAnchor),
            statusLabel.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -12),

            textScroll.topAnchor.constraint(equalTo: modeLabel.bottomAnchor, constant: 8),
            textScroll.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 8),
            textScroll.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -8),
            textScroll.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -8),
        ])
    }

    private func positionAtBottomCenter() {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let width = min(760, max(500, visible.width * 0.48))
        let height: CGFloat = 128
        let x = visible.midX - width / 2
        let y = visible.minY + 36
        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
    }
}
