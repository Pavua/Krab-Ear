# GUI Design Brief — Krab Ear (для Gemini 3.1 Pro)

Дата: 2026-04-12  
Проект: `native/KrabEarAgent/Sources/KrabEarAgent/`  
Дизайн-система: `KrabEarTheme.swift` — Liquid Glass (NSVisualEffectView, `.hudWindow`)

---

## 1. Текущее состояние GUI

### Вкладка 0: «Диктовка» (`dictation`)

**Collapsible sections:**
- `dictationRecordingSection` — качество STT (balanced/max), cleanup (soft/strict), перевод, realtime preview, auto-paste, translate-and-paste, swap RU↔ES
- `dictationSystemSection` — режим (headless/menubar), hotkey, hotkey-профиль, сеть (offline/online), звук старта, буфер обмена (режим вставки), audio ducking (toggle + slider), overlay opacity (slider)
- `dictationAISection` — diarization toggle, LLM rewrite toggle, LLM model selector
- `profileAudioSection` — profile preset (selector + «Применить»), audio device selector, тест микрофона + RMS/peak label
- `diagnosticsSection` — 4 кнопки-запросы (Диагностика / Метрики / Статистика / Хранилище) + прокручиваемый `diagnosticsOutputView`
- `clipboardSection` — «Буфер обмена» (список последних 20) + «Вставить повторно»
- Превью истории (NSTextView, последние 3 транскрипта) + кнопка «Открыть историю»

### Вкладка 1: «Live перевод» (`live_translation`)

**Блоки:**
- Кнопка-пресет «Live Translation» (применяет auto + chat + realtime)
- Call Assist: Старт/Стоп звонка, capture source selector, уведомления, авто-summary
- Voice Gateway: URL field + API key field + «Проверить Gateway»
- Фразовая библиотека: selector (язык-пара), «Загрузить фразы», «Сказать фразу», поле ввода
- Timeline: «Summary», «Диагностика», «Оценка стоимости», «Timeline», «Экспорт Timeline», «Timeline → история», «Очистить Timeline», selector «Оставить последних»
- `callAssistOutputView` — прокручиваемый лог событий сессии
- Realtime status label + `realtimeTextView` (polling 0.9 с)

### Вкладка 2: «История» (`history`)

**Collapsible sections:**
- `historyFiltersSection` — фильтры: paste status, translation mode, translation status, дата от/до, badge счётчика активных фильтров
- `historyAdvancedSection` — экспорт (Markdown/SRT/CSV/NDJSON/Obsidian), компактация, чистка старых (days selector), vocab suggestions, glossary suggestions, авто-summary batch
- `historyImportSection` — import audio + drop zone, прогресс-бар, cancel/pause import, отчёт

**Основной список:** NSTableView (пагинация, двойной клик — детали, строка поиска, плотность normal/compact, page size 20/50/100/200)

**Кнопки под таблицей:** Копировать / Вставить выбранное / Копировать оригинал / Копировать перевод / Повторить перевод / Summary выбранного / Удалить / Показать ещё / К последней / Загрузить всё

**Bottom bars:** обзорный label (today_count, 24h, вставка ok/err, перевод ok/err) + статус label

---

## 2. IPC-методы без GUI

На основе `IPC_API_REFERENCE.md` следующие методы **не имеют** представления в GUI:

### Аналитика и статистика
| IPC-метод | Описание |
|---|---|
| `get_recording_stats` | Вызывается кнопкой «Статистика» → дамп в diagnosticsOutputView. Нет структурированного отображения. |
| `get_usage_stats` | Ежедневное использование (recordings/duration/words) — GUI отсутствует |
| `get_session_history` | История сессий с метаданными — GUI отсутствует |
| `get_session_stats` | Агрегированная по сессиям — GUI отсутствует |
| `get_error_report` | Последние ошибки из кольцевого буфера — GUI отсутствует |
| `get_error_stats` | Счётчики ошибок по компоненту/типу — GUI отсутствует |
| `word_frequency_analysis` | Топ N слов — GUI отсутствует |
| `get_history_statistics` | Агрегация по длительности/словам/датам — GUI отсутствует |

