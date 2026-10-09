# A5.4 — подготовка candidate/rollback и возврат исчезнувшего release

## Результат и scope, 2026-10-08

**RECOVERY APPLIED / POSTCHECK PASS. A5.4 CUTOVER HOLD.**

После явного owner разрешения 23:48 CEST восстановление выполнено в 23:54:01 CEST.
2644 tracked blobs/modes, Git HEAD/tree/status, fsync обоих parents и postcheck PASS;
Backend3957/REST3924/Swift2800 PID/start time/config не менялись, idle0/health200ok.
Никаких рестартов. SHA на диске — прежний registered pin; in-memory SHA UNKNOWN.
Затем существующий helper прошёл prepare+verify(before) приватного bundle1ebd→9fee,
config не применена. Swap вырос до ~27.9 GiB; build/restart пока отложены.

**Разделы ниже фиксируют первоначальную подготовку до применения.** Слова
«root отсутствует», «APPLY HOLD», «bundle отсутствует» относятся к этому checkpoint.

Работа этой карточки: metadata-only проверки, отдельные source-копии,
приватные копии plist и synthetic проверка атомарного переноса.
В первоначальной подготовке записей в отсутствующий live-root/LaunchAgents, restart/build-install, активации
шифрования, чтения live истории/Keychain, действий Main/Gateway не было.
[Общий план A5.4](2026-10-08-a54-release-acceptance.md) сохраняет отдельные gates.

## Что установлено, проверки начаты в 21:36 UTC

- Backend PID `3957` и REST PID `3924`, start time 08.10 02:43 local: loaded
  entrypoint и process command независимо указывают на
  `/Users/pablito/.codex/worktrees/ear-release-1ebd12eb/Krab Ear/KrabEar/`.
  Две filesystem-пробы: root, `main.py` и `backend/rest_server.py` отсутствуют
  (ENOENT); родитель содержит только `.codex-worktree-name`.
- Git registry сохраняет detached locked HEAD
  `1ebd12eb69695296a13c39eac35a9b7af3405ba5`. Это provenance для recovery,
  **не доказательство loaded module SHA**, который остаётся UNKNOWN.
  Причина исчезновения каталога UNKNOWN; Git lock не предотвращает удаление файлов.
- `prepare_release_cutover.py prepare` отказал exit 1 до создания bundle:
  `REFUSED: Не удалось проверить Git release`. Output bundle отсутствует.
  Не ослаблять helper и не подменять before plist для обхода проверки.
- В 21:40 UTC `safe_backend_restart.command --check-only`: IDLE/exit 0;
  REST `/health`: 200/status ok. Это один snapshot, не reservation,
  не quiet window REST, не restart-readiness и не доказательство loaded SHA.
- Swap ~19.8 GiB used. Full Swift build отложен, нового Swift package нет.
  Режим supervisor текущего Swift-сеанса, Sentry freshness и полный live wiring
  этой подготовкой не подтверждены.

## Что реально подготовлено

Root: `/Users/pablito/.codex/ear-release-staging/20261008/`.

| Артефакт | Проверка | Статус |
| --- | --- | --- |
| `rollback-1ebd12eb` | HEAD `1ebd12eb…`, tree `7f5c724dd48a29b9e7911665852d920d1a678269` | Clean/detached/locked, код не запускался |
| `candidate-9fee1ef4` | HEAD `9fee1ef4c2f5540636fd4faae1e53c93142054a5`, tree `0a294fb73efb7a7707177384e665f325fd274d36` | Clean/detached/locked, код не запускался |
| `config-backups` | Byte-exact original Backend/REST plist, SHA256 сверены | Private 0700/0600, live plist не изменены |
| `live-path-restore` | Git archive прежнего registered SHA, 2644 blobs+modes совпали | Prepared, fsync regular files/directories выполнен; APPLIED=false |
| `live-path-restore-manifest.json` | Frozen manifest; independent Astra exact entries/extras/symlinks | Source provenance PASS, не release GO |

Manifest SHA256: `db540b6bc64418bdd924a2a7a74a2c8af2454b330c2224c9189fe70e30d0b0b8`.
`.git` recovery payload указывает на original metadata `Krab-Ear7`; его HEAD
и обратный gitdir path соответствуют old root. Read-only explicit-worktree status
чист. Девять tracked symlink остаются внутри payload. Same-filesystem подтверждён,
original root всё ещё отсутствует. Plist и полные private artifacts не публиковать
и не передавать внешним агентам.

## Конкретное предложение восстановления — только после owner разрешения

