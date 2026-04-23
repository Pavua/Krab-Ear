/*
 SystemAudioCapture.swift — Захват системного аудио выхода через ScreenCaptureKit (Phase 2B).

 Sprint 2B: Live субтитры над видео.
 Шаг 1/3: skeleton — permission check + start/stop lifecycle + HUD placeholder.
 Actual audio format conversion (16kHz mono PCM → STT pipeline) — следующий PR.

 Связи модуля:
 1) main.swift: создаётся как property, запускается по hotkey Cmd+Shift+L или Settings toggle.
 2) HistoryPanelController+Settings: toggle "Live субтитры для видео" (UserDefaults KrabEar_LiveSubsEnabled).
 3) RealtimeOverlayController: будет показывать субтитры из onAudioBuffer — следующий PR.

 Тех. подход: SCStream (ScreenCaptureKit, macOS 12.3+) с capturesAudio=true.
 Разрешение "Screen Recording" запрашивается macOS TCC автоматически при первом start().

 NOTE (skeleton): SCStream требует и video, и audio делегата одновременно.
 В этом PR video stream включён (нулевой размер) но video frames игнорируются —
 нас интересуют только audio CMSampleBuffer-ы.
 AVAudioConverter (PCM → 16kHz mono) подключается в следующем PR.
*/

import AVFoundation
import Foundation
@preconcurrency import ScreenCaptureKit

// MARK: - Error

/// Ошибки SystemAudioCapture.
enum SystemAudioCaptureError: Error, LocalizedError {
    /// Пользователь отказал или ещё не дал разрешение Screen Recording.
    case permissionDenied
    /// Нет доступных audio output (редко, но возможно).
    case noAudioOutputAvailable
    /// SCStream не удалось запустить.
    case streamStartFailed(underlying: Error)
    /// SCStream не удалось остановить.
    case streamStopFailed(underlying: Error)
    /// macOS < 12.3 — ScreenCaptureKit не поддерживается.
    case unsupportedOS

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            return "Нет разрешения Screen Recording. Откройте Системные настройки → Конфиденциальность → Запись экрана и включите Krab Ear."
        case .noAudioOutputAvailable:
            return "Не найдены аудио-выходы системы."
        case .streamStartFailed(let err):
            return "Не удалось запустить захват аудио: \(err.localizedDescription)"
        case .streamStopFailed(let err):
            return "Не удалось остановить захват аудио: \(err.localizedDescription)"
        case .unsupportedOS:
            return "Захват системного аудио требует macOS 12.3+."
        }
    }
}

// MARK: - Delegate protocol

/// Протокол для получения audio frames от SystemAudioCapture.
/// onAudioBuffer вызывается на background-потоке SCStream.
protocol SystemAudioCaptureDelegate: AnyObject {
    /// Вызывается для каждого входящего CMSampleBuffer.
    /// В Phase 2B/step 1: skeleton — реализация в следующем PR.
    /// В Phase 2B/step 2: получает 16kHz mono PCM AVAudioPCMBuffer → STT pipeline.
    func systemAudioCapture(
        _ capture: SystemAudioCapture,
        didReceiveSampleBuffer sampleBuffer: CMSampleBuffer,
        capturedSeconds: TimeInterval
    )

    /// Вызывается при ошибке захвата (напр. разрешение отозвано).
    func systemAudioCapture(_ capture: SystemAudioCapture, didFailWithError error: Error)
}

// Опциональные методы делегата.
extension SystemAudioCaptureDelegate {
    func systemAudioCapture(_ capture: SystemAudioCapture, didFailWithError error: Error) {}
}

// MARK: - SystemAudioCapture

/// Захватывает audio output системы через ScreenCaptureKit (macOS 12.3+).
///
/// Phase 2B step 1/3 — skeleton:
/// - Permission check / запрос разрешения Screen Recording
/// - start() / stop() lifecycle + HUD status string
/// - CMSampleBuffer delegate (аудио frames приходят, но конвертация → 16kHz в следующем PR)
///
/// Для filter по конкретному приложению (Safari) установите captureAppFilter в будущем PR.
@available(macOS 12.3, *)
@MainActor
final class SystemAudioCapture: NSObject {

    // MARK: - Public state

    /// Идёт ли в данный момент захват.
    private(set) var isCapturing: Bool = false

    /// Накопленных секунд аудио с последнего start().
    private(set) var capturedSeconds: TimeInterval = 0

    /// HUD status string — обновляется по мере захвата.
    var hudStatusString: String {
        guard isCapturing else { return "Live субтитры: остановлены" }
        let sec = String(format: "%.1f", capturedSeconds)
        return "Live субтитры: захвачено \(sec)с аудио | следующий шаг: STT pipeline"
    }

    /// Делегат для получения аудио frames.
    weak var delegate: SystemAudioCaptureDelegate?

