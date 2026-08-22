/*
 LaunchAgentManagerTests — безопасные тесты автозапуска Krab Ear.

 Проверяет:
 1. label == "com.antigravity.krab-ear" (канонический bundle ID).
 2. legacyLabel == "com.krabear.agent".
 3. makePlistContent() содержит /usr/bin/open -W <bundle> (НЕ start_agent.command).
 4. makePlistContent() НЕ содержит /bin/zsh и start_agent.command.
 5. makePlistContent() содержит канонический label.
 6. plistPath содержит label "com.antigravity.krab-ear".
 7. bundlePath содержит расширение ".app" рядом с projectRoot.
 8. isAutostartEnabled: false/true для plist в изолированном каталоге.
 9. install() создаёт plist только во временном каталоге.
 10. install() идемпотентен (двойной вызов = одинаковый результат).
 11. uninstall() удаляет файл plist.
 12. Корректная обработка недоступного тестового каталога.
 13. Plist является валидным XML.
 14. Параллельный install безопасен.

 Подход:
 - Каждый manager получает UUID-каталог внутри temporaryDirectory.
 - Runner процессов всегда подменён потокобезопасным recorder: настоящий
   `/bin/launchctl` никогда не запускается.
 - Очистка удаляет только созданный конкретным тестом временный корень.
*/

import XCTest
@testable import KrabEarAgent

/// Потокобезопасно сохраняет запросы на запуск процесса, не создавая `Process`.
private final class LaunchAgentProcessRecorder: @unchecked Sendable {
    struct Call: Equatable {
        let executable: String
        let arguments: [String]
    }

    private let lock = NSLock()
    private var storedCalls: [Call] = []

    func run(executable: String, arguments: [String]) -> Int32 {
        lock.lock()
        storedCalls.append(Call(executable: executable, arguments: arguments))
        lock.unlock()
        return 0
    }

    var calls: [Call] {
        lock.lock()
        defer { lock.unlock() }
        return storedCalls
    }
}

/// Полный изолированный контур одного теста LaunchAgentManager.
private struct LaunchAgentManagerFixture: Sendable {
    let manager: LaunchAgentManager
    let root: URL
    let projectRoot: URL
    let launchAgentsDirectory: URL
    let canonicalPlist: URL
    let legacyPlist: URL
    let recorder: LaunchAgentProcessRecorder
}

@MainActor
final class LaunchAgentManagerTests: XCTestCase {

    // MARK: - Изолированные фикстуры

    private func makeFixture() throws -> LaunchAgentManagerFixture {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("KrabEarLaunchAgentTests-\(UUID().uuidString)", isDirectory: true)
            .standardizedFileURL
        let projectRoot = root.appendingPathComponent("project", isDirectory: true)
        let bundle = projectRoot.appendingPathComponent("Krab Ear.app", isDirectory: true)
        let launchAgentsDirectory = root
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        let recorder = LaunchAgentProcessRecorder()

        try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
        addTeardownBlock {
            // Удаляется только UUID-корень этого теста; пользовательский home не затрагивается.
            try? FileManager.default.removeItem(at: root)
        }

        let manager = LaunchAgentManager(
            projectRoot: projectRoot.path,
            launchAgentsDirectory: launchAgentsDirectory,
            processRunner: recorder.run(executable:arguments:)
        )
        return LaunchAgentManagerFixture(
            manager: manager,
            root: root,
            projectRoot: projectRoot,
            launchAgentsDirectory: launchAgentsDirectory,
            canonicalPlist: launchAgentsDirectory
                .appendingPathComponent("com.antigravity.krab-ear.plist"),
            legacyPlist: launchAgentsDirectory
                .appendingPathComponent("com.krabear.agent.plist"),
            recorder: recorder
        )
    }

    private func makeManager() throws -> LaunchAgentManager {
        try makeFixture().manager
    }

    // MARK: - Идентификаторы launchd

    func testLabelIsCanonical() throws {
        let manager = try makeManager()
        XCTAssertEqual(manager.labelForTest, "com.antigravity.krab-ear",
                       "Label must match canonical bundle ID com.antigravity.krab-ear")
    }

