/*
 WakeWordListener — обёртка над Porcupine SDK для детекции слова-пробуждения «Краб».

 Архитектура:
 - Porcupine SDK (Picovoice) встраивается как Swift Package (см. Package.swift).
 - Аудиопоток захватывается через AVAudioEngine (16 kHz моно PCM).
 - Inference запускается каждые 80 мс (512 samples на 16 kHz).
 - При детекции вызывается callback onWakeWordDetected на главном потоке.
 - По умолчанию выключен (toggle в настройках, UserDefaults ключ).

 Требования к ключу доступа:
 - AccessKey получается через https://console.picovoice.ai (бесплатный tier).
 - Хранится в ~/.krab_ear_data/porcupine_access_key ИЛИ env var
   KRAB_EAR_PORCUPINE_ACCESS_KEY.
 - Если ключ отсутствует → wake word отключён с предупреждением.

 Файл .ppn для фразы «Краб»:
 - Тренируется вручную через https://console.picovoice.ai/ppw (Custom Wake Word)
 - Результирующий файл «Краб_ru_mac_v3_0_0.ppn» кладётся рядом с .app или в
   ~/Library/Application Support/KrabEar/
 - Если .ppn не найден → fallback на встроенный тест-ресурс (если доступен)
   или выводится предупреждение и wake word не запускается.

 Связи:
 - HotkeyManager.swift или main.swift: создаёт WakeWordListener, вызывает start().
 - HistoryPanelController+VoiceTab.swift: triggerConversationFromWakeWord().
 - HistoryPanelController+Settings.swift: toggle «Детектор пробуждения Краб».
*/

import AppKit
import AVFoundation
import Foundation

// MARK: - Porcupine protocol (для mock в тестах без реального SDK)

/// Абстрактный протокол Porcupine-движка для возможности мокирования в тестах.
/// Реальная реализация делегирует вызовы к Picovoice Porcupine SDK.
protocol PorcupineEngineProtocol: AnyObject, Sendable {
    /// Обрабатывает 512 PCM-семплов (Int16), возвращает индекс обнаруженного
    /// ключевого слова (>=0) или -1 если ничего не обнаружено.
    func process(pcm: [Int16]) throws -> Int32
    func delete()
}

// MARK: - WakeWordListener

/// Детектор слова-пробуждения «Краб» на базе Porcupine (Picovoice).
///
/// Использование:
/// ```swift
/// let listener = WakeWordListener(onWakeWordDetected: {
///     panel.triggerConversationFromWakeWord()
/// })
/// listener.start()
/// // ...
/// listener.stop()
/// ```
@MainActor
final class WakeWordListener {

    // MARK: - Constants

    /// Частота дискретизации, требуемая Porcupine (16 kHz).
    static let sampleRate: Double = 16_000.0

    /// Размер фрейма в семплах (512 → 32 мс на 16 kHz).
    static let frameLength: AVAudioFrameCount = 512

    // MARK: - State

    private var audioEngine: AVAudioEngine?
    private var porcupineEngine: PorcupineEngineProtocol?
    private let onWakeWordDetected: () -> Void
    private var isRunning = false
    private var buffer: [Int16] = []

    // MARK: - Init

    /// - Parameters:
    ///   - engine: Экземпляр Porcupine-движка. Если nil — используется реальный
    ///     SDK через `createPorcupineEngine()`. Передайте mock для тестов.
    ///   - onWakeWordDetected: Callback на главном потоке при детекции «Краб».
    init(
        engine: PorcupineEngineProtocol? = nil,
        onWakeWordDetected: @escaping () -> Void
    ) {
        self.porcupineEngine = engine
        self.onWakeWordDetected = onWakeWordDetected
    }

    // NOTE: porcupineEngine cleanup happens via stop() which must be called before dealloc.
    // Cannot call delete() from nonisolated deinit in Swift 6 — MainActor-isolated property.

    // MARK: - Public API