### Система и здоровье
| IPC-метод | Описание |
|---|---|
| `health_check` | Агрегированный статус подсистем — GUI отсутствует |
| `get_last_llm_diff` | Последний word-level diff LLM-перезаписи — GUI отсутствует |
| `get_context_memory` | Контекст-память STT — GUI отсутствует |

### Управление историей (продвинутое)
| IPC-метод | Описание |
|---|---|
| `filter_by_confidence` | Фильтр по confidence STT — GUI отсутствует |
| `search_by_speaker` | Поиск по diarization-спикеру — GUI отсутствует |
| `search_annotations` | Полнотекстовый поиск по заметкам — GUI отсутствует |
| `toggle_favorite` / `get_favorites` / `is_favorite` | Избранное — GUI отсутствует |
| `set_annotation` / `get_annotation` | Пользовательские заметки к записям — GUI отсутствует |
| `create_collection` / `list_collections` / `add_to_collection` / `get_collection_items` | Коллекции — GUI отсутствует |
| `start_chain` / `add_to_chain` / `merge_chain_text` / `list_chains` | Цепочки записей — GUI отсутствует |
| `save_transcript_version` / `get_transcript_versions` / `revert_transcript_version` | Версии транскриптов — GUI отсутствует |
| `backup_history` / `restore_history` / `list_backups` | Резервные копии — GUI отсутствует |

### Текстовой анализ
| IPC-метод | Описание |
|---|---|
| `extract_terms` | Ключевые термины из текста — GUI отсутствует |
| `get_topic_timeline` | Смены тем в истории — GUI отсутствует |
| `get_keyword_cloud` | Данные облака слов — GUI отсутствует |
| `score_transcription` | Оценка качества 0–100 / A–F — GUI отсутствует |
| `analyze_speech_pace` | Темп речи WPM — GUI отсутствует |
| `score_readability` | Readability scoring — GUI отсутствует |
| `detect_emotion` | Эмоция в транскрипте — GUI отсутствует |

### Экспорт (расширенный)
| IPC-метод | Описание |
|---|---|
| `export_html_report` | HTML-отчёт аналитики — GUI отсутствует |
| `batch_export` | Пакетный экспорт в несколько форматов — GUI отсутствует |
| `export_settings` / `import_settings` | Экспорт/импорт настроек — GUI отсутствует |
| `prepare_share` | Пакет для шаринга — GUI отсутствует |

### Obsidian Sync
| IPC-метод | Описание |
|---|---|
| `configure_obsidian_sync` / `run_obsidian_sync` / `get_obsidian_sync_status` | Синхронизация с Obsidian vault — GUI отсутствует |

### Очередь транскрипций
| IPC-метод | Описание |
|---|---|
| `list_transcription_queue` / `enqueue_transcription` / `cancel_transcription` / `get_queue_status` | Очередь заданий — частично скрыта за import flow, нет явного UI |

---

## 3. Предлагаемые новые секции

### Вкладка «Диктовка» — новая секция «Аналитика»

**CollapsibleSectionView**, `sectionId: "dictationAnalytics"`, заголовок: «Аналитика»

Содержимое (ThemeCardView):

#### Блок «Обзор использования»
- **Label**: «Сегодня / Неделя / Всего» — 3 inline-лейбла в NSStackView горизонтально
  - IPC: `get_recording_stats` → поля `today_count`, `week_count`, `total_count`, `today_duration_sec`
  - Обновление: при открытии секции + кнопка «Обновить»
  - Компонент: NSTextField(labelWithString), `Typography.monospaced`, 3 колонки по 120px

#### Блок «Использование по дням»
- **Label**: «За 7 дней: X записей, Y минут»
  - IPC: `get_usage_stats` params `{days: 7}` → `{daily: [{date, count, duration_sec, word_count}]}`
  - Компонент: NSTextView (readonly, 5 строк, monospaced) для отображения таблицы

#### Блок «Ошибки»
- **Button** «Отчёт об ошибках» → вызывает `get_error_stats` params `{}` → `{errors_by_component, errors_by_type}` → показ в diagnosticsOutputView
- **Button** «Последние ошибки» → `get_error_report` params `{limit: 10}` → показ в diagnosticsOutputView
  - Компоненты: ThemeSecondaryButton, размещение в горизонтальном NSStackView

