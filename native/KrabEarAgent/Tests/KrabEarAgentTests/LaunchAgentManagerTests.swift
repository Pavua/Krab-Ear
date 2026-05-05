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

 Подход:
 - buildPlistContent() вызывается в DEBUG-режиме без FileManager side-effects.
 - Launchctl и launchd не трогаются.
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
}
