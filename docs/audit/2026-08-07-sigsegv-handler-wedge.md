# Разбор: Python-обработчик SIGSEGV подвешивал бэкенд на часы (2026-08-07)

> Улика (полный `sample` живого зависшего процесса) лежит вне репозитория —
> `.remember/forensics/backend-1057-sigsegv-storm-2026-08-07.sample.txt`
> (`.remember/` в `.gitignore`). Этот документ — выжимка, которая должна
> пережить и машину, и сессию: решение «Python-обработчика для SIGSEGV не
> бывает» контринтуитивно, и без доказательства его вернут.

## Симптом

Прод-бэкенд `ai.krab.ear.backend` (pid 1057) **8 часов** не принимал IPC:
`ConnectionRefusedError` на `~/Library/Application Support/KrabEar/krabear.sock`.
При этом:

* `launchctl print` показывал `state = running`, аптайм 8ч — юнит «здоров»;
* процесс жёг 20–56% CPU (то есть НЕ простаивал в ожидании памяти);
* на SIGTERM не реагировал — лечил только `kickstart -k` (то есть SIGKILL);
* в `out.log` за час до этого: `AudioRecorder worker не завершился за 3.0 с`,
  `stop_recording: audio worker завис — отдаю recorder_timeout`,
  `||PaMacCore (AUHAL)|| Error on line 2523: err='-50'`.

Найдено не по алерту, а случайно, при проверке состояния после перезагрузки
машины. Внешний монитор в это время рапортовал «всё хорошо» — отдельный разбор
в брифе `.remember/FOR_KRAB_ear-watcher-blind-2026-08-07.md`.

## Как диагностировали

`sample <pid> 3` — на живом зависшем процессе, БЕЗ root (в отличие от `py-spy`,
который на macOS требует sudo). Читать код было бесполезно: конструкция
выглядит совершенно невинно.

Ключевые строки sample'а:

```
Thread-4 (_worker)                       ← поток захвата аудио
  cdata_call (_cffi_backend)
    ffi_call_int (libffi) → ReadStream (libportaudio)
      PaUtil_ReadRingBuffer (libportaudio)
        1902  _sigtramp  (libsystem_platform.dylib) + 0,4   ← 1902 из 1966 сэмплов
           1  signal_handler (in Python) → trip_signal → _PyEval_SignalReceived
```

```
MAIN (com.apple.main-thread)
  … 46 × [ _Py_HandlePending → handle_signals → _PyErr_CheckSignalsTstate
           → _PyEval_Vector → _PyEval_EvalFrameDefault ] …
  → builtin_isinstance → _abc__abc_instancecheck
    → lock_PyThread_acquire_lock → _PyMutex_LockTimed → _PyParkingLot_Park
```

Что это доказывает:

1. Поток захвата аудио 97% времени сидит в трамплине сигналов на одном и том же
   PC внутри `PaUtil_ReadRingBuffer` — то есть сбойная инструкция повторяется
   бесконечно (отсюда и «worker не завершился за 3.0 с»).
2. Сбой **синхронный**: одна и та же инструкция переисполняется в 1902 сэмплах
   из 1966 внутри одного кадра. Асинхронные сигналы (SIGINT/SIGTERM) так
   выглядеть не могут — их доставка не привязана к конкретной инструкции, и
   никто не шлёт их сотнями в секунду.
3. В стеке того же потока разрешается цепочка **общего C-обработчика CPython**
   `signal_handler` → `trip_signal` → `_PyEval_SignalReceived` (в sample она
   видна в 1 сэмпле из 1966 — остальные обрываются на `_sigtramp`). CPython
   ставит этот обработчик только для сигналов с Python-колбэком. Проверено на
   том же интерпретаторе: по умолчанию SIGSEGV/SIGBUS/SIGFPE/SIGILL/SIGABRT
   остаются `SIG_DFL` (Python-колбэк есть только у SIGINT —
   `default_int_handler`, и у SIGTERM/SIGINT из `service.main()`, но они
   отпадают по п. 2).
