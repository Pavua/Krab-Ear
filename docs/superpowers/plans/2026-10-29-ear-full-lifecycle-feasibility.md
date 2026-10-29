# Full BackendService offline lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** проверить настоящий импорт, полный конструктор и idle-close BackendService без production I/O и воспроизвести конкретный дефект shutdown verdict.

**Architecture:** отдельный дочерний Python-процесс получает scratch HOME/TMP/cwd и защиту до импорта приложения. Внутренние constructors, dispatch, start/stop и close исполняются из настоящих модулей; внешние необязательные ML/audio imports представлены недоступными, запуск только известных scheduler targets перехватывается на границе Thread.start.

**Tech Stack:** существующий Python 3.12, unittest, subprocess, sys audit hooks; без новых зависимостей.

**Spec:** поручение координатора: полный actual-class lifecycle feasibility, без очередного AST-prefix и без подмены внутренних сервисов ради PASS.

**База:** remote `165526430d6c4d378b69da5abfa71eb6076982c9`, проверен ls-remote 29.10; локальный принятый offline checkpoint `91a9b71490bdc629beaa069973bc0e1d68804748`.
**Worktree:** `ear-full-lifecycle-1029`, branch `codex/ear-full-lifecycle-1029`.

## Актуальный результат

**Локальный full-constructor/close contract gate PASS; runtime не проверен.**
Настоящий полный import/constructor/dispatch/idle-close принят в RUN3:
1PASS/1.445s, child52378 exit0/reaped/PGIDempty; violations/denials/providers/
errors[], Python3.12.7, threads1, пять suppressed scheduler targets.

После этого отдельный причинный RED (1FAIL/1.345s, child55407) подтвердил
ложный успех close: exportAliveTrue после join2, closeTrue, transcriberClose1.
Guard violations0. Минимальный production fix5строк повторно проверяет alive
после join и устанавливает all_workers_stopped=False. Handle сохраняется;
join-exception policy не менялась и новым тестом не квалифицируется.

На exact service `349728695517ba38b9632d539a0d96d663b5be4bfe12750c889090830d93be8b`,
probe `1a43da7a48e90a71fc42779f30ceb021e6ea168d04517807aaa164e4337f3192`,
parent `f61785357fca5393c285f622e1a154e6ee6160173532356f6593f0b8e36f507e`:
strong whole-diff SOURCE_GO → GREEN **2PASS/2.768s**, children61451/61486
exit0/reaped/PGIDempty, guards/providers/errors0/threads1/suppressed5 каждый.
До cleanup export case: closeFalse/aliveTrue/joins[2]/ownerRetained/stopSet,
transcriberClose0; после снятия alive retryTrue и настоящий transcriberClose1.
Idle case closeTrue. Все три source hashes после прогона совпали.

Артефакты: `ear-full-lifecycle-20261029/RUN3_TOOL_RESULTS.json`,
`EXPORT_RED_TOOL_RESULTS.json`, `EXPORT_GREEN_TOOL_RESULTS.json` в artifact
folder чата; RUN1/RUN2 отрицательные квитанции сохранены отдельно.

Материальный предел: existing `_shutdown_backend` при closeFalse вызывает
hard-exit до metadata (`service.py:309–317`), automatic retry не делает.
Фикс исправляет неверный verdict/direct close, не гарантирует сохранность
активного export/metadata или безопасный production shutdown. Три bus listeners
и replay остаются удержанными в product snapshot — отдельный незакрытый gate.
Full workers/drain/main/IPC/native/audio/реальный OS tzconfig/Ubuntu/hosted CI
не квалифицированы. Runtime, модели, клон и OrbStack не изменялись.

