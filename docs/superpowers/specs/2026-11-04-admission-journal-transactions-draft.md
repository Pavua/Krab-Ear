# Экспериментальный журнал резервирований: уточнённый контракт

Статус: source-only draft после независимого архитектурного review; не production
admission, не запуск работы, не выполнение cutover. База: `1a9ddc78`.
Источником общих требований остаётся `2026-11-04-admission-drain-design.md`.

## Граница и жизненный цикл

Один синтетический serializing owner единолично владеет SQLite и lifetime-lock.
Эксперимент не реализует daemon, транспорт, auth, реальные grant capabilities,
claim, запуск/завершение работ, очистку долга, production OPEN или drain.
Все ответы содержат `drain_authority=false` и не дают права на execution.

Явный `initialize_new` создаёт `ACTIVE_FRESH` с `CLOSED`; `open_existing` при
`CLOSED` возвращает `INSPECTION_ONLY`, где запись команд и grants запрещена.
Persisted `TEST_OPEN` отклоняется как `RECOVERY_REQUIRED`; данные сохраняются.
Ни повторное открытие, ни новый PID/epoch не активируют owner. Смена схемы
требует новой версии без молчаливой миграции старой базы.

Только тестовая fixture за пределами production module может прямым SQL изменить
состояние на `TEST_OPEN` в уже созданном свежем временном store. В продукте
нет флага, environment-переключателя, helper или callback для такого перехода.
Fixture не работает с production путями или IPC/REST. Отсутствие runtime wiring
и импортов экспериментального модуля проверяется отдельно.

## Данные и ограничения

Предлагается schema version 2 с двумя таблицами:

- `control`: ровно одна строка: версия, installation ID, journal UUID,
  controller epoch, control revision, `CLOSED`/`TEST_OPEN`.
- `command_ledger`: sequence PK; principal ID, client incarnation, request key;
  operation и канонические параметры; неизменяемый outcome; nullable unique
  grant ID; controller epoch и revision решения. UNIQUE по тройке
  `(principal_id, client_incarnation, request_key)`; operation и ожидаемый
  controller epoch входят в неизменяемый payload, а не в область ключа.
  CHECK связывает grant ID исключительно с положительным acquire.

Principal пока доверенный synthetic input, не транспортная идентификация.
Каждый grant — только запись резервирования и outstanding долг; даже после
перезапуска/смены epoch он остаётся видимым. Нет RUNNING, TERMINAL, claim,
удаления или отдельной mutable таблицы grants. Параметры не содержат аудио,
тексты диктовки, телефоны или секреты.

Для эксперимента предлагаются границы: request key до 128 ASCII-символов,
канонические параметры до 2 KiB, фиксированные enum операций и synthetic
kinds, не более 1024 записей. Нужны точные validation tests для этих границ.
Переполнение или ошибка доступа дают UNKNOWN/HOLD без удаления записей.

## Атомарность и результаты

Новые записи сериализуются одним owner и выполняются в короткой SQLite
`BEGIN IMMEDIATE` транзакции. Решение, соответствующий grant ID и запись
идемпотентности фиксируются вместе. Положительный ACK **записи** возможен
только после подтверждённого commit; он не означает execution authority.

| Событие | Требуемый результат |
| --- | --- |
| Новый acquire в TEST_OPEN | Одно durable положительное решение с одним grant ID; revision не меняется |
| Новый acquire в CLOSED | Durable `DENIED_CLOSED`, grant отсутствует |
| Begin в TEST_OPEN при совпадении expected revision | В одной транзакции CLOSED, revision+1 и решение; старые долги сохраняются |
| Begin в CLOSED при совпадении revision | Durable `ALREADY_CLOSED`; это не drain receipt |
| Повтор той же области key и payload | Прежнее историческое решение; не новая capability и не повторный запуск |
| Тот же key с другим payload, operation или epoch | Конфликт без изменения первоначальной записи |
| Новый begin с неверной revision | Конфликт, без закрытия по чужой revision |
| Storage/lookup/commit ошибка или неизвестный результат | UNKNOWN и отказ последующих мутаций текущим owner, без ложного ACK |

Lookup существующей записи выполняется **до** проверки expected revision: иначе
повтор уже выполненного begin после увеличения revision ошибочно откажет.
`control_revision` меняется только при переходе TEST_OPEN→CLOSED; acquire не
меняет его. DENIED_CLOSED — устойчивое решение, а storage UNKNOWN не
записывается как обычный отказ.

При исключении commit rollback можно попытаться выполнить, но не считать его
доказанным: commit мог фактически состояться. Текущий объект становится
UNKNOWN и больше не мутирует state. После confirmed commit и потери ACK retry
в том же активном owner возвращает записанную историю. Безопасность restart
обеспечивается отсутствием auto-activation, а не обещанием durable poison,
который процесс мог не успеть записать перед crash.

После неоднозначного `close` системный flock мог уже освободиться: локальный
UNKNOWN не является межпроцессным fence и не переживает crash как доказательство
безопасности. Production transport/auth, owner fencing и recovery потребуют
отдельного решения и независимого review.

## Синтетические проверки следующего среза

- Оба порядка acquire/begin через один sandbox-only owner и два клиента,
  управляемые барьерами: ledger, revision и количество долгов соответствуют
  транзакционному порядку. Клиенты не открывают SQLite напрямую.
- Confirmed commit → потерянный ACK → повтор возвращает один исход и grant;
  replay после begin остаётся историей, execution callback отсутствует.
- Replay begin проверяется до CAS; другой payload/operation/epoch конфликтует.
- Fault injection до записи, до commit и после реального commit с последующим
  исключением: нет разрешающего ACK, sticky UNKNOWN, сохранившаяся запись не
  стирается. Проверить отдельно устойчивый DENIED_CLOSED и transient UNKNOWN.
- Crash только принадлежащего тесту owner до/после commit: все подтверждённые
  записи сохранены; TEST_OPEN reject, CLOSED остаётся inspection-only.
- Проверки размеров/capacity, corrupt/missing store и ошибок commit без очистки,
  авто-создания, открытия допуска или drain receipt.
- Fixture доступна исключительно из тестов; runtime code не импортирует основу.
  Тест неоднозначного close не выдаёт локальный UNKNOWN за OS fence.

Для настоящего использования дополнительно потребуются claim/terminal и учёт
реального дерева работ, аутентификация, защищённое восстановление и fencing,
реальные IPC/REST/Main/Gateway adapters, no-fallback, E2E и проверенный отдельный
legacy bootstrap/rollback. Ни один зелёный тест данного среза их не заменяет.
