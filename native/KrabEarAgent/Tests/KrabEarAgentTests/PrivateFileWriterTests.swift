/*
 PrivateFileWriterTests — экспорты расшифровок пишутся с правами 0600 (ревью E4, п.9).

 Раньше `content.write(to:atomically:)` создавал файл с правами по umask (0644):
 расшифровка встречи читалась любым локальным пользователем. Теперь запись идёт через
 временный файл, созданный сразу с 0600 (без окна «сначала 0644, потом chmod»), и
 атомарный rename.
*/

import XCTest
@testable import KrabEarAgent

final class PrivateFileWriterTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("PrivateFileWriter-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: dir)
    }

    private func mode(_ url: URL) throws -> Int {
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        return (attrs[.posixPermissions] as? NSNumber)?.intValue ?? -1
    }

    func test_newFile_hasMode0600AndContent() throws {
        let url = dir.appendingPathComponent("a.md")
        try PrivateFileWriter.write("привет", to: url)
        XCTAssertEqual(try mode(url), 0o600)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "привет")
    }

    func test_overwrite_replacesContentAndTightensMode() throws {
        let url = dir.appendingPathComponent("b.md")
        try "old".write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: url.path)
        try PrivateFileWriter.write("new", to: url)
        XCTAssertEqual(try mode(url), 0o600)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "new")
    }

    func test_dataVariant_hasMode0600() throws {
        let url = dir.appendingPathComponent("c.pdf")
        try PrivateFileWriter.write(Data([1, 2, 3]), to: url)
        XCTAssertEqual(try mode(url), 0o600)
        XCTAssertEqual(try Data(contentsOf: url), Data([1, 2, 3]))
    }

    func test_noTemporaryFilesLeftBehind() throws {
        try PrivateFileWriter.write("x", to: dir.appendingPathComponent("d.md"))
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path)
        XCTAssertEqual(names, ["d.md"])
    }

    func test_missingDirectory_throwsAndLeavesNothing() {
        let url = dir.appendingPathComponent("no/such/dir/e.md")
        XCTAssertThrowsError(try PrivateFileWriter.write("x", to: url))
    }

    func test_exportSites_useThePrivateWriter() throws {
        var root = URL(fileURLWithPath: #file)
        for _ in 0..<6 where !FileManager.default.fileExists(
            atPath: root.appendingPathComponent("Sources/KrabEarAgent").path) {
            root.deleteLastPathComponent()
        }
        let src = root.appendingPathComponent("Sources/KrabEarAgent")
        for name in ["HistoryPanelController+History.swift", "HistoryPanelController+MeetingMode.swift",
                     "HistoryPanelController+CallAssist.swift",
                     "AnalyticsDashboardViewController+PDFExport.swift",
                     "HistoryPanelController+Import.swift"] {
            let text = try String(contentsOf: src.appendingPathComponent(name), encoding: .utf8)
            XCTAssertTrue(text.contains("PrivateFileWriter.write"), "\(name): экспорт не через PrivateFileWriter")
            XCTAssertFalse(text.contains(".write(to: url, atomically: true")
                           || text.contains(".write(to: url, options: .atomic")
                           || text.contains(".write(toFile: reportPath, atomically"),
                           "\(name): остался запись с правами по умолчанию")
        }
    }
}
