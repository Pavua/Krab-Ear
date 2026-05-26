# Krab Ear — GUI Redesign Brief для Gemini 3.1 Pro
**Дата:** 2026-05-12  
**Статус:** ГОТОВ К ОТПРАВКЕ (нужен активный Gemini API key из Krab `.env`)  
**Модель:** `gemini-3-pro-preview` (endpoint `/v1beta/models/gemini-3-pro-preview:generateContent`)  
**Температура:** 0.2 / maxOutputTokens: 65536  
**Язык кода:** Swift 6.0, AppKit, macOS 13+  
**Дизайн-система:** `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift`  
**Токены:** `design-tokens/krab-ear-tokens.json`

---

## A. CURRENT STATE INVENTORY

### Компонент 1 — HistoryPanel (NSWindowController)
Основное окно приложения. 6 вкладок:
- **Dictation** (`dictation`) — главная вкладка диктовки + настроек
- **Live Translation** (`live_translation`) — call assist, Voice Gateway, фразовая библиотека, timeline
- **History** (`history`) — NSTableView с пагинацией, фильтры, экспорт, импорт
- **Conversation** (`conversation`) — вкладка «Разговор с AI» (Phase 1 Voice Assistant)
- **Call Automation** (`call_automation`) — Phase 3 автозвонки
- **Diagnostics** (`diagnostics`) — Phase B.2 диагностика backend

Вкладка Dictation содержит следующие 9 коллапсируемых секций (CollapsibleSectionView):
1. `dictationRecordingSection` — качество STT (balanced/max), cleanup (soft/strict), перевод, realtime preview, auto-paste, swap RU↔ES
2. `dictationSystemSection` — режим (headless/menubar), hotkey, сеть (offline/online), звук старта, clipboard mode, audio ducking, overlay opacity
3. `dictationAISection` — diarization toggle, LLM rewrite toggle, LLM model selector
4. `profileAudioSection` — profile preset selector, audio device selector, тест микрофона + RMS/peak
5. `diagnosticsSection` — 4 кнопки-запросы + прокручиваемый diagnosticsOutputView
6. `clipboardSection` — список последних 20 буфер-обмена + «Вставить повторно»
7. Превью истории (последние 3 транскрипта) + кнопка «Открыть историю»
8. `callAssistSection` — (в live_translation tab)
9. `historyFiltersSection`, `historyAdvancedSection`, `historyImportSection` — (в history tab)

### Компонент 2 — ConversationViewController
Вкладка «Разговор с AI». Чат-подобный UI с голосовым вводом (WebSocket к Voice Gateway). Содержит:
- Кнопку старта разговора, waveform визуализацию уровня звука
- NSScrollView с историей сообщений (user/assistant bubbles)
- NSTextView для текстового ввода (fallback к голосу)
- AudioLevelMeter (горизонтальная полоска, CAShapeLayer)

### Компонент 3 — GlobalStatusBar
Liquid Glass thin pill в верхней части HistoryPanel (видна со ВСЕХ вкладок). Показывает текущую long-running операцию:
- «Транскрибация · 3/12 · diarization»
- «Obsidian sync · 4/24»
- Idle: скрыт (auto-hide policy)
Реализация: NSVisualEffectView (`.hudWindow`), corner radius 10, SSE subscribe `/v1/events`.

### Компонент 4 — RealtimeOverlayController
Плавающий NSPanel для live transcription feedback (around cursor position). Состояния:
- `live` — во время записи, пульсирующий текст + red dot (CABasicAnimation)
- `reveal` — 3-stage progression after stop_recording: STT → processing → result
- `hidden`
Уже переделан Gemini 3.1 Pro (2026-04-19): DynamicTintView, StageBadgeView, state-differentiated tint (red 0.04 recording / accent 0.04 transcribing).

### Компонент 5 — StatusIndicatorView
8x8 dot indicator. Используется в двух местах:
1. Menu bar (NSStatusItem) — глобальный статус
2. History panel header (слева от заголовка)
Состояния: healthy (green) / hung (yellow) / stopped (red). Phase B.1: severity badge overlay (6pt circle в top-right углу).

### Компонент 6 — LiveSubtitlesOverlay
Плавающий NSPanel HUD внизу экрана для System Audio live subtitles. Хранит последние 3 строки, auto-fade 4 с, draggable (позиция в UserDefaults). Original + translation строки.

