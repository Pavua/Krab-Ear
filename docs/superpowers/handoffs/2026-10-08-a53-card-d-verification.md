# A5.3 Card D — verification checkpoint, 2026-10-08

Статус: LOCAL GREEN после Linux fixture fix; independent Astra Ultra final
Linux delta + whole-D/matrix review PASS. Exact-SHA CI — в PR Checks.
Это не merge/deploy GO. Cumulative A/B/C base:
`886f061a5c5b2ab97f3b43afd7bcfd2867cdebee`; main:
`efecb801aae3f3c62fa03314ba4f5b142ac8e906` (повторный fetch выполнен).
Собственный D worktree; исходный чужой WIP сохранён по manifest, не изменён.
Production sources A/B/C (34 файла) побайтно совпадают с C source freeze.

## Входные ворота

- A `00ba3300`, B `d24f001a`, C `886f061a`: полный exact-SHA CI GREEN;
  по 27 checks, оба backend jobs SUCCESS. C три Swift jobs SUCCESS;
  свежий опубликованный job log подтверждает 156 wiring/source-contract tests.
- Local B/C fixture repair: 34 файла, 839 passed, 37 subtests, 1 skipped;
  independent Astra Ultra fixture delta review PASS.
- Whole-diff A/B/C production source/security PASS в явном narrow Card C scope.
  Один новый P2 committed fixture isolation был BLOCK и закрыт ниже в D.
- D pre-execution isolation и successive guard/dynamic-secret delta review PASS
  для CPU-only Python 3.12.11 launcher. Audio stub до parent pytest collection и
  повторно до child backend imports; MLX/torch/pyannote/gigaam отсутствуют локально. Новый import guard ниже
  делает fixture CPU-only также при установленных optional ML dependencies в CI.

## Выполненная матрица

| Сценарий | Наблюдаемый результат | Статус |
|---|---|---|
| Synthetic seed | Ровно 1 активная marker-запись, также после restart | PASS; RED 0 → GREEN 1 |
| Swift revoke-before | 0 actual writes; повтор callback запрещён | PASS |
| Swift validate→revoke→deliver | 1 actual write; повтор callback запрещён | PASS |
| Epoch/session/sequence replay | Повтор seq, другой session и старый epoch запрещены | PASS |
| Batch | Deny до mkdir; full 3 writes/3 gates; partial 1 write/1 gate и точный результат | PASS |
| Obsidian | Deny до mkdir; full 2 writes/2 gates; partial 1 write/1 gate и точный результат | PASS |
| Error/redaction/persistence | TypeError/internal_error, RuntimeError/invalid_request; socket/malformed/unknown/signing; actual logger→fake Sentry; settings/backup без auth | PASS |
| Swift raw-error envelope | После real validate: 0 writes, 1 validation, no retry, actual dynamic secrets вне streams | PASS |
| CPU-only imports | 6 ML roots и подмодули запрещены до loader; positive control работает, cached imports отклонены | PASS; 12 cases RED→GREEN |
| Formatter failure | Реальный pytest long/funcargs: stream helper и raw/call при EOF/timeout/decode; dynamic-token positive control | PASS; 6 transport cases RED→GREEN |

Integration первоначально: 8 passed после canonical-root fix; затем новый
formatter test PASS; затем 6 transport failure cases RED→GREEN. Требуемый `scripts/pre_merge_py312_check.sh` выполнялся для
обоих новых test-файлов: **27 integration + 1 isolation regression, ALL GREEN**.
Тот же собственный CPU-only venv, temporary pre-import noaudio hook удалён в
finally. Backend imports предварительно проверены: rebuild/install не запускались.
Это targeted Python 3.12 qualification, не полная Ubuntu/ML environment parity.
`make audit-all`: PASS. Ни один local ML/GPU/full-app build не запускался.

## Исправленные fixture defects и RED→GREEN

- `StateStore` constructor уже touch-ит journal. Старый existence seed давал
  0 active items; safe real IPC RED зафиксировал 0 вместо 1. Seed теперь
  проверяет активные записи при OFF и не дублирует marker при restart.
- urllib3 import пытался IPv6 bind; fake Sentry client пытался git release probe.
  Guard отказал до физического действия. В fixture IPv6 detection выключен и
  задан fixed release/server/environment; network/process запреты не ослаблены.
- Несвязанная startup binary-drift диагностика пыталась dwarfdump; guard
  отказал, fixture явно suppress-ит только эту диагностику.
- `/tmp` и `/private/tmp` на macOS: canonical root исправил false-zero counter,
  actual file counts/gates/partial assertions сохранены.
- Raw streams очищаются до failure; boolean-only inspection до каждого child
  stop/restart и после последнего Swift error; registry старого child не теряется
  до проверки. Sentry count требует конкретный dispatcher fault event.

- Final D review выявил транспортный failure-output P2: `raw/call` сохраняли
  auth-bearing arguments в traceback при EOF/timeout/JSON decode. Шесть
  formatter regressions сначала упали по утечке dynamic token, затем прошли
  после очистки args и безопасного исключения вне except без исходной цепочки.
  Полный D project parity повторён: 15 + 1 GREEN.