4. Значит шторм-сигнал — синхронный И с Python-колбэком, а таких в процессе было
   ровно два: **SIGSEGV и SIGABRT**, оба из `install_signal_handlers()`.
   SIGABRT при этом менее вероятен: после возврата из обработчика `abort()`
   восстанавливает `SIG_DFL` и добивает процесс, а он жил 8 часов.
5. Главный поток — 46 вложенных Python-обработчиков: каждый следующий сбой
   перевзводил колбэк поверх незавершённого предыдущего.

Чем именно кончалось вложение — дедлоком на локе внутри `sentry_sdk.flush()`
или лайвлоком (сбоящий поток взводит флаг быстрее, чем главный дренирует) —
sample НЕ различает: все простаивающие треды процесса стоят на том же
`_PyParkingLot_Park`, поэтому этот кадр сам по себе уликой дедлока не является.
Лечение в обоих случаях одно.

## Корень

`KrabEar/backend/observability.py::install_signal_handlers()` вешал
Python-колбэк на SIGSEGV и SIGABRT, чтобы отправить событие в Sentry перед
смертью.

Это документированная ловушка CPython: Python-обработчик НЕ исполняется в
момент сбоя. C-уровень лишь ставит флаг и **возвращается**, после чего ядро
повторяет сбойную инструкцию. Для синхронного (аварийного) сигнала это
бесконечный цикл. Из документации `signal`: «Python will return from the signal
handler to the C code, which is likely to raise the same signal again, causing
Python to apparently hang».

**Цена бага:** честный крэш, после которого launchd (`KeepAlive=true`) поднял бы
бэкенд за пару секунд, превращался в многочасовой ТИХИЙ простой, невидимый ни
владельцу, ни мониторингу.

## Живое доказательство диагноза и фикса

Изолированный процесс, `ctypes.string_at(0)` (настоящий SIGSEGV из C):

| обработчик | результат |
|---|---|
| Python-колбэк (как было) | жив через 8 с, **100% CPU**, рабочий поток замер — процесс подвешен |
| `faulthandler.enable(all_threads=True)` | умер мгновенно, код **139**, полный трейсбек всех потоков в stderr |

В C-стеке крэша при фиксе виден тот же `_sigtramp` — совпадение с прод-sample'ом.

## Фикс

`faulthandler.enable(all_threads=True)` — то же намерение, но на C-уровне и
signal-safe: печатает трейсбек ВСЕХ потоков в stderr (у launchd это
`logs/krab-ear-backend.err.log`) и передаёт сигнал default-обработчику, процесс
честно умирает → перезапуск + macOS crash report. `all_threads` обязателен:
сбой прилетает в рабочий поток, не в главный.

Наблюдаемость крэшей закрыта отдельно: вердикт
`shutdown_forensics.check_and_collect()` больше не выбрасывается, а становится
`KrabError("system.unclean_restart")` (severity=error → в Sentry немедленно) с
кросс-рестартовым лимитом на диске — без него крэш-луп (launchd
`ThrottleInterval=5` → до ~720 подъёмов в час) положил бы квоту Sentry.

Гейт: `KrabEar/tests/test_fault_signal_handler_2026_08_07.py` — поведенческий
инвариант (ни один аварийный сигнал не получает Python-колбэк) + AST-контракты.

## Что осталось незакрытым

1. **Почему `PaUtil_ReadRingBuffer` вообще сегфолтится** — не разобрано. Фикс
   меняет вечное зависание на честный крэш с автоперезапуском (строго лучше), но
   теперь это станет ВИДИМЫМ crash-loop'ом. С `faulthandler` следующий крэш
   даст полный трейсбек в err.log — ждать первого дампа дешевле, чем гадать.
   Смежные улики указывают на гонку жизненного цикла PortAudio-стрима при
   реинициализации аудио-стека (`backend/audio_reinit.py`).
2. **Swift self-heal не лечит живой-но-заклиненный бэкенд** —
   `HealthMonitor.hangFiredForCurrentEpisode` сбрасывается только успешным
   пингом, а `BackendSupervisor.restartIfDeadDetailed()` на живом процессе
   возвращает `.alreadyAlive` и ничего не делает. Подробности и требования к
   дискриминатору (отказ соединения ≠ таймаут) — в `docs/ROADMAP-2026H2.md`.
