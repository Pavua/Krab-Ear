/*
 IPCClientReconnectE2ETests — E2E integration tests for Phase C C.2 IPC reconnect.

 Verifies that `callWithReconnect` exercises real exponential-backoff retry logic
 using a `FakeIPCTransport` that fails N times then succeeds.  No Python backend
 process required — the fake implements `IPCSocketProviding` in-process.

 Детерминизм (2026-08-08). Тесты НЕ зависят ни от настенных часов, ни от порядка
 планирования потоков:
   1) Пауза бэкоффа инжектится (`IPCBackoffSleeping`) — фейк записывает запрошенные
      интервалы и возвращается мгновенно. Расписание ретраев проверяется по
      записанным задержкам, а не по замеру elapsed (старая версия мерила
      `ContinuousClock` и падала под нагрузкой, когда 750 мс сна растягивались).
   2) Фейк считает вызовы ПО ИМЕНИ МЕТОДА. `callWithReconnect` после удачного
      ретрая шлёт fire-and-forget `report_reconnect` через тот же провайдер, и
      общий счётчик догонялся этой detached-задачей уже после `return` —
      ассерт «3 попытки» гонялся с телеметрией и наблюдал 4.

 Test cases:
   1. recovers after 2 failures — 3 вызова `ping`, задержки [0.25, 0.5]
   2. fails after max 5 retries — 6 вызовов, все 5 задержек, throws on exhaustion
   3. non-transient errors rethrown immediately — 1 вызов, ни одной задержки
   4. telemetry report_reconnect отправляется после удачного ретрая
*/

import XCTest
@testable import KrabEarAgent

// MARK: - RecordingBackoffSleeper

/// Мгновенная замена `Task.sleep` для `callWithReconnect`: ничего не ждёт,
/// только записывает запрошенные интервалы. Тест проверяет РАСПИСАНИЕ бэкоффа
/// вместо реально проведённого времени.
final class RecordingBackoffSleeper: @unchecked Sendable {

    private let lock = NSLock()
    private var storedDelays: [TimeInterval] = []

    /// Интервалы, которые `callWithReconnect` запросил, в порядке запроса.
    var recordedDelays: [TimeInterval] {
        lock.withLock { storedDelays }
    }

    /// Значение для параметра `backoffSleep:` инициализатора `IPCClient`.
    var sleepFunction: IPCBackoffSleeping {
        { [self] seconds in
            lock.withLock { storedDelays.append(seconds) }
        }
    }
}

// MARK: - FakeIPCTransport

/// In-process фейк транспорта: разбирает имя метода из JSON-пейлоада (тот же
/// подход, что у `TooltipMockSocketProvider`) и ведёт счёт вызовов ПО МЕТОДУ.
///
/// Учёт по методу обязателен: телеметрия `report_reconnect` уходит через этот же
/// провайдер из detached-задачи, и общий счётчик был бы недетерминирован.
final class FakeIPCTransport: IPCSocketProviding, @unchecked Sendable {

    private let lock = NSLock()
    private var callsByMethod: [String: Int] = [:]

    /// Сколько первых вызовов каждого метода должны упасть транзиентной ошибкой.
    private let transientFailures: [String: Int]

    /// Методы, отвечающие фатальной (не-транзиентной) ошибкой на любой вызов.
    private let fatalErrorMethods: Set<String>

    /// Наблюдатель: вызывается с именем метода на каждом запросе (для expectation).
    private let onCall: (@Sendable (String) -> Void)?

    init(
        transientFailures: [String: Int] = [:],
        fatalErrorMethods: Set<String> = [],
        onCall: (@Sendable (String) -> Void)? = nil
    ) {
        self.transientFailures = transientFailures
        self.fatalErrorMethods = fatalErrorMethods
        self.onCall = onCall
    }

    /// Сколько раз запрашивался конкретный IPC-метод.
    func callCount(for method: String) -> Int {
        lock.withLock { callsByMethod[method] ?? 0 }
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        let method = Self.extractMethod(from: payload) ?? "<unparsed>"
        let attemptForThisMethod: Int = lock.withLock {
            let next = (callsByMethod[method] ?? 0) + 1
            callsByMethod[method] = next
            return next
        }
        onCall?(method)

        if fatalErrorMethods.contains(method) {
            throw IPCError.backendError("method_not_found")
        }

        if attemptForThisMethod <= (transientFailures[method] ?? 0) {
            throw IPCError.socketConnectFailed(
                "fake: backend not ready (\(method) attempt \(attemptForThisMethod))"
            )
        }

        let response = #"{"id":"fake-1","ok":true,"result":{}}"# + "\n"
        return Data(response.utf8)
    }

    private static func extractMethod(from payload: Data) -> String? {
        guard
            let dict = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
            let method = dict["method"] as? String
        else { return nil }
        return method
    }
}

// MARK: - IPCClientReconnectE2ETests

final class IPCClientReconnectE2ETests: XCTestCase {

    // MARK: 1. Recovers after 2 transient failures

