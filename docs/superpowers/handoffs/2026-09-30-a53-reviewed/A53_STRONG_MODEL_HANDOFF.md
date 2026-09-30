# A5.3 — независимый контрактный gate и handoff
Дата: 2026-09-30. SOURCE-ONLY; production/история/Keychain не читались, IPC не вызывался.
Проверена чистая база `/Users/pablito/.codex/worktrees/ear-safe-cutover/Krab Ear`, HEAD `b91cff7c81175e67e6a70e2caa946a518f2037cf`.
Входной runtime `1ebd12eb`, encryption OFF — историческая справка координатора, не live verification.
Источники: AGENTS.md, NOW/EXECUTOR_PLAYBOOK, A5 design §7 и ограниченное чтение перечисленного кода.

## Вердикт
**BLOCK для исполнения одной только нынешней §7 слабой моделью: неполна карта writer’ов и порядок policy-revocation.**
**GO для реализации после внесения нижеследующего обязательного уточнения в spec и карточки.**
Это gate дизайна, НЕ source acceptance, merge/deploy approval или разрешение encryption ON.
A5.2b snapshot/restore/diskguard не переаудированы; их gates не ослаблять.
Нового решения владельца по известным policy-вопросам не требуется: session consent включает Quick Capture → Obsidian.

## Зафиксированный scope и реальные пробелы
- Защищаем файловые plaintext-производные Ear history/journals и явно разрешённый Quick Capture → Obsidian.
- Encryption ON: запрет по умолчанию; Privacy ON: grant не помогает. Encryption OFF: прежний export UX, но свежий privacy gate обязателен.
- История на экране, `get_history_page`, read-only render и наличие расшифрованного текста НЕ выдают разрешение.
- Clipboard/диктовка/Notes/iMessage/внешняя передача остаются отдельной policy. Не объявлять защиту всех PII.
- Подтверждено кодом: `history_service.py:4383/4635/4658` export_obsidian имеет privacy gate, затем mkdir/write без capability.
- Подтверждено: `obsidian_sync.py:319/361/398` прямой sync не имеет privacy/capability gate; `handle_sync:547` проверяет только privacy.
- Подтверждено: `history_service.py:5623/5679/5708/5714/5806` batch создаёт каталог и пишет sibling-форматы напрямую.
- Подтверждено: `main+QuickCapture.swift:713` шлёт `run_obsidian_sync` с `force:true`, без session context, ошибки игнорируются.
- Подтверждено: локальные Swift writers ниже пишут кэшированный текст после async UI/рендера без backend preflight.
- Это отсутствующая A5.3 реализация, а не найденный обход уже реализованной capability.

## Карта обязательных sinks (пути ниже относительно проверенного checkout)
| Владелец | Источник / конечная запись | Решение |
|---|---|---|
| `KrabEar/backend/history_service.py` | export_history:1449, selected:1830, _finalize_srt_export:1998, JSON:2166, CSV:2293 | Auth до mkdir/temp/write; сохранить render-only варианты |
| тот же | Obsidian:4658, HTML:5989 и alias generate_html_report | Оба HTML alias при save_to_file ведут к одному gate; render-only без file consent |
| тот же | batch_export:5623 + _export_csv_to_dir:5748 | Проверка до bundle mkdir и отдельная validation каждого файла; прокинуть context делегатам |
| `KrabEar/backend/service.py` | export_timeline_svg/json/ical:5566/5620/5669, чтение StateStore history | В scope как производные истории; gate ДО _resolve_timeline_export_dir:5433 (он делает mkdir) |
| `KrabEar/backend/obsidian_sync.py` | sync и handle_sync; direct manager callers | Инъекция общего authorizer; отсутствие authorizer = deny; force не меняет policy |
| `KrabEar/backend/export_scheduler.py` | check_and_export:413 → _do_export:164, StateStore history → temp/fsync/replace | Таймер не получает чужой grant; ON запрещён до mkdir/pruning, включая direct _do_export |
| `KrabEar/backend/sharing_manager.py` | _fetch_items:609 читает history → prepare_share:215 → _persist_package:736/751 | Локальный plaintext package — файловый sink; gate direct prepare/persist; grant не разрешает внешнюю публикацию |
| `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+History.swift` | .md:141, .ndjson:192 из self.items; дополнительная backend-копия:158 | Две записи = две validation; отказ первой не запускает вторую |
| тот же каталог, `HistoryPanelController+ExportSelection.swift:325` | export_selected_items → локальный файл | Fresh preflight после NSSavePanel |
| тот же, `HistoryPanelController+ActionItems.swift:230/266` | get_history_page → Markdown action items | В scope; прочитанная история не consent |
| тот же, `HistoryPanelController+MeetingMode.swift:267` | get_meeting_report; service.py:4895 явно отчёт одной записи history | В scope; inject shared session coordinator также в standalone report VC |
| тот же, `HistoryPanelController+StatsReport.swift:98` | generate_stats_report → StatsReportGenerator читает active history (stats_report.py:734) | В scope как history-derived отчёт |
| тот же, `AnalyticsDashboardViewController+PDFExport.swift:110` | generate_html_report с полными транскриптами → PDF во temporaryDirectory | В scope; fresh validation ПОСЛЕ renderer callback; temp тоже plaintext sink |
| тот же, `main+QuickCapture.swift:713` | полученный текст заметки → run_obsidian_sync(items) | Явно включён владельцем; consent общий в рамках этого app-session |

