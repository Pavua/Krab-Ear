# Wake-word Watchdog — постоянный фикс audio-wedge (дизайн)

**Дата:** 2026-07-15
**Статус:** одобрен владельцем (подход B + авто-рестарт rate-limited), готов к writing-plans
**Автор:** Claude Fable 5 (брейнсторм-сессия), решения ратифицированы владельцем

---

## 1. Проблема

Слушатель wake word (`backend/openwakeword_adapter.py::_listen_loop`) — независимый
долгоживущий аудио-поток — может «заклинить» без единого исключения. Два живых
инцидента, две сигнатуры одного класса:

| Вариант | Инцидент | Симптом внутри процесса | Что лечило |
|---|---|---|---|
| **Шторм нулей** | 2026-07-12 (диктовка) | `stream.read()` возвращает кадры, но все сэмплы = 0; список устройств stale | Полный рестарт процесса; после — построен `AudioSelfHealer` с in-process reinit как soft-fix |
| **Зависшее чтение** | 2026-07-13 (wake word) | Тред жив, но завис до/внутри активации CoreAudio: ни кадров, ни исключений; индикатор микрофона не загорается | ТОЛЬКО полный рестарт процесса (`launchctl kickstart -k ai.krab.ear.backend`); рестарт одного треда (`wake_word_stop`/`start`) НЕ помог |

Sentry-контекст: `KRAB-EAR-BACKEND-1J`, `PortAudioError -9986`, 4747 исторических
срабатываний; текущий клин был «тихим» (0 свежих исключений за 17ч+ аптайма).

**Почему существующие механизмы слепы (4 подтверждённых пробела):**

1. `wake_word_status.running` = голый `thread.is_alive()` — при клине врёт `true`.
2. `OpenWakeWordAdapter.start()` — fire-and-forget: спавнит тред и сразу отвечает
   `ok:true`, не дожидаясь первого успешно прочитанного чанка.
3. `AudioSelfHealer` — триггер пассивный (счётчик пустых **диктовок** из
   `RecordingCoreService.handle_stop_recording`). Когда владелец не диктует —
   обычное состояние — заклинивший wake-word поток для него невидим.
4. Swift `WakeWordPoller.tick()` self-heal триггерится на `running == false` —
   т.е. на лживый сигнал из п.1 — и потому при клине никогда не срабатывает.
   Существующий circuit-breaker адаптера (`_MAX_CONSECUTIVE_STREAM_FAILURES`)
   покрывает только СИНХРОННЫЕ ошибки открытия потока (исключение из
   `sd.InputStream()`), не тихое зависание.

## 2. Цели / не-цели

**Цели:**
- Wake word самовосстанавливается после обоих вариантов клина без участия владельца.
- Правдивый сигнал здоровья потока в `wake_word_status` (аддитивно к контракту).
- Право на рестарт процесса остаётся на Swift-стороне (чартер `AudioSelfHealer`
  сохранён), но обретает работающий примитив для launchd-owned процесса.

**Не-цели (YAGNI):**
- Не генерализуем watchdog на прочие аудио-потоки (диктовку уже покрывает
  `AudioSelfHealer`; live subs — отдельный поток с собственным жизненным циклом,
  вне scope).
- Не добавляем barrier/блокировку в `start()` — правда переезжает в heartbeat,
  IPC-ответ остаётся мгновенным.
- Не трогаем мёртвые настройки `wake_word_enabled`/`wake_word_engine`
  (`core/config.py:884-886`, нигде не читаются) — кандидат на отдельную уборку.
- Не строим постоянную персистентность `wedged` — состояние in-memory,
  рестарт процесса легитимно его очищает.

## 3. Архитектура

```
_listen_loop ──(heartbeat: last_chunk_ts)──► WakeWordWatchdog (тик 5с)
                                                   │ stale ≥ wake_word_stale_sec
                                                   ▼
                                    AudioReinitCoordinator (single-flight,
                                    общий с AudioSelfHealer, уважает is_recording)
                                    ├─ stop() дождался треда → sd._terminate/_initialize
                                    │  → рестарт слушателя (model/threshold сохранены)
                                    └─ stop() таймаут (тред завис в PortAudio) ИЛИ
                                       повторный stale после reinit
                                                   ▼
                                  wedged:true в wake_word_status + ErrorBus
                                  audio.wakeword_wedged
                                                   ▼
                            WakeWordPoller (Swift): rate-limit ≥30 мин →
                            BackendSupervisor.forceRestartBackend()
                            (passive: launchctl kickstart -k; active: stop+ensure)
                                                   ▼
                            respawn → poller self-heal переармит wake_word_start
                            (путь проверен живым инцидентом 2026-07-13)
```

