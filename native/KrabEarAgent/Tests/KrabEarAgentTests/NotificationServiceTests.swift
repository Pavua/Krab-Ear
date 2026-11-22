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

        // E4 п.1: текст идёт ОТДЕЛЬНЫМИ argv-аргументами (после `--`), а не склеивается
        // в исходник AppleScript — экранирование больше не нужно и обойти его нельзя.
        XCTAssertEqual(
            runner.calls,
            [
                .init(
                    executableURL: URL(fileURLWithPath: "/usr/bin/osascript"),
                    arguments: NotificationService.scriptArguments(
                        title: #"Krab "Ear""#, body: #"Тело "уведомления""#
                    )
                ),
            ]
        )
        XCTAssertEqual(
            runner.calls.first?.arguments,
            [
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run",
                "--", #"Тело "уведомления""#, #"Krab "Ear""#,
            ]
        )
    }

    /// E4 п.1: `\"` в тексте раньше закрывало строку AppleScript и позволяло
    /// подставить `do shell script`. Теперь ни один символ текста не попадает в исходник скрипта.
    func test_notify_hostileText_neverReachesScriptSource() {
        let runner = RecordingNotificationProcessRunner()
        let service = NotificationService(processRunner: runner)
        let hostile = #"\" & (do shell script "touch /tmp/pwned") & ""#

        service.notify(title: hostile, body: hostile)

        guard let args = runner.calls.first?.arguments else {
            return XCTFail("runner не вызван")
        }
        let separator = args.firstIndex(of: "--")
        XCTAssertNotNil(separator, "текст обязан идти после `--`, иначе body вида -x читается как опция osascript")
        let scriptParts = zip(args, args.dropFirst()).filter { $0.0 == "-e" }.map { $0.1 }
        for part in scriptParts {
            XCTAssertFalse(part.contains("do shell script"), "текст попал в исходник скрипта: \(part)")
        }
        if let separator {
            XCTAssertEqual(Array(args[(separator + 1)...]), [hostile, hostile])
        }
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

    // MARK: - Передача текста (argv, без экранирования)

    /// Спецсимволы идут в runner дословно: ни кавычки, ни обратный слэш, ни перевод строки
    /// не меняются и не попадают в исходник скрипта.
    func test_text_passedVerbatimAsArguments() {
        let runner = RecordingNotificationProcessRunner()
        let service = NotificationService(processRunner: runner)
        let samples = [
            "Say \"Hello\"", "Path: C:\\Users\\test", "Line 1\nLine 2", "-e x",
            "日本語テスト", "🎤 Готово ✅", "\\",
        ]
        for raw in samples {
            service.notify(title: raw, body: raw)
        }
        XCTAssertEqual(runner.calls.count, samples.count)
        for (call, raw) in zip(runner.calls, samples) {
            XCTAssertEqual(Array(call.arguments.suffix(2)), [raw, raw])
            XCTAssertEqual(call.arguments.suffix(3).first, "--")
        }
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
