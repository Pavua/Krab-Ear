# История: безопасное получение ключа и включение шифрования

## Цель и подтверждённые дефекты

Не создавать новый ключ рядом с существующим `ENC1` и не сохранять переход
OFF→ON без успешной свежей проверки криптопровайдера. Это отдельная source-карточка
поверх reviewed Whisper head `19f5deaf60c92c5b771f89d376e0b9e32e5dbf3e`
(#2091, merge `86a0b1978086bb4e2d9bbbf7505a264cd2abf928`), включающего
merged passive-key #2090. Реальное шифрование у владельца остаётся OFF.

Независимая source-проверка базы `cdfcf8ee` подтвердила два MED:

1. При включённом шифровании, пустом process cache, отсутствующем Keychain item
   и существующем `ENC1` append мог создать замещающий ключ. Старый шифротекст
   уже недоступен без прежнего ключа; новый ключ добавляет записи другого
   поколения без key ID. Это риск восстановления/доступности, не plaintext leak.
   Dedicated enable и migration имели прямые creating-builder обходы Store.
2. Generic settings, import и backup restore сохраняли OFF→ON без проверки
   ключа, в отличие от dedicated enable. Последующий append отказывал закрыто,
   но политика уже была сохранена как включённая.

Дополнительные обязательные случаи: dedicated `enabled` принимает только bool;
строка `"false"` не превращается в True через `bool(...)`. Явный repair битых
настроек в ON тоже требует проверки: fail-closed чтение неизвестной прежней
политики как True не доказывает, что ON было сохранено корректно.

## Инварианты

- Единый registry `_history_journal_paths()` содержит все десять управляемых
  журналов. Creating lookup разрешён только под существующим exclusive flock
  после проверки registry; `ENC1`, unreadable/irregular файл или invalid UTF-8
  допускают только lookup существующего ключа. Неизвестное состояние не равно
  пустому журналу.
- Passive read сохраняет shared flock и lookup-only. Нельзя безусловно брать
  exclusive lock внутри getter: shared→exclusive upgrade намеренно запрещён.
- Settings commit сначала проверяет CAS, сырой тип, repair permission, итоговые
  настройки и размер сериализации. Только затем fresh provider preflight для
  proposed ON, если прежнее сырое значение не было exact True либо вызывающий
  dedicated endpoint явно требует повторной проверки ON.
- Unrelated settings commit при уже достоверном ON не обращается к Keychain.
  Warm process cache не заменяет обязательную fresh проверку при activation.
- Key acquisition и migration используют единый Store guard. Ошибка проверки
  сохраняет прежние settings, revision, journal bytes и post-save hooks.
- Prepare не публикует проверенный ключ в рабочем cache. При отказе записи до
  `os.replace` settings и crypto cache остаются прежними: missing-settings ветка
  append иначе могла бы использовать новый ключ после отказавшего activation.
- Legacy restore готовит и проверяет settings один раз **до первого copy2**.
  Guard создания ключа видит объединение live и prospective journals; прежний
  guard ON→OFF продолжает учитывать prospective journals. После копирования
  применяется уже подготовленная сериализация без второй проверки провайдера.

## Существующие точки расширения

`KrabEar/backend/state_store.py`:

- `_acquire_history_crypto_unlocked(*, history_paths: tuple[Path, ...] | None = None)`:
  свежий builder, lookup-only при ciphertext/неизвестных journal bytes;
  вызывается только под существующим EX.
- `_prepare_settings_commit_unlocked(...)`: validation/serialization до
  activation preflight; внутренний `force_history_crypto_preflight=False`.
- Prepare возвращает явную тройку `(settings, serialized, crypto_candidate)`;
  `None` означает отсутствие fresh preflight. Никакого скрытого pending cache.
- `_commit_prepared_settings_unlocked(settings: dict, serialized: str, crypto_candidate)`:
  прежняя atomic commit семантика без повторного prepare. Ключ публикуется после
  возврата `atomic_write_text` (успешный replace), до отдельного directory fsync.
  Ошибка последнего не откатывает уже видимую policy и соответствующий cache.
- `_get_history_crypto(allow_create=False)`: passive SH путь не меняет режим;
  creating путь использует existing reentrant EX и общий guard.
- `migrate_history_encryption`: получение ключа под EX, без прямого builder
  вне journal guard.

`SettingsService.handle_set_settings(..., *, force_history_crypto_preflight=False)`
передаёт внутренний флаг к Store; это не новая пользовательская настройка.
Dedicated enable и migration в `backend/service.py` не создают ключ напрямую.
`HistoryService` использует подготовленный commit в legacy restore.

## Проверка и границы доказательств

Нужен proper RED на неизменённой production-базе и GREEN на Python 3.12/3.14:
все десять журналов; отсутствующий ключ; успешное ошибочное создание как RED;
unreadable/invalid journal; generic/dedicated/actual isolated IPC/import/restore;
warm cache; repeated ON; raw non-bool; stale CAS; corrupt-policy repair;
legacy restore до copy2 и ровно один preflight; failed pre-replace commit при
missing/corrupt policy не публикует ключ; успешный activation заменяет старый
warm cache свежим проверенным ключом. Сохраняются положительные
fresh creation, plaintext migration и восстановление прежнего ключа в том же Store.

Каждая IPC-фикстура владеет уникальным коротким temporary socket root и своим
`SocketOwnershipClaim`; readiness требует owned `LISTENING` и живой thread.
Две одновременно живые fixtures с разными profiles должны читать разные
истории. Cleanup освобождает server/thread/claim/tempdir и закрывает service.

Все profiles, provider stubs, sockets и данные временные. `BackendService`
закрывается; реальные Keychain, история, ML, аудио и сеть не используются.
Нужны dependent regression, lint/diff-check, 14 audit gates, independent
Astra Ultra whole-diff review, exact-head CI и отдельный post-merge CI.

Keychain и settings file не образуют общую транзакцию: после успешного создания
ключа и отказа disk commit допустим неиспользуемый ключ. Компенсирующее удаление
ключа запрещено. Карточка не восстанавливает уже потерянные/смешанные ключи и
не доказывает, что найденный ключ расшифрует каждый прежний `ENC1`.

Merge/deploy разрешены владельцем, но эта карточка не входит автоматически в
подготовленный release #2090+#2091. Activation, real migration, purge/rotation,
соседние проекты и OrbStack/VPN не меняются. Sol 6.1 High — implementation/CI;
Astra Ultra — независимый security gate.
