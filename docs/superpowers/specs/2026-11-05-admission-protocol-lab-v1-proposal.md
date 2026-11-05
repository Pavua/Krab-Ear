# Admission lab v1: предложение контракта ответов

Статус: **DESIGN ONLY**, несовместимая с v0 учебная схема; нет общей
production authority, аутентификации, подтверждённой идентичности, fencing,
terminal, recovery, crash durability или разрешения на запуск. Предложение не
подключается к Ear, Main или Gateway. Прежде чем кому-либо менять свой runtime,
все три владельца согласуют отдельный протокол и его доказательства.

## Смысл для клиента

Классификация ответа является лишь локальным сведением; ни один ответ не
создаёт execution permit и не подтверждает drain. Для учебного стенда логические
исходы называются `RECEIPT_ONLY`, `ADMISSION_REFUSED` и `UNKNOWN`.
При любом из них клиент не начинает новую работу и не запускает локальный
fallback. Это специально ограниченный lab-контракт; правила обычного поведения
продукта вне maintenance здесь не определяются.

`ADMISSION_REFUSED` допустим только для полного непротиворечивого ответа
`DENIED_CLOSED` на именно тот `reserve`, который запрашивался. В lab привязка
проверяется синтетическим harness-свидетельством; оно не является auth. В
реальной системе это должно быть заменено проверенной связью участника,
процесса/incarnation, generation и сообщения с аутентифицированным owner.
До этого даже правдоподобный `DENIED_CLOSED` извне рассматривается как UNKNOWN.

`RECEIPT_ONLY` обозначает подтверждённый синтетическим стендом исход операции,
не право её повторить и не доказательство готовности к stop. `UNKNOWN` включает
отсутствующий/неполный ответ, timeout, cancellation, недоступность, неподтверждённую
личность, несовпадение корреляции, версий, поколения или формата, конфликт и
ошибку сериализации. Отсутствие ответа не означает, что команда не исполнилась.
Не выбирать новый request ID и не обходить отказ через Gateway, локальный MLX,
другой endpoint или legacy путь только на основании этих исходов.

## Предлагаемые учебные сообщения

Ниже — **не реализованный wire API**. Для эксперимента v1 все request и response
несут точные `v=1`, `op`, `request_id`, `client_incarnation`, `owner_epoch` и
`controller_epoch`. `v` — строго int 1, не bool. Строки идентификаторов и
`executor` ограничены ASCII `[A-Za-z0-9_.:-]`, длиной 1..128; `kind` строго
`stt` или `tts`, grant ID — канонический lowercase UUID. `expected_revision` —
int без bool от 0 до 2^63−2. Никакие незаданные поля не принимаются.
Ответ должен повторять эти общие корреляционные поля;
`owner_epoch` — метка
инкарнации fixture, не удостоверение личности. Поля версионируются строго;
неизвестные поля, op, outcome, неверные типы и несовпадение хотя бы одного
поля отбрасываются как UNKNOWN. Реальный транспорт, framing, bounds и proof
будут согласованы отдельно; v0 остаётся неизменным.

Для изолированного эксперимента ожидаемые `owner_epoch` и `controller_epoch`
задаются harness заранее; узнавать их из самого ответа и тем самым считать этот
ответ доверенным нельзя. Если любая ожидаемая метка не получена заранее или
оказалась stale — UNKNOWN. В реальном протоколе происхождение и проверка этой
привязки остаются открытыми.

Команды привязаны ко **всему** исходному request payload: harness до отправки
фиксирует его канонические байты и SHA-256; response несёт lowercase hex
`request_sha256`, который должен совпадать с независимо сохранённым значением и
с повторно вычисленным digest фактического request. Для lab canonical encoding —
UTF-8 JSON object, ASCII keys в лексическом порядке, без пробелов, числа только
указанных integer типов, строки ASCII указанных выше правил; никаких повторных
ключей и неизвестных полей. Потеря исходного закреплённого digest или эпох,
включая crash/reopen, означает UNKNOWN. Hash и echo не удостоверяют отправителя;
в production нужна отдельная аутентификация и надёжное хранение контекста.

