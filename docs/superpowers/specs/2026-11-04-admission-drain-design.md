# Ear: допуск и завершение работ перед переключением

Статус: рабочая спецификация. Критические вопросы протокола остаются открытыми; не implementation GO и не live GO.
База исходников: 994cb4f8cbb61932c637f624782a93adcd60cab7.

## Подтверждённый дефект эксплуатационного контракта

Последовательность «idle snapshot → остановка» не резервирует окно: Main или
другой клиент может начать новую операцию после проверки. Backend и REST —
разные процессы. Допуск должен оставаться закрытым при смене обоих процессов.
Сам по себе health200, expired lease, отсутствие handlers или count0 не доказывает
завершение вложенной работы. Полное молчание облачных чатов не является целью.

## Что уже есть — использовать, не переписывать

- backend/ipc_server.py:102 stop(): терминальный _stop_event закрывает новые
  connections, registry handler threads ждётся в общем deadline. Это shutdown,
  не обратимый maintenance. Request timeout не означает завершение callable.
- backend/service.py:240 _shutdown_backend(): CallSTT begin_shutdown → IPC drain
  → service.close workers → metadata; неуспех ownership-barrier запрещает
  последующие стадии. Это нельзя переименовать в pre-cutover drain receipt.
- backend/rest_inprocess.py: begin_shutdown и drain hooks существуют для
  in-process REST; production использует отдельный REST и этот путь не покрыт.
- backend/activity_diagnostics.py: owner_epoch/owner_pid, component snapshots,
  ActivityReservations. coverage.complete=false намеренно; native STT scope
  начинается после single-flight и не охватывает background warmup/batch/
  other admission/nested adapter futures/WS translation emit.
- backend/rest_server.py:2011+ native request reserves ActivityReservations
  token и передаёт ownership actual Future; завершение HTTP/timeout/cancel
  не даёт права освободить ещё живую работу. TTS и WS — отдельные входы.
- recording/meeting generation tokens и finalizing ownership уже существуют:
  запись живёт дольше start RPC, значит request-scoped counter недостаточен.
- MemoryConductor имеет атомарный stop/admission для собственных workers,
  но не заменяет общий backend/REST barrier.

## Реальные альтернативы

1. Проверяемая пауза конкретных Main → Ear отправителей. Самый узкий вариант
   при наличии существующего owner mechanism. Сейчас не найден; независимые
   MCP, voicenotes и другие клиенты не перекрываются одним флагом Main.
2. Общий Ear-owned maintenance protocol на IPC и отдельном REST. Рекомендация
   для постоянного решения: владелец ресурса сам запрещает новые операции,
   сообщает о принятых/ожидающих/неясных работах и разрешает cutover только
   после реального завершения. Больше изменения и E2E, зато не нужна остановка
   всех облачных чатов и нельзя обойти паузу другим клиентским транспортом.
3. Разовый legacy cutover с явно принятым владельцем риском открытого входа.
   Уже подготовлен и проверен, но источник гонки не исправляет. Владелец выбрал сначала проверить защиту; автоматически этот риск не принимаем.

## Инварианты рекомендуемого решения

- Операция начала maintenance и admission каждой новой работы сериализованы.
  Admission получает owner token ДО очереди/потока/Future/вложенного submit;
  потеря coverage или неизвестный owner всегда запрещают drain receipt.
- Общая generation/идентичность maintenance охватывает backend и REST; receipt
  содержит точные process epochs, coverage, pending/running/unknown и не может
  применяться после restart/ABA. Два последовательных нуля не транзакция.
- Закрытый допуск и состояние восстановления переживают рестарт обоих
  процессов. Crash координатора/истечение TTL не открывают вход автоматически.
  PID/lease TTL сами по себе не доказывают exclusivity или завершение.
- Новые work-creating вызовы получают понятный retriable maintenance result;
  authenticated status и owner recovery доступны без создания ML/worker.
  Неизвестные методы/новые routes не становятся обходом по умолчанию.
- Уже начатая диктовка не отменяется. При закрытом допуске разрешены finish/stop
  именно принятой generation; token живёт до finalization/history tail.
