/*
 WakeWordPoller.swift — wake word через IPC-поллинг backend'а.

 Архитектура (spec docs/superpowers/specs/2026-07-05-wake-word-openwakeword-design.md):
 - Микрофоном владеет Python-бэкенд (backend/openwakeword_adapter.py, openWakeWord).
 - Агент шлёт wake_word_start/stop по IPC и раз в 0.75с поллит wake_word_status.
 - Рост last_detection.ts → триггер «Разговор с AI».
 - SSE НЕ используется: прод = два процесса (IPC-бэкенд и REST) с раздельными
   EventBus, событие из service.py до SSE на :5005 не доходит.

 WakeWordDetectionTracker — чистая, тестируемая логика дебаунса (без IPC/таймеров).
 WakeWordPoller — тонкая обвязка: Timer на main + sync IPC на global queue
 (идиом main+RealtimeOverlay.refreshRealtimeOverlay, AGENT-3: без sync IPC на main).
*/

import AppKit
import Foundation

// MARK: - Причины паузы (идемпотентны по причине — Set, не счётчик)

enum WakeWordPauseReason: String, CaseIterable, Sendable {
    case recording      // идёт диктовка — слушатель поймал бы её же
    case conversation   // идёт «Разговор с AI» — микрофон занят разговором
    case privacyMode    // privacy mode — микрофон wake word держать нельзя
}

// MARK: - Чистая логика дебаунса

/// Решает «была ли НОВАЯ детекция» по последовательности значений last_detection.ts.
/// Первый вызов только устанавливает baseline (стейл-детекция прошлой сессии
/// или живого бэкенда при перезапуске агента не триггерит). nil re-arm'ит
/// baseline: после рестарта бэкенда monotonic-отсчёт начинается заново и новый
/// ts может быть меньше старого.
final class WakeWordDetectionTracker {
    private var initialized = false
    private var baselineTs: Double?

    /// true ровно один раз на каждую новую детекцию.
    func shouldTrigger(lastDetectionTs ts: Double?) -> Bool {
        if !initialized {
            initialized = true
            baselineTs = ts
            return false
        }
        guard let ts else {
            baselineTs = nil   // backend сбросил состояние (рестарт/новая сессия)
            return false
        }
        if let base = baselineTs, ts <= base { return false }
        baselineTs = ts
        return true
    }

    func reset() {
        initialized = false
        baselineTs = nil
    }
}
