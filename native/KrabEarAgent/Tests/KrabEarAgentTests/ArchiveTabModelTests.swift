import AppKit
import XCTest
@testable import KrabEarAgent

/// Карта ревью dots 26.11: итог восстановления привязан к ID; старый/пустой ответ не успех;
/// предупреждение закрывается только сверкой со свежим списком. Данные вымышленные.
@MainActor
final class ArchiveTabModelTests: XCTestCase {

    private func restoreResponse(restored: [String]?, failed: [String]?, notFound: [String] = []) -> [String: Any] {
        var result: [String: Any] = ["unarchived_count": restored?.count ?? 0, "not_found": notFound]
        if let restored { result["restored_ids"] = restored }
        if let failed { result["failed_ids"] = failed }
        return ["result": result]
    }

    // MARK: список

    func test_listDecoder_itemsDedupedByID_andDates() {
        let response: [String: Any] = ["result": ["items": [
            ["id": "a1", "text": "один", "archived_at": "2026-11-20T10:00:00+00:00"],
            ["id": "a1", "text": "дубль"],
            ["id": "", "text": "без id"],
            ["id": "b2", "text": "два", "ts": "2026-11-19T09:00:00"]
        ], "total": 4]]
        guard case .items(let entries) = ArchiveListDecoder.decode(response) else { return XCTFail("ожидали items") }
        XCTAssertEqual(entries.map { $0.id }, ["a1", "b2"])
        XCTAssertNotEqual(entries[0].date, .distantPast)
        XCTAssertNotEqual(entries[1].date, .distantPast)
    }

    func test_listDecoder_privacyAndUnavailable() {
        XCTAssertEqual(ArchiveListDecoder.decode(["result": ["items": [], "total": 0, "reason": "privacy_mode_active"]]),
                       .privacyBlocked)
        XCTAssertEqual(ArchiveListDecoder.decode(nil), .unavailable)
        XCTAssertEqual(ArchiveListDecoder.decode(["result": ["ok": false, "reason": "x"]]), .unavailable)
        XCTAssertEqual(ArchiveListDecoder.decode(["result": ["total": 0]]), .unavailable, "нет items — не «пусто»")
    }

    // MARK: итог восстановления (смешанная партия)

    func test_restoreDecoder_mixedBatchByID() {
        let response = restoreResponse(restored: ["a1", "a3"], failed: ["a2"], notFound: ["ghost"])
        let outcome = ArchiveRestoreDecoder.decode(response, requested: ["a1", "a2", "a3", "ghost"])
        XCTAssertEqual(outcome, .partial(restored: ["a1", "a3"], failed: ["a2"], notFound: ["ghost"], unaccounted: []))
    }

    func test_restoreDecoder_completeOnlyWhenEveryRequestedIDRestored() {
        XCTAssertEqual(ArchiveRestoreDecoder.decode(restoreResponse(restored: ["a1", "a2"], failed: []),
                                                    requested: ["a1", "a2"]),
                       .complete(restored: ["a1", "a2"]))
        // Ответ молчит про a2 — это не успех.
        XCTAssertEqual(ArchiveRestoreDecoder.decode(restoreResponse(restored: ["a1"], failed: []), requested: ["a1", "a2"]),
                       .partial(restored: ["a1"], failed: [], notFound: [], unaccounted: ["a2"]))
    }

    func test_restoreDecoder_oldOrMissingResponseIsNeverSuccess() {
        // Старый формат: только счётчик, без restored_ids/failed_ids.
        XCTAssertEqual(ArchiveRestoreDecoder.decode(["result": ["unarchived_count": 1, "not_found": []]], requested: ["a1"]),
                       .unknown)
        XCTAssertEqual(ArchiveRestoreDecoder.decode(nil, requested: ["a1"]), .unknown)
        XCTAssertEqual(ArchiveRestoreDecoder.decode(["result": ["restored_ids": ["a1"]]], requested: ["a1"]), .unknown)
        XCTAssertEqual(ArchiveRestoreDecoder.decode(["result": ["ok": false, "reason": "privacy_mode_enabled"]], requested: ["a1"]),
                       .rejected(reason: "privacy_mode_enabled"))
    }

    func test_restoreDecoder_foreignIDsInResponseAreIgnored() {
        let outcome = ArchiveRestoreDecoder.decode(restoreResponse(restored: ["zzz"], failed: []), requested: ["a1"])
        XCTAssertEqual(outcome, .partial(restored: [], failed: [], notFound: [], unaccounted: ["a1"]))
    }

    // MARK: реестр предупреждений

