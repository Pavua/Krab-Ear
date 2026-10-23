# Translation: reconnect и догрузка пропущенных фраз

**Goal:** сохранять последние 200 показанных фраз штатного IPC-пути панели
и догружать пропущенные при разрыве SSE, не теряя легитимные повторы текста.

**Base:** origin/codex/krab-ear-v2, интеграция #2118 c44db7d93f741237fc8ffb2746169c299d3232aa
(27 exact-head checks SUCCESS перед merge). Post-merge CI проверяется отдельно.

**Architecture:** существующий EventReplayManager остаётся единственным журналом.
get_event_log получает opt-in cursor_mode: epoch+seq, страницы до200 событий,
has_more и явный gap при restart/purge/retention. Старый API сохраняется.
Первый снимок возвращает последние200 подходящих событий текущего журнала.
SSE становится сигналом reconciliation через IPC, без дедупликации по тексту.
Периодическая сверка раз в2s покрывает потерянный сигнал/bridge/startup race;
один IPC in-flight, bounded pages и weak callbacks. Панель хранит не более200фраз.
EventBus записывает replay до публикации сигнала. Новый REST endpoint/bridge не нужен.

**Баны:** не менять Main/Gateway, модели/keepalive, production настройки;
не запускать приложение, capture, микрофон/clipboard; не рестартовать backend;
не возвращать wakeword наSSE; не включать REST_IN_PROCESS_ENABLED;
не мержить#1875; явные git add; не трогать чужойWIP; без новых зависимостей.

- [x] RED/GREEN: курсор+пагинация, одинаковые timestamps/text, фильтрация,
  retention overflow, новый backend, clear/purge, privacy и malformedcursor.
  `PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_translation_replay_cursor.py`
- [x] RED/GREEN: запись replay до fanout сигнала EventBus.
- [x] RED/GREEN: реальный TranslationStreamView с подменой только transport/IPC:
  EOF/retry, пропущенные фразы, одинаковый текст, overlapping hints,
  одна IPCоперация, отказ/поздний callback/деинициализация и ограничение200.
- [x] Real loopback HTTP/URLSession reconnect + production replay IPCcontract,
  проверка пропущенного события и отсутствия дублей, без productionпроцессов.
- [x] Тесты зависимых Python/Swift файлов: combined372 PASS, Python3.12 без MLX
  17 файлов изолированно PASS, release jobs2 и audit-all PASS.
- [x] Независимый whole-diff review (privacy/read-path включён), SOURCE PASS.
- [ ] PR, точный CI; source/CI не выдавать за production memory acceptance.

При исчерпании retention сохраняем показанные фразы и показываем явный разрыв.
Новый backend epoch очищает UI: purge мог пройти между чтениями; серверный
журнал при restart сбрасывается. Обычный SSE reconnect сохраняет и догружает.
Недоступность IPC не двигает cursor и не очищает панель; повтор ограничен timer.

## Уточнения после RED/совместного GREEN

- Initial/SSE-reset hints могут опережать EOF: фикстуры меняют набор фраз
  по фактической фазе соединения, а не по номеру IPCвызова.
- Узкий live_subs cursor-read использует отдельный light-бюджет120/мин.
  Legacy debug-read и курсоры остальных типов остаются medium30/мин.
- Privacy и purge имеют приоритет над сохранением показанных фраз:
  ответ содержит privacy_mode и reset_reason (restart/clear); очистка
  журнала и новый backend epoch должны очищать UIcache.
- Backend snapshot invalidated by concurrent purge не возвращает payload.

Whole-diff review выявил неопределённость durable marker при ошибке диска:
после purge и restart прежний marker мог ложно подтверждать старый cache.
RED: 7paneltests/4assertions. Решение: не добавлять durable metadata, очищать
UI на любом новом backend epoch. Сетевой reconnect того же epoch сохраняет
строки и догружает. Также обнаружен второй REST-native WS producer вне IPC journal.
Compatibility сохранена прямым SSE с серверным `_replay_source`; caller не может
подменить метку. IPC-сигналы не рисуют raw payload. RED2backend/2Swift(4assert),
GREEN14backend/17Swift, включая настоящий HTTPRESTreconnect и повтор текста.
REST-local пропуски во время обрыва не входят в IPC replay. Расширение этого
контракта выделено отдельно; вопрос о включении в текущую карточку задан владельцу.


Предыдущий checkpoint770e2a89: combined370PASS/releasePASS/Python3.12parity5filesPASS;
после review дерево изменено, финальная квалификация требуется заново.

Hosted CI checkpoint4f21e43f выявил старую гонку HealthMonitorTests: fixed300ms
не гарантировали два timer tick. Три PhaseA теста переведены на ожидание реальных
callbacks с предельным timeout5s; тест reset проверяет переход на четвёртом
ping для fail/success/fail/fail. Production HealthMonitor не менялся.

## Расширение 09:49 CEST: владелец включил оба пути в эту карточку

**Base:** reviewed 5bf685b2; отдельный worktree codex/ear-rest-replay-1023.
Ранее27CI SUCCESS относится к 5bf; расширенный diff квалифицируется заново.

Рекомендованный контракт (решение исполнителя):
- Один существующий EventReplayManager для IPC+REST, общий initial tail200.
  Cursor incremental pages до200; retention конечный, gap явный.
- Сервер выдаёт случайный одноразовый ticket до STT, TTL300s, максимум256.
  Ticket metadata хранит expiry/acceptedseq/SHA256 payload, без текста; epoch задаётся общей таблицей. Clear очищает
  tickets атомарно с journal. После expiry неизвестный ticket только отклоняется;
  клиент не выбирает ID и не может повторно зарегистрировать старый.
