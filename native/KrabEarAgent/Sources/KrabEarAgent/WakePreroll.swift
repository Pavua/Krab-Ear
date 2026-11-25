import Foundation

/*
 Pre-roll слова-пробуждения: звук ДО срабатывания «Краб», чтобы первые слова фразы
 («Краб, какая погода…») не терялись, пока стартуют WebSocket и захват разговора
 (живая проверка владельца: задержка до записи ≈1,3–1,7 с).

 Поток: поллер увидел детекцию → `WakePrerollMailbox.arm` и сразу IPC
 `wake_word_take_preroll` в ту же серийную очередь, ДО `wake_word_stop` →
 ответ декодируется (`WakePrerollDecoder`) и кладётся в ящик → разговор, запущенный
 этой детекцией, забирает взвод (`claim`) и по `conv.ready` ставит сэмплы В НАЧАЛО
 prebuffer (`take`). Разговор по горячей клавише ящик сбрасывает; pre-roll, пришедший
 после `ready`, выбрасывается — дописать его позже значило бы перепутать порядок звука.
 Старт разговора pre-roll не ждёт никогда.
*/

/// Разбор ответа `wake_word_take_preroll` (контракт backend, ветка claude/ear-wake-preroll):
/// `{ok, available, reason?, sample_rate: 16000, format: "pcm_s16le_mono_b64", samples, pcm_b64, age_ms}`.
enum WakePrerollDecoder {
    static let sampleRate = 16_000
    static let format = "pcm_s16le_mono_b64"
    /// Backend обещает ≤ 6 с; защитный предел на случай испорченного ответа.
    static let maxSamples = 8 * sampleRate

    /// Float-сэмплы [-1, 1] или nil (нет звука, чужой формат, мусор). Ошибка — молча без pre-roll.
    static func decode(_ response: [String: Any]?) -> [Float]? {
        guard let result = response?["result"] as? [String: Any],
              (result["ok"] as? Bool) == true,
              (result["available"] as? Bool) == true,
              (result["sample_rate"] as? Int) == sampleRate,
              (result["format"] as? String) == format,
              let b64 = result["pcm_b64"] as? String,
              let data = Data(base64Encoded: b64),
              data.count >= 2, data.count % 2 == 0,
              data.count / 2 <= maxSamples else { return nil }
        let count = data.count / 2
        var out = [Float](repeating: 0, count: count)
        data.withUnsafeBytes { raw in
            for i in 0..<count {
                let lo = UInt16(raw[2 * i])
                let hi = UInt16(raw[2 * i + 1])
                out[i] = Float(Int16(bitPattern: lo | (hi << 8))) / 32768.0
            }
        }
        return out
    }
}

/// Одноразовый ящик pre-roll между поллером и разговором (main actor).
@MainActor
final class WakePrerollMailbox {
    static let shared = WakePrerollMailbox()

    /// Разговор должен стартовать сразу за детекцией; позже — это уже не тот разговор.
    static let claimWindow: TimeInterval = 2
    /// Pre-roll старше этого не используется (backend держит его 15 с от срабатывания).
    static let ttl: TimeInterval = 15

    private var armedAt: TimeInterval?
    private var claimed = false
    private var samples: [Float]?

    /// Детекция слова-пробуждения: ждём pre-roll для разговора, который она запускает.
    func arm(at now: TimeInterval) {
        armedAt = now
        claimed = false
        samples = nil
    }

    /// Ответ backend. Принимается, только если ящик взведён и не протух.
    func deliver(_ pcm: [Float], at now: TimeInterval) {
        guard let armedAt, now - armedAt <= Self.ttl, !pcm.isEmpty else { return }
        samples = pcm
    }

    /// Старт разговора: true — его запустила свежая детекция, pre-roll будет его.
    /// Иначе (горячая клавиша, старая детекция) ящик сбрасывается.
    @discardableResult
    func claimIfArmed(at now: TimeInterval) -> Bool {
        guard let armedAt, now - armedAt <= Self.claimWindow, !claimed else {
            reset()
            return false
        }
        claimed = true
        return true
    }

    /// `conv.ready` заявленного разговора: забрать сэмплы (один раз) и закрыть ящик.
    func take(at now: TimeInterval) -> [Float]? {
        defer { reset() }
        guard claimed, let armedAt, now - armedAt <= Self.ttl else { return nil }
        return samples
    }

    func reset() {
        armedAt = nil
        claimed = false
        samples = nil
    }
}