    func testLegacyLabelIsCorrect() throws {
        let manager = try makeManager()
        XCTAssertEqual(manager.legacyLabelForTest, "com.krabear.agent",
                       "Legacy label must be com.krabear.agent for migration cleanup")
    }

    // MARK: - Пути plist

    func testPlistPathContainsCanonicalLabel() throws {
        let manager = try makeManager()
        XCTAssertTrue(manager.plistPathForTest.contains("com.antigravity.krab-ear"),
                      "plistPath must contain canonical label com.antigravity.krab-ear, got: \(manager.plistPathForTest)")
    }

    func testPlistPathEndsWithPlist() throws {
        let manager = try makeManager()
        XCTAssertTrue(manager.plistPathForTest.hasSuffix(".plist"),
                      "plistPath must end with .plist, got: \(manager.plistPathForTest)")
    }

    func testPlistPathUsesInjectedLaunchAgentsDirectory() throws {
        let fixture = try makeFixture()
        XCTAssertEqual(fixture.manager.plistPathForTest, fixture.canonicalPlist.path)
        XCTAssertTrue(fixture.manager.plistPathForTest.hasPrefix(fixture.root.path))

        XCTAssertEqual(
            fixture.canonicalPlist.deletingLastPathComponent(),
            fixture.launchAgentsDirectory
        )
    }

    // MARK: - Содержимое ProgramArguments из Phase C.6.2

