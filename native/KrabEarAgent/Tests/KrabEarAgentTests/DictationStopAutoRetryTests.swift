/*
 DictationStopAutoRetryTests — мини-волна 2026-08-11 (авто-дострел
 отложенного stop_recording после восстановления backend).

 Покрытие (спека docs/superpowers/specs/2026-08-11-auto-refire-pending-stop-design.md §5,
 план docs/superpowers/plans/2026-08-11-auto-refire-pending-stop-plan.md §1д):
 1. DictationStopAutoRetryGate.shouldAttempt — базовый positive + 5 негативов
    (по одному на каждый гард) + budget=1 остаётся true.
 2. fullBudget == 2 — осознанная константа спеки, не магия.
 3. Source-контракты (тот же приём резолва пути, что MainErrorsWiringTests /
    MainHealthMonitorWiringTests — decorative-wiring класс):
    a. main+HealthMonitor.swift: ОДНО замыкание setOnHealthyPing содержит И
       noteHealthy(), И attemptPendingDictationStopRecovery() — R4 (слот один,
       повторный вызов setOnHealthyPing затёр бы предыдущего подписчика).
    b. retainDictationStopRecovery в main+HotkeyRecording.swift взводит
       dictationStopAutoRetryArmed = true.
    c. КАЖДОЕ вхождение `recordingStopRecoveryPending = false` в
       main+HotkeyRecording.swift сопровождается (в пределах ±6 строк)
       сбросом armed И восстановлением бюджета — regex/окно по вхождениям,
       не по фиксированным номерам строк (они уезжают).

 Интеграционный сценарий «retain → healthy ping → stopRecording вызван ровно
 один раз» (спека §5.5) сознательно НЕ продублирован здесь отдельным
 test-only харнесом: AgentAppDelegate требует NSApp/полный lifecycle
 (нетестируемо в чистом XCTest, см. доккомент DictationStopAutoRetryGate.swift),
 а синтетический дубль замыкания воспроизвёл бы урок
 reference_dead_test_only_helper_reshape — тестировал бы копию, а не
 реальное решение. Тот же исход покрыт связкой gate-юнитов (реальная
 decision-функция) + source-контрактов (реальный текст wiring) + живого e2e
 после мержа (спека §5, последний абзац).
*/

import XCTest
@testable import KrabEarAgent

// MARK: - 1. Gate unit tests (чистая struct, без AgentAppDelegate)

final class DictationStopAutoRetryGateTests: XCTestCase {

    private func snapshot(
        recoveryPending: Bool = true,
        generationOwner: String? = "dictation",
        isProcessing: Bool = false,
        quickCaptureActive: Bool = false,
        remainingBudget: Int = DictationStopAutoRetryGate.fullBudget
    ) -> DictationStopAutoRetrySnapshot {
        DictationStopAutoRetrySnapshot(
            recoveryPending: recoveryPending,
            generationOwner: generationOwner,
            isProcessing: isProcessing,
            quickCaptureActive: quickCaptureActive,
            remainingBudget: remainingBudget
        )
    }

    // MARK: Positive

    func test_shouldAttempt_allowsWhenAllGuardsPass() {
        XCTAssertTrue(DictationStopAutoRetryGate.shouldAttempt(snapshot()))
    }

    func test_shouldAttempt_allowsWhenBudgetIsOne() {
        XCTAssertTrue(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(remainingBudget: 1)),
            "Последняя оставшаяся попытка бюджета (1) обязана пройти гейт"
        )
    }

    // MARK: Negatives — по одному на каждый гард

    func test_shouldAttempt_blocksWhenRecoveryNotPending() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(recoveryPending: false)),
            "Без recoveryPending авто-дострелу нечего досстреливать"
        )
    }

    func test_shouldAttempt_blocksWhenOwnerIsQuickCapture() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(
                snapshot(generationOwner: "quick_capture")
            ),
            "Quick Capture имеет свой recovery-путь (панель), спека §2.7"
        )
    }

    func test_shouldAttempt_blocksWhenOwnerIsNil() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(generationOwner: nil)),
            "Отсутствующий owner не является диктовкой"
        )
    }

    func test_shouldAttempt_blocksWhenIsProcessing() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(isProcessing: true)),
            "Ручной стоп уже в полёте — авто-попытка не должна входить"
        )
    }

    func test_shouldAttempt_blocksWhenQuickCaptureActive() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(quickCaptureActive: true)),
            "Симметрия с toggle-входом (handleRecordToggleRequest)"
        )
    }

    func test_shouldAttempt_blocksWhenBudgetExhausted() {
        XCTAssertFalse(
            DictationStopAutoRetryGate.shouldAttempt(snapshot(remainingBudget: 0)),
            "Кап против карусели «healthy ping каждые 3с → полный coordinator-цикл»"
        )
    }

    // MARK: 2. fullBudget pin

    func test_fullBudget_isTwo() {
        XCTAssertEqual(
            DictationStopAutoRetryGate.fullBudget, 2,
            "Осознанная константа спеки §2.2/2.5 (не магическое число)"
        )
    }
}

