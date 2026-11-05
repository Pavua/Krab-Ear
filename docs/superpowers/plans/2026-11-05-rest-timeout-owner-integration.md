# REST timeout: проект изолированной проверки wiring

Статус: исходный план локального runner ниже остаётся проектом и не запускался.
Для отдельного одноразового GitHub-hosted CI подготовлена синтетическая проба
`offline_tests/rest_timeout_owner/ci_probe.py`; её фактический результат нужно
проверять в CI точного SHA. Она вызывает `BackendService` и канонический
`backend.rest_server` с подменёнными аудио, сервером и watchdog, проверяет
response-close и ошибку adoption. В тестовом процессе отключён rate limiter;
его взаимодействие с этим путём, реальный timeout-handler, повторный start и
watchdog эта проба не проверяет. Она не подтверждает живой runtime или drain.

Ниже описан исходный более широкий план; его требования к локальной изоляции
и дополнительные сценарии нельзя считать выполненными наличием CI-пробы.

## Проверяемые сценарии

1. Fresh isolated Python process: статическая настройка false, runtime-настройка
   true на временном `StateStore`; `BackendService` запускается с fake recorder,
   transcriber и translator. Подменить сервер и watchdog до инициализации: никаких
   слушающих сокетов, живых моделей, внешних вызовов или неконтролируемых нитей.
   Проверить, что вызвано настоящее подключение к каноническому экземпляру
   `backend.rest_server`, его метка установлена до создания fake сервера.
2. Создать настоящий Flask response с `_arm_timeout_exit`, удержать его до stop
   fake сервера и изменения настройки, затем закрыть. Перед любым сценарием
   заменить в отдельном процессе `os._exit` и `kill_mlx_whisper_session` на
   проверяемые запреты; их вызов — failure, а не завершение чужого процесса.
3. В другом fresh process воспроизвести ошибку adoption/create после mark и
   повторный start/watchdog через fake контроллер; ни один поздний callback не
   должен попасть в эти запретные операции. Если сценарий не воспроизводит
   настоящий lifecycle, явно указать ограничение.
4. Fresh standalone process с false: callback должен дойти до подменённых kill,
   flush и exit в ожидаемом порядке; с true — не должен. Никакого настоящего
   `os._exit` или kill, marker не сбрасывать в shared interpreter.

Это wiring test с synthetic timeout/response-close: он не воспроизводит реальный
зависший MLX worker и не доказывает его остановку, очистку upload или drain.
Настоящий 504 handler с управляемым fake Future — отдельный необходимый уровень,
если response-close test не докажет путь от handler до callback.

## Граница до исполнения

- Зафиксировать полные hash всех импортируемых project modules, test harness,
  Python executable и profile; команды без сети, model/hardware/providers и
  без настоящих server/process lifecycle. Один локальный лёгкий процесс за раз.
- `Path.home` и данные redirect только через проверенные тестовые stubs к новой
  временной директории; не назначать `HOME` или `CODEX_HOME`. Окружение очистить
  от токенов и пользователя; отключить bytecode и thread pools. До импорта
  проекта установить проверенные deny-guards на filesystem/config, network,
  процессы, сигналы и любые нежелательные thread starts. Условие неизвестно →
  тест не запускать. Audit hook не является полноценной OS-изоляцией.
- Перечислить все пути библиотек, system reads и разрешённые temporary writes;
  заранее доказать отрицательными canary, что protected пути, network, spawn,
  реальный exit/kill и неучтённые потоки закрыты. Не печатать содержимое путей.
- Все стартовавшие threads должны быть собственными, учтёнными и join-confirmed;
  запрещённые scheduler/worker старты должны быть явными проверяемыми stub, а не
  подавленными и затем принятыми за настоящее завершение. Отсутствующий receipt,
  timeout, исключение в cleanup или неопознанный процесс → UNKNOWN/HOLD.
- Parent должен сохранять identity процесса до cleanup и не посылать сигналы
  переиспользованному PID/PGID. SIGTERM/SIGKILL parent, race создания и descendant
  после выхода leader требуют отдельного proof; проблемный history lab runner
  не использовать повторно. Очищать только собственный tmp после доказанного
  завершения; исключение не превращать в success.
- Freeze исполнимых файлов и политики требует независимого source review
  Main и Gateway до фактического запуска, затем свежих START/FINISH. Текущий
  профиль и разрешение на ресурсы не являются разрешением на произвольный runner.

Остальные gates: поведение существующего standalone unit test после включения
sticky marker в одном pytest chunk, Python 3.12 parity, целевые sibling tests,
CI на точном SHA, независимый whole-diff review, последующие release и live
proof. Ни один из этих шагов пока не пройден данным профилем.
