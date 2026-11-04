# Черновик: claim, фактическая работа и потомки

Статус: исследование следующего этапа, не утверждённый контракт и не разрешение
на runtime, admission, выполнение или drain. Предыдущий source-only journal:
`9f9dc1d7`; его ответ не является execution capability.

## Подтверждённая локальная основа

Локальный коммит `fae4ec51bcd0f4d1db78cc8fe5fb020ace127964` содержит
`OwnedOperations` и integration hook только для REST native POST → adapter.
Он регистрирует child до `executor.submit`, связывает actual Future, помнит
sealed и unknown и не объявляет полноту coverage. Он не находится в текущей
базе и сам по себе не является durable журналом или общим admission fence.

Существенные ограничения по исходнику этого коммита: при capacity overflow или
неизвестном parent обычный вызов продолжается без token и ставит coverage_lost;
для настоящего admission такое fail-open поведение недостаточно. Fork создаёт
новый epoch и coverage_lost, но не доказывает завершение старых потомков.
Scope явно исключает RU IPC, WebSocket, TTS, batch, внутренний warmup и вложенные
пулы адаптера, включая GigaAM stuck-future. Терминальное состояние callable или
Future не равно завершению неперечисленных descendant процессов. Старый код
следует повторно использовать или развить после проверки совместимости, а не
предполагать готовым barrier.

## Рабочая гипотеза границ

1. Durable reserve фиксирует только долг и не разрешает запуск. Следующий
   single-use claim должен быть связан с конкретным grant, авторизованным
   owner incarnation и request key; точная аутентификация и fencing не
   определены. Нельзя считать случайный UUID доказательством личности.
2. Переход к claimed/starting записывается до возможного запуска работы. Потеря
   claim ACK или crash после commit до запуска оставляет UNKNOWN/HOLD; replay
   возвращает историю, а не повторную capability. Без доказанной идемпотентности
   side effect невозможно обещать exactly-once запуск через произвольный сбой.
3. Ownership до фактического `submit` резервирует допустимые потомки; исключение
   submit может означать, что worker уже запущен, и не освобождает долг. Даже
   при timeout/cancel вызывающего потока завершение определяется фактическим
   worker/Future и всеми зарегистрированными потомками.
4. Уже принятый root может потребовать REST→IPC handoff после begin. Должны быть
   отдельно проверены заранее зарегистрированное право handoff, связь с root,
   single-use claim target owner, seal и недопущение новых произвольных roots.
   Потеря ответа handoff не разрешает повторно запускать inference.
5. Terminal должен сообщаться только владельцем с проверяемой идентичностью и
   подтверждением завершения всего охваченного дерева; чужой/stale terminal,
   отсутствие PID, истечение TTL и смерть координатора не доказывают completion.
   Неизвестные descendants, пропущенный ingress и потеря coverage остаются HOLD.
6. Реальный startup, authentication, fencing даже при неоднозначном close lock,
   обнаружение orphan процессов и legacy bootstrap пока не имеют доказанного
   решения. Локальные synthetic тесты не дают production drain authority.

## Вопросы до кода

- Где сериализуется single-use claim, что именно клиент получает при потере ACK,
  как исключается переотправка side effect и какие результаты могут быть
  доказаны без атомарной транзакции с внешним исполнителем?
- Как фиксируется переход от зарегистрированной работы к реальному executor,
  если submit вызвал ошибку после запуска worker, и как хранится unknown после
  crash без ложного release?
- Как выдать ограниченный handoff root→target, проверить peer identity и
  привязать его к конкретному owner incarnation без replay чужого token?
- Как сочетаются подтверждённое завершение поддерева и seal, когда callback,
  дальнейший submit или отложенный IPC могут появиться одновременно?
- Кто и как квалифицирует все producers (Main MCP/Perceptor, Gateway, REST,
  IPC, TTS, WS, batch, startup) и первый переход legacy клиентов без нового gate?

## Обязательные synthetic контрпримеры

- Два конкурентных claim одного grant, включая потерянный ACK: ни один повтор
  не приводит ко второму start; при неоднозначности удерживается долг.
- Crash владельца до/после durable claim, до и после реального submit; новый
  owner не обнуляет долг и не делает незаслуженный retry.
- submit фактически начинает child и затем выбрасывает; parent Future/HTTP
  завершается раньше child; UNKNOWN/drain остаются до надёжного reconciliation.
- begin между reserve и IPC handoff: принятому поддереву разрешены только
  заранее оформленные продолжения, чужие и новые roots отвергаются.
- Повторный, чужой или устаревший terminal и late child после seal не снимают
  долг и не приводят к ложному complete.
- Exhausted capacity, нераспознанный child, fork/crash/propagation loss и
  недоступный authority не приводят к fallback или положительному receipt.

Во всех новых тестах должны использоваться изолированные synthetic файлы и
процессы, без production моделей/сокетов. Тест будущего межпроцессного
контракта потребует настоящий transport и отдельно проверенную идентификацию;
mocked in-process model не заменяет эту проверку.

## Предварительная рекомендация независимого review

Первый проверяемый срез сузить до durable claim, strict submit-once и terminal
одного synthetic leaf/executor без REST/IPC и descendants. Между подтверждённым
claim и submit допустим исход «ноль запусков и UNKNOWN»; повторный start без
доказательства запрещён. Это at-most-once только для покрытого synthetic
механизма, не общая гарантия exactly-once side effects.

