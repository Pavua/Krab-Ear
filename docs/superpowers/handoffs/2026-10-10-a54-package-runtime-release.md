# A5.4 — фактический package/runtime release 10.10.2026

## Что применено

Source: `9d7a0aac0cd064a05294f796a6f5fde7804e33a7`, Git tree
`ff06cddf2c72b53600bbbf9f03379b4c8f001960`. Этот source завершил оба workflow:
[CI](https://github.com/Pavua/Krab-Ear/actions/runs/37861320422),
[krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/37861320426).
Сборка и подпись выполнены после явного разрешения владельца выпустить Ear
при текущей нагрузке. Это разрешение не распространяет resource GO на Gateway.

Свежая Git-копия: 2687 blobs/modes; native inputs: 164, без изменений после
сборки. Pinned Sentry 8.58.2, Sparkle 2.9.4, swift-opus 0.0.2 и шесть binary
archives проверены по revision/checksum. Swift release build jobs=1 завершился
успешно; прежние build products не переиспользованы. Две физические копии
Sparkle совпали по полному дереву, bytes, modes, symlinks и служебным атрибутам;
соответствие закреплённому archive payload проверено отдельно.

До подписи проверены packaged framework и unsigned binary. После подписи
strict/deep verification, прежнее Accessibility requirement и arm64 UUID
binary/dSYM/package прошли. Новый signed executable SHA256:
`e1156b14a8bbd5d36ce43e781a54c00aafc71e908668b4ff010dc885d16ebbe9`.
UUID: `EE6DD5B0-5473-3211-A3CF-1F3A16D813D9`. Все 166 package entries сверены.
Existing signing identity сохранена; новый сертификат/TCC reset не требовались.

## Применение и smoke

Перед первым эффектом: exact-SHA CI → Sentry GET → runtime qualification,
независимый Astra whole-plan/package gate, свежая 60-секундная idle-проверка
с финальным наблюдением. Wake word включён: Swift остановлен первым; затем
повторён полный idle gate. Старые Backend/REST jobs, parent/children/PGID
дренированы, socket/listener перестали принимать соединения.

В Backend/REST plist атомарно изменены только entrypoint/PYTHONPATH на новый
source root. Interpreter, working directory, data/socket/log paths и остальные
поля сохранены; plist Swift не переписывался. Новый пакет установлен атомарно;
прежний целый пакет и старый source root сохранены для проверяемого отката.

Запуск по одному bootstrap: Backend → REST → Swift. Backend: оба позитивных
passive STT warmup markers из нового участка журнала, sole-writer check,
более 23 секунд стабильного IPC. REST: HTTP200/status ok, принадлежащий его
PID listener. Swift: единственный exact-app process, новый passive-supervisor
marker. Финальный smoke: все три PID/start/runs стабильны, 11 успешных IPC/REST
samples за более 23 секунд. Wake word остаётся включён.

Новая owner-проверка диктовки и автоматической вставки в Codex без Cmd-V:
**PASS, owner подтвердил 00:08 CEST**. Сохранён только boolean receipt, без
текста диктовки/аудио. После ответа текущие same PID/start/runs, IPC и REST health
подтверждены снова. Подтверждение старой версии от 09.10 не использовалось.

## Сохранённые отрицательные наблюдения

- Первый Backend monitor остановился на argv-tail assertion сразу после
  bootstrap. Позднее tail полностью совпал; причина первого несовпадения
  не установлена. Bootstrap не повторён, Backend не остановлен; новый
  read-only monitor подтвердил стабильность и оба warmups от прежнего cursor.
- Sandbox codesign verification сначала отказала. Та же read-only проверка
  вне sandbox прошла; это не доказанная проблема подписи пакета.
- Перед Swift один read-only вызов codesign неверно передал inline requirement.
  Корректный формат `=expression` прошёл; подпись/Accessibility не менялись.
- Lsof выдавал точное предупреждение об отдельном NFS volume. Локальный журнал
  на APFS и отдельном device квалифицированы; узкий wrapper сохраняет exit,
  stdout и sole-writer проверку, исключая только закреплённый stderr fingerprint.
  Реальные тесты: один writer принят, два writers/device drift/дополнительный
  stderr отвергнуты. Astra подтвердил узкий scope; новые ошибки остаются HOLD.
- Hash settings после Swift старта изменился. Причина не установлена; byte-exact
  settings parity не заявляется. Encryption OFF, migration false и restore false
  подтверждены отдельными актуальными IPC getters.

## Границы и следующий шаг

Шифрование реальной истории OFF. Activation/migration/Keychain/purge не входят
в этот release. Synthetic crypto IPC 109 checks/Astra PASS — отдельный source
контракт; encrypted-history UI/Keychain acceptance и Timeline ON остаются LIMIT.
Loaded Python module SHA неизвестен: config/argv/worker script origin не заменяют
in-memory attestation. Sentry organization/auth PASS, свежий Ear ingress UNKNOWN.

Main runtime не менялся. Gateway сохранил согласованный STOPPED/HOLD и свой
return contract; owner accepted load для Ear не является Gateway ticket.
Окончание quiet window передано соседним координаторам. Global foreign IPC
inflight не наблюдается; maintenance Ear мог прервать Main→Ear TTS.

После нового Swift старта любой откат начинается со Swift bootout, доказанного
исчезновения его job/PID/descendants и нового полного 60-секундного idle gate
с финальным наблюдением перед остановкой Backend/REST. Busy/UNKNOWN исключают
слепой retry/kill. Новых жизненных циклов для получения module proof не требуется.

Независимый финальный Astra review: ACCEPT package/deploy/narrow smoke;
owner acceptance получена после этого metadata review.

Следующий шаг: docs PR CI/merge; затем отдельная карточка оставшихся
UI/Keychain acceptance. Activation требует явного scope.
Модель: Sol 6.1 High для механики и docs, Astra точечно для high-risk gate;
OpenCode Muse и agy использованы только для публичных source/docs scout briefs.
