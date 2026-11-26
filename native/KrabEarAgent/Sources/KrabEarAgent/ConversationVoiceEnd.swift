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
        "заверши разговор", "завершай разговор", "завершим разговор",
        "закончи разговор", "закончим разговор", "заканчивай разговор",
        "закрой разговор", "прекрати разговор", "останови разговор",
        "заверши диалог", "закончи диалог", "закрой диалог",
        "конец связи", "хватит разговаривать",
        // Испанский и английский (проект RU/ES/EN): те же просьбы закончить.
        "desconectate", "desconéctate", "termina la conversacion", "termina la conversación",
        "cierra la conversacion", "cierra la conversación", "fin de la conversacion", "fin de la conversación"
    ]

    /// Переходные глаголы («disconnect my headphones», «stop listening to that song», «end the call at five», «end the conversation with my boss», «I need to end the conversation»): закрывают
    /// разговор, только если стоят в самом конце короткой реплики (не больше `maxLeadingWords` слов перед ними).
    private static let tailAnchoredPhrases: [String] = [
        "disconnect", "stop listening", "end the call", "end the conversation", "end conversation"
    ]
    private static let maxLeadingWords = 2

    /// Прощание закрывает разговор, только если стоит на краю реплики («ну всё, до свидания»). Слова вопроса и
    /// перевода/цитаты в реплике отключают разбор целиком: «как по-испански до свидания», «переведи до свидания на английский» — это просьба, а не конец.
    private static let edgeAnchoredPhrases: [String] = [
        "до свидания", "adios", "adiós", "hasta luego", "hasta pronto", "goodbye", "good bye", "bye bye"
    ]
    private static let quotingWords: Set<String> = [
        "как", "переведи", "перевести", "перевод", "переводится", "скажи", "означает", "значит", "по", "слово", "фраза",
        "что", "почему", "зачем", "можно", "ли", "нужно", "надо",
        // ES/EN: вопрос, перевод, цитата.
        "como", "cómo", "que", "qué", "traduce", "traducir", "significa", "di", "decir", "cuando", "cuándo",
        "how", "translate", "say", "means", "mean", "what", "why", "does", "can", "when", "word", "phrase",
        "should", "could", "would", "will", "shall", "explain", "tell", "do", "did", "is", "are", "if", "whether",
        "podria", "podría", "puedes", "puedo", "deberia", "debería", "explica", "si"
    ]

    /// Слово перед фразой переворачивает смысл: «не отключайся», «нельзя выключаться», «ни в коем случае не…».
    private static let negations: Set<String> = ["не", "нельзя", "ни", "нет", "no", "not", "never", "nunca", "dont", "don", "t"]  // «t» — хвост «don't» после снятия апострофа

    /// Английское/испанское отрицание действует на всю реплику до фразы, а не только на соседнее слово:
    /// «I don't want to end the conversation», «don't ever disconnect» (Codex P2 #2281). Русское — только соседнее.
    private static let wideNegations: Set<String> = ["no", "not", "never", "nunca", "dont", "don", "t"]

    /// Вежливый хвост после хвостовой фразы: «disconnect please», «end the call now», «desconéctate por favor».
    private static let politeTail: Set<String> = ["please", "now", "thanks", "por", "favor", "ahora"]

    /// Реплика целиком — одно из этих слов («пока» внутри фразы не считается).
    private static let exactPhrases: Set<String> = [
        "пока", "пока пока", "хватит", "все хватит", "на этом все", "на сегодня все", "все пока", "ну пока",
        "bye", "see you", "see you later", "see you soon", "hasta la vista", "chao", "chau", "that s all", "that is all", "eso es todo", "nada mas", "nada más"
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
        // Вопрос или цитата («как завершить разговор с банком», «что значит отключись») — обсуждение, а не команда.
        guard quotingWords.isDisjoint(with: words) else { return false }
        if containedPhrases.contains(where: { containsPhrase($0, in: words, anchored: false) }) { return true }
        if tailAnchoredPhrases.contains(where: { containsTail($0, in: words) }) { return true }
        return edgeAnchoredPhrases.contains { containsPhrase($0, in: words, anchored: true) }
    }

    /// Перед фразой, начинающейся с `start`, стоит отрицание: соседнее слово (любой язык) или en/es-отрицание где угодно раньше.
    private static func isNegated(_ words: [String], before start: Int) -> Bool {
        guard start > 0 else { return false }
        if negations.contains(words[start - 1]) { return true }
        return words[..<start].contains { wideNegations.contains($0) }
    }

    /// Фраза в самом конце реплики, перед ней не больше `maxLeadingWords` слов и нет отрицания.
    private static func containsTail(_ phrase: String, in words: [String]) -> Bool {
        let target = phrase.split(separator: " ").map(String.init)
        guard !target.isEmpty, words.count >= target.count else { return false }
        var words = words
        while let last = words.last, politeTail.contains(last), words.count > target.count { words.removeLast() }
        let start = words.count - target.count
        guard Array(words[start...]) == target, start <= maxLeadingWords else { return false }
        return !isNegated(words, before: start)
    }

    /// Фраза целыми словами; не засчитывается, если перед ней отрицание. `anchored` — только в начале или в конце реплики.
    private static func containsPhrase(_ phrase: String, in words: [String], anchored: Bool) -> Bool {
        let target = phrase.split(separator: " ").map(String.init)
        guard !target.isEmpty, words.count >= target.count else { return false }
        for start in 0...(words.count - target.count) where Array(words[start..<(start + target.count)]) == target {
            if anchored && start != 0 && start + target.count != words.count { continue }
            if isNegated(words, before: start) { continue }
            return true
        }
        return false
    }
}
