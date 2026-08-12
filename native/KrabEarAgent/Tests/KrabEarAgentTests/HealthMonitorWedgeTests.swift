/*
 HealthMonitorWedgeTests — вторая ступень self-heal: ЗАКЛИНИВШИЙ backend.

 🔴 Живой инцидент 2026-08-07. Прод-процесс восемь часов был жив, жёг CPU и
 отвергал IPC-соединения, а self-heal сделал РОВНО ОДНУ безрезультатную попытку
 и замолчал навсегда: onHangDetected одноразов на эпизод и сбрасывается только
 успешным ping'ом, а его обработчик зовёт restartIfDeadDetailed(), который на
 живом процессе возвращает .alreadyAlive и не делает ничего. Класс
 «sticky state without an exit».

 Эскалация намеренно консервативна и требует ДВУХ независимых подтверждений,
 потому что цена ложного срабатывания — потерянная диктовка (инцидент
 2026-07-22, kickstart под активной записью):
   1) проба сказала, что соединение ОТВЕРГАЕТСЯ (а не таймаутит);
   2) backend хотя бы раз ответил в этом эпизоде — иначе «не отвечает» означает
      «ещё грузится» (импорт torch под свопом занимает минуты).
*/

import XCTest
@testable import KrabEarAgent

/// Потокобезопасный счётчик вызовов для @Sendable-колбэков.
final class WedgeCallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func bump() {
        lock.lock(); value += 1; lock.unlock()
    }

    var count: Int {
        lock.lock(); defer { lock.unlock() }
        return value
    }
}

/// Провайдер ping'а: сначала N успехов, потом всегда провал.
final class PingScript: @unchecked Sendable {
    private let lock = NSLock()
    private var remainingSuccesses: Int

    init(successes: Int) { self.remainingSuccesses = successes }

    func next() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if remainingSuccesses > 0 {
            remainingSuccesses -= 1
            return true
        }
        return false
    }
}

/// Предикат «это доказательство заклинивания» — решение безопасности:
/// `true` ведёт к принудительному рестарту, а тот под активной диктовкой
/// уничтожает её безвозвратно.
final class BackendWedgeEvidenceTests: XCTestCase {

    /// Отказ на connect() — единственное доказательство заклинивания.
    func testConnectFailureIsWedgeEvidence() {
        XCTAssertTrue(isBackendWedgeEvidence(IPCError.socketConnectFailed("ECONNREFUSED")))
    }

    /// 🔴 Исчерпан лимит коннектов — backend ЗДОРОВ, просто перегружен.
    ///
    /// `ipc_server.py` при лимите делает `accept()` и сразу `conn.close()`,
    /// поэтому клиент СОЕДИНЯЕТСЯ успешно и падает уже на чтении. В логах прода
    /// 2026-08-07 это реально было («лимит 64 коннектов исчерпан»). Считать это
    /// заклиниванием — значит убивать здоровый backend под нагрузкой вместе с
    /// идущей диктовкой.
    func testConnectionLimitRejectionIsNotWedgeEvidence() {
        XCTAssertFalse(
            isBackendWedgeEvidence(IPCError.readFailed),
            "отказ по лимиту коннектов принят за зависание — рестарт убьёт диктовку"
        )
        XCTAssertFalse(isBackendWedgeEvidence(IPCError.writeFailed))
    }

    /// Таймаут — backend жив, просто медленный (под свопом RTT доходил до 2.9с).
    func testTimeoutIsNotWedgeEvidence() {
        XCTAssertFalse(isBackendWedgeEvidence(IPCError.timeout))
    }

    /// Локальная невозможность создать сокет — проблема агента, не backend'а.
    func testLocalSocketCreateFailureIsNotWedgeEvidence() {
        XCTAssertFalse(isBackendWedgeEvidence(IPCError.socketCreateFailed(errno: 24)))
    }

    /// Ответ получен (пусть и с ошибкой) — accept-loop жив по определению.
    func testAnsweredRequestsAreNotWedgeEvidence() {
        XCTAssertFalse(isBackendWedgeEvidence(IPCError.backendError("boom")))
        XCTAssertFalse(isBackendWedgeEvidence(IPCError.invalidResponse))
    }

