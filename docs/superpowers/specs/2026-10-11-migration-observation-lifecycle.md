# Наблюдение миграции: закрытие и повторное открытие панели

## Проблема и ожидаемое поведение

На базе `5d0ed47fa5d0602eda9ef23171b47f5e453c3407` закрытие HistoryPanelController
отменяет текущий SSE, но late start/status completions и scheduled retries могут
снова запустить поллинг/SSE или изменить переиспользуемую панель. Счётчик перед
confirmation sheet и callback самой sheet принадлежат тому же lifecycle.

Закрытие прекращает только UI-наблюдение, не backend migration. После close
никакой прежний completion/timer/sheet completion не отправляет новый IPC,
не планирует retry, не начинает/останавливает watcher нового окна и не меняет UI.
После reopen новый observation работает; повторный show уже открытой панели
не отменяет действующий observation. Существующие IPC методы и UI тексты остаются.

## Архитектура

Один `@MainActor` Foundation `HistoryMigrationObservationCoordinator` владеет
активностью, поколением открытия окна и polling state. В него переносятся
существующие orchestration callbacks, а не тестовая копия управляющей логики.
В adapter остаются AppKit rendering, off-main IPCClient.call и URLSession SSE.
Внедряемые границы: request/completion, delayed scheduler, watcher effects,
render и confirmation presenter. `HistoryMigrationOutcome` и
`HistoryMigrationTerminalWatcher` переиспользуются.

Каждый IPC, scheduler и confirmation completion захватывает поколение до
отправки/планирования и проверяет его на main actor перед состоянием или effects.
Close сначала инвалидирует поколение и закрывает observation, затем очищает
polling и останавливает watcher. Activate закрытого компонента открывает новое
поколение, повторный activate активного компонента идемпотентен. Панель имеет
один coordinator; независимый associated migratePolling Bool удаляется.

## Сохранить поведение

- Watcher перед migrate_history_encryption; started/already_running → polling.
- Refresh migrating=true подхватывает существующую миграцию; неактивный refresh
  ничего не отправляет. Старый refresh не перетирает уже начатое наблюдение.
- IPC failure/status migrating=true → повтор через 0.5 секунды.
- last_migration_status предпочтительнее SSE; при отсутствующем IPC terminal,
  plaintext=0 и watcher без terminal — прежняя пауза 1.5 секунды.
- done, backup_cleanup_failed, failed и partial продолжают использовать
  HistoryMigrationOutcome. UI layout/тексты/цвета и backend контракты не меняются.
- Confirmation после закрытия/reopen не показывает sheet и не запускает migrate.

## Доказательства и границы

Focused Swift tests удерживают реальные production request/queued callbacks,
включая success/error/terminal/confirmation и close→reopen A/B. Fake только
транспорт, scheduler и эффекты; проверяются эффекты настоящего coordinator.
RED должен показывать лишний effect, не compiler/missing-symbol failure.
Для extraction допустим behavior-preserving seam; положительные controls
подтверждают исходный порядок и исходы до lifecycle fix.
Same checks доступны XCTest и standalone swiftc; ограниченные source-contract
checks проверяют panel hooks. Это не живой NSWindow/URLSession/Keychain E2E.

## Ограничения

Source/PR/hosted CI only. Реальная история, Keychain, activation, миграция,
purge, live IPC/SSE, runtime и соседние репозитории не трогаются. Dependency
install, full local package build и lifecycle недопустимы при текущей нагрузке;
полная Swift build/tests выполняются GitHub-hosted CI. База remote колеи
origin/codex/krab-ear-v2=16d2b226, stack через незамерженные #2084 и #2085;
перед будущей интеграцией требуется проверка ancestry и новый exact-head CI
при rebase. Commit/stage только явных owned путей; no reset/stash/clean.
