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
        "конец связи", "хватит разговаривать"
    ]

    /// Прощание закрывает разговор, только если стоит на краю реплики («ну всё, до свидания»), и перед ним нет слов
    /// перевода/цитаты: «как по-испански до свидания», «переведи до свидания на английский» — это просьба, а не конец.
    private static let edgeAnchoredPhrases: [String] = ["до свидания"]
    private static let quotingWords: Set<String> = [
        "как", "переведи", "перевести", "перевод", "переводится", "скажи", "означает", "значит", "по", "слово", "фраза"
    ]

    /// Слово перед фразой переворачивает смысл: «не отключайся», «нельзя выключаться», «ни в коем случае не…».
    private static let negations: Set<String> = ["не", "нельзя", "ни", "нет"]

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
        let words = normalized.split(separator: " ").map(String.init)
        if containedPhrases.contains(where: { containsPhrase($0, in: words, anchored: false) }) { return true }
        let hasQuotingWord = !quotingWords.isDisjoint(with: words)
        return !hasQuotingWord && edgeAnchoredPhrases.contains { containsPhrase($0, in: words, anchored: true) }
    }

    /// Фраза целыми словами; не засчитывается, если перед ней отрицание. `anchored` — только в начале или в конце реплики.
    private static func containsPhrase(_ phrase: String, in words: [String], anchored: Bool) -> Bool {
        let target = phrase.split(separator: " ").map(String.init)
        guard !target.isEmpty, words.count >= target.count else { return false }
        for start in 0...(words.count - target.count) where Array(words[start..<(start + target.count)]) == target {
            if anchored && start != 0 && start + target.count != words.count { continue }
            if start > 0 && negations.contains(words[start - 1]) { continue }
            return true
        }
        return false
    }
}
