import Foundation

/// Голосовое завершение разговора. Живая проверка владельца 26.11: на «отключись» ассистент отвечал «хорошо»,
/// но разговор продолжался и слушал в фоне — у шлюза нет команды «закрыть сессию», а модель только вежливо
/// соглашалась. Клиент сам узнаёт просьбу закончить по финальной расшифровке и закрывает сессию.
enum ConversationVoiceEndSettings {
    static let enabledKey = "KrabEar_ConversationVoiceEndEnabled"

    /// Отсутствующий ключ — включено; явный false — выключено.
    static func isEnabled(in defaults: UserDefaults) -> Bool {
        defaults.object(forKey: enabledKey) == nil ? true : defaults.bool(forKey: enabledKey)
    }
}

enum ConversationVoiceEndPhrases {
    /// Длинные реплики не разбираем: просьба закончить короткая, а в длинном вопросе слово «пока» ничего не значит.
    static let maxWords = 8

    /// Фразы, которые закрывают разговор, если встретились целыми словами в короткой реплике.
    private static let containedPhrases: [String] = [
        "отключись", "отключайся", "выключись", "выключайся",
        "заверши разговор", "завершить разговор", "завершай разговор", "завершим разговор",
        "закончи разговор", "закончить разговор", "закончим разговор",
        "закрой разговор", "прекрати разговор", "останови разговор",
        "заверши диалог", "закончи диалог", "закрой диалог",
        "до свидания", "конец связи", "хватит разговаривать"
    ]

    /// Реплика целиком — одно из этих слов («пока» внутри фразы не считается).
    private static let exactPhrases: Set<String> = [
        "пока", "пока пока", "хватит", "все хватит", "на этом все", "на сегодня все", "все пока", "ну пока"
    ]

    /// Строчные буквы без «ё» и знаков препинания, одиночные пробелы.
    static func normalize(_ text: String) -> String {
        let lowered = text.lowercased().replacingOccurrences(of: "ё", with: "е")
        let cleaned = lowered.unicodeScalars.map { CharacterSet.alphanumerics.contains($0) ? Character($0) : " " }
        return String(cleaned).split(separator: " ").joined(separator: " ")
    }

    /// Просит ли пользователь закончить разговор (по финальной расшифровке одной реплики).
    static func isEndRequest(_ text: String) -> Bool {
        let normalized = normalize(text)
        guard !normalized.isEmpty else { return false }
        let wordCount = normalized.split(separator: " ").count
        guard wordCount <= maxWords else { return false }
        if exactPhrases.contains(normalized) { return true }
        let padded = " \(normalized) "
        return containedPhrases.contains { padded.contains(" \($0) ") }
    }
}
