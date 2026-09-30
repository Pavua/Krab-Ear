# A5: защита хранилища истории и явный plaintext-экспорт

Статус: технический проект для review; продуктовые решения владельца получены
24.09.2026. Это не разрешение включить шифрование или мигрировать живую историю.
База: `5ebfb8375392e45bd4d12da908cd8e7b47a642b0`,
`origin/codex/krab-ear-v2`, повторно сверена 24.09.2026.

## 1. Цель и граница

При `history_encryption_enabled=True` новые записи управляемой истории, её
дельты и служебные индексы не сохраняют plaintext. Отказ ключа, повреждение
ENC1, неизвестная политика или незавершённая транзакция запрещают небезопасную
операцию. Ошибка не должна выглядеть как пустая история или успешная запись.

Владелец выбрал сначала историю и её журналы. Самостоятельные PII-хранилища
приложения (словарь, профили спикеров и другие подсистемы) — последующий аудит.
Это не гарантия шифрования всех данных приложения. Однако создание внутренней
копии самой истории (архив, версии, backup) относится к этой границе: оно не
может служить обходом выбранной политики.

Управляемый набор — явный реестр, не filesystem glob:

| Файл | Содержание |
|---|---|
| `history.ndjson` | Текст и поля записей |
| `history_tombstones.ndjson` | Удалённые ID |
| `history_purged_ids.ndjson` | Постоянный запрет resurrection |
| `history_status.ndjson` | Статус вставки |
| `history_tags.ndjson` | Пользовательские теги |
| `history_favorites.ndjson` | Избранное |
| `history_annotations.ndjson` | Заметки |
| `history_text_updates.ndjson` | Изменения текста и confidence |
| `history_action_items.ndjson` | Задачи, решения и вопросы |
| `history_calendar_links.ndjson` | Связи с событиями и названия |

`settings.json` хранит конфигурацию и не входит в зашифрованный формат истории.
Его нельзя восстанавливать поверх текущей encryption policy через `copy2`.
Сессионное разрешение экспорта никогда не записывается в settings, UserDefaults,
логи, Sentry, backup или IPC-события.

## 2. Решения владельца

- Plaintext-файлы и Obsidian при включённом шифровании запрещены по умолчанию.
- Явное подтверждение разрешает plaintext-вывод до конца текущей сессии.
- Разрешение распространяется на ручные экспорты и Quick Capture → Obsidian.
- Старые plaintext `.md`, backup и экспорты сначала инвентаризируются;
  удаление, перенос или перезапись — отдельное решение владельца по результату.
- Полный A5 не принимается, пока судьба найденных управляемых plaintext-копий
  не определена. Указание неизвестного внешнего каталога как «чистого» запрещено.

Если владелец выбирает сохранить plaintext, отчёт перечисляет принятое
исключение по категории/пути и говорит: «управляемые журналы защищены;
перечисленные legacy plaintext-копии сохранены владельцем». Такой результат
не называется «все копии истории зашифрованы» или безусловным полным A5.

Сессия в этом проекте — текущий запуск Swift-приложения и текущий экземпляр
backend. Закрытие/новый запуск приложения или смена backend epoch требуют нового
подтверждения. Обрыв одного IPC-запроса сам по себе не завершает сессию:
`IPCClient` открывает отдельное соединение для каждого вызова.

## 3. Выбор архитектуры

Рекомендуется существующий построчный ENC1 + единая политика записи StateStore
и отдельный ephemeral export grant. Сохраняются AES-256-GCM, текущий Keychain
ключ и структура журналов. Это позволяет принять небольшие последовательные
диффы и сохранить совместимость с уже зашифрованной основной историей.

Рассмотренные альтернативы:

1. Новая зашифрованная БД: транзакции проще, но новый storage engine, новый
   формат и перенос всех readers/writers существенно расширяют приёмку.
2. Шифровать целый файл при каждом append: проще контейнер, но дорогая
   перезапись большой истории и ухудшение задержки диктовки.