- Не ждать drain из обработчика, который сам считается незавершённой работой.
  Запуск maintenance и чтение status отделены от запроса блокирующего ожидания.
- Вложенные Futures, фоновые startup warmups, batch, TTS и WebSocket emit
  имеют явного owner до actual terminal completion. Timeout/cancel caller
  не освобождают token работающего child. Нужна матрица всех ingress paths.
- Privacy/auth проверяются до управляющих действий; IPC local control также
  требует подходящей аутентификации/защиты. В status нет текстов/секретов.
- Повтор begin/status/resume идемпотентен; чужой/устаревший resume отклоняется.
  Нельзя вернуть старый marker из backup поверх новой активной generation.
- Watchdog/supervisors: maintenance должен быть известен lifecycle owner;
  HTTP maintenance не должен инициировать kickstart. Текущая точечная пауза
  внешнего watchdog — отдельный operational контроль, не программный guard.

## Вопросы, которые дизайн обязан решить до кода

Где хранится durable состояние и как обеспечивается его atomicity между
процессами; кто аутентифицирует coordinator; как учитываются остальные
entrypoints/фоновые owners; какие finish/status методы разрешены; точный
crash recovery и migration/bootstrap старого runtime без нового guard;
какой compatibility ответ ожидают Main/Gateway/Swift. Это предложения
исполнителя, а не уже согласованные решения пользователя.

## Причинные проверки RED → GREEN

| Сценарий | Требуемое доказательство |
| --- | --- |
| begin против admission | Реальные два процесса/барьеры: поздний запрос не получает token и не стартует ML |
| accepted до begin | Drain остаётся false до фактического terminal этого owner |
| очередь/single-flight | Ожидающая работа учтена до входа в native callable |
| HTTP timeout/cancel | Caller завершён, child жив: drain остаётся false |
| вложенный submit | Внешний Future завершён раньше child: child продолжает держать ownership |
| запись между RPC | Start RPC ушёл, запись/финализация активна: drain false, правильный stop разрешён |
| REST и IPC одновременно | Закрытие охватывает обе реальные server entrypoints, не только mocks класса |
| batch/TTS/WS/warmup | Ни один путь не получает ложный zero/complete receipt |
| crash/restart/TTL | Убийство тестового coordinator не открывает ingress; новое epoch инвалидирует receipt |
| malformed/auth/privacy | Нет побочных действий/секретов, fail-closed status |
| stale/foreign resume | Не открывает чужую generation и не подменяет marker |
| unknown/coverage loss | Нет разрешения cutover при неясной живой работе |
| actual wiring | Реальные Unix socket + Flask HTTP с synthetic leaf; не запускать production ASR |
| compatibility | Main/Gateway/Swift корректно обрабатывают retriable maintenance без unsafe fallback |

Это новый межпроцессный контракт: unit tests не доказывают wiring. До реализации
нужен выбранный подход, завершённый дизайн, independent adversarial review;
после реализации — Python3.12/noMLX +3.14, audit-all, точное CI и отдельный
применимый E2E. До этого текущий source/runtime barrier не объявляется готовым.

## Обязательный отдельный bootstrap gate (уточнение Gateway review)

Новый guard защищает только процессы, в которые он уже загружен. Первый
переход со старого runtime не получает этой гарантии задним числом. Поэтому
source implementation и её зелёные тесты НЕ снимают текущий release HOLD.
До первого включения требуется независимо доказанный migration protocol:
проверенная внешняя пауза фактических ingress путей/отправителей с завершением
ранее принятой работы либо отдельное явное принятие владельцем конкретного
legacy риска данного перехода. Тот же bootstrap gate применим к rollback
на старую версию без guard. Нельзя обещать безопасное текущее окно только
на основании будущего исправления. Точные first-install/rollback процедуры
должны получить owner-review отдельно от steady-state протокола.

## Дополнение после независимого adversarial review

Вердикт: исследование пригодно как основа следующего draft; для утверждения
дизайна, реализации и live cutover — HOLD. Эти замечания нельзя считать уже
выполненными гарантиями.