Принцип разделения: **правда о здоровье живёт там, где данные** (Python владеет
потоком — он и мерит); **право на убийство процесса живёт там, где оно уже есть**
(Swift supervisor с его backoff/circuit-breaker/toast).

## 4. Компоненты

### 4.1 `OpenWakeWordAdapter` — наблюдаемость (~40 строк правок)

- `_last_chunk_ts: float | None` (monotonic, под `self._lock`) — штампуется на
  каждом успешном `stream.read()`, в котором **есть хотя бы один ненулевой
  сэмпл** (`bool(flat.any())` — numpy, O(n) по чанку 1280 сэмплов, копейки).
  Ненулевой критерий закрывает оба варианта клина одним порогом: зависшее чтение
  не штампует вовсе, шторм нулей штампует только нулями → staleness растёт.
  Живой микрофон никогда не отдаёт секунды идеальных int16-нулей (noise floor),
  поэтому ложный staleness на «тихой комнате» исключён.
- `_listen_started_ts: float | None` — на входе `_listen_loop` (до открытия
  потока). Staleness считается от `max(_listen_started_ts, _last_chunk_ts)` —
  «ещё не прогрелся» не алармит раньше порога.
- `stop(timeout: float = 3.0) -> bool` — возвращает, вышел ли тред за timeout
  (`not thread.is_alive()` после join). `False` = тред застрял внутри
  PortAudio-вызова; вызывающий обязан считать мягкий reinit НЕБЕЗОПАСНЫМ
  (`Pa_Terminate()` при заблокированном в библиотеке треде — риск сегфолта).
  Все существующие вызовы `stop()` совместимы (возвращаемое значение аддитивно).
- **Generation-токен против зомби-воскрешения**: `self._generation: int`,
  инкремент в `start()` под `_lock`; `_listen_loop` получает свой
  `my_generation` аргументом и в НАЧАЛЕ КАЖДОЙ итерации проверяет
  `self._generation == my_generation` (наравне с `_stop_event`). Сценарий:
  старый тред завис → `stop()` таймаут → позже CoreAudio отвис → старый тред
  продолжил бы цикл параллельно с новым (двойной захват микрофона). Токен
  гарантирует: отвисший чужак выходит на первой же итерации.
- `handle_wake_word_status` — аддитивные поля: `last_chunk_ts`,
  `listen_started_ts` (оба `float | None`), `wedged: bool` (источник —
  watchdog, см. 4.3; адаптер только хранит флаг + сеттер/геттер под `_lock`).
- Сброс heartbeat-полей в `start()` (новая сессия — чистое состояние) и
  `stop()`.

### 4.2 `backend/audio_reinit.py` — `AudioReinitCoordinator` (новый, ~60 строк)