### Компонент 7 — ErrorToastView / BackendToast
Liquid Glass NSPanel для показа ошибок и backend restart уведомлений. Severity-aware auto-dismiss: info=2s / warn=5s / error=10s / critical=manual. Queue.

### Компонент 8 — QuickEditOverlay
Inline quick-replace overlay поверх активного поля ввода. Phase unknown (needs investigation).

---

## B. PAIN POINTS (зафиксированные в сессиях)

### B.1 Diarization toggle недоступен в GUI
Diarization on/off управляется только через `.secrets` env var (`KRAB_EAR_DIARIZATION_ENABLED=false`). Пользователь не может переключить без редактирования файла. IPC `update_settings` поддерживает `diarization_enabled` runtime toggle — нужен только GUI элемент в `dictationAISection`.

### B.2 Статус импорта не виден
Во время пакетного импорта аудио (`historyImportSection`) статус операции «не пишется» — пользователь не может отслеживать прогресс длительных операций (часовые файлы). GlobalStatusBar в теории должен покрыть это через SSE `app.status` events, но визуальная интеграция слабая.

### B.3 Quick Replace hotkey UX confusion
QuickEditOverlay — пользователи путаются в UX: не ясно что является «original» при замене, нет визуальной подсветки diff (before/after). Нет undo-hint.

### B.4 Settings panel перегружена
Вкладка «Dictation» содержит 9+ collapsible sections в одном scroll view. Нет визуальной группировки по частоте использования (частое: quality, auto-paste / редкое: diagnostics, clipboard).

### B.5 Conversation tab визуально пустой при старте
ConversationViewController при первом открытии показывает пустой экран без onboarding-подсказки и без визуального feedback «как начать разговор».

### B.6 Settings panel не имеет modern visual language
По сравнению с Liquid Glass overlay и реализованным дизайном cards в других компонентах — settings panel секции выглядят как чистые NSStackView без visual hierarchy. Заголовки секций и controls смешаны в одном уровне.

### B.7 History timeline без speaker labels
NSTableView в History tab показывает raw transcript text без указания спикера (хотя diarization данные есть в `speaker` поле items). Нет temporal visualization.

---

## C. DESIGN CONSTRAINTS (ЖЁСТКИЕ)

### C.1 Platform
- macOS 13+ (Ventura), Swift 6.0 strict concurrency
- AppKit only (no SwiftUI), NSWindowController/NSViewController pattern
- NSVisualEffectView Liquid Glass как основной container материал

### C.2 Existing Design System (НЕЛЬЗЯ МЕНЯТЬ)
Все токены из `KrabEarTheme.swift` и `design-tokens/krab-ear-tokens.json`:
```
Colors:
  windowBackground: clear (over NSVisualEffectView)
  cardBackground: controlBackgroundColor @ 0.5 alpha
  accent: .controlAccentColor (system blue)
  textPrimary: .labelColor
  textSecondary: .secondaryLabelColor
  textDisabled: .tertiaryLabelColor
  border (dark): white @ 0.15 alpha
  border (light): black @ 0.10 alpha
  success: .systemGreen
  error: .systemRed
  overlayShadow: black @ 0.25

Typography:
  display: SF Pro 17pt regular (overlay transcription)
  sectionTitle: SF Pro 13pt semibold (headers)
  body: SF Pro 13pt regular (controls, inputs)
  caption: SF Pro 11pt regular (dates, filters)
  captionMedium: SF Pro 11pt medium (badges, status)
  monospace: SF Mono 11pt regular (logs, diagnostics)

Spacing (4pt grid):
  tight: 4pt
  standard: 8pt
  comfortable: 12pt
  spacious: 24pt
  cardCornerRadius: 12pt (continuous curve)
  innerCornerRadius: 8pt
  controlHeight: 24pt

Motion:
  micro: 0.15s (hover, press)
  short: 0.25s (expand/collapse, tab switch)
  standard: 0.40s (overlay show, modals)
  long: 0.70s (pulse, attention loops)
  Easing: easeOut (most transitions), easeInOut (expand/collapse)
  Reduce Motion: ОБЯЗАТЕЛЬНО поддерживать (duration=0 при enabled)

Interaction:
  hoverOverlay: white 10% (dark mode)
  pressedScale: 0.98x
  pressedOverlay: black 15%
  disabledOpacity: 0.40
  transparentHoverAlpha: 5% (header buttons)

Elevation:
  card: shadowOpacity=0.15, offset=(0,-2), radius=6
  popup: shadowOpacity=0.20, offset=(0,-6), radius=16
  overlay: shadowOpacity=0.30, offset=(0,-12), radius=32
```

