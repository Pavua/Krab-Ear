# A5.3 — независимый counter-review

Дата: 2026-09-30. Проверены A53_STRONG_MODEL_HANDOFF.md, design §7 и только
точечные source-ссылки ниже. Runtime, тесты, IPC, история и сеть не запускались.

## Вердикт

**BLOCK для передачи текущего handoff исполнителю.** Три обязательные правки
ниже относятся к контракту/карточкам. Это не blocker самой выбранной архитектуры.
После их согласования допустим design GO; реализации и encryption GO ещё нет.

## 1. P1 — однозначный UNKNOWN, включая missing settings

Контракт п.10 требует существующей семантики missing settings/ENC1, но тест 6
требует отказа при missing settings без условий. Это разные правила:
`KrabEar/backend/state_store.py:178` возвращает OFF для missing settings без ENC1.
Кроме того, read_history_encryption_flag возвращает True и для настоящего ON,
и для повреждённой/нечитаемой политики. Для grant это различие критично:
UNKNOWN нельзя превратить в ON, которому разрешено выдать capability.

Обязательное уточнение: новый authorizer различает KNOWN_OFF, KNOWN_ON и UNKNOWN;
missing/corrupt/unreadable/non-bool policy -> UNKNOWN, revoke и отказ grant/validate.
Начальная OFF-политика должна быть явно инициализирована/подтверждена обычным
startup/settings путём до authorizer validation. Не менять старый A5.2 reader
и не наследовать его fresh-profile OFF fallback внутри authorizer.
Fixtures: удалить settings из ранее валидного OFF и ON профиля; оба deny;
восстановить прежние значения -> старый grant остаётся отозванным.

## 2. P2 — исправить фактическую карту Markdown writers

`history_service.py:1560–1724 handle_export_history_markdown` рендерит Markdown
и опционально вызывает pbcopy; файла не пишет и TranscriptWriter не вызывает.
Не вводить ему file-export consent: clipboard вынесен за narrow scope.
Настоящая ручная запись: `handle_export_history`, mkdir/write на 1449–1452,
уже указана другой строкой карты.

`transcript_writer.py:214–244 write_transcript` сам не проверяет policy:
mkdir/reservation/atomic write происходят напрямую. Поэтому утверждение
«TranscriptWriter сохраняет независимый ON-block» заменить точным:
сохраняются ON-block у автоматических recorder/import callers; сам shared writer
не является существующим security boundary. Если новый manual route использует
его, context/gate обязателен до первой mutation; это не разрешает automatic copies.
Карточка B должна назвать реальные защищающие call-sites без фиктивного wiring.

## 3. P2 — обязательная межпроцессная регрессия revocation

RAM generation в одном StateStore не разделяется со вторым экземпляром/процессом.
Контракт предусматривает fingerprint, но тест 5 может пройти только на одном store.
Дополнить acceptance конкретным случаем: процесс A выдаёт grant; отдельный
процесс B с тем же temporary data_dir выполняет поддержанный save_settings
ON -> OFF -> ON; A не делает export между переходами; первая validation A
отказывает старому grant, несмотря на совпадение конечных bool-значений.
Вторая фикстура: атомарно заменить settings вне экземпляра A, сохранив bool-
значения; изменённая версия файла консервативно отзывает grant.
В карточке A зафиксировать поля fingerprint и согласованное чтение policy/version
под StateStore lock; failure/неустойчивое чтение -> UNKNOWN, без cache fallback.
Проверить barriers и ограниченный timeout, а не sleep; дочерний fixture process
обязательно join/cleanup. Тесты также должны ловить инверсию порядка locks.
Не вкладывать history_flock нового FD под уже удержанный StateStore._lock:
state_store.py:253 прямо описывает нерентерабельность этих двух механизмов.

## Что выдержало counter-review

- Narrow lineage history + явно включённый Quick Capture; внешние каналы отдельно.
- Trusted-client consent честно допускает same-UID self-grant; JSON app_session_id
  не выдаётся за удостоверенную Swift identity. Peer-auth не обещан.
- Session grant многоразовый; Swift receipt/closure одноразовые, seq и epoch проверяются.
- Revocation до validation запрещает запись; после неё допускается одна операция.
- Scheduler не заимствует grant; batch, двойная Swift/backend копия и async PDF
  требуют отдельных свежих validations; initial denial предшествует mutation.
- Общий порядок StateStore -> authorizer и запрет callbacks под authorizer lock верны.
- Unit/fake IPC не выдаются за encrypted/production E2E; isolated IPC gate требуется.

Обязательных оснований расширять scope до всех PII или усиливать peer identity
в текущей задаче нет. После трёх правок нужен короткий повторный docs gate.

## FINAL — повторный gate 2026-09-30

**GO для исправленного design/handoff и подготовки последовательных карточек.**
Этот статус заменяет первоначальный BLOCK выше. Проверен A53_STRONG_MODEL_HANDOFF.md
SHA256 `9bb260fe303b081a4883f9e4da8d7e3bffeebc8774898f69ed92acf30c5def5a`.
Три замечания закрыты: typed UNKNOWN/explicit initialization, корректная карта
writers, central durable revision + coherent fingerprint + cross-process fixtures.
Точечно подтверждены settings copy2 restore, запрет encrypted restore_settings,
optional IPC HMAC и существующие auto-.md caller gates. Их runtime не проверялся.
Secret-redaction/sentinel tests включены как требования, а не выполненные проверки.
Обязательное перенесение уточнений в spec/implementation cards сохраняется.
Trusted-local threat model честен; дополнительных обязательных замечаний нет.
Это не source acceptance, CI/merge/deploy/encryption GO; тесты и IPC не запускались.
