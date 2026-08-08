/*
 IPCReconnectTelemetryTests.swift

 Тесты телеметрии `report_reconnect` — сигнала «IPC-вызов пережил перезапуск
 backend'а», который до 2026-08-08 не отправлялся НИ РАЗУ.

 История: экспоненциальный бэкофф-реконнект IPCClient.callWithReconnect (без
 бэктиков — метод удалён; Phase C C.2, PR #367) нёс эту телеметрию внутри, но не имел
 ни одного продового вызова с самого рождения — за 15 дней ДО него в репозитории
 уже жил `callWithRecovery` (PR #96), который на той же ошибке не спит, а зовёт
 `restartIfDead()`, то есть устраняет причину вместо ожидания симптома. Мёртвый
 дубликат удалён, телеметрия переехала в живой путь восстановления.

 Стратегия: `IPCRecoveryTests` вынужденно тестирует РЕПЛИКУ логики
 (`AgentAppDelegate` — @MainActor и требует NSApplication). Реплика не заметила
 бы, что прод забыл позвать телеметрию, поэтому здесь:

 1. Поведенческий тест бьёт по ЖИВОМУ `IPCReconnectTelemetry.report` через
    инъекцию `IPCSocketProviding` — без сокета и без NSApplication.
 2. Source-контракт грепает реальный main+IPCRecovery.swift на наличие вызовов
    в ОБОИХ путях восстановления — тот же приём, что
    `MainErrorsWiringTests.test_setupErrorBus_is_actually_called_from_startup`
    после инцидента с мёртвым setupErrorBus (2026-07-05).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Мок-транспорт, записывающий отправленные запросы

/// Разбирает JSON каждого отправленного payload'а и складывает в потокобезопасный
/// буфер. Отвечает валидным `{"ok": true}`, чтобы клиент не бросил.
private final class RecordingMockProvider: IPCSocketProviding, @unchecked Sendable {
    private let lock = NSLock()
    private var _requests: [[String: Any]] = []

    /// Когда true — транспорт падает, проверяя best-effort семантику телеметрии.
    private let shouldFail: Bool

    init(shouldFail: Bool = false) {
        self.shouldFail = shouldFail
    }

    var requests: [[String: Any]] {
        lock.withLock { _requests }
    }

    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        if let json = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] {
            lock.withLock { _requests.append(json) }
        }
        if shouldFail {
            throw IPCError.socketConnectFailed("mock transport down")
        }
        return Data("{\"id\":\"1\",\"ok\":true,\"result\":{}}\n".utf8)
    }
}

// MARK: - Тесты

final class IPCReconnectTelemetryTests: XCTestCase {

    // MARK: 1. Поведение живого кода

    /// Телеметрия уходит методом `report_reconnect` с attempts + duration_ms.
    func test_report_sends_report_reconnect_with_attempts_and_duration() async {
        let provider = RecordingMockProvider()
        let client = IPCClient(socketProvider: provider)

        await IPCReconnectTelemetry.report(client: client, attempts: 1, durationMs: 1500)

        let requests = provider.requests
        XCTAssertEqual(requests.count, 1, "Телеметрия должна уйти ровно одним вызовом")

        let request = requests.first ?? [:]
        XCTAssertEqual(
            request["method"] as? String, "report_reconnect",
            "Backend-хендлер называется report_reconnect (service.py) — имя контрактное"
        )

        let params = request["params"] as? [String: Any] ?? [:]
        XCTAssertEqual(params["attempts"] as? Int, 1)
        XCTAssertEqual(
            params["duration_ms"] as? Int, 1500,
            "Ключ duration_ms — snake_case, как ждёт _handle_report_reconnect"
        )
    }

    /// Телеметрия — best-effort: упавший транспорт НЕ должен выбрасывать наружу.
    /// Иначе сбой необязательной метрики утопил бы успешно восстановленный вызов.
    func test_report_never_throws_when_transport_fails() async {
        let provider = RecordingMockProvider(shouldFail: true)
        let client = IPCClient(socketProvider: provider)

        // Отсутствие `try` в вызове — уже часть контракта: report не бросает.
        await IPCReconnectTelemetry.report(client: client, attempts: 2, durationMs: 90)

        XCTAssertEqual(
            provider.requests.count, 1,
            "Попытка отправки должна быть сделана даже при заведомо мёртвом транспорте"
        )
    }

    // MARK: 2. Source-контракт — телеметрия реально подключена к обоим путям

    /// Оба пути восстановления обязаны репортить. Асимметрия «один путь научился,
    /// сиблинг нет» — рецидивирующий класс багов в этом репозитории.
    func test_both_recovery_paths_actually_call_telemetry() throws {
        let src = try String(contentsOf: Self.ipcRecoverySourceURL, encoding: .utf8)

        // Оба пути идут через общий helper — считаем его call sites (объявление
        // написано как `since startedAt:` и в подсчёт не попадает).
        let helperCallSites = src.components(separatedBy: "reportReconnect(since: startedAt)").count - 1
        XCTAssertEqual(
            helperCallSites, 2,
            "И callWithRecovery, и callAsyncWithRecovery обязаны слать report_reconnect " +
            "после успешного повтора — иначе телеметрия снова станет декоративной."
        )

        // …а сам helper обязан реально дёргать телеметрию, а не быть пустышкой.
        XCTAssertTrue(
            src.contains("IPCReconnectTelemetry.report"),
            "reportReconnect(since:) обязан звать IPCReconnectTelemetry.report"
        )
    }

    /// Телеметрия не должна ходить через сам recovery-путь: рекурсия и ретрай
    /// необязательной метрики не нужны, у неё свой быстрый таймаут.
    func test_telemetry_does_not_route_through_recovery() throws {
        let src = try String(contentsOf: Self.telemetrySourceURL, encoding: .utf8)
        XCTAssertFalse(
            src.contains("WithRecovery("),
            "IPCReconnectTelemetry обязана звать сырой callAsync, не recovery-обёртку"
        )
    }

    // MARK: - Резолв путей к исходникам (паттерн MainErrorsWiringTests)

    private static var ipcRecoverySourceURL: URL {
        sourceURL(named: "main+IPCRecovery.swift")
    }

    private static var telemetrySourceURL: URL {
        sourceURL(named: "IPCReconnectTelemetry.swift")
    }

    private static func sourceURL(named name: String) -> URL {
        let bundleURL = Bundle(for: IPCReconnectTelemetryTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        return URL(fileURLWithPath: #file)
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
