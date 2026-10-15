import AppKit

/// Все входы существующего render; внешний вид определяет прежний callback.
struct StatusItemRefreshState: Equatable {
    let health: HealthState
    let recording: Bool
    let processing: Bool
    let presetBadge: String
    let privacy: Bool
    let severity: String?
    let badgeOpacity: CGFloat
    let appearance: String
}

/// Не трогаем AppKit при точном повторе входов И наблюдаемого результата.
/// Проверка результата важна: bookmark/quick-replace могут менять title напрямую.
@MainActor
final class StatusItemRefreshGate: NSObject {
    private var previous: StatusItemRefreshState?
    private weak var previousButton: NSButton?
    private var previousTitle: String?
    private var previousImage: NSImage?
    private var previousPosition: NSControl.ImagePosition?

    func apply(_ state: StatusItemRefreshState, to button: NSButton, render: () -> Void) {
        if previous == state,
           previousButton === button,
           previousTitle == button.title,
           previousImage === button.image,
           previousPosition == button.imagePosition {
            return
        }
        render()
        previous = state
        previousButton = button
        previousTitle = button.title
        previousImage = button.image
        previousPosition = button.imagePosition
    }
}
