# Ear — разбор backlog и диагностической достоверности

Scope: исходники, read-only GitHub issues и агрегаты логов, causal offline fixes.
Без публикации/CI/merge/runtime/STT/моделей/клона/OrbStack; тесты после Main START.
База isolated ear-full-lifecycle-1029: 9d9be174; remote16552643.

## Реестр наблюдений

| Источник | Покрытие / факт | Статус / следующий gate |
| --- | --- | --- |
| GitHub OPEN Pavua/Krab-Ear | total8, hasNextPageFalse; bodies8/8 + comments2/2 | исторические issues, не8 новых багов |
| #1965 | toast fade/orderOut, Aug30/31 | сверить текущий source/history, без Swift build сейчас |
| #1919 | preview/MLX-lock/audit tests | preview commit848d92589 уже в истории; MLX tests поддерживают bounded acquire/release; audit test требует сверки |
| #1909 | suite hang/owner leak hypothesis | нет текущего repro, отдельный lifecycle gate |
| #1819 | chunk contamination | нет текущего repro |
| #1795/#1783 | один cache-hit flake | вероятный дубль по test ID, причина из issue не доказана |
| #1775/#1774 | timeout/C-extension/mock loops | перекрывающиеся исторические reports, нужен source triage |
| backend.log | последние3000строк Oct28–29: WARNING30/ERROR0/traceback0 | ограниченная выборка, не global health |
| agent-recovery.log + .1 | tail3000 каждого: FATAL36/0; все36 ipc_connect_fail, Jul1–Oct20 | исторические, не новые падения Oct29; ERROR0 не означает healthy |
| scripts/backend_log_digest.py | absent path печатает healthy и exit0 | P2 candidate, causal CLI RED подготовлен |
| closeFalse hardexit/listener-replay/MemoryConductor | известные прошлой карточке source findings | не новые; runtime/drain не квалифицирован |

Последний подтверждённый cursor разбора backlog/логов не найден; поэтому новые
события от известных пока не отделены. API scan29Oct полный только для OPEN,
closed issues/PR discussions в эту квитанцию не входят. Sentry — центральный owner,
локально auth/DSN не сканируются. Личные записи и аудио не открывались.

## Карточка минимального исправления

- [x] Источник: main() missing-log branch, healthy/exit0.
- [x] Тест: test_backend_log_digest_cli.py; реальные отдельные CLI subprocess,
  scratch файл/отсутствующий путь, timeout10s, только stdlib/без app imports.
- [x] Main START: RED 1FAIL(missing0vs2)/1PASS(clean), 0.140s, exit1/reaped/PGIDempty/no timeout.
- [x] Минимально вывести UNKNOWN/exit2; existing-log поведение не меняется.
  Frozen digest58d115f7c43251bcfbbb9df837179900c645401158b4b00492b9c4d3255946b3,
  testa52d8f8e77db32451bd7e88c22aa2ffa5fba6c72970948aa13ff98960a4580e8.
- [x] Новый Main START → GREEN2: 2PASS/0.088s, outer0.205s, exit0/reaped/PGIDempty/no timeout; hashes стабильны. Main reviewed complete minimal source diff; diff-check PASS.
- [x] Сохранён реестр evidence/severity/repro/fix/tests/remaining gate; общий аудит продолжается.

Это отдельная диагностика, не исправление runtime. Следующие находки брать
отдельными causal карточками, не чинить по предположениям старых issue bodies.

## Доказательства и оставшиеся границы

RED receipt/raw stdout: artifact folder чата `ear-diagnostics-audit-20261029/`
`RED_RECEIPT.json` / `RED_STDOUT.log`. Изоляция: outer sandbox deny-network,
scratch HOME/TMP/cwd, Python3.12 -I-B, no application imports; это не универсальный
I/O-guard. Изменённые source hashes стабильны до/после. После отдельного свежего Main START GREEN выполнен: 2PASS/0.088s,
outer0.205s, pid91593 exit0/reaped/PGIDempty/no timeout/no interruption.
Raw GREEN_RECEIPT.json / GREEN_STDOUT.log сохранены рядом; exact hashes
стабильны. Ear RUN0, окно освобождено. Publication/CI/runtime не выполнялись.

Локальные планы Oct26–29 перечислены, но полный body-triage ещё не окончен;
BACKLOG-PAID-MODELS открыт как исторический Sep17 документ, его модельные
инструкции/рестарты не исполняются. Ear-specific self-improvement cursor не
найден; recovery logger — bootstrap журнал, не Telegram self-improvement loop.

GitHub coverage receipt: `ear-diagnostics-audit-20261029/OPEN_ISSUES_RECEIPT.json`.
Recovery stage классификация сопоставлена с literal source templates:
current tail app_launched2059/ipc_attempt341/success304/first_health260;
rotation app_launched2992/ipc_attempt4/success4. Все36 FATAL — ipc_connect_fail,
диапазон2026-07-01 02:15:41–2026-10-20 04:49:25 по timestamps самого лога.
Это historical range, timezone журнала не устанавливалась; не new Oct29 crashes.

## Дедуп по текущим исходникам (без новых прогонов)

| Issue | Прямое source/history доказательство | Решение / remaining gate |
| --- | --- | --- |
| #1919 preview | FakeRecorder.get_duration_sec/snapshot_range; commit848d92589 (#1920) | reported API mismatch исправлялся; current runtime/CI не проверены |
| #1919 MLX lock tests | тот же commit848d92589: AST допускает bounded acquire/release, FakeLock их реализует | reported stale-test shape устранена по source; новый тест не запускался |
| #1919 purge audit | commit3c46710a7 заменил wallclock test структурным; ee4c2145d уточняет бюджет | исходный issue не содержит assertion — нельзя доказать, что это тот же failure |
| #1795/#1783 | один InlineTranslationCacheTests.test_cacheHit_afterStore_returnsTrue, fresh localNSCache каждый test | объединить в одну triage группу; shared-singleton гипотеза не подтверждается source; eviction не воспроизведён |
| #1775/#1774 abbrev | test_dict_size_limit_allows_update... всё ещё loop до MAX_DICT_SIZE | историческая нагрузочная гипотеза; нужен отдельный bounded reproduction budget |
| #1775/#1774 denoiser | test_windowed_peak_memory... всё ещё8min audio, real scipy | потенциально тяжёлый pack; не запускать в текущем лёгком окне |
| #1774 action_items/mockcycle | нет точного testID в issue/comment | недостаточно данных для causal fix, требуется historical artifact/source map |
| #1965 toast | fadeOutAndHide содержит guard + animation completion→orderOut; test ждёт0.8s | наличие кода не доказывает firing timer/guard/completion; отдельная Swift source/TDD карточка |
| #1909/#1819 | teardown присутствует в актуальном test_backend_service, но inter-file pollution не воспроизведено | не объявлять весь chunk leak исправленным по одному файлу |

Автоматически GitHub issues не закрывались/не комментировались. Source-adapted
и verified-current — разные состояния. Для первого небольшого продолжения
рекомендуется source-map toast timer/guard/completion + deterministic scheduler
seam; bounded Swift test/build требует Main window и отдельного профиля.
