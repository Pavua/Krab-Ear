# STARTUP_MIGRATION_CARD — честный startup-лог миграции (SOURCE debt, не A5.3)

Scope: только SOURCE debt ложного success-логa. Не A5.3 app-session future architecture. Не менять migration policy / encryption / push_error callback / production вне задачи.

## 1. Подтверждённый дефект (read-only evidence)

- `Krab Ear/KrabEar/backend/service.py:1231-1248` (`BackendService.__init__`, W1034 block):
  ```python
  if self._data_migrator.check_migration_needed(self.store.data_dir):
      _plan = self._data_migrator.get_migration_plan(self.store.data_dir)
      logger.info("data_migrator: migration needed — plan: %s", _plan)
      _mig_result = self._data_migrator.migrate(self.store.data_dir)
      logger.info(
          "data_migrator: migration complete %s → %s "
          "(migrated=%d skipped=%d backup=%s)",
          _mig_result.from_version, _mig_result.to_version,
          _mig_result.items_migrated, _mig_result.items_skipped,
          _mig_result.backup_path,
      )
  ```
  Лог `migration complete` безусловный — не смотрит `MigrationResult.reason`.
- `Krab Ear/KrabEar/backend/data_migrator.py:54-65` (`MigrationResult`):
  `from_version, to_version, items_migrated, items_skipped, backup_path, reason: str | None = None`.
- `Krab Ear/KrabEar/backend/data_migrator.py:237-250,271-285` (`migrate`): при encryption ON возвращает `MigrationResult(..., reason=_ENC_OP_UNAVAILABLE)` без backup/rewrite.
- `Krab Ear/KrabEar/backend/history_encryption_policy.py:29`: `OPERATION_UNAVAILABLE_REASON = "history_encryption_operation_unavailable"`.
- `Krab Ear/KrabEar/backend/data_migrator.py:42-51` (`A5_2B_CALLER_SUCCESS_LOG_DEBT`): подтверждает класс — смешанный v1.0 + encryption-ON профиль, caller-side правка вне A5.2a, machine-readable сигнал уже есть (`MigrationResult.reason` / `handle_run_migration["reason"]`).
- Machine-readable уже проброшен в IPC: `data_migrator.py:359-366` (`handle_run_migration` возвращает `reason`).

## 2. Что чинить (caller-side only)

Только `service.py:1235-1244`: после `migrate()` проверить `_mig_result.reason`:
- `reason is None` → текущий `logger.info("data_migrator: migration complete ...")` сохранить.
- `reason is not None` → НЕ логировать `migration complete`; логировать отказ (`logger.warning`, с `reason`, без слов success/complete), продолжать старт с текущей схемой (существующий `except` вокруг блока не трогать по смыслу).
- Не менять: `DataMigrator.migrate` guard/policy, `history_encryption_policy`, `data_dir_policy_reader push_error`, encryption migrate/rollback, `check_migration_needed`/`get_migration_plan`.

## 3. Регрессионные тесты (behavioral, не AST)

Новый файл: `Krab Ear/KrabEar/tests/test_startup_migration_honest_log.py` (имя новое, существующего startup-честного-лога нет; ближайший аналог — `KrabEar/tests/test_data_migrator.py:1017-1105 DataMigratorStartupWiringTestCase`).

