/*
 IsolatedUserDefaultsDomain — одноразовый домен настроек для Swift unit-тестов.

 Каждый экземпляр создаёт собственный UUID-suite и явно очищает persistent domain.
 Это не даёт тестам читать или перезаписывать живые настройки Krab Ear и устраняет
 зависимость результатов от состояния запущенного приложения.
*/

import Foundation

/// Владеет уникальным `UserDefaults`-suite и его детерминированной очисткой.
final class IsolatedUserDefaultsDomain {
    let suiteName: String
    let defaults: UserDefaults

    init(scope: String) {
        suiteName = "KrabEarTests.\(scope).\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            preconditionFailure("Не удалось создать изолированный UserDefaults-suite")
        }
        self.defaults = defaults
        defaults.removePersistentDomain(forName: suiteName)
    }

    /// Удаляет весь тестовый домен; вызывается из `tearDown`, а не полагается на deinit.
    func removePersistentDomain() {
        defaults.removePersistentDomain(forName: suiteName)
    }
}