В отдельном Main START `make -j1 audit-all` PASS: 14 scripts + 2 selftests,
16 Python commands, 48.884s, exit0/reaped/PGIDempty, без timeout; 1808 source
hashes стабильны. Это source audit, не универсальная I/O-изоляция импортов.
Flake8 трёх изменённых Python files PASS/0.598s, exit0/reaped/PGIDempty;
три source hashes стабильны. Receipts: `STATIC_AUDIT_RECEIPT.json`,
`STATIC_AUDIT_SOURCE_HASHES.json`, `STATIC_AUDIT_STDOUT.log`, `LINT_RECEIPT.json`,
`LINT_STDOUT.log` в том же artifact folder. Ear RUN0; новых запусков нет.
Локальный оговорённый блок завершён на 100%; публикация/CI/runtime не входят.
`pre_merge_py312_check.sh` не запускать в текущем scope: preflight импортирует
backend вне guards и может переустановить shared venv/dependencies.
Далее исторические шаги плана и ранние checkpoints, не текущий RUN0/diff0 verdict.

## Global Constraints

- Source preparation разрешена; любой запуск — только после fresh Main START, приоритет VG Translator.
- Не повторять неизменённые cache70/wiring5/component9/cleanup2.
- Никаких моделей, STT, микрофона, production workers, IPC listener, сети, пользовательских данных/credentials, runtime/config/restart, клона или OrbStack.
- Без push/PR/CI/merge/deploy и внешнего экспорта кода. Чужой dirty checkout не менять.
- Не заменять BackendService, его helpers/dispatch/close, StateStore, SettingsService, Recorder, Transcriber, AudioEngine, router или внутренние сервисы generic mocks.
- Не наследовать Thread с фиктивным start; изменять только scheduling boundary в disposable child.
- Все caught guard violations остаются sticky FAIL; skips запрещены. Ожидаемо недоступные provider imports учитываются отдельно от нарушений, до загрузки provider-кода.
- Не вызывать `main`, `run_server`, `_shutdown_backend` или hard-exit helper: это другой gate (IPC/signals/process lifecycle). `os._exit` в child profile перехватывается как sticky violation, чтобы приложение не обходило итоговую квитанцию.

## Review Focus

1. Optional-import fallback не должен скрыть реальную загрузку ML/audio provider.
2. Перехват scheduling не доказывает drain реально работающего worker.
3. Ошибка диагностики не должна проглотить нарушение границ стенда.
4. Shutdown verdict снимается до cleanup самого стенда; старый PASS не заменяет новый запуск.
5. Один service.close не должен удалять подписки/replay другого владельца.

## Статическая карта (линии базы 91a9)

| Узел | Источник | Решение для стенда |
| --- | --- | --- |
| Eager ML/audio imports | `core/engine.py:32–119`, `backend/recorder.py` | Pre-import finder выдаёт ImportError только для точного reviewed перечня optional providers; реальный fallback сохраняется. Не импортировать provider для проверки его наличия. |
| Config | `core/config.py:24–64`, Settings env_file | HOME и cwd в scratch до импорта; env строится заново. Реальный settings.json только в scratch; запрет reads `.env`/`.secrets` вне scratch. |
| Полный конструктор | `backend/service.py:480–1923` | Вызывать `BackendService(StateStore(scratch))`; без AST/exec и без injected internal services. |
| Warmup | `service.py:529–546,688–701`; `core/engine.py:561–569` | Реальные `LLM_ENABLED=False`, `STT_GIGAAM_ENABLED=False`; runtime `rewriter_warmup_on_startup=False`, `stt_warmup_on_startup=False`. |
| Необязательные внешние каналы | `service.py:818–820,934–962,1145–1203,1671–1674` | Реальные flags binary drift, recap, REST_IN_PROCESS, EventBridge, backup, disk/duration watchdog, auto seed выключены. REST watchdog не создаётся при отсутствующем in-process REST. |
| Обязательные scheduler starts | `service.py:774,920,962,1631`; `startup_diagnostics.py:615–626` | Exact target identity/name: MemoryConductor._loop, BackendService._export_scheduler_loop, PurgeScheduler._run, WakeWordWatchdog._run (`wake_word_watchdog.py:198–201`), StartupDiagnostics._run_lm_studio_check_in_background. Записать реальные Thread objects, targets не исполнять; неизвестный start — FAIL. |
| Join не стартовавших thread objects | `memory_conductor.py:196–204`, `purge_scheduler.py:111–116` | Join interception только для конкретных ранее зарегистрированных scheduler objects. Нельзя объявлять их завершившимися реальными workers. |
| Startup diagnostics | `startup_diagnostics.py:126–194,245–269,335–486,762–815` | Реальный aggregator, package/error paths и audio-unavailable. Scratch socket отсутствует: `socket_ownership.py:104–111` возвращает MISSING после lstat без socket creation. Подмена probe не нужна; весь run_all_checks сохраняется. |
| LM diagnostic | `startup_diagnostics.py:615–706` | Только recorded scheduler; pending допустим и явно отражён в receipt, не READY. |
| Actual cleanup | `service.py:2502–2711`, `core/engine.py:595–622` | Реальные service.close/Transcriber.close/router.close. MLX session остаётся отсутствующей; запрещён её spawn. |
| Сильные ссылки | `service.py:773,995,1037,1200`; `event_bus.py:98–141` | Три listener и replay устанавливаются в singleton bus; service.close не содержит remove_listener/replay detach. Проверять отдельно, не смешивать с exporter fix. |

