# Интеграционная offline приёмка накопленного Ear

**Goal:** проверить все 70 исходных cache cases и максимальную безопасную
реальную backend wiring накопленных изменений, не запускать production.
**Base:** clean local b34e238be714230dffa1a4491104277c056ebe04, на remote16552643
содержит c0dcf9f6+b34e238b. Канонический9bc7edfc dirty с чужими app binaries;
не является интеграционной базой и не меняется.
**WT:** managed ear-aggregate-offline-1029, codex/ear-aggregate-offline-1029.
**Scope:** local source/offline/commit; no push/PR/merge/CI/runtime/models,
STT/mic/network/service jobs/credentials/clone/OrbStack/external code exports.

- [x] Сверить canonical/owned HEAD/status/удалённую базу/владельцев.
- [x] Статически квалифицировать cache и IPCThrottle: stdlib threading/locks,
  scratch JSON atomic writes/fsync, throttle token bucket без I/O/threads.
- [x] Fresh Main START → исходные70cache PASS без исключений и skips (.152s parent).
- [x] Квалифицировать безопасный actual backend wiring/startup seam до imports.
- [x] Исполнить функциональные assertions через реальные компоненты, явно
  сохранить непроверенные full startup/native/audio/IPC границы.
- [x] Независимое review, cleanup receipts; docs/localcommit/handoff закрываются
  этим checkpoint без дополнительных тестов.

## Cache gate

Новый parent наследует прежний tested timeout/KeyboardInterrupt cleanup и
45s budget; меняются только probe path/expected count через class attributes.
Child минимальный HOME/TMP/cwd/env до импорта; прежние sticky guards действуют,
добавлен только audited backend.ipc_throttle. Вместо общего запрета Thread.start
разрешены только три точных nested target qualnames из actual cache tests,
не больше41threads:20+20+1. Все остальные targets/direct _thread запрещены.
В конце join всех owned test threads; receipt требует started41/alive0,
один surviving Python thread, никаких violations/skips/exclusions.
Реальные lock/fsync/throttle методы не подменяются стендом; исходные fixtures
могут mock fsync для проверки порядка, как в оригинальных tests.

Команда только после нового Main START:
`/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' /opt/homebrew/anaconda3/bin/python3 -I -B KrabEar/tests/test_translation_aggregate_offline.py`

## Startup boundary, пока source audit

Full service импортирует engine/recorder/event bridge и десятки менеджеров;
constructor запускает MemoryConductor, export/purge schedulers, REST watchdog,
EventBridge и другие workers. Generic MagicMock всего не доказывает wiring.
Нужен конкретный изолированный functional seam, а не запуск main()/lifespan.
Новый дорогой audit-all без интеграционной цели не повторять.

## Functional wiring gate

Независимый read-only source audit подтвердил: service→transcriber→engine
пытается импортировать mlx_whisper/soundfile/pyannote/funasr/nemo/whisperx;
recorder→sounddevice. StartupDiagnostics дополнительно пробует socket/model/
audio. Fake recorder/transcriber при уже выполненном импорте недостаточны.

Ruling: вместо broad generic mocks выполнить непрерывный actual constructor
prefix до `_start_time`, actual helper/getter definitions и отдельные actual
constructor assignments `_live_subs`/`_translation`. Это функциональная
совместимость накопленных helpers+компонентов, а не полный BackendService.
AudioRecorder/Transcriber constructors — forbidden sentinels; переданы inert
recorder и transcriber.engine; `_init_llm_rewriter`/extractor и единственный
pre-import CallSTTService module stub не запускают ML. Warmup policy настоящая,
флаг прогрева выключен persisted settings; неожиданный thread — sticky denial.

Полностью реальные SettingsService/StateStore/3 local stores/Translator/cache/
TranslationService/LiveSubsService + plaintext authorizer/IPC adapter (без
транспорта). LiveSubs импортирует только numpy, core.utils (stdlib regex),
event bus (queues/locks), contracts (pydantic/enum/datetime), stt_budget.
Рабочие callbacks не запускаются, ingest разрешён только privacy early return;
idle close проверяется реально. Shared event bus не получает listeners/workers.

