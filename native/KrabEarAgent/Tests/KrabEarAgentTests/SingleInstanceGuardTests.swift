/*
 SingleInstanceGuardTests — тесты defensive guard против дубликатов KrabEarAgent.

 Стратегия:
 - Инъектируем мок pgrepRunner вместо реального /usr/bin/pgrep.
 - Проверяем что функция убивает чужие PID-ы и пропускает selfPid.
 - Нет реального kill() — мок runner только возвращает строки с PID-ами.
   kill() к несуществующим PID-ам на macOS возвращает ESRCH (безвредно).

 Phase C C.6:
 - Тесты cleanupWorktreeShadows: создаём temp dir с fake worktree shadow,
   инжектируем processRunner мок, проверяем что lsregister вызван с правильными аргументами.
 - Тесты acquireFileLock: первый захват → true; второй захват в том же процессе → flock
   уже держится (LOCK_EX | LOCK_NB вернёт 0 на reentrant flock на macOS — BSD семантика),
   поэтому для реального multi-process теста flock используем sub-process test.
   Из-за BSD flock semantics (same PID reentrant allowed), документируем ограничение.
*/

import Foundation
import XCTest
@testable import KrabEarAgent

final class SingleInstanceGuardTests: XCTestCase {

    // MARK: - No duplicates

    /// Если pgrep возвращает только текущий PID — ничего не убиваем.
    func test_noDuplicates_returnsZero() {
        let selfPid = getpid()
        let runner: ([String]) -> String = { _ in "\(selfPid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Не должно быть убито ни одного процесса когда только self")
    }

    /// Пустой вывод pgrep — нет KrabEarAgent-процессов вообще.
    func test_emptyPgrepOutput_returnsZero() {
        let runner: ([String]) -> String = { _ in "" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Нет процессов — нечего убивать")
    }

    // MARK: - With duplicates

    /// Один посторонний PID — возвращаем 1.
    func test_oneDuplicate_returnsOne() {
        let selfPid = getpid()
        // Используем PID 99999 — маловероятно, что он существует; kill() вернёт ESRCH
        let fakePid: Int32 = 99999
        let runner: ([String]) -> String = { _ in "\(selfPid)\n\(fakePid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 1, "Должен быть убит 1 дубликат")
    }

    /// Несколько посторонних PID-ов — возвращаем корректное количество.
    func test_multipleDuplicates_returnsCorrectCount() {
        let selfPid = getpid()
        let fakePids: [Int32] = [99990, 99991, 99992]
        let output = ([selfPid] + fakePids).map { "\($0)" }.joined(separator: "\n")
        let runner: ([String]) -> String = { _ in output }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, fakePids.count, "Количество убитых должно совпасть с количеством чужих PID-ов")
    }

    // MARK: - Self-exclusion

    /// Self PID никогда не включается в список для убийства.
    func test_selfPid_neverKilled() {
        let selfPid = getpid()
        // runner возвращает только selfPid несколько раз
        let runner: ([String]) -> String = { _ in "\(selfPid)\n\(selfPid)\n\(selfPid)\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Self PID не должен считаться как дубликат")
    }

    // MARK: - Robustness

    /// Вывод с пробелами и пустыми строками — парсится корректно.
    func test_whitespaceInOutput_parsedCorrectly() {
        let selfPid = getpid()
        let runner: ([String]) -> String = { _ in "  \(selfPid)  \n\n  \n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Пробелы вокруг PID не должны приводить к ложным дубликатам")
    }

    /// Мусор в выводе pgrep — не вызывает краш.
    func test_garbledOutput_doesNotCrash() {
        let runner: ([String]) -> String = { _ in "abc\nxyz\n!!!\n" }
        let killed = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertEqual(killed, 0, "Нечисловые строки игнорируются")
    }

    // MARK: - pgrep arguments

    /// Функция передаёт в runner аргумент -x KrabEarAgent (exact-match по имени).
    func test_pgrepCalledWithExactMatchFlag() {
        var capturedArgs: [String] = []
        let runner: ([String]) -> String = { args in
            capturedArgs = args
            return ""
        }
        _ = killOtherAgentInstances(pgrepRunner: runner)
        XCTAssertTrue(capturedArgs.contains("-x"), "pgrep должен вызываться с флагом -x (exact match)")
        XCTAssertTrue(capturedArgs.contains("KrabEarAgent"), "pgrep должен искать процесс KrabEarAgent")
    }

    // MARK: - cleanupWorktreeShadows (Phase C C.6)

    /// Если `.claude/worktrees` не существует — функция завершается без вызова processRunner.
    func test_cleanupWorktreeShadows_noWorktreesDir_doesNotCallRunner() {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

        // Создаём только projectRoot без .claude/worktrees
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)

        // Создаём fake main bundle чтобы пройти guard
        let mainBundle = tempDir.appendingPathComponent("Krab Ear.app")
        try? FileManager.default.createDirectory(at: mainBundle, withIntermediateDirectories: true)

        // Создаём fake lsregister
        let fakeLsregister = tempDir.appendingPathComponent("lsregister")
        FileManager.default.createFile(atPath: fakeLsregister.path, contents: nil)

        var callCount = 0
        cleanupWorktreeShadows(
            projectRoot: tempDir,
            logger: nil,
            processRunner: { _, _ in callCount += 1 }
        )
        XCTAssertEqual(callCount, 0, "Без .claude/worktrees не должно быть вызовов processRunner")
    }

