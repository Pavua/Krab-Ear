# Purge: граница удаления журналов и ключа

**Цель:** очистка истории не сообщает успех и не уничтожает ключ, пока в
управляемых журналах остаются данные под этим ключом; удалённые ID сохраняются
в durable permanent ledger, включая записи, появившиеся во время purge.

**База:** `31568b1fc1038496fe646ca3cf5e84c3bcf24951`, ветка
`codex/ear-purge-key-boundary-0929`. Только source и synthetic tmp-профили.
Прод-история, Keychain, флаги и процессы не изменяются.

## Подтверждённый дефект

После успешного первого compact другой писатель может добавить историю.
Финальный `_purge_wipe_managed_journals` не удаляет `history.ndjson`, когда
`compact_failed=False`; следующий shred делает новую ENC1-запись нечитаемой.
Одного безусловного unlink недостаточно: перед удалением необходимо сохранить
её ID и новые tombstones, иначе восстановление копии вернёт удалённый текст.

## Реализация и проверка

- [x] В `KrabEar/tests/test_a52c1_purge_integrity.py` воспроизвести late append
  после compact и доказать baseline FAIL при реально успешном fake-key shred.
- [x] Проверить late delete/tombstone и запрет resurrection из старой копии.
- [x] В `KrabEar/backend/history_service.py` сериализовать финальный сбор ID,
  durable plaintext ledger, очистку журналов и shred общим store lock.
- [x] При ошибке ledger/fsync/unlink сохранить ключ, вернуть partial failure;
  проверить последующее чтение и безопасный повтор операции.
- [x] Исключить раннее уничтожение данных, ещё не включённых в durable ledger.
- [x] GREEN: точечные регрессии, весь A5.2c1, non-Darwin simulation; связанные
  purge/snapshot/restore suites пофайлово, Python 3.12 без MLX, audit и lint.
- [x] Независимое adversarial-review итогового diff перед PR/приёмкой.

Не расширять эту правку на purge `.secrets`, внешние резервные копии,
активацию шифрования, FIFO startup или настройки соседних проектов.

## Связанный блокер crash-recovery при restore

Проверка `encrypted_snapshot._apply_verified_snapshot_locked` обнаружила,
что каталог staging синхронизируется, но его запись в родительском `data_dir`
не fsync'ится до первой замены живого журнала. После потери питания частичный
restore может остаться без обнаружимого recovery-маркера.

- [x] RED: в `test_a52b2_snapshot_restore.py` проверить durable COMMITTING и
  fsync родительского data_dir до первой live replace; инъекция отказа этого
  fsync должна оставлять все живые журналы нетронутыми.
- [x] GREEN: добавить необходимый parent fsync в существующую prepare-ветку
  `_apply_verified_snapshot_locked`, общую для restore и recovery.
- [x] Прогнать весь b2 и независимое review вместе с purge-границей.
