# ТЗ для Antigravity / Gemini 3.1 Pro — визуальный редизайн Settings-панели Krab Ear

> **Дата:** 2026-06-08 · **Автор ТЗ:** Claude (Krab Ear session) · **Исполнитель:** Antigravity (Gemini 3.1 Pro)
> **Граница ответственности:** этот документ — инженерный контракт + структура + ограничения. **Визуальный язык (палитра, ритм, иконография, hierarchy) — на усмотрение Gemini.** Claude НЕ диктует, как именно должно выглядеть; задаёт *что нельзя ломать* и *что должно стать лучше*.

---

## 0. Цель одним абзацем

Сделать Settings-панель Krab Ear визуально «солиднее/премиальнее», сохранив **100% функциональности и проводки**. Текущий UI функционален, но плоский: длинные списки toggle'ов в коллапс-секциях без визуальной иерархии, неровный ритм отступов, бейджи разнородны. Нужно: чёткая визуальная группировка, единый ритм spacing'а, аккуратная типографика, осмысленная иконография секций, консистентные бейджи/состояния — **в стиле существующего Liquid Glass (NSVisualEffectView)**. Исполнить за **один проход**, чтобы не жечь квоту Antigravity на итерации.

---

## 1. 🔴 ЖЁСТКИЕ ОГРАНИЧЕНИЯ (нарушение = брак, build/функциональность сломаются)

1. **НЕ переименовывать** ни одну переменную контрола. Хендлеры `@objc func onXxxChanged()` (30+) привязаны по именам: `qualitySelector`, `translationSelector`, `diarizationButton`, `llmRewriteButton`, `llmModelSelector`, `hotkeySelector`, `hotkeyModeToggleRadio`, `hotkeyModeHoldRadio`, `hotkeyProfileSelector`, `audioDuckingButton`, `audioDuckingSlider`, `audioDuckingValueLabel`, `overlayOpacitySlider`, `overlayOpacityValueLabel`, `autoStartButton`, `dockIconButton`, `gigaamEnabledButton`, `sttEngineLabel`, `privacyModeButton`, `vaHotkeyToggle`, `vaWakeWordToggle`, `vaEngineSelector`, `vaBrainSelector`, `networkSelector`, `translationStyleSelector` и т.д.
2. **НЕ менять** IPC-ключи (`quality_profile`, `diarization_enabled`, `llm_rewrite_enabled`, `translation_mode`, `hotkey`, `privacy_mode_enabled`, …) и UserDefaults-ключи (`KrabEar_ConversationHotkeyEnabled`, `KrabEar_WakeWordEnabled`, `KrabEar_UseClaudeDesign`, `KrabEar_LiveSubsEnabled`, `KrabEar_ActivePreset`, …).
3. **НЕ менять** строки `sectionId` у `CollapsibleSectionView` — они = ключи персистентности `CollapsibleSection_{sectionId}` в UserDefaults. Смена sectionId сбросит сохранённое состояние раскрытия у пользователей.
4. **НЕ трогать** проводку загрузки/сохранения: `syncSettingsControls(using:)`, `applySettingsPatch(_:)`, `settingsUpdater`, `settingsProvider`, async-фетчи (`fetchAndPopulateLLMModels`, `fetchAndUpdateSTTEngineLabel`, `syncVoiceAssistantControls`). Логику не менять — только то, как контролы **скомпонованы и стилизованы**.
5. **Только AppKit**, Swift 6, macOS 13+. Никаких SwiftUI, никаких сторонних зависимостей.
6. **Использовать существующие theme-токены** `KrabEarTheme.*` (раздел 4). НЕ хардкодить цвета/отступы/шрифты числами — брать из токенов. Если нужен новый токен — добавить в `KrabEarTheme.swift` рядом с существующими, в том же стиле.
7. **Reduce Motion**: любая анимация — только через `KrabEarTheme.Motion.animate(duration:easing:animations:)` (уже уважает системный Reduce Motion). Не вызывать `NSAnimationContext`/CABasicAnimation напрямую.
8. **`.app` sheets, НЕ `runModal()`**: если добавляешь любой `NSAlert`/`NSOpenPanel`/`NSSavePanel` — только через `presentAlertSheet(_:for:completion:)` / `presentPanelSheet(_:for:completion:)` из `AlertHelpers.swift`. `runModal()` запрещён (Sequoia AppHang, гард в CI).
9. **Build должен пройти с первого раза**: `cd native/KrabEarAgent && swift build -c release` без ошибок и новых warning'ов.
10. **Не ломать A/B**: существует две дизайн-ветки — Gemini (default) и Claude Design (`KrabEar_UseClaudeDesign`). Редизайнить **Gemini-ветку** (основные `buildXxxSection`). Claude Design-вариант (`cd*`/`CDSettingsCardView`) не обязателен к правке, но не сломать.

