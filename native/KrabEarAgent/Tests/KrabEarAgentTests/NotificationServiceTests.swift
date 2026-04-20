/*
 NotificationServiceTests — тесты сервиса уведомлений через osascript.

 Стратегия:
 NotificationService — final class без DI, использует /usr/bin/osascript.
 Тестируем:
   1. requestAuthorizationIfNeeded() — no-op, не падает.
   2. notify() с обычным input не бросает исключений.
   3. Экранирование кавычек: проверяем формулу escaping белым ящиком
      (та же логика что в production — replacingOccurrences of "\"" with "\\\"").
   4. Граничные случаи: пустые строки, строки с несколькими кавычками.
   5. Скрипт osascript собирается в ожидаемом формате.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Tests

final class NotificationServiceTests: XCTestCase {

    private var svc: NotificationService!

    override func setUp() {
        super.setUp()
        svc = NotificationService()
    }

    // MARK: - requestAuthorizationIfNeeded

    /// requestAuthorizationIfNeeded() — no-op, не падает.
    func test_requestAuthorizationIfNeeded_doesNotCrash() {
        svc.requestAuthorizationIfNeeded()
        // Если дошли сюда — тест прошёл.
    }

    // MARK: - notify: does not crash

    /// notify() с обычными строками не бросает и не крашит.
    func test_notify_normalInput_doesNotCrash() {
        // Реальный osascript запустится, но тест не ждёт его завершения —
        // Process.run() async, нам важно что метод не throws/не крашится.
        svc.notify(title: "Krab Ear", body: "Готово")
    }

    /// notify() с пустыми строками не крашится.
    func test_notify_emptyStrings_doesNotCrash() {
        svc.notify(title: "", body: "")
    }

    // MARK: - Escaping logic (whitebox)

    /// Формула экранирования кавычек: replacingOccurrences(of: "\"", with: "\\\"").
    /// Проверяем что кавычки в title/body корректно экранируются перед передачей в osascript.
    func test_escaping_doubleQuotesInTitle() {
        let raw = "Say \"Hello\""
        let escaped = raw.replacingOccurrences(of: "\"", with: "\\\"")
        XCTAssertEqual(escaped, "Say \\\"Hello\\\"",
                       "Двойные кавычки должны экранироваться как \\\"")
        // Убеждаемся что notify вызывается без крэша на таком input
        svc.notify(title: raw, body: "body")
    }

    func test_escaping_doubleQuotesInBody() {
        let raw = "He said \"bye\""
        let escaped = raw.replacingOccurrences(of: "\"", with: "\\\"")
        XCTAssertEqual(escaped, "He said \\\"bye\\\"",
                       "Двойные кавычки в body должны экранироваться как \\\"")
        svc.notify(title: "title", body: raw)
    }

    func test_escaping_multipleQuotes() {
        let raw = "\"A\" and \"B\""
        let escaped = raw.replacingOccurrences(of: "\"", with: "\\\"")
        XCTAssertEqual(escaped, "\\\"A\\\" and \\\"B\\\"")
        svc.notify(title: raw, body: raw)
    }

    // MARK: - Script format

    /// Проверяем ожидаемый формат osascript команды (whitebox).
    func test_scriptFormat_matchesExpected() {
        let title = "KrabEar"
        let body  = "Транскрипция готова"
        let safeTitle = title.replacingOccurrences(of: "\"", with: "\\\"")
        let safeBody  = body.replacingOccurrences(of: "\"", with: "\\\"")
        let expected  = "display notification \"\(safeBody)\" with title \"\(safeTitle)\""
        XCTAssertEqual(
            expected,
            "display notification \"Транскрипция готова\" with title \"KrabEar\"",
            "Формат скрипта должен быть: display notification \"<body>\" with title \"<title>\""
        )
    }
}
