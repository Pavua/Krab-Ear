# Изоляция public CI и локальных macOS-гейтов Krab Ear

**Дата:** 2026-09-20

**Статус:** дизайн согласован в чате; требуется просмотр этой спецификации перед implementation plan

**База:** `origin/codex/krab-ear-v2` @ `cbf5c27eae1edfad674d0d459eddf78b1849884b`

## Контекст и подтверждённый риск

`Pavua/Krab-Ear` — публичный репозиторий. В нём зарегистрирован repository-level
self-hosted runner `krab-ear-m4max`, работающий на личном Mac владельца под тем
же OS-пользователем, что production Ear и пользовательская диктовка.

Оба PR-workflow (`.github/workflows/ci.yml` и
`.github/workflows/krabear-ci.yml`) маршрутизируют изменения `native/**` в job
с `runs-on: [self-hosted, macOS, ARM64]`. Значит, pull request способен
доставить изменённый код на личный Mac. Текущая GitHub approval policy
`first_time_contributors` уменьшает частоту автоматического запуска, но не
создаёт sandbox и не является достаточной границей доверия.

GitHub рекомендует не использовать self-hosted runners для публичных
репозиториев: fork PR потенциально может исполнить опасный код на runner-host.
Поэтому изменение одного `if:` или ручной просмотр workflow считается только
временным containment, но не закрытием риска.

## Цели

1. Ни один workflow публичного Krab Ear не исполняет pull-request code на
   личном Mac.
2. Public PR и обычный push сохраняют Swift build и wiring/source-contract
   проверки на disposable GitHub-hosted macOS.
3. MLX/Metal-проверки, которым нужен реальный M4 Max, сохраняются в отдельном
   private control-plane и всегда привязаны к проверенному exact SHA.
4. CI не выдаёт зелёный статус за пропущенный heavy/device gate.
5. Cutover не рестартует production Ear, не запускает собранный агент и не
   ухудшает живую диктовку.
6. Архитектура оставляет место для будущего общего host admission, но не
   смешивает его с первым security cutover.

## Не входит в первую волну

- общий admission-controller для Krab, Ear и Voice Gateway;
- перенос private Krab/Gateway CI в hosted;
- новый GitHub App или cross-repository write token;
- изменение production-флагов, LaunchAgent backend/agent или live runtime;
- выполнение произвольного SHA из внешнего pull request на личном Mac;
- оптимизация или сокращение самого MLX test pack.

## Выбранная архитектура

### 1. Public plane: только disposable runners

В `Pavua/Krab-Ear`:

- Swift jobs в `.github/workflows/ci.yml` и
  `.github/workflows/krabear-ci.yml` переходят на стандартный
  `macos-latest`;
- набор команд остаётся прежним: release build, затем build-tests и узкие
  wiring/source-contract tests;
- backend и статические job продолжают работать на `ubuntu-latest`;
- публичный `.github/workflows/mlx-nightly.yml` удаляется после появления
  эквивалентного private workflow, чтобы после снятия runner не оставались
  вечные queued jobs;
- ни один public workflow с trigger `pull_request` не содержит self-hosted
  job, даже с условием, которое якобы исключает PR.

Последний инвариант намеренно строже минимально возможного. Проверять сложную
логику `if:` как security boundary хрупко; отдельный private workflow проще
аудировать и труднее случайно открыть внешнему PR.

### 2. Fail-closed source contract

В публичном репозитории появляется узкий аудит
`scripts/audit_public_ci_runner_isolation.py` и тест его поведения.

Аудит:

- перечисляет все `.github/workflows/*.yml` и `*.yaml`;
- определяет наличие trigger `pull_request` без выполнения expressions;
- рекурсивно проверяет `jobs.*.runs-on`;
- падает, если PR-workflow содержит `self-hosted` в строке, списке или
  expression;
- падает на неразбираемом workflow вместо разрешающего fallback;
- не считает `pull_request_target` безопасной заменой и отдельно запрещает
  его появление;
- выводит только путь workflow/job и причину, без environment или secret data.

RED-доказательство: новый тест должен сначала упасть на двух текущих Swift
job. GREEN: после перевода на `macos-latest` тот же тест проходит. Аудит
подключается в оба основных hosted workflow, чтобы последующий PR не мог
незаметно вернуть public self-hosted path.

### 3. Private control-plane

Создаётся приватный репозиторий `Pavua/Krab-CI-Control`. На первой волне он
владеет только Ear device gate; расширение на другие проекты потребует
отдельного дизайна.

Минимальные компоненты:

- `.github/workflows/krab-ear-mlx.yml` — schedule в прежнем окне и ручной
  owner dispatch;
- `scripts/resolve_trusted_ear_sha.sh` — fail-closed разрешение exact SHA;
- `scripts/run_krab_ear_mlx_gate.sh` — чистый checkout и запуск перенесённого
  без сокращений MLX pack;
- contract tests для SHA validation, checkout destination и запрета fork refs;
- README/runbook с cutover, evidence и rollback.

Private workflow имеет минимальные permissions (`contents: read`) и не хранит
cross-repository write token. Результат первой версии доказывается URL private
Actions run + exact SHA в summary. Публикация commit status обратно в public
Ear откладывается до отдельного GitHub App/token design, чтобы не вводить
долгоживущий PAT ради косметики.