Финальный reviewed optional provider перечень: mlx_whisper, soundfile,
sounddevice, torch, pyannote, funasr, nemo, whisperx, mistral_inference,
mistral_common, sentry_sdk. Другие `mlx`, `transformers`, `huggingface_hub`
не добавлять автоматически: требуется конкретный source call site.
`find_spec("openwakeword")` контролируемо возвращает отсутствие пакета,
настоящий OpenWakeWordAdapter сохраняется. Настоящий requests нужен при
импорте telegram_bridge; HTTP/DNS/socket transport запрещён до его импорта.
Неожиданный httpx — HOLD до source review. Новые filesystem/metadata probes
разрешать только после точного source просмотра.

Дополнительно выключить реальные semantic search, auto-purge и wake-word
flags. До конструктора проверить равенство `config._SETTINGS_JSON_FILE ==
store.settings_path`: одного DATA_DIR недостаточно.

Независимый source reviewer подтвердил осуществимость с paired start/join seam;
start-only no-op отклонён как искусственный false-green (join не начатого Thread
бросает RuntimeError, который service.close проглатывает). Перехват join хранит
собственную квитанцию каждого suppressed object; наличие записи не означает drain.
В source найдены register_at_fork callbacks (регистрация разрешена, сам fork запрещён),
GigaAMAdapter.__del__ (`core/pipeline/stt_gigaam.py:430`, disabled profile его не создаёт)
и atexit только в запрещённом `backend/rest_server.py:989`.

После product assertions и до выхода child отдельно выполняется fixture cleanup:
реальные `WebhookManager.shutdown(wait=True)` (`webhook_manager.py:609`),
audit/replay/cache close, удаление собственных bus listeners и ownership-safe
восстановление исходного replay pointer. Cleanup не считается результатом
BackendService.close. До него receipt фиксирует retention/открытые ресурсы:
GracefulShutdownHandler закрывает audit/replay отдельными шагами
(`shutdown_handler.py:337–345,378–388`), поэтому отсутствие их закрытия именно
в service.close не объявляется само по себе доказанным production leak.

## Task 1: квалифицировать полный импорт и disabled idle constructor

**Files:**
- Create: `KrabEar/tests/backend_lifecycle_probe.py` — отдельный профиль защиты, без ослабления translation probes.
- Create: `KrabEar/tests/test_backend_lifecycle_offline.py` — bounded child runner по существующему parent cleanup механизму.
- Reference: `KrabEar/tests/test_translation_components_integration.py` — timeout/interrupt cleanup.