### C.3 Accessibility
- VoiceOver labels на всех controls (accessibilityLabel)
- Keyboard navigation (tab order, return/space activation)
- Minimum tap target 24pt (Metrics.controlHeight)
- NSTrackingArea для hover states
- Reduce Motion через `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`

### C.4 Color Modes
- Dark mode: primary use case
- Light mode: fully supported (все токены dynamic)
- System accent color: respected (не хардкодить синий)

### C.5 Язык UI
- Русский язык первичный (все labels, кнопки, tooltips)
- Испанский и английский строки: где есть, сохранять
- Профессиональный тон, без игривости

---

## D. BRAND GUIDELINES

**Krab Ear** — локальный голосовой ассистент и транскрибер для macOS. Privacy-first: работает полностью offline, никакие данные не покидают машину.

**Визуальный тон:**
- Серьёзный профессиональный инструмент (не playful, не candy)
- Продуктивность-first дизайн (как Raycast, Bear, Linear — не как Siri)
- Плотность информации средняя-высокая (power user)
- Liquid Glass как метафора прозрачности и точности

**Иконка:** краб (🦀 → формализованный, не emoji). Не использовать в production UI — только в onboarding и splash.

**Отличие от основного Krab (Telegram userbot):** Krab Ear — отдельное приложение с собственной UI identity. Они связаны через Telegram bridge но визуально независимы.

---

## E. SPECIFIC GEMINI PROMPTS (10 batched prompts)

Каждый prompt — самодостаточный. Gemini должен вернуть **Swift AppKit code** (не SwiftUI, не pseudocode). Включать полный класс/extension файл, импорты, constraints.

---

### PROMPT 1 — Settings Panel Reorganization

```
You are a senior macOS AppKit engineer designing a UI for Krab Ear, a professional voice dictation app.

TASK: Redesign the Settings collapsible sections in HistoryPanelController+Settings.swift.
The current design has 9 flat collapsible sections in one NSScrollView — hard to navigate.

DESIGN GOAL: Group sections by usage frequency into 3 visual groups with subtle NSBox separators:
  GROUP A "Основное" (shown expanded by default):
    - dictationRecordingSection: STT quality, cleanup, translation mode, auto-paste
    - dictationAISection: diarization toggle, LLM rewrite toggle, LLM model selector
  GROUP B "Система" (collapsed by default):
    - dictationSystemSection: hotkey, network mode, audio ducking, overlay opacity
    - profileAudioSection: profile preset, audio device, microphone test
  GROUP C "Инструменты" (collapsed by default):
    - diagnosticsSection: diagnostics buttons + output view
    - clipboardSection: clipboard history + repaste

DESIGN CONSTRAINTS:
  - Use KrabEarTheme.swift tokens (see below)
  - NSVisualEffectView material .hudWindow for group containers
  - Group headers: 11pt semibold, textSecondary color, ALL CAPS
  - NSBox separator between groups: borderType .separator, borderColor = KrabEarTheme.Colors.border
  - CollapsibleSectionView existing class — do NOT redesign the section expand/collapse mechanism
  - Dark mode primary, light mode supported (all colors via semantic tokens only)
  - Swift 6.0 strict concurrency (@MainActor)

DESIGN TOKENS (from KrabEarTheme.swift):
  cardBackground: NSColor.controlBackgroundColor.withAlphaComponent(0.5)
  border: white 0.15 dark / black 0.10 light (dynamic)
  textSecondary: NSColor.secondaryLabelColor
  sectionTitle: SF Pro 13pt semibold
  caption: SF Pro 11pt regular
  comfortable: 12pt padding
  spacious: 24pt margin
  cardCornerRadius: 12pt

OUTPUT: Full Swift extension file HistoryPanelController+SettingsGroups.swift that adds the 3-group visual wrapper on top of existing CollapsibleSectionView components. Include NSBox group headers and separators. No SwiftUI. No hardcoded hex colors.
```

---

### PROMPT 2 — Diarization + AI Controls Card

