# Changelog

## v2.0.0 — 2026-04-12

Крупный релиз. Полный roadmap Krab Ear закрыт: 33+ коммита, 2114 тестов, 100+ новых компонентов.

---

### Новые функции

#### Интерфейс и визуальный стиль
- **Liquid Glass UI** — новая тема на базе `NSVisualEffectView` (`KrabEarTheme.swift`), ThemeCardView, ThemePrimaryButton, коллапсируемые секции с анимацией
- **RealtimeOverlay** — плавающий оверлей с live-превью транскрипта во время записи
- **GUI-кнопки управления AI** — переключатели LLM, диаризации, перевода прямо в главном окне
- **Коллапсируемые секции** (9 штук в 3 вкладках) с сохранением состояния в `NSUserDefaults`

#### Транскрипция и STT
- **Цепочка фоллбэков STT**: balanced → max candidates → remote (если разрешено сетью)
- **Диаризация на Metal GPU**: pyannote.audio + torch 2.11, авто-выбор `mps` на Apple Silicon
- **Фиксатор пунктуации** (`PunctuationFixer`) — постобработка знаков препинания
- **Контроль темпа речи** (`SpeechPaceAnalyzer`) — детектор слишком быстрой/медленной речи
- **Версионирование транскриптов** — хранение нескольких версий одной записи
- **Управление тишиной** — умное пропускание тихих участков, настройка `stop_tail_trim_ms` (0–1200 мс)
- **Хотворды** (`HotwordDetector`) — приоритетные слова для улучшения точности STT

#### Перевод
- Поддержка 6 режимов: `off`, `ru_to_es`, `es_to_ru`, `en_to_ru`, `auto`, `bilingual_ru_es`
- **Глоссарий с авто-обучением** — `get_glossary_suggestions` предлагает новые пары из истории
- **Стили перевода** — `neutral`, `chat`, `formal`
- **Сравнитель текстов** (`TextComparator`) — word-level diff оригинала и перевода

#### LLM-постобработка
- Интеграция с **LM Studio** (qwen3-4b-abliterated) через CircuitBreaker
- Защиты: chatbot guard + length ratio guard (< 35% или > 300% — отклонение)
- `get_last_llm_diff` — просмотр последнего word-level diff LLM-редактора

#### История и хранилище
- **Append-only NDJSON** с tombstone-удалениями и периодической компакцией
- **Теги** — добавление, удаление, поиск по тегу, счётчики использования
- **Коллекции** (`CollectionManager`) — группировка записей
- **Избранное** — через систему тегов
- **Буфер обмена** — последние 20 вставок, повторная вставка (`repaste_item`)
- **Fuzzy-поиск** по истории
- **Поиск по спикеру** (`search_by_speaker`) — фильтр по ID говорящего из диаризации
- **Фильтр по уверенности** (`filter_by_confidence`)
- **Авто-резервирование** (`AutoBackup`) — timestamped бэкапы с восстановлением
- **Дедупликация** — детектор и удаление дубликатов
- **Batch-суммаризация** (`auto_summarize_batch`) — LLM-суммари группы записей

#### Экспорт
- **SRT** (SubRip субтитры)
- **Markdown** (plain + с метаданными и диаризацией)
- **CSV** с заголовком
- **JSON** — структурированный экспорт
- **Obsidian** — `.md` с YAML frontmatter, совместимый с Obsidian Vault
- **Batch-экспорт** выбранных записей

#### CLI (`KrabEar/cli.py`)
- 6 команд: `status`, `history`, `export`, `stats`, `health`, `transcribe`
- ANSI-цвета, авто-определение пути к сокету, флаг `--socket`

#### REST API (порт 5005)
- OpenAPI/Swagger UI (`/api/docs`)
- Bearer-токен аутентификация
- Rate limiting (60 req/min, 10 req/min для STT)
- **SSE-поток** (`GET /v1/events`) — события `stt.final`, `stt.failed`
- **WebSocket** (`WS /ws/events`) — с фильтром по типу событий
- **Prometheus-метрики** (`GET /metrics/prometheus`)
- Идемпотентность через `chat_id` + `message_id`
- API versioning (`/v1/`)

#### Call Assist
- `start_call_assist` / `stop_call_assist` — сессия live-перевода звонков
- Timeline с экспортом, статистикой, LLM-суммари
- Quick phrases — быстрый перевод коротких фраз
- Интеграция с Voice Gateway WebSocket

#### Аналитика и мониторинг
- **MetricsCollector** — sliding-window latency (p50/p95/p99), confidence avg
- **Дашборд метрик** (`get_metrics_dashboard`) — snapshot текущей сессии
- **Статистика использования** — по дням, неделям, языкам
- **Error reporter** — кольцевой буфер ошибок, `get_error_report` / `get_error_stats`
- **Daily digest** (`DailyDigest`) — ежедневная сводка активности
- **Quality trends** (`QualityTrends`) — тренды качества распознавания
- **Period comparison** (`PeriodComparison`) — сравнение периодов
- **Word frequency analysis** — топ-N слов по частоте
- **Keyword cloud** (`KeywordCloud`) — облако ключевых слов
- **Readability scorer** — оценка читаемости транскриптов

#### Системные улучшения
- **Audit logger** — журнал всех IPC-операций
- **Input sanitizer** — очистка входных данных
- **IPC throttle** — защита от перегрузки бэкенда
- **Feature flags** — включение/отключение экспериментальных функций
- **Plugin system** (`PluginSystem`) — базовый API расширений
- **Webhook support** — уведомления о событиях STT во внешние сервисы
- **Structured JSON logging** — `LOG_FORMAT=json` для machine-readable логов
- **GitHub Actions CI** — автоматические тесты (pytest) и сборка Swift на каждый push/PR
- **Integrity checker** (`IntegrityChecker`) — проверка целостности файлов истории

#### Swift-агент
- Разбивка `HistoryPanelController.swift` (2920 → 2196 строк) на 7 extension-файлов
- `LaunchAgentManager` — установка/удаление launchd plist из GUI
- `SystemAudioDuckingService` — снижение громкости системы во время записи
- `NotificationService` — macOS-уведомления о низкой уверенности, ошибках
- `PermissionWizard` — мастер настройки разрешений Accessibility + Microphone
- Коллапсируемые секции с сохранением состояния (`CollapsibleSectionView`)

---

### Архитектурные изменения

- Извлечены 4 сервиса из `BackendService`: `HistoryService`, `TranslationService`, `SettingsService`, `CallAssistService` — удалено 762 строки дублирующегося кода
- `StateStore` рефакторинг: file-lock, компакция, импорт NDJSON
- `EventBus` — in-process pub/sub с типизированными событиями (`emit_typed`)
- `contracts/` — Pydantic-модели событий STT и Translation с JSON Schema экспортом
- Settings с 5-секундным TTL-кэшем в `SettingsService`

---

### Тесты

- **2114 тестов** (с 411 в начале цикла)
- Охват: AudioEngine, BackendService, HistoryService, TranslationService, SettingsService, EventBus, MetricsCollector, StateStore, TextUtils, PunctuationFixer, SpeakerManager, CollectionManager, все IPC-методы

---

### Breaking changes

- Путь сокета в production (launchd Variant B) изменён на `~/Library/Application Support/KrabEar/krabear.sock`
- Deprecated IPC-методы удалены (убраны в рефакторинге `service.py`)
- REST API теперь требует `/v1/` префикс для всех бизнес-эндпоинтов
