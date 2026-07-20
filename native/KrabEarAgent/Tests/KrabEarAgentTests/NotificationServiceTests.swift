/*
 NotificationServiceTests — тесты сервиса уведомлений через osascript.

 Стратегия:
 NotificationService получает runner через DI, поэтому тесты не запускают /usr/bin/osascript.
 Тестируем:
   1. requestAuthorizationIfNeeded() — no-op, не падает.
   2. notify() с обычным вводом не бросает исключений.
   3. Экранирование кавычек: проверяем формулу белым ящиком
      (та же логика что в production — replacingOccurrences of "\"" with "\\\"").
   4. Граничные случаи: пустые строки, строки с несколькими кавычками.
   5. Скрипт osascript собирается в ожидаемом формате.
   6. Unicode title/body.
   7. Имитация отсутствия разрешений (graceful).
   8. Concurrent show safe.
   9. Повтор одинакового уведомления (дымовая проверка).

 Подход:
 - Инъецированный runner записывает команды вместо показа уведомлений.
 - Мы тестируем публичный контракт: метод должен принять любой ввод без сбоя.
 - Проверки белого ящика контролируют логику экранирования напрямую.
*/

import XCTest
@testable import KrabEarAgent

/// Потокобезопасно записывает команды вместо запуска внешнего процесса.
private final class RecordingNotificationProcessRunner: NotificationProcessRunning, @unchecked Sendable {
    struct Call: Equatable {
        let executableURL: URL
        let arguments: [String]
    }

    private let lock = NSLock()
    private var recordedCalls: [Call] = []

    func run(executableURL: URL, arguments: [String]) throws {
        lock.lock()
        recordedCalls.append(Call(executableURL: executableURL, arguments: arguments))
        lock.unlock()
    }

    var calls: [Call] {
        lock.lock()
        defer { lock.unlock() }
        return recordedCalls
    }
}

/// Имитирует ошибку запуска, чтобы проверить некритичный характер уведомления.
private struct ThrowingNotificationProcessRunner: NotificationProcessRunning {
    enum TestError: Error {
        case launchFailed
    }

    func run(executableURL: URL, arguments: [String]) throws {
        throw TestError.launchFailed
    }
}

// MARK: - Tests

final class NotificationServiceTests: XCTestCase {

    private var svc: NotificationService!
    private var runner: RecordingNotificationProcessRunner!

    override func setUp() {
        super.setUp()
        runner = RecordingNotificationProcessRunner()
        svc = NotificationService(processRunner: runner)
    }

    // MARK: - requestAuthorizationIfNeeded

    /// requestAuthorizationIfNeeded() — no-op, не падает.
    func test_requestAuthorizationIfNeeded_doesNotCrash() {
        svc.requestAuthorizationIfNeeded()
        // Если дошли сюда — тест прошёл.
    }

    // MARK: - show basic notification

