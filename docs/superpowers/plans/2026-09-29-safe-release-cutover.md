# Безопасная подготовка cutover 245aae2d → c3a1779f

## Scope и решение

Карточка: подготовить проверяемую смену Python release, сохранив действующую
конфигурацию и копии для отката. Текущий этап **source/preparation only**.
Исполнение раздела «Переключение» требует отдельного разрешения владельца
на downtime Backend и REST. Это не разрешение на encryption, purge,
миграцию истории, перезапуск Swift, звонки или lifecycle соседей.

Используем существующую схему locked detached release + bootout/bootstrap.
`install_*_launchagent.command` и старый `cutover_to_c3a1779f.command` для
этой выкладки не применять: первый путь пересоздаёт конфигурацию, второй
использует kickstart, который не перечитывает plist.

Инструмент `scripts/prepare_release_cutover.py` **только** готовит/проверяет
копии. Он не вызывает launchctl, не пишет в LaunchAgents и не проверяет
загруженный runtime. В plist меняются только `ProgramArguments[1]` и
`EnvironmentVariables.PYTHONPATH`. Python interpreter, data-dir, socket,
WorkingDirectory, logs, все остальные env/ключи сохраняются. Symlink venv
в target release не нужен: действующий interpreter остаётся прежним.
Оба старых worktree и действующий venv сохранять до завершения отката/приёмки.

## Реализация и проверка

База разработки: `c3a1779fcd7b64dbe3d69231479d911a85d5c321`.
Tooling worktree: `/Users/pablito/.codex/worktrees/ear-safe-cutover/Krab Ear`.
Общий checkout с WIP не менять. Новые dependencies не нужны.

1. RED: `test_prepare_release_cutover.py` — реальный CLI, временные Git/plist;
   успешное staging отсутствует до реализации.
2. GREEN: helper проверяет exact SHA, detached/clean roots, ожидаемые entrypoints
   и PYTHONPATH; создаёт единственный bundle 0700/0600 с byte-exact backups,
   кандидатами и SHA256 manifest. FIFO/symlink отклоняются дескрипторной проверкой.
3. RED → GREEN: добавить `--check-only` существующему safe restart. Используется
   тот же fail-closed IPC parser; успешная проба не вызывает ни launchctl, ни ping.
   Прежние флаги/exit-коды сохраняются. Это snapshot, **не блокировка записи**.
4. Локально, файлы последовательно в dev-venv и `/tmp/py312`:

   ```bash
   PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest \
     KrabEar/tests/test_prepare_release_cutover.py -q
   PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest \
     KrabEar/tests/test_safe_backend_restart_contract.py -q
   PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest \
     KrabEar/tests/test_restart_unknown_activity_2026_09_07.py -q
   PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest \
     KrabEar/tests/test_install_backend_busy_gate_contract_S3.py -q
   bash -n scripts/safe_backend_restart.command
   ruff check scripts/prepare_release_cutover.py KrabEar/tests/test_prepare_release_cutover.py
   git diff --check
   ```

5. Независимое adversarial review helper, gate и всей процедуры. Mock launchctl
   + настоящий частный Unix socket доказывают CLI/gate, но не macOS lifecycle.
   Реальный stage/verify доказывает конфигурацию; production smoke — только после
   отдельного разрешённого cutover. Не запускать `run_release_checklist` /
   `run_smoke_release`: в них есть лишние build/agent lifecycle действия.

## Подготовка bundle — без остановки сервисов

В новой shell задать пути; все последующие блоки используют эти переменные.
Никакого `set -x`, печати plist, env процесса или `.secrets`.

