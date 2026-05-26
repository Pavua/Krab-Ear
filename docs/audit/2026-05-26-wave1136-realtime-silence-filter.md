# Аудит RealtimeSilenceFilter (W1136)

**Дата:** 2026-05-26  
**Ветка:** `audit/realtime-silence-filter-W1136`  
**Файл:** `KrabEar/backend/realtime_silence_filter.py`  
**Статус:** 5 findings (2 HIGH, 2 MEDIUM, 1 LOW)

---

## Контекст

`RealtimeSilenceFilter` запускает фоновый поток во время записи, периодически
анализирует rolling-окно аудиобуфера через `SilenceDetector` и накапливает
`silence_ranges` — диапазоны (start\_sec, end\_sec) для последующего
обнуления семплов в `engine.py::transcribe()` перед подачей в Whisper.

Компонент wired в W878, W874 bug fixed. Отключён по умолчанию
(`realtime_silence_filter_enabled: False`).

---

## F1 — HIGH: `_checked_up_to_sec` — мёртвое состояние, отсутствует incremental tracking

**Файл:** `KrabEar/backend/realtime_silence_filter.py:56,71`

`_checked_up_to_sec` инициализируется (`= 0.0`) и сбрасывается при старте, но
**никогда не читается** ни в `_check_once`, ни где-либо ещё. Это означает, что
каждый вызов `_check_once` повторно анализирует тот же rolling-window
(`rt_silence_window_sec`, дефолт 10 с), не зная, какая часть окна уже
проверялась.

**Следствие:** при длинных записях один и тот же тихий участок попадёт в
`new_ranges` на каждой итерации. Merge через `_merge_ranges` поглощает
дубликаты, поэтому итоговый список не раздуется — но CPU тратится на
повторную детекцию уже обнаруженных диапазонов на каждом `check_sec`
интервале (дефолт 5 с).

**Рекомендация:** обновлять `_checked_up_to_sec = total_duration` после каждой
успешной проверки и пропускать анализ участков с `abs_end <= _checked_up_to_sec`.
Или удалить поле, задокументировав, что merge устраняет дубли (менее оптимально,
но честнее).

---

## F2 — HIGH: `_threshold_db` не читается из settings — порог не настраиваем

**Файл:** `KrabEar/backend/realtime_silence_filter.py:47`

```python
self._threshold_db: float = _DEFAULT_THRESHOLD_DB  # всегда -40.0
```

Все прочие параметры (`check_sec`, `window_sec`, `max_silence_sec`) читаются
из `settings`-словаря. Только `_threshold_db` жёстко задан модульной
константой. Это нарушает ожидаемый паттерн настраиваемости через IPC
`set_settings` и расходится с W912, который устанавливает `-40 dB` как
**системный дефолт** — то есть пользователь должен иметь возможность его
переопределить.

Кроме того, в `engine.py:827` вызов `zero_silence_ranges` тоже хардкодирует
`sample_rate=16000`, игнорируя фактический `sample_rate` буфера (хотя для
live recording дефолт рекордера = 16000, поэтому на практике это безопасно).

**Рекомендация:**

```python
self._threshold_db: float = float(
    settings.get("rt_silence_threshold_db", _DEFAULT_THRESHOLD_DB)
)
```

Добавить `RT_SILENCE_THRESHOLD_DB: float = -40.0` в `core/config.py` и в
`DEFAULT_SETTINGS`.

---

## F3 — MEDIUM: RealtimeSilenceFilter не wired в `recording_core_service.py` — фича мертва на практике

**Файлы:** `KrabEar/backend/recording_core_service.py`, `KrabEar/backend/service.py`

`RecordingCoreService.__init__` не принимает и не создаёт экземпляр
`RealtimeSilenceFilter`. Метод `handle_stop_recording` вызывает
`self.transcriber.transcribe(...)` **без** аргумента `silence_ranges` (строка
947 в `recording_core_service.py`). Таким образом, даже при включении флага
`realtime_silence_filter_enabled=True` через `set_settings`, никакие диапазоны
тишины не будут переданы в движок: параметр `silence_ranges` в
`engine.transcribe()` всегда будет `None`.

**W878 wiring** обещал это исправить, но `recording_core_service.py` не
содержит ни одного упоминания `RealtimeSilenceFilter` или `silence_ranges`.
Фича реализована в engine-уровне (параметр присутствует, логика обнуления
работает), но вызывающая сторона её не использует.

**Рекомендация:** добавить `RealtimeSilenceFilter` в конструктор
`RecordingCoreService`, запускать при `handle_start_recording`, останавливать
при `_stop_recording_phase_c` и передавать `silence_ranges` в вызов
`self.transcriber.transcribe(...)`.

