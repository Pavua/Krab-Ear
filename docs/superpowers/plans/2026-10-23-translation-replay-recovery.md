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
