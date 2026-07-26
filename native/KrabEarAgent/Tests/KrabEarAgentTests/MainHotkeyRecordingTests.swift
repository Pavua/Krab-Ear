/*
 MainHotkeyRecordingTests.swift

 Тесты для логики из main+HotkeyRecording.swift.

 Подход: AgentAppDelegate нельзя создать headless (NSApplicationDelegate lifecycle,
 BackendSupervisor, NSWorkspace и т.д.). Поэтому чистая бизнес-логика вынесена
 в статические хелперы RecordingLogic ниже, зеркалящие inline-логику оригинала.
 Каждый хелпер — это прямое отражение ветки кода из extension AgentAppDelegate.

 Тесты проверяют:
   1. Debounce: повторный toggle в пределах toggleDebounceSec игнорируется
   2. Debounce: по истечении окна toggle проходит
   3. startRecording: effectiveDuckingPercent=100 в mic-режиме
   4. startRecording: effectiveDuckingPercent берётся из настроек в non-mic-режиме
   5. syncRecordingState: детекция mismatch (backend != local)
   6. syncRecordingState: no mismatch (backend == local)
   7. handleRecordToggle: localRecording=true, backend=false → sync-only (не стартует новую запись)
   8. handleRecordToggle: legacy local=false/backend=true → stopRecording
   9. handleRecordToggle: foreign/unmanaged owner → запись не трогается
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Pure logic helpers (mirrors of main+HotkeyRecording.swift inline logic)

/// Зеркало debounce-проверки из handleRecordToggleRequest().
enum RecordingLogic {

    /// Возвращает true если запрос должен быть проигнорирован (debounce).
    static func shouldDebounce(
        now: TimeInterval,
        lastToggleAt: TimeInterval,
        debounceSec: TimeInterval
    ) -> Bool {
        return now - lastToggleAt < debounceSec
    }

    /// Возвращает актуальный процент приглушения звука.
    /// В mic-режиме всегда 100 (полный mute), иначе из настроек.
    static func effectiveDuckingPercent(
        captureSourceMode: String,
        audioDuckingPercent: Int
    ) -> Int {
        if captureSourceMode == "mic" {
            return 100
        }
        return audioDuckingPercent
    }

    /// Обнаруживает десинхрон: возвращает true если состояния не совпадают.
    static func hasMismatch(localRecording: Bool, backendRecording: Bool) -> Bool {
        return backendRecording != localRecording
    }

    /// Описывает, что должно произойти при handleRecordToggleRequest,
    /// когда isProcessing=false и debounce прошёл.
    enum ToggleAction {
        /// Не посылать start/stop, пока IPC-state не подтверждён.
        case refuseUnknown
        /// Не трогать запись другого или неуправляемого режима.
        case refuseForeign
        /// Только синхронизировать состояние (не стартовать новую запись).
        case syncOnly
        /// Вызвать stopRecording для завершения зависшей сессии.
        case stopHanging
        /// Нормальный toggle: stop если wasRecording, start если нет.
        case normalToggle(wasRecording: Bool)
    }

    /// Зеркало ветвления в handleRecordToggleRequest (после debounce-проверки).
    static func decideToggleAction(
        wasRecordingLocally: Bool,
        backendRecording: Bool,
        backendOwner: String? = nil,
        ownerFieldPresent: Bool = false,
        stateVerified: Bool = true
    ) -> ToggleAction {
        if !stateVerified {
            return .refuseUnknown
        }
        if HotkeyRecordingOwnershipPolicy.isForeignRecording(
            isRecording: backendRecording,
            owner: backendOwner,
            ownerFieldPresent: ownerFieldPresent
        ) {
            return .refuseForeign
        }
        // local=true, backend=false → sync-only, не стартуем новую запись
        if wasRecordingLocally && !backendRecording {
            return .syncOnly
        }
        // local=false, backend=true → завершаем зависшую сессию
        if !wasRecordingLocally && backendRecording {
            return .stopHanging
        }
        // Состояния совпадают — обычный toggle
        return .normalToggle(wasRecording: wasRecordingLocally)
    }
}

// MARK: - Tests

final class MainHotkeyRecordingTests: XCTestCase {

    // MARK: Debounce

    func test_debounce_withinWindow_isIgnored() {
        let debounceSec = 0.35
        let lastToggle = 1000.0
        let now = lastToggle + 0.10  // 100 мс < 350 мс → должен игнорироваться

        let ignored = RecordingLogic.shouldDebounce(
            now: now,
            lastToggleAt: lastToggle,
            debounceSec: debounceSec
        )

        XCTAssertTrue(ignored, "Toggle в пределах debounce окна должен игнорироваться")
    }

    func test_debounce_afterWindow_isAllowed() {
        let debounceSec = 0.35
        let lastToggle = 1000.0
        let now = lastToggle + 0.40  // 400 мс > 350 мс → должен пройти

        let ignored = RecordingLogic.shouldDebounce(
            now: now,
            lastToggleAt: lastToggle,
            debounceSec: debounceSec
        )

        XCTAssertFalse(ignored, "Toggle после истечения debounce окна должен проходить")
    }

    // MARK: Effective ducking percent

    func test_duckingPercent_micMode_alwaysFullMute() {
        let percent = RecordingLogic.effectiveDuckingPercent(
            captureSourceMode: "mic",
            audioDuckingPercent: 50
        )

        XCTAssertEqual(percent, 100,
            "В mic-режиме effectiveDuckingPercent всегда должен быть 100")
    }

    func test_duckingPercent_nonMicMode_usesSettings() {
        let percent = RecordingLogic.effectiveDuckingPercent(
            captureSourceMode: "system",
            audioDuckingPercent: 25
        )

        XCTAssertEqual(percent, 25,
            "В не-mic-режиме effectiveDuckingPercent должен брать значение из настроек")
    }

    // MARK: State mismatch detection

    func test_mismatch_detectedWhenStatesDisagree() {
        XCTAssertTrue(
            RecordingLogic.hasMismatch(localRecording: true, backendRecording: false),
            "local=true, backend=false — должен детектить mismatch"
        )
        XCTAssertTrue(
            RecordingLogic.hasMismatch(localRecording: false, backendRecording: true),
            "local=false, backend=true — должен детектить mismatch"
        )
    }

    func test_noMismatch_whenStatesAgree() {
        XCTAssertFalse(
            RecordingLogic.hasMismatch(localRecording: true, backendRecording: true),
            "local=true, backend=true — не должен давать mismatch"
        )
        XCTAssertFalse(
            RecordingLogic.hasMismatch(localRecording: false, backendRecording: false),
            "local=false, backend=false — не должен давать mismatch"
        )
    }

    // MARK: Toggle action routing

    func test_toggleAction_localTrue_backendFalse_syncOnly() {
        // local считал что пишем, но backend уже idle → только синхронизировать
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: true,
            backendRecording: false
        )
        if case .syncOnly = action { /* OK */ } else {
            XCTFail("Ожидался .syncOnly, получили \(action)")
        }
    }

    func test_toggleAction_localFalse_backendTrue_stopHanging() {
        // Старый backend без owner: сохраняем согласованный legacy auto-heal.
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: false,
            backendRecording: true
        )
        if case .stopHanging = action { /* OK */ } else {
            XCTFail("Ожидался .stopHanging, получили \(action)")
        }
    }

    func test_toggleAction_foreignOwner_refusesStop() {
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: true,
            backendRecording: true,
            backendOwner: "meeting",
            ownerFieldPresent: true
        )
        if case .refuseForeign = action { /* ожидаемый безопасный отказ */ } else {
            XCTFail("Ожидался .refuseForeign, получили \(action)")
        }
    }

    func test_toggleAction_presentNullOwner_refusesStop() {
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: false,
            backendRecording: true,
            backendOwner: nil,
            ownerFieldPresent: true
        )
        if case .refuseForeign = action { /* ожидаемый безопасный отказ */ } else {
            XCTFail("Ожидался .refuseForeign, получили \(action)")
        }
    }

    func test_toggleAction_unverifiedState_refusesAnyMutation() {
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: true,
            backendRecording: true,
            backendOwner: nil,
            ownerFieldPresent: false,
            stateVerified: false
        )
        if case .refuseUnknown = action { /* ожидаемый fail-safe отказ */ } else {
            XCTFail("Ожидался .refuseUnknown, получили \(action)")
        }
    }

    func test_toggleAction_normalToggle_notRecording() {
        // Состояния совпадают, не пишем → normalToggle(wasRecording: false)
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: false,
            backendRecording: false
        )
        if case .normalToggle(let wasRecording) = action {
            XCTAssertFalse(wasRecording, "Должен передать wasRecording=false")
        } else {
            XCTFail("Ожидался .normalToggle, получили \(action)")
        }
    }

    func test_toggleAction_normalToggle_recording() {
        // Состояния совпадают, пишем → normalToggle(wasRecording: true)
        let action = RecordingLogic.decideToggleAction(
            wasRecordingLocally: true,
            backendRecording: true
        )
        if case .normalToggle(let wasRecording) = action {
            XCTAssertTrue(wasRecording, "Должен передать wasRecording=true")
        } else {
            XCTFail("Ожидался .normalToggle, получили \(action)")
        }
    }
}
