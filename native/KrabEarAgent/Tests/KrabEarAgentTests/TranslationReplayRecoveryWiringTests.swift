import AppKit
import XCTest
@testable import KrabEarAgent

private struct ReplayResponseBox: @unchecked Sendable {
    let value: [String: Any]
}

private final class ReplayIPCBox: @unchecked Sendable {
    let client: IPCClient
    init(path: String) { client = IPCClient(socketPath: path) }
    func read(_ request: Data) throws -> Data {
        let params = try JSONSerialization.jsonObject(with: request) as! [String: Any]
        let response = try client.call(method: "get_event_log", params: params, timeoutSec: 2)
        return try JSONSerialization.data(withJSONObject: response)
    }
}

@MainActor
final class TranslationReplayRecoveryWiringTests: XCTestCase {
    nonisolated let epoch = String(repeating: "a", count: 32)
    nonisolated func page(_ seq: Int, texts: [String], more: Bool = false, gap: String? = nil) -> [String: Any] {
        var result: [String: Any] = ["cursor": "\(epoch):\(seq)", "has_more": more, "privacy_mode": false, "generation": String(repeating: "c", count: 32),
            "events": texts.enumerated().map { offset, text in
                ["type": "live_subs.result", "seq": seq - texts.count + offset + 1,
                 "data": ["text": text, "translation": text]] as [String: Any]
            }]
        if let gap { result["gap"] = gap }
        if gap == "reset" { result["reset_reason"] = "restart" }
        return result
    }
    func settle(_ predicate: @escaping () -> Bool) async {
        for _ in 0..<1000 {
            if predicate() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("replay condition did not settle")
    }
    func testMissedIdenticalPhrasesAreDeliveredOnceInOrderAcrossPages() async {
        var calls = 0
        var texts: [String] = []
        let owner = TranslationReplaySubscription(fetch: { params in
            calls += 1
            if calls == 1 { return self.page(1, texts: ["before"]) }
            if calls == 2 {
                XCTAssertEqual(params["cursor"] as? String, "\(self.epoch):1")
                return self.page(2, texts: ["повтор"], more: true)
            }
            if calls == 3 { return self.page(3, texts: ["повтор"]) }
            return self.page(3, texts: [])
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { _ in XCTFail() })
        defer { owner.stop() }
        owner.start(pollInterval: 3600)
        await settle { texts == ["before"] }
        owner.reconcile()
        await settle { texts.count == 3 }
        owner.reconcile()
        await settle { calls == 4 }
        XCTAssertEqual(texts, ["before", "повтор", "повтор"])
    }

    func testOverlapHintsCoalesceAndStopDiscardsLateResponse() async {
        var pending: CheckedContinuation<ReplayResponseBox, Error>?
        var calls = 0
        var received = 0
        var owner: TranslationReplaySubscription? = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            return try await withCheckedThrowingContinuation { pending = $0 }.value
        }, onEntries: { received += $0.count }, onGap: { _ in })
        owner!.start(pollInterval: 3600)
        await settle { pending != nil }
        for _ in 0..<50 { owner!.reconcile() }
        XCTAssertEqual(calls, 1)
        weak var released = owner
        owner!.stop(); owner = nil
        XCTAssertNil(released, "in-flight IPC must not retain owner")
        pending?.resume(returning: ReplayResponseBox(value: page(1, texts: ["late"])))
        await Task.yield()
        XCTAssertEqual(received, 0)
    }