    /// Запустить детектор. Требует AccessKey и .ppn файл (документировано в заголовке).
    /// Если ключ или .ppn не найдены — логирует предупреждение и возвращает false.
    @discardableResult
    func start() -> Bool {
        guard !isRunning else { return true }

        // Загрузить движок если не был передан (тестовый mock)
        if porcupineEngine == nil {
            guard let eng = createPorcupineEngine() else {
                AgentLogger.shared.warn("[WakeWordListener] Движок Porcupine не инициализирован. Wake word выключен.")
                return false
            }
            porcupineEngine = eng
        }

        do {
            try startAudioCapture()
            isRunning = true
            AgentLogger.shared.info("[WakeWordListener] Детектор «Краб» запущен.")
            return true
        } catch {
            AgentLogger.shared.warn("[WakeWordListener] Ошибка запуска аудио: \(error.localizedDescription)")
            return false
        }
    }

    /// Остановить детектор и освободить аудиодвижок.
    func stop() {
        guard isRunning else { return }
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine = nil
        isRunning = false
        buffer = []
        AgentLogger.shared.info("[WakeWordListener] Детектор «Краб» остановлен.")
    }

    // MARK: - Porcupine engine creation

    /// Создаёт реальный Porcupine-движок через SDK.
    /// Возвращает nil если ключ или .ppn не найдены.
    ///
    /// NOTE: Этот метод требует реального Porcupine Swift SDK (Package.swift).
    /// Сигнатура класса Porcupine в SDK:
    ///   `init(accessKey: String, keywordPaths: [String], sensitivities: [Float32]?) throws`
    ///
    /// При отсутствии SDK (тесты без зависимостей) этот метод возвращает nil —
    /// для тестов передайте mock через init(engine:).
    private func createPorcupineEngine() -> PorcupineEngineProtocol? {
        guard let accessKey = loadAccessKey(), !accessKey.isEmpty else {
            AgentLogger.shared.warn("[WakeWordListener] KRAB_EAR_PORCUPINE_ACCESS_KEY не задан.")
            return nil
        }

        guard let ppnPath = findKeywordFile() else {
            AgentLogger.shared.warn(
                "[WakeWordListener] Файл .ppn для «Краб» не найден. " +
                "Создайте на https://console.picovoice.ai и поместите в " +
                "~/Library/Application Support/KrabEar/"
            )
            return nil
        }

        // Реальная инициализация Porcupine SDK.
        // Когда PorcupineSDK добавлен в Package.swift — раскомментируйте:
        //
        // do {
        //     let porcupine = try Porcupine(
        //         accessKey: accessKey,
        //         keywordPaths: [ppnPath],
        //         sensitivities: [0.5]
        //     )
        //     return PorcupineEngineWrapper(porcupine: porcupine)
        // } catch {
        //     AgentLogger.shared.warn("[WakeWordListener] Ошибка init Porcupine: \(error)")
        //     return nil
        // }

        // Временная заглушка до добавления реального SDK:
        AgentLogger.shared.info("[WakeWordListener] Porcupine SDK не подключён. Используйте PorcupineManager (Package.swift).")
        AgentLogger.shared.info("[WakeWordListener] AccessKey: \(accessKey.prefix(8))..., ppn: \(ppnPath)")
        return nil
    }

    // MARK: - Access key loading

    private func loadAccessKey() -> String? {
        // 1. Env var
        if let envKey = ProcessInfo.processInfo.environment["KRAB_EAR_PORCUPINE_ACCESS_KEY"],
           !envKey.isEmpty {
            return envKey
        }

        // 2. File ~/.krab_ear_data/porcupine_access_key
        let keyFilePath = (NSString(string: "~/.krab_ear_data/porcupine_access_key")).expandingTildeInPath
        if let key = try? String(contentsOfFile: keyFilePath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !key.isEmpty {
            return key
        }

        // 3. File ~/Library/Application Support/KrabEar/porcupine_access_key
        let supportPath = (NSString(string: "~/Library/Application Support/KrabEar/porcupine_access_key")).expandingTildeInPath
        if let key = try? String(contentsOfFile: supportPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !key.isEmpty {
            return key
        }

        return nil
    }

    // MARK: - .ppn keyword file discovery

    private func findKeywordFile() -> String? {
        let candidates = [
            // ~/Library/Application Support/KrabEar/
            (NSString(string: "~/Library/Application Support/KrabEar/Краб_ru_mac_v3_0_0.ppn")).expandingTildeInPath,
            (NSString(string: "~/Library/Application Support/KrabEar/Krab_ru_mac.ppn")).expandingTildeInPath,
            // ~/.krab_ear_data/
            (NSString(string: "~/.krab_ear_data/Краб_ru_mac_v3_0_0.ppn")).expandingTildeInPath,
            // Рядом с .app бинарем
            Bundle.main.resourcePath.map { $0 + "/Краб_ru_mac_v3_0_0.ppn" } ?? "",
        ]

        let fm = FileManager.default
        for path in candidates where !path.isEmpty {
            if fm.fileExists(atPath: path) {
                return path
            }
        }
        return nil
    }

    // MARK: - Audio capture

    private func startAudioCapture() throws {
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let inputFormat = inputNode.outputFormat(forBus: 0)

        // Porcupine требует 16 kHz моно Int16
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Self.sampleRate,
            channels: 1,
            interleaved: false
        ) else {
            throw WakeWordError.audioFormatUnavailable
        }

        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw WakeWordError.converterUnavailable
        }

        inputNode.installTap(
            onBus: 0,
            bufferSize: 1024,
            format: inputFormat
        ) { [weak self] inputBuffer, _ in
            self?.processTap(inputBuffer: inputBuffer, converter: converter, targetFormat: targetFormat)
        }

        try engine.start()
        self.audioEngine = engine
    }