---

## 2. Файлы

**Редактировать (визуал/компоновка):**
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` — мастер-файл секций: `buildAudioPipelineSection`, `buildTranslationSection`, `buildLLMSection`, `buildHotkeySection`, `buildSystemSection`, `buildVoiceAssistantSection`, `buildPrivacySection`, `buildQuickPresetSection`, хелперы `makeSettingRow`/`makeSwitchRow`.
- `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift` — если нужны новые токены / уточнения `CollapsibleSectionView`, `ThemeCardView`.

**Можно трогать при необходимости:**
- `HistoryPanelController+LiveSubsSettings.swift`, `HistoryPanelController+ApplyTheme+DictationSections.swift`.

**НЕ трогать:** backend (`KrabEar/`), любые `*.py`, IPC-слой, `main.swift` проводку.

---

## 3. Текущая структура секций (сохранить состав, можно улучшить компоновку внутри)

Все секции — `CollapsibleSectionView`, на табе Dictation (если не указано иначе). Состав контролов сохранить полностью.

| sectionId (НЕ менять) | Заголовок RU | Контролы |
|---|---|---|
| `dictation_audio_pipeline` | Аудио-пайплайн | diarizationButton (switch, бейдж «бета»), qualitySelector (popup), gigaamEnabledButton (switch, бейдж «RU only»), sttEngineLabel (read-only) |
| `dictation_llm` | LLM постобработка | llmRewriteButton (switch, бейдж «бета»), llmModelSelector (popup, async) |
| `dictation_hotkeys` | Горячие клавиши | hotkeySelector (popup), hotkeyModeToggleRadio/hotkeyModeHoldRadio (radio pair), hotkeyProfileSelector (popup) |
| `dictation_system_settings` | Система | audioDuckingButton (switch), audioDuckingSlider (0–100) + audioDuckingValueLabel, overlayOpacitySlider (15–90) + overlayOpacityValueLabel, autoStartButton (switch), dockIconButton (switch), A/B toggle |
| `settings_voice_assistant` | Разговор с AI | vaHotkeyToggle (switch), vaWakeWordToggle (switch, бейдж «приватность»), vaEngineSelector (popup), vaBrainSelector (popup) |
| `settings_quick_presets` | Пресеты записи | 4 кнопки-пресета (Default/Meeting/Translation/Call) |
| `settings_privacy_security` | Безопасность и приватность | privacyModeButton (switch, бейдж 🔒), кнопка «Просмотр audit log» |
| `live_translation_settings` | Настройки перевода (таб Live Translation) | translationSelector, networkSelector, translationStyleSelector |
| `live_subs_section` | Live субтитры (таб Live Translation) | enable toggle, target-lang popup, show-original toggle, reset-position button |

**Можно** менять *порядок секций*, *группировку строк внутри секции*, *подачу бейджей*, *выравнивание контролов*, *вертикальный ритм* — если состав и id сохранены.

---

## 4. Theme-токены (использовать ТОЛЬКО их, точные имена)

**Colors:** `KrabEarTheme.Colors.{windowBackground, cardBackground, accent, textPrimary, textSecondary, textDisabled, border, separator, success, error, warning, overlayShadow}`

**Typography:** `KrabEarTheme.Typography.{display(17 reg), sectionTitle(13 semibold), body(13 reg), caption(11 reg), captionMedium(11 medium), monospace(11 mono)}`

**Metrics:** `KrabEarTheme.Metrics.{tight(4), standard(8), comfortable(12), spacious(24), cardCornerRadius(12), innerCornerRadius(8), controlHeight(24)}` (+ legacy `sectionSpacing/itemSpacing/cardPadding`)

**Motion:** `KrabEarTheme.Motion.Duration.{micro .15, short .25, standard .40, long .70}`, `KrabEarTheme.Motion.Easing.{easeOut, easeIn, easeInOut, linear}`, обёртка `KrabEarTheme.Motion.animate(duration:easing:animations:)`

**Interaction:** `KrabEarTheme.Interaction.{hoverOverlayAlpha .10, pressedScale .98, pressedOverlayAlpha .15, disabledOpacity .40, transparentHoverAlpha .05}`

**Elevation:** `KrabEarTheme.Elevation.{applyCard(to:), applyPopup(to:), applyOverlay(to:)}`

**Компоненты:** `CollapsibleSectionView(sectionId:title:isExpanded:)` → `.contentStackView`, `.disclosureButton`, `.titleLabel`, `.headerStack`, `.setExpanded(_:animated:)`. `ThemeCardView`, `ThemePrimaryButton`, `ThemeSecondaryButton`, базовый `ThemeButton` (трекинг hover/press/focus уже встроен).

---

## 5. Направление улучшений (ЧТО должно стать лучше — КАК решает Gemini)

Это ориентиры, не пиксельные требования. Gemini вправе предложить лучшее.

1. **Иерархия строки настройки.** Сейчас label + контрол в ряд, описания длинные. Желательно: ведущий заголовок (`sectionTitle`/`body`), под ним вспомогательное описание (`caption`/`textSecondary`), контрол выровнен по правому краю с консистентной шириной popup'ов/switch'ей. Единый паттерн строки (хелпер `makeSettingRow`) для всех секций.
2. **Иконография секций.** Каждой секции — лаконичная SF Symbol слева от заголовка (например waveform/брейн/клавиатура/шестерёнка/щит) для быстрого скана. Только SF Symbols (`NSImage(systemSymbolName:)`), tint = `Colors.textSecondary`/`accent`. (NB: SF Symbol вместо Unicode-глифов — был баг рендера Unicode-буллета в системном шрифте, см. AGENT-J.)
3. **Единая система бейджей.** «бета», «RU only», «приватность», 🔒 сейчас разнородны. Сделать один компонент-бейдж: капсула, `captionMedium`, фон — token-цвет с пониженной альфой (`warning` для «бета», `accent` для «RU only», `textSecondary`/`success` для «приватность»). Заменить эмодзи 🔒 на SF Symbol `lock.fill`.
4. **Вертикальный ритм.** Консистентные отступы: между секциями `Metrics.comfortable`/`spacious`, внутри секции между строками `Metrics.standard`, внутренний padding карточки — из токена. Убрать «рваные» зазоры.
5. **Группировка тяжёлых секций.** «Система» большая: визуально сгруппировать связанное (звук: ducking-toggle+slider; интерфейс: overlay opacity, dock icon, autostart) тонкими разделителями (`Colors.separator`) или под-заголовками `caption`.
6. **Состояния контролов.** Disabled-зависимости видимы: при выкл. `audioDuckingButton` — slider приглушён (`disabledOpacity`); при выкл. `llmRewriteButton` — `llmModelSelector` приглушён. (Только визуальное отражение существующего enabled-состояния, логику включения не менять.)
7. **Заголовок секции как интерактив.** Header `CollapsibleSectionView` — hover-tint уже есть; убедиться, что disclosure-треугольник + иконка + заголовок + (опц.) краткий саммари-бейдж справа (например «вкл/выкл») выглядят как цельная кликабельная зона.
8. **Слайдеры.** `audioDuckingSlider`/`overlayOpacitySlider` + их value-label: значение показывать компактно справа (моноширинный `monospace`-токен для цифр, чтобы не «прыгало»).

---

## 6. Критерии приёмки

- [ ] `cd native/KrabEarAgent && swift build -c release` — **зелёный, без новых warning'ов**.
- [ ] Все перечисленные переменные контролов существуют с теми же именами; все `@objc on*Changed` компилируются.
- [ ] Все `sectionId` неизменны (grep по таблице раздела 3).
- [ ] Никаких новых `runModal()` (CI-гард `test_nsAlertRunModal_onlyInAllowlistedFiles`).
- [ ] Нет хардкод-чисел для цвета/отступа/шрифта в новом коде — только `KrabEarTheme.*`.
- [ ] Анимации только через `KrabEarTheme.Motion.animate`.
- [ ] Панель открывается, секции раскрываются/сворачиваются, состояние персистится; toggle/popup/slider пишут настройки (визуально проверяемо).
- [ ] Claude Design A/B-вариант не сломан (переключатель `KrabEar_UseClaudeDesign` работает).

---

## 7. Формат ответа Antigravity

Полные изменённые файлы (или точные диффы) для каждого тронутого `.swift`. Если добавлены токены — показать вставку в `KrabEarTheme.swift`. **Один проход, без вопросов назад** — все нужные факты в этом ТЗ; при неоднозначности выбирать вариант, наиболее согласующийся с существующим Liquid Glass и токенами. После применения Claude сделает review + `swift build` + интеграцию.
