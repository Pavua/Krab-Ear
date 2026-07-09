/*
 ConversationErrorAnnouncer — локальная озвучка ошибок «Разговора с AI» (Волна 3c).

 Принцип (спека §4): голосовой интерфейс должен голосом сообщать о сбое —
 молчание выглядит как зависание. Работает НЕЗАВИСИМО от VG-соединения:
 синтез — локальный IPC synthesize_speech (инжектится из +VoiceTab как
 speak-клоужер), воспроизведение — отдельный AVAudioPlayer (НЕ conversation-плеер,
 должен жить и после stopConversation()).

 Деградация: privacy mode / backend недоступен / пустой синтез → speak-клоужер
 молча ничего не проигрывает — остаётся текущий текст в transcript. Без ретраев.

 Дебаунс: не чаще 1 озвучки на класс ошибки за 30с (реконнект-циклы не спамят).
 Фразы НЕ содержат слово «Краб» — чтобы не триггерить wake word (поллер к этому
 моменту уже может быть возобновлён).
*/

import AVFoundation
import Foundation

@MainActor
final class ConversationErrorAnnouncer {

    /// Класс ошибки — свой дебаунс-слот на каждый.
    enum ErrorClass: String, CaseIterable {
        /// VG недоступен при старте сессии (WS connect fail в состоянии .connecting).
        case gatewayUnreachable = "gateway_unreachable"
        /// Обрыв WS посреди активной сессии.
        case connectionLost = "connection_lost"
        /// conv.error / conv.fatal от сервера.
        case serverError = "server_error"
    }

    static let debounceInterval: TimeInterval = 30

    /// Фразы фиксированы спекой (§4.1). Без слова «Краб».
    static let phrases: [ErrorClass: String] = [
        .gatewayUnreachable: "Голосовой шлюз недоступен.",
        .connectionLost:     "Связь с голосовым шлюзом потеряна.",
        .serverError:        "Произошла ошибка. Попробуй ещё раз.",
    ]

    /// Реальная озвучка фразы (синтез + воспроизведение). Инжектится из
    /// HistoryPanelController+VoiceTab; в тестах — спай. Вызывается уже ПОСЛЕ
    /// дебаунс-гейта, на main actor. nil → тихая деградация (текст-only).
    var speak: ((String) -> Void)?

    /// Источник времени (инжектируется в тестах для детерминированного дебаунса).
    var now: () -> Date = { Date() }

    private var lastAnnounced: [ErrorClass: Date] = [:]

    /// Озвучить ошибку класса cls с дебаунсом. true = фраза ушла в speak.
    @discardableResult
    func announce(_ cls: ErrorClass) -> Bool {
        if let last = lastAnnounced[cls],
           now().timeIntervalSince(last) < Self.debounceInterval {
            return false
        }
        lastAnnounced[cls] = now()
        guard let phrase = Self.phrases[cls], let speak else { return false }
        speak(phrase)
        return true
    }

    // MARK: - WAV playback (используется реальным speak-клоужером из +VoiceTab)

    /// Держим плеер живым до конца воспроизведения (AVAudioPlayer останавливается
    /// при деаллокации). Одна ошибка перекрывает предыдущую — это ок для коротких фраз.
    static var activePlayer: AVAudioPlayer?

    /// Проиграть WAV-данные (ответ synthesize_speech). Никогда не бросает.
    static func playWav(_ data: Data) {
        guard let player = try? AVAudioPlayer(data: data) else {
            AgentLogger.shared.info("[ErrorAnnouncer] AVAudioPlayer не открыл WAV (\(data.count) bytes)")
            return
        }
        activePlayer = player
        player.play()
    }
}
