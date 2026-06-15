# ТЗ: Визуальный дизайн оверлея быстрого редактирования (QuickEditOverlay)

## Задача
QuickEditOverlay — плавающий мини-оверлей для быстрого редактирования распознанного текста перед автовставкой (Enter→вставить / Esc→отмена / таймаут→вставить оригинал). Сейчас **0 токенов KrabEarTheme** — выбивается из Liquid Glass приложения. Подними до уровня остального UI: стекло, поле редактирования, кнопки, типографика, ритм.

## 🔴 ГРАНИЦА: ТОЛЬКО ВИЗУАЛ. Логику/клавиши/таймаут/completion НЕ ТРОГАТЬ.

## Файлы
- **РЕДАКТИРУЙ ТОЛЬКО**: `native/KrabEarAgent/Sources/KrabEarAgent/QuickEditOverlay.swift` (307 строк).
- **НЕ ТРОГАЙ**: `main+PasteHandling.swift` (вызывает show()), любой другой файл, Python, тесты, бинари.

## 🔴 КОНТРАКТ — НЕ ЛОМАТЬ
- `func show(text: String, timeoutSec: Double = 5.0, completion: @escaping (QuickEditResult) -> Void)` — сигнатура/поведение/имя неизменны. Это точка входа из main+PasteHandling.
- `QuickEditResult` (enum результата: paste/cancel/timeout) + весь completion-флоу — НЕ меняй.
- Поведение клавиш: **Enter / кнопка «Вставить» → paste(editedText); Esc / кнопка «Отменить» → cancel; таймаут без действия → timeout(originalText)**. Сохрани ВСЮ эту логику (keyDown/keyEquivalent handling, таймер таймаута).
- 🔴 **ФОКУС/КЛАВИАТУРА**: этот оверлей ПРИНИМАЕТ ввод (пользователь редактирует текст) — в отличие от пассивных HUD. НЕ делай панель non-activating и не ломай первый-респондер/фокус на поле редактирования. Сохрани существующее key/focus-поведение панели.
- Сохрани поле редактирования (его текущий тип — NSTextField/NSTextView) и обе кнопки с их target/action. Не переименовывай свойства.

## Что улучшить (визуал)
1. **Стекло панели**: NSVisualEffectView + `KrabEarTheme.Colors.cardBackground`, скруг `Metrics.cardCornerRadius`, рамка `Colors.border`, тень `Elevation.applyOverlay` если уместно.
2. **Поле редактирования**: читаемая типографика `Typography.body`/`display`, цвет `Colors.textPrimary`, аккуратный фон/padding (`Metrics.comfortable`).
3. **Кнопки**: «Вставить» → `ThemePrimaryButton`, «Отменить» → `ThemeSecondaryButton` (переиспользуй существующие Theme-кнопки; сохрани их target/action). Усиль presence.
4. **Ритм/отступы/таймер-индикатор**: токены `Metrics`. Если есть визуальный индикатор обратного отсчёта таймаута — стилизуй через `Colors.accent`, но НЕ трогай саму таймаут-логику.
5. Цвета/шрифты/радиусы — строго токены KrabEarTheme.

## 🔴 ЖЁСТКИЕ ПРАВИЛА (CI-гейты)
- **Glyph-guard**: НИКАКИХ `● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱` в строках NSTextField/NSAttributedString/кнопках. Индикаторы — CALayer.
- **Никакого `runModal()`** (оверлей не модальный — НЕ делай его модальным).
- **AGENT-3**: без синхронных IPC (это UI).
- **Reduce Motion**: новые анимации уважают `KrabEarTheme.Motion.animate` / `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`.
- Токены, не хардкод. НЕ коммить. НЕ трогай бинари. НЕ создавай вспомогательные .py/.command/.swift скрипты (правь файл напрямую инструментами редактирования).

## Перед началом прочитай
1. `QuickEditOverlay.swift` целиком. 2. `KrabEarTheme.swift` (токены + ThemePrimaryButton/ThemeSecondaryButton). 3. Корневой `CLAUDE.md` — AGENT-J/AGENT-3/runModal/Reduce Motion.

## Приёмка (отчитайся явно)
1. `cd "native/KrabEarAgent" && swift build -c release 2>&1 | tail -20` → «Build complete!». Чини итеративно.
2. В финале: **DONE/INCOMPLETE**; изменённый файл; последняя строка swift build; список визуальных изменений; **подтверди что show(text:timeoutSec:completion:) + Enter/Esc/timeout-логика + фокус-поведение НЕ изменены**; glyph-чистоту.