```bash
TOOL_ROOT='/Users/pablito/.codex/worktrees/ear-safe-cutover/Krab Ear'
OLD_ROOT='/Users/pablito/.codex/worktrees/ear-release-245aae2d/Krab Ear'
NEW_ROOT='/Users/pablito/.codex/worktrees/ear-release-c3a1779f/Krab Ear'
OLD_SHA=245aae2d9a1cbe77b6675f8bb87783758e85a826
NEW_SHA=c3a1779fcd7b64dbe3d69231479d911a85d5c321
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
# Родитель уже существует; helper сам атомарно создаёт новый каталог 0700.
BUNDLE="$HOME/.codex/ear-cutover-$(date +%Y%m%dT%H%M%S)"
python3 "$TOOL_ROOT/scripts/prepare_release_cutover.py" prepare \
  --old-root "$OLD_ROOT" --old-sha "$OLD_SHA" \
  --new-root "$NEW_ROOT" --new-sha "$NEW_SHA" \
  --launchagents "$LAUNCH_AGENTS" --output "$BUNDLE"
```

Только exit 0 означает готовый bundle. Коллизия каталога/любая ошибка — STOP;
не удалять чужой bundle. В нём есть секреты действующих plist: не коммитить,
не прикладывать к PR и не печатать. Manifest не является подписью; mode 0700
защищает от других локальных пользователей, но не от процессов самого владельца.

Перед исполнением повторно проверить оба exact-SHA CI target `c3a1779f`, review,
чистоту/locked обоих release, доступность исходного interpreter и свободные
ресурсы. Не запускать ML/build под текущим swap. Сверить текущие launchd jobs
с процессами: `state`, `pid`, `program/arguments`, `PYTHONPATH` и `working directory`
из **loaded** конфигурации (вывод захватить, показывать только эти разрешённые поля).
Оба PID должны соответствовать старому release. Имена папок сами по себе не SHA.
PID 67525/67608 — исторические ориентиры, перед остановкой получить заново.
Отдельно подтвердить для текущего сеанса Swift-агента startup mode
`passive (launchd Variant B)` по его стартовому логу. В этом режиме supervisor
только ждёт backend; active mode может самостоятельно запустить процесс между
bootout и заменой plist. Режим фиксируется при старте агента. Если mode нельзя
подтвердить или он active — HOLD; агент в этой процедуре не перезапускается.

## Переключение — только после разрешения владельца

Оператор выполняет стадии отдельно, проверяя каждый exit code. Не вставлять
всю страницу в shell и не применять безусловный trap с остановкой процессов.
Назначить одного оператора; параллельные release/настройки запрещены на время окна.
Владелец не начинает диктовку/встречу до окончания; REST-клиенты не отправляют
STT/TTS/stream-запросы. Backend idle gate не измеряет REST-запросы. Если это окно
нельзя обеспечить — HOLD, без предположения «health 200 значит idle».

1. Зафиксировать старые PID, SHA, счётчик истории (без текста), saved/runtime
   encryption OFF и отсутствие restore_pending. IPC-ошибка/неполный ответ — STOP.
   Проверить REST health HTTP 200 и отсутствие активных REST-клиентов в согласованном
   окне. Доступность Sentry/свежесть ingress записать отдельно; не выдавать
   отсутствие доступа за отсутствие ошибок.
2. Проверить четыре idle snapshot за не менее 60 с (0/20/40/60), без `--wait`:

   ```bash
   (
     set -e
     for probe in 1 2 3 4; do
       bash "$TOOL_ROOT/scripts/safe_backend_restart.command" --check-only
       [ "$probe" = 4 ] || sleep 20
     done
   )
   ```

   Если subshell завершилась ненулевым кодом — STOP; `set -e` прерывает окно
   при первом отказе. Успешное окно не резервирует
   микрофон; прямо перед остановкой обязательна ещё одна проба.
3. `verify --state before` должен пройти; затем финальная idle-проба:

   ```bash
   python3 "$TOOL_ROOT/scripts/prepare_release_cutover.py" verify \
     --bundle "$BUNDLE" --state before
   bash "$TOOL_ROOT/scripts/safe_backend_restart.command" --check-only
   ```

   Любая ошибка — STOP **до** bootout. Если процесс/PID/config изменился после
   предыдущей сверки — повторить preflight, не продолжать старый план.
