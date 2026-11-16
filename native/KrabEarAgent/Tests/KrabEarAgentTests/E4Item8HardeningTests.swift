/*
 E4HardeningItem8 — N2, N3, V10 из внешнего ревью Swift-агента (2026-11-16).

 N2: resolveProjectRoot не доверяет текущему каталогу процесса. В active-режиме
     supervisor запускает `<projectRoot>/.venv_krab_ear/bin/python`, поэтому «корень»
     из чужого cwd = исполнение чужого интерпретатора.
 N3: Bearer-ключ REST не уходит на хост, который не loopback.
 V10: pause(.privacyMode) до activate() не теряется.
*/

import XCTest
@testable import KrabEarAgent

final class E4Item8ProjectRootCwdTests: XCTestCase {
    private var tmp: URL!
    private var savedCwd: String!

    override func setUp() {
        super.setUp()
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("E4Item8Cwd-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        savedCwd = FileManager.default.currentDirectoryPath
    }

    override func tearDown() {
        FileManager.default.changeCurrentDirectoryPath(savedCwd)
        try? FileManager.default.removeItem(at: tmp)
        super.tearDown()
    }

    private func plantMarker(at root: URL) {
        let dir = root.appendingPathComponent("KrabEar/backend")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: dir.appendingPathComponent("service.py").path, contents: Data("# fixture".utf8))
    }

    func test_cwdWithBackendMarker_isNotTrusted() throws {
        let hostile = tmp.appendingPathComponent("hostile-cwd")
        plantMarker(at: hostile)
        // Исполняемый файл вне любого репозитория: walk-up ничего не найдёт.
        let exec = tmp.appendingPathComponent("elsewhere/bin/KrabEarAgent").path
        XCTAssertTrue(FileManager.default.changeCurrentDirectoryPath(hostile.path))

        let options = LaunchOptions(arguments: [exec])

        XCTAssertNotEqual(
            URL(fileURLWithPath: options.projectRoot).standardizedFileURL.resolvingSymlinksInPath().path,
            hostile.standardizedFileURL.resolvingSymlinksInPath().path,
            "projectRoot взят из текущего каталога: чужой .venv_krab_ear/bin/python будет запущен supervisor'ом")
    }

    func test_noRootFound_fallbackIsNeverCwd() throws {
        let hostile = tmp.appendingPathComponent("cwd-no-marker")
        try FileManager.default.createDirectory(at: hostile, withIntermediateDirectories: true)
        let exec = tmp.appendingPathComponent("elsewhere/bin/KrabEarAgent").path
        XCTAssertTrue(FileManager.default.changeCurrentDirectoryPath(hostile.path))

        let options = LaunchOptions(arguments: [exec])

        XCTAssertNotEqual(
            URL(fileURLWithPath: options.projectRoot).standardizedFileURL.resolvingSymlinksInPath().path,
            hostile.standardizedFileURL.resolvingSymlinksInPath().path,
            "запасной вариант «вернуть cwd» отдаёт чужому каталогу место корня проекта")
    }
}

final class E4Item8RestAuthLoopbackTests: XCTestCase {
    private let key = String(repeating: "k", count: 43)

    private func keyFile() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("E4Item8Auth-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("rest_api_key")
        try key.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    func test_bearer_isNotSentToNonLoopbackHost() throws {
        let file = try keyFile()
        for raw in ["http://evil.example.com:5005/v1/events",
                    "http://192.168.1.10:5005/v1/events",
                    "http://127.0.0.1.evil.example.com/v1/events"] {
            var req = URLRequest(url: URL(string: raw)!)
            RestAuth.apply(to: &req, environment: [:], defaultFile: file)
            XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"), "ключ ушёл на \(raw)")
        }
    }

    func test_bearer_isSentToLoopbackHosts() throws {
        let file = try keyFile()
        for raw in ["http://127.0.0.1:5005/v1/events",
                    "http://localhost:5005/v1/events",
                    "http://[::1]:5005/v1/events"] {
            var req = URLRequest(url: URL(string: raw)!)
            RestAuth.apply(to: &req, environment: [:], defaultFile: file)
            XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer \(key)", raw)
        }
    }
}

@MainActor
final class E4Item8WakeWordPauseTests: XCTestCase {
    func test_privacyPauseBeforeActivate_isRememberedAndActivateDoesNotStart() {
        var ipcLookups = 0
        let poller = WakeWordPoller(
            ipcProvider: { ipcLookups += 1; return nil },
            isToggleEnabled: { true },
            onDetection: {})
        addTeardownBlock { @MainActor in poller.deactivate() }

        poller.pause(.privacyMode)          // таймера ещё нет
        poller.activate()

        XCTAssertTrue(poller.isPaused, "пауза privacy потеряна, пока таймер не был запущен")
        XCTAssertEqual(ipcLookups, 0, "activate() запустил wake_word_start при активной паузе privacy")
    }

    func test_deactivateKeepsPrivacyPause() {
        let poller = WakeWordPoller(
            ipcProvider: { nil }, isToggleEnabled: { true }, onDetection: {})
        poller.activate()
        poller.pause(.privacyMode)
        poller.deactivate()
        XCTAssertTrue(poller.isPaused, "deactivate() стёр паузу privacy: повторное включение стартует микрофон")
        poller.resume(.privacyMode)
        XCTAssertFalse(poller.isPaused)
    }
}
