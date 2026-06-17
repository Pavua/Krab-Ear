# ТЗ: Панель «Инсайты записей» в Krab Ear

## Цель
Добавить НОВУЮ самодостаточную сворачиваемую секцию **«Инсайты записей»** во вкладку **История** (History tab) — список эвристических инсайтов о записях пользователя за последние N дней (пиковый час активности, смена языка, динамика уверенности STT, стрик, доминирующая тема, темп речи). Бэкенд УЖЕ полностью готов — нужен только Swift-фронт. Образец для подражания — только что отгруженная секция «Календарь активности» (`HistoryPanelController+ActivityCalendar.swift`, паттерн идентичен).

## Что НЕЛЬЗЯ ломать (жёстко)
- НЕ трогай существующие секции/потоки History-вкладки, аналитический дашборд, другие вкладки.
- НЕ используй `runModal()` (Sequoia AppHang — запрещено; см. `AlertHelpers.swift`).
- ВСЕ IPC-вызовы строго off-main (паттерн AGENT-3 — эталон в `HistoryPanelController+ActivityCalendar.swift` `fetchActivityCalendar()` и `+Analytics.swift`), результат на main через `DispatchQueue.main.async`.
- glyph-guard: текст через `NSTextField`; не вызывай синхронный CoreText glyph-build на первом показе нестандартных глифов.
- Уважай Reduce Motion (если анимации — через `KrabEarTheme.Motion.animate()`).

## IPC-контракт (УЖЕ существует — anti-rebuild, переиспользуй)
Метод: **`get_recording_insights`**, params: `{"days": <int, по умолчанию 7>}`.
Возвращает dict:
```json
{
  "insights": [
    {"type": "peak_hour"|"language_shift"|"confidence_change"|"streak"|"topic"|"speech_pace"|...,
     "title": "<краткий заголовок, готов к показу>",
     "description": "<человекочитаемое описание, готово к показу>",
     "confidence": 0.0..1.0,
     "data": { ...структурные детали, для показа НЕ обязательны... }}
  ],
  "count": <int>,
  "days": <int>
}
```
- Privacy mode / нет данных: вернётся `{"insights": [], "count": 0, "days": N, "privacy_mode_active": true}` (или просто пустой `insights`). Обработай как empty-state.
- `title` и `description` приходят **уже готовыми строками** (на русском) — показывай как есть, НЕ конструируй текст из `data`.
- Парс ответа: `r["result"]` (как в существующих call-site'ах — `ipcClient.call` возвращает полный конверт `[String:Any]`).

## Визуальная спецификация (твоя зона — дизайн)
- Список инсайт-**карточек/строк** в вертикальном стеке (по одной на инсайт). Каждая:
  - **Иконка по `type`** (SF Symbol, подбери подходящие: peak_hour→`clock`, language_shift→`globe`, confidence_change→`gauge`/`checkmark.seal`, streak→`flame`, topic→`tag`, speech_pace→`speedometer`/`waveform`; для неизвестного type — нейтральный `lightbulb`). Цвет иконки — акцент темы.
  - **Title** (жирнее, `KrabEarTheme.Typography` заголовочный токен, `textPrimary`).
  - **Description** (вторичный токен, `textSecondary`, перенос на несколько строк при необходимости).
  - Опционально — лёгкий индикатор `confidence` (например бледный бэйдж «N%» или точка-градация), если уместно по дизайну; не обязателен.
  - Разделение карточек — через отступ или `ThemeCardView`/тонкий divider в духе Liquid Glass.
- **Контрол выбора периода** (опционально, но желательно): сегментед/поповер «7 / 14 / 30 дней» → перезагрузка `get_recording_insights` с новым `days`. Если делаешь — IPC off-main.
- **Empty-state**: аккуратный плейсхолдер «Пока нет инсайтов — записывайте больше, чтобы Krab Ear нашёл закономерности» (или короче), показывается при `count==0` / privacy mode.
- Секция-контейнер — `CollapsibleSectionView(sectionId: "history_recording_insights", title: "Инсайты записей", isExpanded: true)` с персистенцией (как у календаря/других секций).

## Поведение / проводка (механика — можешь сам)
- Новый файл `HistoryPanelController+RecordingInsights.swift` — СКОПИРУЙ структуру `HistoryPanelController+ActivityCalendar.swift`: associated-object для view, `setupRecordingInsightsSection() -> CollapsibleSectionView`, хук одной строкой в History-вёрстке (`HistoryPanelController+ApplyTheme+HistoryTab.swift`, рядом со строкой `historyStack.addArrangedSubview(setupActivityCalendarSection())` — добавь свою строку аналогично).
- Модель: `struct RecordingInsight { type, title, description, confidence }` + парс из `[String:Any]` с безопасными дефолтами (как `ActivityCalendarData.DayInfo`).
- Загрузка: при открытии секции — off-main `ipcClient.call(method: "get_recording_insights", params: ["days": 7])`, парс `r["result"]["insights"]`, reload на main. Empty-state при пустом/ошибке.

## Сборка и проверка (обязательно)
1. `cd native/KrabEarAgent && swift build -c release` — без ошибок и warning'ов по твоим файлам.
2. Без мёртвого кода / неиспользуемых символов.
3. В финале сообщи: созданные/изменённые файлы, краткое описание визуала, результат `swift build`.

НЕ коммить и НЕ пушь — ревью + сборку + parity + codesign + commit сделает ревьюер (Claude).
