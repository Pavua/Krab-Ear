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

/// Жёсткий потолок длительности одной сессии разговора.
///
/// Живая проверка владельца 25–26.11: разговор висел 4–5 часов и отвечал на ролики и шум.
/// Тишина тут не наступает никогда — ролик это «речь» и для локального VAD, и для Voice Gateway,
/// отсчёт `ConversationIdleEndPolicy` обнуляется вечно. Потолок считается только от старта
/// сессии: не зависит ни от активности, ни от состояния (в том числе застрявшего «говорю»), ни от
/// тумблера тишины. Дальше — снова «Краб»; микрофон не остаётся открытым без присмотра.
enum ConversationMaxDurationSettings {
    static let secondsKey = "KrabEar_ConversationMaxSec"
    static let defaultSeconds: TimeInterval = 600
    static let range: ClosedRange<TimeInterval> = 120...3_600

    /// Порог в пределах 2–60 минут; мусор, NaN и неположительное — 10 минут.
    static func seconds(in defaults: UserDefaults) -> TimeInterval {
        let raw = defaults.double(forKey: secondsKey)
        guard raw.isFinite, raw > 0 else { return defaultSeconds }
        return min(max(raw, range.lowerBound), range.upperBound)
    }

    static func title(_ seconds: TimeInterval) -> String {
        "\(Int((seconds / 60).rounded())) мин"
    }
}

struct ConversationMaxDurationPolicy {
    let limit: TimeInterval
    let startedAt: TimeInterval

    init(limit: TimeInterval, startedAt now: TimeInterval) {
        self.limit = limit
        self.startedAt = now
    }

    func isExpired(now: TimeInterval) -> Bool {
        now - startedAt >= limit
    }
}

/// Ответ AI доигран: пора вернуться из «говорю» в «слушаю».
///
/// Ревью Codex #2270: replyFinal и downlink ставят `.speaking`, а воспроизведение
/// (`scheduleBuffer(..., completionHandler: nil)`) назад не переключает — после обычного
/// ответа при молчащем пользователе состояние оставалось «говорю» навсегда, отсчёт тишины
/// удерживался, микрофон оставался открытым. Доиграно = очередь воспроизведения кончилась
/// (с хвостом эхо-защиты) И `settle` секунд не было новых сигналов речи AI — чтобы текст
/// ответа, пришедший раньше звука, не дёргал метку «говорю → слушаю → говорю».
enum ConversationPlaybackSettle {
    static let settleSeconds: TimeInterval = 2.5

    static func isSettled(state: ConversationState, playbackQueueEndsAt: Date, echoTail: TimeInterval,
                          lastAISpeechAt: Date, now: Date) -> Bool {
        state == .speaking
            && now >= playbackQueueEndsAt.addingTimeInterval(echoTail)
            && now.timeIntervalSince(lastAISpeechAt) >= settleSeconds
    }
}

/// Локальный признак «пользователь говорит» по уровню микрофона разговора.
///
/// Гейт Opus #2270: Voice Gateway не шлёт conv.vad_speech и частичных расшифровок —
/// только финальную после реплики. Без локального признака разговор при паузе на подумать
/// и монологе дольше порога обрывался бы посреди фразы. Порог по RMS + не чаще раза в
/// `minInterval`, чтобы не дёргать главный актор на каждом чанке. Эхо собственного TTS
/// опасности не несёт: оно лишь откладывает завершение, а пока AI говорит, отсчёт и так стоит.
struct LocalVoiceActivityGate {
    /// ≈ −34 dBFS: голос у микрофона заметно громче фонового шума комнаты.
    static let rmsThreshold: Float = 0.02
    static let minInterval: TimeInterval = 0.5
    private var lastVoiceAt: TimeInterval = -.infinity

    /// true — в этом чанке голос (и с прошлого признака прошло ≥ minInterval).
    mutating func isVoice(_ samples: [Float], now: TimeInterval) -> Bool {
        guard !samples.isEmpty, now - lastVoiceAt >= Self.minInterval else { return false }
        var sumSq: Float = 0
        for s in samples { sumSq += s * s }
        guard (sumSq / Float(samples.count)).squareRoot() >= Self.rmsThreshold else { return false }
        lastVoiceAt = now
        return true
    }
}
