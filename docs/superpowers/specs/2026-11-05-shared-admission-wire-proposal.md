# Межпроектный admission: предложение контракта

Статус: обсуждение, **не действующий IPC**, не authority и не разрешение на
выкат. Связанное исследование: `2026-11-05-admission-claim-and-descendants-draft.md`.
`64d91b66` — лишь локальный synthetic prototype, не подключённый к production.

## Граница и владелец

Предлагается один сериализующий владелец durable журнала вне перезапускаемого
Ear backend. Journal хранит installation ID, epoch владельца, состояние допуска,
зарегистрированные roots, scopes и их долг. Lock/lease, peer authentication,
fencing и durable recovery пока не спроектированы до production-доказательства:
потеря владельца, неоднозначность lock или восстановление без доказанной
целостности → `UNKNOWN`, никакого нового запуска и переключения.

Все источники Main, Ear и Gateway должны быть поимённо квалифицированы; только
сведения от соответствующего owner с проверяемой идентичностью и incarnation
могут менять учёт. UUID и PID сами по себе не подтверждают identity или владение.

## Предлагаемый wire-уровень

Ниже иллюстрация формы, точные методы и transport auth требуют общего review:

Все операции используют общий ключ идемпотентности `(authenticated principal,
client incarnation, request_id)`. Operation и канонические параметры входят в
неизменяемый payload: несовпадение с ранее записанным payload — конфликт без
нового решения; replay проверяется до CAS. Другой request_id не разрешает
повторно claim-ить тот же grant. Эти правила требуют общей сериализации всех
обработчиков и подтверждения схемы сохранения, а не только клиентского UUID.

- `reserve(root, owner, epoch, request_id)` → `{status: "granted", grant_id, epoch}`
  либо `{status: "maintenance_active", retryable: true, epoch}`. Последний
  ответ допустим лишь при доказанном отказе до side effect и durable записи
  denial; `retryable` не разрешает автоматический повтор до подтверждённого
  reopen или повторный звонок, списание либо refund. Отсутствие side effect
  относится только к отвергнутой локальной операции, не к уже существующей
  активности провайдера. Повтор прежнего request_id возвращает прежний denial; новая попытка
  после reopen требует нового request_id. Durable reserve учитывает долг, но
  сам по себе не разрешает выполнение.
- Для корня, чей reserve committed до begin, предлагается разрешать claim и
  выполнение после close только в рамках уже учтённого root/scope при проверке
  полномочий, текущего fence и принадлежности той же закрытой generation;
  новые roots запрещены. Такой долг не может исчезнуть из drain, пока работа и
  потомки не завершены. Альтернатива — отменить reserve до claim посредством
  атомарной сериализованной проверки отсутствия claim/capability и возможного
  I/O; затем записать terminal. Таймаут, локальная отмена ожидания или потерянный
  ACK не доказывают такую отмену. При неоднозначности долг остаётся UNKNOWN;
  reopen и новая generation не делают старую capability безопасной.
- `claim(grant_id, owner, epoch, request_id)` → свежая однократная capability
  лишь после подтверждённого durable commit и проверки всех полномочий. История
  или replay возвращают receipt без capability; потерянный ACK оставляет
  неопределённость и **не** разрешает повторный запуск. Форма и транспорт
  capability пока не определены и её нельзя строить из одних UUID.
- `begin(expected_epoch, request_id)` атомарно закрывает новые roots и создаёт
  новую maintenance-generation/control revision → `{status: "closing", epoch,
  generation}`, включая привязку всех ранее учтённых незавершённых roots к
  этому закрытию. Такая привязка не меняет историю claim, не сбрасывает его
  уникальность и не выдаёт заменяющую capability. Повторное close после reopen имеет другую generation даже при
  том же epoch владельца. Разрешённые handoff/descendants ранее
  принятой работы требуют отдельного доказанного ограниченного scope.
- `status()` → `{status: "open" | "closing" | "unknown" | "drained", epoch, generation,
  unsettled_count, coverage_complete}`. `drained` возможен лишь при полном
  подтверждённом охвате всех ingress/descendants, sealed деревьях, отсутствии
  долгов и UNKNOWN, стабильных epoch и fencing. Эти поля не доказывают всё
  это без проверенного implementation и свежей проверки у владельцев. Сам по
  себе снимок `drained` не разрешает cutover: закрытый admission и действующий
  fence соответствующих owner epoch и generation должны непрерывно удерживаться
  на протяжении всей lifecycle-операции, включая rollback, до безопасного
  завершения и разрешённого reopen. Crash, утрата fence или неподтверждённое
  восстановление журнала не открывают допуск и ведут к UNKNOWN/HOLD.
- `terminal(scope, ...)` принимается только от подтверждённого владельца этого
  scope после доказанного завершения фактической работы и потомков. Child и
  handoff регистрируются до отправки; seal атомарно запрещает последующую
  регистрацию. Terminal-решение, ledger и изменение долга фиксируются вместе;
  положительный ACK допустим только после commit. Повторы и чужой
  scope/incarnation не освобождают чужой долг; неоднозначный commit не даёт
  оснований считать освобождённым и собственный долг.

Timeout, отсутствующий IPC, невалидная схема, неизвестный epoch/owner и
ошибка авторизации → локальный `unavailable`/HOLD; клиент не запускает запасной
локальный или облачный inference и не повторяет side effect автоматически.
Адаптер сохраняет различие maintenance, обычного `stt_failed` и privacy 403,
не превращая ошибку gate в разрешение fallback. `maintenance_active` не отменяет
уже принятую работу. Метрики idle и успешный health не дают drain.

## Первый переход старых клиентов