    /// Будущий фильтр по конкретному приложению (MVP: nil = весь audio output).
    var captureAppFilter: SCRunningApplication?

    // MARK: - Private

    private let logger = AgentLogger.shared
    private var stream: SCStream?
    private var streamOutput: SCStreamOutputAdapter?
    private let captureQueue = DispatchQueue(label: "com.krabear.SystemAudioCapture", qos: .userInteractive)

    // Accumulates sample count → capturedSeconds (rough estimate, updated per buffer)
    private var sampleRate: Double = 44100
    private var totalSamplesReceived: Int64 = 0

    // MARK: - Public API

    /// Запрашивает разрешение Screen Recording (если не дано) и начинает захват audio output.
    /// Вызывается на @MainActor. Completion вызывается на @MainActor.
    func start(completion: @escaping (Error?) -> Void) {
        guard #available(macOS 12.3, *) else {
            completion(SystemAudioCaptureError.unsupportedOS)
            return
        }

        guard !isCapturing else {
            completion(nil)
            return
        }

        logger.info("SystemAudioCapture: запрос разрешения Screen Recording...")

        // Step 1: check / request permission (async, but @MainActor Task)
        Task { @MainActor in
            let hasPermission = await self.checkAndRequestPermission()
            guard hasPermission else {
                completion(SystemAudioCaptureError.permissionDenied)
                return
            }

            // Step 2: enumerate shareable content
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                await self.startStream(content: content, completion: completion)
            } catch {
                self.logger.error("SystemAudioCapture: не удалось получить SCShareableContent: \(error.localizedDescription)")
                completion(SystemAudioCaptureError.permissionDenied)
            }
        }
    }

    /// Останавливает захват и освобождает SCStream.
    /// Вызывается на @MainActor. Completion вызывается на @MainActor.
    func stop(completion: ((Error?) -> Void)? = nil) {
        guard isCapturing, let stream else {
            completion?(nil)
            return
        }

        logger.info("SystemAudioCapture: остановка потока...")

        Task { @MainActor in
            do {
                try await stream.stopCapture()
                self.isCapturing = false
                self.stream = nil
                self.streamOutput = nil
                self.totalSamplesReceived = 0
                self.capturedSeconds = 0
                self.logger.info("SystemAudioCapture: поток остановлен")
                completion?(nil)
            } catch {
                self.logger.error("SystemAudioCapture: ошибка остановки: \(error.localizedDescription)")
                completion?(SystemAudioCaptureError.streamStopFailed(underlying: error))
            }
        }
    }

    // MARK: - Private helpers

    /// Проверяет/запрашивает разрешение Screen Recording через SCShareableContent.
    private func checkAndRequestPermission() async -> Bool {
        do {
            // Попытка получить список контента — инициирует TCC-запрос если не дано разрешение
            _ = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            return true
        } catch {
            logger.warn("SystemAudioCapture: разрешение Screen Recording не дано: \(error.localizedDescription)")
            // Открыть System Settings для пользователя (уже на @MainActor)
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                NSWorkspace.shared.open(url)
            }
            return false
        }
    }

    /// Настраивает и запускает SCStream.
    private func startStream(content: SCShareableContent, completion: @escaping (Error?) -> Void) async {
        guard let display = content.displays.first else {
            completion(SystemAudioCaptureError.noAudioOutputAvailable)
            return
        }

        // Настройка фильтра
        let filter: SCContentFilter
        if let targetApp = captureAppFilter {
            // Future: filter по конкретному приложению (Safari и т.д.)
            // Для filter по app используем excludingApplications с инвертированным подходом:
            // захватываем всё кроме всех остальных приложений — упрощение MVP.
            let otherApps = content.applications.filter { $0.processID != targetApp.processID }
            filter = SCContentFilter(display: display, excludingApplications: otherApps, exceptingWindows: [])
        } else {
            // MVP: захватываем весь audio output системы (все приложения на display)
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        }

        let config = makeStreamConfig()
        await launchStream(filter: filter, config: config, completion: completion)
    }

    // startStream calls launchStream which is already @MainActor async — no extra dispatch needed

    private func makeStreamConfig() -> SCStreamConfiguration {
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        // excludesCurrentProcessAudioFromCapture доступно с macOS 14.2+
        // В MVP пропускаем — TTS Krab Ear будет захватываться, но это edge-case
        // TODO: добавить @available(macOS 14.2, *) guard в следующем PR
        // Минимальный video stream (требуется SCStream, но frames игнорируем)
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1 fps минимум
        config.showsCursor = false
        return config
    }

    private func launchStream(filter: SCContentFilter, config: SCStreamConfiguration, completion: @escaping (Error?) -> Void) async {
        let adapter = SCStreamOutputAdapter(capture: self)
        let newStream = SCStream(filter: filter, configuration: config, delegate: adapter)

        do {
            try newStream.addStreamOutput(adapter, type: .audio, sampleHandlerQueue: captureQueue)
            try newStream.addStreamOutput(adapter, type: .screen, sampleHandlerQueue: captureQueue)
        } catch {
            logger.error("SystemAudioCapture: не удалось добавить output: \(error.localizedDescription)")
            completion(SystemAudioCaptureError.streamStartFailed(underlying: error))
            return
        }

        do {
            try await newStream.startCapture()
            streamOutput = adapter
            stream = newStream
            isCapturing = true
            totalSamplesReceived = 0
            capturedSeconds = 0
            logger.info("SystemAudioCapture: поток запущен успешно")
            completion(nil)
        } catch {
            logger.error("SystemAudioCapture: startCapture failed: \(error.localizedDescription)")
            completion(SystemAudioCaptureError.streamStartFailed(underlying: error))
        }
    }

    // MARK: - Internal buffer handler (called from SCStreamOutputAdapter, background queue)

    /// Вызывается из SCStreamOutputAdapter для каждого audio CMSampleBuffer.
    /// `nonisolated` — SCStreamOutput delegate вызывается на captureQueue (background).
    /// Диспатчим обновление состояния обратно на @MainActor.
    nonisolated func handleAudioBuffer(_ sampleBuffer: CMSampleBuffer) {
        // Вычисляем количество сэмплов на background queue
        var rate: Double = 44100
        if let formatDescription = sampleBuffer.formatDescription,
           let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) {
            let r = asbd.pointee.mSampleRate
            if r > 0 { rate = r }
        }
        let sampleCount = CMSampleBufferGetNumSamples(sampleBuffer)

        // CMSampleBuffer не Sendable — оборачиваем для безопасной передачи в Task.
        // CMSampleBuffer retain-safe: SCStream держит его живым пока он в очереди.
        // nonisolated(unsafe) — используем только для чтения в замыкании @MainActor.
        nonisolated(unsafe) let sendableBuffer = sampleBuffer

        // Обновляем состояние и нотифицируем делегата на @MainActor
        Task { @MainActor [weak self] in
            guard let self else { return }
            if rate != self.sampleRate { self.sampleRate = rate }
            self.totalSamplesReceived += Int64(sampleCount)
            let newCapturedSec = Double(self.totalSamplesReceived) / self.sampleRate
            self.capturedSeconds = newCapturedSec

            // NOTE Phase 2B step 1: skeleton — передаём raw CMSampleBuffer делегату.
            // В следующем PR: конвертируем через AVAudioConverter → 16kHz mono PCM AVAudioPCMBuffer.
            self.delegate?.systemAudioCapture(self, didReceiveSampleBuffer: sendableBuffer, capturedSeconds: newCapturedSec)
        }
    }

    /// Вызывается из SCStreamOutputAdapter при ошибке потока.
    /// `nonisolated` — SCStreamDelegate вызывается на внутренней очереди SCStream.
    nonisolated func handleStreamError(_ error: Error) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.logger.error("SystemAudioCapture: ошибка потока: \(error.localizedDescription)")
            self.isCapturing = false
            self.delegate?.systemAudioCapture(self, didFailWithError: error)
        }
    }
}

