/*
 BackendSupervisorSocketOwnershipTests — супервизор не владеет socket-путём.

 Волна socket-ownership (PR #1944, спека 2026-08-22): единственный владелец
 pathname'а `krabear.sock` — Python-backend, который чистит stale ТОЛЬКО под
 sidecar-flock claim'ом с re-check identity (dev, ino, mtime_ns).

 До этих тестов Swift-супервизор в active-режиме делал безусловный
 `removeItem(atPath: socketPath)` перед спавном ребёнка. Живой класс отказа в
 dev-режиме: ping не уложился в таймаут на живом backend'е → супервизор срывал
 имя работающего сокета (сам сокет остаётся жив на unlink'нутом inode,
 недостижим по пути) → новый ребёнок упирался в flock оригинала и выходил с
 EX_TEMPFAIL → ноль достижимых backend'ов до ручного вмешательства.

 Тесты держат оба конца инварианта: живой сокет не удаляется, stale-сокет тоже
 не удаляется (это работа claim'а на стороне Python, а не гонка двух чистильщиков).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Помощники

/// Настоящий AF_UNIX listener на заданном пути — не заглушка файла: тест обязан
/// отличать «файл пережил вызов» от «живой endpoint пережил вызов».
private final class TestUnixSocketListener {
    enum Failure: Error {
        case socketCreateFailed(errno: Int32)
        case pathTooLong
        case bindFailed(errno: Int32)
        case listenFailed(errno: Int32)
    }

    private var fd: Int32
    let path: String

    init(path: String) throws {
        self.path = path
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.socketCreateFailed(errno: errno) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.utf8)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard bytes.count < capacity else {
            Darwin.close(fd)
            throw Failure.pathTooLong
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { tuple in
            tuple.withMemoryRebound(to: UInt8.self, capacity: capacity) { dst in
                for (index, byte) in bytes.enumerated() { dst[index] = byte }
                dst[bytes.count] = 0
            }
        }

        let bound = withUnsafePointer(to: &addr) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else {
            let code = errno
            Darwin.close(fd)
            throw Failure.bindFailed(errno: code)
        }
        guard listen(fd, 4) == 0 else {
            let code = errno
            Darwin.close(fd)
            throw Failure.listenFailed(errno: code)
        }
    }

    /// Закрывает дескриптор, ОСТАВЛЯЯ файл на месте — ровно то, что остаётся
    /// после SIGKILL'нутого backend'а: stale-сокет, connect в ECONNREFUSED.
    func closeKeepingPath() {
        guard fd >= 0 else { return }
        Darwin.close(fd)
        fd = -1
    }
}

/// Сценарий ping'а: первый вызов не укладывается (тугой таймаут под нагрузкой),
/// последующие отвечают. Именно этот порядок заводит active-ветку
/// ensureBackendRunning на живом backend'е.
private final class TestPingScript: @unchecked Sendable {
    private let lock = NSLock()
    private var results: [Bool]
    private let tail: Bool

    init(_ results: [Bool], thenAlways tail: Bool) {
        self.results = results
        self.tail = tail
    }

    func next() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !results.isEmpty else { return tail }
        return results.removeFirst()
    }
}

// MARK: - Тесты

final class BackendSupervisorSocketOwnershipTests: XCTestCase {

    private var tempDir: String!

    override func setUpWithError() throws {
        try super.setUpWithError()
        // Короткий путь: sun_path ограничен 104 байтами, /var/folders/... из
        // NSTemporaryDirectory съедает почти весь бюджет.
        tempDir = "/tmp/ke-sock-\(UUID().uuidString.prefix(8))"
        try FileManager.default.createDirectory(
            atPath: tempDir, withIntermediateDirectories: true
        )
    }

    override func tearDownWithError() throws {
        if let dir = tempDir { try? FileManager.default.removeItem(atPath: dir) }
        tempDir = nil
        try super.tearDownWithError()
    }

    private func makeActiveSupervisor(pings: TestPingScript) -> BackendSupervisor {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test_krab", dataDir: tempDir)
        supervisor.overrideSupervisionMode(.active)
        supervisor._testPingOverride = { pings.next() }
        supervisor._testSpawnOverride = { }
        return supervisor
    }

    /// Живой endpoint переживает active-ветку ensureBackendRunning.
    func test_ensureBackendRunning_active_doesNotRemoveLiveSocket() throws {
        let pings = TestPingScript([false], thenAlways: true)
        let supervisor = makeActiveSupervisor(pings: pings)
        let listener = try TestUnixSocketListener(path: supervisor.socketPath)
        defer { listener.closeKeepingPath() }

        try supervisor.ensureBackendRunning()

        XCTAssertTrue(
            FileManager.default.fileExists(atPath: supervisor.socketPath),
            "Супервизор сорвал имя ЖИВОГО сокета: backend остаётся жив на unlink'нутом "
                + "inode, а новый ребёнок упрётся в его flock (EX_TEMPFAIL)"
        )
    }

    /// Async-близнец: обе ветки ходили через один и тот же cleanup, обе обязаны
    /// быть закрыты (класс sibling-gate asymmetry).
    func test_ensureBackendRunningAsync_active_doesNotRemoveLiveSocket() async throws {
        let pings = TestPingScript([false], thenAlways: true)
        let supervisor = makeActiveSupervisor(pings: pings)
        let listener = try TestUnixSocketListener(path: supervisor.socketPath)
        defer { listener.closeKeepingPath() }

        try await supervisor.ensureBackendRunningAsync()

        XCTAssertTrue(
            FileManager.default.fileExists(atPath: supervisor.socketPath),
            "Async-ветка обязана вести себя так же, как синхронная"
        )
    }

    /// Даже доказанно stale-сокет удаляет НЕ супервизор: между его probe и
    /// unlink успевает забиндиться свежий backend. Чистка живёт в
    /// SocketOwnershipClaim.prepare_for_bind под flock'ом с re-check identity.
    func test_ensureBackendRunning_active_leavesStaleSocketToBackendClaim() throws {
        let pings = TestPingScript([false], thenAlways: true)
        let supervisor = makeActiveSupervisor(pings: pings)
        let listener = try TestUnixSocketListener(path: supervisor.socketPath)
        listener.closeKeepingPath()  // остался файл сокета без слушателя

        try supervisor.ensureBackendRunning()

        XCTAssertTrue(
            FileManager.default.fileExists(atPath: supervisor.socketPath),
            "Владелец pathname'а — Python-claim; второй чистильщик в Swift "
                + "воспроизводит ровно ту гонку, которую закрыла спека 2026-08-22"
        )
    }

    /// Source-контракт: в супервизоре не должно остаться удаления socket-пути
    /// ни под каким именем (поведенческие тесты выше закрывают только те два
    /// call site'а, что существовали на момент фикса).
    func test_supervisorSource_hasNoSocketUnlink() throws {
        let sourcePath = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/BackendSupervisor.swift")
        let source = try String(contentsOf: sourcePath, encoding: .utf8)
        // Гард проверяет КОД, а не комментарии: у startBackendProcess стоит
        // разбор инцидента, называющий запрещённый паттерн дословно, и
        // substring-совпадение по нему краснило бы корректную реализацию.
        let code = source
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> Substring in
                guard let commentStart = line.range(of: "//") else { return line }
                return line[line.startIndex..<commentStart.lowerBound]
            }
            .joined(separator: "\n")

        for forbidden in ["cleanupStaleSocket", "removeItem(atPath: socketPath)", "unlink("] {
            XCTAssertFalse(
                code.contains(forbidden),
                "Супервизор не владеет socket-путём — запрещённый паттерн: \(forbidden)"
            )
        }
    }
}
