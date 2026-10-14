# Passive history key — source handoff, 2026-10-14

База: `3c8a05bc67f51ef156034f4a37adf65f1807cec6`; ветка
`codex/ear-passive-key-20261014`. Исполнитель не делает commit/push/release.

## Результат и граница

Пассивный `StateStore._get_history_crypto()` читает только существующий ключ.
Missing key больше не создаёт замену при чтении ENC1, получении ключа для
snapshot или pending restore recovery. После возврата прежнего ключа тот же
StateStore повторяет lookup, включая прямого snapshot caller без decrypt.

`get_history_key()` отличает подтверждённое отсутствие (`HistoryKeyMissing`)
от повреждения/отказа/timeout. `get_or_create_history_key()` создаёт только
после этого исключения, без `-U`. No-argument `build_history_crypto()` и явные
запись/миграция сохраняют создание ключа. Формат ENC1, service/account,
dedicated setter, settings и UI production-код не меняются.

**Открыты:** прямой append с отсутствующим ключом рядом со старым ENC1,
generic activation через `set_settings`, полная политика смены поколений
ключей и live encrypted-history acceptance. Эта карточка закрывает только
пассивное создание ключа. Успешный crypto-кэш сохраняет прежнюю семантику;
проверка исчезновения уже закэшированного ключа не добавлялась.

## Воспроизводимость и проверки

Raw receipts и точные argv/env harness:
`.superpowers/sdd/2026-10-14-passive-history-key/` (ignored, локально).

- RED до production-правок: `test_state_store_encryption.TestStateStorePassiveKeyAccess`,
  8 тестов, 6 behavioral failures: пассивный путь вызвал успешный fake
  `add-generic-password`; не import/signature failure. Реальные random AES-GCM,
  StateStore и synthetic profile, единственная crypto-подмена `_run_security`.
- GREEN: main ENC1, sidecar-only, empty ON snapshot, pending recovery, прямой
  getter retry на том же Store, существующий/неправильный ключ, явная новая
  запись и direct migration. History/settings/restore-marker bytes проверяются.
- Guarded unittest, последовательно по файлам на existing Python 3.14 и 3.12:
  **140 тестов на каждой версии, PASS**. Список: crypto_keystore (19),
  history_crypto (20), state_store_encryption (22), encrypted_probe (6),
  encryption_flag (3), lock_reentrancy (8), shared_read_lock (13),
  history_encryption_migration (30), history_encryption_ipc (14),
  migration_terminal_status (5).
- Настоящий Unix IPC E2E: cold reopen с отсутствующим ключом отказывает;
  возвращённый прежний ключ на том же backend читает прежнюю запись;
  `provider_creates=0`, history/settings byte-exact. Python 3.14 — PASS.
  Использован существующий CPU-only IsolatedBackend с synthetic temp profile;
  production providers/builder/AESGCM, fake только security CLI seam.
  Python 3.12 — также PASS. Тот же новый E2E на архиве базы `3c8a05bc`
  с актуальными test fixtures: RED по правильной причине,
  `provider_creates=1`, ожидалось 0 (4.55s); исправленный 3.12 — 3.77s.
  Суммарный финальный GREEN: **549 unit/pytest + 1 IPC на каждой версии**,
  1100 успешных test executions на двух Python; это выбранный блок, не full suite.
- Guarded pytest, последовательно по файлам: **409/409 на Python 3.14 и
  409/409 на Python 3.12, PASS**. Legacy backup gate (42), encrypted snapshot
  b1 (69), restore b2 (157), disk guard b3 (40), journal encryption (28),
  backend plaintext sinks (41), irregular journal (17), encryption failclosed
  (13), плюс оба обязательных post-purge controls (1+1).
- `git diff --check` — PASS. `make -j1 audit-all`: **14/14 PASS** после
  первоначального freeze; root получил финальный external receipt.

Guarded harness устанавливает HOME/data/audit/settings-backup в throwaway до
imports, CPU import fence до приложения, запрет внешней persistent-записи,
сети и process spawn, внешний deny для реального `security`. Исключение для
fork есть только у существующего теста двух StateStore-процессов. Pytest
дополнительно разрешает только exact owned OS security-shim probe и read-only
`file -b <python executable>`/`uname -p` для platform diagnostics; `/dev/null`
разрешён как неперсистентный sink. Ни реальный security, ни ML worker spawn
этими исключениями не разрешены. Pytest
использует `--noconftest` с этими явными защитами; полный hosted CI обязателен.

## Неуспешный ранний локальный прогон

Прямой unguarded `python -m unittest
KrabEar.tests.test_history_encryption_migration -v` на Python 3.14,
2026-10-14 00:44:13–00:44:38 CEST, завершился `-6`/Metal assertion после
30 тестов с одним FAIL. **NOT PASS**. Включились несвязанные ML warmups.
Python 3.12 тот же файл 00:45:57–00:46:19 также имел один FAIL.

Старый `TestMigrateCryptoUnavailable` задавал только cached None, хотя сама
миграция уже на базе принудительно переинициализировала crypto. Без pytest
Keychain guard это могло вызвать живой lookup/read ключа. Точной CLI-телеметрии
не было; создание/изменение нельзя доказывать этим логом. Root отдельно
сообщил metadata-only проверку без `-w`/`-g`: item existed, created/modified
1 октября, признаков создания/перезаписи этим прогоном не обнаружено.
Исполнитель живой Keychain не проверял, не удалял и не ротировал.

Runner остановлен; own test-процессы завершились. Соседние/runtime процессы
не останавливались. Fixture теперь явно возвращает None из builder;
guarded повтор migration — 30/30 PASS на обеих версиях. Первые несовпадения
самого guard harness (import path/запрет нужного fork/перекрытый timeout mock)
не являются source PASS; исправлены и повторно проверены. Первые pytest
INTERNALERROR до коллекции (logging plugin вне throwaway) и два disk-guard
отказа на намеренном OS shim/platform probe также NOT PASS; scoped guard
исправлен, итоговый полный выбранный блок повторно GREEN.

## Следующий gate

Первоначальный head `b6c4b47` получил два одинаковых hosted CI FAILURE:
runs 38481104984 и 38481184605. Два purge fixture вызывали passive getter
до первой записи; warmup shape test получил `MagicMock` из сохранённого
default Transcriber alias. Оба класса воспроизведены guarded RED.

Follow-up меняет только тестовую подготовку: purge helper явно создаёт
synthetic ключ (`allow_create=True`), warmup handler test инъецирует
transcriber и содержит poison-alias regression guard. Production три файла
побайтно совпадают с reviewed `b6c4b47`. Полный purge-файл **100/100** и
warmup-файл **10/10** проходят на каждой Python-версии; poison-control также
GREEN на обеих. Первый локальный полный purge имел 99 PASS/1 harness failure:
облегчённый runner не сбрасывал audit singleton между тестами. После переноса
штатной autouse изоляции conftest в private runner полный файл повторён GREEN.
Это **220 дополнительных file-test executions**, не новый full-suite PASS.

Новый head и его exact-SHA CI отслеживаются в PR #2090. Старые FAILURE
не отменяются и не переименовываются в PASS; source acceptance, hosted CI,
merge и runtime приёмка остаются отдельными gates.

Root владеет независимым whole-diff review, commit/push, exact-head hosted
backend/Swift CI и последующим release decision. Локальные receipts не
доказывают CI, deployment/runtime или live encrypted-history acceptance.
Sol 6.1 High подходит для механики; Astra Ultra — для security whole-diff gate.
