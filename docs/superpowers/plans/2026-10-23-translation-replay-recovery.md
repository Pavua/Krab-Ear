# Translation: reconnect и догрузка пропущенных фраз

**Goal:** сохранять последние 200 показанных фраз и догружать пропущенные
при разрыве SSE, не теряя легитимные повторы одинакового текста.

**Base:** origin/codex/krab-ear-v2 через integration candidate05220ff2.
До публикации требуется принятая интеграционная база и точный CI.

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
- [ ] Тесты зависимых Python/Swift файлов, Ubuntu parity, release jobs2.
- [ ] Независимый whole-diff review (privacy/read-path включён).
- [ ] PR, точный CI; source/CI не выдавать за production memory acceptance.

При исчерпании retention или перезапуске сохраняем показанные фразы,
показываем явный разрыв истории и догружаем доступные записи.
Недоступность IPC не двигает cursor и не очищает панель; повтор ограничен timer.

## Уточнения после RED/совместного GREEN

- Initial/SSE-reset hints могут опережать EOF: фикстуры меняют набор фраз
  по фактической фазе соединения, а не по номеру IPCвызова.
- Узкий live_subs cursor-read использует отдельный light-бюджет120/мин.
  Legacy debug-read и курсоры остальных типов остаются medium30/мин.
- Privacy и purge имеют приоритет над сохранением показанных фраз:
  ответ содержит privacy_mode и reset_reason (restart/clear); очистка
  журнала должна очищать UIcache, а restart сохраняет предыдущие строки.
- Backend snapshot invalidated by concurrent purge не возвращает payload.

Generation хранится только как неперсональный 32байтный marker через существующий
core.atomic_io.atomic_write_text. Он сохраняется через restart и меняется при
clear; UI очищает cache при смене поколения. Неизвестное поколение => failclosed
при новом epoch. Ошибка записи marker не мешает wipe RAM, но возвращает ошибку
очистки; отсутствие stdout/exit0 не считается durable успехом.

Проверенный checkpoint перед whole-diff review: Swift367 общего набора PASS;
после privacy дополнения15narrowPASS; release до privacyPASS. Новые14Python
cursor/dispatch/privacy/IOfailuretestsPASS,старыеreplay/privacy/validationPASS,
flake8PASS,makeaudit-allPASS. Финальныеcombined370/release/Ubuntuparity ещё
не выполнены на текущем tree; CI/merge/runtime этим checkpoint не подтверждены.