    private func processTap(
        inputBuffer: AVAudioPCMBuffer,
        converter: AVAudioConverter,
        targetFormat: AVAudioFormat
    ) {
        let inputSampleRate = inputBuffer.format.sampleRate
        let ratio = Self.sampleRate / max(inputSampleRate, 1.0)
        let capacity = AVAudioFrameCount(Double(inputBuffer.frameLength) * ratio) + 64
        guard let convertedBuffer = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: capacity
        ) else { return }

        var error: NSError?
        var sourceConsumed = false
        converter.convert(to: convertedBuffer, error: &error) { _, outStatus in
            if sourceConsumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            sourceConsumed = true
            outStatus.pointee = .haveData
            return inputBuffer
        }

        guard error == nil,
              convertedBuffer.frameLength > 0,
              let int16Data = convertedBuffer.int16ChannelData?.pointee else { return }

        let count = Int(convertedBuffer.frameLength)
        let samples = Array(UnsafeBufferPointer(start: int16Data, count: count))

        // Накапливаем в буфер, обрабатываем пофреймово
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.buffer.append(contentsOf: samples)
            self.drainBuffer()
        }
    }

    private func drainBuffer() {
        let frameLen = Int(Self.frameLength)
        while buffer.count >= frameLen {
            let frame = Array(buffer.prefix(frameLen))
            buffer.removeFirst(frameLen)
            processFrame(frame)
        }
    }

    private func processFrame(_ frame: [Int16]) {
        testProcessFrameInternal(frame)
    }

    /// Тест-хук: обработать один фрейм без AVAudioEngine.
    /// Вызывается из WakeWordListenerTests через extension в тест-таргете.
    func testProcessFrameInternal(_ frame: [Int16]) {
        guard let engine = porcupineEngine else { return }
        do {
            let keywordIndex = try engine.process(pcm: frame)
            if keywordIndex >= 0 {
                AgentLogger.shared.info("[WakeWordListener] Обнаружено слово-пробуждение «Краб» (индекс \(keywordIndex)).")
                onWakeWordDetected()
            }
        } catch {
            AgentLogger.shared.warn("[WakeWordListener] Ошибка inference Porcupine: \(error)")
        }
    }
}

// MARK: - WakeWordError

enum WakeWordError: Error, LocalizedError {
    case audioFormatUnavailable
    case converterUnavailable
    case engineInitFailed(String)

    var errorDescription: String? {
        switch self {
        case .audioFormatUnavailable:   return "Не удалось создать аудиоформат 16 kHz Int16"
        case .converterUnavailable:     return "Не удалось создать AVAudioConverter"
        case .engineInitFailed(let msg): return "Porcupine init failed: \(msg)"
        }
    }
}

// MARK: - PorcupineEngineWrapper (реальный SDK)

/// Обёртка реального Porcupine SDK для соответствия PorcupineEngineProtocol.
/// Раскомментируйте когда PorcupineManager добавлен в Package.swift.
///
/// final class PorcupineEngineWrapper: PorcupineEngineProtocol {
///     private let porcupine: Porcupine
///     init(porcupine: Porcupine) { self.porcupine = porcupine }
///     func process(pcm: [Int16]) throws -> Int32 {
///         try porcupine.process(pcm: pcm)
///     }
///     func delete() { porcupine.delete() }
/// }
