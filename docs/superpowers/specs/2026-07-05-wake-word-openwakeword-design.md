# Живой wake word через openWakeWord — дизайн

- **Дата:** 2026-07-05
- **Статус:** одобрен (брейнсторм), готов к плану реализации
- **Автор:** Claude (Opus), совместно с владельцем
- **Волна:** A2 из карты идей (боли ежедневного использования + анлок экосистемы-ассистента)

## Проблема

Слово-пробуждение «Краб» **никогда не работало в проде**. Swift `WakeWordListener.swift`
завязан на Porcupine (Picovoice), но `createPorcupineEngine()` возвращает `nil` —
это заглушка («Временная заглушка до добавления реального SDK»). Реальный SDK
требует платного-ish access-ключа + вручную обученного `.ppn`. В логах каждый
старт агента: «Wake word выключен».

При этом в Python-бэкенде уже лежит **полностью готовый и укреплённый**
`backend/openwakeword_adapter.py` (openWakeWord, Apache-2.0, без signup):
- сам захватывает микрофон через `sounddevice` (собственный `_listen_loop`),
- privacy-гейт (`handle_wake_word_start` отказывает при `privacy_mode_enabled`),
- клампинг порога `[0.05, 1.0]` (защита от скрытого прослушивания),
- проверка symlink/path-escape для кастомных моделей,
- таймаут загрузки модели,
- 4 IPC-хендлера, **уже зарегистрированных** в диспетчере `service.py`
  (`wake_word_list_models`, `wake_word_start`, `wake_word_stop`, `wake_word_status`,
  строки ~1884–1888).

Единственный пробел на Python-стороне: при детекции `_on_detected` только пишет
в лог — не публикует ничего, что мог бы получить Swift.

**Задача — соединить готовые части, а не строить заново** (anti-rebuild).

## Транспортная реальность (load-bearing)

Прод состоит из **двух отдельных процессов** (launchd Variant B) с **раздельными
in-process EventBus**:
- `service.py` (IPC-бэкенд) — тут живёт wake word-адаптер и `error_bus`; слушает
  **только** Unix-сокет `krabear.sock`, TCP-порта не открывает.
- `rest_server.py` (порт 5005) — обслуживает SSE `/v1/events` из **своего**
  EventBus; моста к EventBus IPC-бэкенда нет (проверено: rest_server не релеит
  события IPC-процесса, service.py не шлёт в rest_server).

**Следствие:** событие, эмитнутое в EventBus `service.py`, НЕ доходит до Swift
через SSE на 5005. Поэтому wake word использует **не** SSE, а тот же Unix-IPC-канал,
который агент уже надёжно держит (health-ping каждые 3с; поллинг
`get_recording_state` для realtime-оверлея — устоявшийся идиом кодовой базы).

## Цели / не-цели

**Цели (MVP):**
- Wake word реально срабатывает: сказал слово → запускается режим «Разговор с AI».
- Встроенная модель openWakeWord (`hey_jarvis` по умолчанию) — ноль обучения.
- Приватность: микрофон wake word не открывается в privacy mode; детекция не несёт
  транскрипт.
- Корректная координация микрофона с записью и разговором.
- Удаление мёртвого Porcupine-кода.

**Не-цели (следующие волны):**
- Русская модель «Краб» (.onnx) — требует ~15 мин обучения в Jupyter; отдельная волна.
- Server-push по IPC (broadcast) — не нужен, поллинг покрывает.
- Per-frame IPC-стриминг аудио из Swift (отвергнутый подход 3).
- Починка кросс-процессного krab_error SSE-моста — отдельная тема.

## Архитектура (вариант B — IPC-поллинг статуса)

```
Swift agent (main process)          Python IPC backend (service.py)
──────────────────────────          ──────────────────────────────────
toggle ON
  │ IPC wake_word_start ───────────► OpenWakeWordAdapter.start()
  │   {model, threshold}                 sd.InputStream(16k) → oww.predict()
  │                                          │ score ≥ threshold
  │                                          ▼
  │                                     _on_detected: записать
  │                                     last_detection={model, score, ts}
  │  poll every ~0.75s:
  │ IPC wake_word_status ───────────► handle_wake_word_status →
  │  ◄── {running, engine_available,      {..., last_detection:{model,score,ts}}
  │       last_detection:{...,ts}} ───┘
  ▼
  ts вырос со времени прошлого опроса?
    → triggerConversationFromWakeWord()
```

