# A1 — Рекомендованная настройка в один тап — ЧЕРНОВИК

> ## СТАТУС: ЧЕРНОВИК Sonnet-воркера, ждёт гейта контролёра и решений владельца.
> Этот документ НЕ утверждён. Ничего из него не реализовано. Все вердикты в таблицах —
> предложение автора черновика, не решение. Секция 9 «Открытые вопросы» обязательна к
> прочтению перед любой имплементацией.

Дата: 2026-07-07 · Волна: `docs/ROADMAP-2026H2.md` §2 Волна 1 (`### Волна 1 — A1 «Рекомендованная
настройка в один тап»`) · Автор черновика: Sonnet-воркер (research-only сессия, без git-операций
и без запуска бинарей — только чтение кода).

Метод: полная инвентаризация `KrabEar/core/config.py::DEFAULT_SETTINGS` + сопоставление с
`Settings(BaseSettings)` (тот же файл) + grep каждого кандидата в обеих формах (`lower_case` для
runtime-словаря и `UPPER_CASE` для pydantic-полей) по всему `KrabEar/` и `native/KrabEarAgent/`,
включая тесты (тест — это тоже свидетельство «живое ли»). Ни один вердикт в §4 не поставлен
только по имени/комментарию — для каждого «ДА»/«МЁРТВЫЙ» ниже указан файл:строка фактического
чтения (или его отсутствия).

---

## 1. Ключевая архитектурная находка: два параллельных механизма настроек

Прежде чем читать таблицы — это важно для всего дизайна `apply_recommended_setup`.

В Krab Ear есть **два** живых, но структурно разных хранилища одних и тех же логических
настроек:

- **Механизм A — Pydantic `Settings`** (`KrabEar/core/config.py:64`, singleton `settings =
  _build_settings()` на строке 718). Поля `UPPER_CASE` (`SMART_SILENCE_SKIP_ENABLED`,
  `VOXTRAL_ENABLED`, …), читаются низкоуровневым кодом (`core/engine.py`, `core/stt_router.py`)
  напрямую как атрибуты: `settings.SMART_SILENCE_SKIP_ENABLED`.
- **Механизм B — `DEFAULT_SETTINGS` dict + `settings.json`** (`core/config.py:752`), управляется
  `SettingsService` (`backend/settings_service.py`), читается через `cached_settings()` /
  `self._get_runtime_setting(key, default)` (определение: `backend/service.py`, метод
  `_get_runtime_setting`, TTL-кэш 5 сек).

**Хорошая новость**: они не рассинхронизированы структурно. `core/config.py:721`
`reload_settings_from_json()` вызывается из **единой точки** `SettingsService._reload_and_fire_hooks()`
(`backend/settings_service.py:166`) **после каждого из 5 путей сохранения** (`set_settings`,
`apply_profile_preset`, `set_notification_preferences`, `import_settings`,
`restore_settings_backup`). Он читает `settings.json`, аплоадит ключи в `UPPER_CASE` и делает
`setattr(settings, key, value)` на живом singleton — то есть механизм A **обычно** подхватывает
изменения из механизма B без рестарта бэкенда.

**Плохая новость / зона риска, найденная в этом черновике**: этот мост работает, только если
код, использующий значение, **перечитывает** `settings.<ПОЛЕ>` при каждом вызове. Если какой-то
коллаборатор один раз прочитал значение в конструкторе и закэшировал его в `self._enabled`, то
`setattr` на singleton ничего не меняет — коллаборатор продолжает жить со старым значением до
рестарта backend-процесса. Конкретный подтверждённый случай — `semantic_search_enabled`, см. §4
и находку в §3.2. Это должно быть учтено в контракте `apply_recommended_setup` (см. §5.4,
`restart_required`).

**Дополнительный риск, не проверенный в этом черновике до конца**: `rest_server.py` — это
**отдельный процесс** с собственным импортом `core.config.settings` (отдельный объект в памяти).
`reload_settings_from_json()` вызывается только из `SettingsService`, которым владеет
`BackendService` в IPC-процессе (`service.py`). Не подтверждено, вызывает ли что-то эквивалентное
hot-reload **внутри** процесса `rest_server.py`. Для кандидатов, чьё поведение живёт в REST-процессе
(например `rest_api_auth_enabled`), это отдельный источник «требует рестарта REST, а не только
IPC-бэкенда» — см. открытый вопрос в §9.

---

## 2. Существующие строительные блоки (переиспользовать, не изобретать заново)

| Блок | Файл | Что даёт |
|---|---|---|
| `detect_hardware_profile()` | `KrabEar/core/hardware_profile.py:83` | chip/ram_gb/cores/tier (low<16 / mid 16-32 / high>32 ГБ), уже используется `get_hardware_profile`/`get_calibration_recommendation` |
| `get_hardware_profile` IPC | `backend/service.py:4682` (`_handle_get_hardware_profile`) | Без privacy-гейта (не читает пользовательские данные) |
| `get_calibration_recommendation` IPC | `backend/service.py:4703` | Рекомендует STT-модель/движок по tier, уже читает mic SNR из кэша настроек |
| `apply_profile_preset` (образец!) | `backend/settings_service.py:535` (`handle_apply_profile_preset`) | Паттерн: `old_settings = cached_settings()` → merge → `save_settings` → `invalidate_cache()` → EventBus `preset.changed` → `_reload_and_fire_hooks()`. Новый метод должен буквально повторить этот скелет |
| Снапшот настроек | `backend/settings_backup.py` | `SettingsBackup.create_backup(dict, reason) → backup_id`, `restore_backup(backup_id) → dict`, `list_backups(limit)`. **Уже используется** в `_handle_set_settings_locked` (`reason="before_set"`, `settings_service.py:317`) |
| Восстановление снапшота (готовый IPC!) | `backend/settings_service.py:776` (`handle_restore_settings_backup`) | Уже делает pre-restore backup, миграцию схемы, валидацию, rollback при ошибке, редактирование секретов. **Откат для нового IPC не нужно изобретать — вызвать существующий `restore_settings_backup {backup_id}`** |
| Валидатор/схема | `backend/settings_validator.py` (`SettingsValidator`, `CURRENT_SCHEMA_VERSION="2.0"`) | Прогоняется в конце `_handle_set_settings_locked` — новый метод обязан прогонять то же |
| Онбординг (образец шага) | `native/.../ModelDownloadStep.swift` + `main.swift:1417` (`runModelDownloadStepThenComplete`) | Неблокирующий sheet, off-main IPC-поллинг прогресса, «Позже» = graceful skip |
| Settings-секция (образец) | `native/.../HistoryPanelController+Calibration.swift` | Dual Gemini/CD варианты, off-main IPC, associated-object паттерн, уже потребляет `get_hardware_profile`+`get_calibration_recommendation` |