SQLite остаётся у одного actor thread. Future callback не использует SQLite
напрямую: он передаёт ограниченное сообщение owner, и потеря/переполнение
сообщения сохраняет UNKNOWN/HOLD. Terminal подтверждает только конкретный
связанный Future и этот leaf, не дочерний процесс и не глобальный drain.

В `fae4ec51` существующие `run(None)`, повторный/stale run, overflow и submit
без propagated context всё ещё вызывают исходную функцию. Для strict path они
должны отказывать до запуска; старый fail-open metadata path нельзя выдавать за
новую admission гарантию. Вопрос reuse между совместимыми режимами требует
отдельной проверки, чтобы не менять неявно поведение production.

До реализации должны быть определены: область idempotency claim и связь с owner
incarnation; различие свежего claim и исторического ответа; доказательство
отмены до начала; критерий terminal и exact Future identity; schema-v3 без
неявной миграции. Следующим самостоятельным срезом остаются заранее
зарегистрированный child/handoff до отправки, target claim и parent seal.

## Кандидат механики для отдельного review

Ниже — гипотеза, не реализованный интерфейс и не security boundary:

- Отдельная schema v3 без автоматической миграции v2 добавляет durable
  единственную строку claim на grant и команды в общем idempotency ledger.
  Claim payload связывает grant, synthetic owner incarnation и claim-key;
  противоречивый повтор не создаёт вторую запись. Semantic constraints и
  crash/reopen проверяются так же строго, как для v2.
- Только подтверждённый **fresh** claim, возникший в текущем synthetic owner,
  может выдать локальному тестовому executor одноразовый объект start permit.
  Replay/повтор после потери ACK возвращает историческое решение без permit.
  Permit помечается использованным под локальным lock до попытки submit;
  повторное использование, неверная identity и отсутствие permit отказывают
  до вызова функции. Нельзя считать такой объект защитой от произвольного
  same-UID кода, межпроцессной аутентификацией или production capability.
- Исключение submit после использования permit всегда оставляет UNKNOWN: worker
  мог уже начать выполнение. Отмена считается доказанной до старта только если
  exact bound Future действительно terminal/cancelled и wrapper под общим lock
  ни разу не вошёл в пользовательскую функцию. Такая проверка применима только
  к точно определённому типу synthetic executor/Future.
- Для terminal возможны два независимых подтверждения: wrapper отработал свой
  finally и тот же привязанный Future terminal; порядок их наступления может
  быть любым. Копии, stale incarnation, двойной callback или чужой Future не
  освобождают долг. Очередь события к actor bounded; потеря сообщения не должна
  выглядеть как успешное завершение и оставляет локальный UNKNOWN.
- Доказательство после crash не появляется из локального Future: новый owner
  остаётся inspection-only/UNKNOWN. Никакая из этих операций не даёт глобальный
  drain, пока нет transport auth, квалифицированных descendants и bootstrap.

Разобрать race между очень быстрым completion и привязкой Future, блокировку
при отмене, переполнением очереди, синхронным add_done_callback и передачей
состояния actor. Обработчики callback не владеют SQLite-соединением. Отдельно
проверить, где хранится UNKNOWN при гибели actor; утверждать durable poison
без доказанного протокола запрещено.

## Уточнение после независимого review

- Claim должен иметь уникальность на grant и связывать journal ID, неизменяемый
  scope leaf, executor incarnation и claim ID. Единый command ledger оставляет
  operation внутри неизменяемого payload, а не в области idempotency key.
- Только новый claim после нормально завершившегося commit может породить
  локальный permit; записанный historical receipt не содержит восстанавливаемой
  capability. Permit ссылается на общую локальную запись и потребляется под
  lock до единственной попытки submit.
- Минимум состояний: RESERVED → CLAIMED → TERMINAL_SCOPED. UNKNOWN не должен
  стирать последний достоверный переход; CLAIMED означает, что работа могла
  быть поставлена или запущена. Ошибка submit удерживает долг, а не доказывает
  отсутствие запуска. Только durably подтверждённый scoped terminal позволяет
  освободить метаданные; неизвестные узлы удалять нельзя.
- Вопрос claim после begin нужно решить до кода: рекомендация — разрешать только
  ранее зарегистрированный конкретный leaf/executor. Grants v2 не содержат
  этого scope и не должны автоматически превращаться в права v3; если запись
  ограниченного scope не реализована, claim после begin остаётся запрещён.
- Существующий `OwnedOperations` может завершить wrapper раньше, чем сам Future
  становится terminal. Для нового leaf требуются оба подтверждения, включая
  race мгновенного выполнения до bind. При cancellation нужно доказать, что
  точный Future отменён до входа wrapper. Очередь owner не блокируется под
  ownership lock; lost/full callback остаётся UNKNOWN.
- В `call_stt_client` request ID каждый раз новый: для позднего handoff нужен
  отдельный стабильный logical ID; optional signing ещё не подтверждает роль
  и incarnation получателя. Владелец повторно использует существующие
  `CallSTTService` и `_GigaAMCallLease` только после изучения их границ, не
  подменяет их одним Future.done.
