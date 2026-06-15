# ТЗ: Визуальное освежение GlobalStatusBar (Liquid Glass thin pill)

## Задача
GlobalStatusBar — тонкая «пилюля» вверху HistoryPanel, видна со всех вкладок, показывает текущую длительную backend-операцию («Транскрибация · 3/12 · diarization», «Obsidian sync · 4/24») через SSE `app.status`; в idle скрыта. Заявлена как «Liquid Glass thin pill», но использует **0 токенов KrabEarTheme**. Тематизируй и отполируй под дизайн-систему — стекло, типографика, статус-акцент, ритм. Сохрани тонкость/ненавязчивость.

## 🔴 ГРАНИЦА: ТОЛЬКО ВИЗУАЛ. SSE/жизненный цикл/парсинг/show-hide-логику НЕ ТРОГАТЬ.

## Файлы
- **РЕДАКТИРУЙ ТОЛЬКО**: `native/KrabEarAgent/Sources/KrabEarAgent/GlobalStatusBar.swift` (308 строк).
- **НЕ ТРОГАЙ**: любой другой файл, Python, тесты, бинари.

## 🔴 КОНТРАКТ — НЕ ЛОМАТЬ
- `final class GlobalStatusBar: NSView` — тип/имя/публичный интерфейс сохрани.
- **SSE/сеть/жизненный цикл — НЕ КАСАЙСЯ**: подключение к `/v1/events` (long-poll), старт при viewDidAppear / стоп при viewWillDisappear, парсинг `app.status` event'а, вся логика показа/скрытия пилюли по idle/активной операции. Можешь менять КАК пилюля и текст ВЫГЛЯДЯТ, но НЕ КАК обновляются данные и когда показывается/прячется.
- Сохрани свойство-label (которое получает текст статуса из SSE) и все остальные свойства — не переименовывай. Сохрани `setupViews()` роль.
- Пилюля видна со всех вкладок, mounted в windowContentView выше tabSelector — НЕ меняй её позиционирование/иерархию в окне (только внутренний визуал).

## Что улучшить (визуал)
1. **Стекло пилюли**: `KrabEarTheme.Colors.cardBackground` поверх NSVisualEffectView (если ещё не), рамка `Colors.border`, скруг — пилюля округлая (можно `Metrics.cardCornerRadius` или половина высоты для full-pill), тень `Elevation.applyOverlay` если уместно (но тонко).
2. **Типографика статуса**: `Typography.captionMedium` или `caption`, цвет `Colors.textPrimary`/`textSecondary` с хорошим контрастом. Числовые счётчики (3/12) можно `captionMedium.tabular()` чтобы не прыгали.
3. **Статус-акцент**: маленький цветной слой-индикатор (CALayer dot, `Colors.accent`/`success`) слева от текста — опционально, с Reduce-Motion guard если пульсирует.
4. **Ритм**: `Metrics.tight/standard` для внутренних отступов пилюли. Сохрани «тонкость» (не раздувай высоту).
5. Цвета/шрифты/радиусы — строго токены.

## 🔴 ЖЁСТКИЕ ПРАВИЛА (CI-гейты)
- **Glyph-guard**: НИКАКИХ `● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱` в строках NSTextField/NSAttributedString. Индикатор — CALayer.
- **Никакого `runModal()`**. **AGENT-3**: без новых синхронных IPC (SSE уже есть — не трогай).
- **Reduce Motion**: новые анимации уважают `Motion.animate` / `accessibilityDisplayShouldReduceMotion`.
- Токены, не хардкод. НЕ коммить. НЕ трогай бинари. НЕ создавай вспомогательные .py/.command/.swift скрипты.

## Перед началом прочитай
1. `GlobalStatusBar.swift` целиком. 2. `KrabEarTheme.swift` (токены). 3. Корневой `CLAUDE.md` — AGENT-J/AGENT-3/runModal/Reduce Motion.

## Приёмка (отчитайся явно)
1. `cd "native/KrabEarAgent" && swift build -c release 2>&1 | tail -20` → «Build complete!». Чини итеративно.
2. В финале: **DONE/INCOMPLETE**; изменённый файл; последняя строка swift build; список визуальных изменений; **подтверди что класс GlobalStatusBar + вся SSE/жизненный-цикл/show-hide-логика НЕ изменены**; glyph-чистоту.
