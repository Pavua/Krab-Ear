# W2 — Стабильность ежедневной диктовки

Дата: 2026-08-16. Статус: **спека**. Код продукта — только после явного Approve
владельца и **после закрытия W1**. Не начинать в том же PR, что W0/playbook.

Оперативный фронт: [`docs/NOW.md`](../../NOW.md). Рельсы:
[`docs/EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md). Журнал: ROADMAP-2026H2.

Критерий волны: владелец диктует каждый день. Сначала не терять диктовку и не
врать статусом, потом помогать Voice Gateway. Не архитектура «для красоты».

После Approve спека режется на **три маленькие карточки** (не монолит):

| Карточка | Что | Модель |
|---|---|---|
| W2a | Только доки: `CLAUDE.md` про HealthMonitor | Composer Fast |
| W2b | Замер аномалии длительности (keep-WAV + скрипт) | Grok high / Composer |
| W2c | Per-request `deadline_sec` на REST STT под VG | Grok high, Plan если контракт VG поплывёт |

---

## 0. Анти-ребилд: HealthMonitor sticky-hang уже закрыт

План-черновик 2026-08-16 ещё ставил в очередь «эскалацию к `forceRestartBackend()`
под `WedgedEscalationTracker`». Это **docs drift**, не отсутствующая фича.

Живой код (не перестраивать, не дублировать):

- `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift`
  - `wedgeThreshold = 20` (дефолт, ~60 с при ping 3 с)
  - `wedgeReprobeInterval = 60`
  - `sawHealthyPing` — эскалация запрещена, пока процесс ни разу не ответил
    (иначе убиваем медленный импорт torch)
  - `setWedgeProbe` / `setOnWedgeDetected` / `setOnHealthyPing`
- `native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift`
  - проба: возраст процесса ≥ **600 с**, затем `ping` timeout 5 с
  - `isBackendWedgeEvidence`: `true` только на `IPCError.socketConnectFailed`;
    **`timeout` → false** (медленный-но-живой backend)
  - `setOnWedgeDetected` → `WedgedEscalationTracker` (30 мин, кап 3) →
    `supervisor.forceRestartBackend()`, не `restartIfDeadDetailed()`
  - гейт записи/встречи: `isRecording` / `activeGenerationOwner` /
    `meetingPanelController?.isMeetingLive`
- Тесты: `native/KrabEarAgent/Tests/KrabEarAgentTests/HealthMonitorWedgeTests.swift`

`hangFiredForCurrentEpisode` по-прежнему одноразовый на эпизод для **первой**
ступени (`restartIfDeadDetailed`). Вторая ступень **намеренно не одноразовая**:
частоту режет `wedgeReprobeInterval`, число рестартов — трекер.

### 0.1 Оставшийся класс (не чинить в W2)

Accept-loop жив, хендлеры стоят: ping упирается в **timeout**, проба возвращает
`false`, `forceRestartBackend` не зовётся. Это не недоделанный sticky-hang, а
сознательный fail-safe (инциденты 2026-07-22 kickstart под записью и 2026-08-03
убийство backend'а на 46-секундной финализации). Лечение — heartbeat
«последний успешно завершённый хендлер», не «рестартить на timeout». Отдельная
волна, если вообще понадобится.

### 0.2 W2a — только правка CLAUDE.md (после Approve)

В `CLAUDE.md`, абзац про `HealthMonitor.swift` (секция Phase A Swift additions)
сейчас описывает гэп 2026-08-07 как открытый. Заменить хвост после «Сигналы
отсюда НЕ шлются…» на текст ниже. Не трогать соседние абзацы.

Замена (хвост абзаца, начиная с «🔴 Известный гэп»):

```
Вторая ступень заклинивания ЗАКРЫТА: `setWedgeProbe` + `setOnWedgeDetected` в
`HealthMonitor.swift` / `main+HealthMonitor.swift` — `forceRestartBackend()` под
`WedgedEscalationTracker`, только если соединение ОТВЕРГАЕТСЯ
(`IPCError.socketConnectFailed`), процесс старше 600 с, был хотя бы один живой
ping, и нет записи/встречи. `IPCError.timeout` намеренно НЕ эскалация
(медленный backend ≠ заклинивший). Тесты: `HealthMonitorWedgeTests.swift`.
Оставшийся класс «accept жив, хендлеры стоят» — не этот сторож; см. спеку
`docs/superpowers/specs/2026-08-16-w2-daily-stability-design.md` §0.1.
Phase B.1: подписка на `rewriter_recovered` через EventBridge живая.
```

DoD W2a: `grep hangFiredForCurrentEpisode CLAUDE.md` больше не соседствует с
фразой «одна безрезультатная попытка» как с открытым гэпом. Код Swift не в
диффе.

---

## 1. Аномалия длительности — сначала замер, не фикс чанкера

### 1.1 Факт

Живой лог 2026-08-12 00:19:51 (диктовка ~42 с, см. спеку
`2026-08-12-dictation-latency-and-overflow-design.md` §4):

- VAD pre-filter: `total=41.22s` (`engine.py`, лог
  `"VAD pre-filter: … total=%.2fs"`, `total_sec = len(audio) / sample_rate`)
- 15 мс спустя GigaAM chunker: `duration=70.8s`
  (`engine.py`, `"GigaAM chunker path: duration=%.1fs"`,
  `duration_sec = len(audio_data_np) / 16000.0`)

Обе цифры — длина массива в секундах, не «речь минус тишина». Три гипотезы
уже отвергнуты чтением кода (перекрытие `AudioChunker`, неверный
`source_sample_rate` в `_resample_audio_to_mono_16k`, дубли кадров в
рекордере). Аудио инцидента удалено после успеха — ретроспективно не проверить.

Повтор 2026-08-13: VAD `total=34.82s` vs chunker `duration=39.2s` на той же
записи (`2026-08-13-incremental-preview-design.md`). Меньше разрыв, тот же класс.

### 1.2 Почему сейчас нельзя чинить

`recording_core_service.py` (~3025–3083) пишет WAV в
`<data_dir>/failed_recordings/<uuid>.wav` **до** `transcribe()`, затем
**unlink на успехе**. Контролируемого корпуса нет. Любой патч чанкера без WAV
— гадание.

### 1.3 Решение W2b (инструмент, дефолт выкл)

1. **Настройка** `debug_keep_dictation_wav` (bool, **default `False`**).
   В `DEFAULT_SETTINGS` (`KrabEar/core/config.py`) и, если ключ попадёт в UI
   позже — в `_RANGE_FIELDS` не нужен (это bool). Не включать в проде без
   явной просьбы владельца.
2. При `True`: после успешного STT **не unlink**, а перенести файл в
   `<data_dir>/debug_duration_wav/<iso-or-uuid>.wav` (отдельный каталог, не
   смешивать с сорванными `failed_recordings/`). Сидкар `.json` рядом:
   `vad_total_sec`, `chunker_duration_sec`, `wav_nframes`, `sample_rate`,
   `history_id` если уже известен. Текст транскрипта в сидкар **не писать**.
3. **Purge:** оба пути уже/должны покрываться `handle_purge_all_data`.
   `failed_recordings/` уже чистится (`history_service.py` ~2559). Новый
   `debug_duration_wav/` обязан появиться в том же хендлере, иначе
   `scripts/audit_purge_coverage.py --fail-on-found` упадёт — это и есть гейт.
   Тест-кластер: `KrabEar/tests/test_purge_cluster_w1770.py` (добавить каталог
   в `gone_dirs` / зеркальный кейс `failed_recordings`).
4. Существующие тесты **не ломать**:
   `KrabEar/tests/test_phase_c_audio_presave.py::test_presaved_audio_removed_after_success`
   остаётся зелёным при дефолте `False`. Новый тест: флаг `True` → файл лежит
   в `debug_duration_wav/` после успеха, не в `failed_recordings/`.
5. **Скрипт** `scripts/measure_duration_anomaly.py` (CLI, без IPC к прод-сокету):

```bash
PYTHONPATH=KrabEar python scripts/measure_duration_anomaly.py /path/to.wav
```

Печатает JSON в stdout (три длительности + дельты):

- `wav_duration_sec` — `soundfile.info` / `nframes / samplerate`
- `vad_total_sec` — тот же расчёт, что `engine.py` VAD pre-filter
  (`len(audio)/sample_rate` на загруженном массиве)
- `chunker_duration_sec` — `len(mono_16k) / 16000.0` после того же ресемпла,
  что `_resample_audio_to_mono_16k`
- `delta_chunker_minus_vad`, `delta_chunker_minus_wav`

Не гоняет GigaAM/MLX. Не открывает прод-`data_dir` сам. Не `kickstart`.

Интерпретация для следующей волны (не W2):

- Три числа совпадают на сохранённом WAV → расхождение 41 vs 70 было **до**
  presave (рекордер / метаданные sr) или в логах на **разных** массивах внутри
  одного `transcribe()`. Тогда следующий шаг — логировать `id(audio)` /
  `len` / `sr` в обеих точках, не «чинить AudioChunker».
- Chunker > WAV на том же файле → баг ресемпла/конкатенации, тогда карточка
  фикса с RED-тестом на этом WAV.

### 1.4 Вне скоупа W2b

- Патч `AudioChunker` / GigaAM worker.
- Хранение WAV по умолчанию в проде.
- UI-тумблер (достаточно settings.json / `set_settings`).
- Запись голоса владельца «для датасета» — это не эта волна.

---

## 2. STT ReadTimeout под нагрузкой звонка VG

### 2.1 Факт контракта (два репо)

Voice Gateway, `Krab Voice Gateway/app/stt_engines.py` (~266):

```python
resp = await self._get_client().post(
    f"{self.base_url}/v1/stt/transcribe",
    ...,
    timeout=30.0,
)
```

Krab Ear REST, `KrabEar/backend/rest_server.py`:

- `_TRANSCRIBE_TIMEOUT_SEC = 600` — wall-clock на `Future.result`
- на таймауте: 504, затем `_arm_timeout_exit` → `_exit_poisoned_rest_process`
- `os._exit` **запрещён**, если `REST_IN_PROCESS_ENABLED` (иначе убиваем
  backend вместе с диктовкой в RAM). Это уже в коде (~588–595). **Не
  включать рубильник в W2.**

Следствие: VG рвёт HTTP через 30 с, Ear продолжает транскрибировать до 10 мин
(занятый GPU/лок), следующие звонки встают в очередь. Sentry у шлюза
(ReadTimeout, порядок сотен) — симптом рассинхрона бюджетов, не повод менять
движок.

Диктовка хоткеем идёт через IPC `stop_recording`, не через этот REST-путь.
Глобальные 600 с нужны длинным импортам. Их **нельзя** опустить до 30.

### 2.2 Решение W2c (бюджет на запрос, не новый STT)

Добавить optional form-поле `deadline_sec` в `POST /v1/stt/transcribe`:

- отсутствует / пусто → как сейчас, `_TRANSCRIBE_TIMEOUT_SEC` (600)
- задано → `int/float`, clamp **[5, 120]**, использовать как timeout
  `Future.result` **вместо** 600 для этого запроса
- мусор / ≤0 / >120 → 400 `invalid deadline_sec`, без транскрибации

VG (отдельная карточка в репо шлюза, **не в этом чекауте**): передать
`deadline_sec=25` при `timeout=30.0`, чтобы 504 успел прийти до ReadTimeout
клиента. Пока VG не поменялся, поведение Ear для него не хуже: поле
опционально.

На таймауте:

- отдельный REST-процесс: нынешний 504 + `_arm_timeout_exit` (как сейчас)
- in-process: 504 **без** `os._exit` (уже так) — даже если W2c тестирует с
  рубильником в юните, прод-дефолт остаётся `False`

Очередь: один `ThreadPoolExecutor(max_workers=1)` на запрос уже есть. Не
строить второй пул и не сериализовать диктовку через REST. IPC-диктовка
этого хендлера не касается.

Документировать поле в `docs/IPC_API_REFERENCE.md` только если там уже есть
REST `/v1/stt/transcribe`; иначе — docstring хендлера + этот абзац достаточны.
Не раздувать IPC_API «на всякий случай».

### 2.3 Тесты (имена — в карточку W2c целиком)

Файлы-якоря (не писать новый rest-тест, который импортирует `rest_server` на
коллекции модуля без `setUp` patch.object — класс chunk-pollution):

- расширить `KrabEar/tests/test_rest_upload_security_W1224.py` (там уже
  патчат `_TRANSCRIBE_TIMEOUT_SEC` и 504): кейс `deadline_sec=5` при
  зависшем transcribe → 504 быстрее глобальных 600; кейс `deadline_sec=0` и
  `deadline_sec=999` → 400
- ubuntu-parity: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_rest_upload_security_W1224.py`
- `BackendService` в новых тестах не создавать; если всё же понадобится —
  `close()` в `tearDown`

### 2.4 Вне скоупа W2c

- Флип `REST_IN_PROCESS_ENABLED` / канарейка S3 (это W4, отдельное «да»).
- Снижение `_TRANSCRIBE_TIMEOUT_SEC` / `MLX_TRANSCRIBE_TIMEOUT_SEC`.
- Новый STT-движок, облачный fallback «чтобы звонок не ждал».
- Правки VG из дерева Ear. После мержа Ear — короткая карточка в VG:
  `deadline_sec=25` в `KrabEarSTTEngine`.

---

## 3. Сознательно не в W2

- C2 Live Meeting / C3 Quick Capture (закрыты июль 2026).
- PR #1875 / дообучение `krab_ru` TTS-негативами.
- Sparkle v2.11.0 (W3, только по «релизы»).
- Apple Developer ID, iOS companion, Plugin SDK, EN-UI.
- Второй EventBridge, wake word на SSE.
- Массовый delete `audit/*`.
- Запуск собранного `KrabEarAgent` из воркера.

---

## 4. Порядок и DoD волны целиком

1. W1 закрыта (инвентарь + issues), `NOW.md` указывает на W2.
2. Владелец пишет Approve на код (достаточно сообщения в чате).
3. W2a → W2b → W2c отдельными PR в `codex/krab-ear-v2` с worktree
   `feat/w2a-healthmonitor-docs`, `feat/w2b-duration-measure`,
   `feat/w2c-stt-deadline`.
4. Координатор гейтит дифф, не самоотчёт.
5. Живой e2e диктовки — только W2b, если владелец сам включит
   `debug_keep_dictation_wav` и даст WAV; воркер не включает флаг в проде.

DoD: три карточки смержены; `NOW.md` следующий фронт = W3 релиз **или**
ожидание замера владельца по W2b; продукт без нового сторожа HealthMonitor;
`REST_IN_PROCESS_ENABLED` по-прежнему false.
