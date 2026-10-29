# Компонентный gate реального translation wiring

База `c0dcf9f6ba577e3c89efa092dad718da2fb80a4a`, та же owned ветка/WT.
Продолжение практической приёмки helper, не новая фаза архитектуры.

## Статический import graph и границы

- Translator → performance_profiler → numpy: singleton держит lock/deques,
  worker не запускает. `_build_pipeline` — единственная подменённая transport
  граница; реальные normalize/cache/result/error/cancellation пути сохраняются.
- TranslationCache → stdlib: constructor читает только явный data_dir.
- SettingsService → StateStore/models/config, validator, backup, observability.
  core.config создаёт Settings при импорте, читает HOME/settings/.secrets и cwd/.env:
  HOME/TMP/cwd и минимальный env задаются ДО импорта в новом процессе.
- StateStore → parsing_utils/search_index/plaintext policy: явный scratch
  data_dir, реальные NDJSON/flock/settings atomic writes. False encryption
  policy читает history_crypto SENTINEL/cryptography, Keychain не вызывается.
- validator → gigaam_compat/stt_budget: константы/типы, моделей не импортируют.
- observability импортируется реально, Sentry не инициализируется; вызовы
  breadcrumb возвращаются до lazy SDK import. Backup directory задан явно.
- Полный backend.service затягивает audio/recorder/engine/event bridge и здесь
  запрещён. Исполняются только четыре actual owner method definitions:
  helper/getter/cache getter/invalidate; компоненты импортируются целиком реально.

## Исполнение

Один unittest-wrapper запускает один disposable child (45 с, собственная PGID),
в котором девять реальных компонентных случаев. До импортов: OS network deny
на всём pack; Python import allowlist и audit-hook запрещают subprocess/socket,
модели/keystore/Sentry, non-scratch writes/config reads; запрет Thread.start.
Даже подавленный компонентом denial остаётся в receipt и делает gate красным.
Проверяется отсутствие backend.service, фоновых Python threads и skips.
SQLite в проверенном графе нет; unexpected non-scratch sqlite запрещён.

Команда из owned WT (только после отдельного Main START):
`/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' /opt/homebrew/anaconda3/bin/python3 -I -B KrabEar/tests/test_translation_components_integration.py`

Случаи: persisted getter до запроса/cache identity; настоящий cache write/read/
reopen; SettingsService TTL/invalidate; privacy transition очищает RAM/disk;
transport error; cancellation; failed atomic settings commit; corrupt settings
fail-closed; malformed non-string persistent cache entry. Последний случай
воспроизвёл `.split()` на int: исходный загрузчик принял нестроковый cache hit,
реальный Translator выбросил AttributeError. RED: 8 PASS / 1 ERROR из 9,
без skips/guard violations, один Python thread, backend.service не импортирован.
После communicate дочерний процесс reaped; точная пустота его PGID в этом
первом RED не была записана и остаётся UNKNOWN (проверка стояла после assert).

## Ledger исправления и независимого ревью

- Подтверждённый корень: `_load` принимает произвольные JSON values, тогда как
  Translator ожидает строковый hit. Фильтрация строк перенесена до count-limit,
  чтобы плохая последняя запись не вытесняла хорошую. Byte/LRU правила сохранены.
- Регрессия расширена: int/float/bool/list/dict/null и mixed valid/invalid
  при max_entries=1; это subcases одного из девяти компонентных случаев.
- Независимое Astra Ultra review: loader SOURCE GO, wrapper P2 HOLD:
  KeyboardInterrupt оставлял detached child, timeout терял cleanup receipt.
- Causal lifecycle RED2: 2 FAIL, 0 ERROR, 0.017 с. Оба controlled sleeper
  гарантированно убраны fixture finally; production-компоненты не импортировались.
- Исправление P2: общий finally убирает только собственную PGID, bounded
  communicate(5), печатает factual reaped/group_empty/timeout до verdict/rethrow.
  Тот же reviewer проверил fix: P2 CLOSED / SOURCE GO, hash wrapper `c0390355`.
- GREEN после нового Main START: 3 parent PASS за 0.375 с (2 lifecycle +
  1 component wrapper), child 9 PASS / 0 failures / 0 errors / 0 skips.
  Violations=[], Python threads=1, backend.service=false. Child PID49856,
  exit0/reaped=true/group_empty=true/timeout=false. Lifecycle cases проверяют
  reaped/group_empty до страховочного cleanup fixture. Это не живой E2E.
- Ruling: полный backend не импортировать. Проверять реальные компоненты
  вместе с четырьмя actual owner methods; startup/IPC/native остаются открытыми.
- Ruling: совместимость проверять исходными cache tests без pytest/conftest:
  64 из 70, явно исключены 3 thread cases и 3 unrelated IPC throttle cases.
  После отдельного Main START: 64 PASS за 0.069 с, exit0, skips0,
  violations=[], Python threads=1, backend.service=false. Это исходные
  проверки, не копии assertions; три thread cases и три IPC throttle
  cases не исполнялись. Внешний runner `legacy_cache_gate.py` hash `d56ea5e8`.

## Закрытие локального блока

flake8 трёх Python-файлов и git diff --check PASS. Полный pre_merge_py312_check
не выполнялся: он импортирует полный backend и может создавать/изменять venv,
что выходит за этот узкий offline gate. Python 3.12/Ubuntu/hosted CI остаются
отдельной проверкой перед будущей публикацией; текущая публикация запрещена.
Нового audit-all не требовалось: production менялся только в loader кэша,
core/pipeline и service extraction этого блока не менялись.

Квитанции вне репозитория:
`/Users/pablito/.codex/visualizations/2026/10/21/01a15c4e-ea0a-7d80-b807-f6c6224a9030/ear-constructor-translation-20261029/`
— `component_green_tool_output.txt` (копия завершённого tool output),
`component_gate_receipt.json`, `cache_compat_tool_result.json`.

Правки production — только после causal regression. Без full BackendService,
lifespan/IPC/native/model/network/production/GUI, push/PR/CI/merge/deploy.
Независимое whole-diff review после результата. Девять проверок прошлого среза
были 8 characterization + 1 shape, не девять новых функциональных регрессий.