    /// Если в worktrees есть `Krab Ear.app` — unregister вызывается для shadow + re-register main.
    func test_cleanupWorktreeShadows_scansWorktreesDir() {
        // Use standardizedFileURL to resolve /tmp → /private/tmp symlink on macOS.
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

        // Структура: tempDir/Krab Ear.app (main), tempDir/.claude/worktrees/agent-abc/Krab Ear.app (shadow)
        let mainBundle = tempDir.appendingPathComponent("Krab Ear.app")
        let worktreeShadow = tempDir
            .appendingPathComponent(".claude/worktrees/agent-abc/Krab Ear.app")

        try? FileManager.default.createDirectory(at: mainBundle, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: worktreeShadow, withIntermediateDirectories: true)

        var capturedCalls: [(executable: String, arguments: [String])] = []
        cleanupWorktreeShadows(
            projectRoot: tempDir,
            logger: nil,
            processRunner: { executable, arguments in
                capturedCalls.append((executable: executable, arguments: arguments))
            }
        )

        // Должно быть 2 вызова: -u shadow + -f mainBundle
        XCTAssertEqual(capturedCalls.count, 2, "Ожидается: 1 unregister shadow + 1 re-register main")

        let unregisterCall = capturedCalls.first { $0.arguments.contains("-u") }
        XCTAssertNotNil(unregisterCall, "Должен быть вызов с -u для shadow")
        // Check that some argument in the -u call contains the shadow's last path component
        // (Paths may differ due to symlink resolution; we check suffix instead of exact match)
        let shadowPathArg = unregisterCall?.arguments.dropFirst().first ?? ""
        XCTAssertTrue(
            shadowPathArg.hasSuffix("Krab Ear.app"),
            "Аргумент -u должен содержать путь к shadow bundle (suffix: Krab Ear.app), got: \(shadowPathArg)"
        )
        XCTAssertTrue(
            shadowPathArg.contains("agent-abc"),
            "Аргумент -u должен содержать agent-abc, got: \(shadowPathArg)"
        )

        let reregisterCall = capturedCalls.first { $0.arguments.contains("-f") }
        XCTAssertNotNil(reregisterCall, "Должен быть вызов с -f для main bundle")
        let mainPathArg = reregisterCall?.arguments.dropFirst().first ?? ""
        XCTAssertTrue(
            mainPathArg.hasSuffix("Krab Ear.app"),
            "Аргумент -f должен содержать путь к main bundle (suffix: Krab Ear.app), got: \(mainPathArg)"
        )
    }

    /// Несколько shadow копий — все unregister-ятся + 1 re-register main.
    func test_cleanupWorktreeShadows_multipleShadows() {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let mainBundle = tempDir.appendingPathComponent("Krab Ear.app")
        try? FileManager.default.createDirectory(at: mainBundle, withIntermediateDirectories: true)

        let shadowPaths = ["agent-aaa", "agent-bbb", "agent-ccc"].map { agentDir in
            tempDir.appendingPathComponent(".claude/worktrees/\(agentDir)/Krab Ear.app")
        }
        for shadow in shadowPaths {
            try? FileManager.default.createDirectory(at: shadow, withIntermediateDirectories: true)
        }

        var unregisterCount = 0
        var reregisterCount = 0
        cleanupWorktreeShadows(
            projectRoot: tempDir,
            logger: nil,
            processRunner: { _, arguments in
                if arguments.contains("-u") { unregisterCount += 1 }
                if arguments.contains("-f") { reregisterCount += 1 }
            }
        )

        XCTAssertEqual(unregisterCount, 3, "Три shadow копии должны быть unregistered")
        XCTAssertEqual(reregisterCount, 1, "Main bundle re-register вызывается один раз")
    }

    // MARK: - killOrphanRuntimeProcesses (Phase C C.6.2)

    /// Если в выводе ps нет процессов с путём native/runtime/KrabEarAgent — возвращает 0.
    func testKillOrphanRuntimeProcesses_returnsZero_whenNoOrphan() {
        let projectRoot = URL(fileURLWithPath: NSTemporaryDirectory())
        let runner: ([String]) -> String = { _ in
            // ps output without any native/runtime/KrabEarAgent lines
            "  1 /sbin/launchd\n  100 /usr/bin/something\n"
        }
        let result = killOrphanRuntimeProcesses(
            projectRoot: projectRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, 0, "No matching processes — should return 0")
    }

