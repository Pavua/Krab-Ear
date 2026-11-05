# Admission lab v0: предлагаемый wire-контракт

Статус: локальный дизайн для совместного тестирования, не production API,
аутентификация, fence, capability, recovery или разрешение на переключение.
Owner запускается только harness в новом временном каталоге 0700; каждый запуск
создаёт новый тестовый журнал. Не подключать к работающим Ear/Main/Gateway.
До listen harness-only bootstrap переводит только что созданный synthetic
журнал из CLOSED в TEST_OPEN, как в его локальных тестах. Wire OPEN отсутствует;
переиспользование или recovery журнала для выдачи новых решений запрещено.

## Transport и валидация

Unix socket `<temp-0700>/echo.sock`. Одно соединение: клиент отправляет один
request frame и закрывает только свою сторону записи (write EOF); owner до
исполнения проверяет EOF и отсутствие дополнительных байтов, затем отправляет
не более одного response frame и закрывает соединение. Лишние байты или
неполученный EOF не допускают исполнение запроса. Клиент проверяет
EOF в общем deadline; дополнительные байты, отсутствие EOF, недоступность,
timeout или cancellation означают UNKNOWN, а не отказ операции. Lost ACK может
закрыть соединение вообще без ответа. Deadline и cleanup нельзя считать hard
bounded для произвольного cancellation-suppressing connector.

Frame: четыре байта unsigned big-endian длины, затем 1..65536 байт UTF-8 JSON.
Размер проверяется до выделения/чтения payload; truncated header/body, пустой
frame, некорректный UTF-8, surrogate, duplicate JSON keys, NaN/Infinity и
результат декодирования nonfinite (включая 1e309) запрещены. Корень — object;
неизвестные поля, операции и статусы запрещены. `v` строго целое 0, не bool.
Все идентификаторы, кроме grant_id и claim_id, — ASCII 1..128 символов из
`[A-Za-z0-9_.:-]`. grant_id и claim_id — canonical lowercase UUID. Все ответы повторяют
точные `v` и `request_id`; невалидный или неполный запрос можно просто закрыть
без ответа. Нет retry со стороны транспорта.

## Requests / responses

Общие поля request: `v`, `op`, `request_id`, `client_incarnation`.
Принципал фиксирован внутри тестового owner (`lab-client`), это не identity.

- `reserve`: дополнительно `kind` (`stt` или `tts`) и `executor`. Response:
  `{v, request_id, status, grant_id}`, status `reserved` (UUID), `denied`
  (null), `conflict` (null) либо `unavailable` (null).
- `claim`: дополнительно `grant_id`, `executor`. Response:
  `{v, request_id, status, claim_id}`, status `claimed` (UUID),
  `already_claimed` (тот же UUID при повторе того же committed request),
  `conflict` (null) либо `unavailable` (null). Это receipt; ни ответ, ни UUID
  не являются execution capability. Permit остаётся внутри owner и не передаётся.
- `status`: без дополнительных полей. Response:
  `{v, request_id, status: "ok", outstanding_count, claimed_count, unknown,
  drain_authority: false}`. Counts — неотрицательные int, не bool;
  `claimed_count ≤ outstanding_count`, потому что claimed входит в outstanding.
  `unknown` и `drain_authority` строго bool, второй всегда false. Всякий
  неучтённый/испорченный ответ — UNKNOWN на клиенте; `ok` не доказывает drain.
  `claimed_count` берётся owner из validated SQLite snapshot вместе с остальными
  полями, не из отправленных ACK или счётчика в памяти; весь снимок читается
  одним владельцем в одной транзакции. Status при ошибке журнала закрывает
  соединение без ответа.

Все вызовы журнала и validated snapshot выполняет один actor-thread, создавший
SQLite connection; socket handlers не обращаются к connection параллельно.
Необработанная SQLite-ошибка снимка завершает actor; незавершённый запрос и
все клиенты после этого видят UNKNOWN. Автоматического restart/reopen нет.

Ключ идемпотентности команд reserve/claim в журнале —
`(lab-client, client_incarnation, request_id)`; read-only status в ledger не
входит, его request_id используется только для корреляции ответа.
другая operation/payload с тем же ключом или новый claim для уже занятого
scope дают conflict, который не доказывает отсутствие иной удалённой работы.
`conflict` допустим только для подтверждённого JournalConflict с успешным
rollback. `unavailable` означает UNKNOWN: ошибка могла произойти после commit;
она не разрешает новый request_id, fallback или повтор выполнения.
Reserve replay возвращает исходный результат. Claim replay
возвращает исторический receipt, но не новый permit. История claimed и
outstanding не очищается клиентом; terminal API здесь нет. Никаких реальных
STT/TTS/provider side effects.

## Контрольный канал теста

Контрольные команды доступны только harness через унаследованные private pipes,
не через wire-клиент. Harness заранее выбирает request_id для задержки ACK;
owner после успешного вызова журнала (commit завершился) сообщает harness
`committed`, после чего ждёт отдельного release/drop. Тест убеждается, что
клиент получил timeout/cancel или соединение закрыто без ACK, затем отпускает
owner и повторяет тот же request. Replay должен вернуть тот же receipt без
второго permit; status сохраняет долг/UNKNOWN. Контрольный канал и ошибки его
очистки — часть тестового fixture и не являются интерфейсом продукта.
Одинакового claim_id и счётчиков недостаточно для доказательства отсутствия
второго permit: owner отправляет только private harness свидетельство
`permit_present` для первой и повторной попытки (ожидаются true, затем false),
не сериализуя сам permit и не помещая это свидетельство в wire-ответ.
Replay здесь выполняется при том же процессе owner, журнале, controller epoch
и client_incarnation после разрыва сокета. После перезапуска owner replay не
разрешён: старый журнал только для inspection, новый запуск создаёт новый.
Endpoint принадлежит одному запуску, без rebinding/reuse; этот тест не
показывает recovery или дедупликацию между запусками.

Все названия и сценарии требуют независимого review и проверки совместимости
Gateway до freeze; опубликованный статус и production authority отсутствуют.