// MARK: - 3. Source contracts

final class DictationStopAutoRetryWiringSourceContractTests: XCTestCase {

    // MARK: 3a. setOnHealthyPing: оба действия в ОДНОМ замыкании (R4)

    func test_onHealthyPing_composesNoteHealthyAndAutoRetryAttempt() throws {
        let src = try Self.source("main+HealthMonitor.swift")
        guard
            let sigRange = src.range(of: "await monitor.setOnHealthyPing {"),
            let closeRange = src.range(
                of: "\n            }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдено замыкание setOnHealthyPing в main+HealthMonitor.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("wedgeGate.noteHealthy()"),
            "setOnHealthyPing обязан перевзводить кап второй ступени (noteHealthy) — " +
            "слот один, эта проводка НЕ должна быть затёрта авто-дострелом"
        )
        XCTAssertTrue(
            block.contains("attemptPendingDictationStopRecovery()"),
            "setOnHealthyPing обязан композировать авто-дострел (спека 2026-08-11, R4)"
        )
    }

    // MARK: 3b. retainDictationStopRecovery взводит armed

    func test_retainDictationStopRecovery_armsAutoRetry() throws {
        let src = try Self.source("main+HotkeyRecording.swift")
        guard
            let sigRange = src.range(of: "private func retainDictationStopRecovery("),
            let closeRange = src.range(
                of: "\n    }\n",
                range: sigRange.upperBound..<src.endIndex
            )
        else {
            return XCTFail("Не найдена retainDictationStopRecovery в main+HotkeyRecording.swift")
        }
        let block = src[sigRange.lowerBound..<closeRange.upperBound]
        XCTAssertTrue(
            block.contains("dictationStopAutoRetryArmed = true"),
            "retainDictationStopRecovery обязана взводить armed — иначе первый " +
            "здоровый ping после провала стопа не попробует авто-дострел"
        )
        XCTAssertTrue(
            block.contains(#"activeGenerationOwner == "dictation""#),
            "Взвод обязан быть ограничен owner == \"dictation\" (спека §2.7 — " +
            "quick capture не в скоупе)"
        )
    }

    // MARK: 3c. КАЖДЫЙ сброс recoveryPending разоружает + восстанавливает бюджет

    func test_everyRecoveryPendingFalseSite_disarmsAndRestoresBudget() throws {
        let src = try Self.source("main+HotkeyRecording.swift")
        let lines = src.components(separatedBy: "\n")
        let resetLineIndices = lines.indices.filter {
            lines[$0].contains("recordingStopRecoveryPending = false")
        }
        XCTAssertFalse(
            resetLineIndices.isEmpty,
            "Ожидался хотя бы один сброс recordingStopRecoveryPending = false " +
            "в main+HotkeyRecording.swift — файл изменился сильнее, чем ожидал тест"
        )

        let window = 6
        for lineIndex in resetLineIndices {
            let lowerBound = max(0, lineIndex - window)
            let upperBound = min(lines.count - 1, lineIndex + window)
            let surrounding = lines[lowerBound...upperBound].joined(separator: "\n")

            XCTAssertTrue(
                surrounding.contains("dictationStopAutoRetryArmed = false"),
                "Строка \(lineIndex + 1) сбрасывает recoveryPending, но не " +
                "разоружает dictationStopAutoRetryArmed в пределах ±\(window) строк"
            )
            XCTAssertTrue(
                surrounding.contains(
                    "dictationStopAutoRetryBudget = DictationStopAutoRetryGate.fullBudget"
                ),
                "Строка \(lineIndex + 1) сбрасывает recoveryPending, но не " +
                "восстанавливает dictationStopAutoRetryBudget в пределах ±\(window) строк"
            )
        }
    }

    /// Резолвит `native/KrabEarAgent/Sources/KrabEarAgent/<name>` от тестового
    /// бандла, с fallback на #file-относительный walk-up — тот же приём, что
    /// MainHealthMonitorWiringTests.mainSwiftURL / MainErrorsWiringTests.mainSwiftURL.
    private static func source(_ name: String) throws -> String {
        let bundleURL = Bundle(for: DictationStopAutoRetryWiringSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #filePath)
        let fallback = fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: fallback, encoding: .utf8)
    }
}