Текущие legacy MCP и другие непокрытые producers не обязаны соблюдать новый
контракт, поэтому закрытие нового API само по себе их не останавливает.
Нужны полный инвентарь входов, owner-controlled ограничение новых операций,
учёт незавершённых работ и потомков, защита от повторного автоматического
запуска и проверка первого перехода. Механизм на существующем runtime пока не
доказан. Остановка по PID, timeout, исчезновение сокета или простой снимок idle
не являются достаточными доказательствами. Пока остаётся HOLD.

## Минимальная матрица доказательств

1. Reserve/begin и claim races; idempotency, повтор после lost ACK, stale epoch,
   чужой scope/owner, malformed/absent IPC, outage без fallback.
2. Crash до/после durable commit и submit, submit с ошибкой после запуска,
   cancellation вызывающего при продолжающемся worker, отложенные callbacks,
   дочерние процессы и незаконченный handoff; UNKNOWN остаётся HOLD.
3. Закрытие новых roots при контролируемом завершении уже принятого дерева;
   отсутствие или потеря учёта любого ingress не даёт drained.
4. Безопасный первый переход старых MCP/Perceptor/Gateway/Ear в изолированной
   интеграционной среде; повтор после restart и потеря/восстановление lock.
5. Совместное owner review и применимый межпроцессный E2E на точных версиях;
   synthetic unit tests, журнал и `health 200` сами не дают runtime GO.
6. Устаревший `drained` после reopen и нового close при прежнем epoch; утрата
   fence во время переключения или rollback; crash и восстановление журнала.
7. Один ключ с другой operation/payload; другой request_id для того же grant;
   гонка child-register/seal и terminal commit с потерянным ACK.
8. Reserve до begin и задержанный claim после close, атомарная отмена против
   claim; committed claim → lost ACK → begin → replay, lost ACK при reopen/new
   generation. Локальная отмена await REST/IPC
   не подтверждает завершение удалённой операции или её потомков.
9. Pending provider create и callback до ACK или после DELETE/restore; подмена
   metadata и чужой SID/attempt, aliases, timer auto-resume, bot financial CAS,
   crash/replay/restore и потеря fence во время внешнего I/O.

## Инвентаризация владельцев: подтверждено для последующей сверки

Main сообщает отдельные пути KrabEarClient в runtime/web voice и TTS, raw IPC
voice_assistant_tools, Telegram MCP transcribe_paths, Perceptor/Gateway, REST
video/commands, а также локальные STT/TTS fallback и автозапуск legacy MCP.
Общего клиентского choke point пока нет. Инвентарь не объявляется полным;
диагностический health следует отличать от создающих работу вызовов.

Отдельно необходим инвентарь операционных lifecycle-точек Ear: в source
`remove_agent.command` способен выгрузить backend и послать pkill, а
`profile_gigaam_worker.command` и `validate_c1_mps_fix.command` содержат
`kickstart -k`. Это явно запускаемые инструменты, а не доказательство их работы
сейчас. При квалификации перехода нужно исключить их конкурентный запуск или
доказать применение того же fence; ранее подготовленные draft-защиты сами по
себе эти точки не покрывают.

Gateway сообщает owner/bot и generic outbound, inbound voice, manual/auto
resume, STT/proxy/TTS/session/mic, background retranscribe, WS, call-control,
startup warmups/clone и retention/recovery. После закрытия новых roots status
callbacks и media уже принятых звонков должны продолжать работу. Без доказанной
семантики webhook retry/TwiML отказ нового входящего запроса может потерять
звонок. Health/pipeline не учитывает автоматически pending provider create,
удалённую работу и все descendants; их terminal-path требует отдельного
подтверждения. Это owner-сообщения для общего review, не проверка runtime.

Дополнительные межпроцессные сценарии: provider create остаётся pending при
health=0; callbacks/media продолжаются после close; auto-resume гоняется с
close; lost ACK/cancel не освобождает долг; stale epoch и восстановление DB
не создают право на запуск; два независимых процесса и rollback не обходят
fence. Старый Gateway бинарь не соблюдает проектируемый контракт.

Gateway отдельно отмечает aliases `/v1/telephony/{voice,status,media}` и
startup promises sweep; их нельзя забыть при отборе только Twilio-маршрутов.
Rollback deployer также способен вызвать bootout: доказательство rollback
требует того же полного ingress/descendant и provider-work охвата, что cutover.

Дополнение Gateway для совместной квалификации: входящий вызов уже может
существовать у провайдера до HTTP webhook. Отказ reserve сам по себе не
доказывает отсутствие внешнего side effect и требует отдельного проверенного
исхода без потери для звонящего. Provider create/result/callback могут прийти
до или после закрытия, в том числе после DELETE/restore; reconciliation требует
verified provider identity и привязки server-owned attempt/SID, а не публичной
metadata/query или dev-no-op verifier. Gateway отмечает, что текущая подпись
Telnyx не покрывает query; это owner source-report, а не наша проверка runtime.
Незавершённые bot held/starting и обещания/финансовый settlement требуют
собственного ownership/recovery: terminal вычисления не равен safe refund.
Запрет stale owner должен действовать в точке внешнего side effect; долгий
await может пересечь потерю fence. Если провайдер не поддерживает необходимый
fencing/idempotency и исход нельзя доказать, сохраняется UNKNOWN/HOLD.

Уточнение Main: `video_studio_ear_stt.py` и `voicenotes_command.py` обращаются к
REST `/v1/stt/transcribe`, причём у последнего синхронный read timeout до 600 с;
`voice_engine.py` использует `KrabEarClient.synthesize_speech` и при ошибке
может перейти к облачному `edge_tts`. Raw IPC и Telegram MCP остаются отдельными
входами. Следовательно, блокировать нужно не только локальный, но и облачный
fallback на gate/outage, не прерывая уже принятую работу без её учёта.
