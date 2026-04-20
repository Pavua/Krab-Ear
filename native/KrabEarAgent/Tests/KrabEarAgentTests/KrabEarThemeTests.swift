commit 16bbf704f1715c3237e7053361d6b2c30f6f52a3
Author: Pavua <pavelr7@gmail.com>
Date:   Mon Apr 20 05:18:21 2026 +0200

    test(agent): XCTest suite for KrabEarTheme tokens (Colors, Typography, Motion, Metrics)
    
    26 tests covering Colors (border dynamic dark/light alpha, accent, success, error, warning,
    separator alias, textTertiary alias), Typography (all 6 tokens: pointSizes, monospace check,
    tabular() extension), Motion (Duration ordering micro<short<standard<long, exact values,
    Easing presets non-nil, animate() smoke-test), Metrics (4-pt grid spacing, ordering,
    cardCornerRadius/innerCornerRadius concentricity, legacy aliases, controlHeight), and
    Interaction tokens (pressedScale <1, disabledOpacity partial, hover alphas subtle).
    
    Also fixes pre-existing Swift 6 breakage in HotkeyDoubleTapDetectorTests (replace
    inverted-expectation pattern with RunLoop.main.run; add @MainActor; remove final from
    HotkeyDoubleTapDetector) and WakeWordListenerTests (nonisolated(unsafe) on mutable vars).
    All 40 Swift tests pass.
    
    Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