    func testPlistUsesOpenNotZsh() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertTrue(content.contains("/usr/bin/open"),
                      "ProgramArguments must use /usr/bin/open (not /bin/zsh), got:\n\(content)")
        XCTAssertFalse(content.contains("/bin/zsh"),
                       "ProgramArguments must NOT contain /bin/zsh, got:\n\(content)")
    }

    func testPlistUsesWFlag() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertTrue(content.contains("<string>-W</string>"),
                      "ProgramArguments must include -W flag for /usr/bin/open, got:\n\(content)")
    }

    func testPlistDoesNotReferenceStartAgentCommand() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertFalse(content.contains("start_agent.command"),
                       "ProgramArguments must NOT reference start_agent.command (Phase C.6.2), got:\n\(content)")
    }

    func testPlistDoesNotReferenceLaunchedByLaunchd() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertFalse(content.contains("--launched-by-launchd"),
                       "ProgramArguments must NOT include --launched-by-launchd, got:\n\(content)")
    }

    func testPlistContainsCanonicalLabel() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertTrue(content.contains("com.antigravity.krab-ear"),
                      "Plist Label must be com.antigravity.krab-ear, got:\n\(content)")
        XCTAssertFalse(content.contains("com.krabear.agent"),
                       "Plist Label must NOT be legacy com.krabear.agent, got:\n\(content)")
    }

    func testPlistContainsBundleAppExtension() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        // Аргумент bundle path обязан указывать на .app.
        XCTAssertTrue(content.contains(".app"),
                      "ProgramArguments must include a .app bundle path, got:\n\(content)")
    }

    func testPlistContainsRunAtLoad() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertTrue(content.contains("<key>RunAtLoad</key>"),
                      "Plist must contain RunAtLoad key, got:\n\(content)")
    }

    func testPlistContainsKeepAlive() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertTrue(content.contains("<key>KeepAlive</key>"),
                      "Plist must contain KeepAlive key, got:\n\(content)")
    }

    func testPlistDoesNotContainWorkingDirectory() throws {
        // WorkingDirectory удалён: `/usr/bin/open` в нём не нуждается.
        let manager = try makeManager()
        let content = manager.makePlistContent()
        XCTAssertFalse(content.contains("<key>WorkingDirectory</key>"),
                       "Plist must NOT contain WorkingDirectory (not needed for /usr/bin/open), got:\n\(content)")
    }

    // MARK: - Разрешение bundlePath

    func testBundlePathContainsApp() throws {
        let manager = try makeManager()
        let path = manager.bundlePathForTest
        // Фикстура создаёт bundle рядом с projectRoot, поэтому fallback не нужен.
        XCTAssertTrue(path.hasSuffix(".app") || path.contains("Krab Ear.app"),
                      "bundlePath must point to a .app bundle, got: \(path)")
    }

    func testBundlePathUsesBundleAdjacentToInjectedProjectRoot() throws {
        let fixture = try makeFixture()
        XCTAssertEqual(
            fixture.manager.bundlePathForTest,
            fixture.projectRoot.appendingPathComponent("Krab Ear.app").path
        )
    }

    // MARK: - Состояние установки в изолированном каталоге

    func test_isAutostartEnabled_returnsFalseWhenInjectedPlistIsAbsent() throws {
        let fixture = try makeFixture()
        XCTAssertFalse(fixture.manager.isAutostartEnabled())
    }

    func test_isAutostartEnabled_returnsTrueWhenInjectedPlistExists() throws {
        let fixture = try makeFixture()
        try FileManager.default.createDirectory(
            at: fixture.launchAgentsDirectory,
            withIntermediateDirectories: true
        )
        try Data("test".utf8).write(to: fixture.canonicalPlist)

        XCTAssertTrue(fixture.manager.isAutostartEnabled())
    }

    // MARK: - Создание plist через install()

    func test_installWritesInjectedPlistAndUsesInjectedRunner() throws {
        let fixture = try makeFixture()

        fixture.manager.install()

        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.canonicalPlist.path))
        XCTAssertEqual(
            fixture.recorder.calls,
            [
                .init(
                    executable: "/bin/launchctl",
                    arguments: ["bootout", "gui/\(getuid())", fixture.canonicalPlist.path]
                ),
                .init(
                    executable: "/bin/launchctl",
                    arguments: ["bootstrap", "gui/\(getuid())", fixture.canonicalPlist.path]
                ),
            ]
        )
        XCTAssertTrue(
            fixture.recorder.calls.allSatisfy { call in
                call.arguments.last?.hasPrefix(fixture.root.path) == true
            },
            "Все launchctl-пути теста обязаны оставаться внутри UUID-каталога"
        )
    }

    // MARK: - Идемпотентность install()

    func test_installIsIdempotentInsideInjectedDirectory() throws {
        let fixture = try makeFixture()

        fixture.manager.install()
        let firstContent = try String(contentsOf: fixture.canonicalPlist, encoding: .utf8)
        fixture.manager.install()
        let secondContent = try String(contentsOf: fixture.canonicalPlist, encoding: .utf8)

        XCTAssertEqual(firstContent, secondContent)
        XCTAssertEqual(fixture.recorder.calls.count, 4)
        XCTAssertTrue(
            fixture.recorder.calls.allSatisfy {
                $0.arguments.last == fixture.canonicalPlist.path
            }
        )
    }

    func test_installRemovesLegacyPlistThroughInjectedRunner() throws {
        let fixture = try makeFixture()
        try FileManager.default.createDirectory(
            at: fixture.launchAgentsDirectory,
            withIntermediateDirectories: true
        )
        try Data("legacy".utf8).write(to: fixture.legacyPlist)

        fixture.manager.install()

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.legacyPlist.path))
        XCTAssertEqual(
            fixture.recorder.calls.first,
            .init(
                executable: "/bin/launchctl",
                arguments: ["bootout", "gui/\(getuid())", fixture.legacyPlist.path]
            )
        )
    }

    func test_setAutostartRoutesBothDecisionsInsideInjectedDirectory() throws {
        let fixture = try makeFixture()

        fixture.manager.setAutostart(enabled: true)
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.canonicalPlist.path))
        fixture.manager.setAutostart(enabled: false)

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.canonicalPlist.path))
        XCTAssertEqual(fixture.recorder.calls.count, 3)
        XCTAssertTrue(
            fixture.recorder.calls.allSatisfy {
                $0.arguments.last == fixture.canonicalPlist.path
            }
        )
    }

    // MARK: - Удаление plist через uninstall()

    func test_uninstallRemovesOnlyInjectedPlistAndUsesInjectedRunner() throws {
        let fixture = try makeFixture()
        try FileManager.default.createDirectory(
            at: fixture.launchAgentsDirectory,
            withIntermediateDirectories: true
        )
        try Data("dummy".utf8).write(to: fixture.canonicalPlist)

        fixture.manager.uninstall()

        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.canonicalPlist.path))
        XCTAssertEqual(
            fixture.recorder.calls,
            [
                .init(
                    executable: "/bin/launchctl",
                    arguments: ["bootout", "gui/\(getuid())", fixture.canonicalPlist.path]
                ),
            ]
        )
    }

    // MARK: - Недоступный каталог

    func test_unavailableInjectedDirectoryIsHandledWithoutRealProcess() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("KrabEarLaunchAgentBlocked-\(UUID().uuidString)")
            .standardizedFileURL
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let regularFile = root.appendingPathComponent("regular-file")
        try Data("block directory creation".utf8).write(to: regularFile)
        let blockedDirectory = regularFile.appendingPathComponent("LaunchAgents")
        let recorder = LaunchAgentProcessRecorder()
        let manager = LaunchAgentManager(
            projectRoot: root.path,
            launchAgentsDirectory: blockedDirectory,
            processRunner: recorder.run(executable:arguments:)
        )

        manager.install()
        manager.uninstall()

        XCTAssertFalse(FileManager.default.fileExists(atPath: manager.plistPathForTest))
        XCTAssertEqual(recorder.calls.count, 3)
        XCTAssertTrue(recorder.calls.allSatisfy { $0.executable == "/bin/launchctl" })
    }

    // MARK: - Валидность XML plist

    func test_plist_format_valid_xml() throws {
        let manager = try makeManager()
        let content = manager.makePlistContent()

        // XML обязан начинаться декларацией.
        XCTAssertTrue(content.contains("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"),
                      "Plist must start with XML declaration")

        // Формат plist обязан объявлять DOCTYPE.
        XCTAssertTrue(content.contains("<!DOCTYPE plist"),
                      "Plist must contain DOCTYPE declaration")

        // Корневой элемент — <plist>.
        XCTAssertTrue(content.contains("<plist version=\"1.0\">") || content.contains("<plist version='1.0'>"),
                      "Plist must have root <plist> element")

        // Корневой элемент должен быть закрыт.
        XCTAssertTrue(content.contains("</plist>"),
                      "Plist must close root </plist> element")

        // Корневой plist содержит словарь.
        XCTAssertTrue(content.contains("<dict>"),
                      "Plist must contain <dict> root element")
        XCTAssertTrue(content.contains("</dict>"),
                      "Plist must close <dict> element")

        // Финальная проверка реальным XMLParser.
        let data = Data(content.utf8)
        let parser = XMLParser(data: data)
        let delegate = XMLParseErrorDelegate()
        parser.delegate = delegate
        let ok = parser.parse()
        XCTAssertTrue(ok && !delegate.hadError,
                      "makePlistContent() must produce parseable XML. Error: \(delegate.errorDescription ?? "none")")
    }

    // MARK: - Безопасность параллельного install

    func test_concurrentInstallRemainsInsideInjectedDirectory() throws {
        // Параллельные вызовы проверяют существующую атомарную запись plist;
        // recorder защищён lock и никогда не создаёт системный процесс.
        let fixture = try makeFixture()

        let expectation = self.expectation(description: "concurrent install")
        expectation.expectedFulfillmentCount = 5

        let queue = DispatchQueue(label: "test.concurrent", attributes: .concurrent)
        for _ in 0..<5 {
            queue.async {
                fixture.manager.install()
                expectation.fulfill()
            }
        }

        wait(for: [expectation], timeout: 10)

        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.canonicalPlist.path))

        // Побеждает последний writer, но итог обязан остаться валидным plist.
        if let written = try? String(contentsOf: fixture.canonicalPlist, encoding: .utf8) {
            XCTAssertTrue(written.contains("com.antigravity.krab-ear"),
                          "Plist written by concurrent install() must contain canonical label")
        }
        XCTAssertEqual(fixture.recorder.calls.count, 10)
        XCTAssertTrue(
            fixture.recorder.calls.allSatisfy {
                $0.arguments.last == fixture.canonicalPlist.path
            }
        )
    }
}

// MARK: - Вспомогательный XMLParseErrorDelegate

private final class XMLParseErrorDelegate: NSObject, XMLParserDelegate {
    var hadError = false
    var errorDescription: String?

    func parser(_ parser: XMLParser, parseErrorOccurred parseError: Error) {
        hadError = true
        errorDescription = parseError.localizedDescription
    }
}