Swift **не** держит свой AVAudioEngine для wake word. Один владелец микрофона
(Python), ноль per-frame IPC, ноль кросс-процессной SSE-зависимости.

## Изменения по компонентам

### Backend (`backend/openwakeword_adapter.py`)
1. Хранить последнюю детекцию **как состояние адаптера**: поле
   `self._last_detection: dict | None = None`, обновляется **в точке детекции
   внутри `_listen_loop`** (там, где `score >= threshold`) под `self._lock` →
   `{"model": name, "score": float, "ts": time.monotonic()}`. Владение — на
   адаптере, НЕ зависит от внешнего `on_detected`-колбэка (тот остаётся для
   логирования). Монотонный таймстамп для дебаунса на стороне агента (не
   wall-clock — согласуется с ограничением среды на `Date.now()`).
   `start()` сбрасывает `_last_detection = None` (свежее состояние на сессию),
   `stop()` тоже.
2. `handle_wake_word_status` дополнительно возвращает `"last_detection"` (или `None`)
   под локом. Существующие поля (`running`, `active_model`, `engine_available`) —
   без изменений (контракт-совместимо, только добавление).
3. `handle_wake_word_start` уже принимает `model`, `threshold` и гейтит privacy —
   без изменений по логике. Внешний `_on_detected` в хендлере остаётся
   логирующим; состояние `last_detection` пишет сам loop (см. п.1), поэтому статус
   отражает детекцию независимо от того, кто установил колбэк.

### Swift agent
4. **`setupWakeWordListenerIfEnabled()` (main.swift:476)** — вместо создания
   локального `WakeWordListener` шлёт IPC `wake_word_start {model, threshold}`
   **off-main** (паттерн AGENT-3), затем запускает лёгкий poll-таймер.
5. **Новый poll-механизм** — по образцу `HealthMonitor` (actor, 3с ping):
   пока wake word включён И не на паузе, опрашивать `wake_word_status` раз в ~0.75с
   off-main; при росте `last_detection.ts` относительно последнего виденного →
   на main-потоке `historyPanel?.triggerConversationFromWakeWord()`. Первый
   опрос инициализирует «последний виденный ts» без триггера (чтобы старая
   детекция не выстрелила сразу).
6. **`applyWakeWordEnabled(false)`** → IPC `wake_word_stop` + остановка поллинга.
7. **Удаление мёртвого кода:** `WakeWordListener.swift` (~250 строк Porcupine-пути +
   AVAudioEngine-захват + `PorcupineEngineProtocol`) удаляется; связанные
   AVAudioEngine-тесты удаляются/переписываются. Триггер-цепочка
   `triggerConversationFromWakeWord()` → `triggerConversationStart()` остаётся.
   Свойство `wakeWordListener` заменяется на дескриптор poll-таска.

## Координация микрофона

Wake word **на паузе во время активной записи И активного разговора**,
возобновляется после — по образцу `recordingDidStart()/recordingDidStop()`
(main+RealtimeOverlay.swift), которым уже пользуется streamingPasteController.

- `recordingDidStart` / старт разговора → IPC `wake_word_stop` + пауза поллинга.
- `recordingDidStop` / конец разговора → IPC `wake_word_start` + возобновление
  поллинга (только если тумблер включён и не privacy mode).

Причина: (а) иначе слушатель сработает на собственную диктовку пользователя;
(б) избегаем дубль-владения микрофоном между wake word, записью и разговором.

## Опциональная зависимость `openwakeword`

Сейчас не установлена (закомментирована в `requirements.txt`), адаптер честно
уходит в stub (`is_available()` → False).

- Оставить **опциональной**, в отдельной секции `requirements.txt` (не тянуть в
  CI-ubuntu — там нет смысла и вес лишний).
- Доустановка: `bootstrap_backend.command` (наш свежий инсталлятор) и
  `Start Krab Ear.command` доставляют `openwakeword` при setup.
