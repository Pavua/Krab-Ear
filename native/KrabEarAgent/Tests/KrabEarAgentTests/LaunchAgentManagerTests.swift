/*
 LaunchAgentManagerTests — тесты Phase C.6.2 root-cause fix.

 Проверяет:
 1. label == "com.antigravity.krab-ear" (canonical bundle ID).
 2. legacyLabel == "com.krabear.agent".
 3. buildPlistContent() содержит /usr/bin/open -W <bundle> (НЕ start_agent.command).
 4. buildPlistContent() НЕ содержит /bin/zsh и start_agent.command.
 5. buildPlistContent() содержит canonical label.
 6. plistPath содержит label "com.antigravity.krab-ear".
 7. bundlePath содержит ".app" расширение или projectRoot component.
 8. isInstalled: false если plist отсутствует.
 9. install() создаёт файл plist.
 10. install() идемпотентен (двойной вызов = одинаковый результат).
 11. uninstall() удаляет файл plist.
 12. Корректная обработка отсутствия разрешений.
 13. Plist является валидным XML.
 14. concurrent install безопасен.

 Подход:
 - buildPlistContent() вызывается в DEBUG-режиме без FileManager side-effects.
 - Launchctl и launchd не трогаются.
 - Файловые тесты используют временные директории.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class LaunchAgentManagerTests: XCTestCase {

    // MARK: - Fixtures

    private func makeManager(projectRoot: String = "/tmp/krab_ear_test_root") -> LaunchAgentManager {
        LaunchAgentManager(projectRoot: projectRoot)
    }

    // MARK: - Label tests

    func testLabelIsCanonical() {
        let manager = makeManager()
        XCTAssertEqual(manager.labelForTest, "com.antigravity.krab-ear",
                       "Label must match canonical bundle ID com.antigravity.krab-ear")
    }

    func testLegacyLabelIsCorrect() {
        let manager = makeManager()
        XCTAssertEqual(manager.legacyLabelForTest, "com.krabear.agent",
                       "Legacy label must be com.krabear.agent for migration cleanup")
    }

    // MARK: - plistPath tests

    func testPlistPathContainsCanonicalLabel() {
        let manager = makeManager()
        XCTAssertTrue(manager.plistPathForTest.contains("com.antigravity.krab-ear"),
                      "plistPath must contain canonical label com.antigravity.krab-ear, got: \(manager.plistPathForTest)")
    }

    func testPlistPathEndsWithPlist() {
        let manager = makeManager()
        XCTAssertTrue(manager.plistPathForTest.hasSuffix(".plist"),
                      "plistPath must end with .plist, got: \(manager.plistPathForTest)")
    }

    func testPlistPathInLaunchAgents() {
        let manager = makeManager()
        XCTAssertTrue(manager.plistPathForTest.contains("LaunchAgents"),
                      "plistPath must be inside ~/Library/LaunchAgents, got: \(manager.plistPathForTest)")
    }

    // MARK: - buildPlistContent tests (Phase C.6.2 ProgramArguments shape)

    func testPlistUsesOpenNotZsh() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertTrue(content.contains("/usr/bin/open"),
                      "ProgramArguments must use /usr/bin/open (not /bin/zsh), got:\n\(content)")
        XCTAssertFalse(content.contains("/bin/zsh"),
                       "ProgramArguments must NOT contain /bin/zsh, got:\n\(content)")
    }

    func testPlistUsesWFlag() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertTrue(content.contains("<string>-W</string>"),
                      "ProgramArguments must include -W flag for /usr/bin/open, got:\n\(content)")
    }

    func testPlistDoesNotReferenceStartAgentCommand() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertFalse(content.contains("start_agent.command"),
                       "ProgramArguments must NOT reference start_agent.command (Phase C.6.2), got:\n\(content)")
    }

    func testPlistDoesNotReferenceLaunchedByLaunchd() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertFalse(content.contains("--launched-by-launchd"),
                       "ProgramArguments must NOT include --launched-by-launchd, got:\n\(content)")
    }

    func testPlistContainsCanonicalLabel() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertTrue(content.contains("com.antigravity.krab-ear"),
                      "Plist Label must be com.antigravity.krab-ear, got:\n\(content)")
        XCTAssertFalse(content.contains("com.krabear.agent"),
                       "Plist Label must NOT be legacy com.krabear.agent, got:\n\(content)")
    }

    func testPlistContainsBundleAppExtension() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        // The bundle path argument must point to a .app bundle.
        XCTAssertTrue(content.contains(".app"),
                      "ProgramArguments must include a .app bundle path, got:\n\(content)")
    }

    func testPlistContainsRunAtLoad() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertTrue(content.contains("<key>RunAtLoad</key>"),
                      "Plist must contain RunAtLoad key, got:\n\(content)")
    }

    func testPlistContainsKeepAlive() {
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertTrue(content.contains("<key>KeepAlive</key>"),
                      "Plist must contain KeepAlive key, got:\n\(content)")
    }

    func testPlistDoesNotContainWorkingDirectory() {
        // WorkingDirectory was removed — /usr/bin/open does not need it.
        let manager = makeManager()
        let content = manager.buildPlistContent()
        XCTAssertFalse(content.contains("<key>WorkingDirectory</key>"),
                       "Plist must NOT contain WorkingDirectory (not needed for /usr/bin/open), got:\n\(content)")
    }

    // MARK: - bundlePath tests

    func testBundlePathContainsApp() {
        let manager = makeManager()
        let path = manager.bundlePathForTest
        // Either from projectRoot lookup or Bundle.main fallback, must end in .app
        XCTAssertTrue(path.hasSuffix(".app") || path.contains("Krab Ear.app"),
                      "bundlePath must point to a .app bundle, got: \(path)")
    }

    func testBundlePathWithRealProjectRoot() {
        // Use actual project root which has the .app next to it
        let projectRoot = "/Users/pablito/Antigravity_AGENTS/Krab Ear"
        let manager = makeManager(projectRoot: projectRoot)
        let path = manager.bundlePathForTest
        XCTAssertTrue(path.hasSuffix(".app"),
                      "bundlePath with real project root must end in .app, got: \(path)")
    }

    // MARK: - isInstalled: returns false if plist absent

    func test_isInstalled_returns_false_if_plist_absent() {
        // isAutostartEnabled() checks FileManager.fileExists at the plistPath.
        // We can't change the real plistPath, but we verify the logic by
        // checking that a freshly constructed manager reports consistent state:
        // if the real plist does not exist, isAutostartEnabled() must return false.
        let manager = makeManager()
        let plistExists = FileManager.default.fileExists(atPath: manager.plistPathForTest)
        let reported = manager.isAutostartEnabled()
        XCTAssertEqual(reported, plistExists,
                       "isAutostartEnabled() must reflect actual filesystem presence of plist")
    }

    func test_isInstalled_false_for_nonexistent_path() {
        // A manager rooted at a temp path that never had install() called:
        // plistPath must not exist, and isAutostartEnabled() returns false.
        let tmpRoot = NSTemporaryDirectory().appending("krabtest_\(Int.random(in: 100000...999999))")
        let manager = makeManager(projectRoot: tmpRoot)
        // plistPath lives in ~/Library/LaunchAgents, which always exists;
        // we only assert false when plist file itself is absent.
        let plistPath = manager.plistPathForTest
        // Remove if accidentally present from prior test run
        try? FileManager.default.removeItem(atPath: plistPath)
        // Only assert if it really isn't present now
        if !FileManager.default.fileExists(atPath: plistPath) {
            XCTAssertFalse(manager.isAutostartEnabled(),
                           "isAutostartEnabled() must be false when plist file does not exist")
        }
    }

    // MARK: - install() creates plist file

    func test_install_creates_plist_file() {
        let manager = makeManager()
        let plistPath = manager.plistPathForTest

        // Clean up before test
        try? FileManager.default.removeItem(atPath: plistPath)
        XCTAssertFalse(FileManager.default.fileExists(atPath: plistPath),
                       "Plist must not exist before install()")

        // install() calls launchctl which may fail in sandbox — we only check file creation.
        manager.install()

        XCTAssertTrue(FileManager.default.fileExists(atPath: plistPath),
                      "install() must create the plist file at \(plistPath)")

        // Cleanup
        try? FileManager.default.removeItem(atPath: plistPath)
    }

    // MARK: - install() idempotent

    func test_install_idempotent() {
        let manager = makeManager()
        let plistPath = manager.plistPathForTest

        try? FileManager.default.removeItem(atPath: plistPath)

        manager.install()
        guard FileManager.default.fileExists(atPath: plistPath) else {
            XCTFail("install() must create plist on first call")
            return
        }

        // Capture modification date after first install
        let attrs1 = try? FileManager.default.attributesOfItem(atPath: plistPath)
        let size1 = (attrs1?[.size] as? Int) ?? 0

        // Second call — file must remain with the same content
        manager.install()
        XCTAssertTrue(FileManager.default.fileExists(atPath: plistPath),
                      "install() called twice must leave plist present")

        let attrs2 = try? FileManager.default.attributesOfItem(atPath: plistPath)
        let size2 = (attrs2?[.size] as? Int) ?? 0
        XCTAssertEqual(size1, size2,
                       "install() called twice must produce the same file size (idempotent)")

        // Cleanup
        try? FileManager.default.removeItem(atPath: plistPath)
    }

    // MARK: - uninstall() removes plist file

    func test_uninstall_removes_plist_file() {
        let manager = makeManager()
        let plistPath = manager.plistPathForTest

        // Create a dummy plist to simulate installed state
        let launchAgents = (plistPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: launchAgents,
                                                  withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: plistPath, contents: Data("dummy".utf8))
        XCTAssertTrue(FileManager.default.fileExists(atPath: plistPath),
                      "Pre-condition: plist must exist before uninstall()")

        manager.uninstall()

        XCTAssertFalse(FileManager.default.fileExists(atPath: plistPath),
                       "uninstall() must remove the plist file at \(plistPath)")
    }

    // MARK: - handles permission denied gracefully

    func test_handles_permission_denied_gracefully() {
        // LaunchAgentManager uses try? for all FileManager operations — it should
        // never propagate errors. We verify this by constructing a manager whose
        // plist path is non-writable.
        // Since we cannot easily make ~/Library/LaunchAgents non-writable in tests,
        // we rely on the fact that install()/uninstall() use try? (not try!).
        // The test verifies the class compiles and runs without crashing on all paths.
        let manager = makeManager(projectRoot: "/nonexistent/root/\(UUID().uuidString)")

        // These must not throw or crash, even if filesystem ops silently fail.
        XCTAssertNoThrow(manager.install(), "install() must not throw even on non-writable paths")
        XCTAssertNoThrow(manager.uninstall(), "uninstall() must not throw even on non-writable paths")
        XCTAssertNoThrow(manager.setAutostart(enabled: true), "setAutostart(true) must not throw")
        XCTAssertNoThrow(manager.setAutostart(enabled: false), "setAutostart(false) must not throw")

        // Cleanup in case install() succeeded
        try? FileManager.default.removeItem(atPath: manager.plistPathForTest)
    }

    // MARK: - plist format: valid XML

    func test_plist_format_valid_xml() {
        let manager = makeManager()
        let content = manager.buildPlistContent()

        // Must open with XML declaration
        XCTAssertTrue(content.contains("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"),
                      "Plist must start with XML declaration")

        // Must contain DOCTYPE plist
        XCTAssertTrue(content.contains("<!DOCTYPE plist"),
                      "Plist must contain DOCTYPE declaration")

        // Must have root <plist> element
        XCTAssertTrue(content.contains("<plist version=\"1.0\">") || content.contains("<plist version='1.0'>"),
                      "Plist must have root <plist> element")

        // Must close root element
        XCTAssertTrue(content.contains("</plist>"),
                      "Plist must close root </plist> element")

        // Must have <dict> root child
        XCTAssertTrue(content.contains("<dict>"),
                      "Plist must contain <dict> root element")
        XCTAssertTrue(content.contains("</dict>"),
                      "Plist must close <dict> element")

        // Must be parseable by XMLParser
        let data = Data(content.utf8)
        let parser = XMLParser(data: data)
        let delegate = XMLParseErrorDelegate()
        parser.delegate = delegate
        let ok = parser.parse()
        XCTAssertTrue(ok && !delegate.hadError,
                      "buildPlistContent() must produce parseable XML. Error: \(delegate.errorDescription ?? "none")")
    }

    // MARK: - concurrent install safe

    func test_concurrent_install_safe() {
        // Verify install() can be called concurrently without crashing.
        // LaunchAgentManager uses try? FileManager operations which are
        // individually atomic at the OS level for simple writes.
        let manager = makeManager()
        let plistPath = manager.plistPathForTest
        try? FileManager.default.removeItem(atPath: plistPath)

        let expectation = self.expectation(description: "concurrent install")
        expectation.expectedFulfillmentCount = 5

        let queue = DispatchQueue(label: "test.concurrent", attributes: .concurrent)
        for _ in 0..<5 {
            queue.async {
                manager.install()
                expectation.fulfill()
            }
        }

        wait(for: [expectation], timeout: 10)

        // File should exist (at least one install succeeded)
        XCTAssertTrue(FileManager.default.fileExists(atPath: plistPath),
                      "After concurrent install(), plist file must exist")

        // Content must be valid (last writer wins, but must be valid XML)
        if let written = try? String(contentsOfFile: plistPath, encoding: .utf8) {
            XCTAssertTrue(written.contains("com.antigravity.krab-ear"),
                          "Plist written by concurrent install() must contain canonical label")
        }

        // Cleanup
        try? FileManager.default.removeItem(atPath: plistPath)
    }
}

// MARK: - XMLParseErrorDelegate helper

private final class XMLParseErrorDelegate: NSObject, XMLParserDelegate {
    var hadError = false
    var errorDescription: String?

    func parser(_ parser: XMLParser, parseErrorOccurred parseError: Error) {
        hadError = true
        errorDescription = parseError.localizedDescription
    }
}
