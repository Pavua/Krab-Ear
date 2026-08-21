import AppKit
import AVFoundation
import Foundation

protocol CallAudioEngineProtocol: AnyObject {
    func start() throws
    func stop()
    func schedule(_ samples: [Float])
}

/// Реальный движок: AVAudioEngine + AVAudioPlayerNode, 8кГц моно Float32;
/// mainMixer ресемплит в hardware rate. Переключение аудио-устройства/сон
/// (AVAudioEngineConfigurationChange) → перезапуск движка на месте.
final class CallAudioEngine: CallAudioEngineProtocol {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let format = AVAudioFormat(standardFormatWithSampleRate: 8000, channels: 1)!
    private var observer: NSObjectProtocol?

    func start() throws {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        try engine.start()
        player.play()
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            // Наушники/AirPods/сон: движок остановился — рестарт, иначе кнопка
            // выглядит включённой при мёртвом звуке (spec §3 комп. 3).
            try? self.engine.start()
            self.player.play()
        }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        player.stop()
        engine.stop()
        engine.detach(player)
    }

    func schedule(_ samples: [Float]) {
        guard let buf = AVAudioPCMBuffer(pcmFormat: format,
                                         frameCapacity: AVAudioFrameCount(samples.count)) else { return }
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { src in
            buf.floatChannelData![0].update(from: src.baseAddress!, count: samples.count)
        }
        player.scheduleBuffer(buf, completionHandler: nil)
    }
}

/// Прослушка звонка: WS /monitor/audio → μ-law декод → движок.
/// ОДИН владелец listen-состояния (spec §3 комп. 3): HUD и панель — два
/// рендера onStateChange. Single-flight + generation: двойной клик или
/// HUD+панель одновременно не открывают второй сокет (лимит VG = 2).
/// autoReconnect=false: 1013 (лимит) ретраится только явным повторным кликом.
final class CallAudioPlayer {
    enum ListenState: Equatable { case idle, connecting, listening, subscriberLimit, failed }

    var onStateChange: ((ListenState, UInt64) -> Void)?
    var engineFactory: () -> CallAudioEngineProtocol = { CallAudioEngine() }
    var connectionFactoryForTests: ((URL, UInt64,
                                     @escaping (VGWebSocketConnection.Message, UInt64) -> Void,
                                     @escaping (Int, UInt64) -> Void) -> VGWebSocketConnecting)?

    private var connection: VGWebSocketConnecting?
    private var engine: CallAudioEngineProtocol?
    private(set) var generation: UInt64 = 0
    private var state: ListenState = .idle
    private var metadataValidated = false
    private var lastConnect: (baseURL: URL, sessionId: String, tokenProvider: () -> String)?
    private var wakeObserver: NSObjectProtocol?

    init() {
        // Сон/пробуждение: WS умирает при «живом» на вид состоянии — переподключаемся
        // (spec §3 комп. 3: кнопка не смеет врать про играющий звук).
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.state == .listening || self.state == .connecting,
                  let p = self.lastConnect else { return }
            let gen = self.generation
            self.teardownLocked(newState: .idle)
            self.startListening(baseURL: p.baseURL, sessionId: p.sessionId,
                                generation: gen, tokenProvider: p.tokenProvider)
        }
    }

    func startListening(baseURL: URL, sessionId: String, generation: UInt64,
                        tokenProvider: @escaping () -> String) {
        DispatchQueue.main.async { [self] in
            if generation == self.generation, state == .connecting || state == .listening {
                return  // single-flight
            }
            teardownLocked(newState: nil)
            self.generation = generation
            metadataValidated = false
            lastConnect = (baseURL, sessionId, tokenProvider)
            guard let url = VGWebSocketConnection.wsURL(httpBase: baseURL,
                                                        path: "/v1/sessions/\(sessionId)/monitor/audio") else {
                setState(.failed); return
            }
            let onMessage: (VGWebSocketConnection.Message, UInt64) -> Void = { [weak self] msg, gen in
                DispatchQueue.main.async { self?.handleMessage(msg, gen) }
            }
            let onClose: (Int, UInt64) -> Void = { [weak self] code, gen in
                DispatchQueue.main.async { self?.handleClose(code, gen) }
            }
            let conn: VGWebSocketConnecting
            if let factory = connectionFactoryForTests {
                conn = factory(url, generation, onMessage, onClose)
            } else {
                conn = VGWebSocketConnection(url: url, generation: generation, autoReconnect: false,
                                             tokenProvider: tokenProvider,
                                             onMessage: onMessage, onStateChange: nil,
                                             onClose: onClose)
            }
            connection = conn
            setState(.connecting)
            conn.connect()
        }
    }

    func stopListening() {
        DispatchQueue.main.async { [self] in
            guard connection != nil || state != .idle else { return }
            teardownLocked(newState: .idle)
        }
    }

    private func handleMessage(_ msg: VGWebSocketConnection.Message, _ gen: UInt64) {
        guard gen == generation, connection != nil else { return }
        switch msg {
        case .text(let s):
            guard !metadataValidated else { return }
            guard let data = s.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  obj["format"] as? String == "mulaw_8k" else {
                teardownLocked(newState: .failed)
                return
            }
            metadataValidated = true
            let engine = engineFactory()
            do { try engine.start() } catch { teardownLocked(newState: .failed); return }
            self.engine = engine
            setState(.listening)
        case .binary(let frame):
            guard metadataValidated, let engine else { return }
            engine.schedule(MuLawDecoder.decodeToFloat(frame))
        }
    }

    private func handleClose(_ code: Int, _ gen: UInt64) {
        guard gen == generation else { return }
        teardownLocked(newState: code == 1013 ? .subscriberLimit : .idle)
    }

    private func teardownLocked(newState: ListenState?) {
        connection?.permanentStop()
        connection = nil
        engine?.stop()
        engine = nil
        metadataValidated = false
        if let newState { setState(newState) }
    }

    private func setState(_ s: ListenState) {
        state = s
        onStateChange?(s, generation)
    }
}