```
You are a senior macOS AppKit engineer.

TASK: Design the dictationAISection collapsible card for Krab Ear (Swift AppKit, macOS 13+).
This section needs a redesigned ThemeCardView with 3 controls:

CONTROL 1 — Diarization Toggle:
  - NSSwitch (macOS 13+) labeled «Разделение спикеров»
  - Subtitle: «Определяет кто говорит (требует GPU)» in textSecondary 11pt
  - On toggle: call IPC update_settings {diarization_enabled: bool}
  - When ON: show inline warning badge «Требует GPU · ~2-3с задержка» (yellow .systemOrange, captionMedium)
  - When OFF: badge hidden

CONTROL 2 — LLM Rewrite Toggle:
  - NSSwitch labeled «AI-улучшение текста»
  - Subtitle: «Qwen / Gemma через LM Studio» in textSecondary 11pt
  - On toggle: call IPC update_settings {llm_rewrite_enabled: bool}
  - When LM Studio unreachable (stored in agentSettings.llmRewriteEnabled + probe result): show inline warning «LM Studio недоступен» in textDisabled

CONTROL 3 — LLM Model Selector:
  - NSPopUpButton with list of available models (loaded via IPC list_llm_models)
  - Enabled only when LLM Rewrite is ON
  - Label: «Модель» 13pt body
  - Loading state: NSProgressIndicator .spinning while fetching models

LAYOUT: NSStackView vertical, spacing 8pt. ThemeCardView container with comfortable padding (12pt).
State: all 3 controls reflect current AgentSettings. Update on IPC success only (optimistic UI is NOT used).

DESIGN TOKENS: KrabEarTheme.Colors.*, Typography.*, Metrics.* (same as Prompt 1).

OUTPUT: Full Swift extension HistoryPanelController+AIControlsCard.swift. Include setupAIControlsCard() method and all @IBAction equivalents as @objc private func. Swift 6.0 @MainActor.
```

---

### PROMPT 3 — History Timeline with Speaker Labels

```
You are a senior macOS AppKit engineer designing a history view for a dictation app.

TASK: Redesign the NSTableView cell in the History tab (HistoryPanelController+History.swift) to show transcript items with:
  1. Temporal context: relative timestamp («2 мин назад», «вчера 14:32») in caption 11pt textSecondary — top right
  2. Speaker badge: if item.speaker is non-nil, show pill badge «Спикер 1» in captionMedium accent color
  3. Quality dot: 8x8 colored dot based on item.confidence:
     - >= 0.85: systemGreen
     - 0.60..0.85: systemOrange
     - < 0.60: systemRed
  4. Transcript preview: first 2 lines of text, body 13pt, clipped with ellipsis
  5. Translation preview: if item.translation non-nil, 1 line below in 11pt textSecondary italic
  6. Action strip: on row hover, reveal 3 icon buttons (Copy, Paste, Delete) with transparentHoverAlpha overlay

LAYOUT:
  - NSView-based cell (NSTableCellView subclass)
  - Total row height: 64pt normal, 48pt compact (density toggle in existing toolbar)
  - NSStackView horizontal: [quality dot (8pt)] [main content stack] [timestamp (right-align)]
  - Hover: NSTrackingArea, action strip slides in from trailing edge (motion.short = 0.25s)
  - Selection: NSTableView selection highlight + KrabEarTheme.Colors.accent tint overlay 10%

CONSTRAINTS:
  - Swift 6.0 @MainActor
  - No SwiftUI
  - Reuse KrabEarTheme tokens (no hardcoded colors)
  - Reduce Motion: action strip appears instantly (no slide animation) when enabled

OUTPUT: Full Swift class HistoryItemCellView: NSTableCellView — complete implementation with configure(item:isCompact:) method.
```

---

### PROMPT 4 — Conversation Tab Empty State + Onboarding

