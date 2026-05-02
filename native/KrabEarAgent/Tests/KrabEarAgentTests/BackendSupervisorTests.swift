/*
 BackendSupervisorTests — тесты логики BackendSupervisor.

 Подход:
 - Используем #if DEBUG тест-хуки: _testPingOverride и _testEnsureOverride,
   чтобы избежать реальных IPC/process/sleep вызовов.
 - overrideSupervisionMode() позволяет форсировать режим без launchctl.
 - Тесты проверяют: initialization, passive/active mode behavior,
   restart throttle, stopBackend no-op в passive.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Helpers

private func makeSupervisor(
    mode: SupervisionMode,
    pingResult: Bool = false,
    ensureError: Error? = nil
) -> BackendSupervisor {
    let supervisor = BackendSupervisor(projectRoot: "/tmp/test_krab")
    supervisor.overrideSupervisionMode(mode)
    supervisor._testPingOverride = { pingResult }
    supervisor._testEnsureOverride = {
        if let err = ensureError { throw err }
    }
    supervisor._testBackoffOverride = 0  // без реальных sleep'ов в тестах
    return supervisor
}

// MARK: - BackendSupervisorTests

final class BackendSupervisorTests: XCTestCase {

    // MARK: Initialization

    func test_init_setsCorrectPaths() {
        let root = "/tmp/my_project"
        let supervisor = BackendSupervisor(projectRoot: root)

        XCTAssertEqual(supervisor.projectRoot, root)
        XCTAssertTrue(
            supervisor.dataDir.contains("KrabEar"),
            "dataDir должен содержать KrabEar; got: \(supervisor.dataDir)"
        )
        XCTAssertTrue(
            supervisor.socketPath.hasSuffix("krabear.sock"),
            "socketPath должен заканчиваться на krabear.sock; got: \(supervisor.socketPath)"
        )
    }

    func test_init_noProcessByDefault() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp")
        XCTAssertNil(supervisor.backendProcess, "При инициализации backendProcess должен быть nil")
    }

    // MARK: isBackendAlive — test hook

    func test_isBackendAlive_returnsTrueWhenPingSucceeds() {
        let supervisor = makeSupervisor(mode: .passive, pingResult: true)
        XCTAssertTrue(supervisor.isBackendAlive())
    }

    func test_isBackendAlive_returnsFalseWhenPingFails() {
        let supervisor = makeSupervisor(mode: .passive, pingResult: false)
        XCTAssertFalse(supervisor.isBackendAlive())
    }

    // MARK: restartIfDead — passive mode

    func test_restartIfDead_passive_backendAlive_returnsTrue() {
        let supervisor = makeSupervisor(mode: .passive, pingResult: true)

        let result = supervisor.restartIfDead()

        XCTAssertTrue(result, "Если backend жив — restartIfDead должен вернуть true")
    }

    func test_restartIfDead_passive_backendDead_ensureOK_returnsTrue() {
        let supervisor = makeSupervisor(mode: .passive, pingResult: false, ensureError: nil)

        let result = supervisor.restartIfDead()

        XCTAssertTrue(result, "passive+dead+ensure_OK → должен вернуть true")
    }

    func test_restartIfDead_passive_ensureFails_returnsFalse() {
        let supervisor = makeSupervisor(
            mode: .passive,
            pingResult: false,
            ensureError: IPCError.socketConnectFailed("backend timeout")
        )

        let result = supervisor.restartIfDead()

        XCTAssertFalse(result, "Если ensureBackendRunning бросает — restartIfDead должен вернуть false")
    }

    // MARK: restartIfDead — active mode

    func test_restartIfDead_active_backendAlive_returnsTrue() {
        let supervisor = makeSupervisor(mode: .active, pingResult: true)

        let result = supervisor.restartIfDead()

        XCTAssertTrue(result)
    }

    func test_restartIfDead_active_respectsCircuitBreaker() {
        // Ping всегда false + ensure всегда кидает → каждый вызов restartIfDead
        // накапливает consecutiveRestarts. Circuit открывается после 5-го fails.
        // 6-й вызов возвращает false без вызова ensureBackendRunning.
        let err = IPCError.socketConnectFailed("test")
        let supervisor = makeSupervisor(mode: .active, pingResult: false, ensureError: err)
        supervisor._testBackoffOverride = 0  // без реальных sleep'ов

        let r0 = supervisor.restartIfDead()   // consecutiveRestarts → 1
        let r1 = supervisor.restartIfDead()   // consecutiveRestarts → 2
        let r2 = supervisor.restartIfDead()   // consecutiveRestarts → 3
        let r3 = supervisor.restartIfDead()   // consecutiveRestarts → 4
        let r4 = supervisor.restartIfDead()   // consecutiveRestarts → 5 → circuit opens
        let r5 = supervisor.restartIfDead()   // circuit open → false без ensureBackendRunning

        XCTAssertFalse(r0, "1-й restart: ensure кидает → false")
        XCTAssertFalse(r1, "2-й restart: ensure кидает → false")
        XCTAssertFalse(r2, "3-й restart: ensure кидает → false")
        XCTAssertFalse(r3, "4-й restart: ensure кидает → false")
        XCTAssertFalse(r4, "5-й restart: circuit открывается → false")
        XCTAssertFalse(r5, "6-й restart: circuit open → false (без вызова ensure)")
        XCTAssertTrue(supervisor.isCircuitOpen(), "После 5 fails circuit должен быть open")
    }

    // MARK: stopBackend — passive mode

    func test_stopBackend_passive_isNoOp_processRemainsNil() {
        let supervisor = makeSupervisor(mode: .passive)
        // passive mode: stopBackend() — no-op, не трогает backendProcess
        supervisor.stopBackend()
        XCTAssertNil(supervisor.backendProcess, "passive mode: backendProcess остаётся nil после stopBackend")
    }
}

// MARK: - BackendSupervisorBackoffTests

final class BackendSupervisorBackoffTests: XCTestCase {

    /// 5-я попытка restart открывает circuit breaker (без вызова ensure на 5-й и 6-й итерации).
    func testCircuitBreakerOpensAfterFiveFails() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        var ensureCalls = 0
        supervisor._testEnsureOverride = {
            ensureCalls += 1
            throw NSError(domain: "test", code: 1, userInfo: nil)
        }
        supervisor._testPingOverride = { false }
        supervisor._testBackoffOverride = 0  // без реальных sleep'ов
        supervisor.overrideSupervisionMode(.active)

        for _ in 0..<6 {
            _ = supervisor.restartIfDead()
        }
        XCTAssertTrue(supervisor.isCircuitOpen())
        // Итерации 1-4: ensure вызывается (restarts 1..4 < circuitOpenAfter=5).
        // Итерация 5: restarts=5 >= 5 → circuit opens, ensure НЕ вызывается.
        // Итерация 6: circuit open → ensure НЕ вызывается.
        XCTAssertEqual(ensureCalls, 4, "5-я и 6-я попытки не должны вызвать ensureBackend (circuit opens at restarts=5)")
    }

    /// После cooldown circuit закрывается, restartIfDead снова работает.
    func testCircuitClosesAfterCooldown() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        supervisor._testEnsureOverride = { throw NSError(domain: "test", code: 1) }
        supervisor._testPingOverride = { false }
        supervisor._testBackoffOverride = 0
        supervisor._testCooldownSec = 0.1  // override 5min default to 0.1s
        supervisor.overrideSupervisionMode(.active)

        for _ in 0..<5 { _ = supervisor.restartIfDead() }
        XCTAssertTrue(supervisor.isCircuitOpen())

        Thread.sleep(forTimeInterval: 0.2)
        XCTAssertFalse(supervisor.isCircuitOpen())
    }

    /// Backoff delays формируются: 1=0s, 2=2s, 3=5s, 4+=15s.
    func testBackoffSchedule() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        XCTAssertEqual(supervisor.backoffDelay(attempt: 1), 0)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 2), 2)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 3), 5)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 4), 15)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 5), 15)
    }
}
