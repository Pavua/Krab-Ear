/*
 ConversationViewController+LanguageHint — сохранение языка разговора.

 Что это даёт и чего НЕ даёт (сверено с кодом Voice Gateway, 24.11): `lang` идёт только в РАСПОЗНАВАНИЕ речи
 (`krab_ear_engine`: language=src_lang); без параметра шлюз берёт «ru». Синтез ответа язык не получает ни при
 каком `lang`, поэтому смена голоса/акцент на латинице чинится на стороне шлюза, а не этим выбором.
 Здесь: выбор запоминается между запусками, по умолчанию RU (основной язык владельца), «Авто» = RU у шлюза.
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
