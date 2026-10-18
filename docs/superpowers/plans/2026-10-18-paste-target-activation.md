# Автовставка: асинхронное ожидание активации адресата

**База:** `origin/codex/krab-ear-v2`, `19124f51`.
**Scope:** самостоятельная часть пункта 6 карточки `2026-10-16-paste-async.md`
из worktree `ear-paste-async-20261016`. Исполнение последовательно через
`superpowers:executing-plans`; финальный независимый review сильной моделью.

## Контракт и границы

Диктовка и QuickEdit сохраняют захваченный адресат; snapshot выбирает приложение
при запросе пользователя. Ожидание не выбирает новый target. Активация использует
монотонный бюджет 400 мс и освобождает MainActor. Отмена, закрытие приложения,
отказ активации или timeout не вызывают PasteService. Одна операция владеет
активацией до возврата синхронной вставки; конкурентный запрос получает явный
отказ. Это узкий предохранитель активации, не будущая общая input/clipboard queue.

Без новых зависимостей, живых клавиш, пользовательского clipboard, TCC-настроек,
audio, загрузки моделей, lifecycle, merge/deploy и правок соседних проектов.
Чужой WIP в canonical и старом worktree сохраняется. Git add — явные пути.
Исторические AppHang не атрибутируются этой задержке.

## Шаги

- [x] Выделить существующее ожидание в настоящий production helper с подменой
  только OS leaves. Подключить оба вызова делегата.
- [x] RED: MainActor heartbeat, создаваемый внутри requestActivation, успевает
  изменить fake foreground до вставки; неподтверждённая активация не вызывает paste.
  Запуск: `swift test --package-path native/KrabEarAgent -j 2 --filter PasteTargetActivationWiringTests`.
  Ожидается: assertion RED на блокировке и на вызове paste после timeout.
- [x] GREEN: async sleep, проверки cancellation/liveness/foreground/deadline,
  отказ конкурентной активации, immutable target и delay без преждевременного
  clipboard write в snapshot. Те же тесты должны PASS.
- [x] Actual delegate → activator → actual PasteService с private concealed
  pasteboard; cancellation, activation refusal, target exit и overlapping requests.
- [x] Зависимые DictationPasteTarget/PasteHandling/PasteService/Streaming тесты,
  зависимые Python source contracts, audit-all и Swift release build.
- [ ] Независимый adversarial whole-diff review; frozen SHA/hosted CI в draft PR.

## Что остаётся в полной карточке

Ожидания modifiers/backspace/AppleScript в PasteService, общая bounded очередь,
streaming generation/final barrier, SelectionTranslator и native acceptance.
Успех этой части не означает завершение всей async-миграции или устранение AppHang.

## Локальная проверка 2026-10-18

Actual production helper: RED5 tests / 12 ожидаемых assertion failures → GREEN5.
Расширенный targeted набор64 PASS, включая13 новых activation/wiring тестов.
Существующий hosted Swift filter229 PASS (наборы пересекаются, не суммировать).
Python3.14 source/settings contracts50 PASS; есть прежнее предупреждение
о torchcodec dylib, exit0, skips отсутствуют. Python-код/тесты не менялись.
`make audit-all` PASS; release build `swift build --package-path native/KrabEarAgent -c release -j 2`
PASS (75,66 с). Артефакт не запускался и не устанавливался.
Секреты/тексты владельца не читались и в тестах не использовались.
Независимый review зафиксированного source diff Astra Ultra и hosted CI pending.