    func testFailedAndMalformedPagesKeepCursorAndExistingPhrases() async {
        var calls = 0; var texts: [String] = []
        let owner = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            switch calls {
            case 1: return self.page(1, texts: ["kept"])
            case 2: throw NSError(domain: "offline", code: 1)
            case 3: return ["cursor": "broken", "events": [], "has_more": false]
            case 4: return self.page(0, texts: ["backwards"])
            default: return self.page(2, texts: ["caught up"])
            }
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { _ in XCTFail() })
        defer { owner.stop() }; owner.start(pollInterval: 3600)
        await settle { texts.count == 1 }
        for count in 2...4 {
            owner.reconcile(); await settle { calls == count }; await Task.yield()
            XCTAssertEqual(owner.cursor, "\(epoch):1")
            XCTAssertEqual(texts, ["kept"])
        }
        owner.reconcile(); await settle { texts.count == 2 }
        XCTAssertEqual(texts, ["kept", "caught up"])
    }

    func testResetSurfacesGapAndAcceptsNewJournalWithoutTextDedup() async {
        var calls = 0; var texts: [String] = []; var gaps: [String] = []
        let owner = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            if calls == 1 { return self.page(8, texts: ["повтор"]) }
            var result = self.page(1, texts: ["повтор"], gap: "reset")
            result["cursor"] = "\(String(repeating: "b", count: 32)):1"
            return result
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { gaps.append($0) })
        defer { owner.stop() }; owner.start(pollInterval: 3600)
        await settle { texts.count == 1 }
        owner.reconcile(); await settle { texts.count == 2 }
        XCTAssertEqual(gaps, ["reset"])
        XCTAssertEqual(texts, ["повтор", "повтор"])
    }
}

@MainActor
final class TranslationPanelReplayWiringTests: XCTestCase {
    func waitUntil(_ predicate: @escaping () -> Bool) async {
        for _ in 0..<1000 {
            if predicate() { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("panel recovery did not settle")
    }
    func testPanelPreservesPhrasesAndCatchesUpAfterCleanEOF() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        var disconnected = false
        let data = TranslationReplayRecoveryWiringTests()
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                if !disconnected {
                    return data.page(1, texts: params["cursor"] == nil ? ["до обрыва"] : [])
                }
                return data.page(3, texts: params["cursor"] as? String == "\(data.epoch):3" ? [] : ["повтор", "повтор"])
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals == ["до обрыва"] }
        disconnected = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { factory.sessions.count >= 2 }
        await waitUntil { view?.displayedOriginals == ["до обрыва", "повтор", "повтор"] }
        weak var released = view
        view = nil
        XCTAssertNil(released)
        XCTAssertTrue(factory.sessions.allSatisfy { $0.invalidations == 1 })
        XCTAssertTrue(factory.sessions.allSatisfy { $0.task.cancellations == 1 })
    }
}