**Interfaces:** child печатает один JSON receipt: full_module_imported, constructor_completed, dispatch_built, close_result, scheduled_targets, started_workers, violations, tests, skips, threads_after. Parent подтверждает rc/reaped/PGIDempty; runtime_ready не выдаётся.

- [x] Завершить независимое source review точного import/scheduler/I/O профиля. Профиль принят независимым source review до каждого запуска.
- [x] Написать actual-class тест: `service.__class__ is backend.service.BackendService`; constructor дошёл до `_LIVE_INSTANCES`; dispatch содержит реальные settings/history handlers; Recorder/Transcriber/engine созданы настоящими классами; `close()` вызван в finally.
- [x] В child до приложения поставить no-egress/no-spawn/non-scratch mutation guards, точный expected-unavailable import перечень и scheduler identity ledger. Проверить ноль violations даже если приложение поймало исключения.
- [x] Target сверять по bound owner и `__func__`, не только имени/qualname. Scheduled/suppressed/intercepted joins отражать раздельно; stop вправе не звать join при is_alive=False. Sticky ledger проверять после всей fixture cleanup.
- [x] Сохранить owner до вызова actual `__init__` через actual `BackendService.__new__`, затем вызвать настоящий полный `__init__` с scratch store. При constructor failure owner остаётся доступен для явной cleanup; `_LIVE_INSTANCES` до конца конструктора недостаточно. Это не подмена класса/constructor body.
- [x] После fresh Main START выполнить один bounded child. На неожиданном импорте/side effect остановить gate, записать точный source prerequisite; не расширять mocks на внутреннюю логику.
- [x] Parent command после source review и Main START: `/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' /opt/homebrew/anaconda3/bin/python3 -I -B -m unittest discover -s KrabEar/tests -p test_backend_lifecycle_offline.py -v`; это тот же interpreter path, что в aggregate checkpoint (тогда3.12.7; текущую версию добавить в receipt). Обычный `/opt/homebrew/bin/python3` сейчас указывает на3.14.8 и не заменяет этот gate. Child обязательно `sys.executable -I -B backend_lifecycle_probe.py <scratch>`, wall budget45s, own process group. При timeout/interrupt — только owned PGID cleanup и обязательная parent receipt; выполненные запуски и их границы перечислены в актуальном результате выше.
- [x] Успех означает только full-import/full-constructor/idle-close в disabled scheduling profile. Реальные scheduler loops/startup worker drain/IPC/main/native/audio не проверены.

## Task 2: причинный export-worker shutdown verdict

**Files:**
- Test: `KrabEar/tests/backend_lifecycle_probe.py`.
- Conditional minimal fix: `KrabEar/backend/service.py`, только export scheduler close block.

**Interfaces:** actual `BackendService.close() -> bool`; controlled external thread-handle boundary предоставляет `is_alive()` и `join(timeout)` и не запускает thread. Остальные внутренние cleanup bodies остаются настоящими, предпочтительно на полном owner из Task 1.

- [x] Сразу после полного constructor назначить export handle со сценарием alive после join; transcriber close-call counter оборачивает реальный close, не подменяет результат.
- [x] Проверить до fixture cleanup: service.close возвращает False, stop event взведён, join вызван с timeout2, handle сохранён для повторного close, transcriber.close не вызван при незавершённом worker.
- [x] Ожидаемый RED текущего исходника: close возвращает True и/или закрывает transcriber при alive exporter. Отдельный причинный RED подтвердил этот сценарий до исправления.
- [x] После причинного RED минимально перенести alive-after-join в существующий all_workers_stopped verdict. Join-exception policy сохранена; новые architecture/runtime изменения не делать.
- [x] GREEN нового case с повторным close после завершения плюс затронутый idle baseline: 2PASS; отдельного join-exception покрытия нет, owner handle сохранён.
- [x] Не потребовалось (Task1 PASS): если Task1 HOLD, отдельный actual imported class `__new__` owner допускается только как partial-owner close test с честным названием и явным review minimal prerequisites; не объявлять его fullconstructor proof.

