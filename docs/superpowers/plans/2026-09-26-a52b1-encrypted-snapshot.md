# A5.2b1 — encrypted snapshot (создание) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> (или executing-plans). Шаги — чекбоксы для трекинга.

**Goal:** Вернуть backup при `history_encryption_enabled=ON` как **согласованный
encrypted snapshot** всех десяти журналов с durable-манифестом, вместо отказа
`history_encryption_operation_unavailable`.

**Architecture:** Новый модуль `backend/encrypted_snapshot.py` владеет всем
snapshot-протоколом: реестр 10 журналов → построчное шифрование в private
staging → fsync → durable manifest (фиксированные имена, размер + sha256
**ciphertext**, transaction id, состояние, версия) → `COMMITTING` → повторная
проверка fingerprint источников → замена → read-back всех файлов → `COMMITTED`.
Никаких plaintext-файлов, никаких хэшей plaintext, никаких новых секретов.

**Tech Stack:** уже в репо — `cryptography` (AESGCM через
`backend/history_crypto.py`), `fcntl` (тот же `history.lock`), `hashlib`,
`json`, `shutil`. Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2` @ `4bf1c4e9`. Worktree: `.worktrees/a52b1-encrypted-snapshot`,
ветка `codex/ear-a52b1-encrypted-snapshot`.

**Спека (канон):** `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5
(шаги 1–6, абзацы про backup/manifest). **Handoff:** `2026-09-25-a5-lifecycle-start.md`.
**Предыдущая карточка:** `2026-09-26-a52a-legacy-backup-gate.md` (её гейты не ослабляем).

## Баны (playbook §1 + границы волны)

- `history_encryption_enabled` в проде остаётся **OFF**; карточка даёт код пути,
  который сработает после отдельного owner-«да». Никаких live-операций.
- Только synthetic tmp-профили + случайный тестовый ключ. **Никакого Keychain**,
  никакого доступа к реальной истории владельца, никаких правок `settings.json`
  владельца.
- `KrabEar/backend/service.py` и `KrabEar/core/engine.py` — **не трогать**.
- `state_store.py` — только re-export/контракт, без изменения существующей семантики.
- MacBook может быть под нагрузкой: только точечные прогоны, никаких полных сьютов
  и ML/GPU-тестов.
- `git add` явными путями. Секреты не печатать и не коммитить.

## Скоуп b1 (что НЕ входит)

- **Restore** (проверка snapshot+key, ledger-union, commit при восстановлении) — **b2**.
- **Recovery после crash** на `COMMITTING` — **b2** (в b1 `COMMITTING` пишется
  атомарно, но авто-докатка не реализуется; b1 обязан оставлять систему в
  fail-closed состоянии с явным признаком, а не «успехом»).
- A5.2c inventory, archive/versions encrypted, export/import.

---

### Task 1: Модуль snapshot + manifest (write-only протокол)

**Files:**
- Create: `KrabEar/backend/encrypted_snapshot.py`
- Test: `KrabEar/tests/test_a52b1_encrypted_snapshot.py`

- [ ] **Step 1: RED — тест на манифест и полноту набора**

Тест обязан проверить: (а) снимок содержит **ровно 10** файлов реестра;
(б) каждая строка в снимке `ENC1:` и **дешифруется тем же ключом** в исходную
строку (побайтовое соответствие); (в) manifest содержит `version`,
`transaction_id`, `state`, и по каждому файлу `name`/`size`/`sha256` — **и не
содержит** plaintext, ключа и sha256 от plaintext (отдельно: подставь sentinel
строку, проверь, что её plaintext-хэш не встречается в манифесте).

