/*
 main+LiveSubs — интеграция SystemAudioCapture + LiveSubtitlesOverlay в AgentAppDelegate.

 Горячая клавиша Cmd+Shift+L: toggle захвата системного аудио.
 При старте захвата — показывает HUD.
 При остановке — скрывает HUD и отправляет live_subs_stop бэкенду.

 Связи:
 - AgentAppDelegate: хранит systemAudioCapture и liveSubsOverlay
 - main+StatusMenu.swift: регистрирует Cmd+Shift+L пункт меню
*/

import AppKit
import Foundation

// MARK: - AgentAppDelegate + Live Subs

extension AgentAppDelegate {

    // MARK: - Accessors (stored via objc associated objects)

    // UInt8 keys: address is what uniquely identifies the key, not the value
    private static var captureKey: UInt8 = 0
    private static var overlayKey: UInt8 = 0

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

    // MARK: - Toggle

    /// Вызывается из Cmd+Shift+L меню и из Settings toggle.
    func toggleLiveSubsCaptureFromMenu() {
        if systemAudioCapture.isCapturing {
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
        logger.info("Live Subs: захват системного аудио запущен")
        notify(title: "Krab Ear", body: "Live субтитры включены (Cmd+Shift+L для остановки)")
    }

    @objc func onToggleLiveSubs() {
        toggleLiveSubsCaptureFromMenu()
    }

    func stopLiveSubsCapture() {
        systemAudioCapture.stop()
        liveSubsOverlay.hide()
        logger.info("Live Subs: захват остановлен")
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
