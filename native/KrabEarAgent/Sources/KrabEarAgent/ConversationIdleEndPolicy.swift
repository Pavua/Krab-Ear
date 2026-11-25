import Foundation

/*
 Автозавершение «Разговора с AI» по тишине.

 Живая проверка владельца: после «Прервать» окно оставалось, микрофон открыт, и ассистент
 «просыпался» от шума и сам заговорил. Теперь, если N секунд нет речи пользователя И AI не
 говорит, разговор завершается (`.end`, оверлей закрыт, пауза wake word снята — это делает
 обычный stopConversation()).

 Чистая логика без таймеров и AppKit — проверяется headless, как ConversationHotkeyPolicy.
 Отсчёт идёт только в состоянии «слушаю»: пока AI думает или говорит, а также пока движок
 грузится (холодный старт бывает долгим), разговор не завершается.
*/

/// Настройка: тумблер и порог в UserDefaults (агентская, как KrabEar_ConversationEngine).
enum ConversationIdleEndSettings {
    static let enabledKey = "KrabEar_ConversationIdleEndEnabled"
    static let secondsKey = "KrabEar_ConversationIdleEndSec"
    static let defaultSeconds: TimeInterval = 30
    static let range: ClosedRange<TimeInterval> = 10...300
    /// Варианты выбора в панели (секунды) — внутри `range`.
    static let choices: [TimeInterval] = [10, 20, 30, 60, 120, 300]

    /// Отсутствующий ключ — включено (защита по умолчанию); явный false — выключено.
    static func isEnabled(in defaults: UserDefaults) -> Bool {
        defaults.object(forKey: enabledKey) == nil ? true : defaults.bool(forKey: enabledKey)
    }

    /// Порог в пределах 10–300 с; мусор и отсутствие — 30 с.
    static func seconds(in defaults: UserDefaults) -> TimeInterval {
        let raw = defaults.double(forKey: secondsKey)
        guard raw.isFinite, raw > 0 else { return defaultSeconds }
        return min(max(raw, range.lowerBound), range.upperBound)
    }

    static func choiceTitle(_ seconds: TimeInterval) -> String {
        seconds < 60 ? "\(Int(seconds)) с" : "\(Int(seconds / 60)) мин"
    }
}

/// Решение «пора ли завершать по тишине».
struct ConversationIdleEndPolicy {
    let timeout: TimeInterval
    private(set) var lastActivity: TimeInterval

    init(timeout: TimeInterval, startedAt now: TimeInterval) {
        self.timeout = timeout
        self.lastActivity = now
    }

    /// Речь пользователя (VAD, частичная расшифровка), реплика или звук AI, смена состояния.
    mutating func noteActivity(at now: TimeInterval) {
        lastActivity = max(lastActivity, now)
    }

    /// true — завершать. Только в «слушаю»: AI не говорит и не думает, движок готов.
    /// Вне «слушаю» отсчёт удерживается: вернувшись в «слушаю», считаем заново.
    mutating func shouldEnd(now: TimeInterval, state: ConversationState) -> Bool {
        guard state == .listening else {
            lastActivity = max(lastActivity, now)
            return false
        }
        return now - lastActivity >= timeout
    }
}