4. Остановить backend через `launchctl bootout "gui/$(id -u)/ai.krab.ear.backend"`.
   Это разрешённый reviewed release-путь: обычный safe restart использует kickstart
   и не подходит для смены pin. До следующего шага подтвердить исчезновение job
   **и** старого PID, опрашивая не чаще раза в 2 с, с общим deadline 60 с.
   Не трактовать произвольную ошибку `launchctl print` как отсутствие: нужен ответ
   `Could not find service` для точного label при доступном gui domain. `ps -p PID`
   должен подтвердить отсутствие. Не делать kill/force и не продолжать по таймауту.
   Порядок backend → REST согласован с обратным запуском backend → ping → REST.
   `RestWatchdog` относится к in-process REST и не управляет отдельным REST launchd job;
   считать его защитой этой операции нельзя.
5. Аналогично `launchctl bootout "gui/$(id -u)/ai.krab.ear.rest"`, подтвердить
   исчезновение job/PID и освобождение listener :5005 (60 с). Ошибка — ветка
   восстановления ниже, не bootstrap нового поверх старого.
6. Повторить `verify --state before`. Пока оба юнита остановлены, установить
   кандидаты атомарно для каждого файла. Оператор выбирает `after` для cutover,
   `before` только для согласованного отката. Это две файловые замены, **не** одна
   транзакция; при частичном отказе оба юнита остаются остановленными.

   ```bash
   python3 - "$BUNDLE" "$LAUNCH_AGENTS" after <<'PY'
   import os, pathlib, sys, tempfile
   bundle, target, phase = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
   assert phase in ('before', 'after')
   for role in ('backend', 'rest'):
       raw = (bundle / f'{role}.{phase}.plist').read_bytes()
       fd, temporary = tempfile.mkstemp(prefix='.ear-cutover-', dir=target)
       with os.fdopen(fd, 'wb') as stream:
           stream.write(raw)
           stream.flush()
           os.fsync(stream.fileno())
       os.replace(temporary, target / f'ai.krab.ear.{role}.plist')
       directory = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
       try:
           os.fsync(directory)
       finally:
           os.close(directory)
   PY
   python3 "$TOOL_ROOT/scripts/prepare_release_cutover.py" verify \
     --bundle "$BUNDLE" --state after
   ```

7. Только после verify: `launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS/ai.krab.ear.backend.plist"`.
   Ненулевой bootstrap — не повторять вслепую. Сначала сверить, не появилась ли job,
   и что старые job/PID действительно отсутствуют. Если job существует — перейти
   к её диагностике; если подтверждено отсутствие — допустима одна повторная
   попытка через 5 с. После второго отказа — восстановление.
8. До 120 с ждать валидного IPC ping (`ok:true`, `result.status:ok`, без error)
   и проверить **новый** PID/loaded entrypoint/PYTHONPATH на target root и SHA.
   Отказ соединения, stale socket или неготовность — повтор через 2 с, а не
   немедленный успех/выход. При истечении deadline — восстановление с учётом
   того, что backend уже мог принять новую запись.
9. Только после backend readiness аналогично bootstrap REST. До 120 с ждать
   HTTP 200 `/health` с валидным JSON/status; проверить listener :5005 принадлежит
   новому REST PID и loaded пути ведут к target root. Применять тот же bounded retry.
10. Приёмка: оба новых PID, exact SHA, loaded paths, IPC+REST, history count и
    чтение одной записи **без вывода текста**, encryption OFF, restore_pending=false.
    WorkingDirectory/logs/interpreter должны совпасть с before, а не с target root.
    Наблюдать доступность и отсутствие restart loop; Sentry проверять отдельно.
    Live-диктовка/MLX/encrypted E2E не считаются выполненными этими read-only пробами.

## Восстановление после ошибки

