/*
 main+LiveSubs.swift
 AgentAppDelegate extension: Phase 2B Live субтитры для видео.

 Hotkey: Cmd+Shift+L (keyCode 37 = kVK_ANSI_L)
 Toggle: Settings → "Live субтитры для видео" (UserDefaults KrabEar_LiveSubsEnabled)

 Phase 2B step 1/3 skeleton:
 - Permission check + start/stop через SystemAudioCapture
 - HUD overlay показывает hudStatusString (захваченные секунды + STT placeholder)
 - Actual STT pipeline + 16kHz conversion — следующий PR

 Связи:
 1) main.swift: вызывает setupLiveSubsHotkey() в applicationDidFinishLaunching.
 2) SystemAudioCapture: start()/stop() с permission error handling.
 3) HistoryPanelController+Settings: applyLiveSubsEnabled() — toggle из Settings UI.
*/

import AppKit
import Foundation

extension AgentAppDelegate {

    // MARK: - Setup / Teardown

    /// Регистрирует глобальный Cmd+Shift+L monitor для toggle Live субтитров.
    /// Вызывается в applicationDidFinishLaunching. Только macOS 12.3+.
    func setupLiveSubsHotkey() {
        guard #available(macOS 12.3, *) else {
            logger.info("Live субтитры: macOS < 12.3, ScreenCaptureKit недоступен, hotkey не регистрируем")
            return
        }

        liveSubsHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.handleLiveSubsKeyEvent(event)
            }
        }
        logger.info("Live субтитры hotkey зарегистрирован (Cmd+Shift+L)")
    }

    /// Снимает Cmd+Shift+L monitor и останавливает захват если активен.
    func teardownLiveSubsHotkey() {
        if let monitor = liveSubsHotkeyMonitor {
            NSEvent.removeMonitor(monitor)
            liveSubsHotkeyMonitor = nil
        }

        guard #available(macOS 12.3, *) else { return }
        if systemAudioCapture.isCapturing {
            systemAudioCapture.stop(completion: nil)
        }
    }

    // MARK: - Key handler

    @MainActor
    private func handleLiveSubsKeyEvent(_ event: NSEvent) {
        guard #available(macOS 12.3, *) else { return }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        // Cmd+Shift+L: keyCode 37 = kVK_ANSI_L
        guard flags == [.command, .shift] && event.keyCode == 37 else { return }
        toggleLiveSubs()
    }

    // MARK: - Toggle

    /// Переключает состояние Live субтитров (start ↔ stop).
    /// Вызывается из hotkey handler и из Settings toggle.
    @MainActor
    func toggleLiveSubs() {
        guard #available(macOS 12.3, *) else {
            notify(title: "Krab Ear", body: "Live субтитры требуют macOS 12.3+.")
            return
        }

        if systemAudioCapture.isCapturing {
            stopLiveSubs()
        } else {
            startLiveSubs()
        }
    }

    /// Запускает захват системного аудио.
    @MainActor
    private func startLiveSubs() {
        guard #available(macOS 12.3, *) else { return }

        logger.info("Live субтитры: запрос старта...")
        systemAudioCapture.start { [weak self] error in
            guard let self else { return }
            if let error = error {
                self.logger.error("Live субтитры: ошибка старта: \(error.localizedDescription)")
                self.notify(
                    title: "Krab Ear — Live субтитры",
                    body: error.localizedDescription
                )
                return
            }
            self.logger.info("Live субтитры: захват начат")
            self.notify(
                title: "Krab Ear — Live субтитры",
                body: "Захват аудио запущен. HUD: \(self.systemAudioCapture.hudStatusString)"
            )
            // Обновлять HUD статус каждую секунду пока идёт захват
            self.scheduleLiveSubsHUDUpdate()
        }
    }

    /// Останавливает захват и обновляет HUD.
    @MainActor
    private func stopLiveSubs() {
        guard #available(macOS 12.3, *) else { return }

        logger.info("Live субтитры: остановка...")
        systemAudioCapture.stop { [weak self] error in
            guard let self else { return }
            if let error = error {
                self.logger.error("Live субтитры: ошибка стопа: \(error.localizedDescription)")
            }
            self.notify(
                title: "Krab Ear — Live субтитры",
                body: "Захват аудио остановлен."
            )
        }
    }

    /// Запускает периодическое обновление HUD (каждые 2с) пока идёт захват.
    @MainActor
    private func scheduleLiveSubsHUDUpdate() {
        guard #available(macOS 12.3, *) else { return }
        guard systemAudioCapture.isCapturing else { return }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self, #available(macOS 12.3, *) else { return }
            guard self.systemAudioCapture.isCapturing else { return }
            self.logger.info("Live субтитры HUD: \(self.systemAudioCapture.hudStatusString)")
            self.scheduleLiveSubsHUDUpdate() // продолжаем пока идёт захват
        }
    }

    // MARK: - Settings integration

    /// Применяет состояние toggle из Settings UI ("Live субтитры для видео").
    /// Вызывается из HistoryPanelController+Settings при изменении toggle.
    @MainActor
    func applyLiveSubsEnabled(_ enabled: Bool) {
        UserDefaults.standard.liveSubsEnabled = enabled
        guard #available(macOS 12.3, *) else { return }

        if enabled && !systemAudioCapture.isCapturing {
            startLiveSubs()
        } else if !enabled && systemAudioCapture.isCapturing {
            stopLiveSubs()
        }
        logger.info("Live субтитры: \(enabled ? "включены" : "выключены") из настроек")
    }
}