    /// Чужая ошибка (не IPC) доказательством не является.
    func testNonIPCErrorIsNotWedgeEvidence() {
        struct Other: Error {}
        XCTAssertFalse(isBackendWedgeEvidence(Other()))
    }
}

/// Source-контракт: вторая ступень реально проводится в продовом старте.
///
/// 🔴 В этом проекте уже был класс багов «функция определена, но её никогда не
/// зовут» — `setupErrorBus` и сам `setupHealthMonitor` были МЁРТВЫМИ при 100%
/// зелёных юнит-тестах, потому что тесты проверяли компоненты в изоляции.
/// Детектор заклинивания без проводки — такой же мёртвый код.
final class WedgeWiringSourceContractTests: XCTestCase {

    /// Текст между двумя маркерами — устойчиво к длине тела.
    static func regionBetween(_ src: String, from: String, to: String) -> String {
        guard let start = src.range(of: from) else { return "" }
        let tail = src[start.upperBound...]
        guard let end = tail.range(of: to) else { return String(tail) }
        return String(tail[..<end.lowerBound])
    }

    private func healthMonitorWiringSource() throws -> String {
        var url = URL(fileURLWithPath: #file)
        for _ in 0..<6 {
            let candidate = url.appendingPathComponent(
                "Sources/KrabEarAgent/main+HealthMonitor.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        XCTFail("не нашли main+HealthMonitor.swift")
        return ""
    }

    func test_wedge_probe_is_wired_in_production_startup() throws {
        let src = try healthMonitorWiringSource()
        XCTAssertTrue(
            src.contains("setWedgeProbe"),
            "проба заклинивания не проводится — детектор мёртв"
        )
        XCTAssertTrue(
            src.contains("setOnWedgeDetected"),
            "обработчик заклинивания не проводится — детектор мёртв"
        )
    }

    /// Лечить обязан forceRestartBackend: restartIfDeadDetailed на ЖИВОМ
    /// процессе возвращает .alreadyAlive и ничего не делает — именно поэтому
    /// он не вылечил инцидент 2026-08-07.
    func test_escalation_uses_force_restart_not_restart_if_dead() throws {
        let src = try healthMonitorWiringSource()
        // Регион, а НЕ префикс фиксированной длины: окно на 2000 символов
        // сломалось от одного добавленного гарда — хрупкий тест краснеет на
        // безобидной правке и приучает игнорировать себя.
        let body = Self.regionBetween(src, from: "setOnWedgeDetected", to: "await monitor.start()")
        XCTAssertTrue(
            body.contains("forceRestartBackend"),
            "эскалация не зовёт forceRestartBackend — на живом процессе лечения не будет"
        )
    }

    /// Рейт-лимит обязан быть: без него подтверждённое заклинивание
    /// перезапускало бы backend каждую минуту.
    /// Весь фикс HIGH прошлого раунда держится на ОДНОЙ строке в пробе.
    /// В проекте уже был случай отката тела при зелёных тестах — отсюда гард.
    func test_probe_checks_process_age() throws {
        let src = try healthMonitorWiringSource()
        let body = Self.regionBetween(src, from: "setWedgeProbe", to: "setOnHealthyPing")
        XCTAssertTrue(
            body.contains("backendProcessAgeSeconds"),
            "проба не проверяет возраст процесса — молодой (загружающийся) "
            + "backend снова будет принят за заклинивший"
        )
    }

    /// Живая встреча не выставляет isRecording/activeGenerationOwner —
    /// её надо проверять отдельно (амендмент 2026-07-16, потерянный item).
    func test_escalation_guards_live_meeting() throws {
        let src = try healthMonitorWiringSource()
        XCTAssertTrue(
            src.contains("isMeetingLive"),
            "эскалация не смотрит на живую встречу — kickstart посреди неё"
        )
    }

    func test_escalation_is_rate_limited() throws {
        let src = try healthMonitorWiringSource()
        XCTAssertTrue(
            src.contains("WedgeEscalationGate") && src.contains("shouldEscalate"),
            "принудительный рестарт без рейт-лимита — карусель рестартов"
        )
    }
}

/// Циклический паттерн ответов ping'а.
final class PingPhases: @unchecked Sendable {
    private let lock = NSLock()
    private let pattern: [Bool]
    private var index = 0

    init(pattern: [Bool]) { self.pattern = pattern }

    func next() -> Bool {
        lock.lock(); defer { lock.unlock() }
        let value = pattern[index % pattern.count]
        index += 1
        return value
    }
}

/// Парсер `ps -o etime=`. 🔴 На macOS НЕТ `etimes` (это GNU) — запрос такого
/// поля печатает список ключей, и «возраст» молча становится мусором.
final class ProcessElapsedParsingTests: XCTestCase {

    func testParsesMinutesAndSeconds() {
        XCTAssertEqual(parseProcessElapsedSeconds("05:30"), 330)
        XCTAssertEqual(parseProcessElapsedSeconds("  00:07 "), 7)
    }

    func testParsesHours() {
        XCTAssertEqual(parseProcessElapsedSeconds("01:01:27"), 3687)
    }

    func testParsesDays() {
        XCTAssertEqual(parseProcessElapsedSeconds("2-03:00:00"), 2 * 86400 + 3 * 3600)
    }

    /// Мусор обязан давать nil, а не число: nil означает «возраст неизвестен»,
    /// и вызывающая сторона тогда НЕ эскалирует (fail-safe).
    func testGarbageIsNilNotANumber() {
        XCTAssertNil(parseProcessElapsedSeconds(""))
        XCTAssertNil(parseProcessElapsedSeconds("   "))
        XCTAssertNil(parseProcessElapsedSeconds("%cpu%memacflag"))
        XCTAssertNil(parseProcessElapsedSeconds("12"))
        XCTAssertNil(parseProcessElapsedSeconds("aa:bb"))
    }
}

final class HealthMonitorWedgeTests: XCTestCase {

    /// Порог заклинивания ещё не набран — эскалации быть не должно.
    func testNoEscalationBeforeWedgeThreshold() async {
        let escalations = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 100, wedgeReprobeInterval: 0.0
        )
        let script = PingScript(successes: 1)   // 🔴 вне замыкания: внутри
        monitor.setPingProvider { script.next() }  // конструировался новый на
                                                   // каждый вызов и отказы не
                                                   // накапливались вовсе
        await monitor.setWedgeProbe { true }
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 300_000_000)
        await monitor.stop()

        XCTAssertEqual(escalations.count, 0, "эскалация до достижения порога")
    }

    /// 🔴 Backend НИ РАЗУ не ответил — это старт под нагрузкой, а не зависание.
    /// Рестартовать его значит зациклить загрузку, которая и так идёт минутами.
    func testNoEscalationWhenBackendWasNeverHealthy() async {
        let escalations = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { false }          // не отвечал никогда
        await monitor.setWedgeProbe { true }       // и соединение отвергается
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await monitor.stop()

        XCTAssertEqual(
            escalations.count, 0,
            "убили бы медленно стартующий backend, приняв загрузку за зависание"
        )
    }

    /// 🔴 Соединение ПРИНИМАЕТСЯ — backend живой, просто медленный под нагрузкой.
    /// Его рестарт уничтожил бы активную диктовку (инцидент 2026-07-22).
    func testNoEscalationWhenConnectionIsAcceptedButSlow() async {
        let escalations = WedgeCallCounter()
        let script = PingScript(successes: 2)
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { script.next() }
        await monitor.setWedgeProbe { false }      // соединение принимается
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await monitor.stop()

        XCTAssertEqual(escalations.count, 0, "рестарт здорового-но-медленного backend'а")
    }

    /// Оба подтверждения на месте — эскалация обязана произойти.
    func testEscalatesWhenWedgeIsConfirmedAfterHealthyPing() async {
        let escalations = WedgeCallCounter()
        let script = PingScript(successes: 2)
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { script.next() }
        await monitor.setWedgeProbe { true }
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 500_000_000)
        await monitor.stop()

        XCTAssertGreaterThan(escalations.count, 0, "подтверждённое зависание не вылечено")
    }

