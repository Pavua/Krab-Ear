# Дизайн-бриф: визуальный апгрейд секции «Сводка дня» (Daily Recap)

## Контекст
Файл: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+DailyRecap.swift`.
Это новая collapsible-секция во вкладке History (`sectionId: "history_daily_recap"`,
заголовок «Сводка дня»). Сейчас показывает дайджест дня плоским текстом в NSTextView.
Задача — сделать ВИЗУАЛ продакшн-качества в эстетике Liquid Glass / KrabEarTheme,
сохранив всю проводку.

## 🔴 ЧТО НЕЛЬЗЯ ЛОМАТЬ (проводка — НЕ трогать логику)
1. `setupDailyRecapSection() -> CollapsibleSectionView` — публичный билдер, вызывается из
   `HistoryPanelController+ApplyTheme+HistoryTab.swift:217`. Сигнатуру и имя НЕ менять.
2. `CollapsibleSectionView(sectionId: "history_daily_recap", title: "Сводка дня", isExpanded: false)` —
   sectionId НЕ менять (UserDefaults-персист состояния).
3. IPC-flow: `onRefreshDailyRecap()` / `onDailyRecapToday()` (@objc actions) вызывают
   `ipcClient.call(method: "generate_daily_digest", ...)` off-main (DispatchQueue.global →
   DispatchQueue.main для UI). Этот паттерн (AGENT-3) НЕ ломать — IPC только off-main,
   UI только на main.
4. Privacy: при `result["ok"] == false` (privacy_mode) показывать «Сводка недоступна
   в режиме приватности», без данных.
5. Поле даты `dailyRecapDateField` (placeholder «ГГГГ-ММ-ДД (пусто = сегодня)») + кнопки
   «Обновить сводку» / «Сегодня» — оставить функциональными.
6. Assoc-object паттерн доступа к контролам (objc_getAssociatedObject) — стиль файла.
7. `todayISO()` (private static) — оставить.
8. Использовать существующие токены `KrabEarTheme` (Colors/Typography/Metrics) и компоненты
   (ThemeCardView, ThemeSecondaryButton) — НЕ хардкодить цвета/шрифты/отступы.

## Доступные данные дайджеста (из generate_daily_digest result)
- `date` (String), `total_recordings` (Int), `total_duration_min` (Double),
  `total_words` (Int), `languages_used` ([String:Int]), `top_topics` ([String]),
  `highlights` ([String]).

## ЧТО УЛУЧШИТЬ (визуал)
Заменить плоский NSTextView на структурированную карточку:
- **3 metric-плитки** в ряд: «Записей» (total_recordings), «Минут» (total_duration_min,
  1 знак), «Слов» (total_words) — крупная цифра + подпись, в стиле KrabEarTheme.
- **Языки** (languages_used) — компактные чипы «ru · 5», отсортированные по убыванию.
- **Темы** (top_topics) — чипы/теги.
- **Главное** (highlights) — аккуратный список с маркерами.
- Пустой день (total_recordings == 0) — мягкое «За этот день записей нет».
- Статус-лейбл (генерируем/готово/ошибка) сохранить.
Можешь переписать структуру view + populate-логику (заполнение плиток вместо текста) —
это ОК, т.к. визуально-обусловлено; но IPC/threading/privacy/sectionId/имена @objc и
билдера — строго по списку выше.

## Приёмка
- `cd native/KrabEarAgent && swift build -c release` — БЕЗ ошибок (предупреждения
  BackendSupervisor про NSLock — pre-existing, игнор).
- НЕ коммить, НЕ трогать другие файлы, НЕ менять backend. Оставь изменения в рабочем
  дереве — Claude отревьюит дифф + пересоберёт бинаря + закоммитит.
- Заверши кратким отчётом: что изменил + результат swift build.