- Если движок недоступен (`engine_available=False` из `wake_word_status`): тумблер
  показывает «openWakeWord не установлен» с кнопкой-подсказкой — **не молчаливый
  no-op**. Агент не запускает поллинг, если `engine_available=False`.

## Настройки и UI

Тумблер «Детектор пробуждения» уже существует (Settings → Аудио-пайплайн,
UserDefaults `KrabEar_WakeWordEnabled` + backend-setting `wake_word_enabled`).
Добавить:
- индикатор `engine_available` (установлен / нет + подсказка),
- пикер модели (`wake_word_list_models` IPC уже отдаёт builtin+custom),
- слайдер порога (default 0.5, диапазон клампится бэкендом).

Визуальный дизайн контролов — через agy/Gemini (правило «визуал → Gemini»);
Swift даёт точную карту проводки IPC. Auto Layout / поведенческая проводка — Claude.

## Приватность и безопасность

- `handle_wake_word_start` уже отказывает при `privacy_mode_enabled` (микрофон не
  открывается) — переиспользуем.
- При включении privacy mode на лету агент шлёт `wake_word_stop`; при выключении —
  восстанавливает по тумблеру.
- Событие/поле детекции несёт **только** `model` + `score` + `ts` — **ноль
  транскрипта**, приватно by design.
- Порог клампится `[0.05, 1.0]` (защита от скрытого прослушивания) — уже есть.

## Стратегия тестирования

**Python (`tests/`):**
- Детекция обновляет `last_detection` с корректным payload (мок `oww.predict`
  возвращает score выше порога) — fail-before/pass-after.
- `handle_wake_word_status` включает `last_detection`; контракт-совместимость
  (старые поля на месте).
- Privacy-гейт: `handle_wake_word_start` при privacy mode не стартует и не пишет
  детекцию.
- Ubuntu-parity (`pre_merge_py312_check.sh`) на изменённом тест-файле; `openwakeword`
  отсутствует на ubuntu → адаптер в stub, тесты мокают, не импортируют реальную либу.

**Swift (`Tests/`):**
- Poll-логика: рост `last_detection.ts` → вызов триггера (мок IPC-клиента);
  первый опрос НЕ триггерит (инициализация baseline).
- Mic-pause: `recordingDidStart` → `wake_word_stop`-вызов; `recordingDidStop` →
  `wake_word_start` (при enabled+не-privacy).
- `swift build -c release` + глиф-гейт (нет новых non-ASCII глифов).

## Развёртывание и верификация

- Мерж по зелёному CI (backend-tests ×2, swift-build, аудит-стражи).
- `openwakeword` установить локально в `.venv_krab_ear` для живого дыма.
- Живой смок: включить тумблер → `wake_word_status` показывает `running=true`,
  `engine_available=true`; произнести `hey jarvis` → в логах детекция → агент
  открывает «Разговор с AI»; privacy mode → микрофон не открывается.
- Рестарт прод-бэкенда (kickstart) для подхвата Python-изменений; Swift —
  rebuild+deploy+parity-коммит бинарей (меняется main.swift + удаляется файл).

## Оценка объёма и риск

- **Объём:** M. ~30 строк Python (last_detection + status-поле), ~150 строк Swift
  (IPC-старт/стоп + poll-actor + mic-координация) минус ~250 строк удалённого
  мёртвого кода. Плюс тесты.
- **Риск:** низкий — переиспользуем укреплённый адаптер; транспорт через
  существующий надёжный IPC-идиом; чистое удаление никогда-не-работавшего кода.

## Открытые вопросы / follow-ups

- **Русская модель «Краб»** — обучить .onnx (~15 мин Jupyter), положить в
  `{data_dir}/wake_word_models/`, добавить в пикер. Отдельная волна.
- **Кросс-процессный krab_error SSE** — похоже, IPC-backend ошибки не доходят до
  Swift-тостов в проде (два EventBus без моста). Отдельное расследование, не блокер
  для wake word.
- **Адаптивная частота поллинга** — при желании снизить латентность до <1с можно
  0.5с в idle; 0.75с — баланс латентность/нагрузка для MVP.