#### Блок «Темп и качество (выбранная запись)»
- **Button** «Темп речи» → `analyze_speech_pace` params `{text: selectedItem.text, duration_sec: selectedItem.duration}` → показ `{words_per_minute, pace_category}` в label
- **Button** «Оценка качества» → `score_transcription` params `{text, confidence, duration_sec}` → показ `{overall_score, grade}` в label
  - Компоненты: ThemeSecondaryButton + NSTextField label «WPM: — | Оценка: —»

---

### Вкладка «Диктовка» — новая секция «Система»

**CollapsibleSectionView**, `sectionId: "dictationSystem"`, заголовок: «Система»

Содержимое (ThemeCardView):

#### Блок «Здоровье подсистем»
- **Button** «Проверить здоровье» → `health_check` → `{status, components: {stt, llm, history, translation}}` → цветовые индикаторы (NSTextField с цветом: зелёный/оранжевый/красный из KrabEarTheme.Colors)
- **Grid из 4 label**: STT / LLM / История / Перевод — каждый с иконкой-статусом (● зелёный / ● оранжевый / ● красный)
  - Компоненты: NSStackView горизонтальный, NSTextField textColor = success/warning/error

#### Блок «LLM diff»
- **Button** «Последний LLM diff» → `get_last_llm_diff` → `{original, rewritten, diff_tokens}` → показ в diagnosticsOutputView
  - Компонент: ThemeSecondaryButton

#### Блок «Контекст-память STT»
- **Button** «Контекст STT» → `get_context_memory` params `{}` → `{context_words, recent_topics, size}` → показ в diagnosticsOutputView
- **Button** «Сбросить контекст» → `get_context_memory` params `{clear: true}`
  - Компоненты: ThemeSecondaryButton × 2 в горизонтальном NSStackView

#### Блок «Настройки»
- **Button** «Экспорт настроек» → `export_settings` → сохранить через NSSavePanel
- **Button** «Импорт настроек» → NSOpenPanel → `import_settings` params `{file_path}`
  - Компоненты: ThemeSecondaryButton × 2

> **Примечание**: существующую `dictationSystemSection` (hotkey, сеть, audio ducking) переименовать в «Поведение» (`sectionId: "dictationBehavior"`), чтобы не путать с новой секцией «Система».

---

### Вкладка «История» — новая секция «Управление»

**CollapsibleSectionView**, `sectionId: "historyManagement"`, заголовок: «Управление»

Содержимое (ThemeCardView):

#### Блок «Избранное»
- **Button** «Добавить в избранное» → `toggle_favorite` params `{id: selectedItem.id}`; title меняется при повторном вызове
- **Button** «Показать избранное» → `get_favorites` → `{items: [...]}` → загрузка результата в tableView поверх текущего списка
  - Компоненты: ThemeSecondaryButton × 2, горизонтальный NSStackView

#### Блок «Заметки»
- **NSTextField** (редактируемый, placeholder «Заметка к записи...»)
- **Button** «Сохранить» → `set_annotation` params `{id, text: annotationField.stringValue}`
- **Button** «Поиск по заметкам» → `search_annotations` params `{query: searchField.stringValue}` → результаты в tableView
  - Компоненты: NSTextField + ThemePrimaryButton «Сохранить» + ThemeSecondaryButton «Поиск»

#### Блок «Резервные копии»
- **Button** «Создать резервную копию» → `backup_history` → `{path, size}` → уведомление NotificationService
- **Button** «Список копий» → `list_backups` → `{backups: [{path, ts, size}]}` → показ в diagnosticsOutputView (на вкладку Диктовка)
- **Button** «Восстановить» → `list_backups` → NSAlert с picker → `restore_history` params `{path}`
  - Компоненты: ThemePrimaryButton + ThemeSecondaryButton × 2

#### Блок «Версии транскриптов»
- **Button** «Версии» → `get_transcript_versions` params `{item_id}` → `{versions: [{version_id, text, ts}]}` → NSAlert с NSPopUpButton выбора версии → «Откатить» → `revert_transcript_version` params `{item_id, version_id}`
- **Button** «Сохранить версию» → `save_transcript_version` params `{item_id, text: item.text}`
  - Компоненты: ThemeSecondaryButton × 2