### Проверенный reuse inventory

В Git доступен локальный коммит `fae4ec51bcd0f4d1db78cc8fe5fb020ace127964`,
которого нет в базе 994. Его `core/operation_ownership.py` регистрирует
корень/детей до executor submit и отслеживает actual Future identity, seal,
unknown, epoch и capacity. При overflow работа продолжается, coverage_lost
фиксируется; admission всегда открыт; terminal nodes не компактизируются.
Его integration hook охватывает только REST native POST и отдельный adapter
submit в engine. Явно исключены RU IPC, WebSocket, TTS, batch, UI, startup
warmup и вложенные пулы, включая GigaAM stuck future. После fork coverage
теряется; это не межпроцессная fencing authority.

Коммит `139d228f` добавляет композиционный тест error 503: настоящий Flask
POST и настоящий nested executor, синтетические ASR/normalization leaves.
Он проверяет, что root остаётся running, когда HTTP уже вернул ошибку, а
child ещё исполняется. Этот старый тест — ценный источник сценария, а не
подтверждение работы нового runtime или полный drain. Перенос требует проверки
совместимости с 994, существующим REST error fix и новыми причинными тестами;
автоматический cherry-pick или предположение о готовом барьере недопустимы.

### Обязательные design cases

1. RU `voice_gateway_call`: REST принимает root до закрытия ingress, затем
   `_transcribe_call_ru` отправляет IPC owner после закрытия. Нужен проверяемый
   capability handoff именно принятого root, без допуска чужой новой работы.
   Потеря ответа или timeout HTTP не освобождают фактически живой CallSTT.
2. IPC connection уже принята до begin, payload пришёл после; допуск проверяется
   в соответствующей точке dispatch. Registry handler threads недостаточен:
   `_call_handle_request_bounded` при timeout возвращает управление и вызывает
   `shutdown(wait=False)`, а worker ещё может работать.
3. WebSocket уже открыт: новые frames, включая native и cloud branches, нельзя
   пропустить через gate только из-за прежнего handshake; завершение допустимой
   ранее работы, `end` и cleanup должны иметь отдельные явные правила.
4. Полный список внутренних producers и точек до spawn: startup GigaAM,
   wake-word detection, schedulers, live-subs и batch callbacks, settings/model
   reload. Durable закрытие должно действовать до конструкторов/startup threads.
5. Crash владельца при живом subprocess/вложенном Future, потеря registry,
   частичный restart backend/REST: новый epoch или отсутствие старого PID не
   является completion receipt. Без достоверного reconciliation — UNKNOWN/HOLD.
6. Canonical shared control-state, безопасное разрешение пути, fencing и
   atomicity нужно определить явно: REST settings.DATA_DIR может отличаться от
   backend default/CLI data dir; совпадение каталогов нельзя предполагать.
7. Частично записанный, повреждённый, чужой или устаревший maintenance state
   и ошибки хранения/аутентификации должны закрывать допуск без стирания debt.

### Дополнительные причинные проверки

| Сценарий | Ожидаемое наблюдение |
| --- | --- |
| REST→RU IPC handoff пересекает begin | Только заранее принятый root может продолжить с проверяемой связью; чужой IPC отклонён |
| Timeout IPC handler, worker жив | Drain остаётся false/unknown до фактического завершения worker |
| Принятое IPC соединение без payload до begin | Поздний payload не создаёт новую работу через старое соединение |
| Открытый WS с native/cloud frames | Новая работа после begin отклоняется, разрешённые cleanup/end не теряются |
| Crash owner при живом child | Ни новый epoch, ни restart пары не объявляют orphan завершённым |
| Ошибка/частичный state file и несовпадающие data dirs | No grant; обе стороны обнаруживают один проверенный canonical state или HOLD |
| Startup после persisted maintenance | Ни GigaAM, ни прочие producers не запускают новую работу до восстановления |
| Повторный первый install и rollback на старый runtime | Нужен отдельно проверенный внешний admission; steady-state unit tests его не доказывают |