### Проверенные границы карты
- `HistoryPanelController+CallAssist.swift:465`: payload приходит из VG HTTP (`call_assist_service.py:963`), не из Ear history; отдельный кандидат внешней policy, A5.3 не расширять на него молча.
- `HistoryPanelController+Import.swift:594/642`: operational queue report (счётчики, источники, ошибки), не transcript export; вне narrow scope.
- `HistoryPanelController.swift:2532` glossary CSV, `+ConfigPresets.swift:627`, settings export, logger/plist writers — вне narrow history scope.
- `handle_export_history_markdown:1560–1724` только render/optional pbcopy; не файловый sink, не вызывает TranscriptWriter. Сохранить privacy gate, НЕ добавлять file consent для clipboard. Ручной .md writer — `handle_export_history:1449`.
- `TranscriptWriter.write_transcript:214–244` сам НЕ имеет policy gate: mkdir/reserve/atomic write. Известный caller — `recording_core_service.py:3615/3627`; import пишет напрямую:3875/3883. Оба caller’а используют `_should_write_plaintext_md:4428`, сохраняем их ON-block, grant его не отменяет.
- Archive/versions/backup сохраняют собственные ON-block. Новый manual route через TranscriptWriter обязан передать authorizer context и проверить его до первой mutation; не утверждать, что leaf уже безопасен.
- Локальный share package попадает в файловый scope по происхождению из history; существующая внешняя share-link policy не получает новых полномочий.
- Карта ограничена перечисленными экспортными потоками; whole-diff gate обязан проверить новые/переименованные sinks, а не считать таблицу вечным allowlist.

