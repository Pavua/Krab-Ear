# Design brief: полировка Call Observer (HUD + панель звонка)

Исполнитель: agy / Gemini 3.1 Pro (High). Волна Call Observer w1 смержена; UI функционален, но нарочно не полирован. Твоя задача — ТОЛЬКО визуал.

## Файлы (только эти два)
- `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift`
- `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverPanelController.swift`

## Что улучшить
1. **HUD** (плавающая плашка ~340px): привести к Liquid Glass языку проекта (`KrabEarTheme`, паттерн `LiveSubtitlesOverlay`): материал/скругления/тени, типографика статуса и последних реплик, компактный ряд кнопок. Добавить **статус-дот** (цвет по статусу звонка: ringing=жёлтый, talking=зелёный, ended=серый) и бейджи mute/hold — данные уже приходят в `updateHUD(session:status:...)`.
2. **Панель**: карточная лента транскрипта (реплики собеседника/агента визуально различимы: сторона, цвет-акцент, перевод под оригиналом мельче), header с телефоном/направлением/таймером/стоимостью, состояние «завершён»/«переподключение…» как аккуратные бейджи, «прервано N %» — приглушённый стиль. Прокрутка прилипает к низу.
3. Пустые состояния («ждём реплик…»), hover-состояния кнопок через `KrabEarTheme.Interaction`.

## Жёсткие инварианты (нарушение = откат диффа)
- НЕ трогать: имена/сигнатуры методов протоколов `CallObserverHUDPresenting`/`CallObserverPanelPresenting`, координатор, все `coordinator?.*`-вызовы, test hooks (`testHook_*`), `HUDClickView`/`isClick`, логику `windowWillClose`/`presentAlertSheet`/`hangupSheetOpen`.
- НЕ добавлять IPC/WS-вызовов и новых ключей настроек (класс бага: agy однажды выдумал IPC-ключ).
- SF Symbols only; НИКАКИХ эмодзи и новых non-ASCII глифов, кроме уже живущих в native/ (кириллица, « », ·, →, …, — разрешены). После правок прогони: `grep -o '[^\x00-\x7F]' <оба файла> | sort -u` и сверь каждый новый символ грепом по native/.
- Никаких `runModal`; никакой загрузки шрифтов/ресурсов извне.
- Reduce Motion уважается (`KrabEarTheme.Motion.animate()`).
- `swift build -c release` и `swift test --filter CallObserverUITests` обязаны остаться зелёными (тесты читают `testHook_*` и поведение — их менять нельзя).

## Проверка после (делает Claude, не ты)
Дифф-ревью grep'ом: runModal / протокол-сигнатуры / IPC-строки / глиф-гейт; пересборка; parity бинарей.
