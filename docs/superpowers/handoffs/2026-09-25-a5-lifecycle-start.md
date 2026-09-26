# A5.2 — вход в lifecycle, 25.09.2026

Статус: source-разведка завершена; реализация и новые tests не начаты.
Это handoff с проверенными механизмами, не исполнимая карточка и не новый
product approval gate. По уже разрешил продолжать разработку/PR/merge.
Scope live activation по-прежнему отдельный.

База `a0d101638e4b91d54f430df5e59bb7cf28214b92`, exact remote и оба CI зелёные.
Рабочая ветка `codex/ear-a5-lifecycle`; worktree
`/Users/pablito/.codex/worktrees/ear-a5-lifecycle/Krab Ear`.
Спека: `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`.
Карта: `docs/PROJECT_MAP_20260925_RU.md`; A5.1 повторно не реализовывать.

## Принятые границы

- Scope истории и десяти managed journals; общий PII audit позже.
- `history_encryption_enabled` остаётся OFF; tests только synthetic tmp
  профили и in-memory ключ, без system Keychain/копирования реальной истории.
- Plaintext export допускается по явному согласию до конца Swift+backend
  сессии; manual и Quick Capture → Obsidian. Это A5.3, не legacy backup grant.
- Старые plaintext-копии: inventory и отдельное решение владельца.
- A5.2 обязан закрыть копии, transaction/recovery, encrypted backup/restore и
  inventory. Одни guards не означают A5.2 DONE.

## Что нашли в source

| Файл / symbol | Текущий путь | Риск для A5.2 |
|---|---|---|
| `state_store.py:_history_journal_paths` | Явный набор десяти файлов | Переиспользовать, не вводить glob |
| `state_store.py:_read_encryption_flag_unlocked` | Invalid/missing settings при ENC1 fail-closed | Не заменять cached settings default False |
| `StateStore._lock` | Общий flock с per-thread reentrancy, SH/EX | Policy и sink в одном lock; без SH→EX |
| `ArchiveManager.__init__` | mkdir + два touch | Lazy storage creation; ON не должен ломать backend construction |
| `archive_items / unarchive_items` | Plaintext append / rewrite | До любых write; tombstones не менять при отказе |
| `TranscriptVersionManager.__init__` | mkdir/touch без StateStore | Нужен store policy wiring; raw direct API тоже входит в gate |
| `save_version / revert_to_version` | Plaintext append, cap/rewrite | Auto cap/orphan cleanup не переписывает legacy plaintext при ON |
| `AutoBackup._do_backup` | mkdir до store lock, затем copy трёх journals | Проверка до mkdir; managed snapshot позже охватывает все десять |
| `check_and_backup` | После backup делает prune и meta-write | ON не меняет старые backups/meta; отказ виден через status |
| `HistoryService.handle_backup_history` | mkdir/copy history+tombstones+status+settings без общего lock | Guard и snapshot под общим lock; пока encrypted snapshot не реализован — явный отказ |
| `handle_restore_history` | copy2 history/sidecars; optional settings | OFF-settings в backup не могут понизить текущую policy |
| `DataMigrator.migrate / rollback_migration` | Создают/чистят backup, raw schema rewrite и restore settings | Обязательный legacy обход; нельзя забыть из-за слова schema |
| `migrate_history_encryption` | Только основной history, plaintext .bak/rollback | Заменяемый legacy entry; это не готовая multi-file transaction |

Исходники подтверждены на tree `a0d10163`; scout использовал `83b2d62`, чей tree
идентичен `a0d10163` (`git diff --stat` пуст). Main agent перечитал policy,
конструкторы, manual backup/restore, auto backup и schema migration.

## Наблюдаемость отказа

- Backend startup и RecordingCore каждые 100 транскрипций игнорируют результат
  auto backup и подавляют exception. Поэтому возвращаемый `skipped_reason`
  нужно также видеть через `get_auto_backup_status`.
- BulkReprocessor ловит ошибку `save_version`, продолжает текстовое обновление;
  нельзя заявлять, что версия сохранена. Нужен машинно-читаемый warning/result.