## Обязательный контракт authorizer и согласованного policy snapshot
1. Backend epoch = случайные 32 bytes на новый BackendService/process; app_session_id = UUID на запуск Swift, capability = secrets.token_urlsafe(32). Grants/receipts только RAM, в settings/UserDefaults/Keychain/logs/Sentry не попадают.
2. Grant привязан к profile/service, app_session_id, epoch и локальному policy_generation плюс полному snapshot fingerprint. Не глобальный boolean.
3. Новый `PolicySnapshot` имеет enum `KNOWN_OFF | KNOWN_ON | UNKNOWN`, отдельно privacy bool, fingerprint и reason; bool ошибки НЕ кодирует. ON означает только явно валидный true, а не «reader считает ON при сбое».
4. Authorizer сам не инициализирует/чинит настройки: отсутствующий файл, не-object/битый JSON, duplicate policy keys, нечитаемый/нерегулярный файл, отсутствие любого privacy/encryption ключа, не-exact-bool, missing/invalid internal revision → UNKNOWN.
5. UNKNOWN немедленно очищает grants/receipts, увеличивает локальный generation и запрещает grant И validation с `plaintext_policy_unavailable`. Восстановление файла не возвращает старое согласие; новый sheet обязателен при ON.
6. Не менять A5.2 `read_history_encryption_flag`: он намеренно объединяет ON и ошибку в True и missing без ENC1 в False. Не использовать его bool как новый typed snapshot; отсутствие flag никогда не трактовать OFF в authorizer.
7. Startup/settings initialization ДО authorizer сохраняет явные bool-defaults только для достоверно нового профиля: data_dir создан этим startup с exist_ok=False, нет восстановленного/унаследованного состояния. Уже существующий missing/incomplete profile НЕ объявлять новым.
8. Startup initializer (владение карточки A, не authorizer) берёт exclusive StateStore._lock заново; если caller держит shared, сначала полностью отпускает его, затем exclusive и повторное чтение/проверка. Запрещён SH→EX upgrade и сохранение прежнего snapshot после reacquire. Legacy file с обоими валидными bool, но без revision: обычная startup migration под store lock сохраняет те же flags и добавляет internal revision, без grant. Legacy missing/invalid flags остаётся UNKNOWN до обычного validated settings repair/save; export UI сообщает «Настройки требуют восстановления», никаких defaults из authorizer.
9. Новый internal `_plaintext_export_policy_revision` = uuid4().hex, генерируется центральным settings commit при КАЖДОЙ поддержанной записи, даже с теми же значениями. Входное/backup значение игнорировать. Это не secret/capability, а durable версия против ON→OFF→ON между процессами.
10. `StateStore.save_settings` и общий `_save_settings_unlocked` сохраняют существующие A5 write guards, затем записывают flags+новую revision одним atomic replace. Import/restore/reset/recovery, которые меняют settings, обязаны пользоваться этим commit; callback не является единственным revoke механизмом.
11. Конкретный bypass для карточки A: legacy `HistoryService.handle_restore_history:5456` делает copy2(settings). Заменить settings-часть на validated commit под тем же StateStore lock; не вкладывать save_settings/history_flock нового FD друг в друга. Encrypted snapshot restore settings уже запрещает (`encrypted_snapshot.py:3034`), этот запрет сохранить.
12. Snapshot читать на КАЖДОМ issue/validate/backend sink под `StateStore._lock` того же профиля. Затем брать authorizer lock; никогда наоборот. Для manager без store инъецировать callback этого store, не новый независимый history_flock. Ошибка/timeout захвата → UNKNOWN+revoke, без cache fallback.
13. `_read_plaintext_policy_snapshot_unlocked` требует удерживаемый store lock: open O_RDONLY|O_NOFOLLOW|O_NONBLOCK, fstat regular, читать максимум 16 MiB; превышение → UNKNOWN. fstat до/после и финальный lstat должны совпасть по dev/ino/size/mtime_ns/ctime_ns; иначе UNKNOWN, без retry по старому snapshot.
14. Fingerprint = `(profile identity, st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, SHA256(raw_bytes), internal_revision)`; JSON flags и revision разобрать ИЗ ЭТИХ bytes. Не читать hash/stat/flags разными cached путями. SHA не логировать вместе с содержимым.
15. При любом fingerprint mismatch отозвать прежние grants ДО validation; обновить remembered snapshot/generation. При valid snapshot вернуть `plaintext_session_expired` для старого context; UNKNOWN вернуть policy_unavailable. Даже same-bool atomic external replacement консервативно отзывает.
16. Межпроцессная точка commit — atomic replace flags+revision под общим flock; validation сравнивает snapshot под этим flock. Процесс B не очищает RAM A напрямую, но первая validation A гарантированно увидит новую revision поддержанного commit и откажет старому grant.
17. Не обещать полную обнаруживаемость злонамеренного same-UID, восстанавливающего revision/metadata или обходящего locks. Raw recovery replacement вне commit ловится fingerprint; supported recovery обязана выдавать новую revision. Partial/failed restore → UNKNOWN при неконсистентном snapshot, не fallback OFF.
18. A5.2 mock/no-store OFF fallback сюда не копировать. Authorizer создаётся/подключается ДО scheduler/manager threads. Privacy true всегда deny даже при KNOWN_OFF; KNOWN_ON+privacy false требует grant.
19. Normal Swift quit отзывает grant best-effort; backend shutdown очищает RAM; crash не гарантирует немедленный отзыв украденного токена. Никакого global active-session для таймера: Quick Capture передаёт context своего Swift.
20. Консервативный trade-off: любое settings save/replacement, даже unrelated key, может потребовать нового consent. Это намеренное безопасное поведение A5.3, а не обещание «только смена двух bool отзывает».