### 4. Модель доверия exact SHA

Scheduled run:

1. Получает текущий SHA `refs/heads/codex/krab-ear-v2` через hard-coded remote
   `https://github.com/Pavua/Krab-Ear.git`.
2. Требует полный lowercase SHA-1 формата `[0-9a-f]{40}`.
3. Fetch выполняется без интерполяции пользовательского ref в shell-команду.
4. Проверяется, что SHA достижим из доверенной default-ветки.
5. Создаётся новый detached checkout в каталоге конкретного run/attempt.

Manual dispatch принимает только полный SHA и применяет те же проверки. В
первой версии нельзя запустить unmerged fork/PR SHA. Проверка внешнего PR до
merge остаётся на hosted runners; M4 Max gate является post-merge или
pre-release доказательством доверенного дерева.

### 5. Admission и честная семантика результата

Private device workflow использует одну concurrency group
`krab-ear-device-gate` с `cancel-in-progress: false`.

До MLX запускается read-only preflight:

- нет активной записи/встречи Ear;
- нет другого известного heavy CI/ML workload;
- memory pressure и доступное место не находятся за согласованным стоп-порогом.

Если admission не получен, job завершается отдельной ошибкой
`admission_denied`; это не считается успешным тестом. Ожидание допускается
только ограниченное, а слот не освобождается, пока жив дочерний test process.
Точные источники сигналов и пороги фиксируются в implementation plan после
чтения существующих safe-runner/runtime probes.

## Runner cutover

Cutover выполняется отдельным тихим окном после зелёного public PR:

1. Создать private controller и проверить workflow/static contracts без
   запуска heavy MLX.
2. Смержить public PR; подтвердить hosted CI exact merge SHA.
3. Убедиться, что Ear runner idle и нет активной пользовательской записи.
4. Остановить только service `actions.runner.Pavua-Krab-Ear...`, не Ear
   backend/agent и не соседние runners.
5. Удалить регистрацию runner из public `Pavua/Krab-Ear`.
6. Зарегистрировать отдельный runner instance/directory только в private
   `Pavua/Krab-CI-Control`; токен не печатать и не хранить в Git.
7. Через API подтвердить `total_count == 0` для public Ear и online private
   runner с ожидаемыми labels.
8. Запустить private gate на exact default-branch SHA и получить terminal
   evidence.

Нельзя держать public и private listener одновременно активными как способ
«мягкого перехода»: это оставляет старую поверхность атаки и допускает два
heavy job на одном Mac.

## Rollback

Security-инвариант не откатывается: public self-hosted runner не
регистрируется обратно.

Если private controller не стартует:

- оставить public runner удалённым;
- выполнить owner-controlled локальный exact-SHA gate по тому же скрипту;
- исправить private registration/workflow и повторить read-back;
- hosted Swift CI продолжает работать независимо.

Если `macos-latest` обнаружит несовместимость образа, исправляется hosted
toolchain pin или build contract. Возврат PR-кода на личный Mac не является
допустимым rollback.

## Проверки и критерии приёмки

### Source

- audit test наблюдал RED на исходных workflows;
- audit test и YAML parsing зелёные после изменения;
- `git diff --check`;
- публичный synthetic PR с `native/**` получает только GitHub-hosted jobs;
- push default SHA сохраняет hosted Swift build/tests;
- public workflow list не содержит self-hosted MLX job.

### GitHub boundary

- API public Ear: `total_count == 0` self-hosted runners;
- private controller: ровно один ожидаемый Ear runner online;
- public Actions permissions остаются read-only по умолчанию;
- нет `pull_request_target` с checkout/исполнением PR content.

### Private device gate

- summary содержит requested/resolved/checked-out SHA и attempt отдельно;
- checkout detached и чистый;
- SHA ancestry check fail-closed;
- один terminal MLX run проходит на доверенном SHA;
- admission failure отображается как failure/blocked evidence, не success;
- cancellation не оставляет test subprocess после завершения runner job.

### Runtime boundary

- production Ear PID/health не меняются из-за source/cutover;
- Krab и Voice Gateway runners/processes не останавливаются;
- во время первого heavy acceptance нет активной диктовки;
- memory/IO и отзывчивость снимаются до/во время/после как отдельное evidence.

## Rollout по PR

1. **Ear public isolation PR:** hosted Swift, fail-closed audit, удаление
   public MLX workflow, документация границы.
2. **Private controller bootstrap:** workflow, scripts, tests и runbook без
   public write credential.
3. **Operational cutover:** runner unregister/register + exact-SHA read-back
   и один контролируемый acceptance run.

PR 1 и PR 2 могут готовиться независимо, но operational cutover начинается
только после их зелёного состояния. Merge/deploy права одного репозитория не
переносятся автоматически на другой; каждый remote mutation фиксируется
отдельно.

## Последующая фаза

После стабильного Ear gate можно отдельно спроектировать общий cross-repo
admission-controller: один heavy + один доказанно isolated light, корректная
cancellation/reaping семантика и единая телеметрия очереди. Эта фаза имеет
больший blast radius и не является условием закрытия public Ear P0.