// MARK: - SCStreamOutputAdapter

/// Адаптер-обёртка для SCStreamOutput и SCStreamDelegate.
/// Отделён от SystemAudioCapture чтобы избежать retain-cycle.
///
/// SCStreamOutput / SCStreamDelegate вызываются на captureQueue (background).
/// `nonisolated` — поэтому храним capture через `nonisolated(unsafe)` weak reference.
/// Вызов handleAudioBuffer/handleStreamError безопасен: те методы nonisolated и диспатчат на @MainActor.
@available(macOS 12.3, *)
private final class SCStreamOutputAdapter: NSObject, SCStreamOutput, SCStreamDelegate {
    // nonisolated(unsafe) позволяет обращаться к @MainActor-типу из nonisolated контекста.
    // Безопасность гарантируется тем, что handleAudioBuffer и handleStreamError — nonisolated.
    nonisolated(unsafe) weak var capture: SystemAudioCapture?

    init(capture: SystemAudioCapture) {
        self.capture = capture
    }

    // MARK: SCStreamOutput — вызывается на captureQueue (background)

    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return } // video frames игнорируем
        capture?.handleAudioBuffer(sampleBuffer)
    }

    // MARK: SCStreamDelegate — вызывается на внутренней очереди SCStream

    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        capture?.handleStreamError(error)
    }
}

// MARK: - UserDefaults key

extension UserDefaults {
    /// Включены ли Live субтитры для видео (Phase 2B).
    static let krabLiveSubsEnabledKey = "KrabEar_LiveSubsEnabled"

    var liveSubsEnabled: Bool {
        get { bool(forKey: UserDefaults.krabLiveSubsEnabledKey) }
        set { set(newValue, forKey: UserDefaults.krabLiveSubsEnabledKey) }
    }
}