## Конкретный новый IPC (имена зафиксировать одинаково в Python/Swift/reference)
- `get_plaintext_export_policy {}` → `{epoch, policy_generation, encryption_enabled, privacy_mode_enabled, allowed_without_grant}`; UNKNOWN отдаёт явный отказ, без токенов.
- `grant_plaintext_export_session {app_session_id, expected_epoch, expected_policy_generation}` → `{capability, epoch, policy_generation}` только после явного async sheet в доверенном Swift.
- `revoke_plaintext_export_session {app_session_id, epoch, capability}` → идемпотентный revoke этой сессии; не отзывать другие сессии по одному открытому ID.
- `validate_plaintext_export {app_session_id, epoch, capability, expected_policy_generation, operation_seq, sink_kind}` → одноразовый локальный receipt для одной заранее выбранной Swift-записи.
- Swift operation_seq монотонен и сериализован shared coordinator; backend хранит high-water в grant. Повторный/меньший seq отклоняется. После неопределённого IPC-результата локальной записи нет.
- OFF также требует fresh validation для Swift (privacy/restart); отсутствие capability допустимо только при подтверждённом OFF. Operation tracking привязать к RAM app-session/epoch также в OFF.
- Существующие backend export IPC получают namespace `plaintext_export: {app_session_id, epoch, capability, expected_policy_generation}`; booleans confirm/force/save_to_file не заменяют context.
- `plaintext_confirmation_required`, `plaintext_session_expired`, `plaintext_policy_unavailable`, `privacy_mode_active` — стабильные reasons. Ни один отказ не возвращает ложный успешный path/file.
- **Проверенная trust boundary:** `ipc_server.py:292/305` передаёт в service только payload; `_handle_connection:313` не удостоверяет Swift/peer identity; `ipc_constants.py:26` и chmod:204 устанавливают socket 0600. service.py:3249 дополнительно поддерживает optional HMAC signing; его runtime-состояние не проверялось. Ни HMAC, ни handshake версии/capabilities (`IPCClient.swift:220`) не доказывают клик человека; client_id/app_session_id/строка «Swift» не являются Swift identity.
- **Решение сейчас:** trusted-client consent protocol, как явно заявляет §7. Backend НЕ отличит headless self-grant от Swift sheet. Same-UID caller, удовлетворяющий существующей transport authentication (если включена), способен вызвать grant RPC; не скрывать это за random UUID/challenge/confirm boolean.
- Supported Swift вызывает grant только после sheet; supported CLI/scripts grant не запрашивают автоматически. Headless export без выданного context получает отказ. Это защита от accidental export, НЕ запрет всех программных self-grant.
- Если требуется запрещать и ручной headless вызов grant RPC, исполнение этой усиленной гарантии **BLOCK**: нужен отдельный peer-auth/bootstrap дизайн и решение владельца, существующий транспорт доказательств согласия не даёт. В текущем approved narrow threat model дополнительного owner choice нет.
- Session capability многоразовая, scope фиксирован перечисленными history-file/Obsidian sinks. Preflight receipt одноразовый и связан с tuple(session, epoch, generation, seq, sink_kind) плюс неизменной локальной closure(destination, content); его нельзя использовать как session grant или в другом sink.
- Не делать grant на get_history/read/show, reconnect, settings load или по старому confirm=True. Capability forge в export, replay epoch и receipt reuse запрещены; ручной self-grant malicious same-UID честно вне гарантии.

## Линеаризация и отложенный UI
- Swift coordinator сначала при необходимости показывает sheet «открытые файлы, включая Quick Capture→Obsidian, до закрытия приложения/смены backend или policy»; cancel ничего не выдаёт.
- Полученный grant позволяет много операций в сессии, но НЕ заменяет свежую validation каждой записи.
- После NSSavePanel/рендера зафиксировать destination + immutable content + sink_kind; выполнить preflight; receipt потребляется ровно один раз в одной closure, не передаётся следующему writer.
- Revocation BEFORE validation: запись запрещена. Revocation AFTER успешной validation: разрешено закончить только эту одну запланированную запись, включая её atomic temp/rename.
- Задержка ответа IPC после серверной validation не меняет эту границу. Нет обещания атомарности IPC+локального write или мгновенного отзыва уже проверенного write.
- Backend writer применяет тот же принцип на каждом файле: fresh authorization непосредственно перед первой связанной mutation; после неё можно закончить этот файл, следующий требует новую проверку.
- Batch/Obsidian N файлов = N validations; сначала deny до любого mkdir, затем повторные проверки по файлам. Отзыв посередине даёт явный partial result, не rollback уже разрешённых файлов.
- Backend Markdown + вторичная копия Swift UX — отдельные операции; после отзыва вторая запрещена даже если первая завершилась.
- Policy/epoch mismatch сбрасывает Swift grant. Нет автоматического regrant/retry после нового epoch; требуется новый явный sheet.
- Bearer capability, operation receipt и app-session secret/ID запрещены в logs/diagnostics/profile/settings/backups. Internal policy_revision не secret и не bearer; его персистентность не исключение для grant.
- Точечная redaction wiring: service.py:3233 handle_request (warning str(exc), exception traceback), ipc_server.py:389 exception, observability.py:182 _sentry_before_send; сохранить include_local_variables=False:402. Никогда payload/params в logs; auth errors — константные причины без repr token. Sanitizer обрабатывает вложенные auth-key values в log/error/event; не логирует исходные exception/request при сбое sanitizer.
- Не считать raw content в RAM нарушением file-export policy. Logging-redaction проверяется отдельно от файловых export gates.

