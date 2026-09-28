# A5.2c1 — purge: целостность профиля и полнота зачистки — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> (или executing-plans). Шаги — чекбоксы.

**Goal:** Два дефекта, найденных анализом волны A5.2b (не тестами, а разбором кода):

1. **Purge при ON ломает чтение истории.** Шаг 1b (компактирование) дописывает
   ENC1-строки в `history_purged_ids.ndjson` **старым** ключом; этот файл
   allowlisted и purge его **не** чистит; шаг 38 удаляет ключ → следующее чтение
   истории получает `InvalidTag` → `HistoryEncryptionUnavailable`. То есть после
   privacy purge профиль с шифрованием **нечитаем**. Теста на это нет.
2. **Purge не зачищает копии.** В `data_dir` лежат `history.ndjson.bak-*`
   (в прод-профиле владельца — 23.4 МБ **открытой** истории) и
   `settings.json.bak*` (шесть копий с непустыми секретами: `hf_token`,
   `sentry_dsn_agent`, `voice_gateway_api_key`, `stt_gigaam_hf_token`,
   `llm_api_key`, `lm_studio_api_key`). Ни один из них не попадает в зону purge
   (в коде нет glob'а на `*.bak*`) ⇒ privacy purge **оставляет** их на диске.

**Architecture:** purge приводит профиль к **консистентному** состоянию в один
вызов: сначала сносит все производные копии данных (включая permanent ledger и
`*.bak*`), затем удаляет ключ, и **честно сообщает** результат машинно-читаемо.
Ничего не удаляется «по сети» — только то, что является копией данных, которые
purge и так уничтожает.

**Tech Stack:** уже в репо — `handle_purge_all_data` (`backend/history_service.py`),
`crypto_keystore`, `health_check_service`, `purge_coverage_allowlist.txt`.
Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2` (с A5.2a/b1/b2/b3 + whisper-волной).
Worktree: `.worktrees/a52c1-purge-integrity`, ветка `codex/ear-a52c1-purge-integrity`.

**Карточка лежит рядом с этой карточкой в worktree** (её создаст координатор) —
закоммить первой строкой (provenance).

**Канон:** спека `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` §5
(«восстановление не должно уменьшать deletion ledger»), §6, и правило AGENTS.md
«`handle_purge_all_data` обязан подчищать любое новое персистентное хранилище» +
`scripts/audit_purge_coverage.py` как гейт полноты.

## Баны

- `history_encryption_enabled` в проде **OFF**; никаких live-операций и **никакого
  purge на реальном профиле** (все проверки — на synthetic tmp-профилях).
- Только synthetic tmp-профили + `os.urandom(32)`. **Keychain не трогать**:
  счётчик обращений к `security` (с разделением: чтение/создание vs удаление —
  удаление тут **ожидаемо**, purge по контракту shred'ит ключ) + OS-шим на
  `security` для доказательства нуля на остальных путях.
- `KrabEar/backend/service.py`, `KrabEar/core/engine.py`,
  `KrabEar/backend/state_store.py` — **не трогать** (диф к базе обязан быть пуст).
- Точечные прогоны; `git add` явными путями; **не пушить, не мержить**.

## Принятые решения (не переигрывать)

1. **Permanent ledger при purge удаляется**, а не перешифровывается. Обоснование:
   его единственная роль — блокировать resurrection ID'ов, которых история уже
   нет; после полного wipe внутри профиля он бессмысленен. **Восстановление из
   внешней копии** защищено ledger'ом **самой копии** — A5.2b2 при сборе union
   берёт `текущий ledger ∪ ledger снимка`, так что возвращённый извне старый снимок
   не сможет воскресить собственные удалённые ID. Это свойство надо **закрепить
   тестом**, а не только заявить в комментарии.
2. **`*.bak*`-копии истории и настроек в `data_dir` входят в зону purge.**
   Они являются копиями уничтожаемых данных; их сохранение противоречит смыслу
   операции. Удаление — `unlink` с явным перечислением паттернов
   (`history.ndjson.bak*`, `settings.json.bak*`), **не** широкий glob по `data_dir`.
3. **Порядок «данные → ключ»** сохраняется и усиливается: ledger и `.bak*` сносятся
   **до** шага удаления ключа. Если `.bak*` снос не удастся — это шаговая ошибка,
   а не повод пропустить ключ (ключ удаляется в любом случае — иначе профиль
   остался бы с читаемым прошлым, что хуже).
4. **Машинно-читаемый результат purge** расширяется: `encryption_key_shredded: bool`,
   `backups_deleted: int`, `deletion_ledger_purged: bool`, `stale_copies_removed: int`
   (сколько `*.bak*` убрано), плюс `history_encryption_enabled_after: bool`
   (пост-фактум из settings). **Флаг `history_encryption_enabled` не меняется** —
   решение владельца остаётся его; purge не переключает политику молча.
5. **Наблюдаемость ключа — в `get_diagnostics`** (`health_check_service.py`,
   разрешён к правке), НЕ в `service.py` (в бане): read-only проба
   «есть ли ключ» **без создания** ключа. Диагностика не должна иметь побочным
   эффектом восстановление ключа.
6. Вне скоупа (отдельными волнами): инвентаризация legacy-копий вне профиля
   (Time Machine и т.п.), крипто-shredding как опция, `restore_history` в UI.

---

### Task 1: Удаление permanent ledger в purge + зачистка `*.bak*`

**Files:**
- Modify: `KrabEar/backend/history_service.py` (`handle_purge_all_data`)
- Modify: `scripts/purge_coverage_allowlist.txt` (снять/переписать запись ledger'а)
- Test: `KrabEar/tests/test_a52c1_purge_integrity.py`

- [ ] **Step 1: RED**
1. **Главный кейс:** профиль ON (реальный `build_history_crypto` с тестовым ключом) →
   purge → **следующее** `get_history_page` **не падает** и возвращает пустую
   страницу (сегодня: `HistoryEncryptionUnavailable`). Обязателен RED именно с
   ротацией ключа, как в проде: ключ удаляется и создаётся заново.
2. `history_purged_ids.ndjson` отсутствует после purge.
3. `history.ndjson.bak-*`, `settings.json.bak*` удалены; **посторонние** файлы
   (`*.md`, `session.log`, `notes.txt`, каталоги) **не** тронуты.
4. Resurrection-защита внешней копии: ledger, взятый из возвращённого снимка,
   по-прежнему блокирует его собственные удалённые ID (тест на union из b2).
5. `make audit-all` не должен флажить (allowlist согласован с кодом).

- [ ] **Step 2: Run** — Expected: FAIL по правильной причине.
- [ ] **Step 3: Реализация** — шаг ledger'а и шаг `*.bak*` **до** шага ключа;
  запись `history_purged_ids.ndjson` из allowlist снимается с обоснованием
  в самом файле allowlist (комментарий обязателен).
- [ ] **Step 4: GREEN** — [ ] **Step 5: Commit** `fix(purge): permanent ledger и .bak-копии в зоне зачистки (A5.2c1)`.

---

### Task 2: Наблюдаемость purge + признак ключа

- [ ] **Step 1: RED**
1. Ответ purge содержит `encryption_key_shredded: True`, `backups_deleted: N`,
   `deletion_ledger_purged: True`, `stale_copies_removed: M`,
   `history_encryption_enabled_after: True` (флаг **не** изменился).
2. `get_diagnostics` отдаёт признак наличия ключа **без** его создания: на
   профиле без ключа — `false`, и после вызова ключ **не появляется**
   (проверить отсутствием побочного эффекта — это и есть смысл пробы).
3. Существующий контракт purge не сломан: прежние поля на месте,
   `confirm`-гейт работает (без confirm ничего не удалено).
4. Никаких новых секретов в ответе/логе.

- [ ] **Step 2–3**: реализация; документирование в `docs/IPC_API_REFERENCE.md`;
  новые поля проходят существующий AST-гейт паритета reason-кодов, если появятся
  новые причины.
- [ ] **Step 4–5**: GREEN; commit `feat(purge): машинно-читаемый результат и признак ключа в диагностике`.

## Гейты перед отчётом


```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_a52c1_purge_integrity.py -q
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_purge_all_data_w1730.py KrabEar/tests/test_purge_privacy_gaps_w1767.py \
  KrabEar/tests/test_a52b2_snapshot_restore.py KrabEar/tests/test_a52b3_disk_guard.py \
  KrabEar/tests/test_a52a_legacy_backup_gate.py -q
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_health_check*.py \
  KrabEar/tests/test_state_store*.py -q
scripts/pre_merge_py312_check.sh KrabEar/tests/test_a52c1_purge_integrity.py
make audit-all          # 🔴 обязателен: allowlist-гейт полноты purge
.venv_krab_ear/bin/flake8 <изменённые source/test> --max-line-length=120 --ignore=E501,W503,E402
```

**KEYCHAIN:** счётчики с разделением (удаление ключа в purge — **ожидаемо** и
помечается отдельно) + OS-шим на `security` для доказательства, что вне шага
purge обращений нет.

## Tracked risks (записано при выполнении, 2026-09-28)

1. **Найдено и закрыто в этой волне сверх карточки: fail-open в shred'е ключа.**
   `delete_history_key()` глотал неудачный exit code `security` (только лог) и
   возвращал `None`, а purge рапортовал `encryption_key_shredded: true` — при
   живом ключе. Теперь функция возвращает `bool` (fail-closed: отказ ⇒ `False`),
   и не shred'ённый ключ попадает в `errors`/`complete` (иначе W1749 loud-error
   обходил бы именно этот случай). Асимметрия зафиксирована тестом: отсутствие
   Keychain (Linux/CI) — **не** ошибка purge, иначе CI всегда «частичный».
2. **Инвентаризация копий вне профиля — НЕ сделана** (Time Machine, iCloud Drive,
   FS-снапшоты, старые `~/.local/share/krab-ear/releases/*`-копии). Purge чистит
   только то, что лежит в `data_dir`. Внешние копии — отдельная волна; пока purge
   их не видит, «зачистил» ≠ «удалил у всех носителей».
3. **Крипто-shredding как ОПЦИЯ (вместо безусловного удаления ключа) — не
   сделано.** Сейчас purge всегда shred'ит ключ, даже если владелец не просил
   «убить» шифрование. Обратная сторона решения владельца: вернуть историю,
   случайно уничтоженную purge'ом, уже нельзя.
4. **Реальный purge на профиле владельца — решение владельца.** Карточка даёт
   код + доказательства на synthetic tmp-профилях. До решения владельца: не
   запускать, профиль содержит реальные данные и реальные секреты.
5. **Копии `settings.json.bak*`/`history.ndjson.bak*` удаляются только в
   `data_dir` текущего профиля.** Копии, оставшиеся в старых release-worktree'ах
   или в бэкапах самого StateStore (`backups/migration_backup_*` — они уже в
   зоне purge), в этой волне не инвентаризованы.

## Что НЕ делаем

Живой purge на профиле владельца (это **решение владельца** и отдельный
инвентарь; карточка даёт только код и доказательства), крипто-shredding-опция,
инвентаризация копий вне профиля, разбиение `encrypted_snapshot.py`, append-гейт.

**Source-only; флаг OFF; деплой — отдельным решением.**
