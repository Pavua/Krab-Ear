/*
 LiveSubsOverlayWatchdogGate — решение «пора ли закрыть повисший оверлей
 live-субтитров» (спека 2026-08-12-live-subs-backpressure-design.md §4, F5).

 Класс бага "sticky state without an exit" (кодекс проекта): оверлей
 LiveSubtitlesOverlay показывается стартом захвата и раньше скрывался ТОЛЬКО
 явным вызовом AgentAppDelegate.stopLiveSubsCapture(). Если захват умирал
 молча (бэкенд завис/отверг соединение, permission-отказ ScreenCaptureKit) —
 окно оставалось always-on-top и перетаскиваемым навсегда (живой инцидент
 владельца 2026-08-12 00:41: 2x-темп видео захлебнул бэкенд, оверлей повис
 пустым, пришлось убирать вручную).

 Чистая struct без Timer/Date: main+LiveSubs.swift считает elapsed сам и
 передаёт число — тот же приём, что WedgedEscalationTracker.shouldEscalate
 (now: TimeInterval) в WakeWordPoller.swift. Выделена в отдельный тип по той
 же причине, что DictationStopAutoRetryGate — гарды тестируются юнитами без
 конструирования AgentAppDelegate (NSApp/lifecycle нетестируемы в чистом
 XCTest).

 🔴 Гейт по isCapturing, а НЕ по «давно не было результатов»: легитимная
 тишина в видео (пауза, музыка, молчание диктора) не должна закрывать
 субтитры — так что источник сигнала для этого гейта — ТОЛЬКО жизнь самого
 захвата, никогда не активность распознавания.
*/

import Foundation

enum LiveSubsOverlayWatchdogGate {
    /// Интервал тика watchdog-таймера. Таймер живёт только пока оверлей
    /// виден (main+LiveSubs.swift останавливает его вместе с оверлеем) —
    /// не крутится вечно.
    static let tickIntervalSec: TimeInterval = 5.0

    /// Сколько оверлей может провисеть с isCapturing == false, прежде чем
    /// закроется сам. Достаточно, чтобы пережить нормальное окно старта/
    /// рестарта захвата: SystemAudioCapture.start() асинхронный, isCapturing
    /// становится true уже ПОСЛЕ show() — короткий grace здесь не должен
    /// путать обычный запуск со смертью захвата.
    static let graceSec: TimeInterval = 10.0

    /// - Parameters:
    ///   - isOverlayVisible: LiveSubtitlesOverlay.isVisible
    ///   - isCapturing: SystemAudioCapture.isCapturing
    ///   - secondsSinceCapturingWasTrue: сколько секунд прошло с последнего
    ///     подтверждённого isCapturing == true (вызывающая сторона обновляет
    ///     эту отметку на каждом тике, где видит isCapturing == true).
    static func shouldHide(
        isOverlayVisible: Bool,
        isCapturing: Bool,
        secondsSinceCapturingWasTrue: TimeInterval
    ) -> Bool {
        isOverlayVisible && !isCapturing && secondsSinceCapturingWasTrue >= graceSec
    }
}
