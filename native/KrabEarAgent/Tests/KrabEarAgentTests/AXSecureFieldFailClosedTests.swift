/*
 Ревью ChatGPT (2026-11-16): ошибка чтения AXSubrole не равна «поле не защищено».
 Нет атрибута (.noValue/.attributeUnsupported) = обычное поле; любая другая ошибка или не-строка = fail-closed.
*/

import XCTest
import ApplicationServices
@testable import KrabEarAgent

final class AXSecureFieldFailClosedTests: XCTestCase {
    func test_absentAttribute_isOrdinaryField() {
        XCTAssertNil(AXSecureField.interpretSubrole(error: .noValue, value: nil))
        XCTAssertNil(AXSecureField.interpretSubrole(error: .attributeUnsupported, value: nil))
    }

    func test_successfulLookupReturnsTheSubrole() {
        XCTAssertEqual(AXSecureField.interpretSubrole(error: .success, value: "AXSearchField"), "AXSearchField")
        XCTAssertEqual(AXSecureField.interpretSubrole(error: .success, value: "AXSecureTextField"), AXSecureField.marker)
    }

    func test_lookupFailureOrMalformedValue_failsClosed() {
        for error in [AXError.cannotComplete, .failure, .invalidUIElement, .notImplemented] {
            XCTAssertEqual(AXSecureField.interpretSubrole(error: error, value: nil), AXSecureField.marker, "\(error)")
        }
        XCTAssertEqual(AXSecureField.interpretSubrole(error: .success, value: 42), AXSecureField.marker)
        XCTAssertEqual(AXSecureField.interpretSubrole(error: .success, value: nil), AXSecureField.marker)
    }
}