## Linux CI import-side-effect: исправлена изоляция D

У head `68a05ae9507669b2515be0ccda0cd2dc13618d4f` 25 checks прошли,
оба backend jobs упали только на новом D fixture. Установленные в Linux CI
optional ML dependencies выполняли import-time library discovery:
`ctypes.util.find_library` → `ldconfig`/`/dev/null`. Guard отказал до запуска
процесса; broad optional-import except сохранял violation до final assert.
Пакет верхнего уровня по ограниченному diagnostic stack точно не установлен.

Fixture теперь до первого backend import и до завершения child запрещает
`torch`, `mlx`, `mlx_whisper`, `pyannote`, `numba`, `cuda` и все подмодули.
`ModuleNotFoundError` включает существующий optional-dependency путь; cached
SDK в `sys.modules` вызывает явный отказ, без выгрузки native extensions.
Filesystem/process/network guard и final violation assert сохранены.

12 fresh-child behavioral cases: реальный import до installed-like stdlib
loader, root/submodule × 6 packages. RED: loader исполнялся; GREEN: 0 loader
runs. Positive control без fence исполняет тот же loader; повторный fence
отклоняет cached package. Реальные ML/GPU dependencies не устанавливались.
Новый project py312 gate: **27 + 1 GREEN**. Текущий CI status — в PR Checks;
результат прошлого SHA не засчитывается новой дельте.

## Закрытие P2 settings fixture isolation

Astra whole-diff audit выявил: helper отключал только LLM, а при free<5 GiB
DiskSpaceMonitor мог эмитить disk.warning в EventBridge и POST localhost REST.
В D helper явно отключает EVENT_BRIDGE_ENABLED и DISK_MONITOR_ENABLED при
construction. Flags после construction не запускают остановленные adapters.

Behavioral regression: real BackendService/DiskSpaceMonitor/EventBus/EventBridge,
fixed warning/critical thresholds и intercepted transport. RED: 1 post вместо 0;
GREEN: 0 posts, bridge disabled, disk thread отсутствует. Positive control
обычного backend доказывает живую low-disk→event→transport цепь без HTTP.
11 зависимых файлов: **248 passed, 21 subtests**; новый regression дополнительно
повторён после фикса thresholds/startup diagnostic. Independent delta PASS.
Этот test-only fix поставляется в D, не переписывает опубликованные A/B/C SHA.

## Границы доказательства и ресурсный gate

Child использует настоящий BackendService/dispatcher/authorizer/IPCServer и
synthetic профиль. Unix socket 0600; connect/connect_ex/bind ограничены своим
endpoint; datagrams/process spawning запрещены. HOME/temp/cache/outputs свои;
Keychain/crypto traps активны. Python guards — bounded defense,
**не герметичный OS/native sandbox**. При любой guard violation запуск/teardown
падает. Diagnostic metadata содержит только module/function/line/категорию/stage;
request/raw exception/settings values не выводятся.

Swift executable включает production IPCClient/coordinator и counting writer,
с отдельными HOME/TMPDIR/cache и module-cache-path. Production app не запускается.
Scanner проверяет реально выданные capability/session/receipt по каждому Swift
run; global secret registry остаётся до stop inspection; positive control
обязателен. Crypto provider запрещён даже при policy ON: actual encrypted-history
E2E этим plaintext synthetic fixture не доказан.

Полная local Swift release build пропущена по ресурсам: свежий swap snapshot
~17.3 GiB used. Малый standalone harness с production IPC/coordinator скомпилирован
и выполнен в integration/parity. Full Swift CI C GREEN; новый exact D CI ещё нужен.
Source/wiring и harness не исполняют настоящие SavePanel/PDF/MeetingReport/
QuickCapture UI handlers: production UI/live acceptance не заявляется.

Timeline UI заранее вне narrow Card C и не передаёт session context: при ON
backend отказывает даже после consent в другом окне. Это UX/release LIMIT,
не authorization bypass. Same-UID capability — контракт доверенного клиента,
не peer-auth и не доказательство UI click другому процессу.

Production lifecycle, включение шифрования, Main/Gateway, live history/Keychain,
удаление копий/ротация не выполнялись. Accessibility приёмка — отдельный блок.

## Оставшийся gate

Final independent Astra Ultra Linux delta + whole-D/matrix review PASS
на source freeze SHA256
`e4d8a3c511e0b68747848379abd7bc81c06372ec884c6b5d7bc6ca54fac46617`.
Открыт [D #2081](https://github.com/Pavua/Krab-Ear/pull/2081).
Git ancestry — поверх C; база PR — `codex/krab-ear-v2`, чтобы workflow CI
запускал полный набор guards (фильтр ci.yml привязан к этой базе).
[Отдельная дельта D относительно C](https://github.com/Pavua/Krab-Ear/compare/886f061a5c5b2ab97f3b43afd7bcfd2867cdebee...codex/ear-a53-d-20261008).
Следующий gate — полный exact-SHA GitHub CI. Локальный PASS и прежний SHA не заменяют новый CI. Merge/deploy не разрешены
этой приёмкой. Endor package-risk остаётся UNKNOWN из-за отсутствия авторизации;
новых dependencies для D не устанавливали.
