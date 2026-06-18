# Дизайн-бриф: полировка панели «Встреча» (Meeting Report)

## Контекст
Только что отгружена фича Meeting Mode. Панель отчёта собрана механически из готовых компонентов (функциональна, но без дизайн-иерархии). Твоя задача — **поднять визуальное качество** до уровня «полированный отчёт о встрече», НЕ меняя поведение и проводку.

## Файл
`native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+MeetingMode.swift` — класс `MeetingReportViewController` (NSViewController, показывается как sheet через `hostWindow.beginSheet`). Размер sheet ~640×580.

Данные приходят из IPC `get_meeting_report` (УЖЕ работает — НЕ трогай вызов): поля `summary` (String), `summary_is_llm` (Bool), `action_items` ([String]), `decisions` ([String]), `questions` ([String]), `speakers` ([{label, turns:Int, duration_sec:Double}]), `speaker_count` (Int), `word_count` (Int), `ts` (String), `markdown` (String).

## 🔴 ЧТО НЕЛЬЗЯ ЛОМАТЬ (поведение — не твоя зона, оставь как есть)
1. IPC-вызов `get_meeting_report` и разворачивание `resp["result"]` — НЕ трогай.
2. Off-main диспетч (`DispatchQueue.global` → `DispatchQueue.main`) — оставь.
3. Презентацию через `hostWindow.beginSheet` с nil-guard `if let hostWindow = self.window` — НЕ заменяй на `runModal()` (ЗАПРЕЩЕНО, AppHang).
4. Кнопки «Сохранить дайджест» (`presentPanelSheet` NSSavePanel сохраняет `markdown`) и «Копировать» (`NSPasteboard` пишет `markdown`) + «Закрыть» — действия (`onSaveDigest`/`onCopyMarkdown`/`onClose`) оставь рабочими, можешь только перерисовать сами кнопки.
5. Контекст-меню «Открыть как встречу» (`makeMeetingMenuItem`/`onOpenMeeting`) и его single-selection guard — НЕ трогай.
6. Пустые секции должны деградировать в «—» (или скрываться) — не падать на `[]`/nil.
7. ТОЛЬКО токены `KrabEarTheme` (Colors/Typography/Metrics/Interaction) — НИКАКИХ хардкод-цветов/шрифтов/чисел. Если нужен новый размер — добавь в `KrabEarTheme.Metrics`, не хардкодь.
8. Glyph-safe: SF Symbols для иконок, без emoji в NSTextField.

## ЧТО УЛУЧШИТЬ (твоя зона — визуальная иерархия и полировка)
- **Заголовок отчёта**: дата встречи (`ts`) + мета-строка (кол-во слов `word_count`, кол-во спикеров `speaker_count`) — компактно, как подзаголовок.
- **Резюме** — самая важная секция, визуально доминирует: крупнее, в выделенной карточке (`ThemeCardView`), бейдж «LLM»/«локально» по `summary_is_llm`.
- **Задачи / Решения / Вопросы** — каждая секция со своей SF-Symbol-иконкой и заголовком; элементы как аккуратный список (буллеты/чекбокс-стиль для задач, нумерация или маркеры для решений/вопросов). Скрывай пустые секции.
- **Спикеры** — строки или «чипы»: метка спикера + кол-во реплик + длительность (формат M:SS). Можно мини-бар доли участия.
- **Скролл**: весь контент в вертикальном скролле (отчёт может быть длинным) — сохрани/улучши.
- **Кнопочный ряд** внизу: «Сохранить дайджест» (primary), «Копировать», «Закрыть» — выровнен, по токенам кнопок KrabEarTheme (`ThemePrimaryButton`/`ThemeSecondaryButton`).
- Общий ритм: отступы/разделители по `KrabEarTheme.Metrics`, секции визуально разделены (hairline-сепараторы или карточки), читается как опрятный отчёт, а не свалка лейблов.

## Выполни
Отредактируй `HistoryPanelController+MeetingMode.swift` (только визуальная часть `MeetingReportViewController` и его подвьюхи — НЕ логику IPC/презентации). После правок прогони `cd native/KrabEarAgent && swift build -c release` и убедись, что сборка зелёная (исправь свои ошибки/варнинги). НЕ подписывай бинари, НЕ копируй в bundle/runtime — это сделает ревьюер. НЕ открывай PR.
