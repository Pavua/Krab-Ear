# ТЗ: Визуальный дизайн вкладки «Разговор с AI» (ConversationViewController)

## Задача
Вкладка «Разговор с AI» (Voice Assistant) была построена как ВИЗУАЛЬНЫЙ СКЕЛЕТ в ожидании дизайн-прохода Gemini (см. заголовок файла: «Визуальный стиль — skeleton… дизайн — отдельный PR через Gemini»). Сейчас твой проход: подними её до уровня остального приложения (Liquid Glass / KrabEarTheme) — карточки, иерархия, статус-индикатор, область транскрипции, кнопки, ритм. Это премиальная флагманская поверхность голосового ассистента.

## 🔴 ГРАНИЦА: ТОЛЬКО ВИЗУАЛ, ПОВЕДЕНИЕ НЕ ТРОГАТЬ
«Стало выглядеть иначе» — твоя зона. «Стало вести себя иначе» — ЗАПРЕЩЕНО. Не переименовывай свойства/методы/селекторы, не меняй проводку target/action, не убирай контролы.

## Файлы
- **РЕДАКТИРУЙ ТОЛЬКО**: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController+UI.swift` (251 строка).
- **НЕ ТРОГАЙ**: `ConversationViewController.swift` (главный, объявляет свойства), `+Audio.swift`, `+WebSocket.swift` (обновляют statusLabel/transcriptView при событиях), `ConversationEvents.swift`, любой Python, тесты, бинари.
- Новые приватные визуальные хелперы добавляй ВНУТРИ +UI.swift.

## 🔴 КОНТРАКТ — НЕ ЛОМАТЬ (компиляция enforce-нёт, но проверь сам)
- `func buildUI()` — точка входа, вызывается из главного VC. Имя/сигнатура неизменны.
- ВСЕ свойства, на которые ссылается +UI.swift (объявлены в др. файлах, их обновляют +WebSocket/+Audio): `statusLabel`, `waveformPlaceholder`, `transcriptView`, `startButton`, `interruptButton`, `settingsDrawer`, `settingsDisclosure`, `langHintSelector`, `engineSelector`, `brainSelector`, `config`. НЕ переименовывай, НЕ убирай, сохрани их добавление в иерархию.
- ВСЕ селекторы + target/action: `onStartStopTapped`, `onInterruptTapped`, `onSettingsDisclosureTapped`, `onLangHintChanged`, `onEngineChanged`, `onBrainChanged`. Сохрани проводку (`button.target = self; button.action = #selector(...)`).
- Контролы и их наполнение: 3 NSPopUpButton-селектора (langHint: Авто/RU/EN/ES; engine: Авто/moshi/seamless; brain: Авто/qwen3-4b/llama-3.2-3b) + их addItems/индексная логика в onXChanged. Settings-drawer (свёрнут, раскрывается дисклоужером). startButton = ThemePrimaryButton, interruptButton = ThemeSecondaryButton (скрыт пока AI не говорит).
- transcriptView (NSTextView): isEditable=false, isSelectable=true — сохрани. Его .string обновляется извне (stt.partial) — НЕ перетирай логику обновления.

## Что улучшить (визуал)
1. **Карточная иерархия**: используется `ThemeCardView` (makeCard). Подними визуальный ритм — отступы/spacing через `KrabEarTheme.Metrics` (standard/comfortable/spacious), заголовки карточек через `Typography.sectionTitle`.
2. **Статус-индикатор** (statusLabel «🟢 Слушает»/«🟡 Думает»/«🔴 Говорит»): сделай премиальнее. Можно добавить рядом цветной СЛОЙ-индикатор (CALayer dot: `Colors.success`/`warning`/`error` по состоянию) — НО статус-строку обновляет внешний код через statusLabel.stringValue, поэтому либо оставь текст как есть и добавь декоративный слой, либо стилизуй сам label. НЕ ломай внешнее обновление текста.
3. **Waveform placeholder**: сейчас серый прямоугольник-заглушка. Сделай красивее (мягкий градиент/паттерн «ожидания» через CALayer, скруг `Metrics.innerCornerRadius`), сохрани heightAnchor=48 и сам `waveformPlaceholder` (его потом заменит реальный визуализатор).
4. **Область транскрипции**: 180pt скролл — сделай аккуратнее (фон-карточка, padding, читаемая типографика `Typography.body`, цвет `Colors.textPrimary`/`textSecondary`). Можно лёгкий «chat-feel».
5. **Кнопки**: startButton/interruptButton — уже Theme-кнопки, усиль presence (размер/иконка-через-слой если нужно). НЕ меняй их target/action.
6. **Цвета/шрифты/радиусы/отступы** — строго токены KrabEarTheme (`Colors`/`Typography`/`Metrics`/`Motion`), а не хардкод.

## 🔴 ЖЁСТКИЕ ПРАВИЛА (CI-гейты)
- **Glyph-guard**: НИКАКИХ символов `● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱` в строках NSTextField/NSAttributedString/заголовках. Цветные индикаторы рисуй CALayer'ом, не Unicode-символом. (Эмодзи 🟢🟡🔴 в statusLabel — НЕ в запрещённом списке, но их ставит ВНЕШНИЙ код; в layout-строках их не вводи.) CI-гейт `test_swift_no_unicode_glyphs`.
- **Никакого `runModal()`**.
- **AGENT-3**: это построение UI, БЕЗ синхронных IPC. Не добавляй ipcClient-вызовы.
- **Reduce Motion**: анимации (если добавляешь, напр. пульс waveform) уважают `KrabEarTheme.Motion.animate` / `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`.
- Переиспользуй существующие компоненты: `ThemeCardView`, `ThemePrimaryButton`, `ThemeSecondaryButton`, хелперы makeCard/hStack/styleLabel (можешь их улучшить, но сохрани роль).
- НЕ коммить. НЕ трогай бинари.

## Перед началом прочитай
1. `ConversationViewController+UI.swift` целиком.
2. `ConversationViewController.swift` (объявления свойств — чтобы не переименовать).
3. `KrabEarTheme.swift` (точные токены) + `KrabEarTheme` ThemeCardView/ThemePrimaryButton/ThemeSecondaryButton.
4. Корневой `CLAUDE.md` — AGENT-J (glyph), AGENT-3, runModal, Reduce Motion.

## Приёмка (отчитайся явно)
1. `cd "native/KrabEarAgent" && swift build -c release 2>&1 | tail -20` → «Build complete!». Чини ошибки итеративно.
2. В финале: **DONE/INCOMPLETE**; изменённый файл; последняя строка swift build; список визуальных изменений; **подтверди что `buildUI()` + все свойства/селекторы НЕ переименованы**; подтверди glyph-чистоту + Reduce-Motion guard.

Твой финальный текст — отчёт координатору.
