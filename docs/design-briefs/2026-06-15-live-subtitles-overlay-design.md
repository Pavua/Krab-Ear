# ТЗ: Визуальное освежение HUD живых субтитров (LiveSubtitlesOverlay)

## Задача
HUD живых субтитров — плавающий NSPanel внизу экрана, показывает переведённые строки во время трансляции системного звука (Phase 2 Live Translation). Сейчас **0 токенов KrabEarTheme** и 4 хардкод-цвета. Подними его до Liquid Glass / KrabEarTheme: стекло, типографика строк субтитров, индикатор «слушаю», подсказка «нет результатов», ритм. Это заметная поверхность поверх видео/звонка.

## 🔴 ГРАНИЦА: ТОЛЬКО ВИЗУАЛ. Поведение/сеть/таймеры НЕ ТРОГАТЬ.
«Стало выглядеть иначе» — твоя зона. «Стало вести себя иначе» — ЗАПРЕЩЕНО.

## Файлы
- **РЕДАКТИРУЙ ТОЛЬКО**: `native/KrabEarAgent/Sources/KrabEarAgent/LiveSubtitlesOverlay.swift` (426 строк).
- **НЕ ТРОГАЙ**: любой другой файл, Python, тесты, бинари.

## 🔴 КОНТРАКТ — НЕ ЛОМАТЬ
- Публичные `func show()` / `func hide()` — имена/сигнатуры/поведение сохрани.
- **SSE/сеть/таймеры — НЕ КАСАЙСЯ ЛОГИКИ**: `startSSE()`, `stopSSE()`, `startSSEStream(url:)` (подписка на `live_subs.result`), `startNoResultsTimer()`/`cancelNoResultsTimer()`, `noResultsTimer`, `fadeTimers` (per-line 4-секундный auto-fade по UUID), вся логика добавления/удаления строк. Можешь менять КАК строка ВЫГЛЯДИТ (шрифт/цвет/фон/отступ), но НЕ КАК она появляется/исчезает/таймится.
- NSPanel остаётся `[.nonactivatingPanel, .hudWindow, .utilityWindow]`, floating, always-on-top, non-activating, внизу экрана — НЕ меняй styleMask/поведение окна, не делай его activating.
- `showListeningIndicator()` / `showNoResultsHint()` — сохрани их роль (индикатор слушания + подсказка), стилизуй визуально.
- Сохрани все свойства (`backdropView`, label-элементы, panel, fadeTimers и т.д.) — не переименовывай.

## Что улучшить (визуал)
1. **Хардкод-цвета → токены** (4 шт.):
   - стр.153 `NSColor.white.withAlphaComponent(0.18)` (border backdrop) → `KrabEarTheme.Colors.border`.
   - стр.275 `NSColor.white.withAlphaComponent(0.7)` (listening label) → `Colors.textSecondary` (или textPrimary по контрасту).
   - стр.288 `NSColor.white.withAlphaComponent(0.6)` (no-results hint) → `Colors.textSecondary`.
   - стр.337 `NSColor.white.withAlphaComponent(0.65)` → подходящий токен по смыслу.
2. **Стекло**: backdrop/панель — `Colors.cardBackground` поверх NSVisualEffectView (если есть; если нет — можно добавить, сохранив non-activating panel), скруг `Metrics.cardCornerRadius`, тень `Elevation.applyOverlay` если уместно.
3. **Типографика субтитров**: строки субтитров читаемым `Typography.display` или `body` с хорошим контрастом; «слушаю»/«нет результатов» — `caption`/`captionMedium` + `textSecondary`.
4. **Ритм/отступы**: `Metrics.standard/comfortable/tight`.
5. **Индикатор «слушаю»**: можно цветной слой-пульс (CALayer, `Colors.accent`/`success`) — но с Reduce-Motion guard.

## 🔴 ЖЁСТКИЕ ПРАВИЛА (CI-гейты)
- **Glyph-guard**: НИКАКИХ `● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱` в строках NSTextField/NSAttributedString. Индикаторы — CALayer, не Unicode. CI-гейт.
- **Никакого `runModal()`**.
- **AGENT-3**: не добавляй синхронных IPC-вызовов (SSE-логика уже есть — её не трогай).
- **Reduce Motion**: любые новые анимации уважают `KrabEarTheme.Motion.animate` / `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`. Существующие fade-таймеры (4s) НЕ переделывай.
- Панель остаётся non-activating (критично — HUD не должен красть фокус с видео/звонка).
- Токены, не хардкод. НЕ коммить. НЕ трогай бинари. НЕ создавай вспомогательные .command/.swift скрипты.

## Перед началом прочитай
1. `LiveSubtitlesOverlay.swift` целиком.
2. `KrabEarTheme.swift` (токены Colors/Typography/Metrics/Motion/Elevation).
3. Корневой `CLAUDE.md` — AGENT-J (glyph), AGENT-3, runModal, Reduce Motion.

## Приёмка (отчитайся явно)
1. `cd "native/KrabEarAgent" && swift build -c release 2>&1 | tail -20` → «Build complete!». Чини ошибки итеративно.
2. В финале: **DONE/INCOMPLETE**; изменённый файл; последняя строка swift build; список визуальных изменений; **подтверди что show/hide + вся SSE/timer/fade-логика НЕ изменена**; подтверди glyph-чистоту + non-activating panel + Reduce-Motion guard.

Твой финальный текст — отчёт координатору.