#### Блок «Коллекции»
- **NSPopUpButton** (список коллекций, «Загрузить» через `list_collections`)
- **Button** «Добавить в коллекцию» → `add_to_collection` params `{collection_id: selectedCollection, item_id}`
- **Button** «Новая коллекция» → NSAlert с NSTextField → `create_collection` params `{name}`
- **Button** «Показать коллекцию» → `get_collection_items` params `{collection_id}` → загрузка в tableView
  - Компоненты: NSPopUpButton + ThemeSecondaryButton × 3

#### Блок «Поиск по спикеру»
- **NSTextField** (placeholder «Speaker ID, напр. SPEAKER_00»)
- **Button** «Найти» → `search_by_speaker` params `{speaker_id: speakerField.stringValue}` → загрузка результатов в tableView
  - Компоненты: NSTextField + ThemePrimaryButton

---

### Вкладка «История» — новая секция «Статистика»

**CollapsibleSectionView**, `sectionId: "historyStats"`, заголовок: «Статистика»

Содержимое (ThemeCardView):

#### Блок «Частотный анализ»
- **NSPopUpButton** выбора N (10 / 25 / 50 слов), label «Топ слов»
- **Button** «Анализ» → `word_frequency_analysis` params `{limit: selectedN}` → `{words: [{word, count}]}` → NSTextView (readonly) с отсортированным списком
  - Компоненты: NSPopUpButton + ThemePrimaryButton «Анализ» + NSScrollView(NSTextView, 8 строк)

#### Блок «Сводная статистика»
- **Button** «Подробная статистика» → `get_history_statistics` → `{total_items, total_words, total_duration_sec, avg_duration_sec, date_range_start, date_range_end}` → NSTextView
- **Button** «Сессии» → `get_session_stats` → `{total_sessions, avg_duration_sec, total_words}` → NSTextView
  - Компоненты: ThemeSecondaryButton × 2, общий NSScrollView(NSTextView, 6 строк)

#### Блок «Темы»
- **Button** «Timeline тем» → `get_topic_timeline` params `{days: 7}` → `{segments: [{topic, start_ts, items}]}` → NSTextView с форматированным списком смен тем
  - Компонент: ThemeSecondaryButton + NSScrollView(NSTextView, 6 строк)

#### Блок «Облако слов (данные)»
- **Button** «Облако слов» → `get_keyword_cloud` params `{max_words: 50}` → `{words: [{word, count, weight}]}` → NSTextView (top-30 с весами в формате «слово (вес)»)
  - Компонент: ThemeSecondaryButton + NSScrollView(NSTextView, 6 строк)

---

## 4. Детальная спецификация элементов

### Паттерны реализации (из существующего кода)

```swift
// Стандартный collapsible block
let section = CollapsibleSectionView(sectionId: "mySection", title: "Заголовок")
let card = ThemeCardView()
card.title = ""
// ... добавить контролы в card.contentStackView
section.contentStackView.addArrangedSubview(card)

// Кнопка-запрос
let btn = ThemeSecondaryButton()
btn.title = "Название"
btn.target = self
btn.action = #selector(onSomething)

// Label результата
let resultLabel = NSTextField(labelWithString: "—")
resultLabel.font = KrabEarTheme.Typography.smallCaption
resultLabel.textColor = KrabEarTheme.Colors.textSecondary
```

### Полная таблица соответствия IPC → компонент

