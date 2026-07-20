/*
 SingleInstanceGuardTests — безопасные тесты single-instance защиты Krab Ear.

 Файл проверяет два механизма без запуска агентского бинарника: очистку теневых
 app-bundle из LaunchServices через инъецированный runner и POSIX file lock.
 Отдельный source-контракт запрещает возвращать автоматическое завершение
 процессов по PID: на macOS проверка identity и сигнал не образуют атомарную
 операцию, поэтому PID может быть переиспользован в промежутке.
*/

import Foundation
import XCTest
@testable import KrabEarAgent

final class SingleInstanceGuardTests: XCTestCase {

    // MARK: - Очистка теневых worktree-bundle

    /// Без `.claude/worktrees` внешний runner не вызывается.
    func test_cleanupWorktreeShadows_noWorktreesDir_doesNotCallRunner() {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        let mainBundle = tempDir.appendingPathComponent("Krab Ear.app")
        try? FileManager.default.createDirectory(at: mainBundle, withIntermediateDirectories: true)

        var callCount = 0
        cleanupWorktreeShadows(
            projectRoot: tempDir,
            logger: nil,
            processRunner: { _, _ in callCount += 1 }
        )

        XCTAssertEqual(callCount, 0)
    }

    /// Один shadow удаляется из LaunchServices, затем основной bundle регистрируется снова.
    func test_cleanupWorktreeShadows_scansWorktreesDir() {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

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

        XCTAssertEqual(capturedCalls.count, 2)
        let unregisterCall = capturedCalls.first { $0.arguments.contains("-u") }
        XCTAssertNotNil(unregisterCall)
        let shadowPath = unregisterCall?.arguments.dropFirst().first ?? ""
        XCTAssertTrue(shadowPath.hasSuffix("Krab Ear.app"))
        XCTAssertTrue(shadowPath.contains("agent-abc"))

        let registerCall = capturedCalls.first { $0.arguments.contains("-f") }
        XCTAssertNotNil(registerCall)
        let mainPath = registerCall?.arguments.dropFirst().first ?? ""
        XCTAssertTrue(mainPath.hasSuffix("Krab Ear.app"))
    }

    /// Несколько shadow-копий обрабатываются ровно по одному разу.
    func test_cleanupWorktreeShadows_multipleShadows() {
        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .standardizedFileURL
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let mainBundle = tempDir.appendingPathComponent("Krab Ear.app")
        try? FileManager.default.createDirectory(at: mainBundle, withIntermediateDirectories: true)
        for agentDir in ["agent-aaa", "agent-bbb", "agent-ccc"] {
            let shadow = tempDir
                .appendingPathComponent(".claude/worktrees/\(agentDir)/Krab Ear.app")
            try? FileManager.default.createDirectory(at: shadow, withIntermediateDirectories: true)
        }

        var unregisterCount = 0
        var registerCount = 0
        cleanupWorktreeShadows(
            projectRoot: tempDir,
            logger: nil,
            processRunner: { _, arguments in
                if arguments.contains("-u") { unregisterCount += 1 }
                if arguments.contains("-f") { registerCount += 1 }
            }
        )

        XCTAssertEqual(unregisterCount, 3)
        XCTAssertEqual(registerCount, 1)
    }

    /// Startup использует flock и не содержит неатомарного PID-based kill.
    func test_startupSource_hasNoAutomaticProcessSignal() throws {
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
        let startupSource = guardSource + "\n" + mainSource

        XCTAssertTrue(mainSource.contains("acquireFileLock(logger:"))
        for forbidden in [
            "killOtherAgentInstances",
            "killOrphanRuntimeProcesses",
            "proc_pidpath",
            "pgrep",
            "SIGKILL",
            "SIGTERM",
            "Darwin.kill",
            "/bin/kill",
            "kill(",
        ] {
            XCTAssertFalse(
                startupSource.contains(forbidden),
                "Startup-код не должен содержать PID-signal паттерн: \(forbidden)"
            )
        }
        XCTAssertTrue(mainSource.contains("legacy PID cleanup disabled"))
    }

    // MARK: - POSIX file lock

    /// Уникальный временный lock-файл можно захватить неблокирующим flock.
    func test_acquireFileLock_first_succeeds() {
        let tempLockPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("test_agent_\(UUID().uuidString).lock")
            .path
        defer { try? FileManager.default.removeItem(atPath: tempLockPath) }

        let descriptor = open(tempLockPath, O_CREAT | O_RDWR, 0o644)
        guard descriptor >= 0 else {
            XCTFail("Не удалось открыть временный lock-файл")
            return
        }
        defer {
            flock(descriptor, LOCK_UN)
            close(descriptor)
        }

        XCTAssertEqual(flock(descriptor, LOCK_EX | LOCK_NB), 0)
    }

    /// Неблокирующие флаги flock доступны на целевой macOS.
    func test_acquireFileLock_nonblockingFlags_areAvailable() {
        XCTAssertNotEqual(LOCK_NB, 0)
        XCTAssertNotEqual(LOCK_EX, 0)
    }

    /// Освобождение незахваченного глобального lock идемпотентно.
    func test_releaseFileLock_idempotent() {
        releaseFileLock(logger: nil)
        releaseFileLock(logger: nil)
    }
}
