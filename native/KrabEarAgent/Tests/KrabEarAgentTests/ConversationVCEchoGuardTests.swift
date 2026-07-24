/*
 ConversationVCEchoGuardTests — защита от петли самоэха в «Разговоре с AI» (W1893).

 Живой инцидент 2026-07-24: колонки играли TTS-ответ → микрофон слышал его →
 VG распознавал эхо как речь пользователя → мозг отвечал сам себе → бесконечная
 петля. 77 минут ассистент разговаривал сам с собой (про погоду в Москве),
 сжигая облачную квоту, и обрывал каждую свою реплику собственным «барж-ином»
 (`conv.interrupted (user_started_speaking)` десятками в agent.log), из-за чего
 владелец не слышал НИ ОДНОГО ответа целиком.

 Основной фикс — системная эхо-компенсация macOS (VPIO, тот же тракт что у
 FaceTime): барж-ин сохраняется, эхо снимает ОС. Эти тесты покрывают fail-safe
 ВТОРОГО эшелона — полудуплексное окно тишины, работающее когда VPIO включить
 не удалось. Живой AVAudioEngine в юнит-тестах не поднимается (`.isolatedTests`),
 поэтому проверяем именно логику окна.
*/

import XCTest
@testable import KrabEarAgent

@MainActor
final class ConversationVCEchoGuardTests: XCTestCase {

    private var vc: ConversationViewController!

    override func setUp() async throws {
        try await super.setUp()
        vc = ConversationViewController(config: .default, runtimeOptions: .isolatedTests)
        vc.loadView()
        vc.viewDidLoad()
        vc.prepareAudioNegotiation()
        vc.configureNegotiatedAudio(sampleRate: nil)
    }

    override func tearDown() async throws {
        vc = nil
        try await super.tearDown()
    }

    /// Базовое состояние: до прихода TTS микрофон не заглушен.
    func test_beforeAnyDownlink_uplinkIsNotSuppressed() {
        XCTAssertFalse(vc.isOwnPlaybackAudible(),
                       "До получения TTS окно тишины должно быть закрыто — иначе микрофон "
                       + "молчал бы в начале каждой сессии")
    }

    /// Ядро фикса: пришедший TTS-чанк открывает окно тишины.
    /// RED до фикса — окна не существовало, эхо уходило обратно в VG.
    func test_downlinkAudio_opensEchoGuardWindow() {
        // 16 000 сэмплов PCM16 = 32 000 байт = 1.0 с на контрактных 16 кГц.
        let oneSecond = Data(repeating: 0x11, count: 32_000)
        vc.handleDownlinkAudio(oneSecond)

        XCTAssertTrue(vc.isOwnPlaybackAudible(),
                      "Пока играет собственный TTS, uplink обязан молчать — иначе микрофон "
                      + "вернёт эхо в VG и запустит петлю самоэха")
    }

    /// Окно продлевается от КОНЦА уже запланированного, а не от «сейчас»:
    /// чанки приходят из сети быстрее реального времени и копятся в плеере.
    func test_consecutiveChunks_extendWindowCumulatively() {
        let oneSecond = Data(repeating: 0x11, count: 32_000)
        vc.handleDownlinkAudio(oneSecond)
        let afterFirst = vc.echoGuardDeadlineForTests
        vc.handleDownlinkAudio(oneSecond)
        let afterSecond = vc.echoGuardDeadlineForTests

        // Два чанка по 1 с, пришедшие подряд мгновенно, дают ~2 с воспроизведения.
        let delta = afterSecond.timeIntervalSince(afterFirst)
        XCTAssertEqual(delta, 1.0, accuracy: 0.2,
                       "Второй чанк должен продлить окно на СВОЮ длительность от конца "
                       + "первого; отсчёт от «сейчас» схлопнул бы очередь и открыл "
                       + "микрофон посреди ответа")
    }

    /// Перебивание снимает запланированные буферы — звука больше нет,
    /// окно обязано закрыться немедленно.
    func test_flushDownlinkPlayback_closesWindowImmediately() {
        vc.handleDownlinkAudio(Data(repeating: 0x11, count: 32_000))
        XCTAssertTrue(vc.isOwnPlaybackAudible())

        vc.flushDownlinkPlayback()

        XCTAssertFalse(vc.isOwnPlaybackAudible(),
                       "После сброса очереди микрофон должен открыться сразу — иначе после "
                       + "перебивания он молчал бы ещё всю длину снятой очереди")
    }

    /// Симметрия с VPIO: когда системная эхо-компенсация активна, полудуплексный
    /// fail-safe не нужен — барж-ин обязан продолжать работать во время ответа.
    func test_withEchoCancellation_uplinkSurvivesDuringPlayback() {
        vc.isEchoCancellationActive = true
        vc.handleDownlinkAudio(Data(repeating: 0x11, count: 32_000))

        // Окно как таковое ведётся всегда (дёшево и упрощает диагностику), но
        // подавлять uplink оно должно ТОЛЬКО в отсутствие VPIO.
        XCTAssertTrue(vc.isOwnPlaybackAudible(),
                      "Окно ведётся независимо от VPIO")
        XCTAssertTrue(vc.isEchoCancellationActive,
                      "При активном VPIO uplink не глушится — иначе барж-ин "
                      + "(перебить ассистента голосом) перестал бы работать")
    }
}
