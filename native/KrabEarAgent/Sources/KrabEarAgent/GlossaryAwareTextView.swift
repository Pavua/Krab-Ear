/*
 GlossaryAwareTextView — NSTextView с hover-popover для glossary terms.

 UX:
 - Mouse hover over a word → если слово (case-insensitive trim punctuation) есть в
   `glossary`, показываем NSPopover "оригинал → перевод" под cursor'ом.
 - Hover off-word → popover скрывается.
 - Click на текст → popover скрывается (don't interrupt selection).

 Ускорения:
 - Glossary stored как [String: String] — case-folded source → target.
 - Word boundaries определяются Unicode `letterSet` + cyrillic + ASCII apostrophe.
 - tracking only "mouseMoved" + "mouseExited" — NSTrackingArea с .activeAlways.

 Эта view используется в TranslationStreamView (Phase 2 PR 2.3) для обеих
 панелей — пользователь может hover'ить любое слово и увидеть как оно
 переводится в personal glossary (медицинские термины, proper names).
*/

import AppKit

@MainActor
final class GlossaryAwareTextView: NSTextView {

    // MARK: - Public state

    /// Case-folded keys → translations. Updates atomically через `setGlossary(_:)`.
    private(set) var glossary: [String: String] = [:]

    // MARK: - Internal state

    private var trackingArea: NSTrackingArea?
    private weak var popover: NSPopover?
    private var lastHoveredWord: String?

    // MARK: - Public API

    /// Replaces glossary atomically. Lower-cased keys, trimmed punctuation.
    func setGlossary(_ rawGlossary: [String: String]) {
        var normalized: [String: String] = [:]
        for (k, v) in rawGlossary {
            let key = k.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard !key.isEmpty else { continue }
            normalized[key] = v
        }
        self.glossary = normalized
    }

    // MARK: - Tracking area lifecycle

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = trackingArea {
            removeTrackingArea(existing)
        }
        let opts: NSTrackingArea.Options = [.activeAlways, .mouseEnteredAndExited, .mouseMoved, .inVisibleRect]
        let area = NSTrackingArea(rect: bounds, options: opts, owner: self, userInfo: nil)
        trackingArea = area
        addTrackingArea(area)
    }

    override func mouseMoved(with event: NSEvent) {
        super.mouseMoved(with: event)
        guard !glossary.isEmpty else { return }
        let pointInView = convert(event.locationInWindow, from: nil)
        guard let word = wordAtPoint(pointInView), !word.isEmpty else {
            hidePopover()
            return
        }
        let key = word.lowercased()
        guard let translation = glossary[key] else {
            hidePopover()
            return
        }
        if lastHoveredWord == key, popover != nil {
            // Уже показываем — не пересоздаём (no flicker).
            return
        }
        lastHoveredWord = key
        showPopover(forWord: word, translation: translation, at: pointInView)
    }

    override func mouseExited(with event: NSEvent) {
        super.mouseExited(with: event)
        hidePopover()
    }

    override func mouseDown(with event: NSEvent) {
        super.mouseDown(with: event)
        // Click отменяет popover чтобы не мешать selection / drag.
        hidePopover()
    }

    // MARK: - Word detection

    /// Возвращает слово под точкой (Unicode letters + cyrillic) или nil если
    /// курсор на пробеле / punctuation / outside text.
    func wordAtPoint(_ point: NSPoint) -> String? {
        guard let layoutManager = self.layoutManager,
              let textContainer = self.textContainer else { return nil }
        let pointInContainer = NSPoint(
            x: point.x - textContainerOrigin.x,
            y: point.y - textContainerOrigin.y
        )
        let glyphIndex = layoutManager.glyphIndex(for: pointInContainer, in: textContainer)
        let charIndex = layoutManager.characterIndexForGlyph(at: glyphIndex)

        let s = self.string
        guard charIndex < s.count else { return nil }

        let chars = Array(s)
        guard charIndex < chars.count, isWordChar(chars[charIndex]) else { return nil }

        // Расширяем влево/вправо до non-word boundaries.
        var start = charIndex
        while start > 0, isWordChar(chars[start - 1]) {
            start -= 1
        }
        var end = charIndex
        while end + 1 < chars.count, isWordChar(chars[end + 1]) {
            end += 1
        }
        return String(chars[start...end])
    }

    private func isWordChar(_ c: Character) -> Bool {
        for scalar in c.unicodeScalars {
            // Letters of any script (включая cyrillic), digits, apostrophes, hyphens.
            if CharacterSet.letters.contains(scalar) { return true }
            if scalar == "'" || scalar == "-" { return true }
        }
        return false
    }

    // MARK: - Popover

    private func showPopover(forWord word: String, translation: String, at point: NSPoint) {
        hidePopover()

        let pop = NSPopover()
        pop.behavior = .transient
        pop.animates = false  // Снижает мерцание при rapid mouse moves.

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: word)
        title.font = KrabEarTheme.Typography.body.bold()
        title.textColor = KrabEarTheme.Colors.textPrimary

        let arrow = NSTextField(labelWithString: "↓")
        arrow.font = KrabEarTheme.Typography.caption
        arrow.textColor = KrabEarTheme.Colors.textSecondary

        let trans = NSTextField(labelWithString: translation)
        trans.font = KrabEarTheme.Typography.body
        trans.textColor = KrabEarTheme.Colors.accent

        let hint = NSTextField(labelWithString: "Из глоссария")
        hint.font = KrabEarTheme.Typography.caption
        hint.textColor = KrabEarTheme.Colors.textSecondary

        stack.addArrangedSubview(title)
        stack.addArrangedSubview(arrow)
        stack.addArrangedSubview(trans)
        stack.addArrangedSubview(hint)

        let vc = NSViewController()
        let container = NSView()
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: container.topAnchor),
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        vc.view = container
        pop.contentViewController = vc

        // Anchor — small rect под курсор.
        let anchor = NSRect(x: point.x - 1, y: point.y - 1, width: 2, height: 2)
        pop.show(relativeTo: anchor, of: self, preferredEdge: .maxY)
        self.popover = pop
    }

    private func hidePopover() {
        popover?.close()
        popover = nil
        lastHoveredWord = nil
    }
}

// MARK: - Bold helper for NSFont (since KrabEarTheme.Typography.body is plain).

private extension NSFont {
    func bold() -> NSFont {
        let descriptor = self.fontDescriptor.withSymbolicTraits(.bold)
        return NSFont(descriptor: descriptor, size: self.pointSize) ?? self
    }
}