## Последовательные карточки и ownership (каждая отдельный bounded PR)
A. **Контракт/authorizer**: new `KrabEar/backend/plaintext_export_authorization.py`, `state_store.py` typed snapshot/internal revision/central commit, `settings_service.py` startup normalization и supported writes, settings-only branch `history_service.py:5456`, `service.py` init/IPC wiring; new `test_plaintext_export_authorization.py`; IPC reference/spec; ограниченная secret redaction в `ipc_server.py`, `observability.py`, `service.py`, её regression tests.
B. **Python sinks**: `history_service.py`, `obsidian_sync.py`, `export_scheduler.py`, `sharing_manager.py`, timeline handlers в `service.py`; явный authorizer context до writer. `handle_export_history_markdown` оставить render/clipboard без file consent; auto-callers `recording_core_service.py:3615/3875` сохраняют ON-block; не переиспользовать негейтированный TranscriptWriter для manual route без context. Нельзя начинать до принятого A API; один владелец service.py.
C. **Swift**: new `PlaintextExportCoordinator.swift`; один RAM instance в `main.swift`, inject в перечисленные history/report/PDF controllers и QuickCapture; normal quit revoke. Следовать async sheet, не runModal.
D. **Интеграционная приёмка**: synthetic profile + injectable/fake IPC transport + temp outputs, затем изолированный реальный backend IPC + Swift writer harness без запуска production app; проверка API keys и actual writes.
A/B/C не раздавать параллельно с перекрывающимися файлами; общий checkout с foreign WIP не трогать. База будущей реализации — свежий origin/codex/krab-ear-v2, не этот старый SHA вслепую.
Исполнитель: доступная сбалансированная модель (Sol Medium или подтверждённый рабочий эквивалент), 1 карточка за раз. Whole-diff/security acceptance: Astra High.

