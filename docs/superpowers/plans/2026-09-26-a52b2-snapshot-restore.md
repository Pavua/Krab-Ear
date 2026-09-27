# A5.2b2 — restore из снимка + recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> (или executing-plans). Шаги — чекбоксы.

**Goal:** Вернуть восстановление при `history_encryption_enabled=ON`: restore из
проверенного encrypted snapshot по commit-протоколу A5.2b1, с **объединением
deletion ledger** (tombstones ∪ permanent purged) и fail-closed recovery после crash.

**Architecture:** restore = три фазы под store-lock: (1) **pre-restore snapshot**
текущего состояния через уже проверенный b1-протокол (страховка для ручного
решения); (2) **применение** целевого снимка тем же протоколом (COMMITTING →
замены → read-back → COMMITTED); (3) durable restore-маркер. Recovery при
`COMMITTING` **докатывает проверенный целевой снимок** (спека §5.5: «commit
безопасный путь — завершить заранее проверенный encrypted snapshot»); если
докачка невозможна — fail-closed с машинно-читаемой причиной, **без** отката в
plaintext, **без** нового ключа, **без** запуска обычного обслуживания.

**Tech Stack:** уже в репо — `backend/encrypted_snapshot.py` (b1), `history_crypto`,
`history_encryption_policy`, `StateStore._lock`/append+fsync, `fcntl`. Новых
зависимостей нет.

