import AppKit
import XCTest
@testable import KrabEarAgent

@MainActor
final class PasteTargetActivationWiringTests: XCTestCase {
    /// Heartbeat создаётся внутри реального алгоритма, не до входа в него.
    func testActivationWaitYieldsMainActorBeforePaste() async {
        var frontmost: pid_t = 20
        var completed = false
        var heartbeatRanDuringOperation = false
        var heartbeat: Task<Void, Never>?
        var pasted: [pid_t] = []
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            heartbeat = Task { @MainActor in
                heartbeatRanDuringOperation = !completed
                frontmost = 10
            }
            return true
        })
        // Это проверка yielding, а не latency benchmark. На hosted VM возврат
        // MainActor может занять больше рабочего бюджета активации.
        let activator = PasteTargetActivator(budget: .seconds(5), frontmostPID: { frontmost })
        let result = await activator.perform(target: target) { pid in
            XCTAssertTrue(heartbeatRanDuringOperation, "Paste разрешён только после heartbeat на MainActor")
            pasted.append(pid)
            return PasteAttemptResult(ok: true, reason: "fake_paste")
        }
        completed = true
        await heartbeat?.value
        XCTAssertTrue(heartbeatRanDuringOperation, "Ожидание активации заблокировало MainActor")
        XCTAssertTrue(result.ok)
        XCTAssertEqual(pasted, [10])
    }

    func testUnconfirmedActivationDoesNotInvokePaste() async {
        var pasteCalls = 0
        let activator = PasteTargetActivator(budget: .milliseconds(30), frontmostPID: { 20 })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: { true })
        let result = await activator.perform(target: target) { _ in
            pasteCalls += 1
            return PasteAttemptResult(ok: true, reason: "fake_paste")
        }
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, "target_activation_timeout")
        XCTAssertEqual(pasteCalls, 0, "Таймаут не разрешает вставку в неподтверждённую цель")
    }

    func testActivationRefusalDoesNotInvokePaste() async {
        let activator = PasteTargetActivator(frontmostPID: { 20 })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: { false })
        let result = await activator.perform(target: target) { _ in
            XCTFail("Отказ активации не разрешает вставку")
            return PasteAttemptResult(ok: true, reason: "unexpected_paste")
        }
        XCTAssertEqual(result.reason, "target_activation_failed")
    }

    func testTerminatedTargetDoesNotActivateOrPaste() async {
        let activator = PasteTargetActivator(frontmostPID: { 20 })
        let target = PasteActivationTarget(pid: 10, isAlive: { false }, requestActivation: {
            XCTFail("Закрытое приложение не активируется"); return true
        })
        let result = await activator.perform(target: target) { _ in
            XCTFail("Закрытое приложение не получает текст")
            return PasteAttemptResult(ok: true, reason: "unexpected_paste")
        }
        XCTAssertEqual(result.reason, "target_activation_terminated")
    }

    func testCancellationBeforeActivationHasNoPasteEffects() async {
        let activator = PasteTargetActivator(frontmostPID: { 20 })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            XCTFail("Отменённая операция не активирует target"); return true
        })
        let operation = Task { @MainActor in
            await activator.perform(target: target) { _ in
                XCTFail("Отменённая операция не вставляет текст")
                return PasteAttemptResult(ok: true, reason: "unexpected_paste")
            }
        }
        operation.cancel()
        let result = await operation.value
        XCTAssertEqual(result.reason, "target_activation_cancelled")
    }

    func testCancellationDuringActivationDoesNotPasteAndReleasesOwnership() async {
        let entered = expectation(description: "activation requested")
        var frontmost: pid_t = 20
        let activator = PasteTargetActivator(frontmostPID: { frontmost })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            entered.fulfill(); return true
        })
        let operation = Task { @MainActor in
            await activator.perform(target: target) { _ in
                XCTFail("Отмена во время ожидания не разрешает paste")
                return PasteAttemptResult(ok: true, reason: "unexpected_paste")
            }
        }
        await fulfillment(of: [entered], timeout: 2)
        operation.cancel()
        let cancelled = await operation.value
        XCTAssertEqual(cancelled.reason, "target_activation_cancelled")
        frontmost = 10
        let next = await activator.perform(target: target) { _ in PasteAttemptResult(ok: true, reason: "next") }
        XCTAssertTrue(next.ok, "Отмена должна освобождать только собственное владение")
    }

    func testTargetExitWhileWaitingRefusesEvenIfPIDBecomesFrontmost() async {
        var alive = true
        var frontmost: pid_t = 20
        let activator = PasteTargetActivator(frontmostPID: { frontmost })
        let target = PasteActivationTarget(pid: 10, isAlive: { alive }, requestActivation: {
            Task { @MainActor in alive = false; frontmost = 10 }
            return true
        })
        let result = await activator.perform(target: target) { _ in
            XCTFail("PID без живого исходного приложения не даёт права вставки")
            return PasteAttemptResult(ok: true, reason: "unexpected_paste")
        }
        XCTAssertEqual(result.reason, "target_activation_terminated")
    }

    func testConcurrentRequestCannotActivateAnotherTarget() async {
        let entered = expectation(description: "first activation requested")
        var frontmost: pid_t = 30
        var pasted: [pid_t] = []
        let activator = PasteTargetActivator(frontmostPID: { frontmost })
        let first = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            entered.fulfill(); return true
        })
        let operation = Task { @MainActor in
            await activator.perform(target: first) { pid in
                pasted.append(pid); return PasteAttemptResult(ok: true, reason: "first")
            }
        }
        await fulfillment(of: [entered], timeout: 2)
        let second = PasteActivationTarget(pid: 20, isAlive: { true }, requestActivation: {
            XCTFail("Конкурентный запрос не должен переключать приложение"); return true
        })
        let refused = await activator.perform(target: second) { _ in
            XCTFail("Конкурентный запрос не отправляет текст")
            return PasteAttemptResult(ok: true, reason: "unexpected_paste")
        }
        XCTAssertEqual(refused.reason, "target_activation_busy")
        frontmost = 10
        let completed = await operation.value
        XCTAssertTrue(completed.ok)
        XCTAssertEqual(pasted, [10])
    }

    func testAlreadyActiveTargetPreservesPasteResultWithoutActivation() async {
        let activator = PasteTargetActivator(frontmostPID: { 10 })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            XCTFail("Уже активный адресат не нуждается в активации"); return false
        })
        let result = await activator.perform(target: target) { pid in
            XCTAssertEqual(pid, 10)
            return PasteAttemptResult(ok: false, reason: "concealed_clipboard_skipped")
        }
        XCTAssertEqual(result.reason, "concealed_clipboard_skipped")
        XCTAssertFalse(result.ok)
    }

    func testExpiredBudgetDoesNotActivateOrPaste() async {
        let activator = PasteTargetActivator(budget: .zero, frontmostPID: { 10 })
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: {
            XCTFail("Истёкший бюджет не активирует приложение"); return true
        })
        let result = await activator.perform(target: target) { _ in
            XCTFail("Даже уже активная цель не разрешает истёкшую операцию")
            return PasteAttemptResult(ok: true, reason: "unexpected_paste")
        }
        XCTAssertEqual(result.reason, "target_activation_timeout")
    }

    /// Настоящий delegate → activator → PasteService. Concealed guard реального
    /// сервиса гарантирует выход до AX/keyboard; буфер и defaults только приватные.
    func testActualDelegateReachesActualPasteServiceAfterActivation() async {
        await checkDelegateWiring(frontmostPID: 10, expectedReason: "concealed_clipboard_skipped")
    }

    func testActualDelegateDoesNotReachPasteServiceAfterActivationTimeout() async {
        await checkDelegateWiring(frontmostPID: 20, expectedReason: "target_activation_timeout")
    }

    private func checkDelegateWiring(frontmostPID: pid_t, expectedReason: String) async {
        _ = NSApplication.shared
        let suite = "PasteTargetActivationWiring.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        let pasteboard = NSPasteboard(name: .init(suite))
        defer {
            pasteboard.clearContents()
            pasteboard.releaseGlobally()
            defaults.removePersistentDomain(forName: suite)
        }
        let concealed = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
        pasteboard.declareTypes([.string, concealed], owner: nil)
        pasteboard.setString("synthetic concealed fixture", forType: .string)
        let originalChangeCount = pasteboard.changeCount
        let service = PasteService(pasteboard: pasteboard, defaults: defaults)
        let activator = PasteTargetActivator(budget: .milliseconds(30), frontmostPID: { frontmostPID })
        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]), userDefaults: defaults,
            pasteService: service, pasteTargetActivator: activator
        )
        let target = PasteActivationTarget(pid: 10, isAlive: { true }, requestActivation: { true })
        let result = await delegate.pasteToActivatedTarget("synthetic paste", target: target)
        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.reason, expectedReason)
        XCTAssertEqual(pasteboard.changeCount, originalChangeCount)
        XCTAssertEqual(pasteboard.string(forType: .string), "synthetic concealed fixture")
    }

    func testBothDelegateEntryPointsCaptureTargetBeforeTaskAndUseActivationGate() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: root.appendingPathComponent("Sources/KrabEarAgent/main+PasteHandling.swift"), encoding: .utf8)
        for (start, end) in [("    func performAutoPaste(", "    func isDuplicateAutopasteCandidate("),
                             ("    func pasteSnapshotText(", "    func handlePasteFailure(")] {
            let lower = try XCTUnwrap(source.range(of: start)).lowerBound
            let upper = try XCTUnwrap(source.range(of: end)).lowerBound
            let body = String(source[lower..<upper])
            let capture = try XCTUnwrap(body.range(of: "let activationTarget = pasteActivationTarget(targetApp)"))
            let task = try XCTUnwrap(body.range(of: "Task { @MainActor"))
            XCTAssertLessThan(capture.lowerBound, task.lowerBound)
            let reservation = try XCTUnwrap(body.range(of: "let request = pasteService.reserveInput(target: activationTarget.pid)"))
            XCTAssertLessThan(reservation.lowerBound, task.lowerBound)
            XCTAssertTrue(body.contains("await pasteToActivatedTarget(textToInsert, target: activationTarget, request: request)"))
            XCTAssertFalse(body.contains("pasteService.pasteToFrontmostApp("))
            XCTAssertFalse(body.contains("pasteService.putToClipboard("))
        }
    }
}