Новые зависимости для выбранного решения не нужны. Python 3.12 CI, macOS
Bash 3.2 и существующие Swift async sheet helpers сохраняются.

## 4. Единый codec и состояние политики

StateStore предоставляет один реестр десяти путей. `_has_encrypted_history_unlocked`
проверяет весь реестр: утрата settings при ENC1 только в sidecar не означает OFF.
Проверки в `save_settings` пользуются тем же реестром.

`_append_ndjson` становится instance-методом и шифрует сериализованную строку
до файлового sink; вызывающий production-код уже держит history lock.
Сам helper не добавляет exclusive lock в shared-read цепочки. Так закрываются
прямые вызовы из `recording_merger`, archive tombstones и HistoryService.
Обычный history/tombstone append делегирует тому же методу. Static raw helper
остаётся низкоуровневым sink уже подготовленной строки, не policy API.

Все readers управляемых журналов и их счётчики используют существующий
`_read_history_ndjson_unlocked`. Static `_read_ndjson_unlocked` остаётся только
для явно импортируемого plaintext-файла и legacy тестов OFF-профиля.
Ошибка decrypt должна дойти до вызывающего кода, а не пропустить строку.

Построчное чтение plaintext остаётся необходимым для legacy профилей и
контролируемой миграции. Наличие такого reader не означает принятую миграцию:
состояние `verified_encrypted` устанавливается только после проверки всех
управляемых файлов. Ошибка чтения политики не даёт plaintext-разрешение даже
при ранее выданном export grant.

Compaction заранее читает все нужные журналы и готовит зашифрованные строки
результата до замены активного файла. Annotations/calendar остаются ENC1.
Нельзя подавлять ошибку сохранения purged IDs и затем очищать tombstones.
При ошибке crypto preparation активные файлы неизменны. До отдельной
транзакционной фазы несколько rename не объявляются атомарным commit.

## 5. Миграция, восстановление и backup

Миграция работает только с явным реестром файлов, под общим lock, после
подтверждённого idle/resource окна и отдельного разрешения на live-операцию.
В source-тестах используются исключительно синтетические временные профили
и случайный тестовый ключ, без системного Keychain.

Предлагаемый протокол:

1. Проверить типы/контейнмент файлов, политику, доступность существующего ключа
   и отсутствие незавершённой операции. Не следовать symlink из реестра.
2. Под lock создать приватный staging и полностью подготовить encrypted
   snapshot всех десяти журналов. Уже ENC1-строки проверить расшифровкой;
   plaintext зашифровать. Проверить точное соответствие каждой исходной строки
   расшифрованной выходной строке; malformed данные не отбрасывать молча.
3. Fsync staged files и каталог. Durable manifest содержит только фиксированные
   имена файлов, размер/хэш ciphertext, transaction ID, состояние и версию;
   в нём нет текста, ключа или хэша plaintext.
4. Установить durable `COMMITTING` до первой замены. Readers/writers/restore
   проверяют pending state под тем же lock и не работают с частичным набором.
   Замены использовать только после повторной проверки fingerprint источников.
5. Сохранить проверенный encrypted snapshot до завершения всех замен и fsync
   каталога. После crash COMMITTING либо докатывается из этого snapshot,
   либо остаётся fail-closed до восстановления; непроверенный успех запрещён.
6. Установить `COMMITTED` только после read-back всех файлов. Удаление staging
   выполняется отдельно и не превращает незавершённую очистку в успех миграции.

До COMMITTING отмена оставляет исходные файлы без изменений. После начала
commit безопасный путь — завершить заранее проверенный encrypted snapshot;
автоматического отката в plaintext под включённым флагом нет. Recovery при
недоступном ключе не создаёт новый ключ и не запускает обычное обслуживание.

После активации допустимый кодовый rollback обязан уметь читать все десять
ENC1-журналов и понимать pending transaction/recovery из A5.2. Один codec
A5.1 недостаточен. Старые релизы не являются безопасным rollback target;
это проверяет release-процедура до переключения, не флаг в старом бинарнике.

