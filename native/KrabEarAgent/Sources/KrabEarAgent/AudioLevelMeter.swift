/*
 AudioLevelMeter.swift
 VU meter view for Krab Ear realtime overlay.

 Displays a 4pt-height colored bar representing RMS audio level.
 Color coding: green < 0.70, yellow 0.70-0.90, red > 0.90 (clipping).
 Uses KrabEarTheme.Colors tokens. Animated via CATransaction at 33ms.
*/

import AppKit
import QuartzCore

/// A minimal VU (volume unit) meter NSView backed by two CALayers.
@MainActor
public final class AudioLevelMeter: NSView {

    // MARK: - Layers

    /// Background track layer (full width, dim)
    private let trackLayer = CALayer()

    /// Foreground bar layer (animated width + color)
    private let barLayer = CALayer()

    /// Last known level for dark/light mode refresh
    private var currentLevel: Float = 0.0

    // MARK: - Init

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        setupLayers()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        setupLayers()
    }

    // MARK: - Setup

    private func setupLayers() {
        wantsLayer = true
        layer?.masksToBounds = true

        trackLayer.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.25).cgColor
        trackLayer.cornerRadius = 2
        layer?.addSublayer(trackLayer)

        barLayer.backgroundColor = KrabEarTheme.Colors.success.cgColor
        barLayer.cornerRadius = 2
        layer?.addSublayer(barLayer)
    }

    // MARK: - Layout

    public override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        trackLayer.frame = bounds
        let targetWidth = bounds.width * CGFloat(currentLevel)
        barLayer.frame = CGRect(x: 0, y: 0, width: targetWidth, height: bounds.height)
        CATransaction.commit()
    }

    // MARK: - Appearance

    public override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        trackLayer.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.25).cgColor
        barLayer.backgroundColor = barColor(for: currentLevel).cgColor
    }

    // MARK: - Helpers

    private func barColor(for level: Float) -> NSColor {
        if level > 0.90 { return KrabEarTheme.Colors.error }
        else if level > 0.70 { return KrabEarTheme.Colors.warning }
        else { return KrabEarTheme.Colors.success }
    }
}
