# Whisper worker: cleanup и watchdog, 2026-10-14

**Goal:** ошибка записи временного PCM WAV не оставляет аудио на диске;
запоздалый watchdog завершённого запроса не влияет на следующий запрос.
База — merged `cdfcf8ee57aa75f13ee9a2e2ae4c0ae465af6958` (#2090),
owned ветка `codex/ear-whisper-lifecycle-20261014`.

## Подтверждённые дефекты и границы

Независимый read-only аудит Astra Ultra нашёл два MED в
`core/mlx_whisper_session.py`, существовавшие также в release `9d7a0aac`:

- `_write_pcm_wav` вызывается до cleanup `try/finally`: частичная запись
  с `ENOSPC` оставляет пользовательские байты во временном файле.
- `Timer.cancel()` не останавливает уже начавшийся callback. Старый callback
  читает общие `_proc`/`_timed_out` и может убить тот же subprocess, уже
  обслуживающий следующий сериализованный запрос. Захват одного `proc`
  недостаточен: процесс переиспользуется.

Причина наблюдавшегося REST MLX timeout → failfast70 остаётся UNKNOWN.
Отдельный RU `voice_gateway_call` HTTP504 идёт через GigaAM и этой карточкой
не объясняется. Условный singleton/close долг без доказанной production
достижимости не включён; фазовую телеметрию не добавляем.

## Реализация и проверки

1. Proper RED на настоящей production-функции: synthetic partial write
   выбрасывает `OSError(ENOSPC)`, исходная ошибка сохраняется, файл удаляется.
   Positive write и downstream error используют тот же cleanup.
2. Минимально расширить существующий `try/finally`, без нового cleanup API.
3. Deterministic RED старого callback A во время запроса B, включая reuse
   одного процесса. Исправить ownership watchdog на запрос; сохранить
   настоящий current-request timeout и классификацию ошибки.
4. Проверить parent session и pipe protocol с настоящим временным Python
   subprocess без ML. Fake transport не является живой Whisper-приёмкой.
5. Narrow regression и связанные существующие файлы последовательно на
   Python 3.14 и имеющемся Python 3.12; temp HOME/data, CPU import fence,
   никакого production Keychain/истории/сети. Lint, diff-check, audit-all.
6. Independent Astra Ultra whole-diff gate, новый exact-head hosted CI;
   root интегрирует и проверяет post-merge CI итогового SHA.

Исполнитель владеет production-модулем и двумя новыми test-файлами; root —
документами, Git/PR/merge и deployment. Параллельный shared checkout не менять.
Результаты RED/GREEN сохраняются в ignored
`.superpowers/sdd/2026-10-14-whisper-lifecycle/`.

## Release

Merge/deploy/E2E разрешены владельцем в текущем чате. Операционные gates
сохраняются: exact final CI, согласованное тихое окно соседей, strict idle,
полный drain старых процессов и единый откат app + обоих plist.
Переключение runtime объединяется с этой волной; подготовленный #2090
bundle ещё не применён. Реальное encryption activation/purge/rotation и
OrbStack/VPN не входят в scope.

Sol 6.1 High — реализация/CI; Astra Ultra — независимый review этих двух
исправлений и consequential release decision.
