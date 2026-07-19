# S34: Clipboard safety — защита от затирания чувствительного буфера (S-волна)

Дата: 2026-07-19 · Автор: Sonnet 5 (спека + самопроверка) · Статус: draft → execute
Источник: старый `docs/ROADMAP.md` S34 («режимы `always_copy`/`copy_on_fail`/`never_copy`;
предупреждение о риске потери текста; поведение буфера предсказуемо и настраиваемо»).
Скаут-инвентаризация (2026-07-19, Sonnet read-only агент) подтвердила: **3 режима уже
полностью реализованы** (backend + Swift UI + hotkey-профили + тесты) — заново строить
не нужно (анти-rebuild, тот же урок, что S64/S65 в этот же день).

## 1. Реальный гэп (по инвентаризации, не по букве старого тикета)

Ни один из 7 вызовов `PasteService.putToClipboard(_:)` (`main+PasteHandling.swift:61,187,243`,
`main+QuickCapture.swift:464`, `main.swift:981`, `main+QuickReplace.swift:123`,
`PasteService.swift:180` внутри `pasteToFrontmostApp`) не проверяет, что́ СЕЙЧАС лежит
в системном буфере, прежде чем затереть его. Диктовка в режиме `always_copy`/`copy_on_fail`
безвозвратно уничтожает то, что пользователь скопировал ДО этого — включая пароли из
менеджеров паролей (1Password/Bitwarden и др. помечают такой контент типом
`org.nspasteboard.ConcealedType`, де-факто стандарт nspasteboard.org — ни разу не
проверяется в проекте, grep = 0 хитов).

Deliverable «предупреждение о риске» из старого тикета — тоже не реализован
(ни tooltip, ни help-текст у clipboard-mode UI).

## 2. Дизайн

### 2.1 Не переносим паттерн `SelectionTranslator` буквально

`SelectionTranslator.clipboardPasteFallback` делает save→write→Cmd+V→**restore через 1s**
(`SelectionTranslator.swift:306-337`) — но это ТРАНЗИТНЫЙ синтетический paste в чужое
приложение, конечная цель не оставить текст в буфере. Основной dictation-flow —
ПРОТИВОПОЛОЖНЫЙ случай: `always_copy` СОЗНАТЕЛЬНО оставляет транскрипт в буфере для
ручной вставки позже (иногда через минуты) — авто-restore-по-таймеру сломал бы саму
фичу «скопировано, вставь когда удобно». Поэтому решение — НЕ save/restore-таймер,
а **guard перед перезаписью**: не трогать буфер, если он сейчас защищён.

### 2.2 Единая точка защиты — `PasteService.putToClipboard`

Все 7 вызывающих мест уже проходят через один метод — фикс туда, а не в каждый
call site (root cause, не symptom; тот же принцип, что «один гейт лимита — грепни
сиблингов»). Прецедент в этом же файле: `pasteToFrontmostApp` уже отказывается
писать в `AXSecureTextField` (пароль-поле НАЗНАЧЕНИЯ) через `onSecureFieldSkipped`
callback (`PasteService.swift:30,168`, wired `main.swift:476-478`). Симметричный
guard для буфера-ИСТОЧНИКА — тот же архитектурный паттерн, тот же closure-hook стиль:

```swift
// PasteService.swift
var onConcealedClipboardSkipped: (() -> Void)?

func putToClipboard(_ text: String) {
    let pasteboard = NSPasteboard.general
    guard !pasteboardHoldsConcealedContent(pasteboard) else {
        logger.warn("[Clipboard] Overwrite skipped — pasteboard holds concealed content")
        onConcealedClipboardSkipped?()
        return
    }
    pasteboard.clearContents()
    pasteboard.setString(text, forType: .string)
}

private func pasteboardHoldsConcealedContent(_ pasteboard: NSPasteboard) -> Bool {
    // org.nspasteboard.ConcealedType — де-факто стандарт (nspasteboard.org),
    // менеджеры паролей (1Password, Bitwarden, Keychain Access и др.) помечают
    // этим типом чувствительный контент рядом с .string.
    pasteboard.types?.contains(NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")) ?? false
}
```

Сигнатура `putToClipboard` НЕ меняется (void, без `@discardableResult Bool`) — ни один
из 7 call sites не трогается. Единственная новая проводка — `main.swift` (рядом с
`onSecureFieldSkipped`, строка ~476):

```swift
pasteService.onConcealedClipboardSkipped = { [weak self] in
    self?.notify(title: "Krab Ear",
                 body: "Буфер обмена защищён (пароль/секрет) — текст не скопирован, доступен в истории")
}
```

**Самопроверка (реентерабельность)**: closure НЕ должен звать `handlePasteFailure(reason:)`
(в отличие от `onSecureFieldSkipped`) — `handlePasteFailure` сам вызывает
`pasteService.putToClipboard(text)` на строке 243 при `clipboardMode != "never_copy"`,
и если бы closure вёл обратно в `handlePasteFailure`, повторный `putToClipboard` внутри
неё снова упёрся бы в тот же guard → повторный вызов closure → бесконечный цикл при
защищённом буфере. Прямой `notify()` без похода через `handlePasteFailure` — единственный
безопасный вариант.