Новый backup — согласованный encrypted snapshot того же реестра под lock,
с manifest; settings отделены от history payload и не могут понизить policy.
Restore предварительно полностью проверяет snapshot и key, готовит ENC1 под
текущей policy, затем использует тот же commit/recovery протокол. Незнакомый
формат, неполный набор или попытка восстановить OFF-settings отклоняются до
первой записи. Legacy restore напрямую через `copy2` в защищённом профиле
недопустим. До реализации нового протокола backup/restore при ON возвращают
явный `history_encryption_operation_unavailable`, не частичный результат.

До подготовки restore snapshot под тем же lock собирается объединение
текущих tombstones и permanent purged IDs. Эти ID сохраняются в новом encrypted
ledger и исключаются из восстановленной истории и связанных дельт. Старый
snapshot не может уменьшить нынешний deletion ledger. При недоступности ключа
или повреждении текущего ledger restore прекращается без изменения файлов.
Намеренное возвращение ранее удалённых записей обычный restore не разрешает.

Миграция файлов не гарантирует уничтожение прежних plaintext-блоков в APFS
snapshots, SSD или внешних резервных копиях. Инвентарь и приёмка фиксируют эту
границу; приложение не обещает ретроактивное secure erase.

## 6. Внутренние производные копии

В первой поставке при encryption ON операции, создающие plaintext archive,
transcript versions и старые auto/manual backup, прекращаются с явной причиной
до mkdir/append/rewrite/copy/prune. Эта функциональная граница должна быть видна
в UI/status и release notes; включение шифрования не должно давать ложное
сообщение «архив/backup сохранён». OFF-профиль сохраняет прежнее поведение.

Это временная совместимость защищённого режима: новый encrypted snapshot
возвращает backup/restore, а архив/версии получают отдельную encrypted
реализацию до возвращения этих функций при ON. Существующие архивы/версии
включаются в инвентарь, автоматически не удаляются. Explicit owner purge
остаётся отдельной операцией и не запускается из encryption gate.

Auto-backup gate должен стоять до retention/pruning старых backups, а не
только перед `copy2`. Автоматические `.md` recorder/import остаются запрещены
при ON независимо от export grant: владелец разрешил Quick Capture → Obsidian,
а не включение всех автоматических текстовых копий.

## 7. Разрешение plaintext-вывода

Swift хранит случайный app-session ID и выданную capability только в RAM.
Backend при старте генерирует новый epoch и хранит grants только в RAM.
Подтверждение в async sheet выдаёт opaque capability для app-session + epoch.
Каждый ручной export и Quick Capture → Obsidian предъявляет эту capability.
Headless/script callers без подтверждённого app-session получают
`plaintext_confirmation_required`. `force=True`, `save_to_file=True`, batch
export, повторный запрос и старый boolean `confirm=True` grant не заменяют.

Backend проверяет policy + grant перед каждым файловым sink, включая прямой
`ObsidianSyncManager.sync`. Privacy ON запрещает вывод даже при grant.
Смена privacy/encryption policy инвалидирует grants; повторное включение ON
не наследует прошлое согласие. Ошибка policy/epoch проверки запрещает запись.
Grant не хранится как один глобальный boolean на весь backend.

Swift Markdown/NDJSON writers требуют текущего разрешения непосредственно
перед записью, в том числе после ожидания NSSavePanel. Показ/чтение истории
не выдаёт grant. Backend epoch проверяется свежим preflight; после его смены
Swift не восстанавливает grant автоматически. Разрешённая операция,
линеаризуется успешной backend validation одного preflight для одной
запланированной Swift-записи. Отзыв до validation запрещает запись; отзыв
после неё допускает завершение только этой операции. Повторная операция
требует новой validation. Межпроцессная проверка и локальная запись не
объявляются атомарными; отзыв не обещает стереть уже экспортированный файл.