```
You are a senior macOS AppKit engineer.

TASK: Design the empty state and onboarding overlay for ConversationViewController (the "Разговор с AI" tab in Krab Ear).

Current problem: When user opens the Conversation tab for the first time, they see a blank dark NSView.
No affordances, no call-to-action, no visual feedback.

DESIGN: Create ConversationEmptyStateView — shown when conversation history is empty and no active session.

LAYOUT (centered in parent view):
  1. Icon area: 48x48 NSImageView with SF Symbol "waveform.and.mic" (systemSymbolConfiguration .large, textSecondary color)
  2. Title: «Начните разговор с AI» in sectionTitle 13pt semibold
  3. Subtitle: «Нажмите кнопку ниже или дважды нажмите правый Option» in caption 11pt textSecondary, multiline, centered, max width 280pt
  4. Primary button: «Начать разговор» (ThemePrimaryButton, 200pt wide, 32pt tall)
  5. Shortcut hint: «⌥⌥ двойное нажатие» badge below button in monospace captionMedium, textDisabled

VISUAL STYLE:
  - NSVisualEffectView container (.hudWindow material, cardCornerRadius 12pt)
  - Appear animation: fade in + scale from 0.95 to 1.0 (motion.standard 0.40s easeOut)
  - Reduce Motion: opacity only (no scale)
  - Dark mode primary

CONNECTION REQUIREMENTS: Disappear immediately when conversation.startConversation() is called. Show again when session ends and history is empty.

OUTPUT: Full Swift file ConversationEmptyStateView.swift. Include show(in:)/hide() methods. @MainActor. No hardcoded colors.
```

---

### PROMPT 5 — GlobalStatusBar Redesign (Import Progress)

```
You are a senior macOS AppKit engineer.

TASK: Extend GlobalStatusBar.swift to show audio import progress more visibly.

Current problem: During batch audio import, users see no visual feedback. The GlobalStatusBar shows generic text but:
  - Font is too small (12pt) for an operation taking 1-5 minutes
  - Progress bar is hidden behind a non-obvious expand interaction
  - No ETA shown
  - No cancel button

DESIGN: Add ImportProgressMode to GlobalStatusBar alongside existing transcription/idle modes.

ImportProgressMode UI (replaces current pill content when op="import"):
  1. Icon: SF Symbol "square.and.arrow.down" (12pt, textSecondary)
  2. Primary text: «Импорт · {current}/{total} · {filename}» (body 13pt, textPrimary)
  3. Progress bar: NSProgressIndicator .determinate, height 3pt, full width of pill, below text
  4. ETA label: «~{eta_seconds}с» (caption 11pt, textSecondary, right-aligned)
  5. Cancel button: NSButton with SF Symbol "xmark.circle.fill", 16pt, trailing edge, textSecondary
     - On click: emit IPC cancel_import_job
  6. Pill auto-expands to height 36pt (from 22pt default) with motion.short animation

SSE event payload to handle (new `import.progress` event):
  { "op": "import", "current": 3, "total": 12, "filename": "call_2026-05-10.m4a", "eta_seconds": 45 }

EXISTING CODE STRUCTURE: GlobalStatusBar already has NSVisualEffectView backgroundView, iconLabel, textLabel, progressBar, cancelButton (hidden by default). Wire cancel button to existing hidden outlet.

OUTPUT: Full Swift extension GlobalStatusBar+ImportProgress.swift with handleImportProgressEvent(_:) method and animation wiring. @MainActor. No hardcoded colors or magic numbers.
```

---

### PROMPT 6 — Quick Replace UX Redesign

```
You are a senior macOS AppKit engineer.

TASK: Redesign QuickEditOverlay.swift for clearer before/after visual feedback.

Current problem: Users don't understand which text is being replaced. No visual diff. No undo affordance.

DESIGN: Three-stage overlay:
  STAGE 1 "Replace Ready": show original text with red strikethrough + new text with green underline
    - NSAttributedString with NSStrikethroughStyleAttributeName (.single) + foreground .systemRed for original
    - New text: foreground .systemGreen + NSUnderlineStyleAttributeName
    - Both in display font (17pt regular)
    - "Принять" button (ThemePrimaryButton, green accent) + "Отменить" button (ThemeSecondaryButton)
    - Keyboard: Return = accept, Escape = cancel

  STAGE 2 "Accepted": brief green flash (0.3s) + fade out
  STAGE 3 "Cancelled": brief orange flash (0.15s) + fade out

LAYOUT (NSPanel floating near cursor):
  - Width: max(300pt, text width + 48pt)
  - Min height: 88pt
  - NSVisualEffectView (.hudWindow) container, cardCornerRadius 12pt, elevation overlay shadow
  - NSStackView vertical: [diff view] [button row]
  - Drag to reposition (NSEvent mouseDragged override)

KEYBOARD:
  - Return: accept
  - Escape: cancel
  - Tab: cycle focus between buttons

ACCESSIBILITY: accessibilityLabel on buttons in Russian. VoiceOver should read «Заменить [original] на [new]».

OUTPUT: Full Swift class QuickEditOverlay (NSPanel subclass). Include show(original:replacement:onAccept:onCancel:)/hide() API. @MainActor.
```

