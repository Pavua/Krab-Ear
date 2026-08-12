/*
 main+LiveSubs — интеграция SystemAudioCapture + LiveSubtitlesOverlay в AgentAppDelegate.

 Горячая клавиша Cmd+Option+Shift+L: toggle захвата системного аудио (Cmd+Shift+L конфликтует с Safari).
 При старте захвата — показывает HUD.
 При остановке — скрывает HUD и отправляет live_subs_stop бэкенду.

 Живой инцидент 2026-08-12 00:41 (спека
 docs/superpowers/specs/2026-08-12-live-subs-backpressure-design.md §3/§4, F4/F5):
 захват отвалился сам (бэкенд захлебнулся под 2x-темпом видео) → тумблер,
 завязанный только на isCapturing, вместо остановки запускал захват заново,
 а окно оставалось пустым и всегда-поверх навсегда — штатного способа
 закрыть его не было. F4 чинит тумблер (гейт по isCapturing ИЛИ isVisible),
 F5 добавляет watchdog-таймер (см. LiveSubsOverlayWatchdogGate.swift):
 оверлей, повисший без подтверждённого isCapturing дольше grace-периода,
 закрывается сам.

 Связи:
 - AgentAppDelegate: хранит systemAudioCapture и liveSubsOverlay
 - main+StatusMenu.swift: регистрирует Cmd+Option+Shift+L пункт меню
 - LiveSubsOverlayWatchdogGate: чистая решающая логика F5 (без Timer/Date)
*/

import AppKit
import Foundation

// MARK: - AgentAppDelegate + Live Subs

extension AgentAppDelegate {

    // MARK: - Accessors (stored via objc associated objects)

    // UInt8 keys: address is what uniquely identifies the key, not the value
    private static var captureKey: UInt8 = 0
    private static var overlayKey: UInt8 = 0
    private static var watchdogTimerKey: UInt8 = 0
    private static var watchdogCapturingLastTrueAtKey: UInt8 = 0

