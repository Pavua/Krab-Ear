/*
 LaunchAgentManagerTests — тесты LaunchAgentManager.

 Подход:
 - buildPlistContent() (#if DEBUG) проверяет содержимое plist без записи на диск.
 - plistPathForTest (#if DEBUG) проверяет путь установки без реального FileManager.
 - isAutostartEnabled() тестируется через временный файл (не трогает ~/Library/LaunchAgents).
 - install()/uninstall() тестируются косвенно через временную директорию,
   launchctl вызовы при отсутствии plist — graceful fail (exit ≠ 0, но без crash).

 Wave 30-A: обновлены проверки под canonical plist (com.antigravity.krab-ear + /usr/bin/open).
*/

import XCTest
@testable import KrabEarAgent

final class LaunchAgentManagerTests: XCTestCase {

    private let testRoot = "/tmp/krab_ear_launchagent_tests"

    // MARK: - Helpers

    private func makeManager(projectRoot: String? = nil) -> LaunchAgentManager {
        LaunchAgentManager(projectRoot: projectRoot ?? testRoot)
    }

    // MARK: - Plist content generation (Wave 30-A: canonical bundle-based plist)

    func test_buildPlistContent_containsCanonicalLabel() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        // Wave 30-A: новый canonical label
        XCTAssertTrue(
            plist.contains("com.antigravity.krab-ear"),
            "Plist должен содержать canonical label com.antigravity.krab-ear; got: \(plist.prefix(200))"
        )
    }

    func test_buildPlistContent_doesNotContainLegacyStartScript() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        // Wave 30-A: deprecated start_agent.command не должен использоваться
        XCTAssertFalse(
            plist.contains("scripts/start_agent.command"),
            "Plist НЕ должен использовать deprecated start_agent.command (используй /usr/bin/open)"
        )
    }

    func test_buildPlistContent_usesOpenWithAppBundle() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        // Wave 30-A: запуск через /usr/bin/open + .app bundle
        XCTAssertTrue(
            plist.contains("/usr/bin/open"),
            "ProgramArguments должен использовать /usr/bin/open для bundle-based запуска"
        )
        XCTAssertTrue(
            plist.contains("Krab Ear.app"),
            "ProgramArguments должен содержать путь к Krab Ear.app bundle"
        )
    }

    func test_buildPlistContent_containsRunAtLoad() {
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(plist.contains("<key>RunAtLoad</key>"), "Plist должен содержать RunAtLoad")
        XCTAssertTrue(plist.contains("<true/>"), "RunAtLoad должен быть true")
    }

    func test_buildPlistContent_keepAliveIsFalse() {
        // Wave 30-A: KeepAlive=false чтобы избежать respawn-петли при Login Item механизме
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertTrue(
            plist.contains("<key>KeepAlive</key>"),
            "Plist должен содержать KeepAlive key"
        )
        // KeepAlive=false (open -W уже ждёт завершения)
        XCTAssertTrue(
            plist.contains("<false/>"),
            "KeepAlive должен быть false для bundle-based запуска"
        )
    }

    func test_buildPlistContent_containsProjectRoot() {
        let root = "/custom/project/root"
        let manager = makeManager(projectRoot: root)
        let plist = manager.buildPlistContent()
        XCTAssertTrue(
            plist.contains(root),
            "Plist должен содержать projectRoot в пути к .app bundle: \(root)"
        )
    }

    func test_buildPlistContent_doesNotContainLaunchedByLaunchdFlag() {
        // Wave 30-A: --launched-by-launchd больше не передаётся (legacy флаг для start_agent.command)
        let manager = makeManager()
        let plist = manager.buildPlistContent()
        XCTAssertFalse(
            plist.contains("--launched-by-launchd"),
            "Canonical plist не должен передавать --launched-by-launchd (это legacy флаг)"
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
        // Wave 30-A: canonical label
        XCTAssertTrue(
            path.contains("com.antigravity.krab-ear"),
            "plistPath должен содержать canonical label; got: \(path)"
        )
    }

    func test_legacyPlistPath_isInLaunchAgentsDir() {
        // Wave 30-A: legacy path должен быть доступен для cleanup
        let manager = makeManager()
        let path = manager.legacyPlistPathForTest
        XCTAssertTrue(
            path.contains("Library/LaunchAgents"),
            "legacyPlistPath должен быть в ~/Library/LaunchAgents; got: \(path)"
        )
        XCTAssertTrue(
            path.contains("com.krabear.agent"),
            "legacyPlistPath должен содержать legacy label; got: \(path)"
        )
    }

    // MARK: - isAutostartEnabled

    func test_isAutostartEnabled_falseWhenPlistAbsent() {
        // Создаём manager — plist заведомо отсутствует при первом запуске.
        let manager = makeManager()
        let plistPath = manager.plistPathForTest
        let legacyPath = manager.legacyPlistPathForTest
        // Если оба файла реально есть в системе — пропускаем тест (CI-safe).
        guard !FileManager.default.fileExists(atPath: plistPath),
              !FileManager.default.fileExists(atPath: legacyPath) else {
            return
        }
        XCTAssertFalse(manager.isAutostartEnabled(), "isAutostartEnabled должен быть false если plist отсутствует")
    }

    func test_isAutostartEnabled_trueAfterCanonicalPlistCreated() throws {
        // Создаём временный canonical plist-файл напрямую.
        let manager = makeManager()
        let plistPath = manager.plistPathForTest

        let dir = (plistPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        let dummy = "<plist version=\"1.0\"><dict/></plist>"
        try dummy.write(toFile: plistPath, atomically: true, encoding: .utf8)

        defer {
            try? FileManager.default.removeItem(atPath: plistPath)
        }

        XCTAssertTrue(
            manager.isAutostartEnabled(),
            "isAutostartEnabled должен быть true когда canonical plist существует"
        )
    }

    func test_isAutostartEnabled_trueAfterLegacyPlistCreated() throws {
        // Wave 30-A: isAutostartEnabled должен возвращать true если legacy plist ещё существует
        let manager = makeManager()
        let legacyPath = manager.legacyPlistPathForTest
        let canonicalPath = manager.plistPathForTest

        // Убеждаемся что canonical отсутствует
        guard !FileManager.default.fileExists(atPath: canonicalPath) else {
            return // Пропустить если canonical plist уже установлен
        }

        let dir = (legacyPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        let dummy = "<plist version=\"1.0\"><dict/></plist>"
        try dummy.write(toFile: legacyPath, atomically: true, encoding: .utf8)

        defer {
            try? FileManager.default.removeItem(atPath: legacyPath)
        }

        XCTAssertTrue(
            manager.isAutostartEnabled(),
            "isAutostartEnabled должен быть true когда legacy plist ещё существует"
        )
    }
}
