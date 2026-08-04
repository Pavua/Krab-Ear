/*
 PasteTargetPolicy.swift
 Выбор приложения-получателя для автовставки диктовки.

 Живой инцидент 2026-08-03: диктовка длиной 39 с финализировалась 38 с; за это
 окно владелец переключился из Claude Desktop в Safari, и текст ушёл в Safari —
 `resolvePreferredPasteTargetApp()` ставит frontmost ПЕРВЫМ, а запомненную на
 старте записи цель лишь третьим запасным путём. Второй запасной путь,
 `lastExternalApp`, тоже не спасал: `handleWorkspaceActivatedApp` перезаписывает
 его на каждую активацию внешнего приложения, так что к моменту вставки он
 указывал на тот же Safari. Единственным носителем верного адресата оставался
 `recordingTargetApp`, и он проигрывал по приоритету.

 Решение владельца: диктовка приходит туда, где НАЧАЛАСЬ. Запомненная цель
 выигрывает всегда — это предсказуемая ментальная модель («куда начал — туда и
 придёт»), в отличие от выбора по фокусу, где адресат зависит от того, оказалось
 ли случайно под рукой чужое текстовое поле.

 Связи модуля:
 1) main+PasteHandling.swift: `resolveDictationPasteTargetApp(captured:)` отображает выбор
    на конкретные `NSRunningApplication`.
 2) DictationPasteTargetTests.swift: чистая логика + source-контракт проводки.

 Политика намеренно свободна от AppKit и работает над булевыми фактами: объекты
 `NSRunningApplication` невозможно сконструировать в unit-тесте, а решение об
 адресате слишком дорого ошибиться, чтобы оставлять его непокрытым.
*/

import Foundation

/// Кто получит вставку. Разбор, а не сам объект: сопоставление с конкретным
/// `NSRunningApplication` живёт в делегате, где эти объекты доступны.
enum PasteTargetChoice: Equatable {
    /// Приложение, запомненное на старте записи (`recordingTargetApp`).
    case captured
    /// Текущее активное приложение, если оно внешнее по отношению к агенту.
    case frontmost
    /// Последнее внешнее приложение из workspace-нотификаций.
    case lastExternal
    /// Кандидатов нет — честный отказ вместо вставки вслепую.
    case none
}

enum DictationPasteTargetPolicy {

    /// Выбирает адресата автовставки диктовки.
    ///
    /// - Parameters:
    ///   - capturedIsAlive: цель, запомненная на старте записи, ещё существует.
    ///   - frontmostIsExternal: активное приложение есть и это не сам агент.
    ///   - lastExternalIsAlive: последнее внешнее приложение ещё существует.
    ///
    /// Порядок отражает убывающую уверенность в намерении: запомненная цель —
    /// то, куда пользователь СМОТРЕЛ, начиная диктовать; остальные два уровня
    /// нужны лишь чтобы текст не потерялся, когда её уже нет.
    static func choose(
        capturedIsAlive: Bool,
        frontmostIsExternal: Bool,
        lastExternalIsAlive: Bool
    ) -> PasteTargetChoice {
        if capturedIsAlive { return .captured }
        if frontmostIsExternal { return .frontmost }
        if lastExternalIsAlive { return .lastExternal }
        return .none
    }
}