Нормальный выход Swift отзывает grant best-effort. После crash новый процесс
имеет новый ID и не получает старую capability. Не обещается мгновенная
инвалидация всех украденных токенов: она требует peer identity/liveness,
которых сейчас IPC не предоставляет. Socket 0600 защищает границу UID;
grant — защита от случайного plaintext-вывода доверенными локальными клиентами,
а не доказательство человеческого согласия против злонамеренного same-UID
процесса. Последний и сейчас может запрашивать plaintext истории через IPC.

Область этой policy — файловые экспорты и Obsidian. Clipboard/вставка диктовки,
передача в Notes/iMessage и остальные внешние каналы сохраняют существующие
правила; их согласование относится к отдельной политике внешней передачи.

### 7.1 Typed PolicySnapshot и UNKNOWN (обязательное уточнение A5.3 FINAL GO)

Новый `PolicySnapshot` — типизированный: `KNOWN_OFF | KNOWN_ON | UNKNOWN`,
отдельно privacy bool, fingerprint и reason. Bool ошибки НЕ кодирует состояние:
`ON` означает только явно валидный `true`, а не «reader считает ON при сбое».
`OFF` означает только явно валидный `false` при валидном snapshot. Всё остальное —
`UNKNOWN`.

UNKNOWN-триггеры (каждый ведёт к UNKNOWN, без исключений): отсутствующий файл
`settings.json`; не-object или битый JSON; duplicate policy keys при разборе;
нечитаемый или нерегулярный файл (не regular file); отсутствие любого из ключей
privacy/encryption policy; значение не exact-bool (`True`/`False`, без
truthy-интерпретации `1`/`"true"`/`None`); missing или invalid internal revision
`_plaintext_export_policy_revision`. Ошибка или timeout захвата lock при чтении
snapshot — тоже UNKNOWN, без cache fallback на прежний snapshot.

UNKNOWN немедленно очищает grants/receipts, увеличивает локальный
`policy_generation` и запрещает grant И validation с reason
`plaintext_policy_unavailable`. Восстановление файла с прежними bytes НЕ
возвращает старое согласие: старый capability остаётся отозванным, при валидном
ON требуется новый sheet.

A5.2 `read_history_encryption_flag` НЕ менять: он намеренно объединяет ON и
ошибку в `True`, а missing без ENC1 — в `False`. Его bool никогда не трактовать
как typed snapshot и не наследовать его fresh-profile OFF fallback внутри
authorizer. Отсутствие флага никогда не означает OFF для authorizer.

### 7.2 Explicit initialization только для достоверно нового профиля

Startup/settings initialization ДО authorizer сохраняет явные bool-defaults
только для достоверно нового профиля: `data_dir` создан этим startup с
`exist_ok=False` и нет восстановленного/унаследованного состояния (ни copy, ни
restore, ни backup-развёртка). Уже существующий missing/incomplete профиль новым
НЕ объявляется — он остаётся UNKNOWN до validated repair.

Legacy-файл с обоими валидными bool, но без revision — обычная startup migration
под store lock (владение карточки A, не authorizer): сохранить те же flags,
добавить internal revision, без выдачи grant. Legacy с missing/invalid flags
остаётся UNKNOWN до обычного validated settings repair/save; export UI сообщает
«Настройки требуют восстановления», никаких defaults из authorizer. Startup
initializer берёт exclusive `StateStore._lock` заново; если caller держит shared,
сначала полностью отпускает его, затем exclusive и повторное чтение/проверка.
Запрещены SH→EX upgrade и сохранение прежнего snapshot после reacquire.

### 7.3 Central commit, revision, lock ordering, fingerprint

Новый internal `_plaintext_export_policy_revision = uuid4().hex` генерируется
центральным settings commit при КАЖДОЙ поддержанной записи settings, даже с теми
же значениями. Входное или backup-значение revision всегда игнорировать и
заменять свежесгенерированным. Это durable версия против ON→OFF→ON между
процессами, а не secret/capability — её персистентность в settings допустима.

