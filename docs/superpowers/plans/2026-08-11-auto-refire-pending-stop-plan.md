# План: авто-дострел отложенного stop_recording (мини-волна, 1 задача)

Спека: `docs/superpowers/specs/2026-08-11-auto-refire-pending-stop-design.md`
(читать ПЕРВОЙ — она нормативна; при расхождении план уступает спеке).
База: `codex/krab-ear-v2` @ `c06ec20d`. Один воркер, isolated worktree,
ветка `claude/auto-refire-pending-stop`.

## Global Constraints

- Swift 6, MainActor-дисциплина как в соседнем коде; HealthMonitor-АКТОР НЕ
  меняется вообще (ни одной строки в `HealthMonitor.swift`).
- Существующее поведение ручного пути не меняется; `stopRecording()`
  получает только опциональный параметр с default'ом.
- Никаких новых зависимостей/файлов вне списка ниже.
- Тесты: чистая решающая логика выделяется в тестируемую struct (урок
  [[reference_dead_test_only_helper_reshape]] — тестировать РЕАЛЬНОЕ
  решение, не копию); wiring пинится source-контрактом (класс
  «декоративная проводка», см. MainErrorsWiringTests паттерн).
- Гейт: `swift build -c release` + полный `swift test` (все ~1445) зелёные.

## Задача 1 (единственная)

### 1а. Чистая решающая логика — новый файл
`native/KrabEarAgent/Sources/KrabEarAgent/DictationStopAutoRetryGate.swift`

```swift
/*
 DictationStopAutoRetryGate — решение «можно ли авто-дострелить отложенный
 stop_recording» (спека 2026-08-11-auto-refire-pending-stop-design.md §2.3).

 Чистая struct без побочных эффектов: AgentAppDelegate передаёт снимок
 своего состояния и получает решение. Выделена из делегата ровно затем,
 чтобы гарды тестировались юнитами без конструирования AgentAppDelegate
 (демон-объекты, NSApp — нетестируемо в чистом XCTest).
*/

struct DictationStopAutoRetrySnapshot {
    var recoveryPending: Bool
    var generationOwner: String?
    var isProcessing: Bool
    var quickCaptureActive: Bool
    var remainingBudget: Int
}

enum DictationStopAutoRetryGate {
    /// Полный бюджет авто-попыток на один «эпизод потери» (непрерывный
    /// период recoveryPending). Кап против карусели «healthy ping каждые
    /// 3с → полный coordinator-цикл» на терминально-невнятном backend'е —
    /// тот же паттерн, что give-up cap WedgedEscalationTracker.
    static let fullBudget = 2

    static func shouldAttempt(_ s: DictationStopAutoRetrySnapshot) -> Bool {
        return s.recoveryPending
            && s.generationOwner == "dictation"
            && !s.isProcessing
            && !s.quickCaptureActive
            && s.remainingBudget > 0
    }
}
```

### 1б. Состояние делегата — `main.swift`
Рядом с `var isProcessing = false` (строка ~195):

```swift
    // Мини-волна 2026-08-11 (авто-дострел отложенного stop_recording):
    // armed — one-shot «стоп сдался, ждём первого здорового ping'а»;
    // budget — кап попыток на эпизод recoveryPending (спека §2.2/2.5).
    var dictationStopAutoRetryArmed = false
    var dictationStopAutoRetryBudget = DictationStopAutoRetryGate.fullBudget
```

### 1в. Взвод/сброс + попытка — `main+HotkeyRecording.swift`

1. В `retainDictationStopRecovery` (после `recordingStopRecoveryPending = true`,
   строка ~930): взвод ТОЛЬКО для диктовки:

```swift
        // Авто-дострел (спека 2026-08-11): первый же здоровый ping после
        // этого провала попробует стоп сам. Только dictation — quick
        // capture имеет свой recovery-путь (панель), спека §2.7.
        if activeGenerationOwner == "dictation" {
            dictationStopAutoRetryArmed = true
        }
```

2. Восстановление бюджета + disarm во ВСЕХ местах main+HotkeyRecording.swift,
   где `recordingStopRecoveryPending = false` (строки ~471, ~599, ~799,
   ~887, ~910 — воркер обязан grep'нуть все вхождения в ЭТОМ файле и
   покрыть каждое; QuickCapture-файл НЕ трогать):

```swift
            dictationStopAutoRetryArmed = false
            dictationStopAutoRetryBudget = DictationStopAutoRetryGate.fullBudget
```

3. Новый метод (рядом с `stopRecording()`):

