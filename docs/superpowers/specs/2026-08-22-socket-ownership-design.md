# Атомарное владение backend Unix-сокетом и truthful startup diagnostics

Дата: 2026-08-22

Ветка: `codex/socket-ownership-m-20260822`

База: `b0840804b21ad32e86266956b8389de2ac2d153e`

## Проблема

Production backend слушает `<data_dir>/krabear.sock`, но
`StartupDiagnostics` без явно переданного пути проверяет другой файл —
`backend.sock`. Поэтому проверка не наблюдает реальный endpoint.

Механическая подстановка правильного имени недостаточна:

- первый `run_all_checks()` выполняется до `IPCServer.bind()`;
- повторный вызов через `get_startup_diagnostics` после TTL выполняется, когда
  текущий процесс уже слушает сокет, и ошибочно принял бы себя за «другой
  процесс»;
- `IPCServer.serve_forever()` сейчас безусловно удаляет любой существующий
  `socket_path` до `bind()`. Второй backend способен оторвать pathname первого
  живого listener;
- shutdown также безусловно удаляет текущий pathname, даже если после bind его
  заменил другой inode.

Нужна единая атомарная модель владения, общая для startup, bind, повторной
диагностики и cleanup.

## Цели

1. Для каждого canonical Unix-socket endpoint одновременно существует не более
   одного cooperating backend-owner.
2. Contender завершается до StateStore, Sentry, моделей и фоновых потоков и не
   трогает socket первого процесса.
3. Стейловый сокет удаляется только под эксклюзивным ownership claim и только
   после identity re-check.
4. Startup diagnostics использует точный `--socket-path` и отличает текущего
   owner от чужого listener.
5. Bind/listen/shutdown не удаляют regular file, symlink или replacement inode.
6. Crash, `SIGKILL`, `SIGSEGV` и `os._exit` освобождают claim средствами ядра.

## Не входит в scope

- Запрет нескольких backend-процессов с разными socket endpoint, даже если они
  используют один `data_dir`. Ownership задаётся точным endpoint.
- Автоматическое завершение процессов по PID/имени.
- Production restart, deployment или live acceptance.
- Изменения Swift/Call Observer/Voice Gateway.
- Массовое обновление документационных счётчиков IPC.

## Рассмотренные подходы

### A. Sidecar flock до build_service — выбран

Для canonical socket path создаётся стабильный sidecar `<socket>.lock`.
`main()` захватывает `flock(LOCK_EX | LOCK_NB)` до любых тяжёлых side effects и
держит FD до завершения `_shutdown_backend`.

Плюсы: атомарная гарантия между новыми версиями, ранний отказ contender,
truthful ownership state для диагностики, автоматический release при смерти
процесса.

### B. Claim внутри IPCServer.serve_forever — отклонён

Diff меньше, но `BackendService.__init__` уже успевает загрузить ресурсы,
запустить фоновые компоненты и выполнить неверную startup diagnostics.

### C. Только connect-before-unlink — отклонён

Live probe полезен для совместимости со старой версией, но без flock остаётся
check-then-unlink TOCTOU между двумя новыми contenders.

## Компоненты

### 1. `backend/socket_ownership.py`

Новый небольшой модуль — единый владелец path-нормализации, read-only probe и
sidecar claim. Вынос нужен потому, что им пользуются два независимых consumer:
`IPCServer` и `StartupDiagnostics`.

#### Canonical path

Публичный helper `canonical_socket_path(path)` реализует одну формулу:

1. `expanduser()`;
2. parent переводится в absolute и `resolve(strict=False)`;
3. конечное имя добавляется обратно без разрешения конечного symlink.

Это схлопывает relative/parent-symlink aliases в один lock-domain, но оставляет
возможность распознать конечный symlink как небезопасный `OCCUPIED`.

`default_socket_path(data_dir)` остаётся доступен из прежних модулей через
импорт/re-export и всегда возвращает `<data_dir>/krabear.sock`.

#### Состояние пути

`probe_unix_socket_path(path)` возвращает `SocketPathProbe` с состоянием:

- `MISSING` — `lstat` вернул `ENOENT`;
- `LISTENING` — путь является Unix socket и `AF_UNIX connect()` успешен;
- `STALE` — путь является Unix socket и connect завершился только
  `ECONNREFUSED`;
- `OCCUPIED` — regular file, symlink, иной тип, timeout, `EACCES`, `EAGAIN` или
  другой неоднозначный результат.

Probe хранит `(st_dev, st_ino)` для существующего пути. Любой временный socket
закрывается через `finally`/context manager. Неоднозначность классифицируется
fail-closed, а не как stale.

#### Ownership claim

`SocketOwnershipClaim` имеет состояния:

- `UNCLAIMED` — FD отсутствует;
- `CLAIMED` — sidecar flock удерживается, listener ещё не подтверждён либо уже
  закрыт в ходе shutdown;
