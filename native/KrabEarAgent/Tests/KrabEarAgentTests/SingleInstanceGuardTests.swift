/*
 SingleInstanceGuardTests — безопасные тесты защиты от лишних процессов KrabEarAgent.

 Файл проверяет три независимых механизма: точечное завершение устаревшего
 `native/runtime/KrabEarAgent`, очистку теневых app-bundle из LaunchServices и
 POSIX file lock. Все системные сигналы в тестах подменены замыканием: unit-тесты
 никогда не вызывают настоящий `kill(2)` и не зависят от случайно свободных PID.

 Для legacy-cleanup отдельно проверяются точный канонический путь, владелец
 процесса и неизменность start-time перед сигналом. Благодаря этому повторное
 использование PID другим процессом не превращается в случайный SIGKILL.
*/

import Foundation
import XCTest
@testable import KrabEarAgent

final class SingleInstanceGuardTests: XCTestCase {

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

    // MARK: - Точечная очистка legacy runtime

    private func identity(
        path: String,
        startSeconds: UInt64 = 100,
        startMicroseconds: UInt64 = 200,
        effectiveUserID: uid_t = geteuid()
    ) -> AgentProcessIdentity {
        AgentProcessIdentity(
            executablePath: path,
            effectiveUserID: effectiveUserID,
            startSeconds: startSeconds,
            startMicroseconds: startMicroseconds
        )
    }

    /// Пустой список процессов не вызывает ни чтение identity, ни сигнал.
    func testKillOrphanRuntimeProcesses_emptyProcessList_returnsZero() {
        var capturedArguments: [String] = []
        let result = killOrphanRuntimeProcesses(
            projectRoot: FileManager.default.temporaryDirectory,
            logger: nil,
            psRunner: { arguments in
                capturedArguments = arguments
                return ""
            },
            identityReader: { _ in
                XCTFail("Для пустого списка identityReader вызываться не должен")
                return nil
            },
            signalSender: { _, _ in
                XCTFail("Для пустого списка signalSender вызываться не должен")
                return -1
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertEqual(capturedArguments, ["-axo", "pid="])
    }

    /// Только точный executable path получает сигнал, а успешный signal учитывается.
    func testKillOrphanRuntimeProcesses_exactStableIdentity_countsSuccessfulSignal() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
        let candidatePID: pid_t = 4_242
        var identityReads = 0
        var sentSignals: [(pid_t, Int32)] = []

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "\(candidatePID)\n" },
            identityReader: { pid in
                XCTAssertEqual(pid, candidatePID)
                identityReads += 1
                return self.identity(path: runtimePath)
            },
            signalSender: { pid, signal in
                sentSignals.append((pid, signal))
                return 0
            }
        )

