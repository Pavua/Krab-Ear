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

    func pasteToFrontmostApp(_ text: String) -> PasteAttemptResult {
        calls.append(.paste(text))
        return PasteAttemptResult(ok: true, reason: "ok")
    }

    func deleteBackward(count: Int) -> PasteAttemptResult {
        calls.append(.deleteBackward(count))
        return PasteAttemptResult(ok: true, reason: "ok")
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
}