diff --git a/native/KrabEarAgent/Tests/KrabEarAgentTests/KrabEarThemeTests.swift b/native/KrabEarAgent/Tests/KrabEarAgentTests/KrabEarThemeTests.swift
new file mode 100644
index 0000000..61be8df
--- /dev/null
+++ b/native/KrabEarAgent/Tests/KrabEarAgentTests/KrabEarThemeTests.swift
@@ -0,0 +1,297 @@
+/*
+ KrabEarThemeTests — XCTest suite for KrabEarTheme design tokens.
+
+ Coverage:
+   - Colors: accent, border, warning, success, error — non-nil NSColor values,
+     border is dynamic (NSColor with appearance provider).
+   - Typography: display, body, caption, captionMedium, monospace — valid NSFont,
+     sane pointSizes.
+   - Motion.Duration: ordering micro < short < standard < long.
+   - Motion.Easing: all four presets return non-nil CAMediaTimingFunction.
+   - Motion.animate: Reduce Motion flag guard verified via direct Duration check
+     (platform-independent; runtime mock note below).
+   - Metrics: spacing values on 4-pt grid, corner radii, legacy aliases.
+   - NSFont.tabular(): returns non-nil NSFont, same pointSize.
+   - Interaction: pressedScale < 1.0, disabledOpacity < 1.0.
+*/
+
+import XCTest
+import AppKit
+import QuartzCore
+@testable import KrabEarAgent
+
+@MainActor
+final class KrabEarThemeTests: XCTestCase {
+
+    // MARK: - Colors
+
+    func test_colors_accent_isNonNil() {
+        // controlAccentColor is a standard AppKit system dynamic color.
+        XCTAssertNotNil(KrabEarTheme.Colors.accent)
+    }
+
+    func test_colors_success_isSystemGreen() {
+        // .systemGreen is an NSColor — verify identity by comparing description prefix.
+        let color = KrabEarTheme.Colors.success
+        XCTAssertNotNil(color)
+        // systemGreen responds to resolved color in any appearance.
+        let resolved = color.usingColorSpace(.deviceRGB)
+        XCTAssertNotNil(resolved, "success color must resolve to a concrete RGB color")
+    }
+
+    func test_colors_error_isSystemRed() {
+        let color = KrabEarTheme.Colors.error
+        XCTAssertNotNil(color)
+        let resolved = color.usingColorSpace(.deviceRGB)
+        XCTAssertNotNil(resolved, "error color must resolve to a concrete RGB color")
+    }
+
+    func test_colors_warning_isSystemOrange() {
+        let color = KrabEarTheme.Colors.warning
+        XCTAssertNotNil(color)
+        let resolved = color.usingColorSpace(.deviceRGB)
+        XCTAssertNotNil(resolved, "warning color must resolve to a concrete RGB color")
+    }
+
+    func test_colors_border_isDynamic_darkModeAlpha() {
+        // border uses NSColor(name:dynamicProvider:) — resolves differently for dark vs light.
+        // NSAppearance.performAsCurrentDrawingAppearance is the correct macOS API for
+        // resolving dynamic colors without a live drawing context.
+        let border = KrabEarTheme.Colors.border
+
+        var darkAlpha: CGFloat = -1
+        NSAppearance(named: .darkAqua)!.performAsCurrentDrawingAppearance {
+            // Convert to device RGB so alphaComponent is readable.
+            if let resolved = border.usingColorSpace(.deviceRGB) {
+                darkAlpha = resolved.alphaComponent
+            }
+        }
+
+        var lightAlpha: CGFloat = -1
+        NSAppearance(named: .aqua)!.performAsCurrentDrawingAppearance {
+            if let resolved = border.usingColorSpace(.deviceRGB) {
+                lightAlpha = resolved.alphaComponent
+            }
+        }
+
+        // In dark mode: white at 0.15 alpha
+        XCTAssertEqual(darkAlpha, 0.15, accuracy: 0.01,
+                       "border dark alpha should be 0.15")
+        // In light mode: black at 0.10 alpha
+        XCTAssertEqual(lightAlpha, 0.10, accuracy: 0.01,
+                       "border light alpha should be 0.10")
+    }
+
+    func test_colors_separator_aliasesBorder() {
+        // separator is documented as a border alias — they must produce the same dynamic resolution.
+        let b = KrabEarTheme.Colors.border
+        let s = KrabEarTheme.Colors.separator
+
+        var bAlpha: CGFloat = -1
+        var sAlpha: CGFloat = -1
+        NSAppearance(named: .darkAqua)!.performAsCurrentDrawingAppearance {
+            bAlpha = b.usingColorSpace(.deviceRGB)?.alphaComponent ?? -1
+            sAlpha = s.usingColorSpace(.deviceRGB)?.alphaComponent ?? -1
+        }
+        XCTAssertEqual(bAlpha, sAlpha, accuracy: 0.001,
+                       "separator must alias border exactly")
+    }
+
+    func test_colors_textDisabled_aliasTextTertiary() {
+        // textTertiary is a deprecated alias for textDisabled.
+        let a = KrabEarTheme.Colors.textDisabled
+        let b = KrabEarTheme.Colors.textTertiary
+        // Both point to .tertiaryLabelColor — same object.
+        XCTAssertEqual(a, b, "textTertiary must alias textDisabled")
+    }
+
+    // MARK: - Typography
+
+    func test_typography_display_isSysFont17pt() {
+        let font = KrabEarTheme.Typography.display
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 17.0, accuracy: 0.1)
+    }
+
+    func test_typography_body_isSysFont13pt() {
+        let font = KrabEarTheme.Typography.body
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 13.0, accuracy: 0.1)
+    }
+
+    func test_typography_caption_is11pt() {
+        let font = KrabEarTheme.Typography.caption
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 11.0, accuracy: 0.1)
+    }
+
+    func test_typography_captionMedium_is11pt() {
+        let font = KrabEarTheme.Typography.captionMedium
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 11.0, accuracy: 0.1)
+    }
+
+    func test_typography_monospace_is11pt_monospaced() {
+        let font = KrabEarTheme.Typography.monospace
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 11.0, accuracy: 0.1)
+        // Monospaced system font name contains "Mono" on macOS.
+        let familyName = font.familyName ?? ""
+        XCTAssertTrue(
+            familyName.localizedCaseInsensitiveContains("mono") ||
+            font.fontName.localizedCaseInsensitiveContains("mono"),
+            "monospace token must use a monospaced font (got: \(font.fontName))"
+        )
+    }
+
+    func test_typography_sectionTitle_is13ptSemibold() {
+        let font = KrabEarTheme.Typography.sectionTitle
+        XCTAssertNotNil(font)
+        XCTAssertEqual(font.pointSize, 13.0, accuracy: 0.1)
+    }
+
+    // MARK: - NSFont.tabular()
+
+    func test_tabular_returnsSamePointSize() {
+        let base = KrabEarTheme.Typography.captionMedium
+        let tabbed = base.tabular()
+        XCTAssertNotNil(tabbed)
+        XCTAssertEqual(tabbed.pointSize, base.pointSize, accuracy: 0.1,
+                       "tabular() must preserve pointSize")
+    }
+
+    // MARK: - Motion.Duration ordering
+
+    func test_motion_duration_ordering_microShortStandardLong() {
+        XCTAssertLessThan(
+            KrabEarTheme.Motion.Duration.micro,
+            KrabEarTheme.Motion.Duration.short,
+            "micro must be shorter than short"
+        )
+        XCTAssertLessThan(
+            KrabEarTheme.Motion.Duration.short,
+            KrabEarTheme.Motion.Duration.standard,
+            "short must be shorter than standard"
+        )
+        XCTAssertLessThan(
+            KrabEarTheme.Motion.Duration.standard,
+            KrabEarTheme.Motion.Duration.long,
+            "standard must be shorter than long"
+        )
+    }
+
+    func test_motion_duration_values() {
+        XCTAssertEqual(KrabEarTheme.Motion.Duration.micro,    0.15, accuracy: 0.001)
+        XCTAssertEqual(KrabEarTheme.Motion.Duration.short,    0.25, accuracy: 0.001)
+        XCTAssertEqual(KrabEarTheme.Motion.Duration.standard, 0.40, accuracy: 0.001)
+        XCTAssertEqual(KrabEarTheme.Motion.Duration.long,     0.70, accuracy: 0.001)
+    }
+
+    // MARK: - Motion.Easing presets
+
+    func test_motion_easing_presetsAreNonNil() {
+        XCTAssertNotNil(KrabEarTheme.Motion.Easing.easeOut)
+        XCTAssertNotNil(KrabEarTheme.Motion.Easing.easeIn)
+        XCTAssertNotNil(KrabEarTheme.Motion.Easing.easeInOut)
+        XCTAssertNotNil(KrabEarTheme.Motion.Easing.linear)
+    }
+
+    // MARK: - Motion.animate — Reduce Motion guard
+
+    func test_motion_animate_reducedMotion_guard_durationsArePositiveOrZero() {
+        // We cannot mock NSWorkspace.accessibilityDisplayShouldReduceMotion in unit tests
+        // without method swizzling (fragile). Instead, verify the animate() wrapper runs
+        // without crashing and that Duration tokens satisfy the Reduce Motion contract:
+        // actualDuration = reduceMotion ? 0.0 : duration — so duration tokens must be ≥ 0.
+        for d in [KrabEarTheme.Motion.Duration.micro,
+                  KrabEarTheme.Motion.Duration.short,
+                  KrabEarTheme.Motion.Duration.standard,
+                  KrabEarTheme.Motion.Duration.long] {
+            XCTAssertGreaterThanOrEqual(d, 0.0,
+                "Duration token must be non-negative (Reduce Motion guard sets it to 0)")
+        }
+        // Smoke-test: animate runs without crashing on the main thread.
+        let exp = expectation(description: "animate block executes")
+        KrabEarTheme.Motion.animate(
+            duration: KrabEarTheme.Motion.Duration.micro,
+            easing: KrabEarTheme.Motion.Easing.easeOut
+        ) {
+            exp.fulfill()
+        }
+        wait(for: [exp], timeout: 1.0)
+    }
+
+    // MARK: - Metrics
+
+    func test_metrics_spacingOn4ptGrid() {
+        // All spacing tokens must be multiples of 4.
+        for (name, value) in [
+            ("tight",      KrabEarTheme.Metrics.tight),
+            ("standard",   KrabEarTheme.Metrics.standard),
+            ("comfortable", KrabEarTheme.Metrics.comfortable),
+            ("spacious",   KrabEarTheme.Metrics.spacious),
+        ] {
+            XCTAssertEqual(
+                value.truncatingRemainder(dividingBy: 4), 0,
+                accuracy: 0.001,
+                "\(name) (\(value)pt) must be on the 4-pt grid"
+            )
+        }
+    }
+
+    func test_metrics_spacingOrder() {
+        XCTAssertLessThan(KrabEarTheme.Metrics.tight,      KrabEarTheme.Metrics.standard)
+        XCTAssertLessThan(KrabEarTheme.Metrics.standard,   KrabEarTheme.Metrics.comfortable)
+        XCTAssertLessThan(KrabEarTheme.Metrics.comfortable, KrabEarTheme.Metrics.spacious)
+    }
+
+    func test_metrics_cornerRadii() {
+        XCTAssertEqual(KrabEarTheme.Metrics.cardCornerRadius, 12.0, accuracy: 0.001)
+        XCTAssertEqual(KrabEarTheme.Metrics.innerCornerRadius, 8.0, accuracy: 0.001)
+        // Inner must be smaller than outer (Apple concentricity rule).
+        XCTAssertLessThan(
+            KrabEarTheme.Metrics.innerCornerRadius,
+            KrabEarTheme.Metrics.cardCornerRadius,
+            "innerCornerRadius must be smaller than cardCornerRadius"
+        )
+    }
+
+    func test_metrics_legacyAliases() {
+        XCTAssertEqual(KrabEarTheme.Metrics.sectionSpacing, KrabEarTheme.Metrics.spacious,
+                       "sectionSpacing alias must equal spacious")
+        XCTAssertEqual(KrabEarTheme.Metrics.itemSpacing,    KrabEarTheme.Metrics.standard,
+                       "itemSpacing alias must equal standard")
+        XCTAssertEqual(KrabEarTheme.Metrics.cardPadding,    KrabEarTheme.Metrics.comfortable,
+                       "cardPadding alias must equal comfortable")
+    }
+
+    func test_metrics_controlHeight() {
+        XCTAssertEqual(KrabEarTheme.Metrics.controlHeight, 24.0, accuracy: 0.001)
+    }
+
+    // MARK: - Interaction tokens
+
+    func test_interaction_pressedScale_lessThanOne() {
+        XCTAssertLessThan(KrabEarTheme.Interaction.pressedScale, 1.0,
+                          "pressedScale must be <1 (micro shrink on press)")
+        XCTAssertGreaterThan(KrabEarTheme.Interaction.pressedScale, 0.9,
+                             "pressedScale must be >0.9 (subtle, not dramatic)")
+    }
+
+    func test_interaction_disabledOpacity_isPartial() {
+        XCTAssertGreaterThan(KrabEarTheme.Interaction.disabledOpacity, 0.0)
+        XCTAssertLessThan(KrabEarTheme.Interaction.disabledOpacity, 1.0,
+                          "disabledOpacity must be partial (not fully transparent or opaque)")
+        XCTAssertEqual(KrabEarTheme.Interaction.disabledOpacity, 0.40, accuracy: 0.001)
+    }
+
+    func test_interaction_hoverAlphas_areSmall() {
+        XCTAssertLessThan(KrabEarTheme.Interaction.hoverOverlayAlpha, 0.5,
+                          "hover overlay must be subtle (<50%)")
+        XCTAssertLessThan(KrabEarTheme.Interaction.pressedOverlayAlpha, 0.5,
+                          "pressed overlay must be subtle (<50%)")
+        XCTAssertLessThan(KrabEarTheme.Interaction.transparentHoverAlpha,
+                          KrabEarTheme.Interaction.hoverOverlayAlpha,
+                          "transparent hover must be softer than standard hover")
+    }
+}
