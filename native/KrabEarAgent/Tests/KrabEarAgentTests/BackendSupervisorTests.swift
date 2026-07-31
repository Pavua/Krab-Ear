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

    func test_restartIfDead_active_respectsMaxConsecutiveRestarts() {
        // Ping всегда false → каждый вызов restartIfDead пытается перезапуск.
        // maxConsecutiveRestarts == 3, 4-й вызов должен вернуть false без ensureBackendRunning.
        let supervisor = makeSupervisor(mode: .active, pingResult: false, ensureError: nil)

        let r0 = supervisor.restartIfDead()   // consecutiveRestarts → 1
        let r1 = supervisor.restartIfDead()   // consecutiveRestarts → 2
        let r2 = supervisor.restartIfDead()   // consecutiveRestarts → 3
        let r3 = supervisor.restartIfDead()   // превышает лимит

        XCTAssertTrue(r0, "1-й restart должен быть успешным")
        XCTAssertTrue(r1, "2-й restart должен быть успешным")
        XCTAssertTrue(r2, "3-й restart должен быть успешным")
        XCTAssertFalse(r3, "4-й restart превышает лимит — должен вернуть false")
    }

    // MARK: stopBackend — passive mode

    func test_stopBackend_passive_isNoOp_processRemainsNil() {
        let supervisor = makeSupervisor(mode: .passive)
        // passive mode: stopBackend() — no-op, не трогает backendProcess
        supervisor.stopBackend()
        XCTAssertNil(supervisor.backendProcess, "passive mode: backendProcess остаётся nil после stopBackend")
    }

    // MARK: Wave 59 — cooldown reset

    func test_restartIfDead_active_cooldownReset_recoversAfterQuietPeriod() {
        // Сценарий: 3 restart'а подряд исчерпывают лимит. Затем имитируем
        // прошествие cooldownSec. 4-й restart должен снова работать,
        // потому что lastRestartAttemptAt > cooldown назад → счётчик сбросился.
        let supervisor = makeSupervisor(mode: .active, pingResult: false, ensureError: nil)
        supervisor.restartCooldownSec = 0.0  // мгновенный cooldown для теста

        let r0 = supervisor.restartIfDead()  // 1
        let r1 = supervisor.restartIfDead()  // 2
        let r2 = supervisor.restartIfDead()  // 3
        // 4-й после cooldown=0 → сброс счётчика → снова успех
        let r3 = supervisor.restartIfDead()  // reset → 1

        XCTAssertTrue(r0)
        XCTAssertTrue(r1)
        XCTAssertTrue(r2)
        XCTAssertTrue(r3, "После cooldown supervisor должен снова разрешать restart")
    }

    func test_restartIfDead_active_cooldownNotElapsed_stillBlocked() {
        // Если cooldown не истёк (defaultRestartCooldownSec = 300s),
        // 4-й вызов всё ещё должен быть заблокирован.
        let supervisor = makeSupervisor(mode: .active, pingResult: false, ensureError: nil)
        // cooldown по умолчанию 300s — за длительность теста не истечёт.

        _ = supervisor.restartIfDead()  // 1
        _ = supervisor.restartIfDead()  // 2
        _ = supervisor.restartIfDead()  // 3
        let r3 = supervisor.restartIfDead()

        XCTAssertFalse(r3, "4-й restart до cooldown должен оставаться заблокированным")
    }

    // MARK: kickstartArguments — чистая функция для forceRestartBackend

    func test_kickstartArguments_shape() {
        XCTAssertEqual(
            BackendSupervisor.kickstartArguments(uid: 501),
            ["kickstart", "-k", "gui/501/ai.krab.ear.backend"]
        )
    }

    // MARK: S3/Р9 — standalone active-режим обязан спавнить main.py

    /// До фикса startBackendProcess() спавнил KrabEar/backend/service.py
    /// напрямую — модуль исполнялся как __main__, а при включённом
    /// in-process REST (rest_server.py импортирует backend.service) в
    /// процессе жили бы два разных класса BackendService (см. плист-фикс
    /// в ai.krab.ear.backend.plist.template и test_backend_plist_data_dir_parity_S3.py).
    /// Standalone-путь имел ровно тот же класс бага.
    func test_backendScriptPath_pointsToMainPy_notServicePyDirectly() {
        let path = BackendSupervisor.backendScriptPath(projectRoot: "/tmp/my_project")

        XCTAssertTrue(
            path.hasSuffix("KrabEar/main.py"),
            "active-режим обязан спавнить main.py, а не backend/service.py напрямую; got: \(path)"
        )
        XCTAssertFalse(
            path.hasSuffix("KrabEar/backend/service.py"),
            "спавн backend/service.py напрямую раздваивает модуль backend.service при включённом REST (Р9); got: \(path)"
        )
    }
}