---

### PROMPT 7 — Live Subtitles HUD Redesign

```
You are a senior macOS AppKit engineer.

TASK: Redesign LiveSubtitlesOverlay.swift — the floating HUD for system audio live subtitles.

Current design: 3 NSTextField rows stacked vertically, plain dark background, no visual hierarchy.

NEW DESIGN:
  Container: NSVisualEffectView material .fullScreenUI (more transparent than .hudWindow for overlay context), cornerRadius 16pt, max width 640pt, auto-height

  Each subtitle line:
    - Container NSView, height 28pt normal / 44pt when showing original+translation
    - LEFT: language flag emoji in 14pt (detected language: 🇷🇺/🇪🇸/🇬🇧)
    - CENTER stack:
        Original text: body 13pt textSecondary (shown only when showOriginalAndTranslation=true)
        Translation text: body 13pt textPrimary (primary focus)
    - RIGHT: timestamp «{ago}с» in caption 11pt textDisabled

  Age-based opacity: lines fade from 1.0 → 0.0 linearly over 4 seconds
    (oldest line = 0.25 alpha at t=3s, 0.0 at t=4s, newest = 1.0)
    Animation: CABasicAnimation on layer opacity, Reduce Motion = no fade (instant hide at 4s)

  Position: bottom-center of main display, 32pt from bottom edge (drag override saves to UserDefaults "LiveSubsOverlayOrigin")

  Show/hide: fade + translate Y +8pt on show (easeOut 0.25s), reverse on hide. Reduce Motion: opacity only.

  Empty state: hidden (isVisible = false) — never show blank HUD

OUTPUT: Full class LiveSubtitlesOverlay replacing existing file. Maintain existing public API: isVisible, showOriginalAndTranslation, restBaseURL, start()/stop(). @MainActor.
```

---

### PROMPT 8 — Error Toast Severity Visual Polish

```
You are a senior macOS AppKit engineer.

TASK: Polish ErrorToastView.swift — the Liquid Glass error notification panel.

Current issues:
  - All severities look the same (just different text)
  - Critical errors look identical to info toasts — not alarming enough
  - No icon differentiation

DESIGN: Severity-differentiated visual treatment:

  INFO:    SF Symbol "info.circle.fill" (textSecondary), pill border = border token (subtle), background standard
  WARN:    SF Symbol "exclamationmark.triangle.fill" (.systemOrange), pill border = systemOrange @ 0.3 alpha
  ERROR:   SF Symbol "xmark.octagon.fill" (.systemRed), pill border = systemRed @ 0.4 alpha, slight red tint on background (systemRed @ 0.06)
  CRITICAL: Same as ERROR + slow pulse animation (CABasicAnimation opacity 1.0→0.7→1.0, duration=1.4s, repeatCount=MAXFLOAT), border pulse amplitude stronger (0.4→0.7 alpha cycle)

  Layout changes:
    - Icon: 16x16 NSImageView (SF Symbol, symbolConfiguration .body), leading edge, vertically centered
    - Title text: 13pt semibold (not regular)
    - Subtitle text: 11pt regular textSecondary
    - Action button (for actionable errors): trailing edge, captionMedium, accent color, tappable
    - Reduce Motion: no pulse, border stays at max alpha for critical

  Stack: horizontal [icon] [text VStack] [close button or action button]
  Container: NSVisualEffectView .hudWindow, cornerRadius 10pt, comfortable padding (12pt), elevation popup shadow

OUTPUT: Extension file ErrorToastView+SeverityStyle.swift with func applySeverityStyle(_ severity: String) method callable from existing ErrorToastView. @MainActor. No hardcoded colors.
```

---

### PROMPT 9 — Status Indicator Menu Bar Pill (Phase B.1)

