# Design Brief: Visual Polish Pack (Call Observer + CD-ритм + оверлей диктовки)

Дата: 2026-09-05. Исполнитель: **agy / Gemini 3.1 Pro (High)**. Гейт (ревью диффа, тесты, parity-бинари): **Cursor / Grok** — не agy.

**Правки НЕ коммитить.** Работай в изолированном worktree от `origin/codex/krab-ear-v2`. Перед стартом: `git fetch origin` (локальный HEAD может отставать на ~2 коммита). Это **опциональный визуальный пакет**, не волна W-number; карточки в `docs/NOW.md` нет.

## Скоуп (одним абзацем)

Только внешний вид: Liquid Glass / Claude Design токены, типографика, ритм карточек, цвета статусов. **Никакого** нового поведения, IPC, ключей настроек, `sectionId`, проводки координатора, SSE, `syncSettingsControls`, бэкенда, launchd. Не трогать уже смерженные пакеты (#1983 CD-секции, #1987 STT memory labels, C2 meeting, C3 quick capture, Conversation tab). Не полировать мёртвые Telnyx-поля в `cdBuildCallAutomationSection` — их сначала вырежет Cursor.

## Контекст

- Панель `HistoryPanelController` собирается в двух вариантах: **Gemini** (`settingsBar`) и **Claude Design** (`settingsBarCD`). Выбор — `UserDefaults` ключ `KrabEar_UseClaudeDesign` (extension `useClaudeDesignVariant`).
- CD-рецепт карточки (образец — **не выдумывать заново**):
  - `HistoryPanelController+Settings+ClaudeDesign.swift`: `cdBuildRecordingSection()`, `cdMakeRow()`, `cdMakeSeparator()`, `cdMakeSliderRow()`, `cdMakeBadge()`, `CDSettingsCardView`.
  - Образец privacy-карточки: `HistoryPanelController+PrivacyDashboard.swift` → `cdBuildPrivacyDashboardSection()`.
- Правило CD: **те же экземпляры контролов**, что у Gemini; `addArrangedSubview` переносит view между барами — штатный механизм. Новые контролы с дублирующими `#selector` / IPC **запрещены**.
- Call Observer w1 (2026-08) был намеренно «функционален, не полирован» (`docs/design-briefs/2026-08-22-call-observer-polish.md`). Сейчас HUD всё ещё красит статус через `.systemYellow` / `.systemGray` / `.systemGreen`, а не через `KrabEarTheme.Colors.*`. Панель — каркас без выразительной «ленты карточек» транскрипта.
- **Не переделывать** Auto Layout overlap header панели Call Observer — это уже закрыто в #1995 на origin. Твоя зона — стили, не геометрия header vs scroll.

## Таблица работ (файл → строитель → конкретное визуальное изменение)

| Приоритет | Файл | Строитель / зона | Что сделать (визуал) |
|---|---|---|---|
| **P1** | `CallObserverHUD.swift` | `buildPanel()`, `HUDBackdropView`, `updateHUD` | Заменить `.systemYellow` / `.systemGray` / `.systemGreen` на токены (`Colors.warning`, `Colors.textDisabled` или приглушённый secondary, `Colors.success`). Статус-дот: CALayer/NSBox с `Metrics.innerCornerRadius`, лёгкое свечение через opacity (Reduce Motion). Кнопки `ThemeButton`: hover/pressed через существующий `KrabEarTheme.Interaction` (уже `isTransparentStyle`). Ритм: `Metrics.cardPadding`, `itemSpacing`. Тени/рамка: `Colors.border`, `Elevation.applyCard` на backdrop layer (не ломая `masksToBounds`). Бейджи mute/hold — компактные pill на `Typography.captionMedium` + `Colors.textSecondary`. |
| **P1** | `CallObserverPanelController.swift` | `buildUI()`, `row(for:)`, header stack | **Карточная лента** транскрипта: каждая реплика в «полоске» на `Colors.cardBackground` + `border` + `innerCornerRadius` (как bubble, но единый ритм с CD). Стороны: remote — leading + нейтральный фон; agent — trailing + `accent.withAlphaComponent(0.12)`. Перевод — `Typography.caption` / `textSecondary`; «прервано N %» — `textDisabled`, не кричащий. Header: `stateBadgeBox` как pill; `costAlertLabel` уже на `Colors.warning` — только ритм/spacing. 🔴 Панель **не** рисует empty-state при пустом `transcriptStack` (в отличие от HUD `· ждём реплик…`) — **не добавлять** новый placeholder/copy. Scroll: сохранить pin `transcriptStack.widthAnchor` к scrollView. |
| **P2** | `HistoryPanelController+WebhookManager.swift` | `cdBuildWebhookManagerSection`, `rebuildWebhookCard`, `makeWebhookRow` | CD-форма уже на `cdMakeRow` — **список** `makeWebhookRow` всё ещё Gemini (`NSFont.systemFont(13, .medium)`, голый `NSStackView`). В `rebuildWebhookCard` при `CDSettingsCardView`: рендерить строки в CD-ритме — `cdMakeSeparator()` между элементами, подзаголовок через `makeSubhead` → заменить на `Typography.caption` + uppercase tracking ИЛИ оставить текст subhead, но шрифт/цвет из токенов; URL — `Typography.body` medium; бейджи — `cdMakeBadge` где уместно. **Те же** `deleteButton` / `identifier` / `#selector(onUnregisterWebhook)`. |
| **P2** | `HistoryPanelController+RecordingScheduler.swift` | `cdBuildRecordingSchedulerSection`, `rebuildSchedulerCard`, `makeScheduleRow` | Аналогично webhooks: CD-форма готова, **ожидающие записи** — старый layout. CD-ветка списка: separator между строками, типографика токенами, кнопка «Отменить» — `ThemeSecondaryButton` или inline с `Interaction.disabledOpacity` при hover. |
| **P2** | `HistoryPanelController+STTEnginesPicker.swift` | `cdBuildSTTEnginesSection`, `rebuildCDSTTEnginesCard`, `makeCDSTTEngineRow`, `cdBuildGigaamTransportCard` | `makeCDSTTEngineRow` уже на `cdMakeRow` — выровнять loading/fallback шрифты: убрать голый `.systemFont(ofSize: 12)` → `Typography.caption` / `captionMedium`. Проверить ритм separator между движками. Transport card: warning label → `Typography.caption` + `Colors.warning` (как в Gemini-ветке). |
| **P3** | `HistoryPanelController+AllSettings.swift` | `cdBuildAllSettingsSection`, `makeAllSettingsRow`, `rebuildAllSettingsRows` | **Только типографика/ритм.** Лейбл строки остаётся **сырым IPC-ключом** (`makeSettingRow(label: key, …)` в Gemini; в CD — `cdMakeRow(label: key, control:)`). **Не** переводить 259 ключей на русский. Секреты: визуально отличить «задано/не задано» через `Colors.accent` / `textSecondary` (уже есть — можно усилить spacing). Общий `rebuildAllSettingsRows`/`makeAllSettingsRow` — ветвить по `UserDefaults.standard.useClaudeDesignVariant` (или эквивалент), **не** дублировать контролы; в CD-ветке: `cdMakeSeparator()` между строками в `rowsStack` (spacing сейчас `2` — подтянуть к `Metrics.tight`). Header CD: выровнять placeholder поиска с Gemini («Поиск по названию настройки» vs «Поиск по ключу» — **не менять смысл**, только визуальный ритм `headerRow`). |
| **P4** | `RealtimeOverlayController.swift` | `setupUI`, `setAudioLevel`, pulse/breathing | Glass refresh: `surfaceView`/`tintView`/`borderLayer` — `Colors.cardBackground`, `border`, `Elevation.applyOverlay`. Типографика: primary `Typography.display`, secondary `caption`/`captionMedium`. Level meter: улучшить `recordingDot` + `recordingDotHalo` (тонкая полоска или мягкий halo, без тяжёлой перерисовки на каждый RMS). Анимации — только через `KrabEarTheme.Motion.animate` / guard Reduce Motion. Recording dot — **CALayer**, не Unicode. |

## Карта проводки (контролы и associated objects)

### Call Observer (нет sectionId — standalone UI)

| Компонент | Stored / private | Координатор / протокол |
|---|---|---|
| `CallObserverHUD` | `panel`, `statusDot`, `statusLabel`, `badgesLabel`, `linesLabel`, `listenButton`, `hangupButton`, `closeButton`, `buttonActions` | `CallObserverHUDPresenting`; `coordinator?.userExpandedHUD()`, `userToggledListen()`, `userRequestedHangupFromHUD()`, `userClosedHUD()` |
| Test hooks | `testHook_listenButton`, `testHook_hangupButton` | **Не переименовывать** |
| `HUDClickView` | `onClick`, `downPoint` | `CallObserverHUD.isClick` — **не трогать** |
| `CallObserverPanelController` | `stateBadgeBox`, `stateBadge`, `costLabel`, `costAlertLabel`, `listenButton`, `hangupButton`, `transcriptStack`, `scrollView`, `sessionPicker`, `hangupSheetOpen` | `CallObserverPanelPresenting`; `#selector(onListenTapped)`, `onHangupTapped`, `onSessionPicked`; `presentAlertSheet` + `hangupSheetOpen` — **не ломать** |
| Test hooks | `testHook_stateBadgeText`, `testHook_transcriptPlainText` | `container.identifier = "transcript:" + legacyText` в `row(for:)` — **сохранить** для тестов |

### Webhooks (`HistoryPanelController+WebhookManager.swift`)

| Элемент | Хелпер / assoc key |
|---|---|
| URL field | `makeWebhookUrlField()` — в Gemini **локально** создаётся дубликат; CD зовёт хелпер. Assoc: `WebhookManagerAssocKeys.urlField` |
| Events | `makeWebhookEventsField()` → `eventsField` |
| Secret | `makeWebhookSecretField()` → `secretField` |
| Submit | `makeWebhookSubmitButton()` → `#selector(onRegisterWebhook(_:))` |
| Card ref | `WebhookManagerAssocKeys.sectionCard` → `ThemeCardView` или `CDSettingsCardView` |
| Список | `rebuildWebhookCard` → `makeWebhookRow` (общий для обоих card types) |

### Recording Scheduler

| Элемент | Хелпер / assoc key |
|---|---|
| Date | `makeSchedulerTimeField()` → `RecordingSchedulerAssocKeys.datePicker` |
| Duration | `makeSchedulerDurationField()` → `durationField` |
| Label | `makeSchedulerDescField()` → `labelField` |
| Submit | `makeSchedulerSubmitButton()` → `#selector(onScheduleRecording(_:))` |
| Card | `RecordingSchedulerAssocKeys.sectionCard` |

### STT Engines

| Элемент | Assoc key |
|---|---|
| Gemini engines card | `STTEnginesAssocKeys.enginesCard` |
| CD engines card | `STTEnginesAssocKeys.cdEnginesCard` |
| GigaAM transport (Gemini) | `gigaamTransportCard`, `gigaamTransportPicker`, `gigaamTransportWarnLabel` |
| GigaAM transport (CD) | `cdGigaamTransportCard`, `cdGigaamTransportPicker`, `cdGigaamTransportWarnLabel` |
| Toggle handler | `#selector(onSTTEngineToggleChanged(_:))` — `identifier` = `toggleKey` |
| Transport | `#selector(onGigaamTransportChanged(_:))` — **два** пикера, sync в `syncGigaamTransportControls` (не трогать) |

### All Settings

| Элемент | Assoc key |
|---|---|
| Search | `AllSettingsAssocKeys.searchField` |
| Rows stack | `AllSettingsAssocKeys.rowsStack` |
| Row index | `AllSettingsAssocKeys.rowIndex` |
| Status | `AllSettingsAssocKeys.statusLabel` |
| Loaded flag | `AllSettingsAssocKeys.loaded` |
| Secret gate | `isSecretSettingKey(_:)` — **не менять** |
| Handlers | `onAllSettingsSearchChanged`, `onAllSettingsReload`, `onAllSettingsToggle`, `onAllSettingsFieldCommitted` |

**CD invariant:** Gemini и CD секции All Settings **делят** одни assoc keys — одновременно активен только один бар, но дублировать контролы нельзя.

## Таблица sectionId (только затронутые секции настроек)

| RU заголовок | gemini_id (`sectionId`) | cd_id (`sectionId`) | Файл |
|---|---|---|---|
| Все настройки | `all_settings_table` | `cd_all_settings_table` | `+AllSettings.swift` |
| Webhooks | `webhook_manager` | `cd_webhook_manager` | `+WebhookManager.swift` |
| Запланированные записи | `recording_scheduler` | `cd_recording_scheduler` | `+RecordingScheduler.swift` |
| STT-движки | `dictation_stt_engines` | `cd_stt_engines` | `+STTEnginesPicker.swift` |

Call Observer HUD/Panel **не** CollapsibleSection — sectionId нет.

## Запрещено (нарушение = откат диффа целиком)

1. Менять сигнатуры / имена методов `CallObserverHUDPresenting`, `CallObserverPanelPresenting`, публичный API `RealtimeOverlayController` (см. ниже).
2. Трогать `CallObserverCoordinator.swift`, `main+CallObserver.swift`, IPC/WS, `VGSessionWatcher`, настройки Call Observer.
3. Новые `#selector`, IPC-методы, ключи `set_settings`, вызовы `ipcClient.call` (в т.ч. «удобные» новые ключи в UI).
4. Менять `sectionId`, `AgentSettings`, `syncSettingsControls()`, `get_settings` / `isSecretSettingKey` / логику `REDACTED`.
5. `runModal()` — только существующие `presentAlertSheet` в панели; не добавлять новые модалки.
6. Новые Unicode-глифы/эмодзи в Swift-строках (AGENT-J). SF Symbols — ок. Кириллица и уже живущие символы (`·`, `…`, `→`, `↔`) — ок.
7. Хардкод RGB/NSColor вне `KrabEarTheme` (исключение: `NSColor.black` в shadow helpers темы).
8. `Tests/`, `KrabEar/` backend, бинари `Krab Ear.app`, `native/runtime/`.
9. `RealtimeOverlayController+PartialSSE.swift`, `main+RealtimeOverlay.swift`.
10. Полировать Telnyx-строки в `HistoryPanelController+Settings+ClaudeDesign.swift` (`cdBuildCallAutomationSection`) — **OUT OF SCOPE**.
11. Переделывать header overlap панели (#1995).
12. Коммиты, push, копирование parity-бинарей.

## Токены KrabEarTheme (использовать по имени)

**Colors:** `windowBackground`, `cardBackground`, `accent`, `textPrimary`, `textSecondary`, `textDisabled`, `textTertiary`, `border`, `separator`, `success`, `error`, `warning`, `overlayShadow`

**Typography:** `display`, `sectionTitle`, `body`, `caption`, `captionMedium`, `monospace` (+ `.tabular()` для цифр таймера)

**Metrics:** `tight` (4), `standard` (8), `comfortable` (12), `spacious` (24), `cardCornerRadius` (12), `innerCornerRadius` (8), `controlHeight`, `sectionSpacing`, `itemSpacing`, `cardPadding`

**Interaction:** `hoverOverlayAlpha`, `pressedScale`, `pressedOverlayAlpha`, `disabledOpacity`, `transparentHoverAlpha`

**Motion:** `Duration.micro/short/standard/long`, `Easing.easeOut/easeIn/easeInOut/linear`, `Motion.animate(...)`

**Elevation:** `Elevation.applyCard`, `Elevation.applyOverlay`

**Компоненты:** `ThemeButton`, `ThemePrimaryButton`, `ThemeSecondaryButton`, `CDSettingsCardView`, `ThemeCardView`, `CollapsibleSectionView`, `KrabEarTheme.applyTheme(to:)`, `KrabEarTheme.styleCheckbox(_:)` (STT toggles)

## Файлы

| Категория | Пути |
|---|---|
| **Редактировать** | `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/CallObserverPanelController.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+AllSettings.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+WebhookManager.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecordingScheduler.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift` |
| | `native/KrabEarAgent/Sources/KrabEarAgent/RealtimeOverlayController.swift` |
| **Читать (образцы, не править без нужды)** | `KrabEarTheme.swift`, `HistoryPanelController+Settings+ClaudeDesign.swift`, `LiveSubtitlesOverlay.swift`, `MeetingLivePanelController.swift` (лента карточек), `docs/design-briefs/2026-08-22-call-observer-polish.md`, `docs/design-briefs/2026-06-15-dictation-overlay-refresh.md` |
| **Запрещено** | `KrabEar/**`, `Tests/**`, `CallObserverCoordinator.swift`, `RealtimeOverlayController+PartialSSE.swift`, `main+RealtimeOverlay.swift`, `HistoryPanelController.swift` (если не попросят отдельно), `+Settings+ClaudeDesign.swift` (кроме чтения), любые launchd/plist |

## Публичные контракты (не ломать)

### RealtimeOverlayController

```swift
public func show()
public func hide()
public func update(previewText: String, translatedText: String?, durationText: String, modeHint: String)
public func setOpacityPercent(_ value: Int)
public func setAudioLevel(_ rms: Float)   // rms 0…1, вызывается часто
func setPrimaryText(_ text: String)       // вызывается из +PartialSSE.swift
```

`showRevealAnimation` в текущем коде **отсутствует** (reveal внутренний через `revealTask` / `overlayState`) — **не восстанавливать** старую сигнатуру.

Приватные роли сохранить: `setupPanel`, `setupEffectView`, `setupUI`, pulse/breathing/drag, `panel.ignoresMouseEvents = true`, `.nonactivatingPanel` семантика.

### Call Observer test hooks

- HUD: `testHook_listenButton`, `testHook_hangupButton`
- Panel: `testHook_stateBadgeText`, `testHook_transcriptPlainText` (зависит от `identifier` вида `transcript:<legacyText>`)

## Definition of Done (выполни сам перед отчётом)

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -5
cd ../.. && python3 scripts/audit_orphan_panel_controls.py --fail-on-found
python3 scripts/audit_agent_settings_symmetry.py --fail-on-found
```

Все три — без ошибок.

**Отчёт agy (по-русски, коротко):**
- список изменённых файлов;
- по каждой зоне P1–P4 — что визуально изменено;
- подтверждение: публичные сигнатуры overlay **не менялись**; test hooks Call Observer на месте;
- подтверждение: recording dot — слой, не глиф; Reduce Motion учтён;
- последняя строка `swift build` (= `Build complete!` или ошибка).

## Гейт координатора (НЕ agy)

После диффа Cursor/Grok:

```bash
# runModal / sectionId / IPC / хардкод-цвета / новые глифы
rg 'runModal\(' native/KrabEarAgent/Sources --glob '*.swift' | rg -v 'PermissionWizard|allowlist'
rg 'sectionId:\s*"' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+AllSettings.swift
rg 'sectionId:\s*"' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+WebhookManager.swift
rg 'sectionId:\s*"' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+RecordingScheduler.swift
rg 'sectionId:\s*"' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift
rg '\.system(Yellow|Green|Gray|Red|Orange|Blue)' native/KrabEarAgent/Sources/KrabEarAgent/CallObserverHUD.swift
```

При правках HUD/Panel:

```bash
cd native/KrabEarAgent && swift test --filter CallObserverUITests 2>&1 | tail -20
```

**Не** копировать parity-бинари (`Krab Ear.app`, `native/runtime`) — это координатор после гейта.

## Баны playbook (§1 EXECUTOR_PLAYBOOK — копия для agy)

- База: `origin/codex/krab-ear-v2`. Не `audit/*`, не чужой WIP-чекаут.
- `git add` явными путями. Никогда `git add -A`.
- Не запускать собранный `KrabEarAgent` / `open "Krab Ear.app"` (`SingleInstanceGuard`).
- Не рестартить прод-backend; не трогать Main Krab / VG `.env`.
- Визуал Swift — ты (agy); IPC-ключи в UI не выдумывать.
- Секреты не печатать.
- Не коммитить.

## Модель и квота

- **Этот файл = один запрос** Gemini 3.1 Pro (High):

```bash
agy -p "$(cat docs/design-briefs/2026-09-05-visual-polish-pack.md)

ВЫПОЛНИ это ТЗ в worktree от origin/codex/krab-ear-v2. Только визуал. Не коммить." \
  --model "Gemini 3.1 Pro (High)" \
  --dangerously-skip-permissions \
  --add-dir "$(pwd)" \
  --print-timeout 40m < /dev/null > /tmp/krab-ear-gemini/visual-polish-pack.log 2>&1
```

- Slug `gemini-3.8-flash-high` **не** для этого layout-пака.
- Опциональный follow-up «только копирайт» — **отдельный короткий brief**, не этот файл.

## Перед началом ОБЯЗАТЕЛЬНО прочитать

1. Этот brief целиком.
2. Перечисленные Swift-файлы из таблицы работ (не гадать проводку).
3. `KrabEarTheme.swift` — точные имена токенов.
4. `CLAUDE.md` — секции Gemini/agy workflow, AGENT-J (glyph-guard), AGENT-3 (без sync IPC в UI), NSAlert/runModal, Reduce Motion.