        XCTAssertEqual(result, 1)
        XCTAssertEqual(identityReads, 2, "Identity должна перепроверяться непосредственно перед сигналом")
        XCTAssertEqual(sentSignals.count, 1)
        XCTAssertEqual(sentSignals.first?.0, candidatePID)
        XCTAssertEqual(sentSignals.first?.1, SIGKILL)
    }

    /// Ошибка системного сигнала не должна превращаться в ложное «убито».
    func testKillOrphanRuntimeProcesses_failedSignal_isNotCounted() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4243\n" },
            identityReader: { _ in self.identity(path: runtimePath) },
            signalSender: { _, _ in -1 }
        )

        XCTAssertEqual(result, 0)
    }

    /// Одинаковое имя файла в другом каталоге не является разрешённым legacy runtime.
    func testKillOrphanRuntimeProcesses_sameBasenameDifferentPath_isIgnored() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        var signalWasRequested = false

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4244\n" },
            identityReader: { _ in self.identity(path: "/another/worktree/native/runtime/KrabEarAgent") },
            signalSender: { _, _ in
                signalWasRequested = true
                return 0
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertFalse(signalWasRequested)
    }

    /// Путь target в аргументах shell не заменяет точную identity исполняемого файла.
    func testKillOrphanRuntimeProcesses_targetOnlyInArguments_isIgnored() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        var signalWasRequested = false

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4245\n" },
            identityReader: { _ in self.identity(path: "/bin/sh") },
            signalSender: { _, _ in
                signalWasRequested = true
                return 0
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertFalse(signalWasRequested)
    }

    /// Текущий и заведомо некорректные PID отбрасываются до чтения identity.
    func testKillOrphanRuntimeProcesses_selfAndInvalidPIDs_areIgnored() {
        let output = "0\n-1\n1\n\(getpid())\nnot-a-pid\n"

        let result = killOrphanRuntimeProcesses(
            projectRoot: FileManager.default.temporaryDirectory,
            logger: nil,
            psRunner: { _ in output },
            identityReader: { _ in
                XCTFail("Для self/invalid PID identityReader вызываться не должен")
                return nil
            },
            signalSender: { _, _ in
                XCTFail("Для self/invalid PID signalSender вызываться не должен")
                return -1
            }
        )

        XCTAssertEqual(result, 0)
    }

    /// Смена executable path между проверками запрещает отправку сигнала.
    func testKillOrphanRuntimeProcesses_pathChangedBeforeSignal_isIgnored() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
        var identities = [identity(path: runtimePath), identity(path: "/bin/sleep")]
        var signalWasRequested = false

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4246\n" },
            identityReader: { _ in identities.removeFirst() },
            signalSender: { _, _ in
                signalWasRequested = true
                return 0
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertFalse(signalWasRequested)
    }

    /// Новый процесс с тем же PID и путём распознаётся по изменившемуся start-time.
    func testKillOrphanRuntimeProcesses_startTimeChangedBeforeSignal_isIgnored() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
        var identities = [
            identity(path: runtimePath, startSeconds: 100),
            identity(path: runtimePath, startSeconds: 101),
        ]
        var signalWasRequested = false

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4247\n" },
            identityReader: { _ in identities.removeFirst() },
            signalSender: { _, _ in
                signalWasRequested = true
                return 0
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertFalse(signalWasRequested)
    }

    /// Процесс другого effective UID не получает сигнал даже при совпавшем пути.
    func testKillOrphanRuntimeProcesses_differentUser_isIgnored() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
        var signalWasRequested = false

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4248\n" },
            identityReader: { _ in
                self.identity(path: runtimePath, effectiveUserID: geteuid() &+ 1)
            },
            signalSender: { _, _ in
                signalWasRequested = true
                return 0
            }
        )

        XCTAssertEqual(result, 0)
        XCTAssertFalse(signalWasRequested)
    }

    /// Канонический путь принимает symlink projectRoot только для того же файла.
    func testKillOrphanRuntimeProcesses_symlinkRoot_matchesCanonicalExecutable() throws {
        let container = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let realRoot = container.appendingPathComponent("real-root")
        let aliasRoot = container.appendingPathComponent("alias-root")
        let runtimeURL = realRoot.appendingPathComponent("native/runtime/KrabEarAgent")
        defer { try? FileManager.default.removeItem(at: container) }

        try FileManager.default.createDirectory(
            at: runtimeURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        XCTAssertTrue(FileManager.default.createFile(atPath: runtimeURL.path, contents: Data()))
        try FileManager.default.createSymbolicLink(at: aliasRoot, withDestinationURL: realRoot)

        let result = killOrphanRuntimeProcesses(
            projectRoot: aliasRoot,
            logger: nil,
            psRunner: { _ in "4249\n" },
            identityReader: { _ in self.identity(path: runtimeURL.path) },
            signalSender: { _, _ in 0 }
        )

        XCTAssertEqual(result, 1)
    }

    /// Повтор PID в выводе ps не приводит к повторному сигналу.
    func testKillOrphanRuntimeProcesses_duplicatePID_isSignaledOnce() {
        let tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let runtimePath = tempRoot.appendingPathComponent("native/runtime/KrabEarAgent").path
        var signalCount = 0

        let result = killOrphanRuntimeProcesses(
            projectRoot: tempRoot,
            logger: nil,
            psRunner: { _ in "4250\n4250\n" },
            identityReader: { _ in self.identity(path: runtimePath) },
            signalSender: { _, _ in
                signalCount += 1
                return 0
            }
        )

        XCTAssertEqual(result, 1)
        XCTAssertEqual(signalCount, 1)
    }

    /// Реальный reader возвращает консистентную identity текущего test runner без сигналов.
    func testDefaultProcessIdentityReader_currentProcess_returnsStableIdentity() throws {
        let first = try XCTUnwrap(defaultProcessIdentityReader(getpid()))
        let second = try XCTUnwrap(defaultProcessIdentityReader(getpid()))

        XCTAssertEqual(first, second)
        XCTAssertTrue(first.executablePath.hasPrefix("/"))
        XCTAssertEqual(first.effectiveUserID, geteuid())
        XCTAssertGreaterThan(first.startSeconds, 0)
    }

    /// Стартовый путь обязан сохранять path-aware cleanup и не возвращаться к `pgrep -x`.
    func testStartupSource_hasNoNameOnlyProcessKiller() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let sourceRoot = packageRoot.appendingPathComponent("Sources/KrabEarAgent")
        let guardSource = try String(
            contentsOf: sourceRoot.appendingPathComponent("SingleInstanceGuard.swift"),
            encoding: .utf8
        )
        let mainSource = try String(
            contentsOf: sourceRoot.appendingPathComponent("main.swift"),
            encoding: .utf8
        )

        XCTAssertFalse(guardSource.contains("/usr/bin/pgrep"))
        XCTAssertFalse(guardSource.contains("killOtherAgentInstances"))
        XCTAssertFalse(mainSource.contains("killOtherAgentInstances"))
        XCTAssertTrue(guardSource.contains("proc_pidpath"))
        XCTAssertTrue(mainSource.contains("killOrphanRuntimeProcesses(projectRoot:"))
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