    /// 🔴 Суть фикса: детектор НЕ одноразовый.
    ///
    /// Если первая попытка не помогла, молчать навсегда — это ровно тот дефект,
    /// который здесь и чинится. Частоту ограничивает wedgeReprobeInterval,
    /// число реальных рестартов — WedgedEscalationTracker в обработчике.
    func testKeepsTryingInsteadOfGivingUpForever() async {
        let escalations = WedgeCallCounter()
        let script = PingScript(successes: 2)
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { script.next() }
        await monitor.setWedgeProbe { true }
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 700_000_000)
        await monitor.stop()

        XCTAssertGreaterThan(
            escalations.count, 1,
            "детектор сдался после первой попытки — тот же sticky-дефект"
        )
    }

    /// Интервал повторной проверки соблюдается: не пробуем каждый тик.
    func testReprobeIntervalThrottlesChecks() async {
        let probes = WedgeCallCounter()
        let script = PingScript(successes: 2)
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 60.0
        )
        monitor.setPingProvider { script.next() }
        await monitor.setWedgeProbe { probes.bump(); return false }
        await monitor.setOnWedgeDetected { }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 600_000_000)   // ~30 тиков
        await monitor.stop()

        XCTAssertEqual(probes.count, 1, "проба дёргается чаще, чем раз в интервал")
    }

    /// На здоровом backend'е проба не дёргается вовсе.
    func testNoProbeWhileBackendIsHealthy() async {
        let probes = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { true }
        await monitor.setWedgeProbe { probes.bump(); return true }
        await monitor.setOnWedgeDetected { }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await monitor.stop()

        XCTAssertEqual(probes.count, 0, "проба сработала на здоровом backend'е")
    }

    /// Выздоровление ОЧИЩАЕТ накопленное подозрение: отказы → здоров → отказы,
    /// и счётчик считается заново, а не продолжает старый.
    ///
    /// Прежняя версия («ping всегда true») очистку не проверяла вовсе — была
    /// зелёной при любом поведении сброса.
    func testHealthyPingClearsAccumulatedFailures() async {
        let escalations = WedgeCallCounter()
        // Максимум 2 отказа подряд. Если успех НЕ обнуляет счётчик, отказы
        // накопятся монотонно и порог 4 будет взят — тест это и ловит.
        let phases = PingPhases(pattern: [false, false, true])
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 4, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { phases.next() }
        await monitor.setWedgeProbe { true }
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 250_000_000)   // ~12 тиков
        await monitor.stop()


        XCTAssertEqual(
            escalations.count, 0,
            "успешный ping не обнулил накопленные отказы"
        )
    }

    /// 🔴 Актор реентерабелен: пока проба висит, может прийти suspend
    /// (пользователь дожал стоп диктовки — backend занят финализацией STT).
    /// Эскалация в этот момент убила бы запись.
    func testSuspendArrivingDuringProbeCancelsEscalation() async {
        let escalations = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        // 🔴 Нужен хотя бы ОДИН здоровый ping, иначе checkForWedgeIfNeeded
        // выходит на guard sawHealthyPing и проба не вызывается вовсе — тест
        // был бы вакуумным (прошлая версия именно такой и была; доказано
        // мутацией: с удалённой перепроверкой она оставалась зелёной).
        let script = PingScript(successes: 1)
        let probes = WedgeCallCounter()
        monitor.setPingProvider { script.next() }
        await monitor.setWedgeProbe {
            probes.bump()
            // Состояние меняется ровно в окне между пробой и колбэком.
            await monitor.suspend(.finalizingRecording)
            return true
        }
        await monitor.setOnWedgeDetected { escalations.bump() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await monitor.stop()

        XCTAssertGreaterThan(probes.count, 0, "проба не дёрнулась — тест вакуумный")
        XCTAssertEqual(
            escalations.count, 0,
            "kickstart во время финализации STT — потерянная диктовка"
        )
    }
}