## Остальные findings / отдельные prerequisites

- MemoryConductor.stop исключение в service.close логируется и не влияет на verdict; сам stop также не проверяет thread.is_alive после join. Не фиксить независимо без причинного теста и понятной семантики всего owner chain.
- Listener/replay retention требует ownership-aware detach: существующий remove_listener использовать, replay очищать только у своего владельца, с учётом конкурентной смены owner. Не делать безусловное bus._event_replay=None и не утверждать уже прочитанные emit snapshots отменёнными.
- Partial constructor failure до `_LIVE_INSTANCES.add` — отдельный resource ownership gate; успешный idle-close его не покрывает.
- Полный startup с реально работающими schedulers требует отдельных bounded no-egress task fakes/queue window; no-start fixture этого не доказывает.

## Historical source-map checkpoint (до реализации)

Статическая карта и независимый review завершены: SOURCE_GO для реализации
этого плана; execution HOLD до fresh Main START. Production diff0, tests RUN0;
CI/publication/runtime0. Map/review-блок100%, full-import/constructor execution0%.
Для реализации сложного pre-import guard рекомендован Sol6.1 High; финальный
whole-diff gate — сильная модель Ultra. Модель этой сессии не переключалась.

### Execution checkpoint: первый strict gate FAIL

Main выделил одно окно; parent1FAIL/rc1 за1.662s, child43726 rc1/reaped/
PGIDempty/no timeout. Python3.12.7, полный импорт/constructor/dispatch/close
достигнуты, пять suppressed targets, providers0, Pythonthreads1; errors[].
Однако sticky ledger содержит `non-source non-scratch read` и `socket.__new__`.
Это isolation FAIL, не product RED и не acceptance. Runtime0, тестовый слот
освобождён; повторного выполнения не было.

Source review установленного urllib3/util/connection.py:114–137 подтвердило
import-time IPv6 bind probe под `socket.has_ipv6`. Независимый SOURCE_GO на
точечный external capability `socket.has_ipv6=False` до HTTP imports, все
socket/DNS/no-egress запреты сохранены. Без стека первого запуска точную
атрибуцию socket denial не утверждать. Denied-read источник UNKNOWN;
allowlist не расширять. В probe добавлена bounded no-I/O диагностика denials
через sys._getframe/f_back, только basename/function/line, без source/locals/
arguments; frames освобождаются. Для любого следующего запуска нужен новый
Main START после review нового freeze. Сохранённая квитанция:
`ear-full-lifecycle-20261029/RUN1_RECORDED_RECEIPT.json` в artifact folder чата.

В отдельном Main diagnostic START: RUN2 parent1FAIL/rc1 за1.354s,
child48713 reaped/PGIDempty/no timeout, providers0/threads1. Socket denial
исчез; оставшийся read теперь имеет точный stack: Pydantic schema import →
zoneinfo._tzpath._reset_tzpath:22 → sysconfig → _osx_support:99 →
SystemVersion.plist. Raw result `RUN2_TOOL_RESULTS.json`. Read guard сохранён.

Независимый source review разрешил explicit parent env `PYTHONTZPATH=''`:
установленный stdlib zoneinfo/_tzpath.py:19–23 обращается к sysconfig только
при отсутствии env, :37–39 превращает пустое значение в (). Child проверяет
env до app imports, затем настоящий zoneinfo.TZPATH == [], parent сверяет
receipt. Это только timezone search-path fixture, не проверка реального OS
tzconfig и не утверждение, что conversion/data полностью отключены: возможен
tzdata package fallback. Guards не ослабляются, предыдущие отрицательные
квитанции сохраняются; новый запуск только после fresh review/freeze/START.