Оба реальных процесса и транзит RU проверяются с synthetic audio/leaf и
действительными socket/HTTP механизмами, без production inference. Matrix
перед реализацией нуждается в независимом source и race review.

## Межпроектное условие: отклонение не должно запускать fallback

По проверке владельца Main production Telegram MCP `telegram_transcribe_voice`
при IPC `ok=false` либо недоступности сокета возвращает None и запускает
`_transcribe_mlx` через executor. Поэтому простой отказ Ear в maintenance
может вызвать новый локальный ML worker и НЕ закрывает общий ресурсный admission.
Контракт требует явной machine-readable причины maintenance, client-side gate
перед любым fallback и проверки пути недоступного сокета при первом переходе.
REST voice_notes/VideoStudio и synthesize_speech/cloud-TTS имеют различную
семантику отказа: owner Main должен дать source+E2E receipts этих путей; Ear
не объявляет их закрытыми. Gateway отдельно проверяет свои реакции на 503.

Тесты: намеренная maintenance ошибка IPC и отказ соединения во время
авторизованной паузы не создают ML executor; обычная непредвиденная ошибка
сохраняет прежнюю семантику только если она совместима с текущим gate;
сбой чтения maintenance state даёт UNKNOWN/HOLD без fail-open fallback.
Ответственность между проектами определяется отдельными owner-reviewed
контрактами, изменение чужих файлов в этой ветке не входит в scope.

Чтение marker перед fallback не является admission: между read OPEN и
executor.submit может начаться maintenance (подтверждённый Main TOCTOU).
Требуется общая атомарная регистрация владения до fallback submit и drain
фактического worker, либо отдельно квалифицированный запрет fallback на всё
время жизни обновлённого процесса; выбранный механизм должен охватывать уже
принятые и подвисшие workers. Проверка: синхронный барьер между OPEN read и
submit, параллельный begin; работа либо зарегистрирована и удерживает drain,
либо не стартует. Первая миграция legacy-клиента остаётся открытой задачей.

`backend/call_stt_client._decode_response` сейчас различает policy rejection
только для unauthorized/rate_limit_exceeded; иная ошибка становится protocol
error, REST отдаёт обычный 503. Нельзя терять `maintenance_active` при RU
REST→IPC→REST: Gateway ранее трактовал обычный 503 как повод fallback.
Сквозной machine-readable code и no-fallback поведение требуют совместимых
изменений Ear/Gateway, теста real sockets/HTTP и межпроектного review.

## Предлагаемый wire contract для совместных тестов (draft)

- IPC после обязательной авторизации: `{id:<request-id>,ok:false,
  error:{code:"maintenance_active",message:<safe-text>}}`; request ID
  отражает уже существующий envelope. Нельзя раскрывать payload.
- HTTP отказ нового work-creating запроса: статус 503, JSON
  `{status:"error",error:"maintenance_active",text:""}`, действующий
  request-id header сохраняется. Бытовой `stt_failed` 503 остаётся отдельно.
- При maintenance клиенты не запускают автоматически cloud/faster-whisper/
  локальный MLX/TTS fallback; privacy/authorization gates и их приоритет
  задаются source-контрактом, не превращаются в retryable maintenance.
- Атомарный admission нужен до запуска любой первичной или резервной работы,
  в том числе когда Gateway сразу выбирает испанский cloud STT. Уже принятая
  работа остаётся в drain до фактического завершения своего дерева.
- Transport unavailable без доступного авторитетного control-state — UNKNOWN,
  не эквивалент OPEN. Marker существует и надёжно проверяется через переключение;
  это не подтверждает, что unmodified legacy клиенты умеют его учитывать.

Это предложение владельцам Main/Gateway на совместное review, не утверждённый
API. Требуются тесты: явный maintenance через RU REST→IPC→REST не деградирует
в обычный 503; обычный stt_failed 503 сохраняет согласованное поведение;
privacy 403 сохраняет приоритет/no-fallback; socket unavailable с неопределённым
marker не стартует fallback; каждый primary/fallback и accepted-before-begin
проверяется реальной конкурентной проверкой admission, не одним чтением файла.

