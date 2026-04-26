/*
 HistoryPanelGigaamToggleTests — юнит-тесты pure helper'а
 `isGigaamVenvReady(at:)` из +GigaAMToggle.swift.

 Used in handler `onGigaamEnabledChanged` для pre-flight check
 перед IPC enable. Если venv нет — UI alert вместо backend ImportError.
*/

import XCTest
@testable import KrabEarAgent

final class HistoryPanelGigaamToggleTests: XCTestCase {

    func test_returnsFalse_forNonexistentPath() {
        XCTAssertFalse(
            HistoryPanelController.isGigaamVenvReady(at: "/definitely/not/here/python")
        )
    }

    func test_returnsFalse_forDirectoryNotFile() {
        XCTAssertFalse(
            HistoryPanelController.isGigaamVenvReady(at: "/tmp")
        )
    }

    func test_returnsTrue_forExistingExecutable() {
        // /usr/bin/python3 существует на macOS — используем как proxy.
        if FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") {
            XCTAssertTrue(
                HistoryPanelController.isGigaamVenvReady(at: "/usr/bin/python3")
            )
        } else {
            self.skipTest("/usr/bin/python3 не существует на этой системе")
        }
    }

    func test_returnsFalse_forNonExecutableFile() {
        // /etc/hosts существует на macOS, но не executable
        if FileManager.default.fileExists(atPath: "/etc/hosts") {
            XCTAssertFalse(
                HistoryPanelController.isGigaamVenvReady(at: "/etc/hosts"),
                "Non-executable file не должен считаться valid Python venv"
            )
        } else {
            self.skipTest("/etc/hosts не существует на этой системе")
        }
    }

    func test_returnsFalse_forEmptyPath() {
        XCTAssertFalse(HistoryPanelController.isGigaamVenvReady(at: ""))
    }

    private func skipTest(_ message: String) {
        // Replacement для XCTSkip (которое в test failure structure не chainable).
        XCTAssertTrue(true, "[SKIPPED] \(message)")
    }
}