- `LISTENING` — listener успешно прошёл `bind()` и `listen()`.

Минимальный API:

- `acquire()` — открыть стабильный sidecar и неблокирующе взять flock;
- `prepare_for_bind()` — под claim выполнить probe и при необходимости удалить
  только доказанный stale socket;
- `record_bound_socket()` — после `bind()` сохранить identity собственного
  socket inode, не меняя state на `LISTENING`;
- `mark_listening()` — только после успешного `listen()`;
- `cleanup_bound_socket()` — удалить pathname только при совпадении типа,
  `st_dev` и `st_ino`, затем вернуть state в `CLAIMED`;
- `release()` — идемпотентно `LOCK_UN` + `close`, sidecar не удалять;
- `snapshot()` — immutable read-only снимок: canonical socket path, state и
  сохранённый bound `(st_dev, st_ino)` для diagnostics.

Исключения образуют явный контракт:

- `SocketOwnershipError` — общий базовый класс;
- `SocketAlreadyOwnedError` — sidecar contention либо доказанный живой
  listener, включая legacy backend без нового flock;
- `UnsafeSocketPathError` — небезопасный sidecar/path, `OCCUPIED`, смена inode
  перед stale unlink или невозможность доказать корректное владение.

`prepare_for_bind()` возвращает probe только для безопасных `MISSING` и
очищенного `STALE`; для `LISTENING` и `OCCUPIED` бросает соответствующее
исключение до открытия server socket.

Sidecar открывается с `O_CREAT | O_RDWR | O_CLOEXEC`, mode `0600` и
`O_NOFOLLOW`, где он доступен. После `fstat` принимается только regular file,
принадлежащий effective UID. Невозможность доказать безопасный claim блокирует
startup.

Lock-файл никогда не удаляется и не заменяется, включая purge: иначе два
процесса смогут залочить разные inode под одним pathname.

### 2. `IPCServer`

Конструктор получает optional keyword-only `ownership`. Production передаёт
заранее захваченный claim. Прямые unit/embedded callers без аргумента получают
локальный claim, который `serve_forever()` захватывает и освобождает сам.

Перед `bind()` сервер всегда вызывает `prepare_for_bind()`:

- `MISSING` — продолжить;
- `STALE` — helper уже удалил тот же inode под claim;
- `LISTENING` — завершиться, не удаляя путь;
- `OCCUPIED` — завершиться fail-closed, не удаляя путь.

После `bind()` claim запоминает inode. После `listen()` state становится
`LISTENING`. В `finally` порядок: закрыть listener FD, удалить только собственный
совпадающий socket inode, вернуть state в `CLAIMED`; локальный claim затем
освободить.

Production claim в `serve_forever()` не освобождается: им владеет внешний
lifecycle `main()` до конца `_shutdown_backend`.

### 3. `service.main()` и `build_service()`

Порядок startup:

1. разобрать CLI и вычислить один canonical `socket_path`;
2. вызвать существующий `configure_logging(data_dir)`;
3. создать claim, вызвать `acquire()` и `prepare_for_bind()`;
4. только после claim читать early settings, инициализировать Sentry и строить
   `BackendService`;
5. передать в `build_service` точный путь и getter ownership snapshot;
6. передать тот же claim в `IPCServer`;
7. выполнить существующий signal/shutdown lifecycle;
8. во внешнем `finally` освободить claim после `_shutdown_backend`.

Если `build_service()` падает, внешний `finally` освобождает claim. Если
`bind/chmod/listen` падает, сервер закрывает listener и identity-safe чистит
только свой inode, затем штатный coordinator закрывает построенный service, а
внешний `finally` освобождает claim.

`SocketAlreadyOwnedError` (contention или живой legacy listener) завершает startup ненулевым
`SystemExit(os.EX_TEMPFAIL)`. Небезопасный lock/socket path завершается
`UnsafeSocketPathError` и `SystemExit(os.EX_CANTCREAT)`. В обоих случаях
service не строится.

Launchd `KeepAlive=true` может повторять попытку с `ThrottleInterval=5`; это
существующая supervisor policy и не меняется этой задачей.

### 4. `BackendService` и `StartupDiagnostics`

Совместимые optional keyword-only параметры добавляются в `BackendService` и
`build_service`:

- `socket_path`;
- `socket_ownership_snapshot_getter`.

Существующие вызовы `BackendService(store=...)` и `build_service(data_dir)`
сохраняют поведение и сигнатурную совместимость.

`StartupDiagnostics.__init__` сохраняет три существующих позиционных параметра
и добавляет snapshot-getter как keyword-only. Путь выбирается так:

1. явно переданный exact socket path;
2. иначе `default_socket_path(self._data_dir)`;
3. только при отсутствии `data_dir` — default от `settings.DATA_DIR`.

