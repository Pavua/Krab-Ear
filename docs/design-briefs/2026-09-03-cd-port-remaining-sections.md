# Design Brief: порт 13 секций панели настроек на карточки Claude Design

Дата: 2026-09-03. Исполнитель: Gemini 3.1 Pro (agy). Ревью и гейт: Claude.
Скоуп: ТОЛЬКО внешний вид секций в варианте Claude Design. Поведение, проводка,
IPC-ключи, селекторы — не меняются ни на символ.

## Контекст

Панель `HistoryPanelController` собирается в двух вариантах: «Gemini»
(`settingsBar`) и «Claude Design» (`settingsBarCD`), выбор — UserDefaults-ключ
`KrabEar_UseClaudeDesign` (у владельца включён CD). CD-вариант — компактные
карточки `CDSettingsCardView` со строками `cdMakeRow` / `cdMakeSliderRow` и
разделителями `cdMakeSeparator` (файл `HistoryPanelController+Settings+ClaudeDesign.swift`,
образец — `cdBuildRecordingSection()` там же, и `cdBuildPrivacyDashboardSection()`
в `HistoryPanelController+PrivacyDashboard.swift`).

02.09 в CD-стек были добавлены 13 секций, у которых своей CD-версии не было — они
вставлены «как есть», в Gemini-виде (`makeSettingRow`/`makeSwitchRow`, карточки
12pt), и визуально выбиваются из остальной CD-панели. Место вставки —
`HistoryPanelController.swift`, ветка `if UserDefaults.standard.useClaudeDesignVariant {`,
заголовок `makeCategoryHeader(text: "Ещё настройки")` и цикл `for section in [ … ]`.

## Задача

Для каждой из 13 секций написать `cdBuild<Имя>Section() -> CollapsibleSectionView`
в ТОМ ЖЕ extension-файле, где живёт её Gemini-строитель, и в CD-ветке заменить
элемент цикла на вызов CD-строителя.

| Gemini-строитель | файл | CD-строитель (создать) |
|---|---|---|
| `buildAudioPipelineSection` | `+Settings.swift:861` | `cdBuildAudioPipelineSection` |
| `setupDictationProfileAudioSection` | `+ApplyTheme+DictationSections.swift:71` | `cdBuildDictationProfileAudioSection` |
| `buildSystemSection` | `+Settings.swift:1142` | `cdBuildSystemSection` |
| `setupDictationClipboardSection` | `+ApplyTheme+DictationSections.swift:118` | `cdBuildDictationClipboardSection` |
| `buildQuickCaptureSection` | `+Settings.swift:1553` | `cdBuildQuickCaptureSection` |
| `buildQuickPresetSection` | `+Settings.swift:1507` | `cdBuildQuickPresetSection` |
| `buildSelectionTranslatorSection` | `+SelectionTranslator.swift:57` | `cdBuildSelectionTranslatorSection` |
| `buildVoiceAssistantSection` | `+Settings.swift:1267` | `cdBuildVoiceAssistantSection` |
| `buildRecordingSchedulerSection` | `+RecordingScheduler.swift:28` | `cdBuildRecordingSchedulerSection` |
| `buildWebhookManagerSection` | `+WebhookManager.swift:31` | `cdBuildWebhookManagerSection` |
| `buildCallObserverSettingsSection` | `+LiveSubsSettings.swift:44` | `cdBuildCallObserverSettingsSection` |
| `buildSTTModelMemorySection` | `+STTModelMemory.swift:48` | `cdBuildSTTModelMemorySection` |
| `buildAllSettingsSection` | `+AllSettings.swift:67` | `cdBuildAllSettingsSection` |

