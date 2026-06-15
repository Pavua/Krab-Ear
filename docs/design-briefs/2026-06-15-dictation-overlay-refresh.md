# ТЗ: Визуальное освежение оверлея диктовки (RealtimeOverlayController)

## Задача
Освежить **визуал** плавающего оверлея реального времени, который показывается во время диктовки (live-транскрипция возле курсора). Он УЖЕ в стиле Liquid Glass (NSVisualEffectView + KrabEarTheme-токены) — нужен ВИЗУАЛЬНЫЙ АПГРЕЙД, не переписывание с нуля. Сделать его современнее, чище, приятнее: типографика, ритм, аудио-индикатор уровня, плавность анимаций, цвет/тени/свечение — строго через KrabEarTheme-токены.

## 🔴 ГРАНИЦА: ТОЛЬКО ВИЗУАЛ, ПОВЕДЕНИЕ НЕ ТРОГАТЬ
Это правило проекта: «стало выглядеть иначе» — твоя зона; «стало вести себя иначе» — ЗАПРЕЩЕНО. Не меняй сигнатуры, не переименовывай методы/свойства, не трогай проводку.

## Файлы
- **РЕДАКТИРУЙ ТОЛЬКО**: `native/KrabEarAgent/Sources/KrabEarAgent/RealtimeOverlayController.swift` (865 строк).
- **НЕ ТРОГАЙ**: `RealtimeOverlayController+PartialSSE.swift` (SSE-обновления), `main+RealtimeOverlay.swift` (проводка показа/скрытия), любой Python, тесты, бинари.
- Если визуальный элемент требует нового приватного хелпера/слоя — добавляй ВНУТРИ RealtimeOverlayController.swift.

## 🔴 ПУБЛИЧНЫЙ КОНТРАКТ — НЕ ЛОМАТЬ (сигнатуры + поведение обязаны сохраниться, их вызывают извне)
- `public func show()`
- `public func hide()`
- `public func update(previewText: String, translatedText: String?, durationText: String, modeHint: String)`
- `public func setOpacityPercent(_ value: Int)`
- `public func setAudioLevel(_ rms: Float)`  ← вызывается часто из аудио-потока; визуализируй уровень, но СИГНАТУРА и семантика (rms 0…1) неизменны
- `public func showRevealAnimation(...)` (сохрани все параметры как есть)
- `func setPrimaryText(_ text: String)`  ← вызывается из +PartialSSE.swift, НЕ меняй имя/сигнатуру

Приватные методы можно ИЗМЕНЯТЬ ВНУТРИ (стилизация), но СОХРАНИ их роль и вызовы: `setupPanel`, `setupEffectView`, `setupUI`, `startDotPulse`/`stopDotPulse`, `startLabelPulse`/`stopLabelPulse`, `startBreathing`/`stopBreathing`, `stopAllPulse`, `showStage`, `startDragMonitor`/`stopDragMonitor`. Оверлей должен оставаться: NSPanel **non-activating** (не крадёт фокус), перетаскиваемым (drag monitor), позиционируемым у курсора, с reveal-анимацией и pulse/breathing.

## Что улучшить (визуал — твоя свобода в рамках токенов)
1. **Типографика**: основной текст — `KrabEarTheme.Typography.display` (17pt). Вторичный/перевод/длительность/режим — иерархия через `caption`/`captionMedium`/`body`. Чёткий визуальный контраст primary↔secondary.
2. **Стекло**: фон через `KrabEarTheme.Colors.cardBackground` (0.5 alpha) поверх NSVisualEffectView; рамка `Colors.border`; тень `Colors.overlayShadow`. Скругления — `Metrics.cardCornerRadius` (12) / `innerCornerRadius` (8).
3. **Recording-dot**: красная точка записи — можно сделать изящнее (мягкое свечение/halo, плавный pulse). Цвет `Colors.error`.
4. **Аудио-уровень** (`setAudioLevel`): сейчас простая визуализация — сделай аккуратный level-meter / реактивное свечение (например мягкий ореол вокруг dot или тонкая полоса), плавно реагирующий на rms. Без тяжёлых перерисовок на каждый сэмпл.
5. **Ритм/отступы**: `Metrics.tight/standard/comfortable/spacious`, `cardPadding`, `itemSpacing`.
6. **Анимации**: длительности через `KrabEarTheme.Motion.Duration` (micro .15 / short .25 / standard .40 / long .70). Плавные, не дёрганые.

## 🔴 ЖЁСТКИЕ ПРАВИЛА (CI-гейты — нарушение = красный CI)
- **Reduce Motion**: любые анимации уважают системную настройку — используй существующий паттерн `KrabEarTheme.Motion.animate(...)` если он есть, либо guard на `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`. НЕ добавляй безусловных бесконечных анимаций без этого guard.
- **Glyph-guard**: НИКАКИХ символов `● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱` в строках NSTextField/NSAttributedString/label. Recording-dot рисуй СЛОЕМ (CALayer cornerRadius / backgroundColor), НЕ Unicode-символом. CI-гейт `test_swift_no_unicode_glyphs`.
- **Никакого `runModal()`** (оверлей и так его не использует — не добавляй).
- **AGENT-3**: оверлей — чистый UI, БЕЗ синхронных IPC-вызовов. Не добавляй ipcClient-вызовы.
- NSPanel остаётся **.nonactivatingPanel** и не крадёт фокус/ввод (это критично для диктовки — оверлей висит поверх, пользователь печатает в другое приложение).
- Токены, НЕ хардкод: цвета/шрифты/радиусы/длительности — из KrabEarTheme. Магические числа только для геометрии layout, и то предпочитай Metrics.
- НЕ коммить. НЕ трогай бинари.

## Перед началом ОБЯЗАТЕЛЬНО прочитай
1. `RealtimeOverlayController.swift` целиком (понять текущую структуру: effectView, tintView, borderLayer, recordingDot, primaryLabel, pulse/breathing, drag, reveal).
2. `KrabEarTheme.swift` (точные имена токенов: Colors / Typography / Interaction / Motion.Duration / Metrics).
3. Корневой `CLAUDE.md` — секции AGENT-J (glyph-guard), AGENT-3, NSAlert/runModal, Reduce Motion.

## Приёмка (ОБЯЗАТЕЛЬНО отчитайся явно)
1. `cd "native/KrabEarAgent" && swift build -c release 2>&1 | tail -20` → «Build complete!». Чини ошибки компилятора итеративно до зелёного.
2. В финале ЯВНО: **DONE** или **INCOMPLETE**; изменённый файл; последняя строка swift build; СПИСОК визуальных изменений; **подтверди что все 7 публичных сигнатур (show/hide/update/setOpacityPercent/setAudioLevel/showRevealAnimation/setPrimaryText) НЕ изменены**; подтверди что Reduce-Motion guard на месте и recording-dot — слой, а не глиф.

Твой финальный текст — отчёт координатору. Будь конкретным.
