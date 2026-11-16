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

    /// Читает подроль элемента. Отсутствие атрибута = `nil`; ошибка чтения или не-строка = fail-closed
    /// (возвращается маркер защищённого поля, чтобы текст не извлекался и не вставлялся вслепую).
    static func subrole(of element: AXUIElement) -> String? {
        var ref: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, kAXSubroleAttribute as CFString, &ref)
        return interpretSubrole(error: error, value: ref)
    }

    /// Чистая часть чтения подроли (тестируется без живого AX).
    static func interpretSubrole(error: AXError, value: Any?) -> String? {
        switch error {
        case .success:
            guard let text = value as? String else { return marker }
            return text
        case .noValue, .attributeUnsupported:
            return nil
        default:
            return marker
        }
    }
}
