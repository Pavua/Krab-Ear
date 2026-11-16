import ApplicationServices
import Foundation

/// Определение защищённого поля (пароль) через Accessibility.
///
/// В macOS у такого поля роль `AXTextField`, а `AXSecureTextField` — ПОДРОЛЬ
/// (`kAXSubroleAttribute`). Прежний код сравнивал со строкой именно роль, и защита
/// не срабатывала никогда. Старое значение роли оставлено как запасное.
enum AXSecureField {
    static let marker = "AXSecureTextField"

    static func isSecure(role: String?, subrole: String?) -> Bool {
        role == marker || subrole == marker
    }

    /// Роль с учётом подроли: для защищённого поля всегда `AXSecureTextField`.
    /// Так существующие потребители роли (`readRole`) получают корректный сигнал без смены контракта.
    static func effectiveRole(role: String?, subrole: String?) -> String? {
        isSecure(role: role, subrole: subrole) ? marker : role
    }

    /// Читает подроль элемента; `nil`, если атрибута нет или чтение не удалось.
    static func subrole(of element: AXUIElement) -> String? {
        var ref: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSubroleAttribute as CFString, &ref) == .success else { return nil }
        return ref as? String
    }
}
