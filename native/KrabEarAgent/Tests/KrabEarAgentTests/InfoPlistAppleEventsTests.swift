/*
 InfoPlistAppleEventsTests — путь вставки через System Events (PasteService) требует
 NSAppleEventsUsageDescription, иначе системный запрос Automation идёт без объяснения
 (ревью E4, п.9).
*/

import XCTest

final class InfoPlistAppleEventsTests: XCTestCase {
    func test_infoPlist_declaresAppleEventsUsage() throws {
        var url = URL(fileURLWithPath: #file)
        for _ in 0..<8 where !FileManager.default.fileExists(
            atPath: url.appendingPathComponent("Krab Ear.app/Contents/Info.plist").path) {
            url.deleteLastPathComponent()
        }
        let plistURL = url.appendingPathComponent("Krab Ear.app/Contents/Info.plist")
        let data = try Data(contentsOf: plistURL)
        let object = try PropertyListSerialization.propertyList(from: data, format: nil)
        let dict = try XCTUnwrap(object as? [String: Any])
        let text = try XCTUnwrap(dict["NSAppleEventsUsageDescription"] as? String)
        XCTAssertFalse(text.trimmingCharacters(in: .whitespaces).isEmpty)
    }
}