### 2.3 Risk-warning UI (второй deliverable тикета)

Короткий tooltip (`NSMenuItem.toolTip` / `NSControl.toolTip`, дёшево, без нового layout)
у существующих clipboard-mode контролов:
- `HistoryPanelController+Settings.swift` (Settings-таб, селектор режима, ~строка 200-212).
- `main+StatusMenu.swift` (submenu «Буфер обмена», ~строка 438-454) — по пункту на режим.

Текст (точный, отражает РЕАЛЬНОЕ поведение после фикса, не generic disclaimer):
- `always_copy`: «Каждая диктовка заменяет буфер обмена транскриптом. Пароли и другой
  защищённый контент не затираются.»
- `copy_on_fail`: «Буфер заменяется только если вставка в приложение не удалась.
  Пароли и другой защищённый контент не затираются.»
- `never_copy`: «Буфер обмена никогда не используется диктовкой.»

### 2.4 Вне скоупа (осознанно)
- Explicit «Copy» UI-действия (кнопки «Скопировать» в истории/панели/QuickCapture,
  ~10 файлов из инвентаризации) — пользователь сам инициирует затирание осознанно,
  это не тот класс риска, что пассивная фоновая диктовка. Guard в `putToClipboard`
  их всё равно защитит бесплатно (тот же метод), но НЕ повод трогать их UI/логику.
- Полный save/restore прежнего содержимого буфера — сознательно отвергнуто (см. 2.1),
  сломало бы фичу «текст ждёт в буфере».
- `pasteSnapshotText` (`main+PasteHandling.swift:187`) сейчас пишет в буфер БЕЗ проверки
  `clipboardMode` вообще (даже при `never_copy`) — существующее поведение, не трогаем
  (не входит в тикет; фиксируется тем же guard'ом заодно, но раздельная проверка режима
  для этого call site — отдельная задача, не эта волна).

## 3. Тесты (TDD, RED→GREEN)

`native/KrabEarAgent/Tests/KrabEarAgentTests/PasteServiceClipboardSafetyTests.swift`,
`setUp`/`tearDown` сохраняют и восстанавливают РЕАЛЬНЫЙ `NSPasteboard.general` (тесты
трогают системный буфер — прецедент `PasteServiceRepastTests` показывает, что прямая
работа с `NSPasteboard.general`/`UserDefaults` в тестах — норма проекта; changeCount
до/после теста восстанавливается явно, не оставляя мусор в буфере CI-раннера):

1. `test_putToClipboard_writes_normally_when_no_concealed_content` — обычный текст в
   буфере → запись проходит, `pasteboard.string(forType: .string)` == новый текст.
2. `test_putToClipboard_skips_write_when_concealed_type_present` — буфер помечен
   `org.nspasteboard.ConcealedType` → после вызова содержимое буфера НЕ изменилось.
3. `test_putToClipboard_invokes_callback_only_on_skip` — closure вызывается ровно 1 раз
   при concealed, 0 раз при обычной записи.
4. `test_putToClipboard_empty_pasteboard_writes_normally` — пустой буфер (без типов
   вообще) → `types` == nil, guard не должен упасть на unwrap, запись проходит.
5. Source-contract (класс QuickCaptureWiringTests/BrainLeaseMenuTests): `main.swift`
   реально содержит `onConcealedClipboardSkipped = ` (пин проводки — класс
   «setupErrorBus определён, но не вызван»); проводка НЕ вызывает
   `handlePasteFailure` (грепом по отсутствию `handlePasteFailure` в теле closure —
   защита от реентерабельности из 2.2).
6. Tooltip-тексты: source-contract греп присутствия ключевой фразы «не затираются» в
   `HistoryPanelController+Settings.swift` и `main+StatusMenu.swift`.

## 4. Порядок исполнения

Worktree `feature/s34-clipboard-safety`, база свежий `codex/krab-ear-v2`. TDD в этом
же файле. Гейты: `swift build -c release`, полный `swift test`, глиф-гейт (только
русский текст в notify/tooltip — не новый класс глифов, AGENT-J/M это не касается).

**Security-чувствительная волна** (класс «потеря пользовательских данных») — по
кодексу требует adversarial-гейта топ-моделью (Fable) ПЕРЕД мержем, не только
личного построчного гейта. Мерж — после явного OK от Fable-ревью всего диффа ветки.

Живой смок: вручную (или через `pbcopy` + маркер типа) положить в буфер
`org.nspasteboard.ConcealedType`-помеченный контент, продиктовать что-то с
`always_copy` — убедиться, что буфер не тронут и notify показался; затем обычный
smoke без concealed-маркера — буфер заменяется как раньше.
