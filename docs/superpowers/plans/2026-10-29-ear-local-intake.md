# Ear — локальные планы и recovery/self-heal: source intake

Scope: только source/docs после диагностического commit173e86c9; без новых
тестов, публикации, runtime, моделей, клона и OrbStack. Main — serial scheduler.
Эта карта не запускает описанные recovery-механизмы.

## Полнота инвентаризации

В docs/superpowers/plans найдено109 Markdown-файлов. Девять датированы Oct26–29:
это текущая выборка intake, не полный прочитанный исторический backlog.
Frozen100 historical files теперь имеют hash/metadata/source-reference registry:
scan100/100, metadata review100/100, полное чтение17/100; behavior verification0.
Остальные83 не прочитаны полностью; active/done/superseded не присваивается
автоматически по checklist или существованию пути.
NOW содержит исторические checkpoints рядом с текущими — unchecked box старой
карточки сам по себе не означает новый незавершённый implementation task.

| Recent card | Source/checkpoint evidence | Remaining boundary |
| --- | --- | --- |
| constructor-text-stores-characterization Oct26 | test-only prerequisite следующей extraction | stale unchecked checklist, не новая runtime задача |
| constructor-text-stores-extraction Oct26 | NOW PR2125 merged/source CI | runtime acceptance отдельно |
| live-subs-cancellation Oct26 | NOW PR2126 merged/source CI | worker/IPC/native live drain отдельно |
| batch-stt-drain Oct26 | NOW PR2127 merged/source CI | реальный accepted batch drain отдельно |
| constructor-translation-extraction Oct29 | localc0dcf9f6, source/offline pack | hosted CI/full integration/runtime отсутствуют |
| translation-components-integration Oct29 | localb34e238b, malformed cache fix и guarded components | source/offline ≠ production wiring |
| ear-aggregate-offline Oct29 | local91a9b714, separate70/5/9 cases+2cleanup | не повторять неизменённые packs |
| ear-full-lifecycle-feasibility Oct29 | local9d9be174, actual constructor+close contract | реальные scheduler loops/main/IPC/audio не квалифицированы |
| ear-diagnostics-audit Oct29 | local173e86c9, missing-log causalRED→GREEN2 | CLI local only, не deployed; OPEN issues source triage не current test acceptance |

Merge/CI entries здесь прочитаны из NOW как исторические checkpoints; нового
GitHub CI lookup в этой карточке нет. Local SHAs проверяются текущей Git history.
Релизных выводов из таблицы делать нельзя.

## Recovery и self-heal — разные контуры

| Контур | Source mechanism / ownership | Что проверено сейчас |
| --- | --- | --- |
| AudioSelfHealer | recording_core_service phaseB/phaseD empty→record_empty_result; successful text→record_success; service late injection | source map, не real microphone/reinit |
| Reinit policy | threshold counters, eager attempt flag under lock; BUSY/DEFERRED_RECORDING откатывают попытку; следующий streak эскалирует ErrorBus | actual source прочитан; concurrent/live контракты новыми тестами не проверены |
| Audio safety | BackendService._reinit_is_recording_gate сочетает recording и физически живой recorder worker; coordinator владеет reinit | не вызывать coordinator/PortAudio для intake |
| Bootstrap log | AgentRecoveryLogger пишет launch/IPC/health stage и FATAL; наблюдательный журнал | tail classification:36historical ipc_connect_fail, не новые Oct29 crashes |
| Shutdown forensics | dirty marker→startup-recovery→best-effort log show/launchctl print, rate limit | source only; collector не импортирован/не выполнен, private forensics не открывались |
| ensure_agent_running.command | самостоятельный detector/launcher, собственный .remember log | script прочитан как data; не исполнялся (может open app) |
| Telegram self-improvement/reply loop | в прочитанных Ear operational sources самостоятельный cursor/queue не найден | это не доказательство отсутствия; Main/coordinator владеют общим reply intake |

Не смешивать отсутствие ERROR в backend.log, historical bootstrap FATAL и
самоулучшение ответов Краба. Sentry central intake — координатор/task3;
дубликатов запросов, auth/DSN scan и Sentry mutations нет.

## Registry / next safe scope

1. Diagnostic P2 localfixed173e86c9, causalRED1fail/cleanPASS→GREEN2,
   source review Main; publication/CI/deployment не выполнялись.
2. Known lifecycle export fix9d9be174 не отменяет caller hard-exit-on-False;
   listener/replay detach и MemoryConductor nested-worker verdict остаются
   отдельными causal source gates, не новыми найденными runtime инцидентами.
3. Toast1965: новый deterministic seam требует отдельной карточки/scope и
   Main START для Swift; нельзя заменять root-cause анализ увеличенным sleep.
4. Remaining100historicalplans: следующий intake — только индекс/статусы/
   supersession, без автоматического исполнения старых карточек/моделей.
5. Sentry central peerreceipt обновлён: unresolved17backend/0agent полностью
   paginated; latest17/17 metadata прочитаны координатором. Environment daily-use,
   releasekrab-ear@2.4.0 без gitSHA; latestOct27, MLX chainOct24. Это peer metadata,
   не точная runtime revision/causality и не17 независимых новых багов.
6. OPEN PR2120/2108/2119 не интегрированы; текущая карта конфликтов и первый
   causal REST gate: [composition](2026-10-29-ear-pr-composition.md).

Текущий scoped intake свежих девяти карточек/source-map завершён. Общий аудит
Ear остаётся неполным; процент по всему проекту не вычислялся.