## Вариант архитектуры после предварительного Astra/Ultra review

Рекомендован отдельный lightweight control owner с durable journal, не
зависящий от перезапускаемых процессов Ear backend/REST. Это проектное
направление, а не свидетельство реализованной защиты. Общий read-only marker
сам по себе не атомарен с executor.submit; многопроцессные file writers
потребовали бы одинакового fencing/protocol у всех Main/Gateway/MCP.

В control owner начало maintenance и выдача root grant сериализуются.
Grant записан устойчиво ДО положительного ACK и удерживается до проверенного
terminal ownership; потерянный ответ, падение owner и неизвестный grant
превращаются в UNKNOWN/HOLD. TTL или смерть coordinator не стирают долг.
Контракт обязан различать state unknown и открытый допуск. Внешняя проверка
marker не является альтернативой операции atomic acquire.

Черновые операции для проработки: `register_owner(role, epoch, protocol)`,
`acquire_root(owner_session, kind, request_key)`,
`begin_maintenance(admin, expected_revision, idempotency_key)`,
`mark_terminal(grant, owner_epoch, terminal_kind)`, `status(generation)`,
`resume(admin, expected_generation, expected_revision)`. Аутентификация,
fencing incarnations, журнал и idempotency должны быть определены и проверены
до production wiring. Нет прав доступа по одному предъявленному UUID.

Первый малый source milestone: stdlib state machine + durable store в
изолированном окружении, никакого daemon/IPC/REST/ML и всегда
`drain_authority=false`. Он не изменяет runtime и не доказывает право на
выпуск. Его тесты должны проверять сериализацию реальных процессов, durable
write-before-ACK, crash/reopen, duplicate/lost reply, stale epoch/generation,
повреждение/отсутствие state и сохранность unknown. Начальная инициализация
и безопасный resume должны быть явно различимы; оба требуют отдельного review.

Следующий milestone: `reserve_handoff(parent,target_role)` до IPC/send и
`claim_handoff(cap,target_owner_epoch)` до enqueue, single-use и связь
child→parent до actual completion. При потере ACK нельзя автоматически
повторить inference. Эти методы ещё не утверждены и не реализованы. После
этого необходимы раздельные adapters к реальным потокам, per-owner tracking,
межпроектный контракт, positive/negative E2E и отдельный bootstrap/rollback.

### Граница экспериментального SQLite namespace

Текущий изолированный прототип не является daemon или authority. Он открывает
журнал только в каталоге владельца с mode `0700` и удерживает исключительный
`flock` на обычном приватном `.maintenance.lock` в течение жизни SQLite
соединения. Восстановление существующего состояния не создаёт отсутствующий
lock. Неуспешное закрытие соединения не должно освобождать lock; прерывание
до передачи владения должно освобождать полученные файловые дескрипторы.

Безопасность схемы предполагает заранее подготовленный доверенный и стабильный
namespace, включая родительские каталоги пути, а также участие всех писателей
в одном протоколе: lock нельзя удалять или заменять, пока возможны владельцы.
Проверка `0700` у конечного каталога сама по себе не доказывает стабильность
ancestors. Произвольный процесс с тем же UID, игнорирующий протокол и
подменяющий путь, этой схемой не сдерживается; SQLite открывает файл по имени,
а не по проверенному файловому дескриптору. Указанная граница и конкретный путь
ещё требуют отдельного owner review до production. Lock также не является
crash-recovery, учётом работ или доказательством завершения inference; статус
остаётся `drain_authority=false`.

Если `close` файлового дескриптора вернул неоднозначный результат, исходный
`flock` мог уже освободиться. Прототип исключает повторное закрытие числового
fd и сохраняет UNKNOWN в текущем процессе, но это **не** межпроцессный fence:
другой процесс может получить lock. Реальный control owner обязан отдельно
решить этот случай и доказать fencing/recovery до любых production grants или
receipt. Сам по себе локальный объект HOLD не обеспечивает защиту после crash.