Результат `_check_socket_path_available()`:

- `CLAIMED + MISSING` → `ok`, `owner=self`, `phase=claimed`;
- `LISTENING + LISTENING` и совпадающий probe/bound inode → `ok`,
  `owner=self`, `phase=listening`;
- self-state, противоречащий probe → `warning` с обеими фазами;
- `UNCLAIMED/нет getter + MISSING` → `ok`, available;
- `UNCLAIMED/нет getter + STALE` → `ok`, stale;
- `UNCLAIMED/нет getter + LISTENING` → `warning`, other listener;
- `OCCUPIED` → `warning`, путь не трогается.

Публичная JSON-схема `CheckResult` не меняется; в `details` добавляются только
структурированные `path_status`, `ownership_state` и `owner`. Несовпадение
inode никогда не маскируется одним лишь state `LISTENING`.

Первичный cached report может до TTL честно показывать `phase=claimed`, хотя
listener уже перешёл в `LISTENING`: это правдивый startup snapshot. Следующий
прогон после TTL читает актуальный snapshot. Принудительная cache invalidation не
нужна.

## Совместимость со старым backend

Старая версия не держит новый sidecar flock. Поэтому `prepare_for_bind()` после
успешного claim всё равно делает live-connect probe:

- успешный connect сохраняет legacy listener и блокирует contender;
- только `ECONNREFUSED` допускает stale cleanup;
- перед unlink повторяется `lstat`, и удаляется только тот же Unix-socket inode.

Sidecar полностью закрывает гонку между cooperating новыми версиями. Абсолютно
атомарно защититься от одновременно стартующего старого кода, который сам
безусловно делает unlink, невозможно; новый код не усиливает этот legacy риск и
не удаляет доказанно живой старый listener.

## FD, fork и thread safety

- `O_CLOEXEC` не даёт subprocess после exec удерживать claim.
- Production не использует raw `fork()` для backend lifecycle. Тесты
  межпроцессного contention используют свежий `spawn`/`subprocess`, а не fork с
  унаследованным open-file description.
- `release()` сериализован внутренним mutex: FD извлекается из состояния один
  раз до `LOCK_UN/close`, поэтому повторный release не закроет переиспользованный
  номер FD.
- Signal callback claim не трогает и сохраняет текущий request-only контракт.
- При аварийном завершении ядро закрывает FD и снимает flock; socket inode может
  остаться и будет очищен следующим owner как stale.

## TDD и проверка

### Новый focused test `test_socket_ownership.py`

- actual Unix socket: `MISSING`, `LISTENING`, `STALE`;
- regular file и конечный symlink → `OCCUPIED`, данные/ссылка сохранены;
- timeout и неоднозначная connect-ошибка не становятся `STALE`;
- fresh subprocess не получает второй claim;
- после штатного release и после `os._exit` holder следующий процесс получает
  claim;
- sidecar inode сохраняется между release/reacquire;
- legacy listener без sidecar переживает contender, остаётся connectable и с
  тем же inode;
- stale socket удаляется только под claim и с identity re-check.

### `test_ipc_server_wave1767.py`

- live owner не отрывается contender-сервером;
- normal stop удаляет собственный socket;
- bind/chmod/listen failure закрывает FD, восстанавливает umask и удаляет только
  собственный inode;
- replacement-inode: первый listener не удаляет socket второго listener при
  cleanup;
- локально приобретённый claim освобождается при выходе из `serve_forever`.

### `test_startup_diagnostics.py`

- fallback использует `<data_dir>/krabear.sock`;
- custom path побайтово попадает в details;
- `CLAIMED + MISSING` — self/ok;
- actual listener после TTL при `LISTENING` — self/ok;
- `LISTENING + MISSING/OCCUPIED` — warning, а не маскировка поломки;
- foreign listener и stale path сохраняют совместимый смысл.

### Wiring/compat tests

- `main()` захватывает claim до early store/Sentry/build_service;
- exact path и snapshot getter доходят до `StartupDiagnostics`;
- bound identity из snapshot участвует в self-owner решении;
- source-contract `IPCServer` обновляется на production ownership argument;
- старые optional-free конструкторы продолжают работать.

### Обязательные гейты

- focused pytest для всех изменённых test-файлов с
  `PYTHONPATH=$(pwd)/KrabEar`;
- `scripts/pre_merge_py312_check.sh` для каждого изменённого Python test-файла;
- точная CI-команда flake8;
- `make audit-all`, поскольку меняются backend-модули и добавляется новый;
- `git diff --check`;
- Swift build не требуется, Swift-файлы не меняются.

## Operational boundaries

Никакие тесты не используют production data dir или production socket. Live
backend не перезапускается. `kickstart` не вызывается. Все actual-socket и
subprocess scenarios используют UUID/temp paths.