## RED→GREEN: обязательные meaningful негативные тесты
1. Параметризовать КАЖДЫЙ файловый history export route+alias с его file-writing params, formats/selected/batch, timeline, local share, direct sync, direct scheduler: ON без context → причина отказа, нет mkdir/temp/open(write)/copy/prune и новых файлов в tmp fixture.
2. `confirm=True`, `force=True`, `save_to_file=True`, malformed/missing/wrong-session/wrong-epoch token и неизвестный sink_kind не обходят gate; privacy ON блокирует даже корректный grant.
3. Direct manager tests вызывают sync/prepare_share/_persist_package/_do_export с synthetic данными, минуя dispatcher; injected authorizer отсутствует/кидает → deny, не OFF fallback.
4. Отдельный тест текущей trust boundary фиксирует возможность same-UID вызвать grant RPC напрямую; НЕ писать ложный acceptance «headless impersonation denied». Supported script-path обязан не вызывать grant автоматически. Свежий BackendService имеет новый epoch; replay старого grant/receipt отклонён; новый app_session_id после simulated crash не принимает старый token. Read-history/show/settings-status не создают grants.
5. Через каждый supported update path (set/import/restore/reset/direct save) проверить изменение revision и отзыв, включая ON→OFF→ON без export между переходами; входной/backup revision никогда не сохраняется.
5a. Обязательная cross-process fixture: multiprocessing spawn, A/B независимые StateStore одного TemporaryDirectory без ENC1 (чтобы OFF был допустим). A issue grant при ON; Pipe/Event сообщает ready; B save OFF, затем ON, оба через поддержанный commit, сообщает done; первая validation A старого grant → session_expired и 0 writes. Никаких общих Python store/mock и sleep.
5b. Отдельный process B атомарно заменяет settings теми же flags и той же revision/bytes; A ловит новый fingerprint и отказывает старому grant. Повторить поддержанную settings restore/recovery с новым revision. Это synthetic fixture, не работа с реальной историей.
5c. Удержать store lock в B, запустить validation A, затем commit/release через barrier: A обязана прочитать новый consistent snapshot, либо bounded timeout→UNKNOWN; не старый cache. Deadlock/inverted locks → timeout test failure; children terminate/join в finally только свои fixture PID.
6. Удалить settings отдельно из valid OFF и valid ON; corrupt/non-object/duplicate keys/non-bool/missing flag/revision/unreadable/nonregular/oversize/unstable snapshot/provider exception → typed UNKNOWN, issue+validate deny+revoke. Вернуть прежние bytes → старый grant всё равно deny. Fresh explicit initialization и valid legacy revision migration → known state; incomplete legacy остаётся UNKNOWN до validated repair.
7. Swift held SavePanel: получить content/grant, изменить privacy/encryption или backend epoch, закрыть panel → ноль writes. Повторить renderer callback для PDF и standalone MeetingReport VC.
8. Детерминированные barriers: revoke до validation → 0 writes; validation затем revoke затем deliver reply → ровно 1 запланированный write. Повтор receipt/callback/seq → 0 дополнительных writes.
9. Batch/Obsidian: остановить между файлами, revoke, продолжить → первый допустим, остальные не созданы; partial result точен. Отдельно deny до bundle/vault mkdir при начальном отсутствии grant.
10. Swift history .md + backend copy: первая validation не разрешает вторую копию; после отмены/ошибки первой нет скрытого второго export.
11. Quick Capture: ON нет consent → note history сохраняется штатно, vault не меняется, UI показывает причину; valid session → write; force не обход; смена epoch требует новый sheet.
12. Таймер scheduler + любой активный чужой grant → ON deny до mkdir/prune; auto recorder/import .md/archives/versions/backup остаются заблокированы при valid session grant.
13. KNOWN_OFF+privacy false сохраняет export; privacy true/UNKNOWN deny. `export_history_markdown` render/clipboard при ON не требует grant и не выдаёт его; privacy gate прежний. Сохранить schema parity read/render-only ответов и errors без секретов.
14. Исполняемые Swift tests используют fake transport и counting writer, а не только source contains. Python service fixtures обязательно close(); не запускать all-tests/ML/GPU ради gate.
15a. Sentinel-секреты разных значений прогнать через ACTUAL handle_request и socket connection error paths: malformed params/JSON, unknown method с auth в params, unauthorized signing, mocked handler RuntimeError и неожиданный exception с token в exception text. Capture log records+formatted traceback, fake Sentry transport через before_send, diagnostics serialization: ни один sentinel не появляется; request payload не логируется; ошибки не echo token. Никаких внешних Sentry событий.
15b. Проверить normal settings/profile/backup serialization содержит только internal revision, не grant/receipt/session ID; Swift logs/error.localizedDescription тоже без sentinel.
15. В isolated IPC E2E проверить real dispatcher→authorizer→manager; synthetic текст-маркер появляется ТОЛЬКО в разрешённом fixture output. Это не production/encrypted-history E2E.

## Финальный gate после кода
Проверить весь diff, новую карту sinks, typed UNKNOWN/central commit/revision и cross-process snapshot/lock ordering, all callback paths, токены в логах, минимальную сериализацию receipt, no swallowed denial и отсутствие grant bypass через lower-level writer.
Обязательны targeted тесты обоих языков, Linux/Python3.12 parity изменённых tests, audit-all по проектному регламенту и Swift build при доступных ресурсах; затем exact-SHA CI.
Не переносить устный PASS на новый SHA. Без whole-diff independent review и применимого isolated E2E — source BLOCK, даже при unit GREEN.
Deploy/restart/encryption activation/реальная история/удаление прежних plaintext-копий не разрешены этим документом.
Цена решения: удобный session consent оставляет разрешённые plaintext-файлы на диске; revoke их не удаляет; crash-token theft/same-UID и отдельные каналы передачи остаются честно указанными границами.

Counter-review закрыт в контракте: UNKNOWN отделён от ON; ложная Markdown/TranscriptWriter проводка исправлена; two-process ABA/recovery fixtures и consistent fingerprint/revision обязательны. Это изменения документа, не результаты тестов реализации.
