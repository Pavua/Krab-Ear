import AppKit
import XCTest
@testable import KrabEarAgent

/// Регресс #2255: после правила подписей (сопротивление сжатию 240) ряд заголовка секции
/// в стеке с alignment .leading схлопывался до 25–45 pt — заголовки ВСЕХ CollapsibleSectionView
/// пропали, на экране оставался один шеврон.
@MainActor
final class CollapsibleSectionHeaderTests: XCTestCase {

    private func layOut(_ section: CollapsibleSectionView, width: CGFloat) -> NSView {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 400))
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        ])
        stack.addArrangedSubview(section)
        section.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        KrabEarTheme.wrapLongLabels(in: root)  // как при показе вкладки
        root.layoutSubtreeIfNeeded()
        root.layoutSubtreeIfNeeded()
        return root
    }

    func test_title_isFullyVisible_afterLabelRule() {
        for expanded in [false, true] {
            let section = CollapsibleSectionView(sectionId: "t", title: "Расширенные действия", isExpanded: expanded)
            _ = layOut(section, width: 600)
            let title = section.titleLabel
            XCTAssertGreaterThanOrEqual(title.bounds.width + 1, title.intrinsicContentSize.width,
                                        "заголовок виден целиком (expanded=\(expanded)): \(title.bounds.width)")
        }
    }

    func test_headerRow_spansSectionWidth() {
        let section = CollapsibleSectionView(sectionId: "t", title: "Фильтры", isExpanded: false)
        _ = layOut(section, width: 600)
        guard let header = section.titleLabel.superview else { return XCTFail("нет ряда заголовка") }
        XCTAssertEqual(header.bounds.width, section.bounds.width, accuracy: 1,
                       "весь ряд заголовка — цель клика на всю ширину")
    }
}