- [ ] **Step 2: Run — убедиться в RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52b1_encrypted_snapshot.py -q
```
Expected: FAIL (модуля нет / поведение не реализовано).

- [ ] **Step 3: Реализация**

```python
SNAPSHOT_MANIFEST_VERSION = 1
STATES: PREPARED → COMMITTING → COMMITTED
def build_encrypted_snapshot(*, data_dir, backup_dir, crypto, transaction_id, policy_on) -> dict
```
Контракт:
- Реестр — **только** `state_store.history_journal_paths(data_dir)`. Никаких
  glob'ов, никакого `settings.json` в payload (спека: settings отделены и не могут
  понизить policy).
- Каждый журнал читается построчно; `ENC1:`-строки проверяются **дешифрованием**
  (tampered/malformed → отказ, а не молчаливый skip); plaintext-строки
  **шифруются**. Пустой/отсутствующий файл → валидная пустая запись реестра.
- Staging: приватный каталог `0700` внутри `backup_dir`, запись только ENC1.
- Fsync каждого файла + каталога.
- Manifest: `{"version", "transaction_id", "state", "policy_at_capture",
  "created_at", "files": [{"name", "size", "sha256"}]}` — `sha256` от **ciphertext**.
- Финальная запись манифеста — атомарно (tmp + `os.replace` + fsync каталога).

- [ ] **Step 4: GREEN** — та же команда, Expected: PASS.

- [ ] **Step 5: Коммит** (только если карточка явно разрешает — да, разрешает)

```bash
git add KrabEar/backend/encrypted_snapshot.py KrabEar/tests/test_a52b1_encrypted_snapshot.py
git commit -m "feat(a5.2b1): модуль encrypted snapshot — реестр 10 журналов + durable manifest"
```

---

### Task 2: Commit-протокол (fingerprint + COMMITTING + read-back)

**Files:**
- Modify: `KrabEar/backend/encrypted_snapshot.py`
- Test: `KrabEar/tests/test_a52b1_encrypted_snapshot.py`

- [ ] **Step 1: RED — три теста**
1. **Fingerprint mismatch**: источник изменился между prepare и commit
   (имитируй append в журнал) → commit **отказывается**, исходные файлы и
   существующие бэкапы не тронуты, причина машинно-читаема.
2. **Crash на COMMITTING**: состояние `COMMITTING` записано, но замены не
   завершены → повторный вход в `recover_pending_state()` (заглушка b1)
   возвращает **fail-closed признак** (никаких «успехов»), исходные журналы
   целы, snapshot на диске валиден и пригоден для докатки в b2.
3. **Read-back**: после commit каждый файл перечитывается и сверяется с
   манифестом (size + sha256); при расхождении — состояние НЕ `COMMITTED`.

- [ ] **Step 2: Run** — Expected: FAIL по правильной причине.

- [ ] **Step 3: Реализация**
```python
def commit_encrypted_snapshot(*, data_dir, backup_dir, transaction_id, ...) -> dict
def recover_pending_state(*, data_dir, backup_dir) -> dict
```
- `COMMITTING` — **до первой замены**, durable (atomic write + fsync).
- Повторная проверка fingerprint источников **перед** заменами.
- `COMMITTED` — только после успешного read-back **всех** файлов.
- `recover_pending_state` в b1: находит `COMMITTING`, **не** откатывает в
  plaintext, **не** создаёт новый ключ, возвращает `{"ok": False, "reason":
  "snapshot_recovery_pending", ...}` — явный fail-closed (b2 докачает).

- [ ] **Step 4: GREEN** — та же команда.
- [ ] **Step 5: Коммит** — `fix(a5.2b1): commit-протокол — fingerprint, COMMITTING, read-back, fail-closed recovery`.

---

### Task 3: Включение в manual + auto backup (только при ON)

**Files:**
- Modify: `KrabEar/backend/history_service.py` (`handle_backup_history`)
- Modify: `KrabEar/backend/auto_backup.py` (`_do_backup` / `check_and_backup`)
- Test: `KrabEar/tests/test_a52b1_encrypted_snapshot.py` (+ регресс существующих)

- [ ] **Step 1: RED**
1. **ON**: `handle_backup_history` создаёт encrypted snapshot (10 ENC1-файлов +
   манифест) и возвращает `ok=True` с путём; **ни одного** plaintext-файла
   в backup_dir; старые plaintext-копии не создаются.
2. **OFF-регресс**: поведение `handle_backup_history` и auto-backup **бит-в-бит
   прежнее** (старые тесты `test_backup_restore.py`, `test_auto_backup*` зелёные).
3. **ON + auto**: авто-бэкап при ON пишет тот же формат снимка; retention/prune
   **не удаляет** снимки, созданные новым протоколом, и не превращает отказ в
   ложный успех (`skipped_reason` наблюдаем).

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Реализация** — при `policy_blocks(...) == False` всё как раньше;
  при ON — новый путь под **тем же** store-lock, с повторной проверкой политики
  после захвата (контракт A5.2a не ослабляется: guard до lock + re-check под lock).
- [ ] **Step 4: GREEN** — новая карточка + регресс-набор.
- [ ] **Step 5: Коммит** — `feat(a5.2b1): encrypted snapshot в manual/auto backup при ON`.

---

## Гейты перед отчётом

```bash
# 1. новый набор
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52b1_encrypted_snapshot.py -q
# 2. регресс backup/restore/auto (A5.2a-набор не слабеет)
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_backup_restore.py KrabEar/tests/test_a52a_legacy_backup_gate.py \
  KrabEar/tests/test_data_migrator.py KrabEar/tests/test_purge_toctou_w25.py -q