**База:** `origin/codex/krab-ear-v2` (содержит #2056). Worktree:
`.worktrees/a52b2-snapshot-restore`, ветка `codex/ear-a52b2-snapshot-restore`.

**Канон:** спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5
(абзацы про restore/ledger union) + §5.1 (порядок шагов). Предыдущие карточки:
`2026-09-26-a52a-legacy-backup-gate.md`, `2026-09-26-a52b1-encrypted-snapshot.md`.

## Баны (playbook §1 + границы волны)

- `history_encryption_enabled` в проде **OFF**; карточка даёт код, который
  заработает после отдельного owner-«да». Никаких live-операций.
- Только synthetic tmp-профили + `os.urandom(32)` как тестовый ключ. **Никакого
  Keychain**, никакой реальной истории, никакой правки `settings.json` владельца.
- `KrabEar/backend/service.py` и `KrabEar/core/engine.py` — **не трогать**.
  `state_store.py` — **только** однострочное wiring точки входа recovery; никаких
  изменений существующей семантики.
- Точечные прогоны; никаких ML/GPU/полных сьютов.
- Не ослаблять гейты b1: `classify_backup_dir`, `unsupported_backup_format`,
  `published`-семантика pending, `REASON_*` константы.

## Принятые решения (зафиксированы, чтобы не переигрывать в реализации)

1. **Restore требует текущей policy ON.** Восстановление ENC1-снимка в OFF-профиль
   означало бы тихую расшифровку в открытый вид — понижение policy для данных,
   которые владелец шифровал. Поэтому OFF + снимок → честный отказ
   `snapshot_requires_encryption_on`, а не тихая расшифровка. (Обратное тоже
   верно: plaintext-снимок в ON-профиль → `snapshot_policy_mismatch`.)
2. **`settings.json` не восстанавливается никогда** в ON-пути. Запрос
   `restore_settings=True` → явный отказ `restore_settings_unsupported_at_on`
   (молча игнорировать явный запрос пользователя нельзя).
3. **Снимок с `policy_at_capture=off`, восстанавливаемый при ON** →
   `snapshot_policy_mismatch` **до первой записи**.
4. **Ledger union** собирается **до** подготовки выходных файлов, под тем же
   store-lock: `tombstones ∪ purged_ids` текущего профиля. Выходная история и
   связанные дельты фильтруются по этому множеству; в выходной ledger пишется
   **объединение** (старый снимок не может уменьшить нынешний ledger).
5. **Восстанавливаются все 10 журналов** реестра (с фильтрацией по union), а не
   только `history.ndjson` — набор обязан быть связным.
6. **Roll-forward, не roll-back.** При crash в COMMITTING recovery докатывает
   **целевой** проверенный снимок. Pre-restore снимок остаётся на диске как
   страховка и путь возвращается в ответе; автоматического отката из него нет.
7. **Ориентир на `published`, не на `state`.** Неопубликованный staging в
   `backups/.staging/` — мусор (`snapshot_stale_staging`); опубликованный каталог
   снимка с `COMMITTING` — незавершённая транзакция.
8. **Точка входа recovery** — ленивая: `StateStore.__init__` делает дешёвую
   проверку наличия restore-маркера и вызывает recovery только если маркер есть
   (одна строка wiring; тяжёлая работа — в отдельном модуле, тестируется без
   StateStore).

---

### Task 1: Верификация снимка + ledger union (read-only фаза)

**Files:**
- Modify: `KrabEar/backend/encrypted_snapshot.py`
- Test: `KrabEar/tests/test_a52b2_snapshot_restore.py`

- [ ] **Step 1: RED**
1. Валидный снимок + рабочий ключ → верификация проходит; возвращаются 10 файлов.
2. **Неизвестный формат манифеста / отсутствующий файл реестра / несовпадение
   размера или sha256** → отказ с машинно-читаемой причиной, **ни одного байта**
   в живых журналах не изменено.
3. **Чужой ключ** (снимок зашифрован другим ключом) → отказ, не частичная запись.
4. **Повреждённая строка** (tamppered ENC1) → отказ.
5. **Ledger union**: собирается `tombstones ∪ purged`; **при недоступном ключе или
   повреждении текущего ledger** restore прекращается без изменения файлов
   (спека: «При недоступном ключа или повреждении текущего ledger restore
   прекращается без изменения файлов»).
6. Union **не уменьшается** снимком: ID, которого нет в снимке, но есть в текущем
   ledger, остаётся в выходном ledger.

- [ ] **Step 2: Run** — Expected: FAIL (по правильной причине).
- [ ] **Step 3: Реализация**
```python
def verify_snapshot(*, backups_root, snapshot_dir, crypto) -> dict   # 10 имён + size/sha256 + decrypt-check
def collect_ledger_union(*, data_dir, crypto) -> tuple[str, ...]     # tombstones ∪ purged, fail-closed
```
- Оба — **read-only**, без mkdir/записи в живые файлы.
- Decrypt-check каждой строки каждого файла (тот же принцип, что сторож b1).
- Containment: `snapshot_dir` внутри `backups_root`; `..`/symlink внутри — отказ.
- [ ] **Step 4: GREEN** — [ ] **Step 5: Commit** `feat(a5.2b2): верификация снимка и сборка ledger union (read-only)`.

---

### Task 2: Применение снимка (commit-протокол) + restore-маркер

- [ ] **Step 1: RED**
1. Полный round-trip: снимок → живые журналы = 10 файлов, все строки ENC1,
   каждая расшифровывается в исходную; `restored_entries` честный.
2. Фильтрация union: записи из снимка, чей ID в union, **не** появляются ни в
   истории, ни в дельтах; resurrection невозможен.
3. Выходной ledger = union (не меньше текущего).
4. `settings.json` **не тронут** (побайтно), даже при `restore_settings=True` —
   вместо этого явный отказ.
5. Crash-окна: (a) после pre-restore снимка, (b) после COMMITTING до замен,
   (c) после замен до read-back — во всех трёх исходное состояние не выдаётся за
   успех, маркер переживает, `COMMITTED` недостижим без read-back.
6. **Два разных снимка подряд** не путаются (transaction_id в маркере).

- [ ] **Step 2: Run** — [ ] **Step 3: Реализация**
```python
def restore_encrypted_snapshot(*, data_dir, backups_root, snapshot_dir, crypto, restore_settings=False) -> dict
```
Порядок (спека §5.1):
1. дешёвые проверки (тип, containment, policy, ключ, отсутствие незавершённой операции);
2. под store-lock: re-check policy; **pre-restore снимок** через b1-протокол;
3. union; фильтрация; запись ENC1 в tmp + fsync; durable restore-маркер
   (`COMMITTING`, dot-prefixed) **до первой замены**; замены; read-back всех 10;
4. `COMMITTED` + честный `restored_entries`; путь pre-restore снимка в ответе.

- [ ] **Step 4: GREEN** — [ ] **Step 5: Commit** `feat(a5.2b2): применение снимка по commit-протоколу + restore-маркер`.

---

### Task 3: Recovery + wiring

- [ ] **Step 1: RED**
1. `COMMITTING` опубликованного снимка → recovery **докатывает** целевой снимок
   (повторная верификация + применение), `COMMITTED` после read-back.
2. Неопубликованный staging → `pending: false`, `snapshot_stale_staging`, **ничего
   не удаляется автоматически** (доказательство остаётся для владельца).
3. Повреждённый/недоступный целевой снимок → **fail-closed**: никакого отката в
   plaintext, никакого нового ключа, обычное обслуживание (backup) не стартует,
   причина машинно-читаема, путь pre-restore снимка в ответе.
4. Отсутствие маркера → recovery ничего не делает (no-op, дешёво).
5. Wiring: `StateStore.__init__` вызывает recovery **только** при наличии маркера.

- [ ] **Step 2: Run** — [ ] **Step 3: Реализация**
```python
def recover_pending_restore(*, data_dir, backups_root, crypto) -> dict
```
- [ ] **Step 4: GREEN** — [ ] **Step 5: Commit** `feat(a5.2b2): fail-closed recovery с докачкой + ленивый wiring`.

---

### Task 4: IPC-поверхность и наблюдаемость

- [ ] **Step 1: RED**: `handle_restore_history` при ON со снимком → успех с
  `restored_entries`/`pre_restore_snapshot`; при OFF-снимке/с чужим ключом/битом
  манифесте → отказ с причиной **до первой записи**; `settings.json` не тронут.
  (b1-тесты на `unsupported_backup_format` продолжают зелёные.)
- [ ] **Step 2–3**: реализация + документирование новых полей в
  `docs/IPC_API_REFERENCE.md`.
- [ ] **Step 4–5**: GREEN; commit `feat(a5.2b2): IPC restore/recovery + документация`.

## Гейты перед отчётом

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52b2_snapshot_restore.py -q
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_a52b1_encrypted_snapshot.py \
  KrabEar/tests/test_a52a_legacy_backup_gate.py \
  KrabEar/tests/test_backup_restore.py \
  KrabEar/tests/test_purge_toctou_w25.py \
  KrabEar/tests/test_data_migrator.py -q
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_state_store*.py \
  KrabEar/tests/test_history_service*.py -q
scripts/pre_merge_py312_check.sh KrabEar/tests/test_a52b2_snapshot_restore.py
make audit-all
.venv_krab_ear/bin/flake8 <изменённые source> --max-line-length=120 --ignore=E501,W503,E402
```

**RED-доказательство обязательно** для каждого Task (падение по правильной причине,
не ImportError). KEYCHAIN-ATTEMPTS должен остаться **0**.

## Что НЕ доказываем в b2

Живой restore при ON (нужен owner-флаг и реальный профиль), A5.2c inventory,
encrypted archive/versions, retention/disk-full guard (отдельная волна).
Source-only; флаг OFF; деплой — отдельным решением.

---

## Tracked risks (закрыто частично — обязательно к разбору следующей волной)

Ниже то, что **сознательно не чинилось** в b2. Всё это — известные, описанные
ограничения, а не сюрпризы для ревьюера.

### M1 — ридеры И ПИСАТЕЛИ журналов отдают/пишут рваный набор (HIGH, не чинился)

**Поправка к прежней формулировке (факт ревьюера, принято).** Раньше здесь стояло
«writers и restore это делают (B2)». Это было **неправдой**: гейт B2 накрыл только
писателя **снимков** (`build_encrypted_snapshot`), а не писателя **журналов**.
`add_history_item` маркер не проверяет и в окно pending пишет как обычно.

Состояния (актуально после H1/M4):

| Поверхность | Проверяет pending? | Чем закрыто |
|---|---|---|
| писатель снимков (backup/auto) | да | B2: отказ `snapshot_recovery_pending` |
| restore (ON-ветка) | да | B2 + M4: отказ `snapshot_recovery_pending` |
| restore (OFF/legacy `copy2`) | да | **M4** (раньше проходил молча) |
| **писатель журналов** (`add_history_item` и прочие sinks) | **НЕТ** | H1: потери больше нет (записи переносятся), но запись в рваный набор всё ещё возможна |
| ридеры (`count_active_items`, `get_history_page`) | **НЕТ** | не чинилось (ниже) |

Ситуация: crash посреди замен → recovery невозможен (снимок испорчен, ключ
недоступен или политика выключена) → маркер живёт, обслуживание заблокировано, но
`count_active_items()` и `get_history_page()` **продолжают читать смесь старых и
новых журналов** молча: часть записей не видна в выдаче без единого признака.
Видимость добавлена в `get_diagnostics.restore` и статус авто-бэкапа (H1, часть 2),
`handle_ping` намеренно не тронут — там закреплён bit-exact контракт.

Почему не чинил: гейт в горячем пути чтения — это новый отказ на каждом
`get_history_page`/`count_active_items` (а `count_active_items` зовётся из
`handle_ping` каждые 3 с). Ошибка «история недоступна» посреди диктовки хуже, чем
«две записи временно не видны», а цена решения (кеш флага pending в StateStore +
проброс во все read-пути) — отдельная волна с отдельными замерами.

Что предлагается в следующую волну: кэшированный в `StateStore` признак
«есть незавершённый restore» (ставится докачкой, сбрасывается ею же), явный
`reason: "restore_pending"` в `get_history_page`/`get_history_stats` и — что важнее
— предупреждение в UI, а не молчание. **До этого момента карточка считает
чтение «мягко неверным», и это надо проговорить владельцу.**

### H1 — окно pending растянулось вместе с M2 (закрыто, но с оговоркой)

M2 (вынос recovery из конструктора в точки обслуживания) был НЕ МОИМ решением и
устранял реальные дефекты (5 точек создания store, запуск recovery до
`init_sentry`/ErrorBus). Побочный эффект: окно pending выросло с «до рестарта
процесса» до «до следующего вызова backup/restore». В этом окне `add_history_item`
писал в рваный набор, а докачка перезаписывала его снимком — запись исчезала
бесследно (её нет ни в целевом снимке, ни в pre-restore страховке, ни в ledger), а
ответ был `ok: true, COMMITTED`.

Закрыто переносом: в докачке под тем же `history_flock` (поэтому набор стабилен —
append не может взять общий flock и падает громко) собираются записи, чей id
отсутствует в целевом снимке и отсутствует в `blocked`, и дописываются в
восстанавливаемый набор. Счётчики: `records_carried`, `records_at_risk`,
`records_excluded_deleted`, `records_unparsable` + `warnings`. Перенос работает
ТОЛЬКО на пути докачки: сознательный restore переносить не должен.

**Оговорка (остаётся долгом):** окно pending теперь не «момент», а интервал, и
запись в него разрешена. Следующий шаг — кешированный (НЕ per-append `iterdir`)
признак pending в `StateStore`, который (а) отдаёт явный отказ
`restore_pending_writes_at_risk` на append и (б) запрещает сознательный restore,
пока окно открыто. Требует отдельного решения по цене: append — горячий путь
диктовки.

### Вопрос владельцу: purge убивает и pre-restore страховку

`handle_purge_all_data` по явному действию владельца вызывает
`delete_history_key()` (волна `0afc3ff9`: выживший AES-ключ расшифровывает
pre-purge бэкап из Time Machine / iCloud / снапшотов APFS). Следствие, которое
**не было написано** и должно быть сказано владельцу явно:

> После privacy purge **все** довенные снимки, включая `snapshot_prerestore_*`
> (страховку текущего restore), становятся нерасшифровываемыми. Обещание
> «pre-restore снимок как страховка» НЕ переживает purge.

Поведение не менялось (это не волна A5.2b2) — изменена только документация:
оговорка добавлена в `IPC_API_REFERENCE.md` (restore/pre_restore_snapshot) и сюда.
Если владелец захочет иного, это отдельное решение: например, сохранять ключ
до явного подтверждения «стереть ключ шифрования».

### Остальное

- **Retention / disk-full guard.** Снимки и `snapshot_prerestore_*` при ON не
  удаляются (сознательно, инвентаризация — A5.2c). При заполнении диска restore
  честно падает с `snapshot_fsync_failed`/`OSError`, но **превентивного guard'а
  «нет места — не начинай транзакцию» нет**: pre-restore снимок уже записан, и
  останется ещё один. Нужен disk-guard перед фазой prepare.
- **UI-индикация.** `list_backups` по-прежнему показывает снимки как
  `restorable: false` (это закреплено b1-тестом) — при ON они уже
  восстанавливаются. Поле надо обновить вместе с UI-экраном бэкапов.
- **Нет IPC-пути к pre-restore снимку.** Путь возвращается в ответе
  `restore_history`, но открыть/проверить его из UI нечем: отдельного метода нет,
  а `service.py` в бане волны.
- **Нет отдельного диагностического метода для вердикта recovery.** Вердикт
  читается через `list_backups.restore_recovery` и
  `get_auto_backup_status.restore_recovery` — это костыль совместимости, а не
  нормальная поверхность.
- **Модуль вырос до ~2000 строк** (`encrypted_snapshot.py` = b1 + b2). Кандидат на
  разбиение `snapshot_restore.py`; гейт `audit-dead-modules` потребует
  production-импорта, иначе новый модуль сочтётся мёртвым.
- **Два конкурентных restore одного снимка оба успешны** (сериализованы `flock`,
  без порчи), но создают два `snapshot_prerestore_*`. Для двойного клика в UI это
  лишний мусор в `backups/`.
- **Аудит purge-coverage стал строже, но всё ещё статический.** Он теперь видит
  f-string семейства и ходит на один уровень в вызовах purge. Динамические имена
  файлов, собранные конкатенацией переменных (без констант), по-прежнему слепы.
- **`get_history_encryption_flag` fail-closed ON при битых settings** означает, что
  профиль с ENC1-журналами и потерянным `settings.json` не восстановится через
  b2 (потребует явного решения владельца, какой ключ/профиль использовать).
