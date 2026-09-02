import XCTest
import AppKit
@testable import KrabEarAgent

/// Оверлей показывает ХВОСТ накопленного превью, а не его начало (02.09.2026).
///
/// `preview_text` приходит от backend'а КУМУЛЯТИВНЫМ (обрезан до 900 знаков), а
/// панель зафиксирована по высоте. Длинная диктовка целиком не помещается — и
/// владелец видит застывшее НАЧАЛО, пока говорит дальше: «показывает по-прежнему
/// каждое обновление всё сообщение, которое от начала до конца». Меняются только
/// последние слова, их и надо показывать.
///
/// Логика вынесена в чистую функцию с внешним измерителем: настоящая проверяет
/// высоту через `NSString.boundingRect`, тест — через предсказуемую подделку, и
/// поведение проверяется без создания NSPanel.
final class RealtimeOverlayTailTests: XCTestCase {

    /// Подделка измерителя: 20 точек на каждые 40 символов строки.
    private func fakeMeasure(_ s: String) -> CGFloat {
        CGFloat((s.count + 39) / 40) * 20.0
    }

    func test_shortText_isReturnedUnchanged() {
        let text = "Проверяю диктовку"
        let out = RealtimeOverlayController.tailFitting(text, available: 100, measure: fakeMeasure)
        XCTAssertEqual(out, text, "короткий текст помещается целиком и не должен трогаться")
    }

    func test_longText_keepsTheEnd_notTheBeginning() {
        let words = (1...120).map { "слово\($0)" }
        let text = words.joined(separator: " ")
        let out = RealtimeOverlayController.tailFitting(text, available: 100, measure: fakeMeasure)

        XCTAssertNotEqual(out, text, "длинный текст обязан быть подрезан")
        XCTAssertTrue(
            out.hasSuffix("слово120"),
            "видимой должна остаться последняя произнесённая часть, а не начало"
        )
        XCTAssertFalse(
            out.contains("слово1 "),
            "начало диктовки должно уйти за кадр — иначе владелец снова смотрит на застывший текст"
        )
    }

    func test_trimmedText_isMarkedWithEllipsis() {
        let text = (1...120).map { "слово\($0)" }.joined(separator: " ")
        let out = RealtimeOverlayController.tailFitting(text, available: 100, measure: fakeMeasure)
        XCTAssertTrue(out.hasPrefix("…"), "обрезка должна быть честно обозначена многоточием")
    }

    func test_trimmedText_actuallyFitsTheBudget() {
        let text = (1...300).map { "слово\($0)" }.joined(separator: " ")
        let available: CGFloat = 100
        let out = RealtimeOverlayController.tailFitting(text, available: available, measure: fakeMeasure)
        XCTAssertLessThanOrEqual(
            fakeMeasure(out), available,
            "результат обязан помещаться в отведённую высоту — иначе обрезка бессмысленна"
        )
    }

    func test_cutsOnWordBoundary_notMidWord() {
        let text = (1...120).map { "слово\($0)" }.joined(separator: " ")
        let out = RealtimeOverlayController.tailFitting(text, available: 100, measure: fakeMeasure)
        let firstWord = out.dropFirst().split(separator: " ").first.map(String.init) ?? ""
        XCTAssertTrue(
            text.split(separator: " ").map(String.init).contains(firstWord),
            "строка не должна начинаться с обрубка слова: получено '\(firstWord)'"
        )
    }

    func test_zeroBudget_returnsTextRatherThanEmptyString() {
        let text = "Проверяю диктовку"
        let out = RealtimeOverlayController.tailFitting(text, available: 0, measure: fakeMeasure)
        XCTAssertFalse(out.isEmpty, "нулевой бюджет — это ошибка вёрстки, а не повод показать пустой оверлей")
    }
}