extension TranslationReplayRecoveryWiringTests {
    func testHintsDuringReadTriggerOneFollowupWithoutConcurrentIPC() async {
        var pending: CheckedContinuation<ReplayResponseBox, Error>?
        var calls = 0; var texts: [String] = []
        let owner = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            if calls == 1 { return try await withCheckedThrowingContinuation { pending = $0 }.value }
            return self.page(2, texts: ["during read"])
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { _ in })
        defer { owner.stop() }; owner.start(pollInterval: 3600)
        await settle { pending != nil }
        for _ in 0..<50 { owner.reconcile() }
        XCTAssertEqual(calls, 1)
        pending?.resume(returning: ReplayResponseBox(value: page(1, texts: ["first"])))
        await settle { texts.count == 2 }
        XCTAssertEqual(calls, 2)
        XCTAssertEqual(texts, ["first", "during read"])
    }

    func testPeriodicRepairCatchesPhraseWithoutAnySSEHint() async {
        var calls = 0; var texts: [String] = []
        var owner: TranslationReplaySubscription? = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            return calls == 1 ? self.page(0, texts: []) : self.page(1, texts: ["lost hint"])
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { _ in })
        owner!.start(pollInterval: 0.05)
        await settle { texts == ["lost hint"] }
        weak var released = owner
        owner = nil
        XCTAssertNil(released, "polling timer must not retain replay owner")
    }

    func testInvalidPagesAreAtomicAndCannotAdvanceCursor() async {
        var calls = 0; var texts: [String] = []
        let owner = TranslationReplaySubscription(fetch: { _ in
            calls += 1
            if calls == 1 { return self.page(1, texts: ["kept"]) }
            if calls == 2 {
                var p = self.page(3, texts: ["bad", "bad"])
                p["events"] = [["type": "live_subs.result", "seq": 2, "data": ["text": "first"]],
                               ["type": "live_subs.result", "seq": 2, "data": ["text": "duplicate"]]]
                return p
            }
            if calls == 3 { return self.page(202, texts: Array(repeating: "overflow", count: 201)) }
            if calls == 4 { return self.page(2, texts: [], more: true) }
            return self.page(2, texts: ["recovered"])
        }, onEntries: { texts += $0.compactMap { $0["text"] as? String } }, onGap: { _ in XCTFail() })
        defer { owner.stop() }; owner.start(pollInterval: 3600)
        await settle { texts.count == 1 }
        for count in 2...4 {
            owner.reconcile(); await settle { calls == count }; await Task.yield()
            XCTAssertEqual(owner.cursor, "\(epoch):1")
            XCTAssertEqual(texts, ["kept"])
        }
        owner.reconcile(); await settle { texts.count == 2 }
        XCTAssertEqual(texts, ["kept", "recovered"])
    }

    func testRedactedPayloadIsNeverShownEvenIfItContainsText() async {
        var received: [[String: Any]] = []
        let owner = TranslationReplaySubscription(fetch: { _ in
            ["cursor": "\(self.epoch):1", "has_more": false, "privacy_mode": false,
             "events": [["type": "live_subs.result", "seq": 1,
                         "data": ["text": "private", "redacted": true]]]]
        }, onEntries: { received += $0 }, onGap: { _ in XCTFail() })
        defer { owner.stop() }; owner.start(pollInterval: 3600)
        await settle { owner.cursor != nil }
        XCTAssertTrue(received.isEmpty)
    }
}

extension TranslationPanelReplayWiringTests {
    func testRealHTTPReconnectAndUnixIPCRecoverPythonReplayWithoutDuplicateText() async throws {
        _ = NSApplication.shared
        let root = URL(fileURLWithPath: "/tmp/ear-replay-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        var repo = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { repo.deleteLastPathComponent() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", repo.appendingPathComponent("KrabEar/tests/fixtures/translation_replay_loopback.py").path, root.path]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try process.run()
        defer { if process.isRunning { process.terminate() }; process.waitUntilExit() }
        let ready = root.appendingPathComponent("ready.json")
        await waitUntil { FileManager.default.fileExists(atPath: ready.path) || !process.isRunning }
        guard process.isRunning else { XCTFail("synthetic Python peer failed to start"); return }
        let info = try JSONSerialization.jsonObject(with: Data(contentsOf: ready)) as! [String: Any]
        let port = try XCTUnwrap(info["port"] as? Int)
        let client = ReplayIPCBox(path: root.appendingPathComponent("ipc.sock").path)
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://127.0.0.1:\(port)", sessionFactory: { delegate in
                let queue = OperationQueue(); queue.maxConcurrentOperationCount = 1
                return URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: queue)
            }, replayFetch: { params in
                let request = try JSONSerialization.data(withJSONObject: params)
                let bytes = try await Task.detached { try client.read(request) }.value
                let response = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
                return try XCTUnwrap(response["result"] as? [String: Any])
            }, pollInterval: 3600)
        await waitUntil {
            guard let bytes = try? Data(contentsOf: ready),
                  let state = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { return false }
            return state["connections"] as? Int == 2 && view?.displayedOriginals == ["до обрыва", "повтор", "повтор"]
        }
        XCTAssertEqual(view?.displayedOriginals, ["до обрыва", "повтор", "повтор"])
        weak var released = view
        view = nil
        XCTAssertNil(released)
    }
}