`StateStore.save_settings` и общий `_save_settings_unlocked` сохраняют
существующие A5 write guards, затем пишут flags + новую revision одним atomic
replace (temp + fsync + rename). Import/restore/reset/recovery, меняющие
settings, обязаны идти через этот commit; callback не является единственным
revoke-механизмом. Конкретный bypass: legacy
`HistoryService.handle_restore_history:5456` делает `copy2(settings)` —
settings-часть заменить на validated commit под тем же StateStore lock, без
вкладывания `save_settings`/`history_flock` нового FD друг в друга
(`state_store.py:253` прямо описывает нерентерабельность этих двух механизмов).
Запрет encrypted snapshot restore settings (`encrypted_snapshot.py:3034`)
сохранить.

Snapshot читать на КАЖДОМ issue/validate/backend sink под `StateStore._lock`
того же профиля. Затем брать authorizer lock; никогда наоборот. Для manager без
store инъецировать callback этого store, а не новый независимый history_flock.
Ошибка/timeout захвата → UNKNOWN + revoke, без cache fallback.

`_read_plaintext_policy_snapshot_unlocked` требует удерживаемый store lock:
open `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, fstat обязан показать regular file,
читать максимум 16 MiB (превышение → UNKNOWN). fstat до/после чтения и финальный
lstat должны совпасть по `dev/ino/size/mtime_ns/ctime_ns`; иначе UNKNOWN без
retry по старому snapshot. SHA содержимого не логировать вместе с содержимым.

Fingerprint = `(profile identity, st_dev, st_ino, st_size, st_mtime_ns,
st_ctime_ns, SHA256(raw_bytes), internal_revision)`; JSON flags и revision
разобрать ИЗ ЭТИХ bytes, а не читать hash/stat/flags разными cached путями. При
любом fingerprint mismatch отозвать прежние grants ДО validation и обновить
remembered snapshot/generation. При valid snapshot вернуть
`plaintext_session_expired` для старого context; при UNKNOWN —
`plaintext_policy_unavailable`. Даже same-bool atomic external replacement
консервативно отзывает grant.

Межпроцессная точка commit — atomic replace flags+revision под общим flock;
validation сравнивает snapshot под этим flock. Процесс B не очищает RAM процесса
A напрямую, но первая validation A гарантированно видит новую revision
поддержанного commit и отказывает старому grant. Полная обнаруживаемость
злонамеренного same-UID, восстанавливающего revision/metadata или обходящего
locks, НЕ обещается. Raw recovery replacement вне commit ловится fingerprint;
supported recovery обязана выдавать новую revision. Partial/failed restore при
неконсистентном snapshot → UNKNOWN, не fallback OFF.

### 7.4 Epoch, session, capability, потоки, privacy

Backend epoch — случайные 32 bytes на новый `BackendService`/process;
`app_session_id` — UUID на запуск Swift; capability — `secrets.token_urlsafe(32)`.
Grants/receipts — только RAM, никогда в settings/UserDefaults/Keychain/logs/
Sentry/backups/diagnostics/profile. Grant привязан к связке
profile/service + app_session + epoch + generation + fingerprint snapshot, а не к
единому флагу на весь backend (такой единый флаг запрещён). A5.2 mock/no-store
OFF fallback сюда не копировать. Authorizer создаётся и подключается ДО
scheduler/manager threads. Privacy `true` всегда deny, даже при KNOWN_OFF и даже
при валидном grant; KNOWN_ON + privacy false требует grant. Normal Swift quit
отзывает grant best-effort; backend shutdown очищает RAM; crash не гарантирует
мгновенный отзыв украденного токена. Никакого global active-session для таймера:
Quick Capture передаёт context своего Swift-процесса. Консервативный trade-off:
любое settings save/replacement, даже unrelated key, может потребовать нового
consent — это намеренное безопасное поведение A5.3.

### 7.5 Карта обязательных sinks и проверенные границы

Python (`KrabEar/backend/`): `history_service.py` — export_history:1449 (mkdir/
write 1449–1452), selected:1830, `_finalize_srt_export`:1998, JSON:2166,
CSV:2293 — auth до mkdir/temp/write, сохранить render-only варианты; Obsidian:4658
(privacy gate 4383/4635, затем mkdir/write без capability — закрыть), HTML:5989 и
alias `generate_html_report` (оба alias при `save_to_file` ведут к одному gate;
render-only без file consent); batch_export:5623 + `_export_csv_to_dir`:5748
(проверка до bundle mkdir и отдельная validation каждого файла; context
прокинуть делегатам); `service.py` export_timeline_svg/json/ical:5566/5620/5669
(в scope как производные истории; gate ДО `_resolve_timeline_export_dir`:5433 —
он делает mkdir); `obsidian_sync.py` sync:319/361/398 (прямой sync без
privacy/capability gate) и `handle_sync`:547 (только privacy) — инъекция общего
authorizer, отсутствие authorizer = deny, `force` не меняет policy;
`export_scheduler.py` check_and_export:413 → `_do_export`:164 (StateStore history
→ temp/fsync/replace; таймер не получает чужой grant; ON запрещён до
mkdir/pruning, включая direct `_do_export`); `sharing_manager.py`
`_fetch_items`:609 (читает history) → `prepare_share`:215 →
`_persist_package`:736/751 (локальный plaintext package — файловый sink; gate
direct prepare/persist; grant не разрешает внешнюю публикацию).

Swift (`native/KrabEarAgent/`): `HistoryPanelController+History.swift` .md:141,
.ndjson:192 из `self.items` плюс дополнительная backend-копия:158 — две записи =
две validation, отказ первой не запускает вторую; `+ExportSelection.swift:325`
(export_selected_items → локальный файл; fresh preflight после NSSavePanel);
`+ActionItems.swift:230/266` (get_history_page → Markdown action items; в scope,
прочитанная история не consent); `+MeetingMode.swift:267` (get_meeting_report;
`service.py:4895` — отчёт одной записи history; в scope; inject shared session
coordinator также в standalone report VC); `+StatsReport.swift:98`
(generate_stats_report → StatsReportGenerator читает active history,
stats_report.py:734; в scope как history-derived отчёт);
`AnalyticsDashboardViewController+PDFExport.swift:110` (generate_html_report с
полными транскриптами → PDF в temporaryDirectory; в scope; fresh validation
ПОСЛЕ renderer callback; temp тоже plaintext sink); `main+QuickCapture.swift:713`
(текст заметки → `run_obsidian_sync(items)` с `force:true` без session context —
явно включён владельцем; consent общий в рамках этого app-session).

Проверенные границы (вне narrow scope, молча не расширять): `+CallAssist.swift:465`
(payload из VG HTTP `call_assist_service.py:963`, не Ear history — кандидат
отдельной внешней policy); `+Import.swift:594/642` (operational queue report:
счётчики/источники/ошибки, не transcript export); glossary CSV
`HistoryPanelController.swift:2532`, `+ConfigPresets.swift:627`, settings export,
logger/plist writers — вне narrow history scope; `handle_export_history_markdown`:
1560–1724 только render/optional pbcopy, файла не пишет, TranscriptWriter не
вызывает — сохранить privacy gate, НЕ добавлять file consent для clipboard,
ручной .md writer — `handle_export_history:1449`;
`TranscriptWriter.write_transcript`:214–244 сам НЕ имеет policy gate
(mkdir/reserve/atomic write напрямую) — сохраняются ON-block у автоматических
recorder/import callers `recording_core_service.py:3615/3627` и `:3875/3883`
через `_should_write_plaintext_md:4428`; сам shared writer не является
существующим security boundary, новый manual route через него обязан передать
authorizer context и проверить его до первой mutation, grant автоматические
копии не разрешает. Карта ограничена перечисленными потоками; whole-diff gate
обязан проверить новые/переименованные sinks, а не считать таблицу вечным
allowlist.

### 7.6 IPC, trust boundary, capability/receipt

Имена зафиксировать одинаково в Python/Swift/reference:
`get_plaintext_export_policy {}` → `{epoch, policy_generation,
encryption_enabled, privacy_mode_enabled, allowed_without_grant}` (UNKNOWN —
явный отказ, без токенов); `grant_plaintext_export_session {app_session_id,
expected_epoch, expected_policy_generation}` → `{capability, epoch,
policy_generation}` только после явного async sheet в доверенном Swift;
`revoke_plaintext_export_session {app_session_id, epoch, capability}` →
идемпотентный revoke этой сессии, не отзывает другие сессии по одному ID;
`validate_plaintext_export {app_session_id, epoch, capability,
expected_policy_generation, operation_seq, sink_kind}` → одноразовый локальный
receipt для одной заранее выбранной Swift-записи. Существующие backend export IPC
получают namespace `plaintext_export: {app_session_id, epoch, capability,
expected_policy_generation}`; booleans `confirm`/`force`/`save_to_file` context
не заменяют. Стабильные reasons: `plaintext_confirmation_required`,
`plaintext_session_expired`, `plaintext_policy_unavailable`,
`privacy_mode_active`. Ни один отказ не возвращает ложный успешный path/file.

Проверенная trust boundary честна: `ipc_server.py:292/305` передаёт в service
только payload, `_handle_connection:313` не удостоверяет Swift/peer identity;
`ipc_constants.py:26` и chmod:204 устанавливают socket 0600; `service.py:3249`
дополнительно поддерживает optional HMAC signing, его runtime-состояние не
проверялось. Ни HMAC, ни handshake версии/capabilities (`IPCClient.swift:220`)
не доказывают клик человека; client_id/app_session_id/строка «Swift» не являются
Swift identity. Решение сейчас — trusted-client consent protocol, как явно
заявляет §7. Backend НЕ отличит headless self-grant от Swift sheet: same-UID
caller, удовлетворяющий существующей transport authentication (если включена),
способен вызвать grant RPC — не скрывать это за random UUID/challenge/confirm
boolean. Supported Swift вызывает grant только после sheet; supported
CLI/scripts grant автоматически не запрашивают. Headless export без выданного
context получает отказ. Это защита от accidental export, НЕ запрет всех
программных self-grant. Отдельный peer-auth/bootstrap дизайн с усиленной
гарантией в текущем approved narrow threat model отсутствует и не обещается;
если он потребуется — нужен отдельный дизайн и решение владельца (будущий BLOCK,
не часть A5.3).

Session capability многоразовая, scope фиксирован перечисленными
history-file/Obsidian sinks. Preflight receipt одноразовый и связан с
tuple(session, epoch, generation, seq, sink_kind) плюс неизменной локальной
closure(destination, content); как session grant или в другом sink его
использовать нельзя. `operation_seq` монотонен и сериализован shared
coordinator; backend хранит high-water в grant; повторный/меньший seq
отклоняется. После неопределённого IPC-результата локальной записи нет. OFF
тоже требует fresh validation для Swift (privacy/restart); отсутствие capability
допустимо только при подтверждённом OFF. Operation tracking привязать к RAM
app-session/epoch также в OFF. Не делать grant на get_history/read/show,
reconnect, settings load или по старому `confirm=True`. Capability forge в
export, replay epoch и receipt reuse запрещены; ручной self-grant malicious
same-UID честно вне гарантии.

### 7.7 Линеаризация, redaction, запреты модели

Swift coordinator сначала при необходимости показывает sheet «открытые файлы,
включая Quick Capture→Obsidian, до закрытия приложения/смены backend или
policy»; cancel ничего не выдаёт. Полученный grant позволяет много операций в
сессии, но НЕ заменяет свежую validation каждой записи. После NSSavePanel/рендера
зафиксировать destination + immutable content + `sink_kind`; выполнить preflight;
receipt потребляется ровно один раз в одной closure, не передаётся следующему
writer. Revocation BEFORE validation: запись запрещена. Revocation AFTER успешной
validation: разрешено закончить только эту одну запланированную запись, включая
её atomic temp/rename. Задержка ответа IPC после серверной validation границу не
меняет. Нет обещания атомарности IPC+локальный write или мгновенного отзыва уже
проверенного write. Backend writer применяет тот же принцип на каждом файле:
fresh authorization непосредственно перед первой связанной mutation; после неё
можно закончить этот файл, следующий требует новую проверку. Batch/Obsidian N
файлов = N validations; сначала deny до любого mkdir, затем повторные проверки
по файлам. Отзыв посередине даёт явный partial result, не rollback уже
разрешённых файлов. Backend Markdown + вторичная копия Swift UX — отдельные
операции; после отзыва вторая запрещена даже если первая завершилась.
Policy/epoch mismatch сбрасывает Swift grant; нет автоматического
regrant/retry после нового epoch — требуется новый явный sheet.

Bearer capability, operation receipt и app-session secret/ID запрещены в
logs/diagnostics/profile/settings/backups. Internal policy_revision не secret и
не bearer; его персистентность не исключение для grant. Точечная redaction
wiring: `service.py:3233` handle_request (warning str(exc), exception traceback),
`ipc_server.py:389` exception, `observability.py:182` `_sentry_before_send`;
сохранить `include_local_variables=False:402`. Никогда payload/params в logs;
auth errors — константные причины без repr token. Sanitizer обрабатывает
вложенные auth-key values в log/error/event; не логирует исходные
exception/request при сбое sanitizer. Не считать raw content в RAM нарушением
file-export policy. Logging-redaction проверяется отдельно от файловых export
gates.

Не изобретать другую модель доверия, policy fallback или единый флаг-разрешение
на весь backend. Не отменять независимое whole-diff review будущего кода. Не
обещать peer-auth. Не переносить устный PASS на новый SHA. Цена решения: удобный
session consent оставляет разрешённые plaintext-файлы на диске; revoke их не
удаляет; crash-token theft/same-UID и отдельные каналы передачи остаются честно
указанными границами.

## 8. Инвентарь и приёмка

Инвентарь запускается отдельно на согласованных каталогах, без сканирования
всего home, раскрытия текста и контактов. Отчёт: категория, относительный путь,
размер, тип артефакта/статус проверки, наличие plaintext/ENC1/unknown.
Неизвестные/недоступные каталоги не считаются пустыми. Логи не содержат transcript.
Для пользовательских файлов вне управляемых каталогов расширение поиска
согласовывается по конкретным путям. Внешние копии не перезаписываются автоматически.

Source gate: synthetic RED→GREEN для каждого journal writer/reader, crypto/IO
failures, compaction/restart, backup/restore, migration crash points, всех
export sinks, Swift Markdown/NDJSON и Quick Capture. Только mocked Python gate
не доказывает Swift wiring: требуется synthetic IPC/UI integration без запуска
второго production агента. Независимый adversarial review всего финального diff.

Release gate: exact head CI, merge, exact post-merge CI, resource/idle окно,
штатная release-процедура и health. Это не включает активацию флага.
Live activation gate: отдельное owner решение, согласованный инвентарь,
совместимый rollback release, подтверждённая recoverability ключа и проверенная
миграция. Живые записи/ключи до этого не читаются.

## 9. Порядок поставки

1. **A5.1 journals:** единый codec, все readers/counters, sidecar-only ENC1,
   compaction crypto-preparation и tombstone durability; source-only.
2. **A5.2 lifecycle:** защищённые legacy copy gates, multi-file transaction,
   encrypted snapshot backup/restore, recovery и inventory command на fixtures.
3. **A5.3 export session:** backend capability, Swift sheet/preflight и
   Quick Capture; проверка всех Python/Swift sinks.
4. **A5.4 release/acceptance:** совместимые сборки, exact CI, review, safe deploy;
   затем отдельная процедура решения по старым файлам и live activation.

Пункты не выполняются параллельно над `state_store.py` или `service.py`.
Независимый reviewer работает read-only; механика выполняется в этой задаче
последовательно. Ресурсоёмкие тесты/Swift build — только в свободное окно.