```swift
    /// Авто-дострел отложенного stop_recording после восстановления backend
    /// (спека 2026-08-11-auto-refire-pending-stop-design.md). Вызывается из
    /// onHealthyPing-обёртки (main+HealthMonitor.swift) fire-and-forget.
    func attemptPendingDictationStopRecovery() async {
        let snapshot = DictationStopAutoRetrySnapshot(
            recoveryPending: recordingStopRecoveryPending,
            generationOwner: activeGenerationOwner,
            isProcessing: isProcessing,
            quickCaptureActive: quickCaptureActive,
            remainingBudget: dictationStopAutoRetryBudget
        )
        guard DictationStopAutoRetryGate.shouldAttempt(snapshot) else {
            logger.info("Авто-дострел stop_recording: гейт отклонил (pending=\(snapshot.recoveryPending), owner=\(snapshot.generationOwner ?? "nil"), processing=\(snapshot.isProcessing), qc=\(snapshot.quickCaptureActive), budget=\(snapshot.remainingBudget))")
            return
        }
        dictationStopAutoRetryBudget -= 1
        logger.info("Авто-дострел отложенного stop_recording (остаток бюджета: \(dictationStopAutoRetryBudget))")
        await stopRecording(autoRetried: true)
    }
```

4. `stopRecording()` → `stopRecording(autoRetried: Bool = false)`.
   Существующие call sites не меняются (default). В УСПЕШНОЙ терминальной
   ветке (`"ok"`, район строки ~839-887): если `autoRetried`, к
   существующему поведению добавить тост
   `notify(title: "Krab Ear", body: "Остановка достреляна автоматически — текст обработан")`
   (одна строка; если ветка успеха не имеет удобной точки — допустимо
   ограничиться logger.info, отметить выбор в отчёте).

### 1г. Wiring — `main+HealthMonitor.swift` (строка ~281)

ЗАМЕНИТЬ существующее замыкание КОМПОЗИЦИЕЙ (оба действия, спека R4):

```swift
            // Кап подряд-эскалаций перевзводится живым backend'ом.
            // + авто-дострел отложенного stop_recording (спека 2026-08-11):
            // ОБА действия в ОДНОМ замыкании — слот setOnHealthyPing один,
            // повторный вызов затёр бы предыдущего подписчика.
            await monitor.setOnHealthyPing {
                await wedgeGate.noteHealthy()
                let shouldFire = await MainActor.run { () -> Bool in
                    guard delegate.dictationStopAutoRetryArmed else { return false }
                    delegate.dictationStopAutoRetryArmed = false  // disarm ДО hop
                    return true
                }
                if shouldFire {
                    // fire-and-forget: stopRecording живёт секунды-минуты,
                    // await заблокировал бы tick-цикл актора (спека §2.2).
                    Task { @MainActor in
                        await delegate.attemptPendingDictationStopRecovery()
                    }
                }
            }
```

(`delegate` — как он доступен в этом же файле для setOnWedgeDetected ниже;
воркер сверяет реальное имя/захват по соседнему коду.)

### 1д. Тесты — новый файл
`native/KrabEarAgent/Tests/KrabEarAgentTests/DictationStopAutoRetryTests.swift`

1. Gate-юниты (по спеке §5.2-5.3): базовый positive; 5 негативов (каждый
   гард отдельно: pending=false, owner="quick_capture"/nil,
   isProcessing=true, quickCaptureActive=true, budget=0); budget=1 → true.
2. `fullBudget == 2` пин (осознанная константа спеки, не магия).
3. Source-контракты (чтение исходников как в MainErrorsWiringTests):
   - `main+HealthMonitor.swift` содержит в ОДНОМ setOnHealthyPing-замыкании
     И `noteHealthy`, И `attemptPendingDictationStopRecovery` (R4);
   - `retainDictationStopRecovery` в main+HotkeyRecording.swift содержит
     взвод `dictationStopAutoRetryArmed = true`;
   - каждое вхождение `recordingStopRecoveryPending = false` в
     main+HotkeyRecording.swift сопровождается (в пределах ±6 строк)
     сбросом armed и восстановлением бюджета — регекс-проверка по
     вхождениям, НЕ по фиксированным номерам строк.

### 1е. Гейт воркера

`cd native/KrabEarAgent && swift build -c release && swift test` — полный,
не только новый файл. Отчёт: DONE/BLOCKED, headSha, вывод финального
прогона тестов (последние строки), выбор тост-vs-лог из 1в.4.

## Чего НЕ делать

- Не трогать `HealthMonitor.swift`, `RecordingStopCoordinator.swift`,
  `main+QuickCapture.swift`, backend (Python) — вообще.
- Не «улучшать» соседний код, не переименовывать существующее.
- Не коммитить бинарники (`Krab Ear.app/**`, `native/runtime/**`).
