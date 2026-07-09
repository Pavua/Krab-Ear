/*
 StreamingPasteControllerTests — тесты debounce-укрупнения чанков и revision-коррекции
 в StreamingPasteController (см. StreamingPasteController.swift).

 Покрытие:
 1. Debounce НЕ вставляет чанк раньше срока (несколько partial подряд без задержки → 1 вставка).
 2. Debounce ВСТАВЛЯЕТ накопленный чанк, когда интервал истёк (fake clock, без реального sleep).
 3. Знак завершения предложения форсирует вставку немедленно, игнорируя debounce.
 4. Ревизия (final/partial переосмысливает уже вставленный диапазон) вызывает РЕАЛЬНУЮ
    замену — deleteBackward + (при наличии) новая вставка, а не silent skip.
 5. didStreamThisRecording корректно отражает факт вставки/отката.
 6. (review 2026-07-09, Critical #2) state (committedText/latestStable) НЕ продвигается
    оптимистично, когда реальная paste/delete операция вернула ok == false.
 7. (review 2026-07-09, Critical #1, source-contract) handleTranscriptionResult
    (main+PasteHandling.swift) реально вызывает streamingPasteController?.handleFinal(...)
    с авторитетным текстом из IPC-ответа — SSE realtime.final_transcript путь структурно
    недостижим в реальном recording-stop flow (SSE закрывается ДО того как backend успевает
    его эмиттировать внутри stop_recording).
 8. (review 2026-07-09, Important) провалившаяся вставка/откат НЕ ретраится на каждое
    следующее partial-событие — backoff (lastFailureAt, отдельно от lastFlushAt) ограничивает
    retry раз в debounceIntervalSec, и в maybeFlush, и в performRevision.
 9. (review 2026-07-09, Important #2) тот же backoff НЕ должен глушить единственный вызов
    ревизии из handleFinal — performRevision(bypassBackoff: true) там игнорирует
    lastFailureAt, даже если недавний revision-delete на handlePartial провалился в том же
    debounce-окне.

 Паттерн: FakeStreamingPasteTarget — protocol-based test double (StreamingPasteTarget),
 записывает вызовы без реальных keystroke side-effects (тот же паттерн, что
 MockToastPanelFactory/SpyActionInvoker в ErrorToastViewTests.swift). Fake clock через
 `controller.now` (инжектируемый closure) — тесты детерминированы, без Thread.sleep.
 handlePartial/handleFinal умышленно internal (не private) в StreamingPasteController —
 тестовый seam, вызываются здесь напрямую вместо гонки реального SSE-потока.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - FakeStreamingPasteTarget

/// НЕ @MainActor — StreamingPasteTarget сам по себе nonisolated (см. комментарий в
/// StreamingPasteController.swift), а FakeStreamingPasteTarget используется синхронно
/// изнутри @MainActor тестового класса, так что дополнительная изоляция не нужна.
final class FakeStreamingPasteTarget: StreamingPasteTarget {
    enum Call: Equatable {
        case paste(String)
        case deleteBackward(Int)
    }

    private(set) var calls: [Call] = []

    /// Тесты выставляют перед конкретным вызовом, чтобы симулировать провал paste/delete
    /// (Critical #2 review: PasteAttemptResult.ok == false не должен продвигать state).
    var pasteShouldFail = false
    var deleteShouldFail = false
    var failureReason = "simulated_failure"

    func pasteToFrontmostApp(_ text: String) -> PasteAttemptResult {
        calls.append(.paste(text))
        return pasteShouldFail
            ? PasteAttemptResult(ok: false, reason: failureReason)
            : PasteAttemptResult(ok: true, reason: "ok")
    }

    func deleteBackward(count: Int) -> PasteAttemptResult {
        calls.append(.deleteBackward(count))
        return deleteShouldFail
            ? PasteAttemptResult(ok: false, reason: failureReason)
            : PasteAttemptResult(ok: true, reason: "ok")
    }

    var pasteCalls: [String] {
        calls.compactMap { if case let .paste(text) = $0 { return text } else { return nil } }
    }

    var deleteCalls: [Int] {
        calls.compactMap { if case let .deleteBackward(count) = $0 { return count } else { return nil } }
    }
}

// MARK: - StreamingPasteControllerTests

@MainActor
final class StreamingPasteControllerTests: XCTestCase {

    var target: FakeStreamingPasteTarget!
    var controller: StreamingPasteController!
    var fakeNow: Date!

    override func setUp() {
        super.setUp()
        target = FakeStreamingPasteTarget()
        controller = StreamingPasteController(pasteService: target)
        controller.isEnabled = true
        controller.debounceIntervalSec = 0.3
        fakeNow = Date(timeIntervalSince1970: 1_000_000)
        controller.now = { [weak self] in self!.fakeNow }
    }

    override func tearDown() {
        target = nil
        controller = nil
        super.tearDown()
    }

    /// Доводит committedText контроллера до "Привет мир " (оба слова РЕАЛЬНО вставлены,
    /// не просто буферизованы) — общий baseline для revision-тестов. Требует продвижения
    /// fake-часов, иначе второе слово останется в debounce-буфере и ревизия будет тестировать
    /// не то (см. урок из первой редакции этого файла: без этого шага "мир" никогда не
    /// коммитится, и revision-детект на нём не срабатывает).
    private func commitPrivetMir() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls, ["Привет "], "предварительное условие baseline нарушено")
        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        controller.handlePartial("Привет мир как ")
        XCTAssertEqual(target.pasteCalls, ["Привет ", "мир "], "предварительное условие baseline нарушено")
    }

    // MARK: - (a) Debounce does NOT insert too early

    func testDebounceDoesNotInsertBeforeIntervalElapsed() {
        // Первый partial: ничего не коммитится (нет предыдущего partial для LCP).
        controller.handlePartial("Привет ")
        XCTAssertEqual(target.pasteCalls.count, 0)

        // Второй partial подтверждает "Привет " как стабильный префикс → первая вставка
        // происходит немедленно (lastFlushAt ещё nil — не ждём debounce для самого первого чанка).
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls, ["Привет "])

        // Третий partial приходит СРАЗУ (fakeNow не продвинут) — новое слово "мир " готово,
        // но debounceIntervalSec ещё не истёк и знака завершения предложения нет.
        controller.handlePartial("Привет мир как ")
        XCTAssertEqual(target.pasteCalls.count, 1, "чанк не должен вставляться раньше debounce-интервала")
    }

    // MARK: - (b) Debounce DOES insert once the condition is reached

    func testDebounceInsertsOnceIntervalElapsed() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls, ["Привет "])

        controller.handlePartial("Привет мир как ")
        XCTAssertEqual(target.pasteCalls.count, 1, "предварительное условие: до истечения интервала вставки нет")

        // Продвигаем fake clock за пределы debounceIntervalSec.
        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)

        controller.handlePartial("Привет мир как дела ")
        XCTAssertEqual(target.pasteCalls.count, 2, "после истечения debounce-интервала накопленный хвост должен вставиться")
        XCTAssertEqual(target.pasteCalls.last, "мир как ")
    }

    // MARK: - (c) Sentence-ending punctuation forces an immediate flush

    func testSentenceEndingPunctuationForcesImmediateFlush() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир. ")
        XCTAssertEqual(target.pasteCalls, ["Привет "])

        // Без продвижения часов, но хвост "мир. " заканчивается знаком завершения предложения.
        controller.handlePartial("Привет мир. Как дела ")
        XCTAssertEqual(target.pasteCalls.count, 2, "знак завершения предложения должен форсировать вставку невзирая на debounce")
        XCTAssertEqual(target.pasteCalls.last, "мир. ")
    }

    // MARK: - (d) Revision triggers a real replacement, not a silent skip

    func testRevisionOnFinalTriggersRealReplacement() {
        commitPrivetMir()
        XCTAssertTrue(target.deleteCalls.isEmpty)

        // Финальный транскрипт переосмысливает уже вставленное "Привет мир " → "Привет там сегодня".
        controller.handleFinal("Привет там сегодня")

        // Ожидаем РЕАЛЬНЫЙ откат разошедшегося хвоста ("мир " = 4 символа) через deleteBackward,
        // а НЕ silent skip (старое поведение — просто warn-лог и ничего на экране не менялось).
        XCTAssertEqual(target.deleteCalls, [4], "ревизия должна откатить ровно разошедшийся хвост")
        XCTAssertEqual(target.pasteCalls.last, "там сегодня", "после отката должен вставиться исправленный хвост")
    }

    func testRevisionOnPartialTriggersDeleteWhenStableShrinksBelowCommitted() {
        commitPrivetMir()

        // Новый partial расходится с committedText уже на втором слове ("мир" → "там") —
        // LCP с предыдущим raw partial обрывается на "Привет ", что короче committedText
        // ("Привет мир ") → должна сработать ревизия (не silent skip).
        controller.handlePartial("Привет там ")

        XCTAssertEqual(target.deleteCalls, [4], "должен откатиться ровно расходящийся хвост committedText")
    }

    // MARK: - (e) didStreamThisRecording bookkeeping

    func testDidStreamThisRecordingReflectsActivity() {
        XCTAssertFalse(controller.didStreamThisRecording)

        controller.handlePartial("Привет ")
        XCTAssertFalse(controller.didStreamThisRecording, "первый partial ничего не коммитит")

        controller.handlePartial("Привет мир ")
        XCTAssertTrue(controller.didStreamThisRecording, "первая реальная вставка должна выставить флаг")
    }

    func testDidStreamThisRecordingTrueAfterRevisionOnly() {
        // Ревизия (delete без вставки нового текста) тоже должна считаться "стримингом",
        // иначе main+PasteHandling ошибочно продублирует финальную полную вставку поверх отката.
        commitPrivetMir()
        controller.resetAfterFinalPaste()
        XCTAssertFalse(controller.didStreamThisRecording)

        // Final короче committedText ("Привет мир " → "Привет мир", без хвостового пробела) —
        // чистый delete-only revision, ничего нового не вставляется.
        controller.handleFinal("Привет мир")
        XCTAssertTrue(controller.didStreamThisRecording)
        XCTAssertEqual(target.deleteCalls.last, 1)
    }

    // MARK: - No premature flush when there is no new stable tail

    func testNoFlushWhenLatestStableEqualsCommittedText() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls.count, 1)

        // lastPartial сейчас "Привет мир ". Присылаем partial КОРОЧЕ committedText по сырому
        // тексту ("Привет "), но который по-прежнему является префиксом committedText ("Привет ")
        // — LCP с lastPartial даёт ровно committedText, latestStable.count == committedText.count,
        // новых символов для вставки нет → maybeFlush обязан выйти по первому guard.
        controller.handlePartial("Привет ")
        XCTAssertEqual(target.pasteCalls.count, 1, "не должно быть новой вставки, если стабильный хвост не вырос")
        XCTAssertTrue(target.deleteCalls.isEmpty, "не должно быть и отката — это не ревизия, а просто отсутствие роста")
    }

    // MARK: - (f) Critical #2 review (2026-07-09): state does NOT advance on paste/delete failure

    /// Провал вставки в maybeFlush НЕ должен продвигать committedText. Доказываем это
    /// косвенно (committedText приватен): после провала следующая УСПЕШНАЯ попытка обязана
    /// содержать ПОЛНЫЙ непроглоченный хвост (включая слово из провалившейся попытки), а не
    /// только новый прирост — если бы committedText продвинулся оптимистично при провале,
    /// retry содержал бы только "как ", а не "мир как ".
    func testMaybeFlushDoesNotAdvanceStateOnPasteFailure() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls, ["Привет "], "baseline: первый чанк вставлен успешно")

        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        target.pasteShouldFail = true
        controller.handlePartial("Привет мир как ")
        // Попытка ДОЛЖНА была произойти (записана), но провалиться.
        XCTAssertEqual(target.pasteCalls, ["Привет ", "мир "], "попытка вставки произошла, хоть и провалилась")

        target.pasteShouldFail = false
        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        controller.handlePartial("Привет мир как дела ")
        // Если бы committedText продвинулся при провале, здесь вставился бы только "дела ".
        // Раз committedText остался "Привет " — retry содержит ПОЛНЫЙ хвост "мир как ".
        XCTAssertEqual(
            target.pasteCalls.last, "мир как ",
            "после провала committedText НЕ должен был продвинуться — retry обязан содержать весь непроглоченный хвост"
        )
    }

    /// Провал deleteBackward в ревизии НЕ должен приводить к попытке вставки исправленного
    /// хвоста — экран, скорее всего, не тронут, и продолжать "вслепую" (вставлять новый текст
    /// поверх неизвестного состояния) опасно.
    func testRevisionDeleteFailureDoesNotAttemptInsertOrAdvanceActivity() {
        commitPrivetMir()
        controller.resetAfterFinalPaste()
        XCTAssertFalse(controller.didStreamThisRecording)
        let pasteCallsBeforeRevision = target.pasteCalls

        target.deleteShouldFail = true
        controller.handleFinal("Привет там сегодня")

        XCTAssertEqual(target.deleteCalls, [4], "откат был затребован (хоть и провалился)")
        XCTAssertEqual(
            target.pasteCalls, pasteCallsBeforeRevision,
            "insert НЕ должен был случиться — delete провалился, дальше в ревизии не идём"
        )
        XCTAssertFalse(
            controller.didStreamThisRecording,
            "провалившаяся ревизия (delete failed) НЕ должна выставлять флаг активности"
        )
    }

    /// Delete в ревизии проходит успешно, но последующая вставка исправленного хвоста
    /// проваливается. Delete — это уже реальное изменение экрана, поэтому попытка вставки
    /// корректно происходит (не блокируется чем-то посторонним) и активность фиксируется.
    func testRevisionInsertFailureAfterSuccessfulDeleteStillAttemptsInsertAndMarksActivity() {
        commitPrivetMir()
        controller.resetAfterFinalPaste()

        target.pasteShouldFail = true
        controller.handleFinal("Привет там сегодня")

        XCTAssertEqual(target.deleteCalls, [4], "откат должен был реально пройти (deleteShouldFail не выставлен)")
        XCTAssertEqual(
            target.pasteCalls.last, "там сегодня",
            "вставка исправленного хвоста должна была быть ПОПЫТАНА после успешного отката"
        )
        XCTAssertTrue(
            controller.didStreamThisRecording,
            "успешный delete — это уже реальное изменение экрана, должно считаться активностью"
        )
    }

    /// Провал revision-delete на handlePartial, СРАЗУ (в пределах debounce-окна) за которым
    /// следует handleFinal с другим текстом — финальная ревизия ВСЁ РАВНО должна попытаться
    /// выполниться, а не быть заглушена backoff-таймером `lastFailureAt`. handleFinal — это
    /// единственный вызов ревизии за сессию (не storm) и последняя возможность поправить экран
    /// перед resetSessionState(); backoff тут не нужен и вреден (review Important, 2026-07-09,
    /// найдено в ревизии предыдущего фикса — точка ~ строка "performRevision" в handleFinal).
    func testRevisionOnHandleFinalBypassesBackoffFromEarlierFailedPartialRevision() {
        commitPrivetMir() // committedText = "Привет мир " (11)

        target.deleteShouldFail = true
        controller.handlePartial("Привет там ")
        XCTAssertEqual(target.deleteCalls, [4], "первая попытка отката (через handlePartial) произошла и провалилась")

        // handleFinal приходит СРАЗУ, БЕЗ продвижения fakeNow — в пределах того же
        // debounceIntervalSec-окна, что и провал выше. Delete на этот раз проходит успешно.
        target.deleteShouldFail = false
        controller.handleFinal("Привет иначе")

        XCTAssertEqual(
            target.deleteCalls, [4, 4],
            "ревизия в handleFinal НЕ должна быть заглушена backoff'ом от недавнего провала на partial"
        )
        XCTAssertEqual(
            target.pasteCalls.last, "иначе",
            "после успешного отката в handleFinal исправленный хвост должен быть вставлен"
        )
        XCTAssertTrue(controller.didStreamThisRecording)
    }

    // MARK: - (g) Important review (2026-07-09): failed paste/delete backs off, doesn't retry-storm

    /// Провал вставки в maybeFlush НЕ должен ретраиться на КАЖДОЕ следующее partial-событие —
    /// только после того как пройдёт debounceIntervalSec с МОМЕНТА ПРОВАЛА (lastFailureAt),
    /// а не безусловно (как было бы, если бы lastFlushAt просто оставался "старым" и
    /// elapsedEnough был бы навсегда true). Без этого backoff'а провал вставки (напр.
    /// Accessibility не выдан, фокус временно на панели самого Krab Ear) превращается в
    /// шторм синхронных main-thread paste-попыток до конца диктовки.
    func testFailedPasteDoesNotRetryBeforeBackoffIntervalElapsed() {
        controller.handlePartial("Привет ")
        controller.handlePartial("Привет мир ")
        XCTAssertEqual(target.pasteCalls, ["Привет "], "baseline: первый чанк вставлен успешно")

        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        target.pasteShouldFail = true
        controller.handlePartial("Привет мир как ")
        XCTAssertEqual(target.pasteCalls, ["Привет ", "мир "], "первая попытка произошла (и провалилась)")

        // Следующее partial-событие приходит СРАЗУ (fakeNow НЕ продвинут) — до фикса
        // lastFlushAt остался бы "старым" (из первого успешного флаша), elapsedEnough был бы
        // навсегда true, и retry произошёл бы немедленно. С backoff'ом retry должен быть
        // заблокирован до истечения debounceIntervalSec с момента ПРОВАЛА.
        controller.handlePartial("Привет мир как дела ")
        XCTAssertEqual(
            target.pasteCalls.count, 2,
            "провалившаяся попытка НЕ должна ретраиться раньше debounce-интервала от момента провала"
        )

        target.pasteShouldFail = false
        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        controller.handlePartial("Привет мир как дела сегодня ")
        XCTAssertEqual(
            target.pasteCalls.count, 3,
            "после истечения backoff-интервала retry обязан пройти"
        )
        XCTAssertEqual(target.pasteCalls.last, "мир как дела ")
    }

    /// Та же характеристика в performRevision (review отдельно отметил: delete-failure ветка
    /// тоже не продвигала lastFlushAt, а сам performRevision вызывается БЕЗ debounce-гейта на
    /// каждое handlePartial пока условие ревизии держится — без backoff'а провалившийся delete
    /// ретраился бы синхронно на каждое последующее partial-событие.
    func testFailedRevisionDeleteDoesNotRetryBeforeBackoffIntervalElapsed() {
        commitPrivetMir() // committedText = "Привет мир " (11)

        target.deleteShouldFail = true
        controller.handlePartial("Привет там ")
        XCTAssertEqual(target.deleteCalls, [4], "первая попытка отката произошла (и провалилась)")

        // Немедленно следующее partial с тем же условием ревизии (fakeNow не продвинут) —
        // backoff должен заблокировать retry до того, как performRevision вообще посчитает diff.
        controller.handlePartial("Привет там ")
        XCTAssertEqual(
            target.deleteCalls, [4],
            "повторная попытка отката НЕ должна была произойти раньше backoff-интервала"
        )

        target.deleteShouldFail = false
        fakeNow = fakeNow.addingTimeInterval(controller.debounceIntervalSec + 0.05)
        controller.handlePartial("Привет там ")
        XCTAssertEqual(target.deleteCalls, [4, 4], "после истечения backoff-интервала retry должен пройти")
    }
}

// MARK: - Source contract (Critical #1 review, 2026-07-09) — handleTranscriptionResult
// actually calls streamingPasteController?.handleFinal(...) with the authoritative IPC text.
//
// Root cause found in review of commit 29461a53: stopRecording() (main+HotkeyRecording.swift)
// calls stopRealtimeOverlayPolling() -> recordingDidStop() -> stopSSE() SYNCHRONOUSLY and
// BEFORE the IPC call to "stop_recording" is even sent. The backend only emits
// realtime.final_transcript from INSIDE handle_stop_recording's processing of that same RPC —
// i.e. strictly AFTER Swift already closed the SSE connection. So StreamingPasteController's
// SSE case "realtime.final_transcript" handler is structurally unreachable in the real
// recording-stop flow: the tail accumulated since the last debounce/sentence-boundary flush
// would be silently lost, and since didStreamThisRecording was already true from earlier
// chunks, performAutoPaste's fallback full-paste is skipped too — no safety net.
//
// Same "test-validates-the-hole" class of bug as setupErrorBus/setupHealthMonitor
// (see MainErrorsWiringTests.swift / MainHealthMonitorWiringTests.swift): unit tests that
// only exercise StreamingPasteController.handleFinal() in isolation (see tests above) stay
// green whether or not it's ever actually CALLED from the real transcription-result flow.
final class StreamingPasteFinalWiringSourceContractTests: XCTestCase {

    func test_handleTranscriptionResult_calls_streamingPasteController_handleFinal_with_authoritative_text() throws {
        let src = try String(contentsOf: Self.pasteHandlingSwiftURL, encoding: .utf8)
        XCTAssertTrue(
            src.contains("streamingPasteController?.handleFinal(cleanText)"),
            "handleTranscriptionResult() must call streamingPasteController?.handleFinal(cleanText) " +
            "with the authoritative IPC text — SSE realtime.final_transcript is structurally " +
            "unreachable in the real stopRecording() flow (SSE closes before backend emits it)."
        )
    }

    func test_handleFinal_call_is_gated_on_streamingPasteEnabled() throws {
        let src = try String(contentsOf: Self.pasteHandlingSwiftURL, encoding: .utf8)
        // Без гейта handleFinal("") никогда не вызовется вхолостую, а без гейта на
        // streamingPasteEnabled — вставит ВЕСЬ текст напрямую (committedText начинается
        // пустым), задваивая обычную (нестриминговую) полную вставку ниже по цепочке.
        XCTAssertTrue(
            src.contains("if settings.streamingPasteEnabled {\n            streamingPasteController?.handleFinal(cleanText)"),
            "Вызов handleFinal должен быть гейтнут settings.streamingPasteEnabled — иначе при " +
            "выключенном стриминге handleFinal вставит текст напрямую (committedText пуст) и " +
            "задвоит обычную полную вставку в performAutoPaste."
        )
    }

    /// Resolves native/KrabEarAgent/Sources/KrabEarAgent/main+PasteHandling.swift from the
    /// test bundle, falling back to a #file-relative walk-up (same pattern as
    /// MainHealthMonitorWiringTests.mainSwiftURL / SFSymbolVerificationTests).
    private static var pasteHandlingSwiftURL: URL {
        let bundleURL = Bundle(for: StreamingPasteFinalWiringSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/main+PasteHandling.swift")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .appendingPathComponent("Sources/KrabEarAgent/main+PasteHandling.swift")
    }
}