Пять случаев: identity/providers+disk stores; handler cache/reopen без второго
transport вызова; selection routing+real glossary save/reopen; privacy commit
и corrupt settings через actual handler/getter; LiveSubs wiring/privacy gate/
идемпотентный idle close. Model transport подменён, обработчики/кэш/сохранение
реальны. HOME config hot-reload path явно равен StateStore.settings_path.

Команда после нового Main START:
`/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' /opt/homebrew/anaconda3/bin/python3 -I -B KrabEar/tests/test_translation_wiring_offline.py`
Реальные LiveSubs worker/late cancellation/STT/subscription replay этим
не проверяются; full startup/dispatch/socket/native/audio остаются вне gate.

## Evidence ledger

- Cache70: exact parent fda0e134/probe d7b574e0/shared parent61bbb5f9/guard25c1f553
  до/после совпали. Parent1 PASS (.152s), child70PASS/0fail/errors/skips/
  excluded/violations. 41 real test threads started/joined, alive0, finalPython1.
  ChildPID82634 exit0/reaped/group_empty=true/no timeout. Raw artifact
  `ear-aggregate-offline-20261029/cache70_tool_result.json` в каталоге визуализаций
  этого чата. Это новый запуск всех70, старые64 не прибавляются к нему.
- Независимый Astra Ultra whole-diff preflight: SOURCE_GO, blockers0, execution0,
  final wiring probe6fa56f4b/parent ea01b86d. Приняты actualprefix/assignments,
  scratch configpath, реальные handlers/settings и guards/cleanup. Corruption
  проверяется getter/privacy helper, не translate request после corrupt;
  snippets/phonetics — provider consistency, vocabulary дополнительно reopen.
- Интерпретатор текущих запусков проверен: Python3.12.7. Это macOS interpreter,
  не Ubuntu parity/full pre-merge harness. Прежнее «Python3.12 не запускался»
  надо читать как «не было отдельного Ubuntu parity gate», не версия этого Python.
- flake8 пяти новых/изменённых Python test файлов и git diff --check PASS.
- Wiring5 после независимого review и отдельного Main START: parent1 PASS
  (.435s), child5PASS/0fail/errors/skips/violations, threads1. PID94483
  exit0/reaped/group_empty=true/no timeout. 21 исходный constructor-prefix
  statement, full_startup=false/ipc_transport=false/backend.service=false.
  Probe6fa56f4b/parent ea01b86d/shared parent61bbb5f9 до/после совпали.
  Raw artifact `ear-aggregate-offline-20261029/wiring5_tool_result.json`.
- Из-за параметризации общего parent запрошен один минимальный запуск прежнего
  default wrapper9+cleanup2; cache70/wiring5/audit-all не повторяются.
- Default compatibility после fresh Main START: parent3PASS (.354s),
  child9PASS/0fail/errors/skips/violations/Python1/backend.service=false.
  PID95227exit0/reaped/group_empty=true/no timeout; два causal cleanup cases
  также PASS. Shared parent61bbb5f9/probe25c1f553 и новые probe hashes unchanged.
  Raw `ear-aggregate-offline-20261029/default9_cleanup2_tool_result.json`.

## Итог доступного этапа

84 component/cache test cases (70+5+9) и 2 lifecycle checks PASS в трёх отдельных
согласованных запусках; это 5 parent tests, child wrappers повторно не считать.
Предыдущие64cache и старыеconstructor suites не добавляются к текущим counts.
Production code поверх b34e238b НЕ менялся: добавлены только test fixtures и docs.
Новых подтверждённых source регрессий этот этап не нашёл.

Открытые app gates: полный service import/constructor/close; real worker
cancellation/drain/replay delivery; Unix IPC/REST/native Swift startup;
настоящая обработка аудио/модели; Ubuntu/full pre-merge и hosted CI. Ни один
не закрывается пустой очередью/idle close/AST prefix или текущим GREEN.
Никаких runtime/publication/model/config/clone/OrbStack операций.