    /// `callWithReconnect` должен успешно завершиться после двух транзиентных
    /// отказов, запросив ровно первые две паузы бэкоффа.
    func testCallWithReconnect_recoversAfter2Failures() async throws {
        let transport = FakeIPCTransport(transientFailures: ["ping": 2])
        let sleeper = RecordingBackoffSleeper()
        let client = IPCClient(socketProvider: transport, backoffSleep: sleeper.sleepFunction)

        let result = try await client.callWithReconnect(method: "ping", params: [:])

        XCTAssertEqual(result["ok"] as? Bool, true, "Expected ok:true in success response")

        // 2 отказа → две паузы перед попытками 2 и 3, ровно первые две из таблицы.
        XCTAssertEqual(
            sleeper.recordedDelays,
            Array(IPCClient.backoffDelays.prefix(2)),
            "Ожидались ровно первые две задержки таблицы бэкоффа"
        )
        XCTAssertEqual(
            sleeper.recordedDelays,
            [0.25, 0.5],
            "Расписание бэкоффа изменилось — обнови таблицу осознанно"
        )

        // 2 отказа + 1 успех = 3 вызова `ping`; телеметрия считается отдельно.
        XCTAssertEqual(
            transport.callCount(for: "ping"),
            3,
            "Expected 3 provider calls (2 failures + 1 success)"
        )
    }

    // MARK: 2. Fails after exhausting max retries

    /// Когда backend не поднимается, `callWithReconnect` обязан израсходовать все
    /// 5 ретраев (6 попыток) и бросить последнюю транзиентную ошибку.
    func testCallWithReconnect_failsAfterMaxRetries() async throws {
        // 999 > 6 попыток — backend не возвращается.
        let transport = FakeIPCTransport(transientFailures: ["ping": 999])
        let sleeper = RecordingBackoffSleeper()
        let client = IPCClient(socketProvider: transport, backoffSleep: sleeper.sleepFunction)

        var caughtError: Error?
        do {
            _ = try await client.callWithReconnect(method: "ping", params: [:])
            XCTFail("Expected an error after exhausting all retries")
        } catch {
            caughtError = error
        }

        XCTAssertNotNil(caughtError, "Expected error on exhaustion")
        if let ipcErr = caughtError as? IPCError {
            XCTAssertTrue(
                ipcErr.isTransient,
                "Exhausted transient retries — last error should be transient, got: \(ipcErr)"
            )
        } else {
            XCTFail("Expected IPCError, got: \(String(describing: caughtError))")
        }

        XCTAssertEqual(
            transport.callCount(for: "ping"),
            6,
            "Expected 6 provider calls (1 initial + 5 retries)"
        )
        XCTAssertEqual(
            sleeper.recordedDelays,
            IPCClient.backoffDelays,
            "Должны быть запрошены все паузы таблицы, по одной перед каждым ретраем"
        )
    }

    // MARK: 3. Non-transient errors are rethrown immediately (no retry)

    /// Фатальная (не-транзиентная) ошибка backend не должна запускать бэкофф —
    /// она пробрасывается после первой же попытки.
    func testCallWithReconnect_nonTransientErrors_rethrownImmediately() async throws {
        let transport = FakeIPCTransport(fatalErrorMethods: ["ping"])
        let sleeper = RecordingBackoffSleeper()
        let client = IPCClient(socketProvider: transport, backoffSleep: sleeper.sleepFunction)

        var caughtError: Error?
        do {
            _ = try await client.callWithReconnect(method: "ping", params: [:])
            XCTFail("Expected a fatal error to be rethrown")
        } catch {
            caughtError = error
        }

        XCTAssertNotNil(caughtError, "Expected error on fatal backend response")

        if let ipcErr = caughtError as? IPCError {
            XCTAssertFalse(
                ipcErr.isTransient,
                "Non-transient errors must not trigger retries: \(ipcErr)"
            )
        } else {
            XCTFail("Expected IPCError, got: \(String(describing: caughtError))")
        }

        XCTAssertEqual(
            transport.callCount(for: "ping"),
            1,
            "Expected exactly 1 provider call — non-transient errors must not be retried"
        )
        XCTAssertTrue(
            sleeper.recordedDelays.isEmpty,
            "Без ретраев не должно быть ни одной паузы бэкоффа: \(sleeper.recordedDelays)"
        )
    }

    // MARK: 4. Reconnect telemetry

    /// После удачного ретрая клиент шлёт best-effort `report_reconnect`.
    /// Ждём событие (expectation), а не заданное время — именно эта detached-задача
    /// раньше гонялась с ассертом счётчика попыток.
    func testCallWithReconnect_reportsReconnectTelemetryAfterRecovery() async throws {
        let telemetrySent = expectation(description: "report_reconnect отправлен")
        let transport = FakeIPCTransport(
            transientFailures: ["ping": 1],
            onCall: { method in
                if method == "report_reconnect" { telemetrySent.fulfill() }
            }
        )
        let sleeper = RecordingBackoffSleeper()
        let client = IPCClient(socketProvider: transport, backoffSleep: sleeper.sleepFunction)

        _ = try await client.callWithReconnect(method: "ping", params: [:])

        await fulfillment(of: [telemetrySent], timeout: 10)
        // Клиент захвачен detached-задачей слабо — держим ссылку живой до доставки.
        withExtendedLifetime(client) {}

        XCTAssertEqual(transport.callCount(for: "report_reconnect"), 1)
        XCTAssertEqual(
            transport.callCount(for: "ping"),
            2,
            "Телеметрия не должна учитываться как попытка целевого метода"
        )
    }
}