---

## 3. Инвентаризация — все 39 найденных «выключено по умолчанию» кандидатов

Найдено расширенным grep'ом (обе формы имени, весь `KrabEar/` + `native/`, не по памяти):
**35** булевых `False` в `DEFAULT_SETTINGS` (`core/config.py:752-1111`) + **2** режима `"off"`/`"disabled"`
(`translation_mode`, `wake_word_engine`) + **2** настройки, которые существуют **только на стороне
Swift** и отсутствуют в Python `DEFAULT_SETTINGS` вовсе (`streaming_paste_enabled`,
`paste_undo_enabled` — см. находку §3.3). Итого **39**, не ~29 из роадмапа — расхождение
разобрано как открытый вопрос в §9.1.

### 3.1 Полная таблица (что делает, где реально читается)

Обозначения статуса: **ЖИВОЙ** — подтверждено чтение в проде; **МЁРТВЫЙ** — определён, но нигде
не влияет на поведение; **ЖИВОЙ⚠РЕСТАРТ** — читается, но только один раз при конструировании
коллаборатора, hot-reload моста (§1) не достигает.

| # | Настройка | Что делает | Где реально читается | Статус |
|---|---|---|---|---|
| 1 | `auto_start_enabled` | Автозапуск при логине | `backend/service.py` (LaunchAgentManager wiring), Swift `LaunchAgentManager.swift` | ЖИВОЙ |
| 2 | `translate_and_paste` | Вставлять сразу перевод вместо оригинала | `settings_service.py:382` (coerce), используется translation flow | ЖИВОЙ (но бессмыслен без `translation_mode≠off`) |
| 3 | `onboarding_completed` | Флаг «онбординг пройден» | `settings_service.py:383`, main.swift onboarding gate | ЖИВОЙ (это не фича, а состояние) |
| 4 | `llm_rewrite_enabled` | Полировка транскрипта через LM Studio LLM | `core/engine.py` (множество мест), `_get_runtime_setting` каждый вызов | ЖИВОЙ, но требует **отдельно запущенный LM Studio** |
| 5 | `stt_punctuation_llm_pass_enabled` | Отдельный LLM-пасс только на пунктуацию | `core/engine.py:528` `self._settings_get(...)` | ЖИВОЙ; коммент в коде: «burn-in период» (осознанно ручной opt-in) |
| 6 | `auto_save_transcripts` | Доп. сохранение .md в `transcripts/` | `backend/recording_core_service.py:2420` | ЖИВОЙ |
| 7 | `smart_silence_skip_enabled` | Вырезать длинные внутренние паузы до STT | `core/engine.py:1013-1014` `settings.SMART_SILENCE_SKIP_ENABLED` (mechanism A, живой hot-reload) | ЖИВОЙ |
| 8 | `realtime_silence_filter_enabled` | Подавлять partial-события на тишине | `backend/realtime_silence_filter.py:46`, `recording_core_service.py:347` | ЖИВОЙ |
| 9 | `auto_dedup_enabled` | Пропускать почти-дубликаты записей | `recording_core_service.py:1417,1793` | ЖИВОЙ |
| 10 | `wake_word_enabled` | *(предположительно)* включение wake word | **Grep по всему дереву (upper+lower, вкл. тесты) — ноль обращений кроме строки определения** | **МЁРТВЫЙ** — см. §3.2.1 |
| 11 | `voxtral_enabled` | Voxtral realtime STT адаптер | `core/engine.py:1992` `settings.VOXTRAL_ENABLED` | ЖИВОЙ, но требует опциональный пакет `mistral_inference` + скачивание модели ~4B |
| 12 | `voxtral_reasoning_enabled` | Reasoning-режим Voxtral | `backend/models.py:88` (комментарий); отдельно не проверялся — вне фокуса, т.к. сам Voxtral исключён | не проверено детально (зависимый суб-флаг) |
| 13 | `stt_streaming_enabled` | Потоковая чанк-транскрибация длинного аудио | **Ноль реальных обращений** — единственное упоминание: комментарий-заглушка в `core/pipeline/stt_router.py:18` (нечитаемый, недоделанный scaffold-класс) | **МЁРТВЫЙ** — см. §3.2.2 |
| 14 | `stt_language_routing_enabled` | Роутинг STT-модели по языку (RU/ES/EN primary) | `core/stt_router.py:333` (другой, реальный `STTRouter`, обширно протестирован `tests/test_stt_router.py`) | ЖИВОЙ, но бесполезен без `stt_gigaam_enabled=True` (см. §4) |
| 15 | `pipeline_v2_enabled` | Переключение на Phase-4 пайплайн (`core/pipeline/`) | `core/engine.py:821-830` | ЖИВОЙ, но это архитектурный флаг миграции, не «фича» |
| 16 | `stt_use_ru_finetune` | RU whisper fine-tune модель | `core/engine.py:1911`, обширно протестирован `test_engine_ru_finetune.py` | ЖИВОЙ, требует скачивания `antony66/whisper-large-v3-russian` |
| 17 | `stt_gigaam_enabled` | GigaAM-RNNT RU STT движок | `core/pipeline/stt_router_factory.py`, `core/engine.py` (chain) | ЖИВОЙ, требует **отдельный venv** (`STT_GIGAAM_VENV_PYTHON`, см. CLAUDE.md «Subprocess interpreter validation») |
| 18 | `stt_sensevoice_enabled` | SenseVoice multilingual STT | `core/pipeline/stt_router_factory.py:89` | ЖИВОЙ, требует HF-модель `FunAudioLLM/SenseVoiceSmall` (скачивание, если не в кэше) |
| 19 | `export_include_speaker_labels` | Метки спикеров в экспорте | **Не читается.** Реальный код (`history_service.py:3438` `_should_include_speaker_labels`) читает **`params.get("include_speaker_labels")`** — параметр вызова, другое имя! | **МЁРТВЫЙ (orphaned)** — см. §3.2.3 |
| 20 | `voice_fingerprint_enabled` | Голосовой fingerprint-матчинг спикеров | Докстринг `backend/speaker_manager.py:11` заявляет гейт; фактического `if settings.VOICE_FINGERPRINT_ENABLED` внутри класса **не найдено**; методы вызываются только напрямую по explicit IPC (`register_speaker` и т.п.), без единого автоматического вызывающего в pipeline | **ПОД ВОПРОСОМ** — не проверено до конца, вне фокуса этого черновика |
| 21 | `recap_email_enabled` | Email-дайджест по расписанию | `backend/recap_scheduler.py`, требует SMTP | ЖИВОЙ, сетевой |
| 22 | `smtp_use_ssl` | SMTPS вместо STARTTLS | sub-config `recap_email_enabled` | ЖИВОЙ, не отдельная фича |
| 23 | `auto_cleanup_enabled` | Авто-удаление старых записей >N дней | `disk_monitor`-соседний путь | ЖИВОЙ, **необратимо** |
| 24 | `action_items_auto_extract` | Авто-извлечение задач из транскрипта через LLM | `recording_core_service.py:1644` | ЖИВОЙ, требует LLM (см. #4); privacy-гейт **не подтверждён** в этом черновике — открытый вопрос |
| 25 | `calendar_link_enabled` | Авто-связка записи с событием Calendar.app | `service.py:3922` `_get_runtime_setting` | ЖИВОЙ, требует разрешение Calendar (macOS сам спросит) |
| 26 | `rest_api_auth_enabled` | Требовать Bearer-токен на REST :5005 | `rest_server.py:274` (комментарий указывает на реальную ветку `if`) | ЖИВОЙ, но **в другом процессе** (см. §1 риск) |
| 27 | `text_snippets_enabled` | Голосовые триггер-фразы → пользовательские сниппеты | `core/text_snippet_expander.py:54` | ЖИВОЙ |
| 28 | `phonetic_vocab_enabled` | Фонетическая коррекция → каноническое написание | `core/phonetic_vocabulary.py:56` | ЖИВОЙ |
| 29 | `semantic_search_enabled` | Семантический поиск по истории (эмбеддинги) | `backend/service.py:898` — **но только в `BackendService.__init__`**, значение кэшируется в `SemanticSearcher.__init__` → `self._enabled` (`backend/semantic_search.py:38`), никакой `register_after_save_hook` на это поле не заведён | **ЖИВОЙ⚠РЕСТАРТ** — см. §3.2.4 |
| 30 | `quick_edit_enabled` | Показ окна правки перед авто-вставкой | **Только Swift**: `Models.swift`, `HistoryPanelController+Settings.swift`, `QuickEditOverlay.swift`, `main+PasteHandling.swift`. Ноль обращений в Python backend | ЖИВОЙ (полностью клиентская фича, backend не участвует) |
| 31 | `privacy_mode_enabled` | Режим приватности (блокирует сеть/Sentry) | Везде (148 обращений в backend) | ЖИВОЙ — **это не «фича для включения», это противоположность** — см. §4 |
| 32 | `auto_purge_enabled` | Плановое авто-удаление истории старше N дней | `backend/service.py` | ЖИВОЙ, **необратимо** |
| 33 | `auto_learn_corrections_enabled` | Авто-добавление исправленных слов в STT-словарь | `backend/llm_ops_service.py:215` | ЖИВОЙ |
| 34 | `history_encryption_enabled` | AES-256-GCM шифрование новых записей истории | `backend/state_store.py`/`history_crypto.py` (по CLAUDE.md) | ЖИВОЙ, меняет формат хранения на диске |
| 35 | `cloud_rewriter_enabled` | Облачная LLM-полировка (OpenAI/Anthropic/custom) | `core/engine.py:538,1240` | ЖИВОЙ, **сетевой по определению** |
| 36 | `translation_mode` (`"off"`) | Режим перевода (RU↔ES/EN и т.д.) | Ядро продукта, уже видимо в главном UI и в `apply_profile_preset` | ЖИВОЙ — не «скрытая» фича |
| 37 | `wake_word_engine` (`"disabled"`) | Реальный переключатель wake word (см. #10 — дубль-обманка) | `WakeWordPoller.swift`, `openwakeword_adapter.py` (по CLAUDE.md) | ЖИВОЙ — это настоящий тумблер, а не #10 |
| 38 | `streaming_paste_enabled` *(только Swift, нет в Python `DEFAULT_SETTINGS`)* | Вставка текста по мере диктовки | `StreamingPasteController.swift` подписан на **SSE `/v1/events`** (REST-процесс) на события `realtime.partial_transcript`/`realtime.final_transcript`, которые эмитятся в **IPC-процессе** (`backend/realtime_partial.py`) | **РИСК — вероятно жертва 2-EventBus гэпа** (см. §3.3, §9.2) |
| 39 | `paste_undo_enabled` *(только Swift)* | Global hotkey Cmd+Ctrl+Z — откат последней вставки | `PasteUndoService.swift` — **чисто локально**, CGEvent keystroke replay, без IPC/сети | ЖИВОЙ, полностью безопасно |

### 3.2 Подтверждённые «мёртвые» тумблеры (находки, НЕ включать в пресет)

Это не гипотезы — по каждому проверено отсутствие любого чтения (в обеих формах имени) во
всём `KrabEar/` и `native/`, включая тесты.

#### 3.2.1 `wake_word_enabled` — осиротевший дубликат
`WAKE_WORD_ENABLED: bool = False` объявлено в `core/config.py:246` и в `DEFAULT_SETTINGS`
(`core/config.py:867`), но **ни разу не читается** ни в одной форме имени нигде в проде. Реальный
живой переключатель wake word — строковый `wake_word_engine` (`"openwakeword"|"porcupine"|"disabled"`),
который поллит `WakeWordPoller.swift`. Похоже на реликт более раннего дизайна (булев enable/disable
до появления enum движков), который никто не удалил. **Не путать в UI** — включение `wake_word_enabled`
через пресет ничего не изменит в поведении; нужен `wake_word_engine`.

#### 3.2.2 `stt_streaming_enabled` (+3 компаньона) — мёртвый scaffold
`stt_streaming_enabled`, `stt_streaming_min_audio_sec`, `stt_streaming_chunk_sec`,
`stt_streaming_overlap_sec` — все 4 объявлены (`core/config.py:880-883`, plus pydantic-версия
`STT_STREAMING_ENABLED` в `Settings`), но реального читателя нет. Единственное упоминание —
докстринг-план в `core/pipeline/stt_router.py:18` (`3. Speed-first if real-time required
(settings.stt_streaming_enabled)`) — это **не код**, это TODO-комментарий в классе, который сам
не подключён к реальному пайплайну (реальный роутинг — другой файл, `core/stt_router.py`, см. #14).
`core/audio_chunker.py` (`AudioChunker`, упомянут в CLAUDE.md) существует отдельно и не гейтится
этим флагом.

#### 3.2.3 `export_include_speaker_labels` — осиротевший глобальный дефолт
Реальное поведение экспорта меток спикеров управляется **параметром вызова**
`params.get("include_speaker_labels")` (`backend/history_service.py:3438`,
`_should_include_speaker_labels`), а не глобальной настройкой `export_include_speaker_labels`.
Т.е. фича «включать метки спикера в экспорт» реально работает (проверяется per-call), но
глобальный дефолт-тумблер в `DEFAULT_SETTINGS`, который выглядит как «фича, ждущая включения»,
никогда не читается и ни на что не влияет.

### 3.3 Находка вне прямого запроса, но важная для пресета: настройки, которых нет в Python

`streaming_paste_enabled` и `paste_undo_enabled` **отсутствуют в `KrabEar/core/config.py`
`DEFAULT_SETTINGS` целиком** — они существуют только как Swift-side дефолты
(`Models.swift:182,184`, `Self.default.pasteUndoEnabled = false` и т.д.). Механически это не
ломается: `_handle_set_settings_locked` делает `settings.update(params)` без проверки набора
ключей (`settings_service.py:322`), так что произвольный ключ от Swift сохраняется и переживает
рестарт. Но это значит: (1) Python-код и его инвентаризация (в т.ч. этот черновик, если бы он
смотрел только на `core/config.py`) **не увидит** эти настройки без grep по Swift; (2) для
`apply_recommended_setup`, если он должен когда-либо включить `paste_undo_enabled` (см. §4, ДА),
IPC должен уметь писать ключ, которого нет в `DEFAULT_SETTINGS` — это работает уже сегодня
(generic merge), но стоит явно протестировать (см. §8, тест-план).

Отдельная, более серьёзная находка про `streaming_paste_enabled`: `StreamingPasteController.swift`
подписан на SSE `/v1/events`, обслуживаемый **REST-процессом** (`rest_server.py`, :5005), на
события `realtime.partial_transcript`/`realtime.final_transcript`. Но эти события эмитятся
`RealtimePartialTranscriber` (`backend/realtime_partial.py`) через EventBus **IPC-процесса**
(`service.py`). Согласно ROADMAP §0 («2-EventBus гэп») и §2 Волна 2 («live subs SSE-путь агента
— под подозрением, проверить в Волне 2»), это ТОЧНО ТА ЖЕ архитектурная дыра. Если запись идёт
через агентский путь (IPC-сокет, не REST), партиал-события, скорее всего, **никогда не доходят**
до `StreamingPasteController`. Это не подтверждено живым смоком в рамках этого черновика (задача
явно запрещала запуск бинарей/серверов) — оставлено как явный риск в §9.2, требующий проверки
ДО включения `streaming_paste_enabled` куда-либо в пресет.

---

## 4. Классификация безопасности + ресурсы + tier + вердикт

Критерии из роадмапа (§2 Волна 1): **(а)** не меняет данные необратимо; **(б)** не шлёт ничего в
сеть; **(в)** не требует внешних зависимостей/моделей сверх уже установленных; **(г)** деградирует
тихо при сбое. Ресурсы — качественная оценка по коду (не измерено профилировщиком). Tier — из
`core/hardware_profile.py` (`low<16` / `mid 16-32` / `high>32` ГБ).

Легенда вердикта: **ДА** — безопасно включать сразу; **УСЛОВНО-ДА** — безопасно, но только после
проверки предусловия (модель скачана / venv настроен / LLM отвечает); **ВОПРОС** — решение за
владельцем/контролёром, не автоматизировать; **НЕТ** — не включать в пресет (с причиной).

| Настройка | а | б | в | г | RAM/CPU | Min tier | ВЕРДИКТ |
|---|---|---|---|---|---|---|---|
| `smart_silence_skip_enabled` | ✅ | ✅ | ✅ | ✅ (try/except → «продолжаем с оригинальным аудио») | низкое (DSP) | low | **ДА** |
| `realtime_silence_filter_enabled` | ✅ | ✅ | ✅ | ✅ | низкое | low | **ДА** |
| `auto_dedup_enabled` | ⚠️ (пропускает сохранение похожей записи — не удаляет существующее, но может «спрятать» новую) | ✅ | ✅ | ✅ (threshold=0.9 консервативен) | низкое | low | **ДА** |
| `auto_save_transcripts` | ✅ (только добавляет .md, ничего не трогает) | ✅ | ✅ | ✅ | низкое (доп. файл I/O) | low | **ДА** |
| `phonetic_vocab_enabled` | ✅ | ✅ | ✅ | ✅ (пустой список = no-op) | низкое | low | **ДА** |
| `text_snippets_enabled` | ✅ | ✅ | ✅ | ✅ (пустой список = no-op) | низкое | low | **ДА** |
| `auto_learn_corrections_enabled` | ✅ (аддитивно, есть remove-IPC) | ✅ | ✅ | ✅ | низкое | low | **ДА** |
| `quick_edit_enabled` | ✅ (чисто UX) | ✅ | ✅ (Swift-only, backend не участвует) | ✅ | нулевое (backend) | low | **ДА** |
| `paste_undo_enabled` | ✅ (keystroke replay, ничего не хранит) | ✅ | ✅ | ✅ (no-op если выключен) | нулевое | low | **ДА** |
| `calendar_link_enabled` | ✅ (только читает Calendar.app, не пишет) | ✅ (osascript локально) | ⚠️ требует разрешение Calendar (macOS сам спросит) | ✅ | низкое | low | **ДА** (с уведомлением про permission-prompt) |
| `llm_rewrite_enabled` | ✅ | ✅ (LM Studio на 127.0.0.1 — не «внешняя сеть», но это отдельный процесс) | ⚠️ **требует запущенный LM Studio + загруженную модель** | ✅ (CircuitBreaker + fallback на raw text уже есть) | среднее-высокое (внешний процесс) | mid | **УСЛОВНО-ДА** — только если `probe_llm_http` (уже есть IPC) успешен |
| `action_items_auto_extract` | ✅ (только добавляет summary) | ✅ (если llm_rewrite локальный) | ⚠️ зависит от LLM (см. выше) | не проверено (нет явного try/except в этом черновике) | среднее (LLM-вызов) | mid | **УСЛОВНО-ДА** — и подтвердить privacy-гейт (см. §9.3) |
| `stt_gigaam_enabled` + `stt_language_routing_enabled` (пара) | ✅ | ✅ | ❌ **отдельный venv (`STT_GIGAAM_VENV_PYTHON`) должен уже существовать** | ✅ (fallback chain) | среднее (RTF=0.041 warm по замерам памяти) | mid | **УСЛОВНО-ДА** — только если venv-путь уже сконфигурирован и проходит валидацию (`Path.is_relative_to`+allowlist) |
| `stt_sensevoice_enabled` | ✅ | ⚠️ первый запуск качает модель с HF, если не в кэше | ⚠️ то же | ✅ | среднее | mid | **УСЛОВНО-ДА** — только если модель уже в HF-кэше |
| `stt_use_ru_finetune` | ✅ | ❌ качает `antony66/whisper-large-v3-russian`, если нет в кэше | ❌ | ✅ | среднее-высокое | mid | **НЕТ** — скачивание модели не должно быть silent-побочным эффектом one-tap пресета |
| `voxtral_enabled` (+`voxtral_reasoning_enabled`) | ✅ | ❌ качает ~4B модель | ❌ требует опциональный `mistral_inference` пакет, которого нет в `requirements.txt` по умолчанию | ✅ (RuntimeError, не крашит) | высокое | high | **НЕТ** |
| `semantic_search_enabled` | ✅ | ⚠️ качает `multilingual-e5-base`, если не в кэше | ⚠️ | ✅ (внутри try/except по коду) | среднее (эмбеддинги при индексации) | mid | **ВОПРОС** — технически безопасно, НО требует рестарта backend (см. §3.1 #29); ломает обещание «мгновенный эффект» |
| `history_encryption_enabled` | ⚠️ меняет формат НОВЫХ записей на диске (не удаляет старые, но необратимо в смысле «нельзя тихо откатить одним твиком настройки» — нужна отдельная миграция) | ✅ | ⚠️ штатно нужен Keychain (деградирует тихо, если нет) | ✅ | низкое | low | **ВОПРОС** — формально проходит а-г, но по духу это «security opt-in», не «discoverability»-фича, и заслуживает явного согласия, а не тихого включения |
| `stt_punctuation_llm_pass_enabled` | ✅ | как `llm_rewrite_enabled` | как `llm_rewrite_enabled` | не проверено | среднее | mid | **ВОПРОС** — сам код помечен `# burn-in период`, автор явно хотел ручной контроль |
| `stt_language_routing_enabled` (в одиночку, без gigaam) | ✅ | ✅ | ✅ | ✅ | нулевое (no-op без gigaam) | low | **ВОПРОС** — включать изолированно бессмысленно; решить как пара с gigaam или не включать вовсе |
| `voice_fingerprint_enabled` | не проверено (гейт не найден) | не проверено | не проверено | не проверено | не проверено | — | **ВОПРОС** — вне уверенной классификации этого черновика, требует отдельного разбора гейта |
| `wake_word_engine` → `"openwakeword"` | ✅ | ✅ (локальный движок) | ⚠️ модель уже должна быть забутстрапена (`bootstrap_backend.command`) | ✅ | среднее (всегда слушающий поток) | mid | **НЕТ** для one-tap — always-on микрофон должен быть явным, осознанным выбором пользователя (см. §9.4), не побочным эффектом «сделай мне хорошо» |
| `rest_api_auth_enabled` | ✅ | ✅ (это и есть защита сети) | ⚠️ живёт в ДРУГОМ процессе (см. §1) | ⚠️ может сломать существующую интеграцию VG-моста, которая ходит без токена | нулевое | low | **НЕТ** — это security-режим, не «discoverability»-фича; включение может сломать соседний проект (Voice Gateway) без предупреждения |
| `privacy_mode_enabled` | — | — | — | — | — | — | **НЕТ, явно исключить** — это противоположность цели пресета; `apply_recommended_setup` должен УВАЖАТЬ уже включённый privacy_mode, а не менять его |
| `cloud_rewriter_enabled` | ✅ | ❌ **транскрипт уходит в облако по определению** | ⚠️ нужен API-ключ | ✅ | нулевое (локально) | low | **НЕТ** (роадмап сам называет критерий «б» именно ради этого) |
| `recap_email_enabled` | ✅ | ❌ SMTP | ❌ нужны SMTP-креды | ✅ | нулевое | low | **НЕТ** |
| `auto_cleanup_enabled` / `auto_purge_enabled` | ❌ **необратимое удаление истории** | ✅ | ✅ | ✅ | нулевое | low | **НЕТ** (роадмап сам называет критерий «а» именно ради этого) |
| `pipeline_v2_enabled` | ⚠️ меняет весь путь обработки аудио | ✅ | ✅ | не проверено | не проверено | — | **НЕТ** — это флаг архитектурной миграции, а не «скрытая фича для пользователя» |
| `translate_and_paste`, `auto_start_enabled`, `onboarding_completed`, `smtp_use_ssl`, `translation_mode` | — | — | — | — | — | — | **НЕТ** — не «скрытые мощные фичи» в духе задачи (либо уже видны в главном UI, либо не самостоятельная фича, либо служебное состояние) |
| `export_include_speaker_labels`, `stt_streaming_enabled`, `wake_word_enabled` | — | — | — | — | — | — | **НЕТ, МЁРТВЫЕ** — см. §3.2 |
| `streaming_paste_enabled` | ✅ (по коду) | ✅ (по коду) | ✅ | ⚠️ | низкое | low | **НЕТ (пока)** — риск 2-EventBus гэпа не проверен живым смоком, отложить минимум до Волны 2 (§9.2) |

### Итог классификации

- **ДА (безусловно безопасно)**: 10 — `smart_silence_skip_enabled`, `realtime_silence_filter_enabled`,
  `auto_dedup_enabled`, `auto_save_transcripts`, `phonetic_vocab_enabled`, `text_snippets_enabled`,
  `auto_learn_corrections_enabled`, `quick_edit_enabled`, `paste_undo_enabled`, `calendar_link_enabled`.
- **УСЛОВНО-ДА (безопасно после проверки предусловия)**: 4 — `llm_rewrite_enabled`,
  `action_items_auto_extract`, `stt_gigaam_enabled`+`stt_language_routing_enabled` (пара),
  `stt_sensevoice_enabled`.
- **ВОПРОС (решение владельца/контролёра)**: 5 — `semantic_search_enabled`,
  `history_encryption_enabled`, `stt_punctuation_llm_pass_enabled`, `stt_language_routing_enabled`
  (в одиночку), `voice_fingerprint_enabled`.
- **МЁРТВЫЕ (находки, не пресет)**: 3 — `wake_word_enabled`, `stt_streaming_enabled`,
  `export_include_speaker_labels`.
- **НЕТ (явно исключены)**: 17 — остальные (сеть/необратимость/тяжёлые зависимости/не-фичи/
  архитектурные флаги/риск гэпа).

---

## 5. Контракт нового IPC `apply_recommended_setup`

Строится **рядом** с `apply_profile_preset` (`backend/settings_service.py:535`), тем же классом
`SettingsService`, тем же скелетом (`_save_lock`, `cached_settings()` → merge → `save_settings()` →
`invalidate_cache()` → `_reload_and_fire_hooks()` → EventBus).

### 5.1 Request

```
apply_recommended_setup {
    "dry_run": bool = true,     // по умолчанию TRUE — превью без записи (см. §9.5, открытый вопрос)
    "keys": list[str] | null,   // опционально: подмножество ключей из рекомендованного пресета
                                // (для «применить только это» в UI); null = всё безопасное множество
}
```

### 5.2 Response

```
{
    "ok": true,
    "dry_run": bool,
    "tier": "low"|"mid"|"high",
    "applied": [
        {"key": str, "old_value": Any, "new_value": Any, "restart_required": bool}
    ],
    "skipped": [
        {"key": str, "reason": str}   // напр. "privacy_mode_enabled=true — фича читает историю",
                                       // "требует stt_gigaam venv, не найден",
                                       // "требует LM Studio, probe_llm_http не ответил",
                                       // "уже включено"
    ],
    "rationale": str,             // человеко-читаемое summary для UI (RU)
    "snapshot_id": str | null,    // null если dry_run=true (снапшот не нужен — ничего не менялось)
    "restart_required": bool      // true если хотя бы один applied-ключ требует рестарта (см. §1, №29)
}
```

### 5.3 Поведение (черновик алгоритма)

1. `old_settings = cached_settings()`.
2. Если `old_settings.get("privacy_mode_enabled")` — сразу помечать `action_items_auto_extract` и
   любой другой transcript-читающий кандидат как `skipped` с причиной `privacy_mode_enabled`
   (см. §9.3 — список таких кандидатов не закрыт в этом черновике).
3. Прогнать список «ДА» безусловно.
4. Для «УСЛОВНО-ДА» — выполнить пробник ПЕРЕД включением:
   - `llm_rewrite_enabled`/`action_items_auto_extract` → вызвать существующий `probe_llm_http`
     (Phase B/C IPC, уже есть по CLAUDE.md) — если не отвечает, `skip`.
   - `stt_gigaam_enabled` → проверить, что `STT_GIGAAM_VENV_PYTHON` (если задан) проходит ту же
     валидацию, что и в `subprocess.Popen`-guard (allowlist + `is_relative_to`) И что путь
     существует на диске — если нет пути вовсе, `skip` с причиной «venv не настроен».
   - `stt_sensevoice_enabled` → проверить наличие модели в HF-кэше (не качать самому!).
5. **«ВОПРОС»-кандидаты и «НЕТ»-кандидаты в пресет НЕ включать вообще** — ни при `dry_run=false`,
   ни при явном перечислении в `keys` (или: разрешить только явное перечисление в `keys` — это
   открытый вопрос §9.5, т.к. затрагивает UX «применить только это»).
6. Если `dry_run=true` — вернуть предпросмотр `applied`/`skipped`, **не писать** `settings.json`,
   `snapshot_id=null`.
7. Если `dry_run=false`:
   - `self._backup.create_backup(old_settings, reason="before_recommended_setup")` → `snapshot_id`.
   - Merge только ключи из финального `applied`.
   - `self.store.save_settings(merged)`, `invalidate_cache()`.
   - `self._reload_and_fire_hooks(old_settings, merged)`.
   - EventBus `recommended_setup.applied` (по аналогии с `preset.changed`).
   - Вернуть тот же shape с заполненным `snapshot_id`.

### 5.4 `restart_required`

Из находки §3.1 №29 — если применённый набор включает `semantic_search_enabled` (пока висит как
ВОПРОС, но если владелец решит включать) или любой другой кандидат, чей коллаборатор кэширует
флаг в конструкторе `BackendService.__init__`, ответ должен честно говорить `restart_required:
true`, а UI — показывать «часть изменений вступит в силу после перезапуска Krab Ear». Точный
список таких ключей **не закрыт** в этом черновике за пределами кейса #29 — нужен отдельный
проход по каждому «УСЛОВНО-ДА»/«ДА» кандидату с вопросом «читается ли этот флаг заново на каждом
вызове, или закэширован в `__init__` коллаборатора» (сделано выборочно для части кандидатов выше,
не исчерпывающе для всех 14).

---

## 6. Откат (rollback)

**Не нужно изобретать новый механизм.** `snapshot_id` из §5.2 — это `backup_id`, совместимый с
уже существующим `restore_settings_backup {backup_id}` (`backend/settings_service.py:776`,
`handle_restore_settings_backup`), который уже делает: pre-restore backup (защита от двойной
ошибки), миграцию схемы, валидацию, редактирование секретов в ответе, и **rollback при
невалидном бэкапе**. UI-кнопка «Отменить» после применения пресета = обычный вызов
`restore_settings_backup` с сохранённым `snapshot_id`. Дополнительно: `list_settings_backups`
(`settings_service.py:758`) уже позволяет показать историю снапшотов с `reason` — новый
`reason="before_recommended_setup"` будет отличим от `before_set`/`before_restore` в этом списке
без каких-либо изменений существующего кода бэкапов.

---

## 7. UI-точки

### 7.1 Онбординг — шаг после `ModelDownloadStep`

Файл-образец: `native/KrabEarAgent/Sources/KrabEarAgent/ModelDownloadStep.swift` +
`main.swift:1417` (`runModelDownloadStepThenComplete`), `main.swift:1231`
(`QuickStartWindowController`).

Предложение (не решено — см. §9.6): новый `RecommendedSetupStepController` (новый файл), тот же
паттерн — неблокирующий `NSWindow.beginSheet`, IPC строго off-main
(`apply_recommended_setup {dry_run: true}` сразу при показе шага → список «что мы включим и
почему» → кнопка «Применить» вызывает `apply_recommended_setup {dry_run: false}` → «Пропустить»
= `ModelDownloadStepOutcome`-подобный `.skipped`, ничего не меняет). Встраивается в цепочку
`runModelDownloadStepThenComplete()` **после** `ModelDownloadStepController`, **перед**
`completion()`/финализацией онбординга — по аналогии с тем, как `ModelDownloadStepController`
сам встроен перед завершением.

### 7.2 Секция в Настройках

Файл-образец: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Calibration.swift`
— уже потребляет `get_hardware_profile` + `get_calibration_recommendation` тем же паттерном,
который нужен здесь. Копировать архитектуру: dual `buildXSection()`/`cdBuildXSection()` (Gemini/CD
варианты), `fetchAndRebuildXCard()` — оба IPC off-main (`DispatchQueue.global`), перестройка
карточки на main. Новый файл по аналогии: `HistoryPanelController+RecommendedSetup.swift`
(**дизайн/цвета/раскладка — только через Gemini/agy** согласно `feedback_frontend_gemini`,
Auto Layout механику и state wiring делает Claude — см. CLAUDE.md «Gemini 3.1 Pro для дизайна»).
Секция показывает: текущий tier, список «включено/пропущено с причиной» из последнего
`dry_run`, кнопку «Применить рекомендуемое» и кнопку «Отменить последнее применение» (читает
последний `snapshot_id` из `list_settings_backups` с `reason=before_recommended_setup`).

### 7.3 Глиф-гейт

Как и для любой Swift-правки — перед сдачей прогнать grep новых non-ASCII глифов против
`native/` (см. `feedback_glyph_gate_swift_workers` в памяти) — в этой секции скорее всего не
нужно новых иконок сверх уже установленных SF Symbols (`checkmark.circle.fill`,
`arrow.uturn.backward` для «Отменить» и т.п., по аналогии с Calibration-секцией).

---

## 8. Privacy-примечания

- `privacy_mode_enabled` **всегда побеждает** (правило проекта, см. CLAUDE.md). `apply_recommended_setup`
  обязан проверить его первым и не предлагать/не включать ничего из кандидатов, которые
  обрабатывают содержимое транскрипта, если privacy_mode уже включён.
- Кандидаты, которые **читают или обрабатывают текст транскрипта** (потенциально требуют
  privacy-гейт по паттерну CLAUDE.md «any handler that returns transcript-derived content»):
  `action_items_auto_extract` (LLM над текстом), `stt_punctuation_llm_pass_enabled` (LLM над
  текстом), `auto_learn_corrections_enabled` (читает исправленные слова из транскриптов),
  `semantic_search_enabled` (индексирует текст истории), `auto_dedup_enabled` (сравнивает тексты
  записей). **Этот черновик не подтвердил построчно**, что каждый из них уже гейтится на
  `privacy_mode_enabled` там, где реально исполняется (кроме `auto_dedup_enabled`, у которого
  комментарий в коде явно упоминает privacy — `recording_core_service.py:1408` «Respects
  auto_dedup_enabled and privacy_mode_enabled settings»). Это должно быть явно проверено на этапе
  реализации, не черновика — см. открытый вопрос §9.3.
- `history_encryption_enabled` и `stt_gigaam_enabled`/`stt_sensevoice_enabled` не читают/не
  отправляют пользовательские данные наружу сами по себе, но `stt_gigaam_enabled` требует
  отдельный процесс/venv — если этот venv когда-либо получит сетевой доступ по ошибке
  конфигурации, это вне контроля этого пресета (существующий risk, не новый).
- `calendar_link_enabled` читает Calendar.app (может содержать чужие данные — участники встреч) —
  это уже разрешено существующей фичей, пресет лишь включает уже существующий, ранее
  запроектированный путь; отдельного нового privacy-риска не добавляет.

---

## 9. Открытые вопросы владельцу/контролёру

Всё спорное — сюда. Ничего из этого не решено черновиком.

### 9.1 Расхождение с числом «29» из роадмапа
Роадмап называет «29 из ~200 настроек» с примерами `smart_silence_skip, auto_dedup,
semantic_search, quick_edit, streaming_paste, paste_undo, phonetic_vocab, auto_learn_corrections,
action_items_auto_extract, text_snippets`. Инвентаризация этого черновика нашла **39** (все
`False`-дефолты + 2 режима `"off"`/`"disabled"` + 2 Swift-only). Если исключить явно
не-«фичи» (состояния типа `onboarding_completed`, суб-флаги типа `smtp_use_ssl`/
`voxtral_reasoning_enabled`, архитектурные типа `pipeline_v2_enabled`, уже-видимые-в-UI типа
`translation_mode`) — получается ближе к ~29-31. **Вопрос**: считать ли числом-целью именно
«29» (тогда нужно формально согласовать, какие из 39 не в счёт) или считать инвентаризацию этого
черновика источником истины взамен исходной оценки роадмапа?

### 9.2 `streaming_paste_enabled` — жертва 2-EventBus гэпа?
Не проверено живым смоком (задача черновика запрещала запуск бинарей). Если подтвердится, что
партиал-транскрипт по агентскому пути не доходит до `StreamingPasteController` через REST SSE —
это **отдельный, самостоятельный баг** (тот же класс, что и live subs / wake word / krab_error
до их фиксов), не про настройки-пресет как таковые. **Вопрос**: чинить ли это как часть Волны 1,
или явно отложить до Волны 2 (event-мост) и просто держать `streaming_paste_enabled` вне пресета
до тех пор — текущая рекомендация черновика №2, но решение за владельцем/контролёром, т.к. Волна
2 по плану идёт СЛЕДОМ за Волной 1, а не одновременно.

### 9.3 Privacy-гейт для LLM-читающих кандидатов не проверен построчно
`action_items_auto_extract`, `stt_punctuation_llm_pass_enabled`, `auto_learn_corrections_enabled`,
`semantic_search_enabled` — черновик пометил их «вероятно нужно проверить», но не прочитал
каждую функцию целиком на предмет реального `if privacy_mode_enabled: return` в начале. **Вопрос
контролёру**: включить построчную privacy-гейт-проверку этих 4 как явную задачу №0 в план волны
(до реализации `apply_recommended_setup`), а не полагаться на то, что «наверное уже гейтится, как
и всё остальное по CLAUDE.md паттерну».

### 9.4 Wake word — исключать ли полностью из «один тап», даже если модель уже забутстрапена?
Черновик рекомендует **не** включать `wake_word_engine` автоматически (всегда слушающий
микрофон — по духу это должно быть явным решением, не побочным эффектом). Но роадмап описывает
wake word как «уже живую» фичу (Волна 0/§0), и она **тоже** не обнаруживается новыми
пользователями без A1-подобного механизма — тот же корень проблемы, который решает вся Волна 1.
**Вопрос**: если не в пресет — то в отдельный явный шаг онбординга («хотите включить голосовой
триггер „hey jarvis"? микрофон будет всегда слушать локально») с отдельным согласием, но всё
равно **в рамках Волны 1**, а не только в Настройках, куда никто не заходит (это же исходная
проблема A1)?

### 9.5 `dry_run` по умолчанию — `true` или `false`?
Черновик предлагает `dry_run: bool = true` (безопасный дефолт для нового API — вызывающий должен
явно попросить запись). Но UX-флоу онбординга (§7.1) скорее всего хочет: показать шаг → сразу
`dry_run=true` для превью → пользователь жмёт «Применить» → **отдельный** вызов с `dry_run=false`.
Это согласуется с дефолтом `true`. **Вопрос**: нужна ли третья форма — «применить только
конкретные ключи» (`keys` параметр из §5.1) — сразу в этой волне, или это over-engineering для
v1 и `keys=null` (всё-или-ничего из безопасного множества) достаточно на старте?

### 9.6 Кто реализует Swift-часть — Sonnet или Gemini/agy?
Разделение по CLAUDE.md: «выглядит иначе» → Gemini/agy, «ведёт себя иначе» → Claude/Sonnet.
Новый онбординг-шаг и Settings-секция здесь — в основном **wiring** (IPC off-main, associated
objects, состояние snapshot/rollback), не визуальный дизайн с нуля — похоже на «поведение», не
«внешний вид», аналогично тому, как `HistoryPanelController+Calibration.swift` сам был
реализован (нужно перепроверить, кем именно — этот черновик не проверял git blame/PR-историю
файла-образца). **Вопрос контролёру**: нужен ли отдельный design-briaf в
`docs/design-briefs/` для agy на визуальную часть новой секции (карточка/цвета/иконки), даже
если механика полностью на Sonnet?

### 9.7 `stt_gigaam_enabled`+`stt_language_routing_enabled` — включать парой или доверить владельцу вручную?
Учитывая, что GigaAM требует отдельный venv (внешняя зависимость по критерию «в»), а memory-заметки
проекта («GigaAM РЕШЕНО — venv install», «GigaAM bench 2026-04-26») говорят, что **у владельца
venv уже настроен и работает**, вопрос практический: должен ли `apply_recommended_setup`
автоматически детектировать «venv существует и валиден → включить», или это всегда должно быть
`skipped` с текстовой подсказкой «настройте GigaAM вручную в Настройках», даже для владельца?
Автодетект технически возможен (§5.3 шаг 4), но не проверен на реальной машине в рамках этого
черновика (задача запрещала выполнение).

---

## 10. Тест-план (черновик, не исчерпывающий)

1. **Контракт `apply_recommended_setup`**: `dry_run=true` не пишет `settings.json` (сравнить
   mtime/hash файла до/после); `dry_run=false` пишет и создаёт ровно один новый backup с
   `reason="before_recommended_setup"`.
2. **Список безопасных кандидатов зафиксирован в тесте** — regression-тест, который проверяет,
   что ни один ключ из §4 «НЕТ»/«МЁРТВЫЕ» никогда не появляется в `applied` ни при каком входе
   (защита от будущего «случайно добавили новый параметр в список»).
3. **`privacy_mode_enabled=true` → все transcript-читающие кандидаты в `skipped`** (список из
   §8, после того как §9.3 будет закрыт).
4. **Snapshot round-trip**: применить пресет → `restore_settings_backup {snapshot_id}` →
   итоговые настройки побитово равны состоянию до применения (кроме `schema_version`, если
   миграция задета).
5. **Пробники для «УСЛОВНО-ДА»**: замокать `probe_llm_http`/отсутствие GigaAM venv/отсутствие
   HF-кэша → убедиться, что соответствующий ключ уходит в `skipped` с понятной причиной, а НЕ
   падает исключением и НЕ включается вслепую.
6. **Дохождение до `_get_runtime_setting`/`settings.<UPPER>` без рестарта** — для каждого «ДА»/
   «УСЛОВНО-ДА» кандидата, у которого это не проверено явно построчно в этом черновике, — тест,
   который меняет настройку через `apply_recommended_setup` и сразу (без пересоздания сервиса)
   проверяет эффект на реальном коллабораторе (аналогично уже существующему классу тестов
   `test_stt_router.py`).
7. **E2E-смок** (по паттерну `scripts/e2e_ipc_smoke.py`, см. `reference_live_e2e_smoke_method` в
   памяти проекта): дописать сценарий «свежий дата-дир → `get_hardware_profile` →
   `apply_recommended_setup {dry_run:true}` → `{dry_run:false}` → `get_settings` содержит
   ожидаемые ключи → `restore_settings_backup` откатывает» на РЕАЛЬНОМ (не мок) backend —
   ловит классы багов, которые юнит-тесты с пустой историей пропускают (прецедент:
   `get_topic_timeline` крашился только на реальных данных).
8. **Swift-сторона**: `RecommendedSetupStepController`/`HistoryPanelController+RecommendedSetup.swift`
   — source-контракт тест по аналогии с `test_setupErrorBus_is_actually_called_from_startup` /
   `test_setupHealthMonitor_is_actually_called_from_startup` (CLAUDE.md паттерн «test-validates-
   the-hole») — подтвердить, что новый шаг онбординга РЕАЛЬНО вызывается из
   `runModelDownloadStepThenComplete()`, а не просто существует как мёртвый класс.
9. **ubuntu-parity**: если реализация трогает что-то в `core/pipeline/` (маловероятно для этой
   волны, но проверить) — прогнать `make audit-all` перед мержем (см. CLAUDE.md
   mlx-masking-предупреждение).

---

## 11. Definition of Done (черновик, из роадмапа + уточнено)

Из ROADMAP §2 Волна 1: «свежий юзер после онбординга получает включённый безопасный максимум под
своё железо; владелец — кнопку «сделай мне хорошо» + откат». Уточнение черновика:

- [ ] Инвентаризация (эта таблица §3-4) прогейчена контролёром и владельцем; открытые вопросы
      §9 закрыты решениями (не обязательно все — минимум 9.1, 9.3, 9.4).
- [ ] `apply_recommended_setup` реализован, дефолтный набор — только строки с вердиктом ДА (10
      шт.) плюс любые УСЛОВНО-ДА, которые владелец явно утвердил включить с пробником.
- [ ] `dry_run` превью работает и показывается пользователю ДО любой записи.
- [ ] Снапшот создаётся перед применением; откат — существующий `restore_settings_backup`,
      без нового кода отката.
- [ ] Онбординг: новый шаг после `ModelDownloadStep`, graceful skip, не блокирует завершение
      онбординга при любой ошибке IPC.
- [ ] Настройки: новая секция с превью + «Применить» + «Отменить последнее».
- [ ] Ни один кандидат со статусом МЁРТВЫЙ/НЕТ не попадает в `applied` ни при каких входных
      данных (тест п.2 из §10).
- [ ] `privacy_mode_enabled=true` соблюдается (тест п.3 из §10).
- [ ] Живой e2e-смок (см. §10 п.7) зелёный на реальном backend, не только юнит-тесты.
- [ ] `make audit-all` зелёный, если затронуты extraction-паттерны.
- [ ] Релиз v2.5.0 (по плану роадмапа) после Sentry-свипа и стандартной колеи между волнами
      (ROADMAP §2 «Колея между волнами»).