Правило построения CD-версии (ровно как `cdBuildRecordingSection`):
- `CollapsibleSectionView(sectionId: "cd_<snake_name>", title: <тот же русский заголовок>, isExpanded: <как у Gemini-версии>)`;
- одна `CDSettingsCardView`, строки `cdMakeRow(label:control:)`, слайдеры —
  `cdMakeSliderRow`, между строками `cdMakeSeparator()`; заголовок строки —
  короткий русский лейбл (в Gemini он часто стоит в `title` чекбокса — у CD
  чекбокс становится `setButtonType(.switch)` с пустым `title`, текст уходит в лейбл);
- В карточку кладутся ТЕ ЖЕ экземпляры контролов, что использует Gemini-версия
  (stored-свойства контроллера: `audioDeviceSelector`, `overlayFollowCursorButton`
  и т. д.). `addArrangedSubview` переносит view из скрытого Gemini-бара — это
  штатный механизм, так работают все существующие `cdBuild…`.
- Если Gemini-строитель создаёт контрол ЛОКАЛЬНО и там же вешает `target`/`action`
  или `#selector`, НЕ дублируй создание в CD-версии: вынеси создание этого
  контрола в общий приватный хелпер `make<Имя>Control()` в том же файле, зови его
  из обоих строителей. Селектор, ключ настройки и действие копируются буква в
  букву из Gemini-версии; новых `#selector`, новых ключей `set_settings`, новых
  IPC-методов быть не должно.
- Кнопки-действия (`ThemePrimaryButton`/`ThemeSecondaryButton`) остаются теми же
  экземплярами; в CD-строке они идут как `control:`.
- Списки/таблицы (`allSettingsSection`: таблица + поиск + кнопка обновления;
  вебхуки, планировщик) — в карточку целиком, без переделки внутренней вёрстки
  таблиц; достаточно CD-обрамления и CD-строк для управляющих элементов.
- Тёмная/светлая тема: только токены `KrabEarTheme.Colors.*`, никаких хардкод-цветов.

## Что НЕЛЬЗЯ ломать (за нарушение дифф отклоняется целиком)

1. **Никаких новых `#selector`, ключей настроек, IPC-методов, `ipcClient.call`**.
   Проводка контролов существующая; Claude сверит каждый ключ с бэкенд-хендлером.
2. **Не трогать** `AgentSettings` (`Models.swift`), `syncSettingsControls()`,
   обработчики `@objc func on…`, `applySettingsPatch`, `persistSettingsPayload`.
3. **Не удалять и не переименовывать** Gemini-строители и stored-свойства
   контролов — Gemini-вариант остаётся рабочим; гард
   `scripts/audit_orphan_panel_controls.py --fail-on-found` обязан остаться CLEAN.
4. В `HistoryPanelController.swift` меняется ТОЛЬКО содержимое цикла
   `for section in [ … ]` в CD-ветке (13 элементов → 13 вызовов `cdBuild…()`)
   и комментарий над ним (убрать фразу про «геминиевский вид» и «отдельную задачу»).
   Строки `let <name>Section = …` ДО развилки остаются — их проверяет тест.
5. **`runModal()` запрещён** (AppHang-класс); `NSAlert`/панели — только через
   `presentAlertSheet`/`presentPanelSheet`, но их ты и не должен трогать.
6. **Новые не-ASCII глифы/эмодзи в Swift — нельзя** (в проекте был AppHang на
   рендере глифа CoreText). Только текст, уже присутствующий в Gemini-версии.
7. Ничего не менять в `Tests/` — тесты правит Claude.
8. **Swift 6 strict concurrency**: новые функции — `@MainActor`, как соседние.

## Definition of Done (выполни сам перед отчётом)

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3
cd ../.. && python3 scripts/audit_orphan_panel_controls.py --fail-on-found | tail -2
python3 scripts/audit_agent_settings_symmetry.py --fail-on-found | tail -1
```
Все три — без ошибок. В конце отчёта: список изменённых файлов и по каждой
секции — какие контролы перенесены и какие хелперы `make…Control()` вынесены.
Отчёт — по-русски, коротко.
