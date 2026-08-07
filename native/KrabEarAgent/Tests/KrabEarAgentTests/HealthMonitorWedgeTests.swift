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

final class HealthMonitorWedgeTests: XCTestCase {

    /// Порог заклинивания ещё не набран — эскалации быть не должно.
    func testNoEscalationBeforeWedgeThreshold() async {
        let escalations = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 100, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { PingScript(successes: 1).next() }
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

    /// Успешный ping снимает подозрение: следующий эпизод считается с нуля.
    func testHealthyPingClearsWedgeState() async {
        let probes = WedgeCallCounter()
        let monitor = HealthMonitor(
            pingInterval: 0.02, hangThreshold: 2,
            wedgeThreshold: 3, wedgeReprobeInterval: 0.0
        )
        monitor.setPingProvider { true }           // всегда здоров
        await monitor.setWedgeProbe { probes.bump(); return true }
        await monitor.setOnWedgeDetected { }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await monitor.stop()

        XCTAssertEqual(probes.count, 0, "проба сработала на здоровом backend'е")
    }
}