| Достигнутый этап | Действие |
|---|---|
| До bootout | Ничего восстанавливать не надо; прод остался прежним. |
| Backend остановлен, REST ещё старый | Подтвердить, что backend job и PID исчезли. Если plist ещё before, bootstrap старого backend; затем IPC/PID/SHA. REST может кэшировать bridge token: после готовности backend согласованно перезапустить старый REST с polling/readiness. |
| Оба остановлены / plist частично заменены | Не запускать смешанную пару. `verify --bundle "$BUNDLE" --state bundle` проверяет SHA/backups/candidates без требования к current plist. После exit 0 вернуть **оба** before файла атомарной записью из шага 6 (аргумент `before`), verify before, старый backend → ping/PID → старый REST → health/PID. |
| Новый backend уже запущен (даже если REST не готов) | Снова полный idle gate/окно, подтверждение отсутствия REST-нагрузки. Если busy/UNKNOWN — **HOLD**, не убивать процесс автоматическим rollback. Владелец решает recovery. При idle: остановить backend, затем REST, дождаться обеих job/PID; вернуть before и запустить в прежнем порядке. |
| Не удаётся подтвердить исчезновение job/PID, повреждены backup/manifest, old release недоступен | **HOLD / ручное восстановление владельцем**. Не force, не kill, не заменять файлы по предположению. |

После rollback success означает именно оба старых SHA/PID + IPC/REST, сохранённый
профиль и encryption OFF. Ошибка release остаётся ошибкой, даже если откат успешен.
Защищённый bundle и старые release не удалять во время этой работы.

## Review focus

- Candidate не изменяет секреты, interpreter, data-dir, cwd, logs или другие env.
- Отказ stage/verify не трогает действующие plist; stale bundle не допускается.
- Idle gate не допускает malformed/privacy/UNKNOWN и не маскирует частичное окно.
- Shutdown/startup с таймаутом не превращается в ложный success.
- Rollback после начала новой диктовки не автоматический; восстановление двух
  файлов возможно после частичной замены, без проверки current как строго before/after.

## Доказательства подготовки 29.09 (не deployment)

- RED: staging CLI отсутствовал; отдельный RED `--check-only` возвращал usage 2.
  GREEN: 110 targeted tests (15 staging, 7 safe-restart, 79 unknown-activity,
  9 installer contract) в Python 3.14 dev и 110 в Python 3.12.7 parity.
  В parity оба импорта `mlx.core` и `mlx_whisper` недоступны.
  Dev-прогоны показывают известное предупреждение окружения torchcodec/FFmpeg;
  сами проверки не вызывают ML/audio inference.
- Ruff, Flake8, `bash -n`, синтаксис всех bash-блоков runbook и diff-check PASS.
- Независимый adversarial review: source/preparation PASS; замечания про
  in-process watchdog и текущий Swift supervisionMode учтены и повторно приняты.
- Создан bundle `/Users/pablito/.codex/ear-cutover-c3a1779f-20260929`, verify before
  успешен. Это приватный локальный каталог, не Git-артефакт. Перед использованием
  повторить verify; сохранённый успех не отменяет дрейф.
- Живой `--check-only` вернул IDLE. PID Backend 67525 / REST 67608 не изменились.
  Swift 88227 стартовал 28.09 01:14:09; startup marker в `agent.log` 01:14:09.899
  указывает `passive (launchd Variant B)`. После рестарта агента доказательство устаревает.
- Exact target SHA `c3a1779fcd7b64dbe3d69231479d911a85d5c321`:
  [CI](https://github.com/Pavua/Krab-Ear/actions/runs/36601970342) и
  [krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/36601970164) success.
  CI новой ветки инструментов — отдельный gate; не смешивать эти SHA.
- Lifecycle, полный quiet-window, новое Sentry qualification, live-диктовка и
  MLX не выполнялись. Шифрование не включалось; прод-профиль не менялся.