Ключ command ledger: проверенная identity, incarnation и request_id. Любое
несовпадение полного canonical payload для занятого ключа — CONFLICT/UNKNOWN;
replay идентичного payload обрабатывается до проверки CAS и возвращает исходный
receipt с теми же grant_id/claim_id без нового permit и без второго synthetic
debt. После потерянного ACK тест проверяет все три свойства, а не только
совпадение receipt. В fixture identity является synthetic label, не auth.
Status — read-only наблюдение и не занимает ключ command ledger.

| Команда | Поля помимо общих | Receipt исходы | Особое условие |
| --- | --- | --- | --- |
| `reserve` | `executor`, `kind` | `RESERVED`, `DENIED_CLOSED` | Только `DENIED_CLOSED` классифицируется как ADMISSION_REFUSED при подтверждённой корреляции; `RESERVED` не permit. |
| `claim` | `grant_id`, `executor` | `CLAIMED` | `claim_id` — исторический receipt; повтор не даёт второй permit. |
| `begin` | `expected_revision` | `CLOSED`, `ALREADY_CLOSED` | Закрывает новые root; не доказывает, что старые завершены. |
| `revoke` | `grant_id` | `REVOKED`, `CLAIMED_UNKNOWN` | Уже заявленная работа остаётся долгом, не считается завершённой. |
| `seal` | `expected_revision` | `SEALED` | Уничтожает право на неиспользованные synthetic grants; claimed/unknown остаются долгом. |
| `status` | нет | `OBSERVED` | Всегда `drain_authority=false`; одно наблюдение не разрешает stop. |

Для обсуждаемого v1 command response имеет ровно общие поля и поля `request_sha256`, `outcome`,
`grant_id`, `claim_id`, `control_revision`. Последний — целый тип без bool в
диапазоне 0..2^63−1; именно `grant_id`/`claim_id` — null либо канонический lowercase UUID.
`RESERVED` и `REVOKED`/`CLAIMED_UNKNOWN` несут grant_id и null claim_id;
`CLAIMED` — оба ID; `DENIED_CLOSED`, `CLOSED`, `ALREADY_CLOSED`, `SEALED`,
`CONFLICT`, `UNAVAILABLE` — оба null. Для успешного claim/revoke grant_id
должен в точности совпадать с запросом. `status` response имеет ровно общие поля, `request_sha256` и
`outcome=OBSERVED`, `control_revision`, `outstanding_count`, `claimed_count`,
`unknown`, `drain_authority`; counts — целые без bool в диапазоне 0..2^63−1,
`claimed_count ≤ outstanding_count`, оба флага строго bool и drain всегда false.
Пока в lab нет terminal API, `claimed_count > 0` требует `unknown=true`;
противоречие классифицируется как UNKNOWN. Обратное неверно: пустые counts
могут оставаться UNKNOWN после ошибки или повторного открытия.
Для `CLOSED` и `SEALED` revision равна `expected_revision + 1`, для
`ALREADY_CLOSED` — `expected_revision`; переполнение или несовпадение — UNKNOWN.
Эта проверка не делает revision вектором всех ресурсов или доказательством drain.

Для любой command разрешены `CONFLICT` и `UNAVAILABLE`, оба классифицируются
как UNKNOWN независимо от причины. Для status transport/protocol error тоже
даёт UNKNOWN, даже если корректный ответ не получен.

Все command outcomes, кроме `DENIED_CLOSED`, `CLAIMED_UNKNOWN`, `CONFLICT`, `UNAVAILABLE`, —
RECEIPT_ONLY при корректной корреляции и учебном свидетельстве; корректный
`OBSERVED` даёт RECEIPT_ONLY лишь при `unknown=false`; `unknown=true` даёт
UNKNOWN. `CLAIMED_UNKNOWN`, `CONFLICT`, `UNAVAILABLE`, нераспознанный статус
или спорный receipt — UNKNOWN. Кроме общих ошибок команд, исходы
разрешены только для соответствующей операции из таблицы; одинаковые названия
в другом op не принимаются. Claim на revoked grant не считается доказательством
отсутствия ранее заявленной работы. Ни клиент, ни consumer не должны принимать
частичный ответ или неизвестное дополнительное поле.