Единственный владелец танца «сохранить → остановить → переинициализировать →
восстановить». Переезжает из `AudioSelfHealer._perform_reinit` (хилер начинает
делегировать — устраняем будущий sibling-drift и класс «double-write одного
side effect из двух tap'ов»).

```python
class ReinitOutcome(str, Enum):
    OK = "ok"                        # reinit выполнен, слушатель перезапущен
    DEFERRED_RECORDING = "deferred"  # шла запись — попытка отложена, не потеряна
    THREAD_HUNG = "thread_hung"      # stop() не дождался треда — reinit пропущен
    FAILED = "failed"                # исключение внутри танца (залогировано)

class AudioReinitCoordinator:
    def __init__(self, *, reinit_audio_backend, is_recording,
                 wake_word_adapter=None): ...
    def reinit_with_wake_word_restore(self) -> ReinitOutcome: ...
```

- Внутри — `threading.Lock` (single-flight): `AudioSelfHealer` и
  `WakeWordWatchdog` физически не могут выполнять reinit одновременно.
  Конкурирующий вызов не блокируется в ожидании, а получает
  `DEFERRED_RECORDING`-подобный быстрый выход (non-blocking `acquire`) —
  повтор придёт со следующего триггера.
- `is_recording()` проверяется под локом непосредственно перед reinit —
  PortAudio никогда не дёргается под живой диктовкой (двойная проверка:
  на входе в танец fail-closed + re-check после `stop()`-join с
  восстановлением слушателя и `DEFERRED_RECORDING`).
- **Maintenance-окно (Critical ревью Task 4)**: на опасный участок танца
  (stop → `Pa_Terminate`) координатор помечает адаптер
  `begin_maintenance()`/`end_maintenance()` — чужой `start()` (в т.ч. IPC
  `wake_word_start` от поллер-self-heal, который видит `running:false` уже
  во время `stop()`-join) получает `ok:false` вместо спавна второго треда,
  под которым исполнился бы `Pa_Terminate`. Окно снимается в `finally` ДО
  restore-фазы: там гонка стартов вырождается в benign no-op
  («уже запущен»-гард; поллер и restore берут model/threshold из одного
  источника).
- **Stop-epoch гард restore (chip Finding 5)**: `stop_epoch` — монотонный
  счётчик публичных `stop()` адаптера (растёт и на no-op stop'ах). Танец
  снапшотит базу ДО своего stop() и ожидает ровно `+1`; любой ВНЕШНИЙ stop
  (toggle-off владельца / pause поллера) в любой фазе танца — включая
  конкурентный со stop-join — сдвигает счётчик мимо ожидания, и restore
  пропускается с INFO-логом: авто-восстановление не смеет включать микрофон
  обратно после явного «выключить». Остаточное µс-окно между epoch-чеком
  и `start()` принято (симметрично is_recording re-check).
- Порядок танца (как в текущем `_perform_reinit`): снять
  `active_model`/`active_threshold` → `adapter.stop()` → если тред не вышел →
  `THREAD_HUNG` (без `sd._terminate`!) → иначе `reinit_audio_backend()`
  (`sd._terminate(); sd._initialize()` — тот же callable из `service.py:1026`)
  → `adapter.start(saved_model, log_callback, threshold=saved_threshold)`.
- Callback детекции после рестарта — логирующий (как текущий
  `_on_wake_word_detected_after_reinit`): доставка детекций агенту идёт через
  `_record_detection()` безусловно внутри цикла, потеря пользовательского
  callback'а косметическая (существующее поведение хилера, сохраняем).
- `AudioSelfHealer` сохраняет свой поведенческий контракт
  (`record_success`/`record_empty_result`, streak/threshold/escalate)
  нетронутым; его КОНСТРУКТОР меняется — аргументы `reinit_audio_backend` +
  `wake_word_adapter` заменяются одним `reinit_coordinator` (внутренность
  `_perform_reinit` становится делегацией). Правятся оба call-site:
  `service.py:1035` и тесты (мок координатора вместо мока адаптера).

### 4.3 `backend/wake_word_watchdog.py` — `WakeWordWatchdog` (новый, ~120 строк)

DI-стиль `AudioSelfHealer` — всё инжектится, тестируется фейками без
sounddevice/тредов:

```python
class WakeWordWatchdog:
    def __init__(self, *, adapter, reinit_coordinator, error_bus=None,
                 settings_get=None, clock=time.monotonic,
                 check_interval_sec: float = 5.0): ...
    def start(self) -> None: ...   # спавнит daemon-тред (Event.wait-цикл)
    def stop(self) -> None: ...    # обязателен в BackendService.close() (#1782)
    def check_once(self) -> str | None: ...  # один тик; возвращает выполненное
                                             # действие ("healed"/"escalated"/None)
                                             # — для юнит-ассертов
    def state(self) -> dict: ...   # для get_diagnostics
```

Логика тика (`check_once`), в порядке гардов:

1. `wake_word_watchdog_enabled` False → no-op.
2. Сессия адаптера не активна — ДВЕ разные ветки (уточнение ревью Task 4,
   Important №4):
   - **Чистая пауза** (`active_model()` is None — чистый `stop()` его
     зануляет): recording/conversation/TTS/privacy, Swift снял слушатель →
     сброс эпизода и anomaly-таймера, no-op. Ложных срабатываний нет
     структурно.
   - **Мёртвая сессия** (`active_model()` НЕ None при `is_running()` False —
     сигнатура упавшего restore/умершего цикла: слушатель должен жить, но
     треда нет): эпизод НЕ сбрасывается; взводится anomaly-таймер, поллеру
     даётся grace `wake_word_stale_sec` на оживление (его self-heal видит
     правдивый `running:false`), затем эскалация `dead_session` (однократно
     на эпизод). Без этой ветки полностью мёртвый слушатель (например,
     `_load_model` упал в restore) маскировался бы под паузу навсегда.
   (Пометка: `is_running()` при зависшем треде врёт `true` — поэтому решение
   о КЛИНЕ принимает heartbeat-шаг ниже, а эта ветка ловит именно МЁРТВУЮ
   сессию.)
3. `listen_started_ts` ещё `None` (тред спавнут, но не вошёл в цикл —
   микросекундное окно) → считается свежим, no-op.
   Иначе — два РАЗНЫХ условия (уточнение против ловушки heal-цикла,
   найдено при written-plans self-review):
   - **close-условие**: `last_chunk_ts` не `None` И `clock() - last_chunk_ts
     < wake_word_stale_sec` → эпизод закрывается (включая `wedged=False`,
     если был выставлен), no-op. Эпизод закрывает ТОЛЬКО реальный свежий
     чанк — НЕ свежий `listen_started_ts`: иначе после heal новая сессия
     закрывала бы эпизод своим grace-окном, и watchdog зациклился бы
     heal'ом каждые ~35с, никогда не эскалируя.
   - **alarm-условие**: `staleness = clock() - max(listen_started_ts,
     last_chunk_ts or 0)`; если `staleness < wake_word_stale_sec` —
     grace-окно прогрева: не алармим И НЕ закрываем эпизод, no-op.
4. Stale, heal в этом эпизоде ещё не пробовали → сначала **анти-шторм гейт**
   (ревью Task 4, Important №3): скользящее окно heal-попыток ПОВЕРХ
   эпизодов (`_heal_history`, константы 3 попытки / 600с) — если владелец
   диктует чаще, чем раз в ~(stale+tick), легитимные паузы сбрасывали бы
   эпизод раньше второй stale-проверки, и сломанный навсегда поток лечился
   бы вечно без эскалации. ≥3 heal'ов в окне → эскалация `heal_storm`
   вместо четвёртого heal'а. DEFERRED/BUSY-ретраи в историю не попадают;
   THREAD_HUNG-танцы ПОПАДАЮТ (Fable-гейт, 1b: каждый стоил 3с stop-join
   и оставил зомби-тред — respawn-циклы при персистентном клине обязаны
   капиться). Иначе → `coordinator.reinit_with_wake_word_restore()`:
   - `OK` → пометить «heal попробован», ждать следующего тика (если heartbeat
     ожил — эпизод закроется в п.3; если снова stale → п.5).
   - `DEFERRED_RECORDING` → ничего не помечать, повтор на следующем тике.
   - `THREAD_HUNG` → сразу п.5 (мягкое лечение физически небезопасно —
     живое свидетельство 13-07: лечит только рестарт процесса).
   - `FAILED` → пометить «heal попробован» (эквивалент неудачной попытки).
5. Эскалация (однократно на эпизод): `adapter.set_wedged(True)` + ErrorBus
   `audio.wakeword_wedged` + `logger.error`. Дальше watchdog молчит до
   закрытия эпизода (реальный свежий чанк / неактивная сессия / рестарт
   процесса) — реакция переходит на Swift-сторону.

«Эпизод» = непрерывный интервал staleness внутри одной сессии слушателя.
Закрывается ТОЛЬКО реальным свежим чанком (`last_chunk_ts`), неактивной
сессией (Swift снял слушатель — паузы recording/conversation/TTS/privacy)
или рестартом процесса. Свежий `listen_started_ts` сам по себе эпизод НЕ
закрывает (см. close-условие в шаге 3).

Жизненный цикл: конструируется в `service.py` рядом с `AudioSelfHealer`
(см. 4.5), `start()` сразу же (тред дешёвый: один лок-рид каждые 5с),
`stop()` — в `BackendService.close()`. Тик в heal-пути может длиться до
~35с (stop-join 3с + `_load_model` до 30с) — `stop().join(2с)` при этом
таймаутится: логируется WARN, daemon дорабатывает текущий тик и выходит
по `stop_event` (направление отказа принято, #1782-класс задокументирован). К `RecordingCoreService._rt_lock`
НЕ привязывается: watchdog не участвует в recording start/stop, он
самогейтится по состоянию сессии адаптера (правило `_rt_lock` касается
демонов, стартуемых/стопаемых синхронно с записью — не наш случай).

### 4.4 ErrorBus — новый код `audio.wakeword_wedged`

В `ERROR_REGISTRY` (`backend/error_codes.py`), компонент `audio` — рядом с
существующим `audio.stack_wedged` (категории `wakeword` в реестре нет,
не заводим ради одного кода): severity `error`, `user_msg_ru` вида
«Wake word завис — требуется перезапуск Krab Ear…» (текст НЕ обещает
немедленный рестарт — агент может отложить его по rate-limit/give-up cap),
`actionable=False`. Пуш идёт через существующий
`ErrorBus.push` → дедуп/ring buffer/Sentry-tier бесплатно; toast у
владельца появится через живой `ErrorBusPoller`-путь.

### 4.5 Проводка в `service.py` (+ настройки)

- Рефактор блока `service.py:1021-1042`: конструируется
  `AudioReinitCoordinator(reinit_audio_backend=_reinit_audio_backend,
  is_recording=..., wake_word_adapter=self._wake_word_adapter)`;
  `AudioSelfHealer` получает координатор (вместо прямых
  reinit/adapter-аргументов — его конструктор упрощается, тесты правятся);
  `WakeWordWatchdog` конструируется следом и стартует.
- `BackendService.close()` — `self._wake_word_watchdog.stop()` (правило #1782:
  каждый daemon-тред обязан останавливаться в close(), иначе chunk-CI
  exit(1)).
- `DEFAULT_SETTINGS` (`core/config.py`): `wake_word_watchdog_enabled: True`,
  `wake_word_stale_sec: 30`.
- `settings_validator.py`: `wake_word_watchdog_enabled` → `_BOOL_FIELDS`;
  `wake_word_stale_sec` → `_RANGE_FIELDS` c clamp (10, 120).
- `get_diagnostics` — новая подсекция `wake_word_watchdog` из
  `watchdog.state()`: `{enabled, session_active, staleness_sec,
  heal_attempted_this_episode, wedged}` — для дебаг-панели и live-смока.

### 4.6 Swift: `WakeWordPoller` + `BackendSupervisor.forceRestartBackend()`

**Чистая решающая логика** (новая структура в стиле
`WakeWordDetectionTracker`, юнит-тестируемая без таймеров/IPC):

```swift
struct WedgedEscalationTracker {
    static let minGapSec: TimeInterval = 1800   // ≥30 мин между рестартами
    static let maxConsecutive = 3               // give-up cap (Fable-гейт, F2)
    private var lastEscalationAt: TimeInterval?
    private(set) var consecutiveEscalations = 0
    var exhausted: Bool                          // >= maxConsecutive
    mutating func noteHealthy()                  // реальный чанк → cap перевзведён
    mutating func shouldEscalate(wedged: Bool, now: TimeInterval) -> Bool
    // true ровно когда: wedged && !exhausted && (lastEscalationAt == nil ||
    //                              now - lastEscalationAt >= minGapSec)
}
```

- **Give-up cap (Fable-гейт волны, Finding 2)**: restart-immune состояния
  микрофона (громкость входа 0 / hardware mute / TCC-нули) рестарт процесса
  НЕ лечит — без капа kickstart повторялся бы каждые 30 минут навсегда
  (48/сутки, каждый убивает in-flight работу backend'а — регрессия против
  baseline «тихо молчащий wake word»). После `maxConsecutive` эскалаций
  без единого здорового сигнала между ними — авто-рестарты прекращаются,
  однократный actionable-тост «проверьте микрофон / выключите тумблер»
  (callback `onWedgedGiveUp`). Здоровый сигнал = ПРИСУТСТВИЕ
  `last_chunk_ts` в status (реально захваченный чанк сессии); wedged-флап
  (start() временно сбрасывает флаг) здоровьем не считается. Принятая
  граница семантики: цикл «kickstart реально оживил мик на время → снова
  клин» перевзводит cap каждый цикл — это осознанно (каждый рестарт
  доказуемо доставил ценность), freshness-порог не вводим (YAGNI).
- `WakeWordPoller.tick()`: парсит `wedged` из `wake_word_status`; порядок
  блоков: healthy-note (`last_chunk_ts != nil` → `noteHealthy`) → детекция →
  wedged-эскалация (`tracker.shouldEscalate` → лог + injected callback
  `onWedgedEscalation: () -> Void`) → однократный give-up
  (`wedged && exhausted`) → self-heal. Гонки in-flight покрыты существующим
  гардом (`timer != nil, pausedReasons.isEmpty`).
- **Self-heal подавлен при wedged (Fable-гейт, Finding 1c)**: ветка
  респавна — `if !running && !wedged`; пока backend объявил wedged,
  `sendStart` НЕ шлётся: `start()` сбросил бы wedged и замаскировал
  эскалацию, а рестарт треда этот класс клина не лечит (живое свидетельство
  13-07). Это же убивает цикл «respawn → новый тред виснет → THREAD_HUNG →
  эскалация» каждые ~40с с утечкой зомби-треда за цикл. НАМЕРЕННЫЙ байпас:
  `resume(force:true)`/`activate()` шлют start и при wedged — это механизм
  честной пере-эскалации после диктовок И единственный авто-путь оживления
  до 30-мин kickstart'а, если клин самоустранился (пере-ревью Fable-гейта,
  ANSWER_1A).
- **Бюджет self-heal освежается наблюдённой живой сессией** (`running:true`
  → `failedStartAttempts = 0`): отказы старта из-за maintenance-окна танца
  не выжигают 3 попытки навсегда (Fable-гейт, F4).
- `main.swift` проводка: `onWedgedEscalation` → toast «Wake word завис —
  перезапускаю backend…» → `supervisor.forceRestartBackend()` (off-main,
  как остальные IPC/Process-вызовы) → toast результата (успех/провал),
  зеркаля стиль `setOnHangDetected`-callback'а из `main+HealthMonitor.swift`.
- **`BackendSupervisor.forceRestartBackend() -> Bool`** — новый метод.
  НЕ переиспользуем `restartIfDead()`: тот short-circuit'ится на живом
  процессе (`isBackendAlive()` → true → return), а наш случай — «жив по IPC,
  мёртв по аудио». Семантики «перезапусти если умер» и «убей заведомо
  живого» не смешиваются в одной функции:
  - `.passive` (прод, launchd Variant B): `Process` →
    `/bin/launchctl kickstart -k gui/<uid>/ai.krab.ear.backend`
    (uid — `getuid()`; label — константа рядом с существующими
    supervisor-константами). Это ровно ручной рецепт, вылечивший инцидент
    13-07. Неноль от launchctl → `false`, лог, БЕЗ ретраев до следующего
    30-мин окна. (`spawn-failed`/EX_CONFIG рецепт bootout+bootstrap относится
    к смене бинаря — здесь бинарь не менялся, kickstart достаточен.)
  - `.active` (dev standalone): `stopBackend()` + `ensureBackendRunning()`,
    переиспользуя бюджет `consecutiveRestarts`.
- После рестарта: HealthMonitor увидит краткие ping-фейлы (штатно),
  `WakeWordPoller` self-heal переармит `wake_word_start` в течение цикла
  поллинга — путь проверен живым инцидентом 2026-07-13. `tracker`
  детекций re-arm'ится существующей логикой (`ts == nil` → baseline reset).

## 5. Сценарии (end-to-end)

1. **Норма**: чанки идут → heartbeat свежий → watchdog no-op (один лок-рид/5с).
2. **Шторм нулей** (сигнатура 12-07): кадры нулевые → 30с без ненулевого чанка
   → координатор: `stop()` успешен → `sd` reinit → рестарт слушателя →
   heartbeat ожил → эпизод закрыт. Не ожил → wedged → Swift рестартит backend.
3. **Зависшее чтение** (сигнатура 13-07): чанков нет → stale → `stop()`
   таймаут → `THREAD_HUNG` → **немедленно** wedged (без опасного
   `Pa_Terminate`) → ErrorBus + toast → kickstart → respawn → poller
   переармил → wake word живой. Время до самолечения ≈ stale(30с) + тик(≤5с)
   + kickstart/respawn(~5-10с) ≈ **под минуту** против «до ручного вмешательства».
4. **Stale во время диктовки**: координатор → `DEFERRED_RECORDING` → повтор
   на следующем тике; PortAudio под записью не трогается. (Обычно недостижимо:
   Swift ставит wake word на паузу при записи → сессия не активна → гард 2.)
5. **Privacy mode**: адаптер сам выходит из цикла (`_privacy_blocked`),
   Swift шлёт stop → сессия не активна → watchdog молчит; эскалация
   невозможна структурно.
6. **Рестарт-шторм невозможен**: одна эскалация на эпизод (Python) ×
   rate-limit 30 мин (Swift) × give-up cap 3 подряд-эскалаций без здорового
   сигнала (Swift) × launchd ThrottleInterval. Ложный staleness стоит
   максимум один цикл stop/reinit/start (~1-2с тишины микрофона) раз
   в эпизод; THREAD_HUNG-танцы тоже тратят шторм-окно (3/600с) — цикл
   «respawn → hang → dance» не может плодить зомби-треды бесконечно.
7. **Restart-immune микрофон** (громкость входа 0 / TCC-нули): heal
   «успешен», чанков нет → wedged → до 3 kickstart'ов (каждый — шанс, что
   лечилось процессом), затем give-up + actionable-тост; wake word молчит
   с честным `wedged:true` в диагностике до ручного вмешательства.

## 6. Направления отказов (fail-safe)

- Исключение в тике watchdog'а ловится, логируется, тред живёт — уронить
  процесс он не может. Сломанный `settings_get` → дефолты (enabled=True,
  stale=30).
- ErrorBus отсутствует/упал → только лог; Swift-эскалация НЕ зависит от
  ErrorBus (читает `wedged` из status).
- Отказ направлен в сторону «микрофон молчит дольше» (deferred/rate-limit),
  никогда — «ложные срабатывания wake word» (симметрично фиксу #1876).
- Утёкший зависший тред: принят (daemon, generation-токен гарантирует выход
  при отвисании); максимум один на эпизод, эпизод завершается рестартом
  процесса.
- Принятый компромисс: kickstart убьёт возможные batch-транскрипции в очереди
  (rare: клин wake word обычно совпадает с idle; поллер и так на паузе во
  время записи/разговора). Зафиксировано осознанно, mitigations не строим
  (YAGNI).

## 7. Тестирование

**Python (unit, фейки + инжектированный clock; тред-стабы — duck-type, НЕ
наследники `threading.Thread`):**
- Адаптер: heartbeat штампуется только ненулевыми чанками; сброс в
  start/stop; `stop() -> bool` оба исхода; generation-токен выгоняет
  зомби-тред; аддитивные поля status.
- Координатор: single-flight (конкурирующий вызов не блокируется),
  `is_recording` → DEFERRED, `THREAD_HUNG` без вызова reinit-callable,
  сохранение/восстановление model/threshold, все 4 исхода.
- Watchdog: полная матрица `check_once` (гарды 1-3, heal-путь, THREAD_HUNG
  → немедленный wedged, однократность эскалации на эпизод, закрытие эпизода
  свежим heartbeat + сброс wedged), clamp настроек.
- `AudioSelfHealer`: существующие тесты перепривязываются к делегации
  координатору, счётчиковая семантика не меняется (регрессия-гард).
- Teardown: каждый тест с `BackendService` — `service.close()`; проверить,
  что `close()` останавливает watchdog-тред.
- Контракт: дополненный `wake_word_status` + `get_diagnostics.wake_word_watchdog`;
  dispatch-инварианты.

**CI-дисциплина:** `make pre-merge-check` (ubuntu-parity py3.12, sounddevice
на CI нет — всё через фейки), flake8 CI-командой, `make audit-all` (новые
extracted-модули `audio_reinit.py`/`wake_word_watchdog.py` обязаны иметь
production-импортёров — проводка 4.5 это гарантирует).

**Swift (unit):** `WedgedEscalationTracker` — первая эскалация, подавление
внутри 30-мин окна, повтор после окна; парсинг `wedged` в tick (по образцу
существующих поллер-тестов).

**Живой смок:** `scripts/e2e_ipc_smoke.py` дополняется проверкой новых полей
`wake_word_status`/`get_diagnostics` (санити значений). Реальный клин по
команде не воспроизводится — компенсация: юнит-матрица обоих вариантов по
живым сигнатурам инцидентов + ручная проверка эскалационного пути один раз
руками (временно занизить `wake_word_stale_sec` до 10 на dev-инстансе и
заглушить heartbeat дебаг-хуком — шаг плана, не прод-код).

## 8. Открытые вопросы

Нет — обе развилки (владение watchdog'ом; политика эскалации) закрыты
владельцем 2026-07-15.
