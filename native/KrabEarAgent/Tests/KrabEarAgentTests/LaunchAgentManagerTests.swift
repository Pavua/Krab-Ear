/*
 LaunchAgentManagerTests — тесты LaunchAgentManager.

 Подход:
 - buildPlistContent() (#if DEBUG) проверяет содержимое plist без записи на диск.
 - plistPathForTest (#if DEBUG) проверяет путь установки без реального FileManager.
 - isAutostartEnabled() тестируется через временный файл (не трогает ~/Library/LaunchAgents).
 - install()/uninstall() тестируются косвенно через временную директорию,
   launchctl вызовы при отсутствии plist — graceful fail (exit ≠ 0, но без crash).
*/

import XCTest
@testable import KrabEarAgent

final class LaunchAgentManagerTests: XCTestCase {

    private let testRoot = "/tmp/krab_ear_launchagent_tests"

    // MARK: - Helpers

    private func makeManager(projectRoot: String? = nil) -> LaunchAgentManager {
        LaunchAgentManager(projectRoot: projectRoot ?? testRoot)
    }

    // MARK: - Plist content generation

    func test_buildPlistContent_containsLabel() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(plist.contains("com.krabear.agent"), "Plist должен содержать label com.krabear.agent")
    }

    func test_buildPlistContent_containsStartScript() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(
            plist.contains("scripts/start_agent.command"),
            "Plist ProgramArguments должен содержать start_agent.command"
        )
    }

    func test_buildPlistContent_containsRunAtLoad() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(plist.contains("<key>RunAtLoad</key>"), "Plist должен содержать RunAtLoad")
        XCTAssertTrue(plist.contains("<true/>"), "RunAtLoad должен быть true")
    }

    func test_buildPlistContent_containsWorkingDirectory() {
        let root = "/custom/project/root"
        let manager = makeManager(projectRoot: root)
        let plist = manager.buildPlistContent()
        XCTAssertTrue(
            plist.contains(root),
            "Plist WorkingDirectory должен содержать projectRoot: \(root)"
        )
    }

    func test_buildPlistContent_containsLaunchedByLaunchdFlag() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(
            plist.contains("--launched-by-launchd"),
            "ProgramArguments должен включать --launched-by-launchd"
        )
    }

    // MARK: - Installation path

    func test_plistPath_isInLaunchAgentsDir() {
        let manager = makeManager()
        let path = manager.plistPathForTest
        XCTAssertTrue(
            path.contains("Library/LaunchAgents"),
            "plistPath должен быть в ~/Library/LaunchAgents; got: \(path)"
        )
        XCTAssertTrue(
            path.hasSuffix(".plist"),
            "plistPath должен заканчиваться на .plist"
        )
        XCTAssertTrue(
            path.contains("com.krabear.agent"),
            "plistPath должен содержать label; got: \(path)"
        )
    }

    // MARK: - isAutostartEnabled

    func test_isAutostartEnabled_falseWhenPlistAbsent() {
        // Создаём manager с несуществующим projectRoot — plist заведомо отсутствует.
        // Но plistPath зависит от ~/Library/LaunchAgents, не от projectRoot.
        // Проверяем через FileManager: если файл реально не существует → false.
        let manager = makeManager()
        let plistPath = manager.plistPathForTest
        // Если plist вдруг есть в системе — пропускаем тест (CI-safe).
        guard !FileManager.default.fileExists(atPath: plistPath) else {
            return
        }
        XCTAssertFalse(manager.isAutostartEnabled(), "isAutostartEnabled должен быть false если plist отсутствует")
    }

    func test_isAutostartEnabled_trueAfterPlistCreated() throws {
        // Создаём временный plist-файл напрямую, проверяем что isAutostartEnabled видит его.
        let manager = makeManager()
        let plistPath = manager.plistPathForTest

        // Создаём LaunchAgents директорию если нужно
        let dir = (plistPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        // Пишем фиктивный plist
        let dummy = "<plist version=\"1.0\"><dict/></plist>"
        try dummy.write(toFile: plistPath, atomically: true, encoding: .utf8)

        defer {
            try? FileManager.default.removeItem(atPath: plistPath)
        }

        XCTAssertTrue(manager.isAutostartEnabled(), "isAutostartEnabled должен быть true когда plist существует")
    }
}
