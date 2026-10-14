# Whisper lifecycle: локальный RED→GREEN checkpoint

База `cdfcf8ee57aa75f13ee9a2e2ae4c0ae465af6958`, owned ветка
`codex/ear-whisper-lifecycle-20261014`.
[Карточка и границы](../plans/2026-10-14-whisper-lifecycle.md).

## Результат

В `core/mlx_whisper_session.py` подготовка временного WAV находится внутри
прежнего `try/finally`. Файл удаляется и при частичной записи с `ENOSPC`,
и при ошибке worker; исходное исключение сохраняется. Caller-owned path
не удаляется.

Watchdog использует per-request closure: `active`, `timed_out`, captured
process и отдельный короткий guard. Callback не берёт session lock; retire
происходит до его освобождения. После retire запоздалый callback — noop,
включая повторное использование одного subprocess следующим запросом.
Guard не охватывает pipe I/O, wait или logging; новый join не добавлен.
Настоящий timeout текущего запроса по-прежнему завершает его процесс.

## Доказательства на временных данных

- Cleanup proper RED на обеих версиях: **1 failure из 4** — partial WAV
  остаётся после исходного `ENOSPC`; GREEN **4/4**.
- Watchdog proper RED: **3 failure из 5** — поздний cancelled callback,
  уже вошедший callback A во время B, ложный `MLXTimeoutError` B с настоящим
  subprocess. GREEN **5/5**, включая оба current-timeout controls.
- В двух integration tests настоящий parent session общается по stdin/stdout
  JSON с временным stdlib echo-child. Тот же child переживает старый callback
  и отвечает B; current timeout убивает только собственный child. Все child
  процессы дождались завершения, pipes закрыты, stderr thread присоединён.
- Финально восемь связанных test-файлов: **81 PASS + 2 subtests на Python
  3.14**, столько же на имеющемся Python 3.12. Это selected-file evidence.
- Flake8 `E9,F63,F7,F82`, diff-check и full patch whitespace check PASS.
  `make audit-all`: **14 gates PASS**. Последняя test-only правка изолирует
  fake Timer в namespace тестируемого модуля; production после audit не менялся.

Harness использовал throwaway HOME/data/TMP, CPU/native import fence,
запрет production/private files, live IPC/network и subprocess, кроме exact
interpreter + собственного временного echo-script. Два pytest config warning
`timeout`/`timeout_method` вызваны отключённым plugin autoload; это не failure.
Commands/raw results и hashes — ignored
`.superpowers/sdd/2026-10-14-whisper-lifecycle/implementation-receipt.json`.
Frozen implementation patch SHA256:
`5c28f5bc6a75cec0495e0dba19c4016bc5b24569d834cd0b037602ebfb07f301`.

## Integration и runtime

Independent whole-diff Astra Ultra и новый exact-head hosted CI — обязательные
следующие gates; прежний #2090 CI их не заменяет. После merge нужны оба
post-merge workflow итогового SHA. Исполнитель не делал commit/push/runtime;
root выполняет интеграцию по явному owner scope.

На этом локальном checkpoint Ear остаётся на retained release `9d7a0aac`;
#2090 source merged, его bundle подготовлен, но не применён. Root объединяет
выкладку с этой карточкой после fresh idle/quiet window и process drain.
Конфигурация реального шифрования OFF; activation/purge/rotation не выполнялись.

Живой MLX timeout → failfast70 остаётся с UNKNOWN причиной. Сам по себе worker
timeout не доказывает начало inference; RU call-profile HTTP504 использует
отдельный GigaAM path. Ни один из этих инцидентов не объявляется исправленным
по synthetic echo-child тестам. Sentry AppHanging AGENT-19 также остаётся OPEN.