Фикстуры (переиспользовать, не изобретать):
- v1-запись: `test_data_migrator.py:94-101 _make_v1_item` (без tags/favorite/annotation) + `_write_ndjson`.
- Stub-сервис: `test_data_migrator.py:963-1039 _StubEngine/_StubTranscriber/_StubRecorder/_StubTranslator + StateStore(data_dir) + BackendService(store, recorder, transcriber, translator)` (лёгкие фейки, без ML).
- Профиль: временный OFF (`False` или отсутствие флага, паттерн `test_a52b2_snapshot_restore.py:82-83` инвертированный); ON-профиль / ENC1-сид НЕ требуются для этой caller-only регрессии. Keychain не трогать.
- Лог-перехват: только caller-логгер `KrabEar.Backend.Service` на пороге INFO (`caplog.set_level("INFO", logger="KrabEar.Backend.Service")` или `assertLogs("KrabEar.Backend.Service", level="INFO")`); затем отдельно assert `record.name == "KrabEar.Backend.Service"` для каждой matching-записи + explicit levels. WARNING-only capture запрещён — скрывает ошибочный INFO completion и ломает positive case. Нефильтрованный `caplog` запрещён — `DataMigrator` сам уже логирует warning с тем же reason, unfiltered-захват даст false-green. Logger filter + explicit levels mandatory.
- Cleanup: если `BackendService` сконструирован — обязательный `service.close()` в `finally`/`addCleanup` (conftest `_close_backend_services_created_in_test` добирает, но явный close required; см. `test_history_encryption_migration.py:759-766` паттерн `tearDown: service.close()`).
- Детерминированный caller-only фикстур (approved, без обязательного ENC1-сида): реальный `BackendService` через существующие стабы + временный OFF-профиль; patch `DataMigrator.check_migration_needed=True`, корректный plan, `migrate` возвращает реальный `MigrationResult(reason='history_encryption_operation_unavailable')`, assert called once. Success-вариант — `reason=None`. Это доказывает только caller result reporting, не real encryption policy / encrypted E2E; real policy покрытие остаётся на существующих тестах отдельно. No AST extraction / duplicate impl test.

Тест 1 — отказ не логирует success (RED до фикса):
- data_dir: временный OFF-профиль + v1 `history.ndjson` (1 запись `_make_v1_item`) + детерминированный стаб выше.
- сконструировать реальный service (stub-коллабораторы), перехватить логи startup на INFO-пороге caller-логгера.
- assert: `migrate` вызван once и вернул `reason == "history_encryption_operation_unavailable"` И в записях caller-логгера `KrabEar.Backend.Service` НЕТ `migration complete` на INFO вообще; ЕСТЬ refusal с reason (содержит reason, без слов success/complete, level WARNING). Каждую matching-запись проверить на exact caller `record.name` + explicit level. `service.close()` в finally. Current service всегда completion — поэтому RED немедленный.

Тест 2 — успех логирует completion (guard от инверсии):
- тот же v1 + OFF-профиль, но stub `migrate` возвращает реальный `MigrationResult(reason=None)`.
- assert: `migrate` вызван once И в записях caller-логгера `KrabEar.Backend.Service` ЕСТЬ `migration complete` на INFO (exact caller + INFO). `service.close()` в finally. History-мутацию (tags/favorite/annotation, schema/history changes) в этом stubbed caller-only тесте НЕ требовать — стаб не меняет history; real history-трансформации остаются на существующем `test_data_migrator.py`.

Разрешён именно этот детерминированный stubbing-вариант для немедленного RED; конфликтующий blanket-запрет 'no mock results' снят для этого варианта. Запрещено: AST/source-inspection тесты (`assert "reason" in source`), дубликат реализации в тесте.

## 4. RED → GREEN

- RED: добавить файл из §3, запустить per-file — Тест 1 падает (логирует `migration complete` при reason set), Тест 2 проходит.
- GREEN: минимальная правка §2 в `service.py`, перезапустить per-file — оба зелёные.
- Команды (cwd — корень worktree, где `KrabEar/`; verified venv Python 3.14.6 exit 0):
  - `PYTHONPATH="$PWD/KrabEar" "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest KrabEar/tests/test_startup_migration_honest_log.py -v`
  - `PYTHONPATH="$PWD/KrabEar" "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest KrabEar/tests/test_data_migrator.py -v` (регресс wiring)
  - Py3.12 parity: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_startup_migration_honest_log.py KrabEar/tests/test_data_migrator.py`.
- Отдельное independent review: свежий контекст проверяет diff `service.py` + новый тест-файл, без участия автора правки.

## 5. Missing facts (не изобретать)

- Verified (не missing): venv Python `"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python"` версия 3.14.6 exit 0; команды — с `PYTHONPATH="$PWD/KrabEar"` из корня worktree; Py3.12 parity — `scripts/pre_merge_py312_check.sh` с двумя точными файлами из §4.
- No mandatory ENC1 seed: карточка НЕ требует смешанный v1.0 + ENC1 + encryption-ON сид через реальный `policy_blocks`; approved caller-only regression — детерминированный вариант из §3 (реальный `BackendService` + временный OFF-профиль + patch `check_migration_needed=True` / корректный plan / реальный `MigrationResult(reason='history_encryption_operation_unavailable')`). Неразрешённая seed-зависимость удалена. Существующие тесты отдельно сохраняют real policy покрытие.
