/*
 ConversationViewController+LanguageHint — сохранение языка разговора.

 Voice Gateway озвучивает ответ с языком, который передал клиент (`lang`); без него TTS идёт с `auto` и
 реплика на латинице (имена, числа, английские слова) звучит чужим голосом с акцентом (живой случай 24.11).
 Поэтому язык не остаётся «auto» по умолчанию: берём сохранённый выбор, а без него RU (основной язык владельца).
*/

import Foundation

private let kLanguageHintUserDefaultsKey = "KrabEar_ConversationLanguageHint"

extension ConversationViewController {

    /// Значения селектора «Язык подсказки» в порядке индекса (Авто, RU, EN, ES).
    nonisolated static let languageHintValues = ["auto", "ru", "en", "es"]

    /// Последний сохранённый выбор; «ru», если ключа нет или значение неизвестно.
    nonisolated static var savedLanguageHint: String {
        savedLanguageHint(in: .standard)
    }

    nonisolated static func savedLanguageHint(in defaults: UserDefaults) -> String {
        let raw = defaults.string(forKey: kLanguageHintUserDefaultsKey) ?? "ru"
        return languageHintValues.contains(raw) ? raw : "ru"
    }

    nonisolated static func saveLanguageHint(_ value: String, in defaults: UserDefaults = .standard) {
        defaults.set(value, forKey: kLanguageHintUserDefaultsKey)
    }
}