Вернуть **прежний registered root**, не candidate `9fee1ef4`, без restart,
смены plist/settings или encryption. Восстановленные файлы могут немедленно
использоваться lazy imports, поэтому нужен отдельный owner scope, один оператор
и quiet window записи/meeting/REST. Independent Astra разрешает предложение
с условиями ниже; execution и cutover пока HOLD.

Перед commit заново проверить:

1. Scope владельца покрывает именно восстановление live source-path.
2. Frozen manifest и каждый blob/mode, original gitdir HEAD/mapping, private
   plist hashes, PID/start time/loaded paths прежние. Нет параллельного release/
   config-оператора; есть quiet window REST клиентов и свежий recording/meeting idle.
3. Original root отсутствует, включая broken symlink; parent canonical;
   filesystem совпадает. BUSY/UNKNOWN, изменившийся PID/config/payload или
   появившийся destination означают STOP, без overwrite/merge.
4. Подготовленные regular files и каталоги fsynced до переноса.

Commit: **macOS `renamex_np(staged_restore, original_root, RENAME_EXCL)`**,
один atomic directory move с отказом при существующем destination.
Installed SDK подтвердил `RENAME_EXCL=0x00000004`. Synthetic probe в частном
`/private/tmp`: existing destination → EEXIST, оба marker trees сохранены;
absent destination → полный успешный перенос. Production этим тестом не тронут.
Не заменять операцию обычным `os.rename`/`mv`; nonzero означает STOP.

После успешного rename fsync обоих parent-каталогов. **Rename — точка commit**:
ошибка fsync/postcheck после него означает APPLIED/HOLD, а не «ничего не изменено».
Проверить HEAD/tree/status и hashes на original root, прежние PID/start time/
loaded config/plist hashes, разрешённый metadata-only idle/REST health.
При отказе не удалять восстановленный live-root и не рестартовать автоматически;
остановиться для owner diagnostic. Loaded module SHA всё ещё UNKNOWN даже после
возврата exact старого tree.

## Дальнейший A5.4

После восстановления и postcheck вновь проверить исходный staging helper по
[существующему runbook](2026-09-29-safe-release-cutover.md). Не подставлять
исторические PID или повторно использовать старый idle snapshot.
Resource, supervisor/REST quiet, совместимость Swift/Backend, exact review/CI,
owner cutover, UI/crypto/activation gates остаются отдельными условиями.
Восстановление прежних исходников не означает выкладку A5.3 или A5.4 GO.

## Выполнено после разрешения владельца

Применение: same-filesystem `renamex_np(..., RENAME_EXCL)`, без fallback.
Повторены frozen payload/extras/modes, original gitdir/HEAD/mapping, root absence,
PID/start time/loaded paths, byte-exact original plist и idle. Ear5005 TCP clients
в момент commit — собственный Swift и REST; соседям направлено quiet-window
сообщение, после postcheck окно закрыто. Не заявлять TCP count доказательством
отсутствия всех возможных запросов; recording/meeting snapshot также не lock.

Private receipt: `restoration-result.json` в `/private/tmp/ear-a54-preparation-20261008/`;
постоянная копия receipt включена в private `cutover-prepared-evidence.json` в staging root.
Исходный frozen manifest сохраняет APPLIED=false как историческую запись подготовки,
актуальный receipt сообщает APPLIED=true/POSTCHECK=PASS. Не применять payload повторно:
staged_restore был перенесён и сейчас находится на original root.

Bundle: `/Users/pablito/.codex/ear-release-staging/20261008/cutover-1ebd-to-9fee`.
`prepare`/`verify(before)` exit0; old/new exact SHA и четыре private plist проверены.
Ни loaded launchd config, ни новые PID/код этими helper-проверками не меняются.
Готовность — **CONFIG STAGING PASS**, не A5.4 RELEASE GO. Далее resource window,
совместимый Swift package, supervisor/Sentry qualification, whole release gate
и свежее окно lifecycle. Новых live exports/Keychain/encryption действий не было.

Дополнительная qualification: startup marker `2026-10-08 03:39:12.765` в
AgentLogger sink совпал с start time Swift PID2800 и сообщает passive (Variant B).
Упорядочивать markers по времени: последняя прочитанная ротация может быть старее
основного файла. Gateway подтвердил, что в recovery окно дополнительных Ear REST/
STT/TTS запросов и Ear lifecycle/config изменений не делал; clone5105 отдельный.
Sentry freshness UNKNOWN: remote MCP настроен, но callable tools/read token в
этом harness не доступны. Отсутствие проверки не означает отсутствие ошибок.
