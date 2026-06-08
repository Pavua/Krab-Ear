# ТЗ для Antigravity (agy / Gemini 3.1 Pro) — Privacy-индикатор в menu-bar / status dot

> **Дата:** 2026-06-08 · **Автор ТЗ:** Claude · **Исполнитель:** `agy` (Antigravity, Google AI Pro), model `Gemini 3.1 Pro (High)`

## 0. Цель

Когда включён режим приватности (`privacy_mode_enabled`), пользователь должен **видеть это в menu-bar статус-индикаторе** (и в dot заголовка History-панели): сейчас никакого визуального признака нет. Добавить аккуратный privacy-оверлей (SF Symbol `lock.fill` или замок-бейдж в стиле Liquid Glass) поверх/рядом с supervisor-dot, который появляется при `privacy_mode_enabled == true` и исчезает при false. Один проход.

## 1. Точная карта проводки (уже разведано — используй это)

- **StatusIndicatorView** живёт как associated object на `AgentAppDelegate` (`main+HealthMonitor.swift:41`, getter `statusIndicatorView`). Обновляется каждую 1с в `applyHealthStateToStatusItem(_ state:)` (`main+HealthMonitor.swift:123`) → `statusIndicatorView.updateState(state)` (line 148) + пересборка menu-bar image через `StatusIndicatorImage.image(for:)`.
- **StatusIndicatorView.swift** — view с dot + Phase B.1 severity-badge (top-right 6pt) + `flashGreen`. `StatusIndicatorImage.image(for:size:)` строит NSImage для NSStatusItem.button.image.
- **Смена privacy_mode** наблюдается в `HistoryPanelController+Settings.swift`: `onPrivacyModeChanged()` (~line 115) и `syncSettingsControls()` (~line 479, читает `settings.privacyModeEnabled`). Модель: `AgentSettings.privacyModeEnabled: Bool` (Models.swift).
- **Мост уже есть:** `HistoryPanelController+Settings.swift` достаёт делегат через `NSApp.delegate as? AgentAppDelegate` (строки 1233/1241/1421) — используй ТОТ ЖЕ паттерн.

## 2. Рекомендуемая реализация

1. На `AgentAppDelegate` (в `main+HealthMonitor.swift`): добавить cached `var privacyModeEnabled: Bool` (associated object, как statusIndicatorView) + метод `setPrivacyMode(_ on: Bool)` который сохраняет флаг, вызывает `statusIndicatorView.setPrivacyMode(on)` и **немедленно пересобирает menu-bar image** (вызвать существующую логику применения, напр. повторить путь `applyHealthStateToStatusItem(currentState)` или эквивалент, чтобы оверлей применился сразу, а не через ≤1с).
2. В `applyHealthStateToStatusItem(_:)` (1с-таймер): передавать текущий `privacyModeEnabled` в `StatusIndicatorImage.image(for:privacyMode:)` и в `statusIndicatorView` — иначе каждая 1с-пересборка СОТРЁТ privacy-оверлей. Это критично: таймер не должен «съедать» индикатор.
3. В `StatusIndicatorView.swift`: метод `setPrivacyMode(_ on: Bool)` рисует/прячет privacy-оверлей (`lock.fill` SF Symbol слой или маленький замок-бейдж). Не конфликтовать с Phase B.1 severity-badge (он top-right) — размести privacy-маркер так, чтобы оба читались (напр. severity top-right, privacy bottom-left, или замок как tint/overlay). `StatusIndicatorImage.image(for:privacyMode:)` — отрисовать замок в NSImage-версии для menu-bar.
4. В `HistoryPanelController+Settings.swift`: в `onPrivacyModeChanged()` И в `syncSettingsControls()` после чтения `privacyModeEnabled` вызвать `(NSApp.delegate as? AgentAppDelegate)?.setPrivacyMode(enabled)` (паттерн строки 1233).

## 3. 🔴 Ограничения

- Только Swift в `native/KrabEarAgent/Sources/KrabEarAgent/` (`main+HealthMonitor.swift`, `StatusIndicatorView.swift`, `HistoryPanelController+Settings.swift`). НЕ трогать KrabEar/ (Python), тесты, main.swift entrypoint-проводку (кроме `main+HealthMonitor.swift`).
- НЕ менять сигнатуры/поведение `updateState`, `applyErrorBadge`, `flashGreen`, data source, sectionId, имена контролов. Privacy — ДОБАВКА, не замена.
- Только токены `KrabEarTheme.*` (Colors.accent/textSecondary/success и т.д.), без хардкод цвета/размера, кроме SF Symbol pointSize. **SF Symbols, не Unicode** (был баг рендера — AGENT-J). Замок = `lock.fill`.
- Анимации (если есть) через `KrabEarTheme.Motion.animate`. Thread-safe: методы StatusIndicatorView уже делают `DispatchQueue.main.async` — соблюдать.
- Никаких `runModal()`.
- `swift build -c release` зелёный, без новых warning'ов; чинить до зелёного.

## 4. Приёмка

- [ ] При `privacy_mode_enabled=true` в menu-bar появляется замок-индикатор; при false — исчезает; переживает 1с-таймер health-обновления (не мигает/не стирается).
- [ ] `onPrivacyModeChanged` (toggle в Settings) и `syncSettingsControls` (открытие панели / синк) оба обновляют индикатор.
- [ ] supervisor-dot (green/yellow/red) и severity-badge продолжают работать; privacy не ломает их.
- [ ] build зелёный; токены; SF Symbol; нет runModal.

## 5. Формат ответа

Полные изменённые `.swift` + итог `swift build`. Один проход. После — Claude review + build + bundle parity.
