/*
 AXSecureFieldTests — E4 п.3: поле пароля определяется по AXSubrole, а не по AXRole.

 В macOS у защищённого поля роль `AXTextField`, а `AXSecureTextField` — это ПОДРОЛЬ.
 Сравнение роли со строкой `AXSecureTextField` никогда не срабатывало, поэтому вставка
 текста и перевод выделения шли в поля паролей.
*/

import XCTest
@testable import KrabEarAgent

final class AXSecureFieldTests: XCTestCase {
    func test_secureSubroleOnTextFieldRole_isSecure() {
        XCTAssertTrue(AXSecureField.isSecure(role: "AXTextField", subrole: "AXSecureTextField"))
    }

    func test_legacyRoleValue_isStillSecure() {
        XCTAssertTrue(AXSecureField.isSecure(role: "AXSecureTextField", subrole: nil))
    }

    func test_plainFields_areNotSecure() {
        XCTAssertFalse(AXSecureField.isSecure(role: "AXTextField", subrole: nil))
        XCTAssertFalse(AXSecureField.isSecure(role: "AXTextArea", subrole: "AXSearchField"))
        XCTAssertFalse(AXSecureField.isSecure(role: nil, subrole: nil))
    }

    func test_effectiveRole_foldsSecureSubroleIntoRole() {
        XCTAssertEqual(AXSecureField.effectiveRole(role: "AXTextField", subrole: "AXSecureTextField"), "AXSecureTextField")
        XCTAssertEqual(AXSecureField.effectiveRole(role: "AXTextField", subrole: "AXSearchField"), "AXTextField")
        XCTAssertEqual(AXSecureField.effectiveRole(role: nil, subrole: "AXSecureTextField"), "AXSecureTextField")
        XCTAssertNil(AXSecureField.effectiveRole(role: nil, subrole: nil))
    }
}