extension TranslationPanelReplayWiringTests {
    func testPanelKeepsOnlyLatest200PhrasesAcrossRecoveryPages() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let data = TranslationReplayRecoveryWiringTests()
        var disconnected = false
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                let end = disconnected ? 400 : 200
                let alreadyRead = params["cursor"] as? String == "\(data.epoch):\(end)"
                return data.page(end, texts: alreadyRead ? [] : (end-200..<end).map(String.init))
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals.count == 200 }
        disconnected = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { view?.displayedOriginals.first == "200" }
        XCTAssertEqual(view?.displayedOriginals, (200..<400).map(String.init))
        view = nil
    }

    func testPanelShowsGapAndPreservesExistingPhrasesAfterBackendReset() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let data = TranslationReplayRecoveryWiringTests()
        var disconnected = false
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                if !disconnected {
                    return data.page(8, texts: params["cursor"] == nil ? ["before reset"] : [])
                }
                let newCursor = "\(String(repeating: "b", count: 32)):1"
                var page = data.page(1, texts: params["cursor"] as? String == newCursor ? [] : ["after reset"],
                                     gap: params["cursor"] as? String == newCursor ? nil : "reset")
                page["cursor"] = newCursor
                return page
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals == ["before reset"] }
        disconnected = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { view?.displayedOriginals.last == "after reset" }
        XCTAssertEqual(view?.displayedOriginals.first, "before reset")
        XCTAssertEqual(view?.displayedOriginals.count, 3)
        XCTAssertTrue(view?.displayedOriginals[1].contains("Разрыв истории") == true)
        view = nil
    }
}

extension TranslationPanelReplayWiringTests {
    func testPrivacyReadClearsAlreadyDisplayedPhrasesWithoutReplayPayload() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let data = TranslationReplayRecoveryWiringTests()
        var privacy = false
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                var p = data.page(1, texts: !privacy && params["cursor"] == nil ? ["private phrase"] : [])
                p["privacy_mode"] = privacy
                return p
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals == ["private phrase"] }
        privacy = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { view?.displayedOriginals.isEmpty == true }
        view = nil
    }

    func testPurgeClearsAlreadyDisplayedPhrasesWhileRestartPreservesThem() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let data = TranslationReplayRecoveryWiringTests()
        var purged = false
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                if !purged { return data.page(1, texts: params["cursor"] == nil ? ["purged phrase"] : []) }
                let newCursor = "\(String(repeating: "b", count: 32)):1"
                let first = params["cursor"] as? String != newCursor
                var p = data.page(1, texts: first ? ["new phrase"] : [], gap: first ? "reset" : nil)
                p["cursor"] = newCursor
                if first { p["reset_reason"] = "clear" }
                p["privacy_mode"] = false
                return p
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals == ["purged phrase"] }
        purged = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { view?.displayedOriginals.last == "new phrase" }
        XCTAssertFalse(view?.displayedOriginals.contains("purged phrase") == true)
        view = nil
    }
}

extension TranslationPanelReplayWiringTests {
    func testPurgeFollowedByBackendRestartCannotResurrectCachedPhrases() async {
        _ = NSApplication.shared
        let factory = RecoverySessionFactory()
        let data = TranslationReplayRecoveryWiringTests()
        var restartedAfterPurge = false
        var view: TranslationStreamView? = TranslationStreamView(frame: .zero,
            restBaseURL: "http://translation.invalid", sessionFactory: { factory.make($0) },
            replayFetch: { params in
                if !restartedAfterPurge {
                    return data.page(1, texts: params["cursor"] == nil ? ["purged before restart"] : [])
                }
                let newCursor = "\(String(repeating: "b", count: 32)):1"
                let first = params["cursor"] as? String != newCursor
                var p = data.page(1, texts: first ? ["new session"] : [], gap: first ? "reset" : nil)
                p["cursor"] = newCursor
                p["generation"] = String(repeating: "d", count: 32)
                return p
            }, pollInterval: 3600)
        await waitUntil { view?.displayedOriginals == ["purged before restart"] }
        restartedAfterPurge = true
        factory.delegate(at: 0)._testComplete()
        await waitUntil { view?.displayedOriginals.last == "new session" }
        XCTAssertFalse(view?.displayedOriginals.contains("purged before restart") == true)
        view = nil
    }
}