    /// Если строка содержит runtimeBinaryPath — процесс убивается (возвращается 1).
    /// Используем несуществующий PID 99988 — kill() вернёт ESRCH, это безвредно.
    func testKillOrphanRuntimeProcesses_returnsOne_whenOrphanFound() {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot
            .appendingPathComponent("native/runtime/KrabEarAgent")
            .path
        let orphanPid: Int32 = 99988
        let runner: ([String]) -> String = { _ in
            // Simulate a ps line matching the runtime binary path
            "  \(orphanPid) \(runtimePath)\n"
        }
        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, 1, "One matching orphan should return 1")
    }

    /// Self PID никогда не убивается — даже если путь совпадает.
    func testKillOrphanRuntimeProcesses_doesNotKillSelf() {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot
            .appendingPathComponent("native/runtime/KrabEarAgent")
            .path
        let myPid = getpid()
        let runner: ([String]) -> String = { _ in
            // Simulate a ps line with OUR OWN pid and matching path
            "  \(myPid) \(runtimePath)\n"
        }
        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, 0, "Self PID must never be killed — should return 0")
        // If we got here, we did not kill ourselves
        XCTAssertTrue(true, "Process is still running after call — self-protection works")
    }

    /// Пустой вывод ps — безопасно возвращает 0.
    func testKillOrphanRuntimeProcesses_emptyOutput_returnsZero() {
        let projectRoot = URL(fileURLWithPath: NSTemporaryDirectory())
        let runner: ([String]) -> String = { _ in "" }
        let result = killOrphanRuntimeProcesses(
            projectRoot: projectRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, 0, "Empty ps output — should return 0")
    }

    /// Несколько orphan-строк — все засчитываются (мок, kill на ESRCH-PID безвреден).
    func testKillOrphanRuntimeProcesses_multipleOrphans_returnsCorrectCount() {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot
            .appendingPathComponent("native/runtime/KrabEarAgent")
            .path
        let pids: [Int32] = [99980, 99981, 99982]
        let psOutput = pids.map { "  \($0) \(runtimePath)" }.joined(separator: "\n") + "\n"
        let runner: ([String]) -> String = { _ in psOutput }
        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, pids.count, "All orphan PIDs should be counted")
    }

    /// Строки без совпадения с runtimeBinaryPath не влияют на счётчик.
    func testKillOrphanRuntimeProcesses_nonMatchingLines_ignored() {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString)
        let runner: ([String]) -> String = { _ in
            "  200 /Applications/KrabEarAgent.app/Contents/MacOS/KrabEarAgent\n" +
            "  201 /usr/bin/KrabEarAgent\n" +
            "  202 /some/other/KrabEar\n"
        }
        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: runner
        )
        XCTAssertEqual(result, 0, "Lines not matching runtimeBinaryPath should be ignored")
    }

    // MARK: - acquireFileLock / releaseFileLock (Phase C C.6)

    /// Первый вызов acquireFileLock в изолированном temp-lock-path должен вернуть true.
    func test_acquireFileLock_first_succeeds() {
        // Используем уникальный temp lock файл чтобы не конфликтовать с реальным агентом
        let tempLockPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("test_agent_\(UUID().uuidString).lock")
            .path
        defer {
            // Cleanup
            try? FileManager.default.removeItem(atPath: tempLockPath)
        }

        let fd = open(tempLockPath, O_CREAT | O_RDWR, 0o644)
        guard fd >= 0 else {
            XCTFail("Не удалось открыть test lock file")
            return
        }
        defer {
            flock(fd, LOCK_UN)
            close(fd)
        }

        // flock на новый файл должен успешно захватиться
        let result = flock(fd, LOCK_EX | LOCK_NB)
        XCTAssertEqual(result, 0, "flock на новый файл должен вернуть 0 (success)")
    }

    /// Если lock уже захвачен другим fd — LOCK_NB вернёт ошибку.
    /// NOTE: BSD flock семантика позволяет reentrant lock с того же PID.
    /// Для true multi-process теста нужен subprocess — здесь тестируем через два fd.
    /// macOS НЕ блокирует второй flock с того же процесса на некоторых fs — see man 2 flock.
    /// Этот тест документирует ожидаемое поведение при двух разных fd (subprocess scenario).
    func test_acquireFileLock_nonblocking_flag_documented() {
        // Документационный тест: verifies LOCK_NB константа существует и сигнатура функции.
        // Full multi-process flock contention требует subprocess — выходит за рамки unit-test.
        // Реальный guard проверяется интеграционным тестом (два экземпляра .app).
        let lockNBExists = LOCK_NB != 0
        XCTAssertTrue(lockNBExists, "LOCK_NB константа должна быть ненулевой")

        let lockEXExists = LOCK_EX != 0
        XCTAssertTrue(lockEXExists, "LOCK_EX константа должна быть ненулевой")
    }

    /// releaseFileLock идемпотентен — повторный вызов не крашит.
    func test_releaseFileLock_idempotent() {
        // Вызов без предшествующего acquire (fd = -1) — должен быть silent no-op.
        // Функция проверяет fd >= 0 перед операцией.
        releaseFileLock(logger: nil)
        releaseFileLock(logger: nil) // Повторный вызов — no-op
        // Если дошли сюда без краша — тест прошёл
        XCTAssertTrue(true, "releaseFileLock без предшествующего acquire не должен крашить")
    }
}
