# ТЗ: панель «Память» (витрина дирижёра памяти)

Проект: Krab Ear, Swift-агент macOS (AppKit, swift-tools 6.0, macOS 13+).
Задача: НОВАЯ визуальная панель, показывающая картину памяти и решения дирижёра.
Это витрина для недели shadow-наблюдения: владелец смотрит, что дирижёр СОБИРАЛСЯ
сделать, прежде чем включать enforce.

## Что уже есть (не переделывать)

- Строка в статус-меню: `main+MemoryLine.swift` — «Память: brain 19Г · whisper idle 4м».
  Она остаётся. Панель — отдельная, более подробная.
- Тема и токены: `KrabEarTheme.swift` (Liquid Glass, NSVisualEffectView, ThemeCardView,
  CollapsibleSectionView, ThemePrimaryButton). ИСПОЛЬЗУЙ их, не изобретай свои цвета.
- Образцы панелей-окон: `MeetingReportViewController.swift`, `AnalyticsDashboardViewController.swift`
  — посмотри, как они устроены и открываются, следуй тому же стилю.

## 🔴 ЖЁСТКИЕ ОГРАНИЧЕНИЯ (нарушение = работа не принимается)

1. **НЕ трогать Python.** Вообще. Ни одного файла в `KrabEar/`.
2. **IPC-контракт ПИНОВАН — не выдумывай ключи.** Ровно один метод:
   `get_memory_ledger` с `params: [:]`. Ответ:
   ```
   {ok: true,
    ledger: {v: 1, entries: {"<owner>/<resident>": {owner, resident, size_mb: Int,
             state: "active"|"warm"|"idle", idle_since_ts: Double?, reload_cost: "cheap"|"expensive",
             pid: Int?, updated_ts: Double}}},
    conductor: {enabled: Bool, thread_alive: Bool, last_tick_ts: Double?, shadow_since: Double?,
                pressure_streak: Int, would_skip_brain_reload: Int,
                residents: {"<name>": {attempted, succeeded, skipped_gate, unknown, failed, would}},
                decisions: [String]}}
   ```
   Все таймстампы — EPOCH (сравнивай с `Date().timeIntervalSince1970`).
   Если тебе кажется, что нужен другой ключ или метод — НЕ добавляй, напиши об этом в отчёте.
3. **IPC строго off-main** (`DispatchQueue.global`), мутация UI — на main. Это жёсткое
   правило проекта (синхронный IPC на главном потоке = AppHang).
4. **Никаких `runModal()`** — только non-blocking sheets через `AlertHelpers.swift`
   (`presentAlertSheet`/`presentPanelSheet`). Правило проекта, гейт в CI.
5. **Глиф-гейт**: перед использованием любого non-ASCII символа грепни его по `native/` —
   встречается ли уже. Незнакомый глиф в CoreText давал AppHang. Кириллица, `·`, `—`,
   `Г`, `→` — уже используются, безопасны.
6. Не переименовывать существующие контролы, не менять `sectionId` у секций.

## Что нарисовать

Окно «Память» (открывается пунктом статус-меню — пункт добавь сам рядом со строкой «Память»).

Три блока сверху вниз:

**1. Резиденты (главное).** Горизонтальные бары по одному на запись `ledger.entries`,
отсортированные по `size_mb` убыв. На каждом: имя (часть ключа после «/»), размер
(«19,5 ГБ»), состояние. Цвет бара по `state`: active — акцентный, warm — нейтральный,
idle — приглушённый. Для idle подпиши «простаивает N мин» (из `idle_since_ts`).
Владельца (`krab_ear` / `krab_ear_rest` / `krab`) покажи мелко — важно видеть, чьё это.
Внизу блока — суммарный объём.

**2. Режим дирижёра.** Крупно: «SHADOW — решения только логируются» либо
«ENFORCE» (если `conductor.shadow_since == nil`). При shadow — сколько дней идёт
(из `shadow_since`). Рядом мелко: `thread_alive`, давление (`pressure_streak`),
время последнего тика (`last_tick_ts` → «N сек назад»).

**3. Решения.** Таблица счётчиков `conductor.residents`: строка на резидента,
колонки would / attempted / succeeded / skipped_gate / unknown / failed.
Под ней — список `conductor.decisions` (последние решения, моноширинным, скроллящийся).

Обновление: кнопка «Обновить» + автообновление раз в 5 секунд, пока окно открыто
(таймер останавливать при закрытии — утечка таймера недопустима).

Пустое состояние: если `entries` пуст — «Дирижёр ещё не публиковал данные».
Ошибка IPC — «Данные недоступны» + кнопка «Обновить», НЕ пустое окно.

## Приёмка

- `cd native/KrabEarAgent && swift build -c release` — обязан пройти.
- `swift test` — существующие тесты обязаны остаться зелёными (1513+).
- Отчёт: какие файлы создал/изменил, где добавил пункт меню, что решил по спорным местам.