```
You are a senior macOS AppKit engineer.

TASK: Upgrade StatusIndicatorView.swift to support Phase B.1 severity badge overlay.

CURRENT: 8x8 circle dot in green/yellow/red based on HealthState. Menu bar image.

UPGRADE: Add severity badge (6pt circle) in top-right of the dot when errors are present.

Badge colors:
  info: systemBlue
  warn: systemOrange
  error: systemRed
  critical: systemRed + CABasicAnimation blink (opacity 1.0↔0.3, duration=0.6s)

Badge visibility rules:
  - Show: when applyErrorBadge(severity:) is called
  - Hide: when hideBadge() is called
  - Critical auto-blink: start timer in applyErrorBadge, invalidate in hideBadge
  - Multiple calls to applyErrorBadge: update in place (idempotent)
  - Reduce Motion: blink disabled (badge stays solid red for critical)

Implementation: CALayer-based (not NSView) for menu bar rendering compatibility.
  - badgeLayer: CALayer?, CGSize 6x6, cornerRadius 3
  - Position: CGPoint(x: bounds.maxX - 3, y: bounds.maxY - 3)
  - Update via needsDisplay = true after state change

NSImage rendering: override draw(_ dirtyRect:) to render both dot and badge into NSImage for NSStatusItem.button.image.

OUTPUT: Complete replacement StatusIndicatorView.swift. Keep existing HealthState enum and updateState(_ state: HealthState) API. Add applyErrorBadge(severity: String)/hideBadge(). @MainActor.
```

---

### PROMPT 10 — Conversation Chat Bubbles

```
You are a senior macOS AppKit engineer.

TASK: Design the message bubble cell for ConversationViewController's message history NSTableView.

Two bubble types:
  USER bubble (right-aligned):
    - Background: accent color (controlAccentColor) @ 0.15 alpha + NSVisualEffectView .selection material
    - Corner radii: 12pt all corners, 4pt trailing-bottom corner (chat convention)
    - Max width: 75% of table width
    - Text: body 13pt textPrimary
    - Timestamp: caption 11pt textDisabled, below text, right-aligned

  ASSISTANT bubble (left-aligned):
    - Background: cardBackground (controlBackgroundColor @ 0.5) + NSVisualEffectView .hudWindow
    - Corner radii: 12pt all corners, 4pt leading-bottom corner
    - Max width: 85% of table width
    - Text: body 13pt textPrimary
    - Thinking indicator: when isThinking=true, show 3-dot pulse animation (CABasicAnimation sequential opacity on 3 CALayer circles, staggered 0.2s)
    - Timestamp: caption 11pt textDisabled, below text, left-aligned

  Appearance animation: bubble fades in + slides from side (USER: from right, ASSISTANT: from left), translate X 12pt → 0, opacity 0→1, motion.short 0.25s easeOut. Reduce Motion: opacity only.

  ACCESSIBILITY: accessibilityLabel = «[role]: [text]» (e.g. «Пользователь: Привет»)

  Cell API: configure(role: "user"|"assistant", text: String, timestamp: Date, isThinking: Bool)

OUTPUT: Full Swift class ConversationBubbleCell: NSTableCellView. @MainActor. No hardcoded colors or magic numbers — use KrabEarTheme tokens.
```

---

## F. GEMINI CALL WORKFLOW

Читать ключ: `KEY=$(grep "^GEMINI_API_KEY=" /Users/pablito/Antigravity_AGENTS/Краб/.env | cut -d= -f2)`  
Model: `gemini-3-pro-preview` · temperature: 0.2 · maxOutputTokens: 65536  
Запуск батчами: Batch 1 (Prompt 1-4 параллельно), Batch 2 (5-7), Batch 3 (8-10).

---

## G. INTEGRATION CHECKLIST (после получения Gemini output)

После каждого ответа Claude/Sonnet выполняет:
- [ ] Проверить что файл компилируется (`swift build -c release` в `native/KrabEarAgent/`)
- [ ] Проверить отсутствие hardcoded hex colors (`grep -E "#[0-9A-Fa-f]{3,6}" <file>`)
- [ ] Проверить Swift 6.0 compatibility (нет `@preconcurrency` без причины, `@MainActor` там где нужно)
- [ ] Проверить Reduce Motion путь (ищи `NSWorkspace.shared.accessibilityDisplayShouldReduceMotion`)
- [ ] Проверить Russian strings (нет английских label'ов в production UI)
- [ ] Запустить тесты (`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -q --tb=no`)

**НЕ делать самому (граница Gemini/Claude):**
- Менять цвета, шрифты, spacing, corner radius → только Gemini
- Добавлять новые анимации → только Gemini
- Менять visual hierarchy → только Gemini

**ОК делать Claude/Sonnet:**
- Исправлять constraints конфликты
- Добавлять отсутствующие IPC calls
- Fixing Swift 6.0 actor isolation errors
- Wireup в HistoryPanelController.setupUI()