## Уже принятые работы и bootstrap

Close не отменяет уже зарегистрированный parent. Его continuation допускается
лишь в заранее зафиксированном immutable scope. Claim, победивший гонку с
отзывом, остаётся учтённым и может завершать разрешённую работу, включая
финализацию; revoke закрывает неиспользованные права и не стирает этот долг.
Любой допустимый child регистрируется до исполнения; seal исключает позднюю
регистрацию согласно согласованному общему протоколу. До общего seal нужно
доказать, что дальнейшие права parent создавать child закрыты и каждое ещё
необходимое обязательство finalization/persistence заранее учтено либо parent
действительно terminal. Иначе seal должен оставаться HOLD: он не даёт права
выполнить незарегистрированный child и не доказывает сохранение данных.
Как обеспечить эти предусловия без потери незавершённой работы — открытая
проблема общего протокола; лабораторный seal об этом доказательств не даёт.
Дочерние работы и обязательные persistence/consumer completion учитываются по отдельности.
Этот lab не осуществляет проверку этих требований и не выдаёт право на
continuation. Идентифицированный claimed worker остаётся UNKNOWN до
подтверждённого owner terminal и всех обязательств; таймаут, окончание HTTP,
внешнего Future или один снимок idle этого не заменяют.

Восстановление после перезапуска не реализовано. Запуск без доказанного fencing
старых писателей и legacy клиентов — UNKNOWN; новый owner epoch или пустой
журнал не доказывают, что прежняя работа отсутствует. Существующий lab: persisted
`TEST_OPEN` не открывается без recovery, `CLOSED/SEALED` — только inspection и
UNKNOWN, без нового permit. Нет автоматического retry с новым ключом или stop.

## Критерии для дальнейшего согласования

Нужны отдельные тесты на точную корреляцию и типы полей, потерянный ACK,
повреждённый/посторонний ответ, неподтверждённый synthetic owner, оба порядка
claim/revoke/seal, неизменяемый continuation scope, доказательство терминальности
child и сохранения, протокол исключения legacy входов и crash/recovery. Нужно
описать отдельно семантику provider error и подтверждённого maintenance отказа
для двух Main fallback путей и отдельной MCP `telegram_transcribe_voice`
цепочки Ear IPC → fallback. Последняя воспроизведена на синтетическом коде
ошибки, не на подтверждённом текущем ответе Ear. Также надо учитывать
потенциального прямого Ear POST клиента, флаговый Ear IPC TTS и MLX warmup.
Main отдельно воспроизвёл на synthetic IPC error без WAV переход admin
`/api/admin/voice/test_tts` через `voice_engine` и `KrabEarClient` к fake
cloud TTS fallback при включённом local TTS; фактический runtime-флаг и
формат живой ошибки Ear не подтверждены. Учёт origin и продолжения этой
работы требует отдельного согласования.
Production wiring VideoStudioEarSttClient и живая активность путей не подтверждены.
По Main source, MCP до Ear IPC вызывает `_bridge.get_voice_file`, а voice-profile
скачивает файл до Gateway; fake-тест воспроизвёл порядок без сети/ML. Общая root
должна регистрироваться до первого внешнего dispatch/download, а не только у STT.
Для уже принятого parent сохраняются отдельно согласованные continuation и долги.
Gateway source также содержит fallback после Ear HTTP 503 на локальный
faster-whisper и потенциально cloud при выключенном privacy. Отрицательный
health/degraded может обойти Ear, а cloud-primary — выполниться раньше Ear;
три пустых ответа Ear могут включить временный skip, privacy 403 исключён.
Evidence: source и существующие fake HTTP/faster тесты, не живой inference.
Сам HTTP 503 не является подтверждением отказа admission: нужна отдельная
типизированная owner-bound семантика, которая проходит все уровни без
переопределения любого provider 503.
Для реальной авторизации потребуются независимые security review и работающий
межпроцессный/live тест; результат лабораторной модели это не заменяет.