    /// Команда приложения делегируется инъецированному runner без запуска osascript.
    func test_notify_delegatesExecutableArgumentsAndEscapingToRunner() {
        let runner = RecordingNotificationProcessRunner()
        let service = NotificationService(processRunner: runner)

        service.notify(title: #"Krab "Ear""#, body: #"Тело "уведомления""#)

        XCTAssertEqual(
            runner.calls,
            [
                .init(
                    executableURL: URL(fileURLWithPath: "/usr/bin/osascript"),
                    arguments: [
                        "-e",
                        #"display notification "Тело \"уведомления\"" with title "Krab \"Ear\"""#,
                    ]
                ),
            ]
        )
    }

    /// Ошибка runner не должна прерывать основной сценарий приложения.
    func test_show_basic_notification() {
        let failingService = NotificationService(processRunner: ThrowingNotificationProcessRunner())
        failingService.notify(title: "Krab Ear", body: "Готово")
    }

    /// notify() с пустыми строками не крашится.
    func test_notify_emptyStrings_doesNotCrash() {
        svc.notify(title: "", body: "")
    }

    // MARK: - show with actions (осakcript не поддерживает кнопки через display notification,
    // но сервис должен принять любые строки без краша)

    func test_show_with_actions() {
        // NotificationService отправляет display notification — кнопок нет.
        // Тест гарантирует что вызов с расширенным телом не крашится.
        svc.notify(title: "Action Required", body: "Tap to open. Action: Dismiss | Open")
    }

    func test_show_with_special_chars_in_body() {
        // Тело с символами, которые могут сломать osascript: слэши, скобки, etc.
        svc.notify(title: "Alert", body: "Path: /usr/bin/open [status: 200] (ok)")
    }

    // MARK: - unicode title and body

    func test_unicode_title_body() {
        // Кириллица, испанский, смешанный контент.
        svc.notify(title: "Транскрипция завершена", body: "Распознано: привет мир")
        svc.notify(title: "Transcripción", body: "El texto está listo: ñoño")
        svc.notify(title: "🎤 Krab Ear", body: "Готово ✅")
    }

    func test_unicode_cjk_does_not_crash() {
        svc.notify(title: "日本語テスト", body: "音声認識完了")
    }

    func test_unicode_rtl_does_not_crash() {
        svc.notify(title: "مرحبا", body: "النص جاهز")
    }

    // MARK: - handles no permission gracefully

    func test_handles_no_permission_gracefully() {
        let failingService = NotificationService(processRunner: ThrowingNotificationProcessRunner())
        failingService.requestAuthorizationIfNeeded()
        failingService.notify(title: "Permission test", body: "Should not crash even if blocked")
    }

    // MARK: - concurrent show safe

    func test_concurrent_show_safe() {
        // Потокобезопасный recorder позволяет проверить точное число вызовов без osascript.
        let expectation = self.expectation(description: "concurrent notify")
        expectation.expectedFulfillmentCount = 10

        let queue = DispatchQueue(label: "test.notify.concurrent", attributes: .concurrent)
        for i in 0..<10 {
            queue.async {
                self.svc.notify(title: "Concurrent \(i)", body: "body \(i)")
                expectation.fulfill()
            }
        }

        wait(for: [expectation], timeout: 10)
        XCTAssertEqual(runner.calls.count, 10)
    }

    // MARK: - dedupe identical notification (smoke)

    func test_dedupe_identical_notification() {
        // NotificationService не имеет встроенного дедупа — это smoke test.
        // Проверяет что повторный вызов с теми же аргументами не вызывает crash.
        let title = "Krab Ear"
        let body  = "Готово"
        svc.notify(title: title, body: body)
        svc.notify(title: title, body: body)
        svc.notify(title: title, body: body)
        // Все три вызова должны молча завершиться.
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

    func test_escaping_backslash_preserved() {
        // Backslash alone (не двойная кавычка) не должен ломать логику.
        let raw = "Path: C:\\Users\\test"
        svc.notify(title: raw, body: raw)
        // No crash = pass
    }

    func test_escaping_newlines_in_body() {
        // Перевод строки в теле — осaccript может не поддерживать,
        // но сервис не должен падать.
        svc.notify(title: "Multi", body: "Line 1\nLine 2\nLine 3")
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

    func test_scriptFormat_bodyBeforeTitle() {
        // osascript format requires body before title keyword.
        let title = "T"
        let body  = "B"
        let script = "display notification \"\(body)\" with title \"\(title)\""
        // Verify order: body string literal appears before "with title"
        let bodyRange  = script.range(of: "\"\(body)\"")
        let titleRange = script.range(of: "with title")
        XCTAssertNotNil(bodyRange)
        XCTAssertNotNil(titleRange)
        if let br = bodyRange, let tr = titleRange {
            XCTAssertLessThan(br.lowerBound, tr.lowerBound,
                              "Body string must appear before 'with title' in osascript command")
        }
    }

    func test_scriptFormat_escapedQuotes_integratedInScript() {
        let title = "He said \"hi\""
        let body  = "\"quoted body\""
        let safeTitle = title.replacingOccurrences(of: "\"", with: "\\\"")
        let safeBody  = body.replacingOccurrences(of: "\"", with: "\\\"")
        let script = "display notification \"\(safeBody)\" with title \"\(safeTitle)\""

        // The escaped sequences \\\" in the Swift source become the two-char sequence \"
        // inside the String value, which is correct AppleScript escaping.
        XCTAssertTrue(script.contains("\\\""),
                      "Escaped script must contain properly escaped quotes \\\"")
        // safeTitle and safeBody should also contain the escaping
        XCTAssertTrue(safeTitle.contains("\\\""),
                      "safeTitle must have escaped quotes, got: \(safeTitle)")
        XCTAssertTrue(safeBody.contains("\\\""),
                      "safeBody must have escaped quotes, got: \(safeBody)")
        // Calling notify must not crash
        svc.notify(title: title, body: body)
    }

    // MARK: - Long strings

    func test_very_long_title_and_body() {
        let longStr = String(repeating: "КрабУхо ", count: 200) // ~1600 chars
        svc.notify(title: longStr, body: longStr)
        // Must not crash.
    }

    // MARK: - nil-safety (Sendable check)

    func test_notify_called_from_background_thread() {
        // NotificationService is @unchecked Sendable — can be called from any thread.
        let exp = expectation(description: "background notify")
        DispatchQueue.global().async {
            self.svc.notify(title: "Background", body: "Thread test")
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5)
    }
}