- Version orphan-cleanup и hooks compaction ловят ошибки. Gate автоматического
  rewrite не должен превратиться в скрытый plaintext-write через fallback.
- Owner purge/clear_all — отдельное разрешённое уничтожение данных по явной
  команде; запрет нового plaintext не блокирует уже существующий purge.
- Schema startup ожидает `MigrationResult`, а не произвольный failure dict.
  Нужна адаптация результата без ложного success-log.
- Swift manual backup показывает response как diagnostics; явный backend
  reason уже доступен. Auto-backup status — отдельная обязательная проверка.

## Порядок следующей реализации

1. Подготовить узкую исполнимую карточку A5.2a и review контракта lock/order.
   Первый независимый кусок — manual/auto legacy backup+restore: отказ при ON
   под существующим store lock и наблюдаемый статус. OFF поведение сохраняется.
2. Тот же контракт довести до archive/version/schema-migration и direct APIs.
   Не объявлять copy gates закрытыми до всех sinks и constructor/cleanup paths.
3. A5.2b — manifest/state machine encrypted multi-file snapshot, recovery и
   restore с union нынешних tombstones/purged IDs. Rollback к plaintext запрещён.
4. A5.2c — scoped inventory, типы/containment/symlinks, unknown как unknown.

Каждый merge — source-only до полной приёмки. Не проводить параллельные
изменения `state_store.py` или `service.py`. Механика последовательно;
независимый Astra High reviewer на конкретный законченный security diff.

## RED случаи для карточки A5.2a

- ON: ни mkdir/touch/copy/append/rewrite/prune/meta-update, исходные bytes
  и deletion ledgers неизменны. Проверять direct manager и IPC входы.
- Повреждённые settings, неверный тип flag, missing settings с ENC1 только
  в sidecar и ошибка policy read дают отказ без Keychain.
- Restore `restore_settings=True`, OFF-policy в backup: текущая policy и
  history не изменены до/после отказа.
- Retention overflow: ON сохраняет прежние backup paths/bytes/meta; status
  содержит `history_encryption_operation_unavailable`, backed_up=false.
- OFF→ON через второй StateStore до получения lock: повторная проверка
  блокирует sink; OFF контрольный путь работает без deadlock.
- Archive/version construction в ON не создаёт storage, но backend assembly
  завершается. Auto cap/orphan rewrite блокирован; explicit purge сохранён.
- Schema migrate/rollback при ON не backup/copy/rewrite/prune. OFF regression
  остаётся зелёным; no-op current schema не выдаёт ложного создания backup.

Зависимые suites: `test_backup_restore.py`, `test_auto_backup*`,
`test_archive_manager*`, `test_transcript_versioning*`, `test_data_migrator.py`,
`test_purge_toctou_w25.py`, `test_history_encryption_failclosed_a5.py`.
Следующий исполнитель превращает эти случаи в полные tests/код карточки;
данный handoff не выдаётся за уже выполненный RED/GREEN.

## Ресурсы после reboot и наблюдения

25.09 03:22–03:31 CEST: macOS 27.2, load1 201.68→17.28, swap 5954→8630.69 MiB,
117 GiB available APFS Data. Тяжёлые локальные тесты не запускались.
Backend/rest argv и release HEAD — `bc09490f`; ping/REST health отвечают,
recording=false, meeting=false, wake running=true/wedged=false.
Все проверенные runtime flags encryption/cloud/rewrite/semantic/preload OFF.
Свежий Sentry agent AppHang 01:17:43Z; REST timeout25s/exit70 в 03:20 CEST,
новый REST в 03:21, позже STT HTTP200. Причины не установлены.
Эти наблюдения не дают разрешения на новый lifecycle restart или live recording.

## Документирование и расходы

Карта и этот handoff — docs-only локальная ветка; отдельный тяжёлый CI цикл
ради моментального снимка не нужен. Включить применимый lifecycle handoff
в следующий source PR, обновив факты. Динамический machine snapshot не
публиковать автоматически как публичный product status.