    var systemAudioCapture: SystemAudioCapture {
        if let existing = objc_getAssociatedObject(self, &Self.captureKey) as? SystemAudioCapture {
            return existing
        }
        let capture = SystemAudioCapture(ipcClient: ipcClient)
        objc_setAssociatedObject(self, &Self.captureKey, capture, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return capture
    }

    var liveSubsOverlay: LiveSubtitlesOverlay {
        if let existing = objc_getAssociatedObject(self, &Self.overlayKey) as? LiveSubtitlesOverlay {
            return existing
        }
        let overlay = LiveSubtitlesOverlay()
        objc_setAssociatedObject(self, &Self.overlayKey, overlay, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return overlay
    }

    /// F5 watchdog: живёт только пока оверлей виден, см. startLiveSubsWatchdog/stopLiveSubsWatchdog.
    private var liveSubsWatchdogTimer: Timer? {
        get { objc_getAssociatedObject(self, &Self.watchdogTimerKey) as? Timer }
        set { objc_setAssociatedObject(self, &Self.watchdogTimerKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    /// F5 watchdog: последний момент, когда isCapturing наблюдался true (или запуск захвата).
    private var liveSubsCapturingLastTrueAt: Date? {
        get { objc_getAssociatedObject(self, &Self.watchdogCapturingLastTrueAtKey) as? Date }
        set { objc_setAssociatedObject(self, &Self.watchdogCapturingLastTrueAtKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    // MARK: - Toggle

    /// Вызывается из Cmd+Option+Shift+L меню и из Settings toggle.
    ///
    /// F4 (спека §3): если захват отвалился сам, а окно осталось висеть,
    /// тумблер обязан УБРАТЬ окно, а не запускать захват заново (штатного
    /// способа закрыть висящий оверлей иначе не было).
    func toggleLiveSubsCaptureFromMenu() {
        if LiveSubsToggleGate.shouldStop(
            isCapturing: systemAudioCapture.isCapturing,
            isOverlayVisible: liveSubsOverlay.isVisible
        ) {
            stopLiveSubsCapture()
        } else {
            startLiveSubsCapture()
        }
    }

    func startLiveSubsCapture() {
        // Применить актуальный target lang из settings
        syncLiveSubsSettings()
        systemAudioCapture.start()
        liveSubsOverlay.show()
        liveSubsCapturingLastTrueAt = Date()
        startLiveSubsWatchdog()
        logger.info("Live Subs: захват системного аудио запущен")
        notify(title: "Krab Ear", body: "Live субтитры включены (Cmd+Option+Shift+L для остановки)")
    }

    @objc func onToggleLiveSubs() {
        toggleLiveSubsCaptureFromMenu()
    }

    func stopLiveSubsCapture() {
        systemAudioCapture.stop()
        liveSubsOverlay.hide()
        stopLiveSubsWatchdog()
        logger.info("Live Subs: захват остановлен")
    }

    // MARK: - F5: sticky-state watchdog

    /// Стартует таймер тика (интервал — LiveSubsOverlayWatchdogGate.tickIntervalSec).
    /// Живёт ТОЛЬКО пока оверлей виден — не крутится вечно.
    private func startLiveSubsWatchdog() {
        stopLiveSubsWatchdogTimer()
        let timer = Timer.scheduledTimer(
            withTimeInterval: LiveSubsOverlayWatchdogGate.tickIntervalSec,
            repeats: true
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.tickLiveSubsWatchdog()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        liveSubsWatchdogTimer = timer
    }

    /// Полная остановка watchdog: инвалидирует таймер и сбрасывает отметку.
    /// Вызывается из stopLiveSubsCapture() (явная остановка) — следующий
    /// startLiveSubsCapture() заново взводит отметку.
    private func stopLiveSubsWatchdog() {
        stopLiveSubsWatchdogTimer()
        liveSubsCapturingLastTrueAt = nil
    }

    private func stopLiveSubsWatchdogTimer() {
        liveSubsWatchdogTimer?.invalidate()
        liveSubsWatchdogTimer = nil
    }

    /// Тик watchdog: окно скрылось само (не watchdog'ом) — таймеру больше
    /// нечего делать. isCapturing true — освежаем отметку. isCapturing false
    /// дольше grace-периода — оверлей завис (sticky state), закрываем сами.
    private func tickLiveSubsWatchdog() {
        guard liveSubsOverlay.isVisible else {
            stopLiveSubsWatchdog()
            return
        }
        if systemAudioCapture.isCapturing {
            liveSubsCapturingLastTrueAt = Date()
            return
        }
        let since = Date().timeIntervalSince(liveSubsCapturingLastTrueAt ?? Date())
        guard LiveSubsOverlayWatchdogGate.shouldHide(
            isOverlayVisible: true,
            isCapturing: false,
            secondsSinceCapturingWasTrue: since
        ) else { return }

        logger.info(
            "Live Subs: оверлей закрыт watchdog'ом — isCapturing не " +
            "подтверждался \(Int(since))с (grace \(Int(LiveSubsOverlayWatchdogGate.graceSec))с)"
        )
        liveSubsOverlay.hide()
        stopLiveSubsWatchdog()
    }

    /// Синхронизирует настройки из settings → capture + overlay
    func syncLiveSubsSettings() {
        let targetLang: String
        switch settings.translationMode {
        case "ru_to_es", "bilingual_ru_es":
            targetLang = "es"
        case "es_to_ru", "en_to_ru", "auto_to_ru":
            targetLang = "ru"
        case "off":
            targetLang = "ru"
        default:
            // auto → ru
            targetLang = "ru"
        }
        systemAudioCapture.targetLang = targetLang

        let showOrig = UserDefaults.standard.object(forKey: "KrabEar_LiveSubsShowOriginal") != nil
            ? UserDefaults.standard.bool(forKey: "KrabEar_LiveSubsShowOriginal")
            : true
        liveSubsOverlay.showOriginalAndTranslation = showOrig
        systemAudioCapture.showOriginalAndTranslation = showOrig
    }
}