    func test_ledger_failedWarningSurvivesRefreshByID_andClosesOnlyAfterReconcile() {
        var ledger = ArchiveWarningLedger()
        ledger.record(.partial(restored: ["a1"], failed: ["a2"], notFound: [], unaccounted: []))
        XCTAssertFalse(ledger.isClear)
        // Список обновился и порядок поменялся: a2 по-прежнему в архиве → предупреждение остаётся (по ID).
        ledger.reconcile(archiveIDs: ["a3", "a2"])
        XCTAssertEqual(ledger.unresolved, ["a2"])
        // a2 пропала из архива (восстановлена повторно) → предупреждение закрыто сверкой.
        ledger.reconcile(archiveIDs: ["a3"])
        XCTAssertTrue(ledger.isClear)
    }

    func test_ledger_claimedRestoredButStillArchived_isADiscrepancyWarning() {
        var ledger = ArchiveWarningLedger()
        ledger.record(.complete(restored: ["a1"]))
        XCTAssertTrue(ledger.isClear, "до сверки полного итога достаточно, предупреждений нет")
        ledger.reconcile(archiveIDs: ["a1", "a9"])
        XCTAssertEqual(ledger.unresolved, ["a1"], "backend сказал «восстановлено», а запись в архиве — расхождение")
        var clean = ArchiveWarningLedger()
        clean.record(.complete(restored: ["a1"]))
        clean.reconcile(archiveIDs: ["a9"])
        XCTAssertTrue(clean.isClear)
    }

    func test_ledger_unknownAndRejectedDoNotClaimAnything() {
        var ledger = ArchiveWarningLedger()
        ledger.record(.unknown)
        ledger.record(.rejected(reason: "x"))
        ledger.reconcile(archiveIDs: ["a1"])
        XCTAssertTrue(ledger.isClear)
    }

    // MARK: контроллер

    private func makeVC() -> ArchiveTabViewController {
        let vc = ArchiveTabViewController()
        _ = vc.view
        return vc
    }

    private func findTable(_ view: NSView) -> NSTableView? {
        if let table = view as? NSTableView { return table }
        for sub in view.subviews { if let found = findTable(sub) { return found } }
        return nil
    }

    private func entry(_ id: String) -> ArchivedEntry {
        ArchivedEntry(id: id, text: "запись \(id)", date: Date(timeIntervalSince1970: 1_700_000_000), sizeBytes: 10)
    }

    func test_restoreButtonIsBoundToIDNotRow() throws {
        let vc = makeVC()
        let table = try XCTUnwrap(findTable(vc.view))
        vc.archivedItems = [entry("a1"), entry("a2")]
        func buttonID(row: Int) -> String? {
            let cell = vc.tableView(table, viewFor: nil, row: row)
            let stack = cell?.subviews.first as? NSStackView
            return (stack?.arrangedSubviews.last as? NSButton)?.identifier?.rawValue
        }
        XCTAssertEqual(buttonID(row: 0), "a1")
        vc.archivedItems = [entry("a2"), entry("a1")]  // порядок сменился после обновления
        XCTAssertEqual(buttonID(row: 0), "a2")
        XCTAssertEqual(buttonID(row: 1), "a1")
    }

    func test_unavailableListKeepsItemsAndWarns_notDrawnAsEmpty() {
        let vc = makeVC()
        vc.apply(listOutcome: .items([entry("a1")]))
        vc.apply(listOutcome: .unavailable)
        XCTAssertEqual(vc.archivedItems.map { $0.id }, ["a1"])
        XCTAssertNotNil(vc.noticeTextForTesting)
    }

    func test_partialRestoreWarningStaysUntilListReconciles() {
        let vc = makeVC()
        vc.apply(listOutcome: .items([entry("a1"), entry("a2")]))
        vc.apply(restoreOutcome: .partial(restored: ["a1"], failed: ["a2"], notFound: [], unaccounted: []))
        XCTAssertNotNil(vc.noticeTextForTesting)
        vc.apply(listOutcome: .items([entry("a2")]))  // a2 всё ещё в архиве
        XCTAssertNotNil(vc.noticeTextForTesting)
        vc.apply(listOutcome: .items([]))  // a2 исчезла
        XCTAssertNil(vc.noticeTextForTesting)
    }

    func test_unknownRestoreResponseShowsNoSuccessText() {
        let vc = makeVC()
        vc.apply(restoreOutcome: .unknown)
        let text = vc.noticeTextForTesting ?? ""
        XCTAssertFalse(text.contains("Восстановлено записей"))
        XCTAssertFalse(text.isEmpty)
    }

    func test_statsBarHasNoBulletGlyph() {
        let vc = makeVC()
        vc.apply(listOutcome: .items([entry("a1")]))
        let source = (try? String(contentsOfFile: #filePath.replacingOccurrences(
            of: "Tests/KrabEarAgentTests/ArchiveTabModelTests.swift",
            with: "Sources/KrabEarAgent/ArchiveTabViewController.swift"), encoding: .utf8)) ?? ""
        XCTAssertFalse(source.isEmpty)
        XCTAssertFalse(source.contains("\u{2022}"), "U+2022 вешает CoreText (Wave 621/658)")
    }
}