---

## F4 — MEDIUM: SmartSilenceSkipper (W1102) не интегрирован в production pipeline — расхождение с engine

**Файл:** `KrabEar/core/smart_silence_skipper.py`

`SmartSilenceSkipper` реализован и имеет тесты, но не импортируется ни в
`engine.py`, ни в `recording_core_service.py`, ни в `service.py`. В
`DEFAULT_SETTINGS` есть `"smart_silence_skip_enabled": False`, но нет кода,
который читает этот флаг и вызывает `SmartSilenceSkipper.process()`.

Оба компонента (`RealtimeSilenceFilter` и `SmartSilenceSkipper`) решают
смежную задачу разными методами:
- `RealtimeSilenceFilter` — **online**, фоновый поток, обнуляет семплы с
  сохранением временных меток Whisper.
- `SmartSilenceSkipper` — **offline** (post-recording), физически удаляет
  паузы из буфера, уменьшает длину аудио.

Логика их совместного применения не задокументирована. При одновременном
включении обоих (оба off by default) они могут конфликтовать: RSF обнуляет
диапазоны → SSS потом удаляет нулевые участки → временные метки Whisper
сдвигаются. Необходима документация порядка применения и/или взаимоисключение.

**Рекомендация:** либо wired SSS в `engine.py` (после VAD pre-filter,
перед STT), либо пометить как `# TODO: not yet wired`. Добавить комментарий
о взаимодействии с RSF.

---

## F5 — LOW: Тест на suppression false positives отсутствует для mixed-signal (speech + silence)

**Файл:** `KrabEar/tests/test_realtime_silence.py`

Тесты покрывают: disabled, lifecycle, pure silence, pure speech, event
emission, edge cases, merge logic, `zero_silence_ranges`. Отсутствует тест для
смешанного сигнала типа `[speech(5s), silence(10s), speech(3s)]` — проверка
того, что RSF не помечает речевые сегменты как тишину и не создаёт диапазоны,
перекрывающие речь.

**Конкретная риск-ситуация:** если `rt_silence_max_sec` выставлен низко (< 2 с)
а `rt_silence_window_sec` мало, возможны ложные срабатывания на коротких
паузах между словами. Тест `test_adaptive_threshold` с `max_sec=1.0` проверяет
только pure silence — не mixed.

**Рекомендация:** добавить `test_no_false_positive_speech_in_mixed_signal`:
построить буфер `[speech(3s) + silence(10s) + speech(3s)]`, запустить RSF с
`max_silence_sec=8.0`, убедиться что ни один диапазон не перекрывает речевые
участки.

---

## Краткая таблица

| #  | Severity | Описание |
|----|----------|----------|
| F1 | HIGH     | `_checked_up_to_sec` — мёртвое поле, повторный анализ окна на каждой итерации |
| F2 | HIGH     | `_threshold_db` жёстко -40.0 dB, не читается из settings, нет IPC-настройки |
| F3 | MEDIUM   | RSF не wired в `RecordingCoreService` — `silence_ranges` никогда не передаётся в engine |
| F4 | MEDIUM   | `SmartSilenceSkipper` не wired в production, взаимодействие с RSF не документировано |
| F5 | LOW      | Нет теста на false positives для mixed speech+silence буфера |

---

## Взаимодействие с другими волнами

- **W912** (-40 dB default): RSF использует тот же дефолт — согласованность на уровне значения, но нарушена настраиваемость (F2).
- **W1018** (whisper preserve): RSF обнуляет (не удаляет) семплы — таймстемпы Whisper сохраняются. Подход корректен.
- **W1102** (SmartSilenceSkipper): оба компонента dormant. Порядок применения не определён (F4).
- **W878** (wiring): заявлено как completed, но `recording_core_service.py` не содержит RSF (F3).

---

## RAM / CPU

На long sessions (>30 мин):

- `_silence_ranges` накапливает merged диапазоны — RAM рост O(n\_silence\_regions), типично
  единицы / десятки записей, пренебрежимо мало.
- CPU: каждые `check_sec` (дефолт 5 с) делается `snapshot_audio(10 с)` + numpy
  RMS по 10-секундному окну. На 16 kHz float32 = 160k семплов × 2 (int/float
  конвертация) ≈ 1.3 MB per check. Приемлемо, но F1 удвоит работу при обнаружении
  тишины (те же 10 с повторно проверяются).

---

*Аудит W1136. Автор: Claude Sonnet 4.6 sub-agent.*