# 3. локальный auto/backup
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_auto_backup.py \
  KrabEar/tests/test_auto_backup_advanced.py KrabEar/tests/test_auto_backup_lock_w1768.py -q
# 4. ubuntu-parity на изменённых тестах
scripts/pre_merge_py312_check.sh KrabEar/tests/test_a52b1_encrypted_snapshot.py
# 5. аудит
make audit-all
# 6. lint
.venv_krab_ear/bin/flake8 <изменённые source> --max-line-length=120 --ignore=E501,W503,E402
```

**RED-доказательство обязательно**: каждый Task — сначала падающий тест с
конкретной причиной (не ImportError), потом фикс.

## Что НЕ доказываем в b1

Живой backup при ON (нужен owner-флаг и реальный профиль), crash-recovery
(→ b2), inventory (→ A5.2c). Source-only; флаг OFF; деплой — отдельным решением.

## Tracked risks (не чинить в b1/b2 — владелец решает)

Открытые риски, зафиксированные по итогам adversarial-review b1. Ни один не
реализуется в этой волне — они переданы координатору вместе с diff'ом.

1. **Retention при ON выключен ⇒ снимки растут без ограничения `max_copies`.**
   Спека §6 требует gate до prune, а удаление инвентаризированных legacy
   plaintext-копий — решение A5.2c. Плата: при ON `backups/` растёт монотонно.
   Нужен отдельный wave: disk-full guard, иначе рост снимков может довести
   journal-запись до отказа (падение записи журнала = потеря истории).
   **Не при ON включать prune снимков «по умолчанию» без решения владельца:**
   это удалит единственный проверенный encrypted-экземпляр истории.
2. **`get_auto_backup_status()` при ON.** После b1 поле
   `encryption_operation_unavailable` описывает legacy plaintext-операцию, а не
   backup вообще. Семантика поля меняется в b2 вместе с UI-индикацией.
3. **Восстановление (restore) из снимков не реализовано** (b2). До b2
   `handle_restore_history` обязан отказывать на каталогах снимков — это
   сделано в b1 как защита от восстановления шифротекста поверх живой истории.
4. **Доказка recovery после crash на COMMITTING** — b2. `COMMITTING` в b1
   означает «durably записано ДО первой замены»; различать опубликованную
   транзакцию и оставшийся неопубликованный staging обязан b2 (см. контракт
   `recover_pending_state`).