| IPC-метод | Секция | Триггер | Компонент результата |
|---|---|---|---|
| `health_check` | Диктовка / Система | Button «Проверить здоровье» | 4 цветных NSTextField label |
| `get_last_llm_diff` | Диктовка / Система | Button «LLM diff» | diagnosticsOutputView |
| `get_context_memory` | Диктовка / Система | Button «Контекст STT» | diagnosticsOutputView |
| `get_usage_stats` | Диктовка / Аналитика | Button «Обновить» | NSTextView 5 строк |
| `get_error_stats` | Диктовка / Аналитика | Button «Отчёт ошибок» | diagnosticsOutputView |
| `get_error_report` | Диктовка / Аналитика | Button «Последние ошибки» | diagnosticsOutputView |
| `analyze_speech_pace` | Диктовка / Аналитика | Button «Темп речи» | inline NSTextField label |
| `score_transcription` | Диктовка / Аналитика | Button «Оценка» | inline NSTextField label |
| `export_settings` | Диктовка / Система | Button | NSSavePanel |
| `import_settings` | Диктовка / Система | Button | NSOpenPanel |
| `toggle_favorite` | История / Управление | Button | title-toggle кнопки |
| `get_favorites` | История / Управление | Button | tableView (replace) |
| `set_annotation` | История / Управление | NSTextField + Button | inline confirmation label |
| `search_annotations` | История / Управление | Button | tableView (replace) |
| `backup_history` | История / Управление | Button | NotificationService |
| `list_backups` | История / Управление | Button | diagnosticsOutputView |
| `restore_history` | История / Управление | Button | NSAlert picker |
| `get_transcript_versions` | История / Управление | Button | NSAlert + NSPopUpButton |
| `revert_transcript_version` | История / Управление | Alert action | — |
| `save_transcript_version` | История / Управление | Button | inline confirmation |
| `create_collection` | История / Управление | Button | NSAlert + NSTextField |
| `list_collections` | История / Управление | Auto (onExpand) | NSPopUpButton |
| `add_to_collection` | История / Управление | Button | NotificationService |
| `get_collection_items` | История / Управление | Button | tableView (replace) |
| `search_by_speaker` | История / Управление | Button | tableView (replace) |
| `word_frequency_analysis` | История / Статистика | Button | NSTextView 8 строк |
| `get_history_statistics` | История / Статистика | Button | NSTextView 6 строк |
| `get_session_stats` | История / Статистика | Button | NSTextView 6 строк |
| `get_topic_timeline` | История / Статистика | Button | NSTextView 6 строк |
| `get_keyword_cloud` | История / Статистика | Button | NSTextView 6 строк |

---

## 5. Дизайн-ограничения

### Обязательные паттерны (из KrabEarTheme.swift)
- Все карточки: `ThemeCardView` (material `.hudWindow`, cornerRadius 10, border 0.5px separator)
- Все секции: `CollapsibleSectionView` с `sectionId` (UserDefaults-персистентность состояния)
- Кнопки-действия: `ThemePrimaryButton` (акцентный цвет) для основных действий, `ThemeSecondaryButton` для вторичных
- Шрифты: `sectionTitle` (bold 13) для заголовков карточек, `controlLabel` (12) для кнопок, `smallCaption` (10) для статус-лейблов, `monospaced` для числовых значений
- Цвета статусов: `Colors.success` (зелёный), `Colors.warning` (оранжевый), `Colors.error` (красный)
- Отступы: `cardPadding = 12`, `itemSpacing = 8`, `sectionSpacing = 16`
- Все тексты интерфейса — на русском языке

### Ограничения компоновки
- Максимальная ширина окна: 1200px (`NSWindow` minSize constraint)
- NSTextView (readonly) внутри NSScrollView — минимальная высота 80px, максимальная 200px
- Все inline-результаты через `diagnosticsOutputView` (уже существует в `dictationRecordingSection` зоне) — не создавать дублирующих scroll views, если вывод идёт туда
- Новые секции «Управление» и «Статистика» размещаются **ниже** существующих `historyFiltersSection`, `historyAdvancedSection`, `historyImportSection`
- Новые секции «Аналитика» и «Система» размещаются **после** существующих `diagnosticsSection` и `clipboardSection` на вкладке «Диктовка»

### Паттерн загрузки данных
- Тяжёлые IPC-вызовы (сессии, статистика, топик-таймлайн) выполнять через `DispatchQueue.global(qos: .userInitiated).async` с `DispatchQueue.main.async` для обновления UI
- Кнопки делать `isEnabled = false` на время запроса, восстанавливать после ответа
- Все ошибки — через `showDiagnosticsOutput("Ошибка: ...")` (существующий метод)
- Данные, нужные при открытии секции — загружать в `didExpand` (через overriding `setExpanded` в extension)

### IPC-вызовы, требующие `selectedRow`
Методы `toggle_favorite`, `set_annotation`, `search_by_speaker`, `save_transcript_version`, `get_transcript_versions` — требуют выбранной строки в `tableView`. Если `tableView.selectedRow < 0` — показывать `showInfoAlert(title:body:)` с подсказкой выбрать запись.