- REST commit использует fixed live_subs.result и строгую bounded схему. Один
  lock проверяет ticket и append; identical duplicate ticket возвращает прежний seq; изменённый payload даёт conflict.
  ACK означает принятие RAM-журналом, без обещания пережить crash/IOerror.
- Отдельный purpose-bound token0600 для begin/commit, обязательный при любом
  режиме общего IPC signing. Audit не сохраняет параметры/результаты этих RPC.
  Нет generic emit/write endpoint. Обычный IPC auth/throttle остаётся.
- Нового REST outbox и новой истории нет. Вызовы begin/commit идут из текущего
  фонового STT worker с общим ограниченным deadline, не из EventBus listener.
  Никаких фоновых retry: один immediate retry commit с тем же ticket.
- После accepted/duplicate ACK REST публикует origin=ipc SSE hint, payload есть
  в общем журнале; emit_envelope не создаёт echo. Неизвестный commit исход
  не превращается в raw text (иначе lost ACK даст raw+replay дубль).
- Без ticket при недоступном IPC сохраняется прежняя live-доставка с явным
  replay_status=unavailable. Privacy/stale/policy denial подавляют доставку.
  Есть ticket, но commit не подтверждён: metadata-only gap hint и cursor repair.
- REST GET /v1/translation/replay читает тот же фиксированный cursor API через
  IPC с существующими auth/origin/privacy правилами; клиент хранит cursor.
  event_id=ticket позволяет отдельному WS клиенту сопоставить direct result
  и replay. Native панель продолжает canonical IPC путь.

Files: event_replay.py, service.py, новый translation_replay_ingress.py,
новый translation_replay_client.py, live_subs_service.py, rest_server.py,
TranslationStreamView.swift, зависимые protocol/live/REST/Swift тесты и IPC docs.

- [x] RED/GREEN pure journal: tickets/retry/expiry/cap/privacy/purge/mixedtail.
- [x] RED/GREEN auth, tokenfile, request/audit bounds, actual dispatcher.
- [x] RED/GREEN actual REST LiveSubsService worker -> IPC socket -> journal,
  LostACK/stale/purge/privacy/timeout, одна запись и нет echo/double render.
- [x] REST read endpoint: auth/origin/privacy, mixed cursor pagination.
- [x] Native component E2E reconnect catches REST-produced identical phrases.
- [x] Python3.12 noMLX perfile; dependent Swift/release jobs2; audit-all.
- [ ] Independent adversarial review exactSHA; update2119; terminalCI.

No runtime/model/keepalive changes. Heavy окна строго последовательно с VG/Main.

Design уточнение перед реализацией: WS authenticates и открывает epoch один раз
после config до первого server-side PCM ingest. Anchor неизменен до WS close;
begin_window(expected_epoch) выполняется в worker, но не может перепривязать
pending audio после purge. Initial open unavailable оставляет весь сеанс в
legacy-live режиме с explicit gap. После известного anchor любой ambiguous
begin/commit исход подавляет text и публикует только metadata gap; клиент
догружает возможный accepted commit по cursor. Ни новый ticket для retry старой
фразы, ни raw fallback после ambiguous commit не допускаются.


Checkpoint REST Python: 39 тестов core/ingress/client/worker/real Unix +5 endpoint
PASS на Python3.12 без MLX. Полная зависимая квалификация ещё впереди. Независимое
review выявило и закрыло lost-ACK/persistence counterexample: consumed ticket
резервирует seq под тем же lock до fallible disk I/O; retry не создаёт дубль.
Новые privacy-during-commit/redaction/actualWS/Swift tests ещё требуют RED/GREEN.

Cursor response: до480KiB сериализованных entries плюс bounded envelope,
клиент cap1MiB. Incremental has_more продолжает с последнего выданного seq.
Initial page — bounded latest tail с явным retention gap при ограничении байтами.
Одиночное legacy IPC событие больше480KiB заменяется redacted metadata marker,
явно отмечает gap и продвигает cursor; полного восстановления за пределами
этого контракта/retention не обещаем. Privacy/purge/restart имеют приоритет.

Review добавил отдельный non-default profile case: REST socket override не
доказывает data_dir владельца (CLI допускает отдельный --socket-path). Контракт
нового replay client: явный KRAB_EAR_IPC_DATA_DIR задаёт IPC profile/token directory,
KRAB_EAR_SOCKET независимо задаёт socket; без override default_socket_path(profile).
REST settings.DATA_DIR не считается IPC profile. Два synthetic profiles проверяются
настоящей factory, не только вручную сконструированным client.

Review closure: native WS unit fixtures explicitly replace replay factory, so
no default profile/token/socket is consulted. Nonprivacy begin/commit denials
(stale/busy/rejected/invalid/conflict/error) emit metadata-only SSE gap hints:
nonfinal ingest returns no direct result, so otherwise loss was silent.
Panel renders one bounded gap notice; privacy emits neither text nor hint.
RED: six Python cases missing hint; Swift panel missing stale notice.
29 dependent Python files passed serial Python3.12/noMLX before this narrow fix;
its GREEN and final Swift/release/audit qualification follow in next resource window.

Final local qualification: 29 dependent Python files serial noMLX PASS;
latest worker/WS isolation follow-up27 tests/3files PASS. Swift combined376 PASS,
then final strengthened Translation21 PASS; release jobs2 PASS171.43s.
Final make audit-all PASS (first RED uncovered the new auth credential;
explicit survivor policy matches event_bridge_token, no transcript content).
Independent reviewer approved token policy and three bounded warning categories;
frozen-SHA source gate and new hosted CI follow. No runtime/model operations.
