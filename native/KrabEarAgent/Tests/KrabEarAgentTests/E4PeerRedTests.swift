import XCTest
@testable import KrabEarAgent

private final class E4StubProvider: IPCSocketProviding, @unchecked Sendable {
    let respond: @Sendable (String, [String: Any], String) -> [String: Any]
    init(_ r: @escaping @Sendable (String, [String: Any], String) -> [String: Any]) { respond = r }
    func send(payload: Data, timeoutSec: Int) async throws -> Data {
        let req = try JSONSerialization.jsonObject(with: payload) as! [String: Any]
        let env = respond(req["method"] as! String, req["params"] as! [String: Any], req["id"] as! String)
        return try JSONSerialization.data(withJSONObject: env) + Data("\n".utf8)
    }
}

final class E4PeerRedTests: XCTestCase {
    // V01/V02: IPCClient принимает ответ без "ok" и с чужим id.
    func testRed_IPC_rejects_missing_ok_and_foreign_id() async {
        let c = IPCClient(socketProvider: E4StubProvider { _, _, _ in ["id": "FOREIGN", "result": [:] as [String: Any]] })
        do { _ = try await c.callAsync(method: "x"); XCTFail("accepted: no ok and foreign id") } catch {}
    }
    // V16: plist автозапуска ломается на & в пути.
    func testRed_plist_xml_escapes_path() throws {
        let root = NSTemporaryDirectory() + "e4&adv_" + UUID().uuidString
        try FileManager.default.createDirectory(atPath: root + "/Krab Ear.app", withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: root) }
        let m = LaunchAgentManager(projectRoot: root)
        let data = Data(m.makePlistContent().utf8)
        XCTAssertNoThrow(try PropertyListSerialization.propertyList(from: data, options: [], format: nil))
    }
    func testRed_plist_no_injection_via_path() throws {
        let root = NSTemporaryDirectory() + "x</string><string>-evil</string><string>y_" + UUID().uuidString
        try FileManager.default.createDirectory(atPath: root + "/Krab Ear.app", withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: root) }
        let m = LaunchAgentManager(projectRoot: root)
        let data = Data(m.makePlistContent().utf8)
        let obj = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any]
        let args = obj?["ProgramArguments"] as? [String]
        XCTAssertEqual(args?.count, 3, "ProgramArguments injected/invalid: \(String(describing: args))")
    }
    // Killer P01: противоречивая политика (ok:true + privacy:true) должна отказать на клиенте.
    @MainActor func testKiller_P01_contradictory_privacy_policy() async {
        let p = E4StubProvider { m, _, id in
            ["id": id, "ok": true, "result": ["ok": true, "epoch": String(repeating: "a", count: 64), "policy_generation": 1,
              "encryption_enabled": false, "privacy_mode_enabled": true, "allowed_without_grant": false] as [String: Any]]
        }
        let c = PlaintextExportCoordinator(ipc: IPCClient(socketProvider: p))
        do { _ = try await c.prepare(sink: .historyMarkdown, consent: { true }); XCTFail("privacy policy ignored") }
        catch PlaintextExportCoordinator.Failure.privacy {} catch { XCTFail("wrong error \(error)") }
    }
    // Killer P03: пустой id в envelope.
    @MainActor func testKiller_P03_empty_envelope_id() async {
        let p = E4StubProvider { m, _, id in
            ["id": "", "ok": true, "result": ["ok": true, "epoch": String(repeating: "a", count: 64), "policy_generation": 1,
              "encryption_enabled": false, "privacy_mode_enabled": false, "allowed_without_grant": true] as [String: Any]]
        }
        let c = PlaintextExportCoordinator(ipc: IPCClient(socketProvider: p))
        do { _ = try await c.prepare(sink: .historyMarkdown, consent: { true }); XCTFail("empty id accepted") } catch {}
    }
    // Killer P05: вызывающий не может подставить свой plaintext_export.
    @MainActor func testKiller_P05_caller_supplied_namespace() async throws {
        let p = E4StubProvider { m, params, id in
            let r: [String: Any]
            if m == "get_plaintext_export_policy" {
                r = ["ok": true, "epoch": String(repeating: "a", count: 64), "policy_generation": 1,
                     "encryption_enabled": false, "privacy_mode_enabled": false, "allowed_without_grant": true]
            } else { r = ["path": "/x.md", "total_items": 1, "content": "c"] }
            return ["id": id, "ok": true, "result": r]
        }
        let c = PlaintextExportCoordinator(ipc: IPCClient(socketProvider: p))
        let t = try await c.prepareBackend(method: .exportHistoryMarkdown, consent: { true })
        do { _ = try await c.performBackend(t, params: ["plaintext_export": ["capability": "forged"]]); XCTFail("forged namespace accepted") } catch {}
    }
    // Killer G01: существующий лог не должен усекаться при открытии.
    func testKiller_G01_existing_log_preserved() throws {
        let dir = NSTemporaryDirectory() + "e4g01_" + UUID().uuidString
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: dir) }
        try "OLD-LINE\n".write(toFile: dir + "/agent.log", atomically: true, encoding: .utf8)
        let l = AgentLogger(dataDirPath: dir); l.info("new"); l.waitForPendingWrites()
        let s = try String(contentsOfFile: dir + "/agent.log")
        XCTAssertTrue(s.contains("OLD-LINE"), "log truncated: \(s)")
    }
}
